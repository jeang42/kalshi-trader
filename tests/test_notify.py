"""Tests for operator alerting.

An alerting path that can raise is worse than none — it would take down the
thing it is watching. And one that silently does nothing is worse still: you
believe you are covered. Both properties are asserted here.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pm.core import notify
from pm.ops import health_digest as hd


@pytest.fixture(autouse=True)
def health_log(tmp_path, monkeypatch):
    p = tmp_path / "health.jsonl"
    monkeypatch.setenv("HEALTH_LOG_FILE", str(p))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    return p


class TestRecord:
    def test_appends_a_row(self, health_log):
        assert notify.record("unit_failed", "svc died") is True
        row = json.loads(health_log.read_text().strip())
        assert row["event"] == "unit_failed" and row["detail"] == "svc died"

    def test_appends_rather_than_overwrites(self, health_log):
        notify.record("a", "1")
        notify.record("b", "2")
        assert len(health_log.read_text().strip().splitlines()) == 2

    def test_creates_the_parent_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTH_LOG_FILE", str(tmp_path / "deep" / "h.jsonl"))
        assert notify.record("e", "d") is True

    def test_returns_false_instead_of_raising_when_unwritable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTH_LOG_FILE", str(tmp_path))   # a directory
        assert notify.record("e", "d") is False


class TestRecentEvents:
    def test_empty_when_no_log_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTH_LOG_FILE", str(tmp_path / "none.jsonl"))
        assert notify.recent_events() == []

    def test_newest_first(self, health_log):
        notify.record("first", "1")
        notify.record("second", "2")
        assert notify.recent_events()[0]["event"] == "second"

    def test_a_torn_line_does_not_hide_the_rest(self, health_log):
        """A partially-written line is exactly what a crash leaves behind."""
        notify.record("good", "1")
        with open(health_log, "a") as f:
            f.write('{"ts": "2026-01-01", "eve\n')
        notify.record("also_good", "2")
        events = notify.recent_events()
        assert {e["event"] for e in events} == {"good", "also_good"}

    def test_since_hours_filters_old_rows(self, health_log):
        with open(health_log, "a") as f:
            f.write(json.dumps({"ts": "2020-01-01T00:00:00+00:00",
                                "event": "ancient", "detail": ""}) + "\n")
        notify.record("recent", "now")
        events = notify.recent_events(since_hours=1)
        assert [e["event"] for e in events] == ["recent"]


class TestNtfy:
    def test_no_topic_means_no_push_and_no_error(self, health_log):
        assert notify._ntfy("t", "b") is False

    def test_posts_to_the_configured_topic(self, monkeypatch, health_log):
        monkeypatch.setenv("NTFY_TOPIC", "my-topic")
        with patch("requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            assert notify._ntfy("Title", "Body") is True
        url = post.call_args[0][0]
        assert url.endswith("/my-topic")

    def test_a_dead_network_does_not_raise(self, monkeypatch, health_log):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        with patch("requests.post", side_effect=OSError("no route to host")):
            assert notify._ntfy("t", "b") is False

    def test_an_invalid_priority_falls_back(self, monkeypatch, health_log):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        monkeypatch.setenv("NTFY_PRIORITY", "extremely")
        with patch("requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            notify._ntfy("t", "b")
        assert post.call_args[1]["headers"]["Priority"] in notify._PRIORITIES

    def test_a_token_is_sent_when_configured(self, monkeypatch, health_log):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        monkeypatch.setenv("NTFY_TOKEN", "secret")
        with patch("requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            notify._ntfy("t", "b")
        assert post.call_args[1]["headers"]["Authorization"] == "Bearer secret"


class TestAlert:
    def test_reports_which_channels_worked(self, health_log):
        res = notify.alert("T", "B")
        assert res["health_log"] is True
        assert res["ntfy"] is False          # no topic configured

    def test_never_raises_even_when_every_channel_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTH_LOG_FILE", str(tmp_path))    # unwritable
        monkeypatch.setenv("NTFY_TOPIC", "t")
        with patch("requests.post", side_effect=OSError("down")):
            res = notify.alert("T", "B")     # must not raise
        assert res["health_log"] is False and res["ntfy"] is False

    def test_unit_failed_names_the_unit_and_the_command_to_run(self, health_log):
        notify.unit_failed("fade-nearclose.service")
        row = json.loads(health_log.read_text().strip())
        assert row["unit"] == "fade-nearclose.service"
        assert "systemctl" in row["detail"], "an alert should say what to do next"


class TestNotifyCli:
    def test_test_mode_exits_zero_when_the_log_works(self, health_log, capsys):
        assert notify.main(["--test"]) == 0
        assert "health log" in capsys.readouterr().out

    def test_unit_mode_records(self, health_log):
        assert notify.main(["--unit", "svc.service"]) == 0
        assert "svc.service" in health_log.read_text()

    def test_exits_nonzero_when_the_durable_channel_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTH_LOG_FILE", str(tmp_path))
        assert notify.main(["--event", "e", "--detail", "d"]) == 1


class TestHealthDigest:
    def test_quiet_when_nothing_is_wrong(self):
        d = hd.Digest()
        assert d.needs_attention is False
        assert "No problems" in hd.format_digest(d)

    def test_problems_are_listed(self):
        d = hd.Digest(problems=["arm down"], notes=["all else fine"])
        out = hd.format_digest(d)
        assert d.needs_attention is True
        assert "arm down" in out and "all else fine" in out

    def test_a_raising_check_becomes_a_problem_not_a_crash(self):
        """One broken check must not take down the digest — otherwise a
        monitoring bug silently ends monitoring."""
        with patch.object(hd, "check_units", side_effect=RuntimeError("boom")), \
             patch.object(hd, "check_freshness", return_value=([], [])), \
             patch.object(hd, "check_recent_events", return_value=([], [])), \
             patch.object(hd, "check_live_gates", return_value=([], [])):
            d = hd.build_digest()
        assert any("raised" in p and "boom" in p for p in d.problems)

    def test_stale_capture_store_is_reported(self, tmp_path):
        """The failure systemd cannot see: service green, database going cold."""
        import sqlite3
        db = tmp_path / "cap.sqlite"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE t (ts REAL)")
        con.execute("INSERT INTO t VALUES (0)")     # epoch 0 = very stale
        con.commit()
        con.close()
        problems, _ = hd.check_freshness((( str(db), "t", "ts", 6.0),))
        assert problems and "old" in problems[0]

    def test_fresh_capture_store_is_only_a_note(self, tmp_path):
        import sqlite3
        import time
        db = tmp_path / "cap.sqlite"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE t (ts REAL)")
        con.execute("INSERT INTO t VALUES (?)", (time.time(),))
        con.commit()
        con.close()
        problems, notes = hd.check_freshness(((str(db), "t", "ts", 6.0),))
        assert problems == [] and notes

    def test_missing_store_is_skipped_rather_than_reported(self, tmp_path):
        problems, notes = hd.check_freshness(((str(tmp_path / "nope.sqlite"),
                                               "t", "ts", 6.0),))
        assert problems == [] and notes == []

    def test_print_mode_sends_nothing(self, capsys):
        with patch.object(notify, "alert") as alert:
            hd.main(["--print"])
        assert not alert.called
