"""Concurrency tests for the multi-writer ledgers (P3/R-27).

`tests/test_atomic_io.py` already proves the PRIMITIVE (three processes
hammering `mutate()` lose nothing). What was untested until R-27 is one level
up and one level down from that:

- **The real ledger APIs** — a mutation site could bypass `mutate()` (pair
  `read_json` with `write_json`, or open the file raw) and the primitive's
  test would stay green while the ledger silently lost updates. Hammering
  `portfolio.record_transaction` and `position_meta` across processes tests
  what production actually calls.
- **Crash while holding the lock** — the flock design's load-bearing property
  is that the KERNEL releases it when the holder dies. If the lock were a
  "does the lockfile exist" scheme instead, a SIGKILLed writer would deadlock
  every later writer until an operator noticed. SIGKILL is exactly what
  `systemctl kill`, the OOM killer, or a power cut produce.
- **Readers during writes** — `os.replace` atomicity means a plain
  `json.load` (not just our own `read_json`) must never observe a torn file.
  The UI and pandas read paths are plain readers.
- **The SQLite side** — `ensure_schema` claims to be race-safe across
  processes (an arm and the control plane open the same store), and
  concurrent writers rely on stdlib `sqlite3.connect`'s **default
  timeout=5.0s busy handler** — nothing in this repo sets busy_timeout
  explicitly, so that default is load-bearing and worth pinning: if a future
  `_con()` passes `timeout=0`, writer contention becomes an immediate
  `database is locked` crash and this file starts failing.

**This suite is mutation-tested**: with `atomic_io.locked` temporarily
neutered (the flock skipped), both ledger multi-writer tests fail
deterministically with lost updates — so a green run here is evidence the
lock works, not just that the test ran. (Done by hand on 2026-08-24; the
sqlite tests are independent of that lock — their serialization comes from
SQLite's own BEGIN IMMEDIATE and busy handler.)

All workers are separate PROCESSES via the spawn context — threads share the
process's file-description table, so a threaded test would not exercise flock
contention at all. Workers live at module top level (spawn re-imports this
module) and take every path via arguments or environment, never via pytest
fixtures — the child process has no fixtures.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import sqlite3
import time
from pathlib import Path

import pytest

from pm.core import atomic_io, sqlite_schema

# ─── workers (top-level: the spawn context re-imports this module) ─────────


def _portfolio_worker(portfolio_file: str, tag: str, n: int) -> None:
    os.environ["POLYMARKET_PORTFOLIO_FILE"] = portfolio_file
    from pm.ledger import portfolio
    for i in range(n):
        portfolio.record_transaction(
            "buy", f"Q {tag}-{i}", f"COND-{tag}-{i}", "Yes",
            shares=1.0, price=0.5)


def _position_meta_worker(meta_file: str, tag: str, n: int) -> None:
    os.environ["POLYMARKET_POSITION_META_FILE"] = meta_file
    from pm.ledger import position_meta
    for i in range(n):
        position_meta.record_entry(f"COND-{tag}-{i}", "Yes", entry_price=0.4)


def _lock_holder_worker(store: str, ready_file: str) -> None:
    """Enter mutate(), signal, then sleep far longer than the test — the
    parent SIGKILLs us mid-hold. The write must never happen."""
    p = Path(store)
    with atomic_io.mutate(p, {"items": []}) as data:
        data["items"].append("MUST-NEVER-BE-PUBLISHED")
        Path(ready_file).write_text("holding")
        time.sleep(120)


def _churn_writer_worker(store: str, tag: str, n: int) -> None:
    """Rewrite a payload big enough that a torn write would be observable."""
    p = Path(store)
    filler = tag * 2000
    for i in range(n):
        with atomic_io.mutate(p, {}) as data:
            data["writer"] = tag
            data["i"] = i
            data["filler"] = filler
            data["checksum"] = f"{tag}:{i}:{len(filler)}"


def _raw_reader_worker(store: str, n: int, q) -> None:
    """Plain json.load, deliberately NOT read_json: proves os.replace
    atomicity to readers that know nothing of this module."""
    p = Path(store)
    bad = 0
    for _ in range(n):
        try:
            with open(p) as f:
                data = json.load(f)
            tag, i, ln = data["checksum"].split(":")
            if data["writer"] != tag or data["i"] != int(i) or len(data["filler"]) != int(ln):
                bad += 1
        except Exception:
            bad += 1
    q.put(bad)


_TOY_SCHEMA_SQL_V1 = "CREATE TABLE items (k TEXT PRIMARY KEY, v INTEGER)"
_TOY_SCHEMA_SQL_V2 = "ALTER TABLE items ADD COLUMN w TEXT"


def _ensure_schema_worker(db: str, tag: str, q) -> None:
    try:
        migrations = [
            sqlite_schema.Migration(1, "create", [_TOY_SCHEMA_SQL_V1]),
            sqlite_schema.Migration(2, "widen", [_TOY_SCHEMA_SQL_V2]),
        ]
        con = sqlite3.connect(db)          # stdlib default timeout=5.0
        sqlite_schema.set_wal(con)
        sqlite_schema.ensure_schema(con, store="toy", migrations=migrations,
                                    db_path=db)
        con.execute("INSERT INTO items (k, v, w) VALUES (?, 1, 'x')", (tag,))
        con.commit()
        con.close()
        q.put((tag, "ok"))
    except Exception as e:  # noqa: BLE001 — report, don't hang the queue
        q.put((tag, f"{type(e).__name__}: {e}"))


def _sqlite_insert_worker(db: str, tag: str, n: int, q) -> None:
    try:
        con = sqlite3.connect(db)          # default timeout=5.0 is the point
        sqlite_schema.set_wal(con)
        for i in range(n):
            con.execute("INSERT INTO items (k, v) VALUES (?, ?)", (f"{tag}-{i}", i))
            con.commit()
        con.close()
        q.put((tag, "ok"))
    except Exception as e:  # noqa: BLE001
        q.put((tag, f"{type(e).__name__}: {e}"))


# ─── helpers ───────────────────────────────────────────────────────────────


def _run_all(procs, timeout=90):
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=timeout)
        assert p.exitcode == 0, f"worker exited {p.exitcode}"


@pytest.fixture()
def ctx():
    return mp.get_context("spawn")


# ─── the real ledger APIs under multi-process load ─────────────────────────


class TestLedgerMultiWriter:
    def test_portfolio_transactions_survive_concurrent_writers(self, tmp_path, ctx):
        """The production interleaving: an arm daemon, snapshot_runner and the
        UI all append transactions to one profile. Every append must land."""
        pf = str(tmp_path / "portfolio.json")
        per = 12
        _run_all([ctx.Process(target=_portfolio_worker, args=(pf, t, per))
                  for t in ("arm", "snap", "ui")])
        data = json.loads(Path(pf).read_text())
        txns = data["transactions"]
        assert len(txns) == 3 * per, "a transaction was lost to an interleaved write"
        assert len({t["condition_id"] for t in txns}) == 3 * per

    def test_position_meta_keys_survive_concurrent_writers(self, tmp_path, ctx):
        """position_meta backs the TP/SL/trail math — losing another writer's
        key would silently disarm an exit rule on a live position."""
        mf = str(tmp_path / "position_meta.json")
        per = 12
        _run_all([ctx.Process(target=_position_meta_worker, args=(mf, t, per))
                  for t in ("a", "b", "c")])
        data = json.loads(Path(mf).read_text())
        assert len(data["positions"]) == 3 * per, "a position's metadata was lost"


# ─── crash while holding the lock ──────────────────────────────────────────


class TestCrashWhileHoldingLock:
    def test_sigkilled_holder_neither_corrupts_nor_deadlocks(self, tmp_path, ctx):
        """SIGKILL a writer that is INSIDE mutate() holding the flock.

        Two properties, both load-bearing:
        1. The store still holds the pre-crash contents — mutate publishes
           only on clean exit, and the atomic-publish means there is no
           half-written state to find.
        2. A later writer proceeds immediately — flock dies with its holder
           (kernel-released). A lockfile-existence scheme would deadlock here
           until an operator deleted the stale lock; that failure mode is why
           the lock is flock and this test is why it stays flock.
        """
        store = tmp_path / "store.json"
        ready = tmp_path / "ready"
        atomic_io.write_json(store, {"items": ["pre-crash"]})

        holder = ctx.Process(target=_lock_holder_worker, args=(str(store), str(ready)))
        holder.start()
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert time.monotonic() < deadline, "holder never signalled"
            time.sleep(0.02)
        os.kill(holder.pid, signal.SIGKILL)
        holder.join(timeout=10)

        # 1: pre-crash contents intact, the holder's mutation never published
        assert json.loads(store.read_text()) == {"items": ["pre-crash"]}

        # 2: the next writer gets the lock promptly (well under the 10s
        # timeout that would otherwise mask a stale lock as a slow success)
        t0 = time.monotonic()
        with atomic_io.mutate(store, {}) as data:
            data["items"].append("post-crash")
        assert time.monotonic() - t0 < 2.0, "stale lock stalled the next writer"
        assert json.loads(store.read_text())["items"] == ["pre-crash", "post-crash"]


# ─── readers never see a torn file ─────────────────────────────────────────


class TestReadersNeverSeeTornState:
    def test_plain_json_load_always_sees_a_complete_document(self, tmp_path, ctx):
        """Two writers churn a ~8KB payload while a third process runs plain
        json.load in a loop. Every read must parse AND satisfy the payload's
        internal checksum — os.replace guarantees old-or-new, never a mix."""
        store = tmp_path / "store.json"
        _churn_writer_worker(str(store), "seed", 1)   # ensure it exists first

        q = mp.get_context("spawn").Queue()
        ctx2 = mp.get_context("spawn")
        readers = [ctx2.Process(target=_raw_reader_worker, args=(str(store), 300, q))]
        writers = [ctx2.Process(target=_churn_writer_worker, args=(str(store), t, 40))
                   for t in ("aa", "bb")]
        _run_all(readers + writers)
        assert q.get() == 0, "a reader observed a torn or inconsistent document"


# ─── the SQLite side ───────────────────────────────────────────────────────


class TestSqliteConcurrency:
    def test_ensure_schema_initialization_race(self, tmp_path, ctx):
        """Six processes race to migrate the SAME fresh store 0→2 — the
        arm-and-control-plane-open-together case sqlite_schema claims to
        handle (re-checks the version under the write lock). All must
        succeed, and the store must come out at v2 exactly once.

        This test EARNED ITS KEEP on 2026-08-24: with the raw
        `PRAGMA journal_mode=WAL` the workers originally used, it failed in
        roughly half of runs — the DELETE→WAL conversion on a fresh file
        takes an exclusive lock the sqlite3 busy handler does NOT cover
        (instrumented probe: 20/20 failures at the pragma, 0/30 in
        ensure_schema itself). The fix is `sqlite_schema.set_wal`, which
        every store's `_con()` now uses; the workers use it too, so this
        test guards the fix."""
        db = str(tmp_path / "toy.sqlite")
        q = ctx.Queue()
        _run_all([ctx.Process(target=_ensure_schema_worker, args=(db, t, q))
                  for t in "abcdef"])
        results = [q.get() for _ in range(6)]
        assert all(r[1] == "ok" for r in results), results
        con = sqlite3.connect(db)
        assert sqlite_schema.user_version(con) == 2
        assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 6
        cols = [r[1] for r in con.execute("PRAGMA table_info(items)")]
        assert cols == ["k", "v", "w"], "a racing migration mangled the schema"

    def test_concurrent_writers_rely_on_the_stdlib_busy_timeout(self, tmp_path, ctx):
        """Nothing in this repo sets busy_timeout; concurrent WAL writers are
        absorbed by sqlite3.connect's DEFAULT timeout=5.0 busy handler. That
        default is therefore load-bearing — pin it. If a future _con() passes
        timeout=0, this starts failing with 'database is locked' and the row
        count comes up short."""
        db = str(tmp_path / "toy.sqlite")
        con = sqlite3.connect(db)
        sqlite_schema.set_wal(con)
        con.execute(_TOY_SCHEMA_SQL_V1)
        con.commit()
        con.close()

        q = ctx.Queue()
        per = 40
        _run_all([ctx.Process(target=_sqlite_insert_worker, args=(db, t, per, q))
                  for t in "abcd"])
        results = [q.get() for _ in range(4)]
        assert all(r[1] == "ok" for r in results), results
        n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM items").fetchone()[0]
        assert n == 4 * per
