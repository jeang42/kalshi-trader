"""Live cross-market arbitrage scan — guaranteed-profit fields, net of cost.

A mutually-exclusive field (one award, one race — exactly one YES winner) whose
member prices don't sum to 1 is an arbitrage. The map hinted these exist, but it
used MID prices and ignored fees, which overstates everything. A *real* arb uses
the prices you actually transact at (bid/ask) and nets Kalshi's per-leg fee.

Two sides, with very different robustness:

  * OVERROUND — sum of YES *bids* > 1. Buy NO on every observed member. If the
    field is mutually exclusive, this is guaranteed profit **even if the field is
    incomplete** (a member we didn't see can only help: if it wins, all our NOs
    pay). Edge ≥ Σ(yes_bid) − 1 − fees. THE robust, trustworthy side.
  * UNDERROUND — sum of YES *asks* < 1. Buy YES on every member; one wins, pays
    $1. Edge = 1 − Σ(yes_ask) − fees. **Fragile**: needs the COMPLETE field — a
    missing winner means no payout, so a partial field looks like a false arb.

Caveats this can't fully resolve (so it flags, doesn't trade):
  * Mutual exclusivity is heuristic (field's mid-sum near 1) — confirm by reading
    the member questions before trading.
  * Top-of-book only — the edge is per one-contract-of-each; real size needs book
    depth, which a flagged candidate must be checked for.

Read-only. CLI:
  python arb_scan.py                 # scan top-volume open markets
  python arb_scan.py --min-edge 0.02
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pm.core import logctx
from pm.venue import (
    kalshi_client,  # noqa: F401 — registers Kalshi provider
    kalshi_fees,
)
from pm.venue.provider import get_provider

log = logging.getLogger(__name__)

ARB_LOG = Path("data/shared/arb_candidates.jsonl")


# Cumulative / nested thresholds ("Before <date>", "at least X", "above X") are
# NOT a one-winner partition — if an early one resolves YES, every later one
# does too, so multiple win and the buy-NO-all arb math breaks. (Disjoint
# "between X and Y" buckets ARE mutually exclusive, so they're NOT matched here.)
_CUMULATIVE_RX = re.compile(
    r"\bbefore\b|\bafter\b|\bat least\b|\bat most\b|\babove\b|\bbelow\b"
    r"|\bor more\b|\bor fewer\b|\bor higher\b|\bor lower\b|\bby\s", re.I)


def _is_cumulative_field(questions: pd.Series) -> bool:
    """True if most members read like nested thresholds (not a partition)."""
    if questions is None or questions.empty:
        return False
    hits = questions.astype(str).str.contains(_CUMULATIVE_RX, regex=True).mean()
    return bool(hits >= 0.5)


# ─── Pure scan (testable) ──────────────────────────────────────────────────

@dataclass
class ArbField:
    event_id: str
    event_title: str
    n_members: int
    sum_yes_bid: float
    sum_yes_ask: float
    mid_sum: float
    overround_edge: float    # $/contract-set, buy NO all, net fees (robust)
    underround_edge: float   # $/contract-set, buy YES all, net fees (needs full field)
    best_side: str           # "overround" | "underround" | "none"
    best_edge: float
    total_volume: float
    sample: str


def _fee(price: float, ticker: str) -> float:
    try:
        rate = kalshi_fees.fee_rate_for_series(str(ticker))
    except Exception:  # noqa: BLE001
        rate = kalshi_fees.STANDARD_FEE_RATE
    try:
        return kalshi_fees.fee_for_trade(price, 1, fee_rate=rate)
    except Exception:  # noqa: BLE001
        return 0.0


def find_arbs(
    df: pd.DataFrame,
    *,
    min_members: int = 2,
    min_edge: float = 0.01,
    me_band: tuple[float, float] = (0.90, 1.25),
    min_complete_bid_sum: float = 0.85,
) -> pd.DataFrame:
    """Scan a markets DataFrame for executable cross-market arbs.

    `df` needs `event_id`, `best_bid`, `best_ask`, and ideally `question`,
    `event_title`, `volume`, `condition_id`. Returns candidate fields sorted by
    best_edge descending. `me_band` is the mid-sum range a field must fall in to
    be treated as plausibly mutually-exclusive (excludes independent fields).
    """
    cols = [f.name for f in ArbField.__dataclass_fields__.values()]
    if df is None or df.empty or "event_id" not in df.columns:
        return pd.DataFrame(columns=cols)

    has_me_flag = "mutually_exclusive" in df.columns
    rows: list[dict] = []
    for ev, g in df.groupby("event_id"):
        if not str(ev) or len(g) < min_members:
            continue
        # Mutual exclusivity is the whole basis of the arb (exactly one YES
        # winner). Prefer Kalshi's authoritative `mutually_exclusive` flag; only
        # fall back to the mid-sum heuristic + cumulative filter when it's
        # absent (e.g. another provider). The flag eliminates both false-arb
        # classes: cumulative thresholds AND independent multi-select fields.
        if has_me_flag:
            if not bool(g["mutually_exclusive"].iloc[0]):
                continue
        elif "question" in g and _is_cumulative_field(g["question"]):
            continue
        # Valid two-sided quotes only.
        gg = g[(g["best_bid"] > 0) & (g["best_ask"] > 0)
               & (g["best_ask"] < 1)].copy()
        if len(gg) < min_members:
            continue
        bids = gg["best_bid"].astype(float)
        asks = gg["best_ask"].astype(float)
        tickers = gg.get("condition_id", pd.Series([""] * len(gg))).astype(str)

        sum_bid = float(bids.sum())
        sum_ask = float(asks.sum())
        mid_sum = float(((bids + asks) / 2).sum())
        # Heuristic ME band only when we lack the authoritative flag — with the
        # flag, a true one-winner field can sum anywhere and the band would
        # wrongly drop real arbs.
        if not has_me_flag and not (me_band[0] <= mid_sum <= me_band[1]):
            continue

        # Overround: buy NO on each (NO price = 1 - yes_bid). Robust to
        # incomplete fields. edge = Σbid - 1 - Σ fee(no leg).
        fee_no = sum(_fee(1.0 - b, t) for b, t in zip(bids, tickers, strict=False))
        overround_edge = sum_bid - 1.0 - fee_no
        # Underround: buy YES on each at ask. Needs the COMPLETE field — a
        # complete one-winner field's YES bids sum near 1, so a low bid-sum
        # means members are missing (often the dominant "status quo" outcome),
        # and buying YES on the partial basket would lose. Only trust it when
        # the field looks complete.
        if sum_bid >= min_complete_bid_sum:
            fee_yes = sum(_fee(a, t) for a, t in zip(asks, tickers, strict=False))
            underround_edge = 1.0 - sum_ask - fee_yes
        else:
            underround_edge = float("-inf")

        if overround_edge >= underround_edge:
            best_side, best_edge = "overround", overround_edge
        else:
            best_side, best_edge = "underround", underround_edge
        if best_edge < min_edge:
            continue

        title = str(gg.get("event_title", pd.Series([""])).iloc[0]
                    if "event_title" in gg else "")
        sample = " | ".join(str(q)[:40] for q in
                            gg.get("question", pd.Series([], dtype=str)).head(3))
        rows.append(asdict(ArbField(
            event_id=str(ev), event_title=title, n_members=int(len(gg)),
            sum_yes_bid=round(sum_bid, 4), sum_yes_ask=round(sum_ask, 4),
            mid_sum=round(mid_sum, 4), overround_edge=round(overround_edge, 4),
            underround_edge=(round(underround_edge, 4)
                             if underround_edge != float("-inf") else float("nan")),
            best_side=best_side,
            best_edge=round(best_edge, 4),
            total_volume=float(gg.get("volume", pd.Series([0])).sum()),
            sample=sample)))

    out = pd.DataFrame(rows, columns=cols)
    if out.empty:
        return out
    return out.sort_values("best_edge", ascending=False).reset_index(drop=True)


def format_report(arbs: pd.DataFrame, *, n_markets: int, n_fields: int) -> str:
    lines = ["=" * 78, "LIVE CROSS-MARKET ARB SCAN (executable bid/ask, net of fees)",
             f"  scanned {n_markets} open markets across {n_fields} fields",
             "=" * 78, ""]
    if arbs.empty:
        lines.append("No arbitrage candidates clear the edge threshold after fees.")
        lines.append("(Expected — real arbs are rare/fleeting. The robust side is")
        lines.append("OVERROUND: Σ yes_bid > 1 → buy NO on all members.)")
        lines.append("=" * 78)
        return "\n".join(lines)
    lines.append("OVERROUND (buy NO all) is robust to incomplete fields; UNDERROUND")
    lines.append("(buy YES all) needs the COMPLETE field. Confirm mutual exclusivity")
    lines.append("by reading members + check book DEPTH before trading.")
    lines.append("")
    lines.append(f"  {'side':<11}{'edge$':>7}{'n':>4}{'bid_sum':>9}{'ask_sum':>9}"
                 f"{'vol':>9}  event / sample")
    for _, r in arbs.iterrows():
        lines.append(
            f"  {r['best_side']:<11}{r['best_edge']:>+7.3f}{int(r['n_members']):>4}"
            f"{r['sum_yes_bid']:>9.3f}{r['sum_yes_ask']:>9.3f}{r['total_volume']:>9.0f}"
            f"  {(r['event_title'] or r['event_id'])[:32]} :: {r['sample'][:50]}")
    lines.append("")
    lines.append("Each edge is per ONE contract of each member. Verify depth +")
    lines.append("mutual exclusivity before sizing. Not auto-traded.")
    lines.append("=" * 78)
    return "\n".join(lines)


def _alert_candidates(arbs: pd.DataFrame, *, seen: set[str] | None = None) -> int:
    """Append candidates to the JSONL audit trail and log NEW ones loudly.
    `seen` (event_ids already alerted this process) suppresses repeat console
    spam across loop cycles; the JSONL keeps the full time series regardless."""
    if arbs is None or arbs.empty:
        return 0
    ts = datetime.now(UTC).isoformat()
    ARB_LOG.parent.mkdir(parents=True, exist_ok=True)
    n_new = 0
    with open(ARB_LOG, "a") as f:
        for _, r in arbs.iterrows():
            rec = {"ts": ts, **{k: r[k] for k in r.index}}
            f.write(json.dumps(rec, default=str) + "\n")
            ev = str(r["event_id"])
            if seen is None or ev not in seen:
                n_new += 1
                log.warning("ARB CANDIDATE %s edge=$%+.3f n=%d (%s) — CONFIRM "
                            "mutual-exclusivity + depth before trading",
                            r["best_side"], r["best_edge"], int(r["n_members"]),
                            (r["event_title"] or ev)[:48])
                if seen is not None:
                    seen.add(ev)
    return n_new


def _scan_once(prov, *, limit: int, min_members: int, min_edge: float,
               min_field_volume: float) -> pd.DataFrame:
    df = prov.get_markets(limit=limit)
    if df is None or df.empty:
        return pd.DataFrame()
    arbs = find_arbs(df, min_members=min_members, min_edge=min_edge)
    if not arbs.empty and min_field_volume > 0:
        arbs = arbs[arbs["total_volume"] >= min_field_volume].reset_index(drop=True)
    return arbs


def run_scan(args: argparse.Namespace) -> int:
    prov = get_provider(args.provider)
    df = prov.get_markets(limit=args.limit)
    if df is None or df.empty:
        log.warning("no markets fetched")
        return 1
    n_fields = df["event_id"].nunique() if "event_id" in df else 0
    arbs = find_arbs(df, min_members=args.min_members, min_edge=args.min_edge)
    if not arbs.empty and args.min_field_volume > 0:
        arbs = arbs[arbs["total_volume"] >= args.min_field_volume].reset_index(drop=True)
    _alert_candidates(arbs)
    print(format_report(arbs, n_markets=len(df), n_fields=int(n_fields)))
    return 0


def run_loop(args: argparse.Namespace) -> int:
    prov = get_provider(args.provider)
    seen: set[str] = set()
    log.info("arb_scan loop starting (limit=%d, interval=%ds, min_edge=$%.3f)",
             args.limit, args.interval, args.min_edge)
    while True:
        try:
            arbs = _scan_once(prov, limit=args.limit, min_members=args.min_members,
                              min_edge=args.min_edge,
                              min_field_volume=args.min_field_volume)
            n_new = _alert_candidates(arbs, seen=seen)
            if not arbs.empty:
                log.info("scan: %d candidate(s), %d new", len(arbs), n_new)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("scan cycle failed: %s", e)
        time.sleep(max(30, args.interval))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=3000,
                   help="How many open markets to scan (volume-ordered). Higher "
                        "exposes thinner, less-watched fields where arbs survive.")
    p.add_argument("--min-members", type=int, default=2)
    p.add_argument("--min-edge", type=float, default=0.01,
                   help="Min net edge ($/contract-set) to report.")
    p.add_argument("--min-field-volume", type=float, default=0.0,
                   help="Optional: require this much total field volume (depth "
                        "proxy) — filters dead fields with stale-quote pseudo-arbs.")
    p.add_argument("--loop", action="store_true",
                   help="Run forever, alerting new candidates each cycle.")
    p.add_argument("--interval", type=int, default=300)
    p.add_argument("--provider", default="kalshi")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup(args.log_level.upper())
    return run_loop(args) if args.loop else run_scan(args)


if __name__ == "__main__":
    sys.exit(main())
