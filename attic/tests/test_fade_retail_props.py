"""Retired arm tests — `fade_props` and `fade_retail_longshot`.

Both arms were retired to `attic/` on 2026-08-23 after failing out-of-sample
(prop fade −$167 over 224 resolutions, retail fade −$339 over 200; see
`docs/failed-experiments.md`). These tests moved with them, extracted from
`tests/test_arm_cycles.py`, `tests/test_arm_reporting.py`,
`tests/test_shutdown.py`, `tests/test_store_schemas.py` and
`tests/test_provenance.py`, where they had been interleaved with the surviving
near-close arms' tests.

NOT collected by CI (`norecursedirs` excludes `attic`). Run by hand from the
repo root, which is what gives them the root `conftest.py`'s isolation — the
live `fade_props.sqlite` / `fade_retail.sqlite` stores are RETAINED as
evidence, so running these without that isolation would write into them:

    PYTHONPATH=.:attic .venv/bin/pytest --no-cov -p no:cacheprovider \
        attic/tests/test_fade_retail_props.py
"""

from __future__ import annotations

import os
import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

import fade_props as fp
import fade_retail_longshot as fr
import provenance as pv
import shutdown
import sqlite_schema as ss


def _rows(con, table_sql_cols, rows):
    con.executemany(
        f"INSERT INTO positions ({','.join(table_sql_cols)}) "
        f"VALUES ({','.join('?' * len(table_sql_cols))})", rows)
    con.commit()


# ═══ from tests/test_arm_cycles.py ════════════════════════════════════════


# ─── fade_props ───────────────────────────────────────────────────────────


@pytest.fixture
def fp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FADE_PROPS_DB", str(tmp_path / "props.sqlite"))
    con = fp._con()
    yield con
    con.close()


def _fp_market(ticker="KXPGA-1-AAA", *, yes=0.15, volume=500, event="KXPGA-1"):
    return {"id": ticker, "condition_id": ticker, "event_id": event,
            "question": "Q?", "best_bid": yes - 0.01, "best_ask": yes + 0.01,
            "mid": yes, "volume": volume, "days_to_resolution": 14.0,
            "series": "KXPGAR1LEAD", "close_time": "2026-09-01T05:00:00Z"}


class TestFadeProps:
    def test_books_an_in_band_prop(self, fp_db):
        assert fp._open(fp_db, pd.DataFrame([_fp_market()])) == 1

    def test_rejects_out_of_band(self, fp_db):
        assert fp._open(fp_db, pd.DataFrame([_fp_market(yes=0.60)])) == 0

    def test_volume_is_filtered_upstream_not_in_open(self, fp_db):
        """Documents the division of responsibility rather than asserting a
        filter that is not there: _open books whatever it is handed, and
        run_cycle applies the volume floor during discovery. Worth pinning —
        a future caller reaching for _open directly would bypass it."""
        assert fp._open(fp_db, pd.DataFrame([_fp_market(volume=1)])) == 1
        import inspect
        assert "MIN_VOLUME" not in inspect.getsource(fp._open)
        assert "MIN_VOLUME" in inspect.getsource(fp.run_cycle)

    def test_caps_positions_per_event(self, fp_db):
        """Prop fields run ~150 correlated members; without the cap one
        tournament would look like 150 independent observations."""
        df = pd.DataFrame([_fp_market(ticker=f"KXPGA-1-{i}", event="KXPGA-1")
                           for i in range(10)])
        assert fp._open(fp_db, df) == fp.PER_EVENT_CAP

    def test_frame_with_no_matching_rows_is_a_no_op(self, fp_db):
        assert fp._open(fp_db, pd.DataFrame(
            columns=list(_fp_market().keys()))) == 0

    def test_summary_reads_on_an_empty_database(self, fp_db):
        assert isinstance(fp.summary(), dict)


# ─── fade_retail_longshot ─────────────────────────────────────────────────


class TestFadeRetailGates:
    def test_canary_is_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("FADE_CANARY_ENABLED", raising=False)
        assert fr._canary_enabled() is False

    def test_canary_needs_an_explicit_truthy_value(self, monkeypatch):
        for val, want in (("true", True), ("1", True), ("false", False), ("", False)):
            monkeypatch.setenv("FADE_CANARY_ENABLED", val)
            assert fr._canary_enabled() is want, val

    def test_scheduled_macro_is_not_retail(self):
        """CPI/Fed prints are efficient, not the behavioural corner."""
        assert fr.is_retail_series("KXCPIYOY") is False
        assert fr.is_retail_series("KXFEDDECISION") is False

    def test_sport_and_crypto_are_not_retail(self):
        assert fr.is_retail_series("KXNBAGAME") is False
        assert fr.is_retail_series("KXWCROUND") is False, "World Cup is sport"
        assert fr.is_retail_series("KXBTCD") is False

    def test_award_and_election_series_are_retail(self):
        assert fr.is_retail_series("KXOSCARBEST") is True
        assert fr.is_retail_series("KXCAPRIMARY") is True

    def test_band_edges(self):
        assert fr.in_fade_band(fr.FADE_LO) is True
        assert fr.in_fade_band(fr.FADE_HI) is True
        assert fr.in_fade_band(fr.FADE_LO - 0.01) is False
        assert fr.in_fade_band(fr.FADE_HI + 0.01) is False

    def test_market_type_classification(self):
        assert fr.classify_mtype("Will X say 'banana'?") == "say_mention"
        assert fr.classify_mtype("Who will win the Oscar for Best Picture?") == "win_award"

    def test_field_overround_sums_plausible_members(self):
        df = pd.DataFrame([
            {"event_id": "E1", "mid": 0.30},
            {"event_id": "E1", "mid": 0.40},
            {"event_id": "E1", "mid": 0.001},  # below the plausible floor
        ])
        total, n = fr._field_overround(df)["E1"]
        assert total == pytest.approx(0.70), "implausible members are excluded"
        assert n == 2


# ═══ from tests/test_arm_reporting.py ═════════════════════════════════════


# ─── fade_props ───────────────────────────────────────────────────────────


@pytest.fixture
def fp_db2(tmp_path, monkeypatch):
    monkeypatch.setenv("FADE_PROPS_DB", str(tmp_path / "fp.sqlite"))
    con = fp._con()
    _rows(con,
          ["ticker", "event_id", "series", "question", "yes_bid",
           "no_entry_cost", "count", "opened_at", "close_time", "resolved",
           "resolved_yes", "pnl", "closed_at"],
          [("KXPGA-1-A", "KXPGA-1", "KXPGAR1LEAD", "Q1", 0.15, 0.85, 11,
            fp._now() - 9000, "2026-01-01T00:00:00Z", 1, 0, 1.35, fp._now() - 100),
           ("KXPGA-1-B", "KXPGA-1", "KXPGAR1LEAD", "Q2", 0.20, 0.80, 12,
            fp._now() - 8000, "2026-01-01T00:00:00Z", 1, 1, -9.60, fp._now() - 100),
           ("KXPGA-2-A", "KXPGA-2", "KXPGAR3LEAD", "Q3", 0.12, 0.88, 11,
            fp._now() - 7000, "2026-01-01T00:00:00Z", 0, None, None, None)])
    yield con
    con.close()


class TestFadePropsReporting:
    def test_summary_shape(self, fp_db2):
        s = fp.summary()
        assert s["resolved"] == 2 and s["open"] == 1

    def test_stats_runs_end_to_end(self, fp_db2, capsys):
        fp.stats()
        assert capsys.readouterr().out.strip() != ""

    def test_resolve_books_a_no_win(self, fp_db2):
        with patch.object(fp.kc, "_get",
                          return_value={"market": {"status": "finalized", "result": "no"}}), \
             patch.object(fp.time, "sleep"):
            fp._resolve(fp_db2)
        pnl = fp_db2.execute(
            "SELECT pnl FROM positions WHERE ticker='KXPGA-2-A'").fetchone()[0]
        assert pnl is not None and pnl > 0

    def test_resolve_tolerates_a_dead_venue(self, fp_db2):
        with patch.object(fp.kc, "_get", side_effect=RuntimeError("down")), \
             patch.object(fp.time, "sleep"):
            fp._resolve(fp_db2)
        assert fp_db2.execute(
            "SELECT resolved FROM positions WHERE ticker='KXPGA-2-A'").fetchone()[0] == 0

    def test_run_cycle_survives_a_dead_feed(self, fp_db2):
        with patch.object(fp, "_discover_props", side_effect=RuntimeError("down")), \
             patch.object(fp, "_resolve", return_value=0) as resolve:
            fp.run_cycle(verbose=False)
        assert resolve.called


# ─── fade_retail_longshot ─────────────────────────────────────────────────


@pytest.fixture
def fr_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FADE_RETAIL_DB", str(tmp_path / "fr.sqlite"))
    con = fr._con()
    yield con
    con.close()


class TestFadeRetailReporting:
    def test_summary_on_an_empty_store(self, fr_db):
        assert isinstance(fr.summary(), dict)

    def test_stats_on_an_empty_store(self, fr_db, capsys):
        fr.stats()
        assert capsys.readouterr().out != ""

    def test_canary_deployed_is_zero_without_live_rows(self, fr_db):
        assert fr._canary_deployed(fr_db) == 0.0

    def test_canary_size_scales_with_the_stake(self):
        n, flag = fr._canary_size(0.85, 1.0)
        assert isinstance(n, int) and n >= 0

    def test_run_cycle_survives_a_dead_feed(self, fr_db, monkeypatch):
        monkeypatch.delenv("FADE_CANARY_ENABLED", raising=False)
        with patch.object(fr.kc, "get_markets_closing_within",
                          side_effect=RuntimeError("down")), \
             patch.object(fr, "_manage_positions", return_value=(0, 0)) as manage:
            fr.run_cycle(verbose=False)
        assert manage.called, "open positions must still be managed"


def _fr_market(ticker="KXOSCARBEST-26-A", *, mid=0.20, event="KXOSCARBEST-26",
               volume=800, days=6.0):
    return {"id": ticker, "condition_id": ticker, "event_id": event,
            "question": "Will X win Best Picture?", "series": "KXOSCARBEST",
            "best_bid": mid - 0.01, "best_ask": mid + 0.01, "mid": mid,
            "volume": volume, "days_to_resolution": days,
            "close_time": "2026-09-01T05:00:00Z"}


class TestFadeRetailOpen:
    def test_books_an_in_band_retail_longshot(self, fr_db, monkeypatch):
        monkeypatch.delenv("FADE_CANARY_ENABLED", raising=False)
        df = pd.DataFrame([_fr_market()])
        assert fr._open_positions(fr_db, df, fr._field_overround(df)) == 1

    def test_rejects_out_of_band(self, fr_db):
        df = pd.DataFrame([_fr_market(mid=0.80)])
        assert fr._open_positions(fr_db, df, {}) == 0

    def test_skips_a_ticker_already_held(self, fr_db, monkeypatch):
        monkeypatch.delenv("FADE_CANARY_ENABLED", raising=False)
        df = pd.DataFrame([_fr_market()])
        fr._open_positions(fr_db, df, {})
        assert fr._open_positions(fr_db, df, {}) == 0

    def test_caps_positions_per_event_on_a_weak_field(self, fr_db, monkeypatch):
        """Members of one field are correlated; the cap is what keeps a single
        awards night from looking like N independent observations."""
        monkeypatch.delenv("FADE_CANARY_ENABLED", raising=False)
        df = pd.DataFrame([_fr_market(ticker=f"KXOSCARBEST-26-{i}") for i in range(8)])
        opened = fr._open_positions(fr_db, df, {})     # no overround -> weak field
        assert 0 < opened <= fr.PER_EVENT_CAP

    def test_records_the_no_side_cost(self, fr_db, monkeypatch):
        monkeypatch.delenv("FADE_CANARY_ENABLED", raising=False)
        df = pd.DataFrame([_fr_market(mid=0.20)])
        fr._open_positions(fr_db, df, {})
        cost = fr_db.execute("SELECT no_entry_cost FROM positions").fetchone()[0]
        assert 0.0 < cost < 1.0

    def test_places_no_real_order_when_the_canary_is_off(self, fr_db, monkeypatch):
        monkeypatch.setenv("FADE_CANARY_ENABLED", "false")
        df = pd.DataFrame([_fr_market()])
        fr._open_positions(fr_db, df, {})
        live = fr_db.execute(
            "SELECT COUNT(*) FROM positions WHERE live_order_id IS NOT NULL").fetchone()[0]
        assert live == 0


class TestFadeRetailManage:
    def _insert(self, con, ticker="KXOSCARBEST-26-A"):
        con.execute(
            "INSERT INTO positions (ticker,event_id,question,yes_bid,no_entry_cost,"
            "count,opened_at,close_time,resolved) VALUES (?,?,?,?,?,?,?,?,0)",
            (ticker, "EV", "Q?", 0.20, 0.80, 12, fr._now() - 9000,
             "2000-01-01T00:00:00Z"))
        con.commit()

    def test_no_wins_when_the_thing_did_not_happen(self, fr_db):
        self._insert(fr_db)
        with patch.object(fr.kc, "_get",
                          return_value={"market": {"status": "finalized", "result": "no"}}), \
             patch.object(fr.time, "sleep"):
            resolved, _ = fr._manage_positions(fr_db)
        assert resolved == 1
        assert fr_db.execute("SELECT pnl FROM positions").fetchone()[0] > 0

    def test_no_loses_when_it_happened(self, fr_db):
        self._insert(fr_db)
        with patch.object(fr.kc, "_get",
                          return_value={"market": {"status": "finalized", "result": "yes"}}), \
             patch.object(fr.time, "sleep"):
            fr._manage_positions(fr_db)
        assert fr_db.execute("SELECT pnl FROM positions").fetchone()[0] < 0

    def test_scalar_results_are_not_scoreable(self, fr_db):
        """A non-binary result must not be coerced into a win or a loss."""
        self._insert(fr_db)
        with patch.object(fr.kc, "_get",
                          return_value={"market": {"status": "finalized",
                                                    "result": "scalar"}}), \
             patch.object(fr.time, "sleep"):
            fr._manage_positions(fr_db)
        assert fr_db.execute("SELECT resolved FROM positions").fetchone()[0] == 0

    def test_a_dead_venue_leaves_positions_open(self, fr_db):
        self._insert(fr_db)
        with patch.object(fr.kc, "_get", side_effect=RuntimeError("down")), \
             patch.object(fr.time, "sleep"):
            fr._manage_positions(fr_db)
        assert fr_db.execute("SELECT resolved FROM positions").fetchone()[0] == 0


# ═══ from tests/test_shutdown.py, test_store_schemas.py ═══════════════════
#
# Kept as fragments: these were parametrize entries and table rows in files
# that still cover the LIVE arms, so they are reproduced here as data rather
# than as runnable tests. Re-inline them if an arm is ever un-retired.
#

# --- parametrize -= fade_props ---
#         ("fade_props", "FADE_PROPS_DB",
#          "INSERT INTO positions (ticker,event_id,no_entry_cost,count,opened_at,close_time) "
#          "VALUES ('KXT-1','E',0.85,11,1,'2020-01-01T00:00:00Z')"),

# --- test_fade_retail_manage_loop_breaks ---
#
#     def test_fade_retail_manage_loop_breaks(self, tmp_path, monkeypatch):
#         from unittest.mock import patch
#
#         import fade_retail_longshot as fr
#         monkeypatch.setenv("FADE_RETAIL_DB", str(tmp_path / "fr.sqlite"))
#         con = fr._con()
#         con.execute("INSERT INTO positions (ticker,event_id,no_entry_cost,count,opened_at,"
#                     "close_time) VALUES ('KXT-1','E',0.8,12,1,'2020-01-01T00:00:00Z')")
#         con.commit()
#         shutdown._handler(signal.SIGTERM, None)
#         with patch.object(fr.kc, "lookup_markets_by_id",
#                           side_effect=AssertionError("venue must not be called")):
#             assert fr._manage_positions(con) == (0, 0)

# --- STORES -= fade_props, fade_retail_longshot ---
#     ("fade_props", "FADE_PROPS_DB", "_con", 3),
#     ("fade_retail_longshot", "FADE_RETAIL_DB", "_con", 3),

# --- LEGACY -= fade_props, fade_retail_longshot ---
#     "fade_props": (
#         "FADE_PROPS_DB",
#         """CREATE TABLE positions (
#             ticker TEXT PRIMARY KEY, event_id TEXT, series TEXT, question TEXT,
#             yes_bid REAL, no_entry_cost REAL, count INTEGER,
#             opened_at INTEGER, close_time TEXT,
#             resolved INTEGER DEFAULT 0, resolved_yes INTEGER, pnl REAL, closed_at INTEGER)""",
#         [("A", "E1", "S", "q", 0.15, 0.85, 11, 100, "t", 1, 0, 1.35, 200),
#          ("B", "E2", "S", "q", 0.20, 0.80, 12, 100, "t", 0, None, None, None)],
#         # resolved without an outcome/P&L violates the invariant
#         "INSERT INTO positions (ticker,event_id,no_entry_cost,count,opened_at,resolved) VALUES ('X','E',0.8,1,1,1)",
#     ),
#     "fade_retail_longshot": (
#         "FADE_RETAIL_DB",
#         """CREATE TABLE positions (
#             ticker TEXT PRIMARY KEY, event_id TEXT, series TEXT, question TEXT,
#             yes_bid REAL, yes_ask REAL, no_entry_cost REAL, count INTEGER,
#             opened_at INTEGER, close_time TEXT,
#             resolved INTEGER DEFAULT 0, resolved_yes INTEGER, pnl REAL, closed_at INTEGER)""",
#         [("A", "E1", "S", "q", 0.20, 0.22, 0.80, 12, 100, "t", 1, 0, 2.0, 200),
#          ("B", "E2", "S", "q", 0.25, 0.27, 0.75, 13, 100, "t", 0, None, None, None)],
#         "INSERT INTO positions (ticker,event_id,no_entry_cost,count,opened_at,exit_reason) VALUES ('X','E',0.8,1,1,'bogus')",
#     ),

# --- test_fade_retail_take_profit_shape_is_allowed ---
# def test_fade_retail_take_profit_shape_is_allowed(tmp_path, monkeypatch):
#     """A TP exit sets resolved=1 with resolved_yes NULL — must not be rejected."""
#     monkeypatch.setenv("FADE_RETAIL_DB", str(tmp_path / "fr.sqlite"))
#     import fade_retail_longshot as fr
#     c = fr._con()
#     c.execute("INSERT INTO positions (ticker,event_id,no_entry_cost,count,opened_at) "
#               "VALUES ('T','E',0.8,10,1)")
#     c.execute("UPDATE positions SET resolved=1, pnl=0.5, exit_reason='take_profit', "
#               "exit_price=0.85, closed_at=2 WHERE ticker='T'")
#     assert c.execute("SELECT resolved_yes FROM positions").fetchone() == (None,)
