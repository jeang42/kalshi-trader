"""De-correlated, cost-aware backtest of the fade-retail-longshot edge.

Refines the first-pass calibration (which suggested longshot overpricing in
non-sport/crypto retail markets) by fixing the two things that could inflate
it: (1) MULTI-OUTCOME CORRELATION — the markets are members of one-winner
fields (a primary's candidates), so the honest independent observation is the
EVENT, not the member; (2) COST — buying NO costs (1-price)+spread+fee.

event = Kalshi ticker minus its last '-<member>' segment (SERIES-EVENT-MEMBER).
Fade = buy NO on YES-in-band longshots, hold to resolution; NO pays $1 if the
outcome did NOT happen. Reports calibration, per-EVENT fade P&L (so correlated
members count once), independent-event count, concentration (top-event share),
and a spread-haircut sweep. Works offline from a saved get_market_calibration_data
CSV (cols: condition_id, question, category, predicted_prob, actual).

CLI: `python fade_retail_backtest.py --csv <path> [--lo 0.12 --hi 0.45]`.
"""
from __future__ import annotations

import argparse

import pandas as pd

import kalshi_fees


def _event_of(condition_id: str) -> str:
    parts = str(condition_id).rsplit("-", 1)
    return parts[0] if len(parts) == 2 else str(condition_id)


def _series_of(condition_id: str) -> str:
    return str(condition_id).split("-")[0]


def analyze(df: pd.DataFrame, *, lo: float = 0.12, hi: float = 0.45) -> None:
    df = df.dropna(subset=["predicted_prob", "actual"]).copy()
    df["predicted_prob"] = pd.to_numeric(df["predicted_prob"], errors="coerce")
    df["actual"] = pd.to_numeric(df["actual"], errors="coerce")
    df["event"] = df["condition_id"].map(_event_of)
    df["series"] = df["condition_id"].map(_series_of)
    n_events_all = df["event"].nunique()
    print(f"markets={len(df)}  distinct events={n_events_all}  "
          f"(members/event ~ {len(df)/max(n_events_all,1):.1f})\n")

    print("=== CALIBRATION (market-level) ===")
    for a, b in [(0.03, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.55),
                 (0.55, 0.80), (0.80, 0.97)]:
        s = df[(df["predicted_prob"] >= a) & (df["predicted_prob"] < b)]
        if len(s) < 6:
            continue
        print(f"  {a:.2f}-{b:.2f} n={len(s):>4} price={s['predicted_prob'].mean():.3f} "
              f"won={s['actual'].mean():.3f} gap={(s['actual'].mean()-s['predicted_prob'].mean())*100:+.1f}pp")

    # fade candidates
    fade = df[(df["predicted_prob"] >= lo) & (df["predicted_prob"] <= hi)].copy()
    print(f"\n=== FADE (buy NO) on YES in [{lo},{hi}]: {len(fade)} members "
          f"across {fade['event'].nunique()} independent events ===")

    for hs in (0.0, 0.02, 0.04):
        fade["no_cost"] = (1 - fade["predicted_prob"]) + hs
        fade["fee"] = fade["no_cost"].clip(0.01, 0.99).map(
            lambda c: kalshi_fees.fee_for_trade(c, 1))
        # per member, 1 contract: NO wins (payoff 1) when actual == 0
        fade["pnl"] = (fade["actual"] < 0.5).astype(float) - fade["no_cost"] - fade["fee"]
        # aggregate to INDEPENDENT events (correlated members -> one observation)
        ev = fade.groupby("event").agg(
            pnl=("pnl", "sum"), stake=("no_cost", "sum"), n=("pnl", "size")).reset_index()
        tot_pnl = ev["pnl"].sum()
        tot_stake = ev["stake"].sum()
        roi = tot_pnl / tot_stake if tot_stake else 0
        win_ev = (ev["pnl"] > 0).mean()
        top_share = ev["pnl"].abs().max() / ev["pnl"].abs().sum() if len(ev) else 0
        print(f"  spread {hs*100:.0f}c: events={len(ev)} member_fades={len(fade)} "
              f"net=${tot_pnl:+.2f} ROI={roi*100:+.1f}% win_by_event={win_ev*100:.0f}% "
              f"top1_event_share={top_share*100:.0f}%")

    # by category (which corners drive it)
    print("\n=== fade ROI by category (0c spread, event-agg) ===")
    for cat, g in fade.assign(
            no_cost=lambda x: 1 - x["predicted_prob"]).groupby("category"):
        g = g.copy()
        g["pnl"] = (g["actual"] < 0.5).astype(float) - g["no_cost"] - \
            g["no_cost"].clip(0.01, 0.99).map(lambda c: kalshi_fees.fee_for_trade(c, 1))
        evc = g.groupby("event").agg(pnl=("pnl", "sum"), stake=("no_cost", "sum"))
        if evc["stake"].sum() <= 0 or len(evc) < 2:
            continue
        print(f"  {str(cat)[:20]:20s} events={len(evc):>3} "
              f"ROI={evc['pnl'].sum()/evc['stake'].sum()*100:+.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--lo", type=float, default=0.12)
    ap.add_argument("--hi", type=float, default=0.45)
    args = ap.parse_args()
    analyze(pd.read_csv(args.csv), lo=args.lo, hi=args.hi)


if __name__ == "__main__":
    main()
