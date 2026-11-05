"""Persistent archive of executed-trade prints (the Kalshi "trade tape").

Sibling of ``book_archive.py``. Where ``book_archive`` stores *resting
liquidity* snapshots (the order book at a point in time), this stores the
*executed flow* — the actual sequence of fills from Kalshi's public
``markets/trades`` feed, each tagged with the aggressor side.

Why a second store rather than reusing book_archive:
- Different grain: a trade is a discrete event (one fill), not a periodic
  snapshot. Keyed on Kalshi's ``trade_id`` so re-fetching overlapping time
  windows during backfill is idempotent (INSERT OR IGNORE).
- Different query shape: "all trades for ticker T between t0 and t1",
  which the lead-lag analysis and the longshot scalper both want.

The data captured here is BOTH a live feed (poll/stream new prints) AND a
historical backfill source — ``markets/trades`` is cursor-paginated with
min_ts/max_ts, so we can pull a finished game's entire tape after the fact.
That fills the "book_archive is forward-only" gap noted in
``baseball/analysis.py``: trade history exists from before we started
polling.

Storage path: ``data/shared/trade_archive.sqlite`` (override via
``TRADE_ARCHIVE_PATH``). WAL + ``synchronous=NORMAL`` mirror book_archive's
HDD-friendly durability trade-off.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pm.core import sqlite_schema

log = logging.getLogger(__name__)

UTC = UTC

DEFAULT_ARCHIVE_PATH = Path("data/shared/trade_archive.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id           TEXT    PRIMARY KEY,
    ticker             TEXT    NOT NULL,
    ts_unix            INTEGER NOT NULL,
    count              REAL    NOT NULL,
    yes_price          REAL,
    no_price           REAL,
    taker_side         TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts
    ON trades(ticker, ts_unix);
CREATE INDEX IF NOT EXISTS idx_trades_ts
    ON trades(ts_unix);
"""

# Versioned via sqlite_schema (P2/R-19): v1 adopts the shape above verbatim
# (every statement is IF NOT EXISTS, so it is the same for a fresh file and
# a legacy one at user_version 0). Future changes go on this list.
SCHEMA = [sqlite_schema.Migration(
    1, "adopt pre-versioning tables",
    [stmt.strip() for stmt in _SCHEMA.split(";") if stmt.strip()])]


_COLUMNS = ("trade_id", "ticker", "ts_unix", "count",
            "yes_price", "no_price", "taker_side")


def _path() -> Path:
    """Resolved archive path. ``TRADE_ARCHIVE_PATH`` env var overrides."""
    return Path(os.getenv("TRADE_ARCHIVE_PATH", str(DEFAULT_ARCHIVE_PATH)))


def _connect() -> sqlite3.Connection:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    sqlite_schema.set_wal(conn)   # busy-safe (R-27)
    conn.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(conn, store="trade_archive", migrations=SCHEMA, db_path=p)
    return conn


@dataclass(frozen=True)
class Trade:
    """One executed trade, decoded from a SQLite row."""

    trade_id: str
    ticker: str
    ts_unix: int
    count: float
    yes_price: float | None
    no_price: float | None
    taker_side: str

    @property
    def ts(self) -> datetime:
        return datetime.fromtimestamp(self.ts_unix, tz=UTC)


def store_trades(trades: list[dict]) -> int:
    """Bulk-insert normalized trade rows in one transaction.

    Each item is a dict with the keys in ``_COLUMNS`` (the shape produced
    by ``kalshi_client._normalize_trade`` / ``get_all_trades`` rows).
    Returns the count of rows newly inserted — ``INSERT OR IGNORE`` on the
    ``trade_id`` primary key makes re-fetching an overlapping window
    idempotent (already-seen trades are skipped, not duplicated, and don't
    count toward the return value).

    Rows missing required keys are skipped quietly rather than aborting the
    whole batch — partial success beats all-or-nothing during a long
    backfill.
    """
    if not trades:
        return 0
    rows: list[tuple] = []
    for t in trades:
        try:
            rows.append((
                str(t["trade_id"]),
                str(t["ticker"]),
                int(t["ts_unix"]),
                float(t["count"]),
                None if t.get("yes_price") is None else float(t["yes_price"]),
                None if t.get("no_price") is None else float(t["no_price"]),
                str(t.get("taker_side") or ""),
            ))
        except (KeyError, TypeError, ValueError) as e:
            log.warning("trade_archive.store_trades: skipping bad row: %s", e)
            continue
    if not rows:
        return 0
    try:
        with _connect() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO trades "
                "(trade_id, ticker, ts_unix, count, yes_price, no_price, taker_side) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            return conn.total_changes - before
    except sqlite3.Error as e:
        log.warning("trade_archive.store_trades failed: %s", e)
        return 0


def store_trades_df(df: pd.DataFrame) -> int:
    """Convenience: store a ``get_all_trades``-shaped DataFrame. Extra
    columns (``ts``) are ignored; only ``_COLUMNS`` are persisted."""
    if df is None or df.empty:
        return 0
    cols = [c for c in _COLUMNS if c in df.columns]
    return store_trades(df[cols].to_dict("records"))


def get_trades_in_range(
    ticker: str, *, since_ts: int, until_ts: int,
) -> pd.DataFrame:
    """All archived trades for ``ticker`` with ``since_ts <= ts_unix <=
    until_ts``, oldest-first. Columns match ``kalshi_client.get_all_trades``
    plus a derived ``ts`` datetime. Empty DataFrame when nothing matches.
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT trade_id, ticker, ts_unix, count, yes_price, "
                "no_price, taker_side FROM trades "
                "WHERE ticker = ? AND ts_unix BETWEEN ? AND ? "
                "ORDER BY ts_unix",
                (str(ticker), int(since_ts), int(until_ts)),
            ).fetchall()
    except sqlite3.Error as e:
        log.warning("trade_archive.get_trades_in_range failed: %s", e)
        rows = []
    df = pd.DataFrame(rows, columns=list(_COLUMNS))
    if df.empty:
        return df.assign(ts=pd.Series(dtype="datetime64[ns, UTC]"))
    df["ts"] = pd.to_datetime(df["ts_unix"], unit="s", utc=True)
    return df


def latest_trade_ts(ticker: str) -> int | None:
    """Unix-second timestamp of the most recent archived trade for
    ``ticker``, or None if we have none. The backfill/poller uses this as
    the ``since_ts`` for an incremental pull so it only fetches new prints.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT MAX(ts_unix) FROM trades WHERE ticker = ?",
                (str(ticker),),
            ).fetchone()
    except sqlite3.Error as e:
        log.warning("trade_archive.latest_trade_ts failed: %s", e)
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def purge_older_than(*, days: int) -> int:
    """Delete trades older than ``days`` days. Returns the count removed."""
    cutoff = int(time.time()) - days * 86_400
    try:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM trades WHERE ts_unix < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount)
    except sqlite3.Error as e:
        log.warning("trade_archive.purge_older_than failed: %s", e)
        return 0


def clear() -> int:
    """Delete every trade. Returns count removed."""
    try:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM trades")
            conn.commit()
            return int(cur.rowcount)
    except sqlite3.Error as e:
        log.warning("trade_archive.clear failed: %s", e)
        return 0


def stats() -> dict:
    """Counts + coverage for a Settings-page panel. Mirrors
    ``book_archive.stats``: ``entries`` / ``markets`` / ``oldest_age_s`` /
    ``newest_age_s`` / ``file_bytes``."""
    out: dict = {
        "entries": 0,
        "markets": 0,
        "oldest_age_s": None,
        "newest_age_s": None,
        "file_bytes": 0,
    }
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(ts_unix), MAX(ts_unix), "
                "COUNT(DISTINCT ticker) FROM trades",
            ).fetchone()
            out["entries"] = int(row[0])
            now = int(time.time())
            if row[1] is not None:
                out["oldest_age_s"] = now - int(row[1])
            if row[2] is not None:
                out["newest_age_s"] = now - int(row[2])
            out["markets"] = int(row[3])
    except sqlite3.Error as e:
        log.warning("trade_archive.stats failed: %s", e)

    p = _path()
    if p.exists():
        try:
            out["file_bytes"] = p.stat().st_size
        except OSError:
            pass
    return out


__all__ = [
    "Trade",
    "DEFAULT_ARCHIVE_PATH",
    "store_trades",
    "store_trades_df",
    "get_trades_in_range",
    "latest_trade_ts",
    "purge_older_than",
    "clear",
    "stats",
]
