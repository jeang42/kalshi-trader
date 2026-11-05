"""Persistent archive of order-book snapshots.

Why: vendors' archival order-book history is paywalled, and neither
Polymarket nor Kalshi exposes a `/book/history` endpoint. To eventually
backtest slippage, we need to capture the live ``/book`` ourselves over
time and let the archive accumulate. **The data captured today becomes
historical data tomorrow** — the design pays off after weeks of running.

What this module is and isn't:
- It **is** a SQLite K/V-ish store for parsed-order-book snapshots
  keyed on ``(venue, market_id, token_id, snapshot_ts)``, with
  lookup-at-time semantics so the backtest engine can ask for "the
  book at as_of" once enough history exists.
- It **isn't** the poller itself. That's ``book_poller.py``, a
  separate runnable script — Streamlit doesn't do background work, so
  the user runs the poller via cron / systemd / tmux.

Storage path: ``data/shared/book_archive.sqlite`` (override via
``BOOK_ARCHIVE_PATH`` env var). Sibling of ``llm_cache.sqlite`` since
neither is profile-scoped — book history is venue-wide.
"""

from __future__ import annotations

import json
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

DEFAULT_ARCHIVE_PATH = Path("data/shared/book_archive.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS book_snapshots (
    venue        TEXT    NOT NULL,
    market_id    TEXT    NOT NULL,
    token_id     TEXT    NOT NULL,
    snapshot_ts  INTEGER NOT NULL,
    book_json    TEXT    NOT NULL,
    PRIMARY KEY (venue, market_id, token_id, snapshot_ts)
);
CREATE INDEX IF NOT EXISTS idx_lookup
    ON book_snapshots(venue, market_id, token_id, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_by_ts
    ON book_snapshots(snapshot_ts);
"""

# Versioned via sqlite_schema (P2/R-19): v1 adopts the shape above verbatim
# (every statement is IF NOT EXISTS, so it is the same for a fresh file and
# a legacy one at user_version 0). Future changes go on this list.
SCHEMA = [sqlite_schema.Migration(
    1, "adopt pre-versioning tables",
    [stmt.strip() for stmt in _SCHEMA.split(";") if stmt.strip()])]



# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _path() -> Path:
    """Resolved archive path. ``BOOK_ARCHIVE_PATH`` env var overrides."""
    return Path(os.getenv("BOOK_ARCHIVE_PATH", str(DEFAULT_ARCHIVE_PATH)))


def _connect() -> sqlite3.Connection:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    # WAL + synchronous=NORMAL is the standard high-throughput
    # configuration for SQLite. WAL converts random rollback-journal
    # writes into sequential WAL appends (big win on HDD; small win on
    # SSD). synchronous=NORMAL drops the fsync from every commit to
    # checkpoint-time — durable across an app crash but not a power
    # loss. For an order-book archive that's a fine trade: if we lose
    # the last few seconds on power loss, the next poller cycle just
    # writes new snapshots. Both pragmas are idempotent and durable
    # across connections.
    sqlite_schema.set_wal(conn)   # busy-safe (R-27)
    conn.execute("PRAGMA synchronous=NORMAL")
    sqlite_schema.ensure_schema(conn, store="book_archive", migrations=SCHEMA, db_path=p)
    return conn


# ---------------------------------------------------------------------------
# Snapshot serialization
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BookSnapshot:
    """One archived order-book snapshot, decoded from the SQLite row."""

    venue: str
    market_id: str
    token_id: str
    snapshot_ts: int            # unix seconds
    book: pd.DataFrame          # parse_order_book-shaped DataFrame

    @property
    def snapshot_dt(self) -> datetime:
        return datetime.fromtimestamp(self.snapshot_ts, tz=UTC)


def _book_to_json(book: pd.DataFrame) -> str:
    """Serialize a parsed-order-book DataFrame to a compact JSON blob.

    We keep only the columns the schema requires (``side``, ``price``,
    ``size``) — ``cumulative_size`` is derived and can be recomputed
    on load. Dropping it shrinks each row by ~20%.
    """
    if book is None or book.empty:
        return "[]"
    rows = [
        {"side": str(r["side"]),
         "price": float(r["price"]),
         "size": float(r["size"])}
        for _, r in book.iterrows()
    ]
    return json.dumps(rows, separators=(",", ":"))


def _json_to_book(payload: str) -> pd.DataFrame:
    """Reverse of :func:`_book_to_json`. Recomputes ``cumulative_size``
    on each side so loaded snapshots match what ``parse_order_book``
    would return live."""
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError:
        return pd.DataFrame(columns=["side", "price", "size", "cumulative_size"])
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame(columns=["side", "price", "size", "cumulative_size"])
    df = pd.DataFrame(rows)
    if df.empty:
        return df.assign(cumulative_size=pd.Series(dtype=float))
    # Recompute cumulative_size best-first per side (parse_order_book's
    # contract: bids sorted descending, asks ascending).
    bids = df[df["side"] == "bid"].sort_values("price", ascending=False)
    asks = df[df["side"] == "ask"].sort_values("price")
    bids = bids.assign(cumulative_size=bids["size"].cumsum())
    asks = asks.assign(cumulative_size=asks["size"].cumsum())
    return pd.concat([bids, asks], ignore_index=True)


def best_bid_ask(book: pd.DataFrame | None) -> tuple[float | None, float | None]:
    """Best bid (highest bid price) and best ask (lowest ask price) from a
    ``parse_order_book``-shaped frame — i.e. a :class:`BookSnapshot`'s
    ``.book``. ``None`` per side when that side is empty.

    Lived in ``longshot_scalper`` until that daemon was retired to
    ``attic/`` (2026-08-17); ``reactive_scalper`` still needs it, and a
    snapshot's top-of-book belongs with the snapshot store anyway.

    >>> import pandas as pd
    >>> book = pd.DataFrame({"side": ["bid", "bid", "ask", "ask"],
    ...                      "price": [0.40, 0.42, 0.45, 0.44],
    ...                      "size": [10, 5, 7, 3]})
    >>> best_bid_ask(book)
    (0.42, 0.44)
    >>> best_bid_ask(book[book["side"] == "bid"])
    (0.42, None)
    >>> best_bid_ask(None)
    (None, None)
    >>> best_bid_ask(pd.DataFrame(columns=["side", "price", "size"]))
    (None, None)
    """
    if book is None or book.empty:
        return None, None
    bids = book[book["side"] == "bid"]["price"]
    asks = book[book["side"] == "ask"]["price"]
    bid = float(bids.max()) if not bids.empty else None
    ask = float(asks.min()) if not asks.empty else None
    return bid, ask


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def store_snapshot(
    *, venue: str, market_id: str, token_id: str,
    book: pd.DataFrame, ts: int | None = None,
) -> bool:
    """Insert (or replace) a snapshot. Returns True on success.

    ``ts`` defaults to "now" in unix seconds. Empty books are stored —
    "the book was empty at this time" is itself a data point we'd want
    when backtesting illiquid periods.
    """
    snapshot_ts = int(ts) if ts is not None else int(time.time())
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO book_snapshots "
                "(venue, market_id, token_id, snapshot_ts, book_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(venue), str(market_id), str(token_id),
                 snapshot_ts, _book_to_json(book)),
            )
            conn.commit()
        return True
    except sqlite3.Error as e:
        log.warning("book_archive.store_snapshot failed (%s/%s): %s",
                    venue, market_id, e)
        return False


def store_snapshots(snapshots: list[dict]) -> int:
    """Bulk-insert many snapshots in a single transaction.

    Each item is a dict with keys ``venue``, ``market_id``,
    ``token_id``, ``book`` (a parse_order_book-shaped DataFrame), and
    optionally ``ts`` (unix seconds, defaults to "now" *for that row*
    — assigned at iteration time so a slow batch still gets
    monotonically-increasing timestamps).

    Returns the count of rows successfully inserted. One transaction =
    one fsync, which matters on spinning rust: the poller's per-cycle
    cost drops from ~N fsyncs to 1 even at high market counts.

    Rows that fail to serialize (bad book payload) are skipped quietly
    rather than aborting the whole batch — partial success is more
    useful than all-or-nothing here.
    """
    if not snapshots:
        return 0

    now = int(time.time())
    rows: list[tuple] = []
    for s in snapshots:
        try:
            row = (
                str(s["venue"]),
                str(s["market_id"]),
                str(s["token_id"]),
                int(s.get("ts", now)),
                _book_to_json(s["book"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            log.warning("book_archive.store_snapshots: skipping bad row: %s", e)
            continue
        rows.append(row)
    if not rows:
        return 0

    try:
        with _connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO book_snapshots "
                "(venue, market_id, token_id, snapshot_ts, book_json) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        return len(rows)
    except sqlite3.Error as e:
        log.warning("book_archive.store_snapshots failed: %s", e)
        return 0


def get_snapshot_at(
    *, venue: str, market_id: str, token_id: str, ts: datetime,
) -> BookSnapshot | None:
    """Return the most recent snapshot at-or-before ``ts``. None if no
    earlier snapshot exists for this (venue, market, token) triple.

    This is the lookup the backtest engine eventually uses to ask for
    "what was the book at snap.as_of." Until the archive has been
    running for a while, most calls return None — which is fine, the
    caller falls back to the existing half-spread model.
    """
    target = int(ts.timestamp())
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT snapshot_ts, book_json FROM book_snapshots "
                "WHERE venue = ? AND market_id = ? AND token_id = ? "
                "AND snapshot_ts <= ? "
                "ORDER BY snapshot_ts DESC LIMIT 1",
                (str(venue), str(market_id), str(token_id), target),
            ).fetchone()
    except sqlite3.Error as e:
        log.warning("book_archive.get_snapshot_at failed: %s", e)
        return None
    if row is None:
        return None
    snapshot_ts, payload = row
    return BookSnapshot(
        venue=venue, market_id=market_id, token_id=token_id,
        snapshot_ts=int(snapshot_ts), book=_json_to_book(payload),
    )


def has_recent_snapshot(
    *, venue: str, market_id: str, token_id: str, within_seconds: int,
) -> bool:
    """True if a snapshot for (venue, market, token) was stored within
    the last ``within_seconds``. The poller uses this to avoid
    re-polling markets that don't move much."""
    cutoff = int(time.time()) - int(within_seconds)
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM book_snapshots "
                "WHERE venue = ? AND market_id = ? AND token_id = ? "
                "AND snapshot_ts >= ? LIMIT 1",
                (str(venue), str(market_id), str(token_id), cutoff),
            ).fetchone()
    except sqlite3.Error as e:
        log.warning("book_archive.has_recent_snapshot failed: %s", e)
        return False
    return row is not None


def purge_older_than(*, days: int) -> int:
    """Delete snapshots older than ``days`` days. Returns the count
    removed. Useful for capping disk usage — order-book snapshots
    aren't valuable in perpetuity; a typical strategy backtest only
    needs the last few months."""
    cutoff = int(time.time()) - days * 86_400
    try:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM book_snapshots WHERE snapshot_ts < ?", (cutoff,),
            )
            conn.commit()
            return int(cur.rowcount)
    except sqlite3.Error as e:
        log.warning("book_archive.purge_older_than failed: %s", e)
        return 0


def clear() -> int:
    """Delete every snapshot. Returns count removed."""
    try:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM book_snapshots")
            conn.commit()
            return int(cur.rowcount)
    except sqlite3.Error as e:
        log.warning("book_archive.clear failed: %s", e)
        return 0


def stats() -> dict:
    """Counts + coverage for the Settings page panel.

    Returns ``entries`` / ``markets`` / ``venues`` / ``oldest_age_s`` /
    ``newest_age_s`` / ``file_bytes``. Empty archive returns zeros and
    None ages.
    """
    out: dict = {
        "entries": 0,
        "markets": 0,
        "venues": [],
        "oldest_age_s": None,
        "newest_age_s": None,
        "file_bytes": 0,
    }
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(snapshot_ts), MAX(snapshot_ts) "
                "FROM book_snapshots",
            ).fetchone()
            out["entries"] = int(row[0])
            now = int(time.time())
            if row[1] is not None:
                out["oldest_age_s"] = now - int(row[1])
            if row[2] is not None:
                out["newest_age_s"] = now - int(row[2])
            m_row = conn.execute(
                "SELECT COUNT(DISTINCT market_id) FROM book_snapshots",
            ).fetchone()
            out["markets"] = int(m_row[0])
            v_rows = conn.execute(
                "SELECT venue, COUNT(*) FROM book_snapshots GROUP BY venue",
            ).fetchall()
            out["venues"] = [(v, int(c)) for v, c in v_rows]
    except sqlite3.Error as e:
        log.warning("book_archive.stats failed: %s", e)

    p = _path()
    if p.exists():
        try:
            out["file_bytes"] = p.stat().st_size
        except OSError:
            pass
    return out


__all__ = [
    "BookSnapshot",
    "DEFAULT_ARCHIVE_PATH",
    "store_snapshot",
    "store_snapshots",
    "get_snapshot_at",
    "has_recent_snapshot",
    "purge_older_than",
    "clear",
    "stats",
]
