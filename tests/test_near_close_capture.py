"""Offline tests for near-close capture (temp DB, no network)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pm.capture import near_close_capture as ncc


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setenv("NEAR_CLOSE_DB", str(tmp_path / "ncc.sqlite"))
    ncc.migrate()
    return tmp_path


NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


class TestPureHelpers:
    def test_lead_hours(self):
        assert ncc._lead_hours("2026-06-10T14:00:00Z", NOW) == pytest.approx(2.0)
        assert ncc._lead_hours("2026-06-10T11:00:00Z", NOW) == pytest.approx(-1.0)
        assert ncc._lead_hours("garbage", NOW) is None

    def test_yes_mid_prefers_book_then_last(self):
        assert ncc._yes_mid(0.40, 0.44, 0.30) == pytest.approx(0.42)
        assert ncc._yes_mid(None, None, 0.31) == pytest.approx(0.31)
        assert ncc._yes_mid(0.5, None, None) == pytest.approx(0.5)

    def test_resolution_updates_matches_only_known(self):
        upd = ncc._resolution_updates(
            ["A", "B", "C"], {"A": True, "C": False, "Z": True})
        assert upd == {"A": True, "C": False}


class TestStore:
    def test_capture_is_idempotent_keeps_first(self, tmpdb):
        row = {"ticker": "KX1-26", "question": "q", "category": "X",
               "event_id": "E1", "close_time": "2026-06-10T14:00:00Z",
               "snapshot_ts": NOW.isoformat(), "lead_hours": 2.0,
               "yes_bid": 0.40, "yes_ask": 0.44, "yes_mid": 0.42,
               "last_price": 0.41, "spread": 0.04, "volume": 500}
        assert ncc.store_snapshots([row]) == 1
        row2 = {**row, "yes_mid": 0.99}          # same ticker, later
        assert ncc.store_snapshots([row2]) == 0  # ignored
        panel = ncc.load_panel(resolved_only=False)
        assert panel.loc[0, "yes_mid"] == pytest.approx(0.42)  # first kept

    def test_resolve_flow(self, tmpdb):
        rows = [{"ticker": f"KX{i}-26", "question": "q", "category": "X",
                 "event_id": f"E{i}", "close_time": "2026-06-10T11:00:00Z",
                 "snapshot_ts": NOW.isoformat(), "lead_hours": 2.0,
                 "yes_bid": 0.5, "yes_ask": 0.5, "yes_mid": 0.5,
                 "last_price": 0.5, "spread": 0.0, "volume": 100}
                for i in range(3)]
        ncc.store_snapshots(rows)
        pending = ncc.unresolved_past_close(NOW)   # close_time 11:00 < NOW 12:00
        assert set(pending) == {"KX0-26", "KX1-26", "KX2-26"}
        n = ncc.apply_resolutions({"KX0-26": True, "KX1-26": False}, NOW)
        assert n == 2
        assert ncc.unresolved_past_close(NOW) == ["KX2-26"]
        s = ncc.stats()
        assert s["total"] == 3 and s["resolved"] == 2 and s["pending"] == 1

    def test_not_past_close_excluded_from_pending(self, tmpdb):
        ncc.store_snapshots([{
            "ticker": "FUT-26", "question": "q", "category": "X",
            "event_id": "E", "close_time": "2026-06-10T20:00:00Z",  # future
            "snapshot_ts": NOW.isoformat(), "lead_hours": 8.0,
            "yes_bid": 0.5, "yes_ask": 0.5, "yes_mid": 0.5, "last_price": 0.5,
            "spread": 0.0, "volume": 100}])
        assert ncc.unresolved_past_close(NOW) == []
