"""Tests for the crash-safe JSON persistence layer.

These target the two defects the module exists to close — torn writes and
lost updates — rather than the happy path, because the happy path was never
broken.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time

import pytest

from pm.core import atomic_io


@pytest.fixture
def store(tmp_path):
    return tmp_path / "ledger.json"


class TestWriteJson:
    def test_roundtrip(self, store):
        atomic_io.write_json(store, {"a": 1})
        assert json.loads(store.read_text()) == {"a": 1}

    def test_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c.json"
        atomic_io.write_json(nested, [1, 2])
        assert json.loads(nested.read_text()) == [1, 2]

    def test_keeps_previous_contents_as_bak(self, store):
        atomic_io.write_json(store, {"gen": 1})
        atomic_io.write_json(store, {"gen": 2})
        assert json.loads(store.read_text()) == {"gen": 2}
        assert json.loads(atomic_io.backup_path(store).read_text()) == {"gen": 1}

    def test_unserializable_value_leaves_the_existing_file_intact(self, store):
        """Serialize-before-touching-disk is the whole point: a bad value must
        not be able to destroy good data."""
        atomic_io.write_json(store, {"good": True})
        with pytest.raises(TypeError):
            atomic_io.write_json(store, {"bad": object()})
        assert json.loads(store.read_text()) == {"good": True}

    def test_leaves_no_temp_files_behind(self, store):
        atomic_io.write_json(store, {"a": 1})
        leftovers = [p.name for p in store.parent.iterdir() if ".tmp." in p.name]
        assert leftovers == []

    def test_temp_file_is_removed_when_serialization_fails(self, store):
        with pytest.raises(TypeError):
            atomic_io.write_json(store, {object()})
        leftovers = [p.name for p in store.parent.iterdir() if ".tmp." in p.name]
        assert leftovers == []


class TestReadJson:
    def test_missing_file_returns_default(self, store):
        assert atomic_io.read_json(store, {"empty": True}) == {"empty": True}

    def test_recovers_from_bak_when_primary_is_truncated(self, store):
        atomic_io.write_json(store, {"gen": 1})
        atomic_io.write_json(store, {"gen": 2})
        # Simulate the exact historical failure: a truncated write.
        store.write_text('{"gen": 2, "transactions": [{"id"')
        assert atomic_io.read_json(store, None) == {"gen": 1}

    def test_quarantines_the_damaged_file(self, store):
        atomic_io.write_json(store, {"gen": 1})
        atomic_io.write_json(store, {"gen": 2})
        store.write_text("{ truncated")
        atomic_io.read_json(store, None)
        corrupt = list(store.parent.glob("ledger.json.corrupt-*"))
        assert len(corrupt) == 1, "the damaged file must be preserved for inspection"
        assert corrupt[0].read_text() == "{ truncated"

    def test_falls_back_to_default_when_both_copies_are_bad(self, store):
        store.write_text("{ truncated")
        atomic_io.backup_path(store).write_text("also bad {")
        assert atomic_io.read_json(store, {"fallback": True}) == {"fallback": True}

    def test_empty_file_is_treated_as_damaged_not_as_empty_data(self, store):
        """A zero-byte file is the classic post-crash artifact. It must not be
        read as 'no transactions' — that would silently erase a ledger."""
        atomic_io.write_json(store, {"transactions": [1, 2, 3]})
        atomic_io.write_json(store, {"transactions": [1, 2, 3, 4]})
        store.write_text("")
        assert atomic_io.read_json(store, None) == {"transactions": [1, 2, 3]}


class TestMutate:
    def test_writes_on_clean_exit(self, store):
        with atomic_io.mutate(store, {"items": []}) as data:
            data["items"].append("x")
        assert json.loads(store.read_text()) == {"items": ["x"]}

    def test_does_not_write_when_the_block_raises(self, store):
        atomic_io.write_json(store, {"items": ["original"]})
        with pytest.raises(RuntimeError):
            with atomic_io.mutate(store, {"items": []}) as data:
                data["items"].append("doomed")
                raise RuntimeError("boom")
        assert json.loads(store.read_text()) == {"items": ["original"]}

    def test_sees_writes_from_a_previous_mutate(self, store):
        with atomic_io.mutate(store, {"n": 0}) as d:
            d["n"] = 1
        with atomic_io.mutate(store, {"n": 0}) as d:
            d["n"] += 1
        assert json.loads(store.read_text())["n"] == 2


def _append_worker(path_str: str, tag: str, n: int) -> None:
    """Child process: append `n` items under the lock."""
    from pathlib import Path

    from pm.core import atomic_io as aio  # re-import in the child
    p = Path(path_str)
    for i in range(n):
        with aio.mutate(p, {"items": []}) as data:
            data["items"].append(f"{tag}-{i}")
            time.sleep(0.001)  # widen the read-modify-write window


class TestConcurrency:
    def test_concurrent_writers_do_not_lose_updates(self, store):
        """The lost-update defect, reproduced across real processes.

        Without the advisory lock this loses roughly half the appends: each
        process reads, the other writes, and the first overwrites. This is the
        interleaving that ``snapshot_runner`` (every profile, 4x/day) and an
        arm daemon (its own profile, every 15 min) can produce for real.
        """
        atomic_io.write_json(store, {"items": []})
        per_worker = 15
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=_append_worker, args=(str(store), tag, per_worker))
                 for tag in ("a", "b", "c")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
            assert p.exitcode == 0

        items = json.loads(store.read_text())["items"]
        assert len(items) == 3 * per_worker, "appends were lost to an interleaved write"
        assert len(set(items)) == 3 * per_worker, "duplicate entries appeared"

    def test_lock_timeout_proceeds_rather_than_deadlocking(self, store, caplog):
        """A stuck lock holder must not stop an arm from recording a fill it
        has already placed — losing an update is bad, failing to record a real
        position is worse. The timeout path warns loudly instead."""
        if atomic_io.fcntl is None:  # pragma: no cover
            pytest.skip("no fcntl on this platform")
        lock_file = store.with_name(store.name + ".lock")
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        atomic_io.fcntl.flock(fd, atomic_io.fcntl.LOCK_EX)
        try:
            started = time.monotonic()
            with caplog.at_level("WARNING"):
                with atomic_io.locked(store, timeout=0.2):
                    pass
            elapsed = time.monotonic() - started
            assert 0.15 <= elapsed < 5.0
            assert "UNLOCKED" in caplog.text
        finally:
            atomic_io.fcntl.flock(fd, atomic_io.fcntl.LOCK_UN)
            os.close(fd)
