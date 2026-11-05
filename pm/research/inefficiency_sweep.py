"""Inefficiency sweep — map WHERE Kalshi is mispriced, across all sectors.

The strategy search so far tested one market at a time (MLB game-winner) and
found it efficient. This flips the question: instead of "is this strategy
profitable?", ask "where across the whole venue is the market exploitably
mispriced?" — then point strategies at that corner.

Method: for a broad sample of recently-closed markets, take each market's YES
price at a fixed lead before close as its forecast, compare to the actual
outcome, and per-category measure **calibration deviation** — does the observed
resolution rate match the priced rate, bucketed by probability?

Why calibration deviation and not raw Brier: Brier conflates genuine
uncertainty with mispricing (a perfectly-priced coin flip still scores ~0.25).
A *signed* gap between observed rate and priced rate in a probability bucket is
the exploitable signal: if markets priced 0.20 resolve YES 35% of the time,
that's a +0.15 edge to buy YES there. The output is a ranked map of those gaps.

Read-only. No trades, no writes outside the report/cache files.

CLI:
  python inefficiency_sweep.py --since-days 120 --min-volume 100 \
      --lead-hours 24 --max-markets 4000
  python inefficiency_sweep.py --refresh          # ignore the cached fetch
  python inefficiency_sweep.py --category Crypto   # drill one category
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pm.core import logctx, provenance
from pm.ledger import calibration
from pm.venue import kalshi_client  # noqa: F401 — registers the Kalshi provider on import
from pm.venue.provider import get_provider

log = logging.getLogger(__name__)

CACHE_DIR = Path("data/shared/inefficiency_cache")
REPORT_PATH = Path("data/shared/inefficiency_sweep_report.txt")


# ─── Pure analysis (testable, no network) ──────────────────────────────────

# "Will X get between 40% and 45%", "at least 60%", date/number buckets — these
# are scalar ranges over a continuous outcome, not yes/no questions.
_SCALAR_RX = re.compile(
    r"between \d|at least \d|at most \d|or more|or fewer|or higher|or lower"
    r"|% of the|of the (?:popular )?vote", re.I)


def _group_key(ticker: str) -> str:
    """Kalshi ticker SERIES-EVENT-MARKET → SERIES-EVENT (the mutually-exclusive
    group). Genuine yes/no markets (SERIES-YEAR) collapse to themselves."""
    s = str(ticker)
    return s.rsplit("-", 1)[0] if "-" in s else s


def classify(df_cal: pd.DataFrame) -> pd.DataFrame:
    """Tag each market binary vs multi_outcome, plus a scalar-range flag.

    A `multi_outcome` market is one of >=2 mutually-exclusive sub-markets that
    share an event prefix (one award, one race) — binary per-market calibration
    does NOT apply to those (only one of the group wins by construction); they
    need the overround lens instead. `binary` markets are standalone yes/no.

    Caveat: if the volume filter or fetch cap dropped a group's siblings, a true
    member can look standalone here. `multi_outcome_overround` reports
    mean_winners per group as a data-quality check (should be ~1.0).
    """
    if df_cal is None or df_cal.empty:
        return df_cal.copy() if df_cal is not None else pd.DataFrame()
    df = df_cal.copy()
    # Prefer Kalshi's real event_ticker (carried as event_id) — it's the exact
    # mutually-exclusive group. Fall back to the ticker-prefix heuristic only
    # where it's missing (older caches, non-Kalshi providers).
    if "event_id" in df.columns:
        ev = df["event_id"].astype(str)
        df["group_key"] = ev.where(ev.str.len() > 0,
                                   df["condition_id"].map(_group_key))
    else:
        df["group_key"] = df["condition_id"].map(_group_key)
    df["group_size"] = df.groupby("group_key")["condition_id"].transform("size")
    df["is_scalar"] = df["question"].astype(str).str.contains(_SCALAR_RX, regex=True)
    df["market_type"] = df["group_size"].map(
        lambda s: "multi_outcome" if s >= 2 else "binary")
    return df


def multi_outcome_overround(
    df_classified: pd.DataFrame, *, min_groups: int = 5,
) -> pd.DataFrame:
    """Per-category overround for genuinely mutually-exclusive groups.

    overround = (sum of member YES prices) - 1 — the vig you'd collect selling
    the whole group, and the fade-able edge if positive. Only meaningful for
    ONE-WINNER fields, so we score only groups that resolved to exactly one
    winner. Groups with 0 winners are incomplete samples (the winning member
    got filtered out → overround understated); groups with >=2 winners aren't
    mutually exclusive (independent yes/no markets sharing an event, e.g.
    streaming thresholds) → overround is meaningless. Both are reported as
    dropped counts so the exclusion is visible, not silent.
    """
    if df_classified is None or df_classified.empty \
            or "market_type" not in df_classified:
        return pd.DataFrame()
    mo = df_classified[df_classified["market_type"] == "multi_outcome"]
    # Exclude scalar/range fields (vote-share / number buckets): their members
    # span a continuum and their pre-close prices are diffuse, so summing them
    # is not a tradeable "fade the field" overround the way an award/race is.
    if "is_scalar" in mo.columns:
        mo = mo[~mo["is_scalar"].astype(bool)]
    if mo.empty:
        return pd.DataFrame()
    grp = mo.groupby(["category", "group_key"]).agg(
        members=("condition_id", "size"),
        price_sum=("predicted_prob", "sum"),
        winners=("actual", "sum"),
    ).reset_index()
    grp["overround"] = grp["price_sum"] - 1.0
    grp["winners"] = grp["winners"].round().astype(int)

    rows = []
    for cat, g in grp.groupby("category"):
        excl = g[g["winners"] == 1]          # clean one-winner fields
        if len(excl) < min_groups:
            continue
        rows.append({
            "category": str(cat), "n_excl_groups": int(len(excl)),
            "mean_overround": float(excl["overround"].mean()),
            "pct_fadeable": float((excl["overround"] > 0).mean()),
            "mean_members": float(excl["members"].mean()),
            "dropped_incomplete": int((g["winners"] == 0).sum()),
            "dropped_nonexcl": int((g["winners"] >= 2).sum()),
        })
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            .sort_values("mean_overround", ascending=False)
            .reset_index(drop=True))


@dataclass
class CategoryEdge:
    category: str
    n: int
    brier: float | None          # market-baseline Brier over the category
    edge_score: float            # n-weighted mean |observed - priced| over bins
    worst_bin_center: float      # the most-mispriced probability bucket
    worst_bin_n: int
    worst_bin_gap: float         # observed_rate - mean_predicted (signed)


def category_edges(
    df_cal: pd.DataFrame,
    *,
    n_bins: int = 10,
    min_bin_n: int = 10,
    min_cat_n: int = 30,
) -> pd.DataFrame:
    """Per-category exploitable-mispricing map from a calibration dataset.

    `df_cal` needs columns `category`, `predicted_prob`, `actual`. Returns one
    row per category (>= ``min_cat_n`` resolved markets) sorted by
    ``edge_score`` descending — the most mispriced sectors first.

    edge_score = sum(n_bin * |observed - priced|) / sum(n_bin) over bins with
    at least ``min_bin_n`` markets. It's the average exploitable gap a trader
    would see, weighted by how often each bucket occurs.

    >>> import pandas as pd
    >>> # a category priced 0.2 that actually resolves 0.2 → no edge
    >>> fair = pd.DataFrame({"category": ["F"] * 40,
    ...     "predicted_prob": [0.2] * 40,
    ...     "actual": [1.0] * 8 + [0.0] * 32})
    >>> e = category_edges(fair, min_cat_n=10, min_bin_n=5)
    >>> round(float(e.loc[e.category == "F", "edge_score"].iloc[0]), 3)
    0.0
    """
    rows: list[dict] = []
    if df_cal is None or df_cal.empty:
        return pd.DataFrame(columns=[f.name for f in CategoryEdge.__dataclass_fields__.values()])

    for cat, g in df_cal.groupby("category"):
        if len(g) < min_cat_n:
            continue
        brier = calibration.market_brier_score(g)
        bins = calibration.market_calibration_bins(g, n_bins=n_bins)
        if not bins.empty:
            bins = bins[bins["n"] >= min_bin_n].copy()
        if bins.empty:
            rows.append({
                "category": str(cat), "n": int(len(g)), "brier": brier,
                "edge_score": 0.0, "worst_bin_center": float("nan"),
                "worst_bin_n": 0, "worst_bin_gap": 0.0,
            })
            continue
        bins["gap"] = bins["observed_rate"] - bins["mean_predicted"]
        edge_score = float((bins["n"] * bins["gap"].abs()).sum() / bins["n"].sum())
        worst = bins.loc[bins["gap"].abs().idxmax()]
        rows.append({
            "category": str(cat), "n": int(len(g)), "brier": brier,
            "edge_score": edge_score,
            "worst_bin_center": float(worst["bin_center"]),
            "worst_bin_n": int(worst["n"]),
            "worst_bin_gap": float(worst["gap"]),
        })

    cols = [f.name for f in CategoryEdge.__dataclass_fields__.values()]
    out = pd.DataFrame(rows, columns=cols)
    if out.empty:
        return out
    return out.sort_values("edge_score", ascending=False).reset_index(drop=True)


def format_report(
    df_class: pd.DataFrame,
    edges: pd.DataFrame,
    overround: pd.DataFrame,
    *,
    params: dict,
    n_fetched: int,
    n_bins: int = 10,
) -> str:
    """Human-readable inefficiency map. `df_class` is the classified dataset
    (from `classify`), `edges` the binary-only map, `overround` the multi-
    outcome group summary."""
    lines: list[str] = []
    binary = df_class[df_class["market_type"] == "binary"]
    multi = df_class[df_class["market_type"] == "multi_outcome"]

    lines.append("=" * 72)
    lines.append("KALSHI INEFFICIENCY SWEEP")
    lines.append(f"  generated: {datetime.now(UTC).isoformat(timespec='seconds')}")
    lines.append(f"  params: {params}")
    lines.append(f"  markets fetched: {n_fetched}  |  usable price@lead: {len(df_class)}")
    lines.append(f"  composition: {len(binary)} binary  |  {len(multi)} multi-outcome"
                 f"  ({int(df_class['is_scalar'].sum())} scalar/range)")
    if not df_class.empty:
        lines.append(f"  overall market-baseline Brier (all): "
                     f"{calibration.market_brier_score(df_class):.4f}")
    if not binary.empty:
        lines.append(f"  market-baseline Brier (binary only): "
                     f"{calibration.market_brier_score(binary):.4f}")
    lines.append("=" * 72)
    lines.append("")

    # ── Section 1: BINARY calibration map (the trustworthy per-market view) ──
    lines.append("[1] BINARY MARKET MISPRICING (standalone yes/no only)")
    lines.append("  edge_score = n-weighted avg |observed - priced| gap;")
    lines.append("  worst_bin_gap > 0 = bucket resolves YES more than priced (buy YES).")
    lines.append(f"  {'category':<14}{'n':>6}{'brier':>8}{'edge':>8}"
                 f"{'worst_bin':>11}{'gap':>8}{'bin_n':>7}")
    if edges.empty:
        lines.append("  (no category cleared the minimum sample)")
    for _, r in edges.iterrows():
        wb = "—" if pd.isna(r["worst_bin_center"]) else f"{r['worst_bin_center']:.2f}"
        br = "—" if r["brier"] is None or pd.isna(r["brier"]) else f"{r['brier']:.4f}"
        lines.append(
            f"  {r['category']:<14}{int(r['n']):>6}{br:>8}{r['edge_score']:>8.3f}"
            f"{wb:>11}{r['worst_bin_gap']:>+8.3f}{int(r['worst_bin_n']):>7}")
    lines.append("")
    lines.append("  calibration detail — top binary sectors:")
    top_cats = list(edges["category"].head(5)) if "category" in edges else []
    for cat in top_cats:
        g = binary[binary["category"] == cat]
        bins = calibration.market_calibration_bins(g, n_bins=n_bins)
        lines.append(f"    [{cat}]  n={len(g)}    {'priced':>8}{'observed':>10}{'gap':>8}{'n':>6}")
        for _, b in bins.iterrows():
            gap = b["observed_rate"] - b["mean_predicted"]
            lines.append(f"    {'':>{len(cat) + 12}}{b['mean_predicted']:>8.2f}"
                         f"{b['observed_rate']:>10.2f}{gap:>+8.2f}{int(b['n']):>6}")
    lines.append("")

    # ── Section 2: MULTI-OUTCOME overround (the right lens for groups) ──
    lines.append("[2] ONE-WINNER FIELD OVERROUND (clean mutually-exclusive groups)")
    lines.append("  overround = sum(member YES prices) - 1 = fade-able vig if > 0.")
    lines.append("  Scored only on groups that resolved to exactly one winner.")
    lines.append("  dropped: incomplete = winner filtered out (understates vig);")
    lines.append("  nonexcl = >=2 winners (independent markets, overround N/A).")
    lines.append(f"  {'category':<14}{'groups':>7}{'overrnd':>9}{'fadeable%':>10}"
                 f"{'members':>8}{'drop_inc':>9}{'drop_nx':>8}")
    if overround.empty:
        lines.append("  (no category had enough clean one-winner groups)")
    for _, r in overround.iterrows():
        lines.append(
            f"  {r['category']:<14}{int(r['n_excl_groups']):>7}"
            f"{r['mean_overround']:>+9.3f}{100 * r['pct_fadeable']:>9.0f}%"
            f"{r['mean_members']:>8.1f}{int(r['dropped_incomplete']):>9}"
            f"{int(r['dropped_nonexcl']):>8}")
    lines.append("")

    # ── Section 3: venue-wide longshot calibration (favorite-longshot bias) ──
    lines.append("[3] VENUE-WIDE CALIBRATION (favorite-longshot check, all markets)")
    lines.append(f"    {'priced':>8}{'observed':>10}{'gap':>8}{'n':>7}")
    for _, b in calibration.market_calibration_bins(df_class, n_bins=n_bins).iterrows():
        gap = b["observed_rate"] - b["mean_predicted"]
        lines.append(f"    {b['mean_predicted']:>8.2f}{b['observed_rate']:>10.2f}"
                     f"{gap:>+8.2f}{int(b['n']):>7}")
    lines.append("")
    lines.append("Next: a real edge must survive fees + thin-book liquidity. Binary")
    lines.append("gaps and positive overround are leads, not fills — validate before")
    lines.append("trading. Multi-outcome overround is the favorite-longshot hunting ground.")
    lines.append("=" * 72)
    return "\n".join(lines)


# ─── Fetch + cache (network) ───────────────────────────────────────────────

def _cache_path(params: dict) -> Path:
    key = "|".join(f"{k}={params[k]}" for k in sorted(params))
    h = hashlib.sha256(key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"cal_{h}.csv"


def _fetch_closed(
    prov, *, since_days: float, until_days: float, min_volume: float,
    max_markets: int,
) -> pd.DataFrame:
    """Pull closed markets for a recent window.

    NOTE on scope: Kalshi's settled-**events** endpoint returns newest-first
    and `get_closed_markets_in_range` walks from *now* until it fills its
    event cap. Date-chunking into OLD windows therefore can't work — reaching
    N days back means paging through everything newer first, which exhausts
    the cap (→ 0 markets) and trips 429. So this is deliberately a RECENT-
    window sweep: pick `since_days` small enough that the cap actually covers
    it. For a deeper historical map we'd need a different data path (e.g. the
    trade-tape archive), tracked as future work."""
    try:
        df = prov.get_closed_markets_in_range(
            since_days=since_days, until_days=until_days,
            max_items=max_markets, min_volume=min_volume)
    except Exception as e:  # noqa: BLE001 — surface but don't crash the idler
        log.warning("closed-markets fetch failed (%s)", type(e).__name__)
        return pd.DataFrame()
    return df.drop_duplicates(subset="condition_id").head(max_markets)


def fetch_calibration_data(
    *,
    since_days: float,
    until_days: float,
    min_volume: float,
    lead_hours: float,
    max_markets: int,
    refresh: bool,
    provider_name: str,
    pause_s: float = 15.0,
    batch: int = 150,
) -> tuple[pd.DataFrame, int]:
    """Return (per-market calibration df, n_markets_fetched). Cached to disk so
    the slow per-market price-history fetch only happens once per param set.
    Price sampling is batched + paced to stay under Kalshi's rate limit."""
    params = {
        "since_days": since_days, "until_days": until_days,
        "min_volume": min_volume, "lead_hours": lead_hours,
        "max_markets": max_markets, "provider": provider_name,
    }
    cache = _cache_path(params)
    if cache.exists() and not refresh:
        df_cal = pd.read_csv(cache)
        log.info("loaded cached calibration data: %d rows (%s)", len(df_cal), cache)
        return df_cal, len(df_cal)

    prov = get_provider(provider_name)
    log.info("fetching closed markets %s..%s days back (min_volume=%s) via %s",
             since_days, until_days, min_volume, prov.name)

    df_closed = _fetch_closed(
        prov, since_days=since_days, until_days=until_days,
        min_volume=min_volume, max_markets=max_markets)
    n_fetched = len(df_closed)
    log.info("fetched %d closed real markets; sampling price@%sh before close",
             n_fetched, lead_hours)
    if df_closed.empty:
        return pd.DataFrame(), 0

    # Price sampling — also batched + paced (one price-history call/market).
    cal_frames: list[pd.DataFrame] = []
    total = len(df_closed)
    for b0 in range(0, total, batch):
        chunk = df_closed.iloc[b0:b0 + batch]
        try:
            part = prov.get_market_calibration_data(
                chunk, lead_hours=lead_hours, max_markets=batch)
            if not part.empty:
                cal_frames.append(part)
            log.info("  price sampling: %d/%d (usable so far: %d)",
                     min(b0 + batch, total), total,
                     sum(len(f) for f in cal_frames))
        except Exception as e:  # noqa: BLE001
            log.warning("  price batch %d failed (%s) — skipping",
                        b0, type(e).__name__)
        if b0 + batch < total and pause_s > 0:
            time.sleep(pause_s)

    df_cal = (pd.concat(cal_frames, ignore_index=True)
              if cal_frames else pd.DataFrame())
    # Carry Kalshi's real event_ticker (event_id) so classify() can group
    # mutually-exclusive markets exactly, rather than by ticker-prefix guess.
    if not df_cal.empty and "event_id" in df_closed.columns:
        df_cal = df_cal.merge(
            df_closed[["condition_id", "event_id"]].drop_duplicates("condition_id"),
            on="condition_id", how="left")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df_cal.to_csv(cache, index=False)
    log.info("wrote calibration data: %d usable rows -> %s", len(df_cal), cache)
    return df_cal, n_fetched


def run_sweep(args: argparse.Namespace) -> int:
    df_cal, n_fetched = fetch_calibration_data(
        since_days=args.since_days, until_days=args.until_days,
        min_volume=args.min_volume, lead_hours=args.lead_hours,
        max_markets=args.max_markets, refresh=args.refresh,
        provider_name=args.provider, pause_s=args.pause_s, batch=args.batch)

    if df_cal.empty:
        log.warning("no usable calibration data — nothing to map")
        return 1

    if args.category:
        df_cal = df_cal[df_cal["category"] == args.category]
        if df_cal.empty:
            log.warning("no markets in category %r", args.category)
            return 1

    df_class = classify(df_cal)
    binary = df_class[df_class["market_type"] == "binary"]
    edges = category_edges(
        binary, n_bins=args.n_bins, min_bin_n=args.min_bin_n,
        min_cat_n=args.min_cat_n)
    overround = multi_outcome_overround(df_class, min_groups=args.min_groups)
    report = format_report(
        df_class, edges, overround, n_bins=args.n_bins, n_fetched=n_fetched,
        params={"since_days": args.since_days, "min_volume": args.min_volume,
                "lead_hours": args.lead_hours, "max_markets": args.max_markets})

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(provenance.stamp_line() + "\n" + report + "\n")
    print(report)
    log.info("report written to %s", REPORT_PATH)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since-days", type=float, default=21.0,
                   help="Recent-window lookback. Kept modest because the "
                        "settled-events endpoint is newest-first (see "
                        "_fetch_closed) — deep history isn't reachable here.")
    p.add_argument("--until-days", type=float, default=0.0)
    p.add_argument("--min-volume", type=float, default=100.0,
                   help="Skip thin/synthetic markets (parlays have 0 volume).")
    p.add_argument("--lead-hours", type=float, default=24.0,
                   help="Sample the price this long before close as the forecast.")
    p.add_argument("--max-markets", type=int, default=4000)
    p.add_argument("--n-bins", type=int, default=10)
    p.add_argument("--min-bin-n", type=int, default=10)
    p.add_argument("--min-cat-n", type=int, default=30)
    p.add_argument("--min-groups", type=int, default=5,
                   help="Min mutually-exclusive groups for a category's overround.")
    p.add_argument("--category", default=None, help="Drill a single category.")
    p.add_argument("--pause-s", type=float, default=15.0,
                   help="Cooldown between price-sample batches.")
    p.add_argument("--batch", type=int, default=150,
                   help="Price-sample this many markets per batch before pausing.")
    p.add_argument("--provider", default="kalshi")
    p.add_argument("--refresh", action="store_true",
                   help="Ignore the cached fetch and re-pull.")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup(args.log_level.upper())
    return run_sweep(args)


if __name__ == "__main__":
    sys.exit(main())
