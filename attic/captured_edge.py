"""Captured-edge dataset — the microstructure foundation.

Reframes our in-game reaction work from "sports prediction" to "when does
Kalshi temporarily fail to be informationally efficient?" (see
docs/external-review-brief.md). For every historical information shock (an
MLB scoring play) with order-book coverage on the benefiting side, we
measure — exchange-centric, not sports-centric:

  CapturedEdge(t) = equilibrium_mid - executable_ask(t)
      the inefficiency a BUYER captures entering at lag t, valued at where
      the book later settles (the "fair" proxy). Low-variance vs PnL — it
      isolates skill from outcome luck (our PnL is fat-tail garbage at
      n~40, see [[project_reactive_hold_fat_tail]]).

  ExecutableDollars(t) = contracts buyable near the ask × price
      capacity, so we can optimize Expected $ Captured =
      CapturedEdge x ExecutableSize, not edge alone.

  Pre-shock book features (spread, depth, quote age, leverage, hour) —
      the inputs to a future "market-failure predictor": P(still mispriced
      at t | pre-shock state). This dataset is what makes that testable.

CAVEATS (state them, don't bury them):
  * Fair value is EQUILIBRIUM-RELATIVE (median mid in a settle window),
    not ground truth. It assumes the market eventually gets it right, so it
    is mildly biased toward "the market was slow." `contaminated` flags
    shocks with another scoring play inside the settle window.
  * book_archive resolution is ~15-20s during live play (kalshi-ws 15s
    flush), coarser pre-kalshi-ws. So the decay curve is ~15s-grained, not
    per-second; finer needs a faster archive flush (infra follow-up).

Read-only over book_archive + baseball_state. Builds a SQLite dataset at
data/shared/captured_edge.sqlite. CLI: `python captured_edge.py` (build +
summarize) | `--stats` (summarize existing) | `--since-days N`.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import UTC, datetime

from baseball import analysis

BOOK_DB = os.getenv("BOOK_ARCHIVE_PATH", "data/shared/book_archive.sqlite")
STATE_DB = os.getenv("BASEBALL_STATE_PATH", "data/shared/baseball_state.sqlite")
OUT_DB = os.getenv("CAPTURED_EDGE_DB", "data/shared/captured_edge.sqlite")

# Settle window (seconds after the shock) used as the equilibrium / fair proxy.
SETTLE_LO, SETTLE_HI = 150, 360
# Lag buckets (center seconds) for the decay curve; tolerance is half-spacing.
LAG_CENTERS = [15, 30, 45, 60, 90, 120]
LAG_TOL = 12
# Entry = earliest realistic action after the shock.
ENTRY_LO, ENTRY_HI = 2, 90
ASK_WITHIN_CENTS = 0.02     # depth "near the ask" counted as executable
SPORTS_FEE_RATE = 0.035     # Kalshi sports per-trade fee rate


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def top_of_book(levels: list[dict]) -> tuple[float | None, float | None, float | None]:
    """Best bid, best ask, mid from a parsed book level list.

    >>> top_of_book([{"side":"bid","price":0.40,"size":5},
    ...              {"side":"ask","price":0.44,"size":7},
    ...              {"side":"bid","price":0.39,"size":2}])
    (0.4, 0.44, 0.42)
    """
    bids = [x["price"] for x in levels if x.get("side") == "bid"]
    asks = [x["price"] for x in levels if x.get("side") == "ask"]
    bb = max(bids) if bids else None
    aa = min(asks) if asks else None
    mid = round((bb + aa) / 2, 6) if (bb is not None and aa is not None) else None
    return bb, aa, mid


def ask_depth(levels: list[dict], *, within_cents: float = ASK_WITHIN_CENTS
              ) -> tuple[float, float]:
    """Contracts and dollars buyable within ``within_cents`` of the best ask.

    >>> ask_depth([{"side":"ask","price":0.44,"size":10},
    ...           {"side":"ask","price":0.45,"size":20},
    ...           {"side":"ask","price":0.50,"size":99}], within_cents=0.02)
    (30.0, 13.4)
    """
    asks = [x for x in levels if x.get("side") == "ask" and x.get("size")]
    if not asks:
        return 0.0, 0.0
    best = min(x["price"] for x in asks)
    near = [x for x in asks if x["price"] <= best + within_cents + 1e-9]
    contracts = sum(float(x["size"]) for x in near)
    dollars = sum(float(x["size"]) * float(x["price"]) for x in near)
    return round(contracts, 4), round(dollars, 4)


def lag_label(center: int) -> str:
    return f"t{center}"


# ---------------------------------------------------------------------------
# Shock detection (score changes in live_plays)
# ---------------------------------------------------------------------------

def _scoring_shocks(sc: sqlite3.Connection) -> list[dict]:
    """One record per scoring play: the batting side scored, so it is the
    win-prob beneficiary. Carries inning + post-play score diff (leverage)."""
    rows = sc.execute(
        "SELECT game_pk, play_id, ts, half_inning, inning, "
        "home_score_after, away_score_after FROM live_plays "
        "ORDER BY game_pk, play_id").fetchall()
    out: list[dict] = []
    prev: dict[int, tuple[int, int]] = {}
    for gp, pid, ts, half, inn, hs, as_ in rows:
        ph, pa = prev.get(gp, (0, 0))
        hs, as_ = hs or 0, as_ or 0
        if hs > ph or as_ > pa:
            out.append({
                "game_pk": gp, "play_id": pid, "ts": ts,
                "half": (half or ""), "inning": inn or 0,
                "score_diff": abs(hs - as_),
                "batting_top": str(half or "").lower().startswith("top"),
            })
        prev[gp] = (hs, as_)
    return out


def _shock_ts_unix(ts: str) -> int | None:
    try:
        return int(datetime.fromisoformat(ts).timestamp())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS shocks (
            game_pk INTEGER, play_id INTEGER, ts INTEGER, ticker TEXT,
            beneficiary TEXT, inning INTEGER, score_diff INTEGER,
            hour_utc INTEGER, n_simul_games INTEGER,
            pre_bid REAL, pre_ask REAL, pre_mid REAL, pre_spread REAL,
            pre_ask_contracts REAL, quote_age_s REAL, beneficiary_underdog INTEGER,
            equilibrium_mid REAL, n_settle INTEGER, contaminated INTEGER,
            entry_lag_s REAL, entry_ask REAL, entry_edge REAL,
            entry_net_edge REAL, entry_exec_contracts REAL, entry_exec_dollars REAL,
            exp_dollars_captured REAL, decay_json TEXT,
            PRIMARY KEY (game_pk, play_id, ticker)
        )""")
    con.commit()


def _game_info(cache: dict, game_pk: int):
    """Cached (away_abbr, home_abbr, tickers) for a game — many shocks share
    a game_pk, so this avoids thousands of duplicate state-DB lookups."""
    if game_pk not in cache:
        g = analysis._get_game_row(game_pk)
        if not g:
            cache[game_pk] = None
        else:
            try:
                tks = list(analysis.tickers_for_game(game_pk))
            except Exception:
                tks = []
            cache[game_pk] = (g["away_abbr"], g["home_abbr"], tks)
    return cache[game_pk]


def build(*, since_days: float | None = None, limit: int | None = None,
          progress: bool = True) -> dict:
    import bisect
    bc = sqlite3.connect(BOOK_DB)
    sc = sqlite3.connect(STATE_DB)
    out = sqlite3.connect(OUT_DB)
    _ensure_schema(out)

    shocks = _scoring_shocks(sc)
    # precompute unix times once; drop unparseable
    for s in shocks:
        s["u"] = _shock_ts_unix(s["ts"])
    shocks = [s for s in shocks if s["u"] is not None]
    # score-change times per game (contamination) + global sorted times (simul)
    by_game: dict[int, list[int]] = defaultdict(list)
    for s in shocks:
        by_game[s["game_pk"]].append(s["u"])
    simul = sorted((s["u"], s["game_pk"]) for s in shocks)
    simul_t = [x[0] for x in simul]
    cutoff = None
    if since_days is not None:
        cutoff = int(datetime.now(UTC).timestamp()) - int(since_days * 86400)

    game_cache: dict = {}
    n_built = n_skip_noticker = n_skip_nobook = n_skip_noeq = 0
    batch: list[dict] = []

    def _flush(b):
        if not b:
            return
        cols = list(b[0].keys())
        ph = ",".join("?" * len(cols))
        out.executemany(
            f"INSERT OR REPLACE INTO shocks ({','.join(cols)}) VALUES ({ph})",
            [[r[c] for c in cols] for r in b])
        out.commit()

    for s in shocks:
        if limit and n_built >= limit:
            break
        u = s["u"]
        if cutoff and u < cutoff:
            continue
        info = _game_info(game_cache, s["game_pk"])
        if not info:
            continue
        away_abbr, home_abbr, tickers = info
        ben = away_abbr if s["batting_top"] else home_abbr
        cands = [t for t in tickers if t.endswith("-" + ben)]
        if not cands:
            n_skip_noticker += 1
            continue
        lo, hi = u - 150, u + SETTLE_HI + 30
        chosen = None
        for t in cands:
            r = bc.execute(
                "SELECT snapshot_ts, book_json FROM book_snapshots "
                "WHERE market_id=? AND snapshot_ts BETWEEN ? AND ? "
                "ORDER BY snapshot_ts", (t, lo, hi)).fetchall()
            if len(r) >= 4:
                chosen = (t, r)
                break
        if not chosen:
            n_skip_nobook += 1
            continue
        ticker, raw = chosen
        # parse path
        pts = []  # (lag, bid, ask, mid, ask_contracts, ask_dollars)
        for sts, bj in raw:
            try:
                levels = json.loads(bj)
            except Exception:
                continue
            bb, aa, mid = top_of_book(levels)
            if mid is None:
                continue
            c, d = ask_depth(levels)
            pts.append((sts - u, bb, aa, mid, c, d))
        if len(pts) < 4:
            n_skip_nobook += 1
            continue

        pre = [p for p in pts if -90 <= p[0] <= -1]
        settle = [p[3] for p in pts if SETTLE_LO <= p[0] <= SETTLE_HI]
        if not pre or len(settle) < 2:
            n_skip_noeq += 1
            continue
        pre_pt = pre[-1]
        equilibrium = st.median(settle)

        # contamination: another scoring play for this game inside the window
        contaminated = any(u < t2 <= u + SETTLE_HI for t2 in by_game[s["game_pk"]]
                           if t2 != u)
        # simultaneity: distinct OTHER games with a shock within +/-120s
        lo_i = bisect.bisect_left(simul_t, u - 120)
        hi_i = bisect.bisect_right(simul_t, u + 120)
        n_simul = len({simul[j][1] for j in range(lo_i, hi_i)
                       if simul[j][1] != s["game_pk"]})

        # entry = earliest snapshot in the action window
        entry_cand = [p for p in pts if ENTRY_LO <= p[0] <= ENTRY_HI and p[2] is not None]
        entry = min(entry_cand, key=lambda p: p[0]) if entry_cand else None

        decay = {}
        for center in LAG_CENTERS:
            cand = [p for p in pts if abs(p[0] - center) <= LAG_TOL and p[2] is not None]
            if not cand:
                continue
            p = min(cand, key=lambda p: abs(p[0] - center))
            decay[lag_label(center)] = {
                "lag": p[0], "ask": p[2],
                "edge": round(equilibrium - p[2], 4),
                "contracts": p[4], "dollars": p[5],
            }

        rec = {
            "game_pk": s["game_pk"], "play_id": s["play_id"], "ts": u,
            "ticker": ticker, "beneficiary": ben, "inning": s["inning"],
            "score_diff": s["score_diff"],
            "hour_utc": datetime.fromtimestamp(u, UTC).hour,
            "n_simul_games": n_simul,
            "pre_bid": pre_pt[1], "pre_ask": pre_pt[2], "pre_mid": pre_pt[3],
            "pre_spread": (round(pre_pt[2] - pre_pt[1], 4)
                           if pre_pt[1] is not None and pre_pt[2] is not None else None),
            "pre_ask_contracts": pre_pt[4],
            "quote_age_s": abs(pre_pt[0]),
            "beneficiary_underdog": int(pre_pt[3] < 0.5),
            "equilibrium_mid": round(equilibrium, 4),
            "n_settle": len(settle), "contaminated": int(contaminated),
            "entry_lag_s": entry[0] if entry else None,
            "entry_ask": entry[2] if entry else None,
            "entry_edge": round(equilibrium - entry[2], 4) if entry else None,
            "entry_net_edge": (round(equilibrium - entry[2]
                               - SPORTS_FEE_RATE * entry[2] * (1 - entry[2]), 4)
                               if entry else None),
            "entry_exec_contracts": entry[4] if entry else None,
            "entry_exec_dollars": entry[5] if entry else None,
            "exp_dollars_captured": (round((equilibrium - entry[2]) * entry[4], 4)
                                     if entry else None),
            "decay_json": json.dumps(decay),
        }
        batch.append(rec)
        n_built += 1
        if len(batch) >= 100:
            _flush(batch)
            batch = []
            if progress:
                print(f"  built {n_built} shocks (persisted)...", flush=True)

    _flush(batch)
    return {"built": n_built, "skip_no_ticker": n_skip_noticker,
            "skip_no_book": n_skip_nobook, "skip_no_equilibrium": n_skip_noeq,
            "total_shocks": len(shocks)}


# ---------------------------------------------------------------------------
# Summarize
# ---------------------------------------------------------------------------

def _load(con: sqlite3.Connection) -> list[dict]:
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute("SELECT * FROM shocks")]


def summarize() -> None:
    con = sqlite3.connect(OUT_DB)
    rows = _load(con)
    if not rows:
        print("no shocks in dataset — run build first.")
        return
    clean = [r for r in rows if not r["contaminated"] and r["entry_edge"] is not None]
    print(f"=== captured-edge dataset: {len(rows)} shocks "
          f"({len(clean)} clean & with entry) ===")
    print(f"distinct games: {len({r['game_pk'] for r in rows})}  "
          f"(events are correlated within a game — effective n is lower)\n")

    def dist(label, xs, scale=100, unit="c"):
        xs = [x for x in xs if x is not None]
        if not xs:
            print(f"  {label}: n=0")
            return
        print(f"  {label}: n={len(xs)} mean {st.mean(xs)*scale:+.2f}{unit} "
              f"median {st.median(xs)*scale:+.2f}{unit} "
              f"%>0 {100*sum(x>0 for x in xs)/len(xs):.0f}%")

    print("ENTRY captured edge (equilibrium - entry ask), clean shocks:")
    dist("  gross", [r["entry_edge"] for r in clean])
    dist("  net of fee", [r["entry_net_edge"] for r in clean])
    print(f"  median entry lag: {st.median([r['entry_lag_s'] for r in clean]):.0f}s")

    print("\nCAPACITY at entry (executable near the ask), clean shocks:")
    cd = [r["entry_exec_dollars"] for r in clean if r["entry_exec_dollars"]]
    if cd:
        print(f"  executable $: median ${st.median(cd):.0f}  "
              f"p25 ${sorted(cd)[len(cd)//4]:.0f}  p75 ${sorted(cd)[3*len(cd)//4]:.0f}")
    edc = [r["exp_dollars_captured"] for r in clean if r["exp_dollars_captured"] is not None]
    dist("  Expected $ captured (edge x contracts)", edc, scale=1, unit="$")

    print("\nDECAY CURVE (clean shocks, mean captured edge by lag):")
    bybucket = defaultdict(list)
    for r in clean:
        try:
            d = json.loads(r["decay_json"])
        except Exception:
            continue
        for k, v in d.items():
            bybucket[k].append(v["edge"])
    for center in LAG_CENTERS:
        v = bybucket.get(lag_label(center), [])
        if v:
            print(f"  ~{center:3d}s  n={len(v):4d}  mean {st.mean(v)*100:+.2f}c  "
                  f"median {st.median(v)*100:+.2f}c  %>0 {100*sum(x>0 for x in v)/len(v):.0f}%")

    print("\nPREDICTOR TEASER — entry edge by pre-shock state (clean):")
    def split(label, pred):
        a = [r["entry_edge"] for r in clean if pred(r)]
        b = [r["entry_edge"] for r in clean if not pred(r)]
        if a and b:
            print(f"  {label}: yes n={len(a)} mean {st.mean(a)*100:+.2f}c | "
                  f"no n={len(b)} mean {st.mean(b)*100:+.2f}c")
    split("wide pre-spread (>2c)", lambda r: (r["pre_spread"] or 0) > 0.02)
    split("thin pre-depth (<200 ct)", lambda r: (r["pre_ask_contracts"] or 0) < 200)
    split("high leverage (inn>=7 & diff<=1)",
          lambda r: r["inning"] >= 7 and r["score_diff"] <= 1)
    split("beneficiary underdog (pre<0.5)", lambda r: r["beneficiary_underdog"])
    split("many simul games (>=4)", lambda r: r["n_simul_games"] >= 4)
    # hour-of-day buckets
    print("  by hour-of-day (UTC), mean entry edge:")
    byhour = defaultdict(list)
    for r in clean:
        byhour[r["hour_utc"]].append(r["entry_edge"])
    for h in sorted(byhour):
        v = byhour[h]
        if len(v) >= 5:
            print(f"    {h:02d}h n={len(v):3d} mean {st.mean(v)*100:+.2f}c")


def main() -> None:
    ap = argparse.ArgumentParser(description="Captured-edge microstructure dataset")
    ap.add_argument("--stats", action="store_true", help="summarize existing, no build")
    ap.add_argument("--since-days", type=float, default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    if not args.stats:
        print("building captured-edge dataset...")
        res = build(since_days=args.since_days, limit=args.limit)
        print(f"build: {res}\n")
    summarize()


if __name__ == "__main__":
    main()
