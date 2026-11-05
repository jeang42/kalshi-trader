"""Tests for the health digest's backup check (pm/ops/health_digest.py).

The contract: a backup that FAILED, went stale, or never recorded an outcome
is a problem the digest pushes about — because the snapshot directory keeps
appearing nightly even when the sqlite stage inside it fails, which is how
four consecutive failed runs (2026-08-25..28) went unnoticed.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from pm.ops import health_digest as hd


def _write(tmp_path, *, status="ok", hours_ago=2.0, stamp="2026-08-29T05-30-01Z",
           **extra):
    p = tmp_path / "backup_status.json"
    finished = datetime.now(UTC) - timedelta(hours=hours_ago)
    p.write_text(json.dumps({
        "stamp": stamp, "status": status, "elapsed_s": 60,
        "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"), **extra}))
    return str(p)


class TestCheckBackup:
    def test_fresh_ok_run_is_a_note_not_a_problem(self, tmp_path):
        problems, notes = hd.check_backup(_write(tmp_path))
        assert problems == []
        assert notes and "2026-08-29T05-30-01Z" in notes[0]

    def test_failed_run_is_a_problem(self, tmp_path):
        problems, _ = hd.check_backup(_write(tmp_path, status="FAILED"))
        assert len(problems) == 1 and "FAILED" in problems[0]

    def test_stale_ok_run_is_a_problem(self, tmp_path):
        problems, _ = hd.check_backup(_write(tmp_path, hours_ago=30.0))
        assert len(problems) == 1 and "old" in problems[0]

    def test_missing_file_is_a_problem(self, tmp_path):
        problems, _ = hd.check_backup(str(tmp_path / "nope.json"))
        assert len(problems) == 1 and "missing" in problems[0]

    def test_garbage_file_is_a_problem_not_a_crash(self, tmp_path):
        p = tmp_path / "backup_status.json"
        p.write_text("{not json")
        problems, _ = hd.check_backup(str(p))
        assert len(problems) == 1 and "unreadable" in problems[0]

    def test_skipped_database_on_ok_run_is_a_problem(self, tmp_path):
        # The 2026-10 failure: status "ok" every night while the candle panel
        # was over the cap and silently left out.
        problems, _ = hd.check_backup(_write(tmp_path, skipped=["candle_panel.sqlite"]))
        assert len(problems) == 1 and "candle_panel.sqlite" in problems[0]

    def test_empty_skipped_list_is_fine(self, tmp_path):
        problems, notes = hd.check_backup(_write(tmp_path, skipped=[]))
        assert problems == [] and notes

    def test_expected_skip_is_a_note_not_a_problem(self, tmp_path):
        problems, notes = hd.check_backup(_write(
            tmp_path, skipped=[], skipped_expected=["book_archive.sqlite"]))
        assert problems == []
        assert any("book_archive.sqlite" in n for n in notes)
