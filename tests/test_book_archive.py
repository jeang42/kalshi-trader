"""Tests for the order-book archive."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from pm.archive import book_archive


@pytest.fixture
def archive_path(tmp_path, monkeypatch):
    """Redirect the archive to a per-test tempfile."""
    p = tmp_path / "test_book_archive.sqlite"
    monkeypatch.setenv("BOOK_ARCHIVE_PATH", str(p))
    return p


def _book(asks: list[tuple[float, float]] | None = None,
          bids: list[tuple[float, float]] | None = None) -> pd.DataFrame:
    """Helper: build a parse_order_book-shaped DataFrame."""
    rows = []
    for price, size in (asks or []):
        rows.append({"side": "ask", "price": price, "size": size,
                     "cumulative_size": 0.0})
    for price, size in (bids or []):
        rows.append({"side": "bid", "price": price, "size": size,
                     "cumulative_size": 0.0})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Serialization roundtrip
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_book_roundtrip_recomputes_cumulative_size(self, archive_path):
        original = _book(
            asks=[(0.40, 100.0), (0.42, 200.0)],
            bids=[(0.38, 50.0), (0.36, 75.0)],
        )
        book_archive.store_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            book=original, ts=int(time.time()),
        )
        snap = book_archive.get_snapshot_at(
            venue="polymarket", market_id="m1", token_id="t1",
            ts=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert snap is not None
        # Cumulative size is recomputed from price-sorted depth; check
        # that the best ask has its own size as cumulative, and the
        # second ask cumulates.
        asks = snap.book[snap.book["side"] == "ask"].sort_values("price")
        assert list(asks["cumulative_size"]) == [100.0, 300.0]
        bids = snap.book[snap.book["side"] == "bid"].sort_values("price", ascending=False)
        assert list(bids["cumulative_size"]) == [50.0, 125.0]

    def test_empty_book_roundtrip(self, archive_path):
        # Empty books are valid data — "no depth at this time" is itself
        # informative for backtesting illiquid periods.
        book_archive.store_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            book=pd.DataFrame(columns=["side", "price", "size"]),
            ts=int(time.time()),
        )
        snap = book_archive.get_snapshot_at(
            venue="polymarket", market_id="m1", token_id="t1",
            ts=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert snap is not None
        assert snap.book.empty


# ---------------------------------------------------------------------------
# get_snapshot_at — historical at-or-before lookup
# ---------------------------------------------------------------------------

class TestGetSnapshotAt:
    def test_returns_most_recent_at_or_before(self, archive_path):
        base = int(time.time()) - 10_000
        # Three snapshots over time
        for offset, price in [(0, 0.40), (100, 0.41), (200, 0.42)]:
            book_archive.store_snapshot(
                venue="polymarket", market_id="m1", token_id="t1",
                book=_book(asks=[(price, 100.0)]),
                ts=base + offset,
            )
        # Query at base+150 → at-or-before is the base+100 snapshot
        ts = datetime.fromtimestamp(base + 150, tz=UTC)
        snap = book_archive.get_snapshot_at(
            venue="polymarket", market_id="m1", token_id="t1", ts=ts,
        )
        assert snap is not None
        assert snap.snapshot_ts == base + 100
        # Verify it's the 0.41 book, not the 0.40 or 0.42 ones
        asks = snap.book[snap.book["side"] == "ask"]
        assert asks["price"].iloc[0] == pytest.approx(0.41)

    def test_before_all_snapshots_returns_none(self, archive_path):
        base = int(time.time())
        book_archive.store_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            book=_book(asks=[(0.40, 100.0)]), ts=base,
        )
        ts = datetime.fromtimestamp(base - 100, tz=UTC)
        assert book_archive.get_snapshot_at(
            venue="polymarket", market_id="m1", token_id="t1", ts=ts,
        ) is None

    def test_unknown_market_returns_none(self, archive_path):
        assert book_archive.get_snapshot_at(
            venue="polymarket", market_id="never-stored", token_id="t1",
            ts=datetime.now(UTC),
        ) is None

    def test_venue_isolation(self, archive_path):
        # Same market_id, different venues → separate buckets
        ts = int(time.time())
        book_archive.store_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            book=_book(asks=[(0.40, 100.0)]), ts=ts,
        )
        snap = book_archive.get_snapshot_at(
            venue="kalshi", market_id="m1", token_id="t1",
            ts=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert snap is None


# ---------------------------------------------------------------------------
# has_recent_snapshot — used by the poller's de-dup gate
# ---------------------------------------------------------------------------

class TestHasRecentSnapshot:
    def test_true_when_within_window(self, archive_path):
        book_archive.store_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            book=_book(asks=[(0.40, 100.0)]), ts=int(time.time()),
        )
        assert book_archive.has_recent_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            within_seconds=60,
        )

    def test_false_when_outside_window(self, archive_path):
        # Snapshot 2 hours ago
        book_archive.store_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            book=_book(asks=[(0.40, 100.0)]),
            ts=int(time.time()) - 7_200,
        )
        assert not book_archive.has_recent_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            within_seconds=60,
        )

    def test_false_when_unknown(self, archive_path):
        assert not book_archive.has_recent_snapshot(
            venue="polymarket", market_id="missing", token_id="t1",
            within_seconds=60,
        )


# ---------------------------------------------------------------------------
# purge / clear / stats
# ---------------------------------------------------------------------------

class TestMaintenanceOps:
    def test_purge_older_than_removes_only_old(self, archive_path):
        now = int(time.time())
        # 10d old
        book_archive.store_snapshot(
            venue="polymarket", market_id="m_old", token_id="t1",
            book=_book(asks=[(0.40, 100.0)]),
            ts=now - 10 * 86_400,
        )
        # 1d old
        book_archive.store_snapshot(
            venue="polymarket", market_id="m_new", token_id="t1",
            book=_book(asks=[(0.40, 100.0)]),
            ts=now - 86_400,
        )
        n = book_archive.purge_older_than(days=7)
        assert n == 1
        assert book_archive.stats()["entries"] == 1

    def test_clear_wipes_everything(self, archive_path):
        ts = int(time.time())
        for i in range(3):
            book_archive.store_snapshot(
                venue="polymarket", market_id=f"m{i}", token_id="t1",
                book=_book(asks=[(0.40, 100.0)]), ts=ts + i,
            )
        n = book_archive.clear()
        assert n == 3
        assert book_archive.stats()["entries"] == 0

    def test_stats_breakdown(self, archive_path):
        ts = int(time.time())
        # 2 polymarket, 1 kalshi, across 2 markets
        book_archive.store_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            book=_book(asks=[(0.40, 100.0)]), ts=ts,
        )
        book_archive.store_snapshot(
            venue="polymarket", market_id="m1", token_id="t2",
            book=_book(asks=[(0.50, 100.0)]), ts=ts + 1,
        )
        book_archive.store_snapshot(
            venue="kalshi", market_id="m2", token_id="t3",
            book=_book(asks=[(0.60, 100.0)]), ts=ts + 2,
        )
        s = book_archive.stats()
        assert s["entries"] == 3
        assert s["markets"] == 2   # m1, m2
        # by-venue tuples
        by_venue = dict(s["venues"])
        assert by_venue == {"polymarket": 2, "kalshi": 1}
        assert s["file_bytes"] > 0

    def test_stats_empty(self, archive_path):
        s = book_archive.stats()
        assert s["entries"] == 0
        assert s["markets"] == 0
        assert s["venues"] == []
        assert s["oldest_age_s"] is None


# ---------------------------------------------------------------------------
# Idempotent insert (primary key REPLACE)
# ---------------------------------------------------------------------------

class TestBatchInsert:
    """One transaction per batch — important for HDD perf."""

    def test_bulk_insert_writes_all_rows(self, archive_path):
        rows = [
            {"venue": "polymarket", "market_id": f"m{i}",
             "token_id": "t1", "book": _book(asks=[(0.40, 100.0)])}
            for i in range(5)
        ]
        n = book_archive.store_snapshots(rows)
        assert n == 5
        assert book_archive.stats()["entries"] == 5

    def test_empty_batch_returns_zero(self, archive_path):
        assert book_archive.store_snapshots([]) == 0

    def test_per_row_ts_honored(self, archive_path):
        # Custom timestamps should round-trip through the batch path
        base = int(time.time()) - 1000
        rows = [
            {"venue": "v", "market_id": "m1", "token_id": "t1",
             "book": _book(asks=[(0.40, 100.0)]), "ts": base},
            {"venue": "v", "market_id": "m1", "token_id": "t1",
             "book": _book(asks=[(0.45, 100.0)]), "ts": base + 100},
        ]
        book_archive.store_snapshots(rows)
        # at-or-before lookup at base+50 should pick the first row
        snap = book_archive.get_snapshot_at(
            venue="v", market_id="m1", token_id="t1",
            ts=datetime.fromtimestamp(base + 50, tz=UTC),
        )
        assert snap is not None
        assert snap.snapshot_ts == base

    def test_bad_rows_skipped_quietly_rest_succeed(self, archive_path):
        # Missing market_id key → that row is skipped; others land
        rows = [
            {"venue": "v", "market_id": "m1", "token_id": "t1",
             "book": _book(asks=[(0.40, 100.0)])},
            {"venue": "v", "token_id": "t1",   # MISSING market_id
             "book": _book(asks=[(0.40, 100.0)])},
            {"venue": "v", "market_id": "m3", "token_id": "t1",
             "book": _book(asks=[(0.40, 100.0)])},
        ]
        n = book_archive.store_snapshots(rows)
        assert n == 2   # m1 + m3 wrote; the malformed one was dropped


class TestWalMode:
    def test_wal_mode_persists_in_file(self, archive_path):
        # journal_mode IS persisted in the DB file across connections,
        # so a fresh outside connection should see WAL.
        book_archive.store_snapshot(
            venue="v", market_id="m1", token_id="t1",
            book=_book(asks=[(0.40, 100.0)]),
        )
        import sqlite3
        conn = sqlite3.connect(str(archive_path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode.lower() == "wal"

    def test_synchronous_normal_on_our_connections(self, archive_path):
        # synchronous is PER-CONNECTION in SQLite — it resets to FULL
        # on each new connection. We have to verify by querying through
        # our own _connect() so the PRAGMA has run.
        book_archive.store_snapshot(
            venue="v", market_id="m1", token_id="t1",
            book=_book(asks=[(0.40, 100.0)]),
        )
        conn = book_archive._connect()
        try:
            sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        finally:
            conn.close()
        # synchronous: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
        assert int(sync) == 1


class TestIdempotentInsert:
    def test_same_key_replaces(self, archive_path):
        ts = int(time.time())
        book_archive.store_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            book=_book(asks=[(0.40, 100.0)]), ts=ts,
        )
        # Second snapshot with the SAME (venue, market, token, ts) →
        # overwrites the first per the PRIMARY KEY REPLACE semantics.
        book_archive.store_snapshot(
            venue="polymarket", market_id="m1", token_id="t1",
            book=_book(asks=[(0.55, 100.0)]), ts=ts,
        )
        assert book_archive.stats()["entries"] == 1
        snap = book_archive.get_snapshot_at(
            venue="polymarket", market_id="m1", token_id="t1",
            ts=datetime.fromtimestamp(ts, tz=UTC) + timedelta(seconds=1),
        )
        asks = snap.book[snap.book["side"] == "ask"]
        assert asks["price"].iloc[0] == pytest.approx(0.55)
