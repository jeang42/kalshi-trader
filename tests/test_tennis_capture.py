"""Offline tests for tennis_capture: name matching + store (temp DB)."""

from __future__ import annotations

import pandas as pd
import pytest

from pm.capture import tennis_capture as tc


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setenv("TENNIS_CAPTURE_DB", str(tmp_path / "tc.sqlite"))
    tc.migrate()
    return tmp_path


class TestNameMatch:
    def test_norm_strips_accents_and_case(self):
        assert tc._norm("Félix Auger-Aliassime") == "felix auger aliassime"

    def test_build_index_and_match(self):
        mk = pd.DataFrame([
            {"condition_id": "KXATPMATCH-1-ALC", "question": "Will Carlos Alcaraz win the Wimbledon?",
             "best_bid": 0.6, "best_ask": 0.63},
            {"condition_id": "KXATPMATCH-1-SIN", "question": "Will Jannik Sinner win the Wimbledon?",
             "best_bid": 0.37, "best_ask": 0.4},
        ])
        idx = tc.build_player_index(mk)
        row = tc.match_market("Carlos Alcaraz", idx)
        assert row is not None and row["condition_id"] == "KXATPMATCH-1-ALC"

    def test_accent_insensitive_match(self):
        mk = pd.DataFrame([{"condition_id": "KXATPMATCH-1-FAA",
                            "question": "Will Felix Auger-Aliassime win the Halle?",
                            "best_bid": 0.5, "best_ask": 0.53}])
        idx = tc.build_player_index(mk)
        assert tc.match_market("Félix Auger-Aliassime", idx)["condition_id"] \
            == "KXATPMATCH-1-FAA"

    def test_surname_fallback_unique(self):
        mk = pd.DataFrame([{"condition_id": "KXATPMATCH-1-DJO",
                            "question": "Will Novak Djokovic win the French Open?",
                            "best_bid": 0.7, "best_ask": 0.72}])
        idx = tc.build_player_index(mk)
        assert tc.match_market("N. Djokovic", idx)["condition_id"] == "KXATPMATCH-1-DJO"

    def test_no_match_returns_none(self):
        mk = pd.DataFrame([{"condition_id": "KXATPMATCH-1-ALC",
                            "question": "Will Carlos Alcaraz win the Wimbledon?",
                            "best_bid": 0.6, "best_ask": 0.63}])
        idx = tc.build_player_index(mk)
        assert tc.match_market("Daniil Medvedev", idx) is None

    def test_ambiguous_duplicate_dropped(self):
        mk = pd.DataFrame([
            {"condition_id": "A", "question": "Will Player One win the X?", "best_bid": 0.5, "best_ask": 0.5},
            {"condition_id": "B", "question": "Will Player One win the Y?", "best_bid": 0.5, "best_ask": 0.5},
        ])
        idx = tc.build_player_index(mk)
        assert tc.match_market("Player One", idx) is None  # ambiguous → skip


class TestStore:
    def _row(self, mid, seq, kind="set", ben="A", ticker="KX-1"):
        return {"match_id": mid, "event_seq": seq, "kind": kind, "beneficiary": ben,
                "tournament": "T", "detail": "set 1-0", "captured_at": "2026-06-19T12:00:00+00:00",
                "ticker": ticker, "question": "Will A win the T?",
                "yes_is_beneficiary": 1, "yes_bid": 0.5, "yes_ask": 0.53, "yes_mid": 0.515}

    def test_store_dedup(self, tmpdb):
        assert tc.store_events([self._row("m1", 1)]) == 1
        assert tc.store_events([self._row("m1", 1)]) == 0      # dup
        assert tc.store_events([self._row("m1", 2)]) == 1      # next event
        s = tc.stats()
        assert s["total"] == 2 and s["matched_to_market"] == 2

    def test_unmatched_event_stored(self, tmpdb):
        r = self._row("m2", 1)
        r["ticker"] = None
        r["yes_is_beneficiary"] = 0
        assert tc.store_events([r]) == 1
        assert tc.stats()["matched_to_market"] == 0
