"""Tests for the consistent-SQLite-snapshot backup helper."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from pm.ops import backup_sqlite as bs


def _make_db(path: Path, rows: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    con.commit()
    con.close()


class TestFindDatabases:
    def test_missing_root_returns_empty(self, tmp_path):
        assert bs.find_databases(tmp_path / "nope") == []

    def test_finds_nested_databases(self, tmp_path):
        _make_db(tmp_path / "a.sqlite")
        _make_db(tmp_path / "sub" / "b.sqlite")
        names = {p.name for p in bs.find_databases(tmp_path)}
        assert names == {"a.sqlite", "b.sqlite"}

    def test_excludes_wal_and_shm_sidecars(self, tmp_path):
        _make_db(tmp_path / "a.sqlite")
        (tmp_path / "a.sqlite-wal").write_bytes(b"x")
        (tmp_path / "a.sqlite-shm").write_bytes(b"x")
        names = {p.name for p in bs.find_databases(tmp_path)}
        assert names == {"a.sqlite"}, "sidecars must never be copied on their own"


class TestShouldSkip:
    def test_small_database_is_kept(self):
        assert not bs.should_skip(10.0, 2048.0, "x.sqlite", set())

    def test_oversized_database_is_skipped(self):
        assert bs.should_skip(43000.0, 2048.0, "book_archive.sqlite", set())

    def test_force_include_overrides_the_cap(self):
        assert not bs.should_skip(43000.0, 2048.0, "big.sqlite", {"big.sqlite"})

    def test_zero_cap_disables_the_policy(self):
        assert not bs.should_skip(99999.0, 0.0, "big.sqlite", set())


class TestSnapshotOne:
    def test_produces_a_readable_copy(self, tmp_path):
        src = tmp_path / "src.sqlite"
        _make_db(src, rows=50)
        result = bs.snapshot_one(src, tmp_path / "out" / "src.sqlite")
        assert result.ok
        con = sqlite3.connect(result.staged)
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
        con.close()

    def test_snapshot_is_consistent_while_a_writer_is_active(self, tmp_path):
        """The actual defect: rsync copies a hot database and its WAL at
        different instants. The online backup API must produce a
        transactionally consistent copy even under concurrent writes."""
        src = tmp_path / "hot.sqlite"
        _make_db(src, rows=100)
        stop = threading.Event()

        def writer():
            con = sqlite3.connect(src, timeout=30)
            con.execute("PRAGMA journal_mode=WAL")
            i = 0
            while not stop.is_set():
                con.execute("INSERT INTO t (v) VALUES (?)", (f"live{i}",))
                con.commit()
                i += 1
                time.sleep(0.001)
            con.close()

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        time.sleep(0.05)
        try:
            result = bs.snapshot_one(src, tmp_path / "out" / "hot.sqlite")
        finally:
            stop.set()
            t.join(timeout=10)

        assert result.ok, f"snapshot failed: {result.detail}"
        con = sqlite3.connect(f"file:{result.staged}?mode=ro", uri=True)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] >= 100
        con.close()

    def test_reports_an_error_for_a_non_database(self, tmp_path):
        bogus = tmp_path / "not-a-db.sqlite"
        bogus.write_text("this is not a database")
        result = bs.snapshot_one(bogus, tmp_path / "out" / "x.sqlite")
        assert not result.ok
        assert result.status in ("error", "failed-integrity")

    def test_leaves_no_partial_file_after_a_failure(self, tmp_path):
        bogus = tmp_path / "bad.sqlite"
        bogus.write_text("garbage")
        bs.snapshot_one(bogus, tmp_path / "out" / "bad.sqlite")
        partials = list((tmp_path / "out").glob("*.partial"))
        assert partials == []

    def test_source_is_opened_read_only(self, tmp_path):
        """A backup must never be able to modify what it reads."""
        src = tmp_path / "src.sqlite"
        _make_db(src, rows=10)
        before = src.read_bytes()
        bs.snapshot_one(src, tmp_path / "out" / "src.sqlite")
        assert src.read_bytes() == before


class TestStageAll:
    def test_mirrors_the_relative_layout(self, tmp_path):
        data = tmp_path / "data"
        _make_db(data / "shared" / "one.sqlite")
        _make_db(data / "profile" / "two.sqlite")
        staging = tmp_path / "staging"
        results = bs.stage_all(data, staging)
        assert all(r.ok for r in results)
        assert (staging / "shared" / "one.sqlite").exists()
        assert (staging / "profile" / "two.sqlite").exists()

    def test_skips_oversized_and_reports_it(self, tmp_path):
        data = tmp_path / "data"
        _make_db(data / "small.sqlite")
        results = bs.stage_all(data, tmp_path / "staging", max_mb=0.000001)
        assert [r.status for r in results] == ["skipped-too-large"]
        text = bs.format_results(results, title="t")
        assert "SKIP" in text
        assert "NOT in this backup" in text, "a silent omission is worse than a refusal"

    def test_main_writes_skipped_names_for_the_status_file(self, tmp_path):
        data = tmp_path / "data"
        _make_db(data / "big.sqlite")
        _make_db(data / "cold.sqlite")
        _make_db(data / "small.sqlite")
        out = tmp_path / "skipped.json"
        rc = bs.main(["--data-root", str(data), "--staging", str(tmp_path / "staging"),
                      "--max-mb", "0.000001", "--force-include", "small.sqlite",
                      "--skip-expected", "cold.sqlite", "--skipped-file", str(out)])
        assert rc == 0
        assert json.loads(out.read_text()) == {
            "skipped": ["big.sqlite"], "skipped_expected": ["cold.sqlite"]}


class TestIncrementalStaging:
    """Reusing an unchanged staged file is what makes rsync --link-dest
    hardlink it. Re-snapshotting identical content mints a fresh inode and
    mtime, and rsync copies it again — measured as two full 1.8 GB snapshots
    instead of one plus diffs on the real tree."""

    def test_unchanged_database_is_reused_not_restaged(self, tmp_path):
        data = tmp_path / "data"
        _make_db(data / "a.sqlite")
        staging = tmp_path / "staging"
        bs.stage_all(data, staging)
        first_inode = (staging / "a.sqlite").stat().st_ino

        results = bs.stage_all(data, staging)
        assert results[0].detail == "unchanged — reused"
        assert (staging / "a.sqlite").stat().st_ino == first_inode

    def test_changed_database_is_restaged(self, tmp_path):
        data = tmp_path / "data"
        db = data / "a.sqlite"
        _make_db(db, rows=10)
        staging = tmp_path / "staging"
        bs.stage_all(data, staging)

        con = sqlite3.connect(db)
        con.execute("INSERT INTO t (v) VALUES ('new')")
        con.commit()
        con.close()

        results = bs.stage_all(data, staging)
        assert results[0].detail != "unchanged — reused"
        con = sqlite3.connect(f"file:{staging / 'a.sqlite'}?mode=ro", uri=True)
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 11
        con.close()

    def test_missing_staged_file_forces_a_restage(self, tmp_path):
        data = tmp_path / "data"
        _make_db(data / "a.sqlite")
        staging = tmp_path / "staging"
        bs.stage_all(data, staging)
        (staging / "a.sqlite").unlink()
        results = bs.stage_all(data, staging)
        assert results[0].ok
        assert (staging / "a.sqlite").exists()

    def test_corrupt_manifest_is_survivable(self, tmp_path):
        data = tmp_path / "data"
        _make_db(data / "a.sqlite")
        staging = tmp_path / "staging"
        staging.mkdir(parents=True)
        (staging / bs.MANIFEST_NAME).write_text("{ not json")
        results = bs.stage_all(data, staging)
        assert results[0].ok


class TestIsUnchanged:
    def test_no_record_means_changed(self, tmp_path):
        assert bs.is_unchanged(tmp_path / "a", tmp_path / "b", None) is False

    def test_matching_mtime_and_size_means_unchanged(self, tmp_path):
        src = tmp_path / "src"
        src.write_text("hello")
        staged = tmp_path / "staged"
        staged.write_text("x")
        st = src.stat()
        assert bs.is_unchanged(src, staged, [st.st_mtime_ns, st.st_size]) is True

    def test_size_change_is_detected(self, tmp_path):
        src = tmp_path / "src"
        src.write_text("hello")
        staged = tmp_path / "staged"
        staged.write_text("x")
        st = src.stat()
        assert bs.is_unchanged(src, staged, [st.st_mtime_ns, st.st_size + 1]) is False


class TestVerifySnapshot:
    def test_passes_on_a_good_snapshot(self, tmp_path):
        _make_db(tmp_path / "a.sqlite")
        results = bs.verify_snapshot(tmp_path)
        assert results and all(r.ok for r in results)

    def test_fails_on_a_corrupt_snapshot(self, tmp_path):
        (tmp_path / "broken.sqlite").write_text("not a database at all")
        results = bs.verify_snapshot(tmp_path)
        assert results and not any(r.ok for r in results)


class TestCli:
    def test_verify_exit_code_reflects_health(self, tmp_path, capsys):
        _make_db(tmp_path / "ok.sqlite")
        assert bs.main(["--verify", str(tmp_path)]) == 0
        (tmp_path / "bad.sqlite").write_text("garbage")
        assert bs.main(["--verify", str(tmp_path)]) == 1

    def test_verify_on_an_empty_directory_is_an_error(self, tmp_path, capsys):
        assert bs.main(["--verify", str(tmp_path)]) == 1

    def test_stage_exit_code_is_zero_when_only_skips(self, tmp_path):
        data = tmp_path / "data"
        _make_db(data / "a.sqlite")
        code = bs.main(["--data-root", str(data), "--staging",
                        str(tmp_path / "stage"), "--max-mb", "0.000001"])
        assert code == 0, "a size-policy skip is a decision, not a failure"
