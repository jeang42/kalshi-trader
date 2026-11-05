"""Offline tests for wnba_capture: team-index matching + store (temp DB)."""

from __future__ import annotations

import pandas as pd
import pytest

from pm.capture import wnba_capture as wc


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setenv("WNBA_CAPTURE_DB", str(tmp_path / "wc.sqlite"))
    wc.migrate()
    return tmp_path


class TestTeamIndex:
    def test_build_and_match(self):
        mk = pd.DataFrame([
            {"condition_id": "KXWNBAGAME-26JUN03TORNY-NY", "best_bid": 0.6,
             "best_ask": 0.63, "question": "Will NY win?"},
            {"condition_id": "KXWNBAGAME-26JUN03TORNY-TOR", "best_bid": 0.37,
             "best_ask": 0.40, "question": "Will TOR win?"},
        ])
        idx = wc.build_team_index(mk)
        assert wc.match_market("NY", idx)["condition_id"] == "KXWNBAGAME-26JUN03TORNY-NY"
        assert wc.match_market("TOR", idx)["condition_id"] == "KXWNBAGAME-26JUN03TORNY-TOR"
        assert wc.match_market("ATL", idx) is None

    def test_ambiguous_dropped(self):
        # same yes-team in two markets (e.g. doubleheader) → ambiguous, skip.
        mk = pd.DataFrame([
            {"condition_id": "KXWNBAGAME-26JUN03TORNY-NY", "best_bid": 0.6, "best_ask": 0.63},
            {"condition_id": "KXWNBAGAME-26JUN03CONNY-NY", "best_bid": 0.5, "best_ask": 0.53},
        ])
        idx = wc.build_team_index(mk)
        assert wc.match_market("NY", idx) is None

    def test_yes_mid(self):
        assert wc._yes_mid(0.40, 0.44, 0.3) == pytest.approx(0.42)
        assert wc._yes_mid(None, None, 0.31) == pytest.approx(0.31)


class TestStore:
    def _row(self, gid, seq, ben="NY", ticker="KXWNBAGAME-26JUN03TORNY-NY"):
        return {"game_id": gid, "event_seq": seq, "kind": "wp_swing",
                "beneficiary": ben, "wp_delta": 0.15, "detail": "x",
                "captured_at": "2026-06-20T23:00:00+00:00", "ticker": ticker,
                "question": "Will NY win?", "yes_bid": 0.6, "yes_ask": 0.63,
                "yes_mid": 0.615}

    def test_store_dedup(self, tmpdb):
        assert wc.store_events([self._row("g1", 1)]) == 1
        assert wc.store_events([self._row("g1", 1)]) == 0      # dup
        assert wc.store_events([self._row("g1", 2)]) == 1      # next event
        s = wc.stats()
        assert s["total"] == 2 and s["matched_to_market"] == 2

    def test_unmatched_stored(self, tmpdb):
        r = self._row("g2", 1)
        r["ticker"] = None
        assert wc.store_events([r]) == 1
        assert wc.stats()["matched_to_market"] == 0
