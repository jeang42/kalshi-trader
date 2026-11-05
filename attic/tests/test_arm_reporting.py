"""Reporting and cycle paths for the paper arms.

``stats()`` and ``summary()`` are how the operator decides whether an arm is
working. They were almost entirely uncovered, which means a crash or a wrong
aggregate in the numbers driving those decisions would have surfaced only when
someone happened to run the command. These populate a real store and exercise
the reporting end to end.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from pm.arms import back_nearclose as bn
from pm.arms import fade_nearclose as fn


def _rows(con, table_sql_cols, rows):
    cols = ",".join(table_sql_cols)
    ph = ",".join("?" * len(table_sql_cols))
    con.executemany(f"INSERT INTO positions ({cols}) VALUES ({ph})", rows)
    con.commit()


# ─── fade_nearclose ───────────────────────────────────────────────────────


@pytest.fixture
def fn_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FADE_NEARCLOSE_DB", str(tmp_path / "fn.sqlite"))
    con = fn._con()
    _rows(con,
          ["ticker", "event_id", "category", "question", "yes_bid", "yes_mid",
           "no_entry_cost", "count", "opened_at", "close_time", "resolved",
           "resolved_yes", "pnl", "closed_at", "spread", "kind", "stake"],
          # closed_at is set on every resolved row — the v2 schema (R-19)
          # enforces the invariant the live store has always satisfied.
          [("KXBTCD-1", "E1", "Crypto", "Q1", 0.15, 0.15, 0.85, 11,
            fn._now() - 9000, "2026-01-01T00:00:00Z", 1, 0, 1.35, fn._now() - 100,
            0.01, "crypto", 10.0),
           ("KXETHD-1", "E2", "Crypto", "Q2", 0.18, 0.18, 0.82, 12,
            fn._now() - 8000, "2026-01-01T00:00:00Z", 1, 1, -9.84, fn._now() - 100,
            0.03, "crypto", 10.0),
           ("KXOTHER-1", "E3", "Other", "Q3", 0.12, 0.12, 0.88, 11,
            fn._now() - 7000, "2026-01-01T00:00:00Z", 0, None, None, None,
            0.02, "discrete", 10.0)])
    yield con
    con.close()


class TestFadeNearcloseReporting:
    def test_summary_counts_open_and_resolved(self, fn_db):
        s = fn.summary()
        assert s["resolved"] == 2
        assert s["open"] == 1

    def test_summary_nets_pnl_over_resolved_only(self, fn_db):
        """An unresolved position has no P&L; including it as zero would
        quietly dilute the mean."""
        assert fn.summary()["net"] == pytest.approx(1.35 - 9.84, abs=1e-6)

    def test_stats_runs_end_to_end(self, fn_db, capsys):
        fn.stats()
        out = capsys.readouterr().out
        assert "fade_nearclose" in out.lower() or "resolved" in out.lower()

    def test_stats_on_an_empty_store_does_not_crash(self, tmp_path, monkeypatch, capsys):
        """The state on day one of a new arm, and after a reset."""
        monkeypatch.setenv("FADE_NEARCLOSE_DB", str(tmp_path / "empty.sqlite"))
        fn._con().close()
        fn.stats()
        assert capsys.readouterr().out != ""

    def test_summary_on_an_empty_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FADE_NEARCLOSE_DB", str(tmp_path / "empty2.sqlite"))
        fn._con().close()
        s = fn.summary()
        assert s["resolved"] == 0 and s["open"] == 0


# ─── back_nearclose ───────────────────────────────────────────────────────


@pytest.fixture
def bn_db2(tmp_path, monkeypatch):
    monkeypatch.setenv("BACK_NEARCLOSE_DB", str(tmp_path / "bn.sqlite"))
    con = bn._con()
    _rows(con,
          ["ticker", "event_id", "category", "question", "yes_ask", "entry_cost",
           "count", "opened_at", "close_time", "resolved", "resolved_yes", "pnl",
           "closed_at"],
          [("KXBTCD-1", "E1", "Crypto", "Q1", 0.94, 0.94, 10,
            bn._now() - 9000, "2026-01-01T00:00:00Z", 1, 1, 0.53, bn._now() - 100),
           ("KXETHD-1", "E2", "Crypto", "Q2", 0.95, 0.95, 10,
            bn._now() - 8000, "2026-01-01T00:00:00Z", 1, 0, -9.50, bn._now() - 100),
           ("KXSOLD-1", "E3", "Crypto", "Q3", 0.93, 0.93, 10,
            bn._now() - 7000, "2026-01-01T00:00:00Z", 0, None, None, None)])
    yield con
    con.close()


class TestBackNearcloseReporting:
    def test_summary_shape(self, bn_db2):
        s = bn.summary()
        assert s["resolved"] == 2 and s["open"] == 1

    def test_one_loss_dominates_many_small_wins(self, bn_db2):
        """The whole risk shape of this arm in one assertion: 97% win at ~+1c
        against 3% losing the full ~94c entry. Net must reflect that."""
        assert bn.summary()["net"] < 0

    def test_stats_runs_end_to_end(self, bn_db2, capsys):
        bn.stats()
        assert capsys.readouterr().out.strip() != ""

    def test_stats_on_an_empty_store(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BACK_NEARCLOSE_DB", str(tmp_path / "e.sqlite"))
        bn._con().close()
        bn.stats()
        assert capsys.readouterr().out != ""
