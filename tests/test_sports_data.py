"""Tests for the ESPN sports-data client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from pm.feeds import sports_data as sd

# ---------------------------------------------------------------------------
# Moneyline conversion + de-vig
# ---------------------------------------------------------------------------

class TestMoneylineToProb:
    def test_heavy_favorite(self):
        # -200 → 200/300 = 0.6667
        assert sd.moneyline_to_prob(-200) == pytest.approx(0.6667, abs=1e-4)

    def test_heavy_underdog(self):
        # +200 → 100/300 = 0.3333
        assert sd.moneyline_to_prob(200) == pytest.approx(0.3333, abs=1e-4)

    def test_pickem(self):
        # -100 and +100 both → 0.5
        assert sd.moneyline_to_prob(-100) == pytest.approx(0.5)
        assert sd.moneyline_to_prob(100) == pytest.approx(0.5)

    def test_zero_returns_none(self):
        assert sd.moneyline_to_prob(0) is None

    def test_none_returns_none(self):
        assert sd.moneyline_to_prob(None) is None

    def test_unparseable_returns_none(self):
        assert sd.moneyline_to_prob("not-a-number") is None


class TestDevig:
    def test_balanced_book_unchanged(self):
        # Both sides at 50% → already fair → still 50/50
        assert sd.devig(0.5, 0.5) == (pytest.approx(0.5), pytest.approx(0.5))

    def test_real_book_renormalizes(self):
        # -205 ↔ +170 raw: 0.672 / 0.370 → sum 1.042 → fair ~0.645 / ~0.355
        h, a = sd.devig(0.672, 0.370)
        assert h == pytest.approx(0.645, abs=0.001)
        assert a == pytest.approx(0.355, abs=0.001)
        assert h + a == pytest.approx(1.0)

    def test_none_passthrough(self):
        assert sd.devig(None, 0.5) == (None, None)
        assert sd.devig(0.5, None) == (None, None)

    def test_zero_total_returns_none(self):
        assert sd.devig(0.0, 0.0) == (None, None)


# ---------------------------------------------------------------------------
# Event parsing from raw JSON
# ---------------------------------------------------------------------------

def _raw_event(home_name: str, away_name: str, *, eid: str = "1",
               state: str = "pre", home_ml: int | None = -200,
               away_ml: int | None = 170) -> tuple[dict, dict]:
    """Build a (scoreboard-row, summary) pair shaped like ESPN's responses."""
    raw = {
        "id": eid,
        "name": f"{away_name} at {home_name}",
        "status": {"type": {"state": state}},
        "competitions": [{
            "competitors": [
                {"homeAway": "home", "team": {
                    "displayName": home_name, "shortDisplayName": home_name.split()[-1],
                    "abbreviation": "HOM", "name": home_name.split()[-1]}},
                {"homeAway": "away", "team": {
                    "displayName": away_name, "shortDisplayName": away_name.split()[-1],
                    "abbreviation": "AWY", "name": away_name.split()[-1]}},
            ],
        }],
    }
    summary = {"pickcenter": [{
        "homeTeamOdds": {"moneyLine": home_ml},
        "awayTeamOdds": {"moneyLine": away_ml},
    }]} if home_ml is not None and away_ml is not None else {"pickcenter": []}
    return raw, summary


class TestEventParsing:
    def test_basic_event_with_odds(self):
        raw, summary = _raw_event("Los Angeles Lakers", "Miami Heat",
                                   home_ml=-200, away_ml=170)
        ev = sd._event_from_raw(raw, league="NBA", summary=summary)
        assert ev is not None
        assert ev.league == "NBA"
        assert ev.home.name == "Los Angeles Lakers"
        assert ev.away.name == "Miami Heat"
        assert ev.state == "pre"
        # -200/+170 → 0.667/0.370 → vig-free ~0.643/0.357
        assert ev.home_winprob == pytest.approx(0.643, abs=0.01)
        assert ev.away_winprob == pytest.approx(0.357, abs=0.01)
        assert ev.home_winprob + ev.away_winprob == pytest.approx(1.0)

    def test_event_without_pickcenter(self):
        raw, summary = _raw_event("A", "B", home_ml=None, away_ml=None)
        ev = sd._event_from_raw(raw, league="NBA", summary=summary)
        assert ev is not None
        assert ev.home_winprob is None
        assert ev.away_winprob is None

    def test_malformed_event_returns_none(self):
        # Only one competitor — invalid shape
        raw = {"id": "1", "competitions": [{"competitors": [{"homeAway": "home"}]}]}
        assert sd._event_from_raw(raw, league="NBA") is None

    def test_no_competitions_returns_none(self):
        raw = {"id": "1"}
        assert sd._event_from_raw(raw, league="NBA") is None

    def test_summary_none_leaves_probs_none(self):
        raw, _ = _raw_event("A", "B")
        ev = sd._event_from_raw(raw, league="NBA", summary=None)
        assert ev is not None
        assert ev.home_winprob is None


# ---------------------------------------------------------------------------
# Scoreboard + summary HTTP layer
# ---------------------------------------------------------------------------

class TestScoreboard:
    @patch.object(sd, "_get")
    def test_returns_events_list(self, mock_get):
        mock_get.return_value = {"events": [{"id": "1"}, {"id": "2"}]}
        events = sd.get_scoreboard("NBA")
        assert len(events) == 2
        mock_get.assert_called_once_with("sports/basketball/nba/scoreboard")

    def test_unknown_league_returns_empty(self):
        assert sd.get_scoreboard("CRICKET") == []

    @patch.object(sd, "_get")
    def test_http_failure_returns_empty(self, mock_get):
        mock_get.side_effect = requests.RequestException("503")
        assert sd.get_scoreboard("NBA") == []


class TestGetEventsWithProbabilities:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        # The function memoizes per league for EVENTS_CACHE_TTL; clear it so
        # each test fetches fresh rather than seeing a prior test's NBA list.
        sd._events_cache.clear()
        yield
        sd._events_cache.clear()

    @patch.object(sd, "get_event_summary")
    @patch.object(sd, "get_scoreboard")
    def test_pipes_summary_through(self, mock_sb, mock_sum):
        raw_a, sum_a = _raw_event("A", "B", eid="1")
        raw_b, _ = _raw_event("C", "D", eid="2", home_ml=None, away_ml=None)
        mock_sb.return_value = [raw_a, raw_b]
        mock_sum.side_effect = [sum_a, {"pickcenter": []}]
        events = sd.get_events_with_probabilities("NBA")
        assert len(events) == 2
        assert events[0].home_winprob is not None
        assert events[1].home_winprob is None

    @patch.object(sd, "get_event_summary", return_value=None)
    @patch.object(sd, "get_scoreboard")
    def test_skips_summary_failures(self, mock_sb, _ms):
        raw, _ = _raw_event("A", "B")
        mock_sb.return_value = [raw]
        events = sd.get_events_with_probabilities("NBA")
        # Event still appears, just without odds
        assert len(events) == 1
        assert events[0].home_winprob is None

    @patch.object(sd, "get_event_summary")
    @patch.object(sd, "get_scoreboard")
    def test_caches_within_ttl(self, mock_sb, mock_sum):
        raw_a, sum_a = _raw_event("A", "B", eid="1")
        mock_sb.return_value = [raw_a]
        mock_sum.return_value = sum_a
        sd.get_events_with_probabilities("NBA")
        sd.get_events_with_probabilities("NBA")  # served from cache
        assert mock_sb.call_count == 1
        # use_cache=False forces a fresh fetch
        sd.get_events_with_probabilities("NBA", use_cache=False)
        assert mock_sb.call_count == 2


# ---------------------------------------------------------------------------
# Retry / 429 behaviour
# ---------------------------------------------------------------------------

class TestHttpRetry:
    @patch.object(sd, "time")
    @patch.object(sd._session, "get")
    def test_retries_on_429(self, mock_get, _mock_time):
        first = MagicMock(status_code=429, headers={"Retry-After": "1"})
        second = MagicMock(status_code=200)
        second.json.return_value = {"ok": True}
        second.raise_for_status = lambda: None
        mock_get.side_effect = [first, second]
        result = sd._get("sports/basketball/nba/scoreboard")
        assert result == {"ok": True}
        assert mock_get.call_count == 2
