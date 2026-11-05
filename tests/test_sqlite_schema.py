"""sqlite_schema — versioned migrations for the arm/capture stores (R-19)."""

from __future__ import annotations

import os
import sqlite3

import pytest

from pm.core import sqlite_schema as ss
from pm.core.sqlite_schema import Migration, SchemaError, ensure_schema


def _mk(path):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    return c


V1 = Migration(1, "adopt", ["CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY, x REAL)"])


def _v2_rebuild(conn):
    ss.rebuild_table(conn, "t", """CREATE TABLE t__new (
        id TEXT PRIMARY KEY NOT NULL CHECK(id <> ''),
        x REAL CHECK(x IS NULL OR (x >= 0 AND x <= 1)),
        y INTEGER NOT NULL DEFAULT 0)""",
        post_sql=["CREATE INDEX IF NOT EXISTS ix_t_y ON t(y)"])


V2 = Migration(2, "constraints", _v2_rebuild, backup=True)


class TestFreshAndLegacy:
    def test_fresh_db_runs_every_migration_and_stamps(self, tmp_path):
        p = tmp_path / "s.sqlite"
        c = _mk(p)
        assert ensure_schema(c, store="s", migrations=[V1, V2], db_path=p) == 2
        assert ss.user_version(c) == 2
        assert ss.table_columns(c, "t") == ["id", "x", "y"]
        # constraint is live
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO t (id, x) VALUES ('a', 1.5)")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO t (id, x) VALUES ('', 0.5)")

    def test_legacy_db_at_v0_is_adopted_and_rebuilt_preserving_rows(self, tmp_path):
        p = tmp_path / "s.sqlite"
        raw = sqlite3.connect(p)
        raw.execute("CREATE TABLE t (id TEXT PRIMARY KEY, x REAL)")
        raw.executemany("INSERT INTO t VALUES (?, ?)", [("a", 0.1), ("b", 0.9)])
        raw.commit()
        raw.close()
        c = _mk(p)
        assert ss.user_version(c) == 0
        ensure_schema(c, store="s", migrations=[V1, V2], db_path=p)
        assert ss.user_version(c) == 2
        assert c.execute("SELECT id, x, y FROM t ORDER BY id").fetchall() == \
            [("a", 0.1, 0), ("b", 0.9, 0)]
        # backup was taken before the rebuild and holds the OLD shape
        bak = str(p) + ".pre-v2.bak"
        assert os.path.exists(bak)
        b = sqlite3.connect(bak)
        assert ss.table_columns(b, "t") == ["id", "x"]
        assert b.execute("SELECT count(*) FROM t").fetchone()[0] == 2

    def test_second_open_is_a_noop(self, tmp_path):
        p = tmp_path / "s.sqlite"
        ensure_schema(_mk(p), store="s", migrations=[V1, V2], db_path=p)
        c = _mk(p)
        assert ensure_schema(c, store="s", migrations=[V1, V2], db_path=p) == 2
        # no second backup, no re-run
        assert not os.path.exists(str(p) + ".pre-v2.bak.1")


class TestFailClosed:
    def test_newer_than_code_refuses(self, tmp_path):
        p = tmp_path / "s.sqlite"
        c = _mk(p)
        ensure_schema(c, store="s", migrations=[V1, V2], db_path=p)
        c.execute("PRAGMA user_version = 7")
        c.commit()
        with pytest.raises(SchemaError, match="newer code"):
            ensure_schema(c, store="s", migrations=[V1, V2], db_path=p)

    def test_failing_migration_rolls_back_and_leaves_version(self, tmp_path):
        p = tmp_path / "s.sqlite"
        c = _mk(p)

        def bad(conn):
            conn.execute("CREATE TABLE half (a)")   # partial work...
            raise RuntimeError("boom")               # ...then failure

        with pytest.raises(SchemaError, match="v2 .*boom"):
            ensure_schema(c, store="s", migrations=[V1, Migration(2, "bad", bad)], db_path=p)
        assert ss.user_version(c) == 1
        assert "half" not in {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def test_bad_numbering_rejected(self, tmp_path):
        with pytest.raises(SchemaError, match="numbered"):
            ensure_schema(_mk(tmp_path / "s.sqlite"), store="s",
                          migrations=[V1, Migration(3, "skip", "SELECT 1")])

    def test_rebuild_above_size_limit_refuses(self, tmp_path, monkeypatch):
        p = tmp_path / "s.sqlite"
        c = _mk(p)
        ensure_schema(c, store="s", migrations=[V1], db_path=p)
        monkeypatch.setattr(ss, "BACKUP_MAX_BYTES", 0)
        with pytest.raises(SchemaError, match="above the"):
            ensure_schema(c, store="s", migrations=[V1, V2], db_path=p)
        assert ss.user_version(c) == 1

    def test_memory_db_rebuild_warns_but_proceeds(self, caplog):
        c = sqlite3.connect(":memory:")
        with caplog.at_level("WARNING"):
            ensure_schema(c, store="s", migrations=[V1, V2], db_path=":memory:")
        assert ss.user_version(c) == 2
        assert any("without a file backup" in r.message for r in caplog.records)


class TestHelpers:
    def test_add_column_if_missing_is_idempotent(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t (a)")
        assert ss.add_column_if_missing(c, "t", "b", "REAL") is True
        assert ss.add_column_if_missing(c, "t", "b", "REAL") is False
        assert ss.table_columns(c, "t") == ["a", "b"]

    def test_table_columns_missing_table(self):
        assert ss.table_columns(sqlite3.connect(":memory:"), "nope") == []

    def test_rebuild_requires_tmp_name(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t (a)")
        with pytest.raises(SchemaError, match="did not create"):
            ss.rebuild_table(c, "t", "CREATE TABLE wrong (a)")

    def test_string_apply_and_sequence_apply(self):
        c = sqlite3.connect(":memory:")
        ensure_schema(c, store="s", migrations=[
            Migration(1, "one", "CREATE TABLE a (x)"),
            Migration(2, "two", ["CREATE TABLE b (x)", "INSERT INTO b VALUES (1)"]),
        ])
        assert c.execute("SELECT x FROM b").fetchone() == (1,)
        assert ss.user_version(c) == 2
