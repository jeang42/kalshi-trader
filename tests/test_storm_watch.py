"""Tests for the storm-detection trigger (pm/research/storm_watch.py).

The contract that matters: a NEW named storm or NEW storm-flavored series
alerts exactly ONCE (state dedupe), venue/feed failures degrade to "nothing
new" rather than paging anyone, and season-aggregate series never trigger.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from pm.feeds import nhc_data
from pm.research import storm_watch as sw


@pytest.fixture()
def state(tmp_path, monkeypatch):
    p = tmp_path / "storm_watch.json"
    monkeypatch.setenv("STORM_WATCH_STATE_FILE", str(p))
    return p


def _storm(sid="al052026", name="Erin", cls="HU"):
    return nhc_data.Storm(id=sid, name=name, classification=cls,
                          basin=sid[:2].upper())


class TestNhcParser:
    def test_named_classifications(self):
        assert _storm(cls="HU").is_named and _storm(cls="TS").is_named
        assert not _storm(cls="TD").is_named
        assert not _storm(cls="PTC").is_named

    def test_parser_tolerates_garbage(self):
        assert nhc_data.parse_current_storms(None) == []
        assert nhc_data.parse_current_storms({"activeStorms": [None, {}, 42]}) == []


class TestKalshiScan:
    def test_keyword_match_and_aggregate_exclusion(self, state):
        events = [
            {"event_ticker": "KXERINLAND-26SEP", "title": "Where will Hurricane Erin make landfall?"},
            {"event_ticker": "KXHURRICANE-26DEC01", "title": "How many major hurricanes this season?"},
            {"event_ticker": "KXBTCD-26AUG25", "title": "Bitcoin price today at 5pm EDT?"},
        ]
        with patch.object(sw.kc, "_paginate", return_value=events):
            found = sw.scan_kalshi_storm_series()
        assert found == {"KXERINLAND": "Where will Hurricane Erin make landfall?"}

    def test_venue_failure_degrades_to_empty(self, state):
        with patch.object(sw.kc, "_paginate", side_effect=RuntimeError("down")):
            assert sw.scan_kalshi_storm_series() == {}


class TestTriggerDedupe:
    def test_new_storm_alerts_once_then_never_again(self, state):
        with patch.object(sw.nhc_data, "active_storms", return_value=[_storm()]), \
             patch.object(sw, "scan_kalshi_storm_series", return_value={}), \
             patch.object(sw.notify, "alert") as alert:
            s1 = sw.run_once()
            s2 = sw.run_once()
        assert s1["new_storms"] == ["al052026"]
        assert s2["new_storms"] == []
        assert alert.call_count == 1, "the same storm must alert exactly once"

    def test_unnamed_depression_does_not_trigger(self, state):
        with patch.object(sw.nhc_data, "active_storms",
                          return_value=[_storm(cls="TD", name="Nine")]), \
             patch.object(sw, "scan_kalshi_storm_series", return_value={}), \
             patch.object(sw.notify, "alert") as alert:
            s = sw.run_once()
        assert s["new_storms"] == [] and alert.call_count == 0

    def test_new_series_alerts_once(self, state):
        found = {"KXERINLAND": "Where will Hurricane Erin make landfall?"}
        with patch.object(sw.nhc_data, "active_storms", return_value=[]), \
             patch.object(sw, "scan_kalshi_storm_series", return_value=found), \
             patch.object(sw.notify, "alert") as alert:
            s1 = sw.run_once()
            s2 = sw.run_once()
        assert s1["new_series"] == ["KXERINLAND"] and s2["new_series"] == []
        assert alert.call_count == 1

    def test_feed_failures_page_nobody(self, state):
        with patch.object(sw.nhc_data, "active_storms", return_value=[]), \
             patch.object(sw, "scan_kalshi_storm_series", return_value={}), \
             patch.object(sw.notify, "alert") as alert:
            s = sw.run_once()
        assert s == {"named_active": 0, "new_storms": [], "new_series": [],
                     "kalshi_series_seen": []}
        assert alert.call_count == 0
