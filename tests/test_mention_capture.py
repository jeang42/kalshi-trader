"""Tests for mention_capture's dynamic series discovery (2026-09-01).

The contract: any open series whose ticker contains MENTION is discovered
(a hardcoded list missed an earnings wave twice); a failed sweep returns ()
so the caller keeps its previous watchlist; seeds stay first and are never
duplicated.
"""
from __future__ import annotations

from unittest.mock import patch

from pm.capture import mention_capture as mc


class TestDiscovery:
    def test_finds_mention_series_and_ignores_the_rest(self):
        events = [
            {"event_ticker": "KXEARNINGSMENTIONORCL-26SEP09", "title": "Oracle call"},
            {"event_ticker": "KXEARNINGSMENTIONCOST-26SEP25", "title": "Costco call"},
            {"event_ticker": "KXFEDMENTION-26SEP17", "title": "Fed presser"},
            {"event_ticker": "KXBTCD-26SEP01", "title": "Bitcoin"},
            {"event_ticker": "", "title": "garbage"},
        ]
        with patch.object(mc.kc, "_paginate", return_value=events):
            found = mc.discover_mention_series()
        assert found == ("KXEARNINGSMENTIONCOST", "KXEARNINGSMENTIONORCL",
                         "KXFEDMENTION")

    def test_venue_failure_degrades_to_empty(self):
        with patch.object(mc.kc, "_paginate", side_effect=RuntimeError("down")):
            assert mc.discover_mention_series() == ()

    def test_merge_keeps_seeds_first_and_dedupes(self):
        dyn = ("KXNEWMENTION", mc.MENTION_SERIES[0])
        merged = mc._merged_watchlist(dyn)
        assert merged[:len(mc.MENTION_SERIES)] == mc.MENTION_SERIES
        assert merged.count(mc.MENTION_SERIES[0]) == 1
        assert merged[-1] == "KXNEWMENTION"
