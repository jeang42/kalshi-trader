"""Feed-aware maker test — does pulling quotes around plays dodge the toxicity?

Blind market-making lost ~3c/fill to adverse selection (maker_sim.py). But
our actual thesis was feed-PROTECTED making: use the fast live feed to pull/
widen quotes around scoring plays (where the informed reaction-flow lives)
and only rest quotes in the QUIET stretches. This splits every simulated
fill into:

  near-play : a scoring play within +/-WINDOW s on that game (we'd have
              PULLED our quote -> these fills are what we AVOID)
  quiet     : no scoring play nearby (we'd have rested -> these are what we
              actually capture)

If quiet fills are net-POSITIVE, feed-protected making is viable (quote in
the quiet, pull around plays — the one strategy where our feed is the edge).
If quiet fills are still negative, the toxicity is everywhere and making is
dead even with the feed.

Read-only. CLI: `python maker_sim_feedaware.py [--top N] [--hold 60] [--window 90]`.
"""
from __future__ import annotations

import argparse
import bisect
import sqlite3
import statistics as st
from datetime import datetime

import kalshi_fees
from baseball import analysis
from captured_edge import BOOK_DB, STATE_DB
from maker_sim import _future_mid, _mid_at, _mid_series

TRADE_DB = "data/shared/trade_archive.sqlite"
QS_CENTS = [1, 2, 3]


def _scoring_times_by_game(sc):
    """game_pk -> sorted list of unix ts of scoring plays (the info shocks)."""
    rows = sc.execute(
        "SELECT game_pk, ts, home_score_after, away_score_after FROM live_plays "
        "ORDER BY game_pk, play_id").fetchall()
    out, prev = {}, {}
    for gp, ts, hs, as_ in rows:
        ph, pa = prev.get(gp, (0, 0))
        hs, as_ = hs or 0, as_ or 0
        if hs > ph or as_ > pa:
            try:
                u = int(datetime.fromisoformat(ts).timestamp())
                out.setdefault(gp, []).append(u)
            except Exception:
                pass
        prev[gp] = (hs, as_)
    for gp in out:
        out[gp].sort()
    return out


def _ticker_to_game(sc):
    """Reverse map ticker -> game_pk via analysis.tickers_for_game over all
    games that have live plays."""
    games = [r[0] for r in sc.execute("SELECT DISTINCT game_pk FROM live_plays")]
    m = {}
    for gp in games:
        try:
            for t in analysis.tickers_for_game(gp):
                m[t] = gp
        except Exception:
            continue
    return m


def _near_play(times, t, window):
    if not times:
        return False
    i = bisect.bisect_left(times, t)
    for j in (i - 1, i):
        if 0 <= j < len(times) and abs(times[j] - t) <= window:
            return True
    return False


def simulate(*, top=120, hold=60, window=90):
    bc = sqlite3.connect(BOOK_DB)
    td = sqlite3.connect(TRADE_DB)
    sc = sqlite3.connect(STATE_DB)
    scoring = _scoring_times_by_game(sc)
    t2g = _ticker_to_game(sc)

    book_markets = {r[0] for r in bc.execute(
        "SELECT DISTINCT market_id FROM book_snapshots WHERE market_id LIKE 'KXMLBGAME-%'")}
    busy = [r[0] for r in td.execute(
        "SELECT ticker, COUNT(*) n FROM trades WHERE ticker LIKE 'KXMLBGAME-%' "
        "GROUP BY ticker ORDER BY n DESC")]
    targets = [t for t in busy if t in book_markets][:top]
    print(f"feed-aware maker on {len(targets)} markets "
          f"(hold={hold}s, pull window=+/-{window}s, q={QS_CENTS}c)\n")

    res = {q: {"quiet": [], "near": []} for q in QS_CENTS}
    matched = 0
    for ticker in targets:
        gp = t2g.get(ticker)
        times = scoring.get(gp, []) if gp else []
        if gp:
            matched += 1
        ms = _mid_series(bc, ticker)
        if len(ms) < 5:
            continue
        sts = [x[0] for x in ms]
        smid = [x[1] for x in ms]
        for t, yp, taker in td.execute(
            "SELECT ts_unix, yes_price, taker_side FROM trades WHERE ticker=? "
            "ORDER BY ts_unix", (ticker,)):
            p = float(yp)
            if p > 1:
                p /= 100.0
            mid_now = _mid_at(sts, smid, t)
            if mid_now is None:
                continue
            fm = _future_mid(sts, smid, t, hold)
            if fm is None:
                continue
            bucket = "near" if _near_play(times, t, window) else "quiet"
            tk = str(taker).lower()
            for q in QS_CENTS:
                qd = q / 100.0
                bid, ask = mid_now - qd, mid_now + qd
                fee = kalshi_fees.fee_for_trade(
                    mid_now, 1, fee_rate=kalshi_fees.SPORTS_FEE_RATE)
                if tk == "no" and p <= bid + 1e-9 and bid > 0:
                    res[q][bucket].append(fm - bid - fee)
                elif tk == "yes" and p >= ask - 1e-9 and ask < 1:
                    res[q][bucket].append(ask - fm - fee)

    print(f"markets mapped to a game: {matched}/{len(targets)}\n")
    print(f"{'q':>3} {'QUIET fills':>11} {'quiet net/fill':>15} {'quiet $':>10} "
          f"| {'NEAR fills':>10} {'near net/fill':>14}")
    for q in QS_CENTS:
        Q, N = res[q]["quiet"], res[q]["near"]
        qline = f"{len(Q):>11} {st.mean(Q)*100:>+14.2f}c {sum(Q):>+9.2f}" if Q else f"{0:>11} {'-':>15} {'-':>10}"
        nline = f"{len(N):>10} {st.mean(N)*100:>+13.2f}c" if N else f"{0:>10} {'-':>14}"
        print(f"{q:>3} {qline} | {nline}")
    print("\nRead: if QUIET net/fill > 0 (and NEAR < QUIET), the toxicity is "
          "play-concentrated and a feed-protected maker captures spread in the "
          "calm -> viable. If QUIET still < 0, toxicity is everywhere -> dead.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=120)
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--window", type=int, default=90)
    args = ap.parse_args()
    simulate(top=args.top, hold=args.hold, window=args.window)


if __name__ == "__main__":
    main()
