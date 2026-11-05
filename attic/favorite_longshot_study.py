"""Favorite-longshot mining — is the bias the inefficiency map found tradeable?

The map (`inefficiency_sweep.py`) showed a venue-wide favorite-longshot shape:
longshots overpriced (priced ~0.15 resolve ~0.05), moderate favorites
underpriced (priced ~0.75 resolve ~0.89). This asks the only question that
matters for the leaderboard: does acting on it make money **net of fees and
spread**, or does the cost eat the edge?

Two hold-to-resolution rules, backtested on recently-closed markets:
  * FADE longshots — buy NO on markets priced in a low band (the overpriced
    cheap side); win the YES price if it resolves NO.
  * BACK favorites — buy YES on markets priced in a moderate-favorite band
    (the underpriced side); win (1 - price) if it resolves YES.

Honesty controls, because a gross gap is not an edge:
  * net of Kalshi fees (per-trade formula, sports vs standard rate by ticker),
  * a spread haircut on entry (cheap longshots have proportionally huge spread),
  * per-EVENT P&L concentration (one award/race = one winner, so trades within
    a field are correlated — a "win rate" hides that),
  * results broken out by market_type (binary vs multi-outcome field).

Reuses `inefficiency_sweep.fetch_calibration_data` at a SHORT lead (near close,
where you'd actually trade) — not the map's 24h lead, which is diffusely priced.
Read-only; no trades.

CLI:
  python favorite_longshot_study.py --since-days 14 --lead-hours 2
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import asdict, dataclass

import pandas as pd

import kalshi_fees
from inefficiency_sweep import classify, fetch_calibration_data

log = logging.getLogger(__name__)


# ─── Pure backtest (testable, no network) ──────────────────────────────────

@dataclass
class StudyResult:
    strategy: str          # "fade_longshot" | "back_favorite"
    band: str              # e.g. "0.02-0.20"
    market_type: str       # "all" | "binary" | "multi_outcome"
    n: int
    win_rate: float
    gross_roi: float       # before fees/spread
    net_roi: float         # after fees + spread haircut
    total_net: float       # net $ per 1-contract-per-trade
    n_events: int
    top_event_share: float  # |net of the single biggest event| / |total net|


def _fee_rate(ticker: str) -> float:
    try:
        return kalshi_fees.fee_rate_for_series(str(ticker))
    except Exception:  # noqa: BLE001
        return kalshi_fees.STANDARD_FEE_RATE


def simulate(
    df: pd.DataFrame,
    *,
    side: str,                # "yes" (back favorite) | "no" (fade longshot)
    price_lo: float,
    price_hi: float,
    half_spread: float = 0.01,
    strategy: str = "",
    market_type: str = "all",
) -> StudyResult | None:
    """Backtest one hold-to-resolution rule on a price band.

    `df` needs `predicted_prob` (entry YES price at the lead), `actual` (1.0 if
    YES won), `condition_id`, and optionally `event_id`. Returns None if no
    market falls in the band.

    P&L per 1 contract (Kalshi, hold to settlement — no exit fee):
      back YES: cost = price + half_spread; payoff 1 if actual==1 else 0
      fade NO:  cost = (1-price) + half_spread; payoff 1 if actual==0 else 0
      net = payoff - cost - entry_fee
    """
    if df is None or df.empty:
        return None
    sel = df[(df["predicted_prob"] >= price_lo)
             & (df["predicted_prob"] <= price_hi)].copy()
    if sel.empty:
        return None

    if side == "yes":
        sel["cost"] = (sel["predicted_prob"] + half_spread).clip(upper=0.99)
        sel["payoff"] = sel["actual"].astype(float)
    else:  # fade: buy NO
        sel["cost"] = (1.0 - sel["predicted_prob"] + half_spread).clip(upper=0.99)
        sel["payoff"] = 1.0 - sel["actual"].astype(float)

    sel["fee"] = [
        kalshi_fees.fee_for_trade(c, 1, fee_rate=_fee_rate(t))
        for c, t in zip(sel["cost"], sel["condition_id"], strict=False)
    ]
    sel["gross"] = sel["payoff"] - sel["cost"]      # pre-fee (spread still in cost)
    sel["net"] = sel["gross"] - sel["fee"]

    total_cost = float(sel["cost"].sum())
    total_net = float(sel["net"].sum())
    total_gross = float(sel["gross"].sum())
    gross_roi = total_gross / total_cost if total_cost else 0.0
    net_roi = total_net / total_cost if total_cost else 0.0

    # Per-event concentration: how much of |net| rides on the single biggest
    # event (one field = one winner → correlated trades).
    if "event_id" in sel.columns:
        by_ev = sel.groupby(sel["event_id"].astype(str))["net"].sum()
        n_events = int(by_ev.shape[0])
        denom = float(by_ev.abs().sum()) or 1.0
        top_event_share = float(by_ev.abs().max() / denom)
    else:
        n_events = len(sel)
        top_event_share = float("nan")

    return StudyResult(
        strategy=strategy or ("back_favorite" if side == "yes" else "fade_longshot"),
        band=f"{price_lo:.2f}-{price_hi:.2f}", market_type=market_type,
        n=int(len(sel)), win_rate=float((sel["payoff"] > 0).mean()),
        gross_roi=gross_roi, net_roi=net_roi, total_net=total_net,
        n_events=n_events, top_event_share=top_event_share,
    )


# Default bands to scan (entry YES price).
_FADE_BANDS = [(0.02, 0.10), (0.10, 0.20), (0.02, 0.20)]
_BACK_BANDS = [(0.55, 0.70), (0.70, 0.85), (0.55, 0.85)]


def run_grid(df: pd.DataFrame, *, half_spread: float) -> pd.DataFrame:
    """Run both rules across the default bands and market-type slices."""
    slices = {"all": df}
    if "market_type" in df.columns:
        for mt in ("binary", "multi_outcome"):
            slices[mt] = df[df["market_type"] == mt]

    rows: list[dict] = []
    for mt, sub in slices.items():
        for lo, hi in _FADE_BANDS:
            r = simulate(sub, side="no", price_lo=lo, price_hi=hi,
                         half_spread=half_spread, market_type=mt)
            if r:
                rows.append(asdict(r))
        for lo, hi in _BACK_BANDS:
            r = simulate(sub, side="yes", price_lo=lo, price_hi=hi,
                         half_spread=half_spread, market_type=mt)
            if r:
                rows.append(asdict(r))
    return pd.DataFrame(rows)


def format_report(grid: pd.DataFrame, *, params: dict, half_spread: float) -> str:
    lines = ["=" * 78, "FAVORITE-LONGSHOT MINING (hold-to-resolution, net of fees + spread)",
             f"  params: {params}  half_spread={half_spread}", "=" * 78, ""]
    lines.append("net_roi is the verdict. win_rate without net_roi is a trap (fades")
    lines.append("win often but small). top_event_share near 1.0 = the 'edge' is one")
    lines.append("correlated field, not a repeatable pattern.")
    lines.append("")
    hdr = (f"  {'strategy':<14}{'band':>11}{'mkt_type':>14}{'n':>5}"
           f"{'win%':>6}{'gross_roi':>10}{'net_roi':>9}{'net$':>8}{'evts':>6}{'top_ev':>7}")
    for mt in ("all", "binary", "multi_outcome"):
        g = grid[grid["market_type"] == mt] if not grid.empty else grid
        if g.empty:
            continue
        lines.append(f"[{mt}]")
        lines.append(hdr)
        for _, r in g.iterrows():
            tev = "—" if pd.isna(r["top_event_share"]) else f"{r['top_event_share']:.2f}"
            lines.append(
                f"  {r['strategy']:<14}{r['band']:>11}{r['market_type']:>14}"
                f"{int(r['n']):>5}{100 * r['win_rate']:>5.0f}%{100 * r['gross_roi']:>9.1f}%"
                f"{100 * r['net_roi']:>8.1f}%{r['total_net']:>8.1f}{int(r['n_events']):>6}{tev:>7}")
        lines.append("")
    lines.append("Read: a band with net_roi > 0, n and n_events both healthy, and")
    lines.append("top_event_share well below 1.0 is a real lead. Then validate live")
    lines.append("order-book depth before sizing — these are mid-price fills + a flat")
    lines.append("spread haircut, not guaranteed liquidity.")
    lines.append("=" * 78)
    return "\n".join(lines)


def run_study(args: argparse.Namespace) -> int:
    df_cal, n_fetched = fetch_calibration_data(
        since_days=args.since_days, until_days=args.until_days,
        min_volume=args.min_volume, lead_hours=args.lead_hours,
        max_markets=args.max_markets, refresh=args.refresh,
        provider_name=args.provider, pause_s=args.pause_s, batch=args.batch)
    if df_cal.empty:
        log.warning("no usable data")
        return 1
    df = classify(df_cal)
    grid = run_grid(df, half_spread=args.half_spread)
    report = format_report(
        grid, half_spread=args.half_spread,
        params={"since_days": args.since_days, "lead_hours": args.lead_hours,
                "min_volume": args.min_volume, "n": len(df), "fetched": n_fetched})
    print(report)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since-days", type=float, default=14.0)
    p.add_argument("--until-days", type=float, default=0.0)
    p.add_argument("--min-volume", type=float, default=100.0)
    p.add_argument("--lead-hours", type=float, default=2.0,
                   help="Near-close entry — where you'd actually trade.")
    p.add_argument("--max-markets", type=int, default=800)
    p.add_argument("--half-spread", type=float, default=0.01,
                   help="Entry haircut proxying the bid/ask you pay.")
    p.add_argument("--pause-s", type=float, default=15.0)
    p.add_argument("--batch", type=int, default=150)
    p.add_argument("--provider", default="kalshi")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    return run_study(args)


if __name__ == "__main__":
    sys.exit(main())
