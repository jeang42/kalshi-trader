"""Advisory capture — measure-first instrument for the scheduled-advisory
weather candidate (edge log §2026-08-24 late-3, THE SPEC; do not drift from
it without amending it there first).

Thesis under measurement: after an NHC advisory publishes, storm-linked
Kalshi books reprice slowly enough that a feed-reading process at
T0+seconds-to-minutes could capture spread-adjusted edge. House null: they
don't. This daemon only measures; **it places no orders and must never
import `kalshi_orders` or `pm.trading.execution`** (the `mention_listen`
load-bearing rule).

What it records:
- ``advisories`` — one row per (storm, product, advisory number): nominal
  issuance, NHC's ``fileUpdateTime`` (products publish ~27 min BEFORE the
  nominal slot — measured 2026-08-24, Iselle #005), our ``detected_at``, and
  the raw product text so content parsing (max winds, track table) can be
  redone offline. Rows loaded at daemon start are baseline, not detections:
  analysis must require ``detected_at − file_update_time`` small (≤ ~10 min)
  before using a row as a T0.
- ``books`` — top-3-level orderbook snapshots WITH SIZES (capacity is one of
  the questions), stored on change only + a heartbeat so staleness and
  no-change are distinguishable. ORDERBOOKS, not market rows — the markets
  endpoint zeroes quote fields (measured, spec §day-one).
- ``markets`` — the watchlist with close times and (joined later)
  resolutions.

Cadence per spec: NHC 1/min hot, 1/5 min idle; books every 15 s hot / 90 s
idle. Hot = inside [slot−45 min, slot+90 min] around the union of advisory
slots {03,09,15,21} UTC (covers NHC EDT/PDT and CPHC HST cycles), or within
90 min after any live detection.

CLI: ``python -m pm.capture.advisory_capture`` (one cycle) | ``--loop`` |
``--stats``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from pm.core import circuit_breaker, logctx, provenance, shutdown, sqlite_schema
from pm.feeds import nhc_data
from pm.venue import kalshi_client as kc

log = logging.getLogger(__name__)

SEASON_SERIES = ("KXTROPSTORM", "KXHURCTOT", "KXHURCTOTMAJ", "KXFIRSTHURRICANE",
                 "KXNEXTHURDATE", "KXHURPATHHAWAII", "KXHURPATHNC",
                 "KXHURRICANENAMES", "KXNEXTCAT5HURDATE")
PER_STORM_SERIES = ("KXHURCAT",)          # members matched to active storms
ADVISORY_SLOTS_UTC = (3, 9, 15, 21)
HOT_BEFORE_S, HOT_AFTER_S = 45 * 60, 90 * 60
BOOK_HOT_S, BOOK_IDLE_S = 15, 90
NHC_HOT_S, NHC_IDLE_S = 60, 300
WATCHLIST_REFRESH_S = 300
HEARTBEAT_S = 900                          # unchanged-book heartbeat row
RESOLVE_EVERY_S = 3600

SCHEMA = [sqlite_schema.Migration(1, "initial advisory-capture schema", [
    """CREATE TABLE IF NOT EXISTS advisories (
        storm_id      TEXT NOT NULL,
        product       TEXT NOT NULL,
        adv_num       TEXT NOT NULL,
        issuance      TEXT,
        file_update_time TEXT,
        detected_at   INTEGER NOT NULL,
        baseline      INTEGER NOT NULL DEFAULT 0 CHECK (baseline IN (0,1)),
        raw_text      TEXT,
        commit_sha    TEXT,
        PRIMARY KEY (storm_id, product, adv_num))""",
    """CREATE TABLE IF NOT EXISTS books (
        -- ts is REAL: two snapshots of one ticker in the same SECOND (a
        -- change landing right after a heartbeat in the hot loop) collided
        -- on an INTEGER PK and INSERT OR IGNORE swallowed the CHANGE — the
        -- R-19 lesson, caught by the test before deployment.
        ticker  TEXT NOT NULL, ts REAL NOT NULL,
        bid1 REAL, bid1_sz REAL, bid2 REAL, bid2_sz REAL, bid3 REAL, bid3_sz REAL,
        ask1 REAL, ask1_sz REAL, ask2 REAL, ask2_sz REAL, ask3 REAL, ask3_sz REAL,
        heartbeat INTEGER NOT NULL DEFAULT 0 CHECK (heartbeat IN (0,1)),
        PRIMARY KEY (ticker, ts))""",
    """CREATE TABLE IF NOT EXISTS markets (
        ticker TEXT PRIMARY KEY, series TEXT, title TEXT, close_time TEXT,
        first_seen INTEGER NOT NULL, resolved_yes INTEGER, resolved_at INTEGER,
        commit_sha TEXT)""",
])]


def _db_path() -> str:
    return os.getenv("ADVISORY_CAPTURE_DB", "data/shared/advisory_capture.sqlite")


def _con() -> sqlite3.Connection:
    Path(_db_path()).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_db_path())
    sqlite_schema.set_wal(c)   # busy-safe (R-27)
    c.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(c, store="advisory_capture", migrations=SCHEMA,
                                db_path=_db_path())
    return c


def _now() -> int:
    return int(time.time())


# ─── hot-window logic (pure) ───────────────────────────────────────────────

def in_hot_window(ts: int, *, last_detection: int | None = None) -> bool:
    """Inside [slot−45m, slot+90m] of any advisory slot, or within 90 min of
    a live detection.

    >>> from datetime import datetime, UTC
    >>> t = int(datetime(2026, 8, 24, 14, 30, tzinfo=UTC).timestamp())
    >>> in_hot_window(t)          # 14:30Z is inside [14:15, 16:30] for slot 15
    True
    >>> t2 = int(datetime(2026, 8, 24, 12, 0, tzinfo=UTC).timestamp())
    >>> in_hot_window(t2)
    False
    >>> in_hot_window(t2, last_detection=t2 - 60)
    True
    """
    if last_detection is not None and 0 <= ts - last_detection <= HOT_AFTER_S:
        return True
    dt = datetime.fromtimestamp(ts, UTC)
    day = int(datetime(dt.year, dt.month, dt.day, tzinfo=UTC).timestamp())
    for slot in ADVISORY_SLOTS_UTC:
        s = day + slot * 3600
        for cand in (s - 86400, s, s + 86400):
            if cand - HOT_BEFORE_S <= ts <= cand + HOT_AFTER_S:
                return True
    return False


# ─── advisory detection ────────────────────────────────────────────────────

_PRODUCT_KEYS = ("publicAdvisory", "forecastAdvisory", "windSpeedProbabilities",
                 "forecastDiscussion")


def _fetch_product_text_unguarded(url: str) -> str:
    r = requests.get(url, timeout=20,
                     headers={"User-Agent": "polymarket-analyzer advisory-capture"})
    r.raise_for_status()
    return r.text


def fetch_product_text(url: str) -> str | None:
    try:
        return circuit_breaker.get("nhc").call(_fetch_product_text_unguarded, url)
    except (circuit_breaker.CircuitOpenError, requests.RequestException) as e:
        log.warning("advisory text fetch failed (%s): %s", url, e)
        return None


def _fetch_current_unguarded() -> dict | None:
    r = requests.get(f"{nhc_data.NHC_BASE}/CurrentStorms.json", timeout=20,
                     headers={"User-Agent": "polymarket-analyzer advisory-capture"})
    r.raise_for_status()
    return r.json()


def poll_advisories(con: sqlite3.Connection, *, baseline: bool = False) -> tuple[int, list[nhc_data.Storm]]:
    """Detect new advisory products; returns (n_new, active named storms)."""
    try:
        data = circuit_breaker.get("nhc").call(_fetch_current_unguarded)
    except (circuit_breaker.CircuitOpenError, requests.RequestException) as e:
        log.warning("NHC poll failed: %s", e)
        return 0, []
    storms = nhc_data.parse_current_storms(data)
    named = [s for s in storms if s.is_named]
    n_new = 0
    for raw in (data or {}).get("activeStorms") or []:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("id") or "").lower()
        if not sid:
            continue
        for key in _PRODUCT_KEYS:
            prod = raw.get(key)
            if not isinstance(prod, dict):
                continue
            adv = str(prod.get("advNum") or "").strip()
            if not adv:
                continue
            seen = con.execute(
                "SELECT 1 FROM advisories WHERE storm_id=? AND product=? AND adv_num=?",
                (sid, key, adv)).fetchone()
            if seen:
                continue
            url = str(prod.get("url") or "")
            text = fetch_product_text(url) if (url and not baseline) else None
            con.execute(
                "INSERT OR IGNORE INTO advisories (storm_id, product, adv_num, "
                "issuance, file_update_time, detected_at, baseline, raw_text, commit_sha) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, key, adv, prod.get("issuance"), prod.get("fileUpdateTime"),
                 _now(), 1 if baseline else 0, text, provenance.label()))
            n_new += 1
            if not baseline:
                log.info("ADVISORY %s %s #%s (issuance=%s fileUpdate=%s)",
                         sid, key, adv, prod.get("issuance"),
                         prod.get("fileUpdateTime"))
    con.commit()
    return n_new, named


# ─── watchlist ─────────────────────────────────────────────────────────────

def refresh_watchlist(con: sqlite3.Connection, named: list[nhc_data.Storm]) -> list[str]:
    """Open members of the season series + KXHURCAT members for active storms."""
    tickers: list[str] = []
    names = {s.name.upper() for s in named}
    for series in SEASON_SERIES + PER_STORM_SERIES:
        try:
            rows = kc._paginate("markets", {"series_ticker": series,
                                            "status": "open", "limit": 100},
                                items_key="markets", max_items=400)
        except Exception as e:  # noqa: BLE001 — a dead venue skips one refresh
            log.warning("watchlist: %s failed: %s", series, e)
            continue
        for r in rows:
            t = str(r.get("ticker") or "")
            if not t:
                continue
            if series in PER_STORM_SERIES:
                event = t.split("-")[1] if "-" in t else ""
                if not any(n in event for n in names):
                    continue        # only ACTIVE storms' members at 15s cadence
            tickers.append(t)
            con.execute(
                "INSERT OR IGNORE INTO markets (ticker, series, title, close_time, "
                "first_seen, commit_sha) VALUES (?,?,?,?,?,?)",
                (t, series, str(r.get("title") or ""),
                 str(r.get("close_time") or ""), _now(), provenance.label()))
    con.commit()
    return tickers


# ─── book snapshots ────────────────────────────────────────────────────────

def _top3(levels: list[dict], *, reverse: bool) -> list[tuple[float, float]]:
    parsed = []
    for lv in levels or []:
        try:
            parsed.append((float(lv["price"]), float(lv["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    parsed.sort(key=lambda x: x[0], reverse=reverse)
    return parsed[:3]


def snapshot_book(ticker: str) -> tuple | None:
    """(bid1, bid1_sz, …, ask3_sz) best-first, None-padded; None on failure."""
    try:
        ob = kc.get_order_book(ticker)
    except Exception as e:  # noqa: BLE001
        log.debug("book %s failed: %s", ticker, e)
        return None
    bids = _top3(ob.get("bids") or [], reverse=True)    # best bid = highest
    asks = _top3(ob.get("asks") or [], reverse=False)   # best ask = lowest
    flat: list[float | None] = []
    for side in (bids, asks):
        for i in range(3):
            flat.extend(side[i] if i < len(side) else (None, None))
    return tuple(flat)


def store_books(con: sqlite3.Connection, tickers: list[str],
                last: dict[str, tuple], last_hb: dict[str, int]) -> int:
    """Store changed books (+ heartbeats). Mutates `last`/`last_hb`."""
    n = 0
    now = _now()
    for t in tickers:
        if shutdown.requested():
            break
        snap = snapshot_book(t)
        if snap is None:
            continue
        changed = last.get(t) != snap
        hb_due = now - last_hb.get(t, 0) >= HEARTBEAT_S
        if not changed and not hb_due:
            continue
        con.execute(
            "INSERT OR IGNORE INTO books (ticker, ts, bid1,bid1_sz,bid2,bid2_sz,"
            "bid3,bid3_sz, ask1,ask1_sz,ask2,ask2_sz,ask3,ask3_sz, heartbeat) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t, time.time(), *snap, 0 if changed else 1))
        last[t] = snap
        last_hb[t] = now
        n += 1
    con.commit()
    return n


# ─── resolutions ───────────────────────────────────────────────────────────

def resolve_cycle(con: sqlite3.Connection) -> int:
    n = 0
    rows = con.execute(
        "SELECT ticker FROM markets WHERE resolved_yes IS NULL "
        "AND close_time != '' AND close_time < ?",
        (datetime.now(UTC).isoformat(),)).fetchall()
    for (t,) in rows:
        if shutdown.requested():
            break
        try:
            m = kc.get_market(t)
        except Exception:  # noqa: BLE001 — retry next hour
            continue
        if m.get("status") == "finalized" and m.get("result") in ("yes", "no"):
            con.execute("UPDATE markets SET resolved_yes=?, resolved_at=? WHERE ticker=?",
                        (1 if m["result"] == "yes" else 0, _now(), t))
            n += 1
        time.sleep(0.2)
    con.commit()
    return n


# ─── daemon ────────────────────────────────────────────────────────────────

def stats() -> dict:
    con = _con()
    out = {
        "advisories": con.execute("SELECT COUNT(*) FROM advisories WHERE baseline=0").fetchone()[0],
        "baseline": con.execute("SELECT COUNT(*) FROM advisories WHERE baseline=1").fetchone()[0],
        "books": con.execute("SELECT COUNT(*) FROM books").fetchone()[0],
        "tickers": con.execute("SELECT COUNT(DISTINCT ticker) FROM books").fetchone()[0],
        "markets": con.execute("SELECT COUNT(*) FROM markets").fetchone()[0],
        "resolved": con.execute("SELECT COUNT(*) FROM markets WHERE resolved_yes IS NOT NULL").fetchone()[0],
    }
    con.close()
    return out


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        from pm import REPO_ROOT
        load_dotenv(REPO_ROOT / ".env")     # gotcha #4
    except ImportError:  # pragma: no cover
        pass
    logctx.setup()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)
    if args.stats:
        s = stats()
        print("advisory_capture:", " ".join(f"{k}={v}" for k, v in s.items()))
        return 0

    shutdown.install()
    con = _con()
    # First contact loads the CURRENT advisory state as BASELINE rows —
    # detected_at ≈ now says nothing about publication for these; the
    # analysis must use baseline=0 rows only.
    with logctx.bind(capture="advisory", cycle=logctx.new_id("cyc")):
        n0, named = poll_advisories(con, baseline=True)
        tickers = refresh_watchlist(con, named)
        log.info("start: baseline_products=%d named=%d watchlist=%d",
                 n0, len(named), len(tickers))

    last_books: dict[str, tuple] = {}
    last_hb: dict[str, int] = {}
    last_nhc = last_wl = last_resolve = 0.0
    last_detection: int | None = None

    while not shutdown.requested():
        with logctx.bind(capture="advisory", cycle=logctx.new_id("cyc")):
            now = _now()
            hot = in_hot_window(now, last_detection=last_detection)
            if now - last_nhc >= (NHC_HOT_S if hot else NHC_IDLE_S):
                n_new, named = poll_advisories(con)
                last_nhc = now
                if n_new:
                    last_detection = now
                    hot = True
            if now - last_wl >= WATCHLIST_REFRESH_S:
                tickers = refresh_watchlist(con, named)
                last_wl = now
            n_books = store_books(con, tickers, last_books, last_hb)
            if now - last_resolve >= RESOLVE_EVERY_S:
                n_res = resolve_cycle(con)
                last_resolve = now
                if n_res:
                    log.info("resolved=%d", n_res)
            if n_books:
                log.info("hot=%s books_stored=%d watchlist=%d", hot, n_books,
                         len(tickers))
        if not args.loop:
            break
        if shutdown.sleep(BOOK_HOT_S if hot else BOOK_IDLE_S):
            break
    con.close()
    log.info("clean shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
