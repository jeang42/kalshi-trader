"""Backtest the early-exit take-profit vs hold-to-resolution on the retail fade.

The open question the live arm can't answer yet: do overpriced retail longshots
CORRECT before resolution (so the take-profit fires and frees capital fast), or
stay overpriced until the event (so early-exit ~ hold)? Answer it on historical
price paths.

For each fade candidate (from a get_market_calibration_data CSV: condition_id,
predicted_prob@72h, actual), pull the executable price path over the final days
(raw Kalshi hourly candlesticks -> yes_bid/yes_ask close per hour), then simulate:
  ENTRY at ~72h lead: buy NO at (1 - yes_bid).
  WALK the path: sell-value = (1 - yes_ask); if it captures >= TP_FRACTION of the
    move (entry -> $1), EARLY EXIT there (round-trip fees).
  ELSE hold to resolution (NO pays $1 if actual==NO; single fee).
Compares hold vs early-exit ROI, %-of-positions the TP triggered, and hold-time
saved. Rate-limit-paced (per-market candle fetch); run scoped.

CLI: `python fade_early_exit_backtest.py --csv <cal.csv> [--tp 0.6 --lo 0.12 --hi 0.45]`.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime

import pandas as pd

import kalshi_client as kc
import kalshi_fees

FEE_RATE = 0.07  # retail = standard Kalshi fee


def _series(ticker: str) -> str:
    return str(ticker).split("-")[0]


def _close_map(since_days: int) -> dict:
    """ticker -> close unix, from the settled-markets metadata."""
    df = kc.get_closed_markets_in_range(since_days=since_days, max_items=4000)
    out = {}
    tcol = "id" if "id" in df.columns else "ticker"
    for _, r in df.iterrows():
        ct = r.get("close_time") or r.get("closed_time")
        try:
            out[str(r[tcol])] = int(datetime.fromisoformat(
                str(ct).replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
    return out


def _price_path(ticker: str, close_ts: int, *, days_before: int = 6):
    """[(secs_to_close, yes_bid, yes_ask)] hourly over the final window."""
    try:
        data = kc._get(
            f"series/{_series(ticker)}/markets/{ticker}/candlesticks",
            params={"period_interval": 60, "start_ts": close_ts - days_before * 86400,
                    "end_ts": close_ts + 3600})
    except Exception:
        return []
    out = []
    for c in data.get("candlesticks") or []:
        yb = (c.get("yes_bid") or {}).get("close_dollars")
        ya = (c.get("yes_ask") or {}).get("close_dollars")
        try:
            yb, ya = float(yb), float(ya)
        except (TypeError, ValueError):
            continue
        if yb <= 0 or ya <= 0 or yb > ya:
            continue
        out.append((c["end_period_ts"] - close_ts, yb, ya))
    return sorted(out)


def simulate(csv: str, *, tp: float = 0.6, lo: float = 0.12, hi: float = 0.45,
             since_days: int = 180, pace: float = 0.4) -> None:
    df = pd.read_csv(csv)
    df["predicted_prob"] = pd.to_numeric(df["predicted_prob"], errors="coerce")
    df["actual"] = pd.to_numeric(df["actual"], errors="coerce")
    fade = df[(df["predicted_prob"] >= lo) & (df["predicted_prob"] <= hi)].dropna(
        subset=["predicted_prob", "actual"])
    print(f"fade candidates: {len(fade)}; fetching close times...")
    close = _close_map(since_days)

    rows = []
    for _, r in fade.iterrows():
        tk = str(r["condition_id"])
        cts = close.get(tk)
        if cts is None:
            continue
        path = _price_path(tk, cts)
        time.sleep(pace)
        if len(path) < 3:
            continue
        # entry ~72h before close (else earliest available point >= entry window)
        entry_pts = [p for p in path if p[0] <= -3 * 3600]
        entry = entry_pts[0] if entry_pts else path[0]
        no_cost = 1.0 - entry[1]                 # buy NO at 1 - yes_bid
        if not (0.5 <= no_cost <= 0.97):
            continue
        max_profit = 1.0 - no_cost
        # walk forward for a take-profit
        triggered = None
        for lag, _yb, ya in path:
            if lag <= entry[0]:
                continue
            no_sell = 1.0 - ya                   # sell NO at 1 - yes_ask
            if no_sell - no_cost >= tp * max_profit:
                triggered = (lag, no_sell)
                break
        no_won = r["actual"] < 0.5
        hold_fee = kalshi_fees.fee_for_trade(no_cost, 1, fee_rate=FEE_RATE)
        hold_pnl = (1.0 if no_won else 0.0) - no_cost - hold_fee
        if triggered:
            lag, no_sell = triggered
            rt_fee = kalshi_fees.round_trip_fees(no_cost, no_sell, 1, fee_rate=FEE_RATE)
            early_pnl = (no_sell - no_cost) - rt_fee
            hold_secs = lag - entry[0]
        else:
            early_pnl = hold_pnl
            hold_secs = -entry[0]                # held the full window to close
        rows.append({"tk": tk, "no_cost": no_cost, "no_won": no_won,
                     "hold_pnl": hold_pnl, "early_pnl": early_pnl,
                     "triggered": triggered is not None,
                     "hold_days": hold_secs / 86400.0,
                     "full_days": -entry[0] / 86400.0})

    if not rows:
        print("no usable price paths (rate-limited or no history).")
        return
    d = pd.DataFrame(rows)
    n = len(d)
    trig = d["triggered"].mean()
    print(f"\n=== EARLY-EXIT vs HOLD  (n={n}, TP={tp:.0%}) ===")
    print(f"take-profit triggered before resolution: {trig*100:.0f}% of positions")
    print(f"  HOLD-to-resolution : ROI {d['hold_pnl'].sum()/d['no_cost'].sum()*100:+.1f}%  "
          f"net ${d['hold_pnl'].sum():+.2f}  avg hold {d['full_days'].mean():.1f}d")
    print(f"  EARLY-EXIT         : ROI {d['early_pnl'].sum()/d['no_cost'].sum()*100:+.1f}%  "
          f"net ${d['early_pnl'].sum():+.2f}  avg hold {d['hold_days'].mean():.1f}d")
    tg = d[d["triggered"]]
    if len(tg):
        print(f"\n  on the {len(tg)} that triggered TP:")
        print(f"    early ROI {tg['early_pnl'].sum()/tg['no_cost'].sum()*100:+.1f}% in "
              f"{tg['hold_days'].mean():.1f}d  vs  hold ROI "
              f"{tg['hold_pnl'].sum()/tg['no_cost'].sum()*100:+.1f}% in {tg['full_days'].mean():.1f}d")
        won = tg["no_won"].mean()
        print(f"    (of these, {won*100:.0f}% would have resolved NO anyway — TP just cashed early)")
    print("\nRead: high TP-trigger% + similar ROI in far less time => early-exit is a "
          "capital-velocity win. If early ROI << hold ROI, prices only correct AT "
          "resolution and holding is better.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--tp", type=float, default=0.6)
    ap.add_argument("--lo", type=float, default=0.12)
    ap.add_argument("--hi", type=float, default=0.45)
    args = ap.parse_args()
    simulate(args.csv, tp=args.tp, lo=args.lo, hi=args.hi)


if __name__ == "__main__":
    main()
