"""Candle panel — historical near-close price reconstruction for SETTLED markets.

THE POINT (docs/edge-research-log.md 2026-08-07, Gap A). The forward panel
(`near_close_capture`) was built on the premise that historical near-close
prices are unreachable. That premise is FALSE at hourly fidelity:
`kc.get_candlesticks` returns `yes_bid_close` / `yes_ask_close` per hour for
any settled ticker — executable-ish prices at EVERY hourly lead, backfillable
any time. This module turns that into a panel:

    markets:  one row per settled market (outcome, close_time, category)
    candles:  hourly bid/ask/price/volume/OI for the FINAL_HOURS before close

which multiplies the near-close sample the studies run on (band × class ×
lead × z) and lets the WTI exclusion, the lead-dependence question, and both
arms' cells be re-tested on months rather than weeks.

SCOPE LIMITS, honest:
- Discovery uses the settled feed, which reaches back only a few weeks —
  run the incremental cycle forever and the panel GROWS; the deep past stays
  unreachable (same limit the whole project lives with).
- Candle prices are HOURLY CLOSES of bid/ask, not tick top-of-book. Good
  enough for lead studies; the live arms' own recorded entries remain the
  ground truth for fill-level questions.
- One API call per ticker. Paced; an incremental cycle is minutes, a first
  backfill is an hour-plus. `--max-markets` bounds any single cycle and the
  next cycle picks up the remainder (state is per-ticker, idempotent).

Read-only vs the venue; no trading imports. SQLite at
`data/shared/candle_panel.sqlite` (env `CANDLE_PANEL_DB`, resolved lazily —
no import-time constant; see the 2026-08-02 test-contamination incident).
CLI: `python candle_panel.py` (incremental) | `--backfill --since-days 21` |
`--loop --interval 21600` | `--stats`.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime

import pandas as pd

from pm.core import logctx, shutdown, sqlite_schema
from pm.venue import kalshi_client as kc

log = logging.getLogger(__name__)

MIN_VOLUME = 50            # skip never-traded markets (incl. parlay spam)
FINAL_HOURS = 120          # candles kept per market: the last 5 days before
                           # close — covers every lead our studies use (2h
                           # arms, 18h props, 72h retail) with headroom
INCREMENTAL_SINCE_DAYS = 3.0
DEFAULT_MAX_MARKETS = 1500  # per-cycle cap; remainder picked up next cycle


def _db_path() -> str:
    return os.getenv("CANDLE_PANEL_DB", "data/shared/candle_panel.sqlite")


def _con() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path)
    sqlite_schema.set_wal(c)   # busy-safe (R-27)
    c.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(c, store="candle_panel", migrations=SCHEMA,
                                db_path=path)
    return c


# Versioned via sqlite_schema (P2/R-19): v1 adopts the shape verbatim.
SCHEMA = [sqlite_schema.Migration(1, "adopt pre-versioning tables", [
    """CREATE TABLE IF NOT EXISTS markets (
        ticker TEXT PRIMARY KEY, event_id TEXT, category TEXT, question TEXT,
        close_time TEXT, resolved_yes INTEGER, volume REAL,
        fetched_at INTEGER, n_candles INTEGER)""",
    """CREATE TABLE IF NOT EXISTS candles (
        ticker TEXT, ts INTEGER, price REAL, yes_bid REAL, yes_ask REAL,
        volume REAL, open_interest REAL,
        PRIMARY KEY (ticker, ts))""",
])]


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _close_ts(close_time: str) -> int | None:
    """Close time -> unix seconds, or None on junk.

    >>> _close_ts("2026-08-02T01:00:00Z")
    1785632400
    >>> _close_ts("") is None
    True
    """
    try:
        return int(datetime.fromisoformat(
            str(close_time).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _fetch_days(close_ts: int, now: int | None = None) -> int:
    """`days` argument for get_candlesticks (which fetches [now-days, now])
    so the window reaches FINAL_HOURS before this market's close.

    >>> _fetch_days(1_000_000, now=1_000_000 + 6 * 86400)   # closed 6d ago
    12
    >>> _fetch_days(1_000_000, now=1_000_000)               # closed just now
    6
    """
    now = _now() if now is None else now
    days_since_close = max(0.0, (now - close_ts) / 86400.0)
    return int(days_since_close + FINAL_HOURS / 24.0 + 1)


def _store_candles(con: sqlite3.Connection, ticker: str, close_ts: int,
                   df: pd.DataFrame) -> int:
    """Insert candles within FINAL_HOURS before close (+1h grace after).
    Idempotent via the (ticker, ts) primary key."""
    if df is None or df.empty:
        return 0
    lo = close_ts - FINAL_HOURS * 3600
    hi = close_ts + 3600
    n = 0
    for _, r in df.iterrows():
        try:
            ts = int(pd.Timestamp(r["timestamp"]).timestamp())
        except (ValueError, TypeError):
            continue
        if not (lo <= ts <= hi):
            continue
        con.execute(
            "INSERT OR IGNORE INTO candles "
            "(ticker, ts, price, yes_bid, yes_ask, volume, open_interest) "
            "VALUES (?,?,?,?,?,?,?)",
            (ticker, ts,
             _f(r.get("price")), _f(r.get("yes_bid_close")),
             _f(r.get("yes_ask_close")), _f(r.get("volume")),
             _f(r.get("open_interest"))))
        n += 1
    return n


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if f == f else None       # NaN -> None
    except (TypeError, ValueError):
        return None


def run_cycle(*, since_days: float = INCREMENTAL_SINCE_DAYS,
              max_markets: int = DEFAULT_MAX_MARKETS,
              verbose: bool = True) -> dict:
    """Discover settled markets in the window, fetch candles for any not yet
    stored. Safe to re-run; per-ticker state makes it resumable mid-backfill."""
    con = _con()
    try:
        # max_items raised from the 10k default: the first live cycle hit that
        # cap on a 14d window (newest-first), which would silently orphan the
        # older in-window markets — they'd never re-surface as the window
        # slides forward.
        closed = kc.get_closed_markets_in_range(
            since_days=since_days, min_volume=MIN_VOLUME, max_items=40000)
    except Exception as e:  # noqa: BLE001
        log.warning("settled discovery failed: %s", e)
        con.close()
        return {"discovered": 0, "fetched": 0, "candles": 0}
    if closed is None or closed.empty:
        # Loud, per this week's silent-null lesson: an empty settled feed for
        # a multi-day window means the fetch broke, not that nothing settled.
        log.warning("settled discovery returned ZERO markets for %.1fd — "
                    "expected thousands; check the feed", since_days)
        con.close()
        return {"discovered": 0, "fetched": 0, "candles": 0}

    # Binary outcomes only (scalar/None unscoreable for outcome studies).
    closed = closed[closed["resolved_yes"].isin([True, False])]
    have = {r[0] for r in con.execute(
        "SELECT ticker FROM markets WHERE fetched_at IS NOT NULL")}
    todo = closed[~closed["id"].isin(have)]
    log.info("settled in window: %d binary (%d already stored, %d to fetch)",
             len(closed), len(closed) - len(todo), len(todo))

    fetched = candles = 0
    for _, r in todo.head(max_markets).iterrows():
        if shutdown.requested():
            break
        tk = str(r["id"])
        close_ts = _close_ts(r.get("closed_time") or r.get("close_time") or "")
        if close_ts is None:
            continue
        try:
            df = kc.get_candlesticks(tk, days=_fetch_days(close_ts), fidelity=60)
        except Exception as e:  # noqa: BLE001
            log.warning("candlesticks(%s) failed: %s", tk, e)
            continue
        n = _store_candles(con, tk, close_ts, df)
        con.execute(
            "INSERT OR REPLACE INTO markets (ticker, event_id, category, "
            "question, close_time, resolved_yes, volume, fetched_at, n_candles) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (tk, str(r.get("event_id", "") or ""), str(r.get("category", "") or ""),
             str(r.get("question", "") or "")[:140],
             str(r.get("closed_time") or r.get("close_time") or ""),
             1 if bool(r["resolved_yes"]) else 0,
             _f(r.get("volume")) or 0.0, _now(), n))
        fetched += 1
        candles += n
        if fetched % 200 == 0:
            con.commit()
            log.info("...%d markets fetched (%d candles)", fetched, candles)
        time.sleep(0.2)       # pace: ~1 call per ticker, 429-friendly
    con.commit()
    remaining = max(0, len(todo) - fetched)
    if verbose:
        print(f"discovered={len(closed)} fetched={fetched} "
              f"candles={candles} remaining={remaining}")
    con.close()
    return {"discovered": len(closed), "fetched": fetched,
            "candles": candles, "remaining": remaining}


# ─── Study surface ─────────────────────────────────────────────────────────

def load_panel(lead_hours: float, *, tolerance_hours: float = 1.0) -> pd.DataFrame:
    """One row per stored market: the executable candle nearest
    ``close - lead_hours`` (within tolerance) joined to the outcome.

    Columns: ticker, event_id, category, question, close_time, resolved_yes,
    market_volume, lead_hours (actual), yes_bid, yes_ask, price,
    candle_volume, open_interest. Rows lacking a candle near the requested
    lead are dropped — count the drop yourself if coverage matters.
    """
    con = _con()
    mk = pd.read_sql_query("SELECT * FROM markets WHERE fetched_at IS NOT NULL",
                           con)
    if mk.empty:
        con.close()
        return pd.DataFrame()
    out = []
    for _, m in mk.iterrows():
        close_ts = _close_ts(m["close_time"])
        if close_ts is None:
            continue
        want = close_ts - lead_hours * 3600
        row = con.execute(
            "SELECT ts, price, yes_bid, yes_ask, volume, open_interest "
            "FROM candles WHERE ticker=? AND ts BETWEEN ? AND ? "
            "ORDER BY ABS(ts - ?) LIMIT 1",
            (m["ticker"], want - tolerance_hours * 3600,
             want + tolerance_hours * 3600, want)).fetchone()
        if row is None:
            continue
        ts, price, bid, ask, cvol, oi = row
        out.append({
            "ticker": m["ticker"], "event_id": m["event_id"],
            "category": m["category"], "question": m["question"],
            "close_time": m["close_time"],
            "resolved_yes": bool(m["resolved_yes"]),
            "market_volume": m["volume"],
            "lead_hours": (close_ts - ts) / 3600.0,
            "yes_bid": bid, "yes_ask": ask, "price": price,
            "candle_volume": cvol, "open_interest": oi,
        })
    con.close()
    return pd.DataFrame(out)


def stats() -> dict:
    con = _con()
    n_mk = con.execute("SELECT COUNT(*) FROM markets "
                       "WHERE fetched_at IS NOT NULL").fetchone()[0]
    n_cd = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    span = con.execute("SELECT MIN(close_time), MAX(close_time) "
                       "FROM markets").fetchone()
    by_cat = dict(con.execute(
        "SELECT category, COUNT(*) FROM markets "
        "WHERE fetched_at IS NOT NULL GROUP BY category ORDER BY 2 DESC"))
    con.close()
    return {"markets": n_mk, "candles": n_cd, "close_span": span,
            "by_category": by_cat}


def main() -> None:
    import dotenv
    dotenv.load_dotenv()
    logctx.setup()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true",
                    help="deep pass: --since-days window, no per-cycle default cap")
    ap.add_argument("--since-days", type=float, default=None)
    ap.add_argument("--max-markets", type=int, default=None)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=21600)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    if args.stats:
        s = stats()
        print(f"candle_panel: markets={s['markets']} candles={s['candles']}")
        print(f"  close span: {s['close_span'][0]} -> {s['close_span'][1]}")
        for cat, n in s["by_category"].items():
            print(f"  {str(cat) or '(uncategorized)':<16} {n}")
        return
    since = args.since_days or (
        21.0 if args.backfill else INCREMENTAL_SINCE_DAYS)
    cap = args.max_markets or (10**9 if args.backfill else DEFAULT_MAX_MARKETS)
    if args.loop:
        shutdown.install()          # SIGTERM finishes the current market, then exits (R-20)
        while not shutdown.requested():
            run_cycle(since_days=since, max_markets=cap)
            if shutdown.sleep(max(300, args.interval)):
                break
        log.info("clean shutdown")
    else:
        run_cycle(since_days=since, max_markets=cap)


if __name__ == "__main__":
    main()
