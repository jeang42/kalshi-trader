"""Latency-decay curve: after a scoring play, how much price-move is still
available to capture if you enter at lag δ on the benefiting (scoring) side?

Decisive test for the in-game-reaction (speed) thesis + leverage conditioning
(inning/score). Research/analysis tool — read-only over book_archive +
baseball_state. Run: `python reaction_decay.py`.

Findings (2026-06-05, n=790 plays / 71 high-leverage): full-sample reaction
~0 (most runs don't move the price); HIGH-LEVERAGE (inning>=7 & score within
1) shows ~+2.5c mean edge that PERSISTS ~60s (>> our latency, so reachable) —
but fat-tailed (median 0, ~25% hit) and thin vs the ~1c spread. The forward
extensions to try: condition on per-play WPA (the feed's leverageIndex/win-
prob delta) to select the ~25% that actually move; other event types (hits,
outs, runners-on) via base-out state; grow the high-leverage sample.
"""
import json
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime

from baseball import analysis

BOOK = "data/shared/book_archive.sqlite"
STATE = "data/shared/baseball_state.sqlite"

bc = sqlite3.connect(BOOK)
covered = {r[0] for r in bc.execute(
    "SELECT DISTINCT market_id FROM book_snapshots WHERE market_id LIKE 'KXMLBGAME-%'")}

sc = sqlite3.connect(STATE)
_abbr_cache = {}
def abbr_for(gp):
    if gp not in _abbr_cache:
        g = analysis._get_game_row(gp)
        _abbr_cache[gp] = (g["away_abbr"], g["home_abbr"]) if g else None
    return _abbr_cache[gp]
# scoring plays + metadata in one pass
plays = sc.execute(
    "SELECT game_pk, play_id, ts, half_inning, inning, home_score_after, away_score_after "
    "FROM live_plays ORDER BY game_pk, play_id").fetchall()

# detect score changes per game; carry inning + score-diff (leverage proxy)
scoring = []
prev = {}
for gp, pid, ts, half, inn, hs, as_ in plays:
    ph, pa = prev.get(gp, (0, 0))
    if (hs or 0) > ph or (as_ or 0) > pa:
        diff = abs((hs or 0) - (as_ or 0))
        scoring.append((gp, pid, ts, half, inn or 0, diff))
    prev[gp] = (hs or 0, as_ or 0)


def top(bj):
    d = json.loads(bj)
    bids = [x["price"] for x in d if x["side"] == "bid"]
    asks = [x["price"] for x in d if x["side"] == "ask"]
    bb = max(bids) if bids else None
    aa = min(asks) if asks else None
    mid = (bb + aa) / 2 if (bb is not None and aa is not None) else None
    return bb, aa, mid


LAGS = [-20, 0, 15, 30, 45, 60, 90, 120]
TOL = 12
SETTLE_LO, SETTLE_HI = 150, 330
by_lag = defaultdict(list)
by_lag_hi = defaultdict(list)   # high-leverage: inning>=7 and score within 1
total_moves, total_moves_hi, spreads = [], [], []
n_used = n_hi = 0

for gp, _pid, ts, half, inn, diff in scoring:  # book fetched by time window
    hi_lev = (inn >= 7 and diff <= 1)
    ab = abbr_for(gp)
    if not ab:
        continue
    away, home = ab
    ben = away if str(half).lower().startswith("top") else home
    # candidate benefiting tickers among book-covered markets
    cands = [t for t in covered if t.endswith("-" + ben)]
    # match the game by date+teams embedded in ticker is hard; instead use
    # book rows that exist for any covered ticker ending in -ben AND whose
    # snapshots bracket the play time. Simplest: try each candidate, keep one
    # with snapshots in-window.
    try:
        pts_ts = int(datetime.fromisoformat(ts).timestamp())
    except Exception:
        continue
    lo, hi = pts_ts - 360, pts_ts + 360
    chosen = None
    for t in cands:
        rows = bc.execute(
            "SELECT snapshot_ts, book_json FROM book_snapshots "
            "WHERE market_id=? AND snapshot_ts BETWEEN ? AND ? ORDER BY snapshot_ts",
            (t, lo, hi)).fetchall()
        if len(rows) >= 4:
            chosen = rows
            break
    if not chosen:
        continue
    pts = []
    for sts, bj in chosen:
        bb, aa, mid = top(bj)
        if mid is not None:
            pts.append((sts - pts_ts, mid, bb, aa))
    if len(pts) < 4:
        continue
    settle = [m for s, m, _, _ in pts if SETTLE_LO <= s <= SETTLE_HI]
    pre = [(s, m) for s, m, _, _ in pts if -60 <= s <= 0]
    if not settle or not pre:
        continue
    settled = st.median(settle)
    price_before = pre[-1][1]
    total_moves.append(settled - price_before)
    if hi_lev:
        total_moves_hi.append(settled - price_before)
        n_hi += 1
    near0 = min(pts, key=lambda p: abs(p[0]))
    if near0[2] is not None and near0[3] is not None:
        spreads.append(near0[3] - near0[2])
    n_used += 1
    for lag in LAGS:
        cand = [p for p in pts if abs(p[0] - lag) <= TOL]
        if cand:
            m = min(cand, key=lambda p: abs(p[0] - lag))[1]
            by_lag[lag].append(settled - m)
            if hi_lev:
                by_lag_hi[lag].append(settled - m)

print(f"scoring plays with usable benefiting-side book: {n_used} (of {len(scoring)})")
if total_moves:
    print(f"mean total reaction move (settled-pre): {st.mean(total_moves):+.4f}  "
          f"median {st.median(total_moves):+.4f}")
if spreads:
    print(f"typical entry spread: median {st.median(spreads):.4f}")
def curve(title, blob, moves):
    print(f"\n=== {title} ===")
    if moves:
        print(f"mean total reaction move: {st.mean(moves):+.4f}  "
              f"median {st.median(moves):+.4f}  n={len(moves)}")
    print(f"{'lag(s)':>8} {'n':>4} {'mean_edge':>10} {'median':>8} {'%>0':>5}")
    for lag in LAGS:
        e = blob.get(lag, [])
        if not e:
            continue
        lbl = f"{lag}(pre)" if lag < 0 else str(lag)
        print(f"{lbl:>8} {len(e):>4} {st.mean(e):>+10.4f} {st.median(e):>+8.4f} "
              f"{100*sum(x>0 for x in e)/len(e):>4.0f}%")

curve("ALL scoring plays", by_lag, total_moves)
curve(f"HIGH-LEVERAGE (inning>=7 & score within 1)  n_plays={n_hi}",
      by_lag_hi, total_moves_hi)
