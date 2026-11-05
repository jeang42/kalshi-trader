"""Mention-market repricing capture — measure-first instrument for the
live-mention-sniping thesis (2026-07-09).

The mention family (KXTRUMPMENTION / KXLOVEISLMENTION / KXLATENIGHTMENTION /
KXWCMENTION / KXEARNINGSMENTION*) is the fastest-cycling retail corner on
Kalshi: ~300+ resolutions per 3 weeks, tied to broadcasts (nightly shows,
speeches, earnings calls). A mention market settles YES *the moment the word
is said on stream* — deterministic resolution, audible on public streams our
existing GPU whisper stack (`audio_transcribe.py`) can transcribe live.

THE OPEN QUESTION (this instrument answers it before any trader is built):
when a word hits, does the thin retail book reprice instantly (a sniper
already exists → dead, like MLB) or grind up over 30s-minutes (a human
watching Peacock has to react → capturable window)? We measure the JUMP SHAPE.

How: every ~10s poll each mention series' full market list (ONE API call per
series returns every market's yes_bid/yes_ask/last/volume/status — no
per-book calls, no 429 risk) and delta-store rows whose quote/volume/status
changed. Result: full price paths around every mention resolution, passively,
across all live series. Post-hoc: align paths against Kalshi settled_time
(and later, transcript word-timestamps) → repricing-lag distribution.

Read-only. No trades. SQLite data/shared/mention_capture.sqlite
(env MENTION_CAPTURE_DB). CLI: single cycle | `--loop` | `--stats`.
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

def _db_path() -> str:
    """Resolved on every call, never frozen at import.

    A module-level ``DB = os.getenv(...)`` captures whatever the environment
    held at import time, so nothing downstream — tests especially — can
    redirect it. That is not hypothetical: it is how synthetic rows reached
    this project's production stores, twice, and back_nearclose.py carries the
    same note for the same reason.
    """
    return os.getenv("MENTION_CAPTURE_DB", "data/shared/mention_capture.sqlite")

# Static core of mention series to watch. Cheap to extend — one API call per
# series per cycle. Since 2026-09-01 this list is a SEED, not the whole
# watchlist: `discover_mention_series()` sweeps open events hourly for any
# series ticker containing MENTION, because a hardcoded list has now missed
# an earnings wave twice (Jul-14 banks, then the entire Sep wave — ORCL,
# COST, AVGO, 22 series — while this list still named the settled Q2 banks).
MENTION_SERIES = (
    "KXTRUMPMENTION", "KXTRUMPMENTIONB", "KXLOVEISLMENTION",
    "KXLATENIGHTMENTION", "KXWCMENTION", "KXMAMDANIMENTION",
    "KXDEBATEMENTION", "KXFTNMENTION", "KXWORLDNEWSMENTION",
    # Q2 earnings-call mention series (per-company; added 2026-07-18 after the
    # Jul-14 JPM/C/WFC calls went uncaptured — the series were never listed
    # here). Extend as more companies' call-mention series appear.
    "KXEARNINGSMENTIONJPM", "KXEARNINGSMENTIONC", "KXEARNINGSMENTIONWFC",
    "KXEARNINGSMENTIONGS", "KXEARNINGSMENTIONMS", "KXEARNINGSMENTIONBAC",
    # Sports announcer-mention series (added 2026-07-23). "What will the
    # announcers say during [game]" — thin, fan-priced, audibly-deterministic,
    # and HIGH-FREQUENCY (daily all season). Measure-first: does the book
    # cascade like the WC/political mention markets (23-60s, capturable) or
    # snipe-instantly? This is the productive redirect of the (dead) MLB
    # game-outcome reaction thesis — react to the CALL, not the play.
    "KXMLBMENTION", "KXWNBAMENTION",
)
SERIES_DISCOVER_EVERY_S = 3600  # re-sweep open events for new *MENTION* series
POLL_SLEEP_BETWEEN_SERIES = 1.0  # pacing between the per-series calls


# Versioned via sqlite_schema (P2/R-19): v1 adopts the shape verbatim.
SCHEMA = [sqlite_schema.Migration(1, "adopt pre-versioning tables", [
    """CREATE TABLE IF NOT EXISTS quotes (
        ticker TEXT, ts INTEGER, yes_bid REAL, yes_ask REAL, last REAL,
        volume REAL, status TEXT,
        PRIMARY KEY (ticker, ts))""",
    """CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY, v TEXT)""",
])]


def _con():
    c = sqlite3.connect(_db_path())
    sqlite_schema.set_wal(c)   # busy-safe (R-27)
    c.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(c, store="mention_capture", migrations=SCHEMA,
                                db_path=_db_path())
    return c


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def snapshot_series(series: str) -> pd.DataFrame:
    """One market-list call → all open markets in the series w/ top-of-book."""
    items = kc._paginate("markets",
                         params={"series_ticker": series, "status": "open", "limit": 1000},
                         items_key="markets", max_items=1000)
    if not items:
        return pd.DataFrame()
    rows = [kc._market_to_row(m, event_category="", event_title="") for m in items]
    return pd.DataFrame(rows)


def discover_mention_series() -> tuple[str, ...]:
    """Every open series whose ticker contains MENTION; () on venue failure.

    A full open-events sweep (~60 paginated calls) — hourly, not per-cycle.
    The caller unions this with the static seed list and keeps its previous
    watchlist when this returns (), so a failed sweep never shrinks coverage
    mid-broadcast.
    """
    try:
        events = kc._paginate("events", {"status": "open", "limit": 200},
                              items_key="events", max_items=12000)
    except Exception as e:  # noqa: BLE001 — degrade to "no change"
        log.warning("mention series discovery failed: %s", e)
        return ()
    found = {str(ev.get("event_ticker") or "").split("-")[0].upper()
             for ev in events}
    return tuple(sorted(t for t in found if "MENTION" in t))


def _merged_watchlist(dynamic: tuple[str, ...]) -> tuple[str, ...]:
    """Static seeds first, then discovered series not already named."""
    return MENTION_SERIES + tuple(t for t in dynamic if t not in MENTION_SERIES)


def changed_rows(df: pd.DataFrame, prev: dict) -> list[tuple]:
    """Delta filter: keep only tickers whose (bid, ask, last, volume) moved
    since the last stored state. ``prev`` maps ticker -> that tuple and is
    updated in place.

    >>> prev = {}
    >>> df = pd.DataFrame([{"id": "T-A", "best_bid": 0.2, "best_ask": 0.25,
    ...                     "last_trade_price": 0.22, "volume": 100.0}])
    >>> len(changed_rows(df, prev))          # first sight → stored
    1
    >>> len(changed_rows(df, prev))          # unchanged → skipped
    0
    """
    out = []
    ts = _now()
    for _, r in df.iterrows():
        tk = str(r.get("id", ""))
        if not tk:
            continue
        state = (r.get("best_bid"), r.get("best_ask"),
                 r.get("last_trade_price"), r.get("volume"))
        if prev.get(tk) == state:
            continue
        prev[tk] = state
        out.append((tk, ts,
                    _f(r.get("best_bid")), _f(r.get("best_ask")),
                    _f(r.get("last_trade_price")), _f(r.get("volume")),
                    str(r.get("status", "") or "")))
    return out


def _f(v):
    try:
        f = float(v)
        return f if f == f else None    # NaN → None
    except (TypeError, ValueError):
        return None


def run_loop(interval: float = 10.0) -> None:
    con = _con()
    prev: dict[str, tuple] = {}
    series = MENTION_SERIES
    next_discover = 0.0
    log.info("mention capture watching %d seed series every %.0fs", len(series), interval)
    shutdown.install()          # SIGTERM finishes the current series, then exits (R-20)
    while not shutdown.requested():
        t0 = time.time()
        if t0 >= next_discover:
            dyn = discover_mention_series()
            if dyn:
                merged = _merged_watchlist(dyn)
                new_series = sorted(set(merged) - set(series))
                if new_series:
                    log.info("mention discovery: +%d series %s",
                             len(new_series), new_series)
                series = merged
            next_discover = t0 + SERIES_DISCOVER_EVERY_S
        stored = 0
        for s in series:
            if shutdown.requested():
                break
            try:
                df = snapshot_series(s)
            except Exception as e:  # noqa: BLE001
                log.warning("snapshot failed for %s: %s", s, e)
                continue
            if df.empty:
                continue
            rows = changed_rows(df, prev)
            if rows:
                con.executemany(
                    "INSERT OR IGNORE INTO quotes VALUES (?,?,?,?,?,?,?)", rows)
                stored += len(rows)
            shutdown.sleep(POLL_SLEEP_BETWEEN_SERIES)
        con.commit()
        if stored:
            log.info("stored %d quote changes", stored)
        if shutdown.sleep(max(0.0, interval - (time.time() - t0))):
            break
    log.info("clean shutdown")


def run_cycle() -> dict:
    """Single pass (for testing / cron). Discovery included, one attempt."""
    con = _con()
    prev: dict[str, tuple] = {}
    stored = 0
    for s in _merged_watchlist(discover_mention_series()):
        try:
            df = snapshot_series(s)
        except Exception as e:  # noqa: BLE001
            print(f"snapshot failed for {s}: {e}")
            continue
        if not df.empty:
            rows = changed_rows(df, prev)
            con.executemany("INSERT OR IGNORE INTO quotes VALUES (?,?,?,?,?,?,?)", rows)
            stored += len(rows)
        time.sleep(POLL_SLEEP_BETWEEN_SERIES)
    con.commit()
    return {"stored": stored}


def load_quotes(ticker: str | None = None) -> pd.DataFrame:
    con = _con()
    q = "SELECT * FROM quotes"
    args: tuple = ()
    if ticker:
        q += " WHERE ticker=?"
        args = (ticker,)
    return pd.read_sql_query(q + " ORDER BY ts", con, params=args or None)


def stats() -> dict:
    con = _con()
    total, tickers = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ticker) FROM quotes").fetchone()
    first, last = con.execute("SELECT MIN(ts), MAX(ts) FROM quotes").fetchone()
    out = {"total": int(total or 0), "tickers": int(tickers or 0),
           "first_ts": first, "last_ts": last}
    print(f"mention_capture: {out['total']} quote-changes across {out['tickers']} markets"
          + (f" | span {datetime.fromtimestamp(first, UTC):%m-%d %H:%M} → "
              f"{datetime.fromtimestamp(last, UTC):%m-%d %H:%M}" if first else ""))
    return out


def main() -> None:
    logctx.setup()
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    if args.stats:
        stats()
    elif args.loop:
        run_loop(args.interval)
    else:
        print(run_cycle())


if __name__ == "__main__":
    main()
