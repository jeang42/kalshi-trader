"""Market-maker feasibility simulation — replay the tape against our quotes.

The evidence-driven pivot (see docs/external-review-brief.md + the captured-
edge falsifications): every TAKER edge died paying the spread. So test the
inversion — could we EARN the spread as a maker? The one question that
decides it: is the flow that hits our resting quotes too informed (adverse
selection > spread captured)?

Method (data-ready, no new capture): for each MLB market we replay the
executed-trade tape (trade_archive) against quotes we WOULD have rested at
bid = mid − q, ask = mid + q around the contemporaneous book mid
(book_archive). A fill is marked to the near-future mid (+HOLD s) — the
"maker who flattens" P&L:

    buy  fill: net = future_mid − our_bid − fee
    sell fill: net = our_ask − future_mid − fee

If net-per-fill is positive across realistic q, making is viable and we
refine. If even the optimistic always-fill assumption is net-negative, the
flow is too informed and making is dead like the rest.

Conservative-where-it's-easy, optimistic-where-it-matters:
  * optimistic FILL (we assume we fill whenever a taker prints through our
    quote) — an upper bound on fills; if THIS is negative, making is dead.
  * realistic COST (real contemporaneous mid, real future mid, Kalshi fee).

Read-only. CLI: `python maker_sim.py [--top N] [--hold 60]`.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import statistics as st

import kalshi_fees
from captured_edge import BOOK_DB, top_of_book

TRADE_DB = "data/shared/trade_archive.sqlite"
QS_CENTS = [1, 2, 3]
DEFAULT_HOLD = 60


def _mid_series(bc, market_id):
    out = []
    for sts, bj in bc.execute(
        "SELECT snapshot_ts, book_json FROM book_snapshots WHERE market_id=? "
        "ORDER BY snapshot_ts", (market_id,)):
        try:
            _b, _a, mid = top_of_book(json.loads(bj))
        except Exception:
            mid = None
        if mid is not None:
            out.append((sts, mid))
    return out


def _mid_at(series_ts, series_mid, t):
    i = bisect.bisect_right(series_ts, t) - 1
    return series_mid[i] if i >= 0 else None


def _future_mid(series_ts, series_mid, t, hold):
    i = bisect.bisect_left(series_ts, t + hold)
    return series_mid[i] if i < len(series_mid) else (
        series_mid[-1] if series_mid else None)


def simulate(*, top=100, hold=DEFAULT_HOLD):
    bc = sqlite3.connect(BOOK_DB)
    td = sqlite3.connect(TRADE_DB)
    book_markets = {r[0] for r in bc.execute(
        "SELECT DISTINCT market_id FROM book_snapshots WHERE market_id LIKE 'KXMLBGAME-%'")}
    # busiest markets (most trades = most fill opportunities)
    busy = [r for r in td.execute(
        "SELECT ticker, COUNT(*) n FROM trades WHERE ticker LIKE 'KXMLBGAME-%' "
        "GROUP BY ticker ORDER BY n DESC")]
    targets = [t for t, _ in busy if t in book_markets][:top]
    print(f"simulating maker on {len(targets)} busiest MLB markets "
          f"(hold={hold}s, q={QS_CENTS}c)\n")

    # per q: lists of net P&L per fill, plus side counts
    res = {q: {"buy": [], "sell": []} for q in QS_CENTS}
    n_trades_seen = 0
    for ticker in targets:
        ms = _mid_series(bc, ticker)
        if len(ms) < 5:
            continue
        sts = [x[0] for x in ms]
        smid = [x[1] for x in ms]
        trades = td.execute(
            "SELECT ts_unix, yes_price, taker_side FROM trades WHERE ticker=? "
            "ORDER BY ts_unix", (ticker,)).fetchall()
        for t, yp, taker in trades:
            n_trades_seen += 1
            p = float(yp)
            if p > 1:
                p /= 100.0
            mid_now = _mid_at(sts, smid, t)
            if mid_now is None:
                continue
            fm = _future_mid(sts, smid, t, hold)
            if fm is None:
                continue
            for q in QS_CENTS:
                qd = q / 100.0
                bid, ask = mid_now - qd, mid_now + qd
                fee = kalshi_fees.fee_for_trade(
                    mid_now, 1, fee_rate=kalshi_fees.SPORTS_FEE_RATE)
                # aggressive seller (no-taker) printing at/below our bid -> we buy
                if str(taker).lower() == "no" and p <= bid + 1e-9 and bid > 0:
                    res[q]["buy"].append(fm - bid - fee)
                # aggressive buyer (yes-taker) printing at/above our ask -> we sell
                elif str(taker).lower() == "yes" and p >= ask - 1e-9 and ask < 1:
                    res[q]["sell"].append(ask - fm - fee)

    print(f"trades replayed: {n_trades_seen}\n")
    print(f"{'q':>3} {'fills':>7} {'buys':>6} {'sells':>6} "
          f"{'net/fill':>9} {'gross spd':>9} {'adv sel':>8} {'total $':>9}")
    for q in QS_CENTS:
        buys, sells = res[q]["buy"], res[q]["sell"]
        allf = buys + sells
        if not allf:
            print(f"{q:>3}  (no fills)")
            continue
        net_fill = st.mean(allf)
        gross = q / 100.0  # spread earned per fill before adverse + fee
        adv = gross - net_fill  # everything eaten (adverse selection + fee)
        print(f"{q:>3} {len(allf):>7} {len(buys):>6} {len(sells):>6} "
              f"{net_fill*100:>+8.2f}c {gross*100:>+8.2f}c {adv*100:>+7.2f}c "
              f"{sum(allf):>+8.2f}")
    print("\nRead: net/fill > 0 => spread captured beats adverse selection+fee "
          "=> making is viable, refine (queue/fill realism, inventory). "
          "net/fill < 0 even here (optimistic fills) => flow too informed, dead.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--hold", type=int, default=DEFAULT_HOLD)
    args = ap.parse_args()
    simulate(top=args.top, hold=args.hold)


if __name__ == "__main__":
    main()
