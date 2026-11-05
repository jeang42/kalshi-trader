"""Settlement-timing probe — does the book lag a CERTAIN outcome at game end?

The next mechanism after the reaction-entry edge was falsified
([[project_strategy_research_findings]]). Different class of inefficiency:
not probabilistic reaction, but CERTAINTY arbitrage. Once an MLB game is
over the winner is 100% to pay $1 — so any executable ask < $1 in the
window before Kalshi converges/settles is real money, with NO
equilibrium-bias caveat (fair = 1.0 exactly, validated by the known result).

For each finished game with book coverage we take the last live play as the
game-over proxy, then measure the WINNER's executable ask vs $1 at lags
after game-over:

  CapturedEdge(t) = 1.0 - winner_ask(t)      (gross; net subtracts the fee)
  ExecutableSize  = contracts buyable near that ask (capacity)

Reports the convergence curve (how fast the ask climbs to $1), the share of
games still buyable below thresholds at each lag, and capacity. Read-only
over book_archive + baseball_state. Reuses captured_edge helpers.

CLI: `python settlement_lag_probe.py`.
"""
from __future__ import annotations

import json
import sqlite3
import statistics as st
from collections import defaultdict

from baseball import analysis
from captured_edge import BOOK_DB, STATE_DB, ask_depth, top_of_book

SPORTS_FEE_RATE = 0.035
LAG_BUCKETS = [(0, 30), (30, 60), (60, 120), (120, 300), (300, 600)]


def _winner_by_game(bs: sqlite3.Connection) -> dict:
    """game_pk -> (home_abbr, away_abbr, winner_abbr) for decided games."""
    bs.row_factory = sqlite3.Row
    q = ("SELECT g.game_pk, g.home_score, g.away_score, "
         "ht.abbreviation home, at.abbreviation away "
         "FROM games g JOIN teams ht ON g.home_team_id=ht.id "
         "JOIN teams at ON g.away_team_id=at.id")
    out = {}
    for r in bs.execute(q):
        hs, as_ = r["home_score"], r["away_score"]
        if hs is None or as_ is None or hs == as_:
            continue
        out[r["game_pk"]] = (r["home"], r["away"],
                             r["home"] if hs > as_ else r["away"])
    return out


def _game_end_ts(sc: sqlite3.Connection, game_pk: int) -> int | None:
    """Last live-play unix ts for the game (game-over proxy)."""
    r = sc.execute("SELECT MAX(ts) FROM live_plays WHERE game_pk=?",
                   (game_pk,)).fetchone()
    if not r or not r[0]:
        return None
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(r[0]).timestamp())
    except Exception:
        return None


def probe() -> None:
    bc = sqlite3.connect(BOOK_DB)
    sc = sqlite3.connect(STATE_DB)
    bs = sqlite3.connect(STATE_DB)
    winners = _winner_by_game(bs)

    by_bucket_edge = defaultdict(list)
    by_bucket_contracts = defaultdict(list)
    by_bucket_below97 = defaultdict(lambda: [0, 0])  # buyable<0.97 count, total
    by_bucket_below99 = defaultdict(lambda: [0, 0])
    n_games = n_used = 0

    for game_pk, (_home, _away, winner) in winners.items():
        n_games += 1
        t_end = _game_end_ts(sc, game_pk)
        if t_end is None:
            continue
        try:
            cands = [t for t in analysis.tickers_for_game(game_pk)
                     if t.endswith("-" + winner)]
        except Exception:
            cands = []
        if not cands:
            continue
        rows = None
        for t in cands:
            r = bc.execute(
                "SELECT snapshot_ts, book_json FROM book_snapshots "
                "WHERE market_id=? AND snapshot_ts BETWEEN ? AND ? "
                "ORDER BY snapshot_ts", (t, t_end - 120, t_end + 600)).fetchall()
            if len(r) >= 3:
                rows = r
                break
        if not rows:
            continue
        pts = []
        for sts, bj in rows:
            try:
                levels = json.loads(bj)
            except Exception:
                continue
            _bb, aa, _mid = top_of_book(levels)
            if aa is None:
                continue
            c, _d = ask_depth(levels)
            pts.append((sts - t_end, aa, c))
        if not any(lag >= 0 for lag, _, _ in pts):
            continue
        n_used += 1
        for lo, hi in LAG_BUCKETS:
            cand = [p for p in pts if lo <= p[0] < hi]
            if not cand:
                continue
            p = min(cand, key=lambda p: p[0])  # earliest in bucket
            lag, ask, contracts = p
            edge = 1.0 - ask
            key = f"{lo}-{hi}"
            by_bucket_edge[key].append(edge)
            by_bucket_contracts[key].append(contracts)
            by_bucket_below97[key][1] += 1
            by_bucket_below99[key][1] += 1
            if ask < 0.97:
                by_bucket_below97[key][0] += 1
            if ask < 0.99:
                by_bucket_below99[key][0] += 1

    print(f"finished games: {n_games}  with usable winner book post-final: {n_used}\n")
    print("WINNER ask vs $1.00 after game-over (the certainty-arb window):")
    print(f"{'lag(s)':>10} {'n':>4} {'mean edge':>10} {'med':>7} "
          f"{'%<0.99':>7} {'%<0.97':>7} {'med $cap':>9} {'mean net':>9}")
    for lo, hi in LAG_BUCKETS:
        key = f"{lo}-{hi}"
        e = by_bucket_edge.get(key, [])
        if not e:
            continue
        c = by_bucket_contracts.get(key, [])
        b99 = by_bucket_below99[key]
        b97 = by_bucket_below97[key]
        # net of fee: edge - fee(ask); ask = 1 - edge
        net = st.mean([ed - SPORTS_FEE_RATE * (1 - ed) * ed for ed in e])
        # executable dollars near ask ~ contracts * ask
        capdollars = st.median([ct * (1 - em) for ct, em in zip(c, e, strict=False)]) if c else 0
        print(f"{key:>10} {len(e):>4} {st.mean(e)*100:>+9.2f}c "
              f"{st.median(e)*100:>+6.2f}c {100*b99[0]/b99[1]:>6.0f}% "
              f"{100*b97[0]/b97[1]:>6.0f}% {capdollars:>8.0f} {net*100:>+8.2f}c")
    print("\nRead: mean edge = $1 - winner ask (gross capturable on a CERTAIN "
          "outcome). %<0.99 = share of games where the winner was still buyable "
          "below 99c at that lag. If edge is meaningful AND persists for a "
          "reachable lag AND has capacity, that's a certainty-arb edge.")


if __name__ == "__main__":
    probe()
