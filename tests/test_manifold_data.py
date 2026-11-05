"""Tests for the Manifold Markets community-forecast client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from pm.feeds import manifold_data as md

# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------

class TestRowToMarket:
    def _row(self, **overrides):
        base = {
            "id": "abc", "question": "Q?", "outcomeType": "BINARY",
            "probability": 0.42, "volume": 1000, "isResolved": False,
            "closeTime": 1790726340000,
        }
        base.update(overrides)
        return base

    def test_binary_parses(self):
        m = md._row_to_market(self._row())
        assert m is not None
        assert m.market_id == "abc"
        assert m.probability == pytest.approx(0.42)
        assert m.volume == pytest.approx(1000)
        assert m.is_resolved is False
        assert m.close_time_ms == 1790726340000

    def test_multiple_choice_rejected(self):
        assert md._row_to_market(self._row(outcomeType="MULTIPLE_CHOICE")) is None

    def test_pseudonumeric_rejected(self):
        assert md._row_to_market(self._row(outcomeType="PSEUDO_NUMERIC")) is None

    def test_missing_probability_rejected(self):
        assert md._row_to_market(self._row(probability=None)) is None

    def test_out_of_range_probability_rejected(self):
        assert md._row_to_market(self._row(probability=1.5)) is None
        assert md._row_to_market(self._row(probability=-0.1)) is None

    def test_string_probability_coerced(self):
        m = md._row_to_market(self._row(probability="0.55"))
        assert m is not None
        assert m.probability == pytest.approx(0.55)

    def test_resolved_market_still_parses(self):
        m = md._row_to_market(self._row(isResolved=True))
        assert m is not None
        assert m.is_resolved is True


# ---------------------------------------------------------------------------
# search_markets
# ---------------------------------------------------------------------------

class TestSearchMarkets:
    @patch.object(md, "_get")
    def test_returns_binary_only(self, mock_get):
        mock_get.return_value = [
            {"id": "1", "question": "Q1?", "outcomeType": "BINARY",
             "probability": 0.3, "volume": 100, "isResolved": False},
            {"id": "2", "question": "Q2?", "outcomeType": "MULTIPLE_CHOICE",
             "probability": None, "volume": 50, "isResolved": False},
            {"id": "3", "question": "Q3?", "outcomeType": "BINARY",
             "probability": 0.6, "volume": 200, "isResolved": False},
        ]
        out = md.search_markets("anything", limit=5)
        assert len(out) == 2
        assert {m.market_id for m in out} == {"1", "3"}

    @patch.object(md, "_get")
    def test_empty_query_short_circuits(self, mock_get):
        assert md.search_markets("") == []
        assert md.search_markets("   ") == []
        mock_get.assert_not_called()

    @patch.object(md, "_get")
    def test_http_failure_returns_empty(self, mock_get):
        mock_get.side_effect = requests.RequestException("boom")
        assert md.search_markets("Q") == []

    @patch.object(md, "_get")
    def test_only_open_filter_is_passed(self, mock_get):
        mock_get.return_value = []
        md.search_markets("Q", only_open=True)
        params = mock_get.call_args.kwargs["params"]
        assert params.get("filter") == "open"

        md.search_markets("Q", only_open=False)
        params = mock_get.call_args.kwargs["params"]
        assert "filter" not in params

    @patch.object(md, "_get")
    def test_non_list_response_returns_empty(self, mock_get):
        mock_get.return_value = {"error": "weird shape"}
        assert md.search_markets("Q") == []


# ---------------------------------------------------------------------------
# get_market
# ---------------------------------------------------------------------------

class TestGetMarket:
    @patch.object(md, "_get")
    def test_happy_path(self, mock_get):
        mock_get.return_value = {
            "id": "x", "question": "Q?", "outcomeType": "BINARY",
            "probability": 0.7, "volume": 500, "isResolved": False,
            "closeTime": 1790726340000,
        }
        m = md.get_market("x")
        assert m is not None
        assert m.probability == pytest.approx(0.7)
        mock_get.assert_called_once_with("market/x")

    @patch.object(md, "_get")
    def test_failure_returns_none(self, mock_get):
        mock_get.side_effect = requests.RequestException("404")
        assert md.get_market("x") is None

    @patch.object(md, "_get")
    def test_non_dict_returns_none(self, mock_get):
        mock_get.return_value = ["not", "a", "dict"]
        assert md.get_market("x") is None


# ---------------------------------------------------------------------------
# Jaccard similarity + best_match
# ---------------------------------------------------------------------------

class TestSimilarity:
    def test_identical_questions_max_similarity(self):
        s = md.jaccard_similarity("Trump impeached 2026?", "Trump impeached 2026?")
        assert s == pytest.approx(1.0)

    def test_disjoint_tokens_zero(self):
        assert md.jaccard_similarity("Bitcoin to $200k", "Heat vs. Lakers") == 0.0

    def test_partial_overlap(self):
        # "Trump impeached 2026" tokens: {trump, impeached, 2026}
        # "Trump impeached by house 2026" tokens: {trump, impeached, house, 2026}
        # intersection=3, union=4, j=0.75
        s = md.jaccard_similarity(
            "Trump impeached 2026?", "Trump impeached by house 2026?",
        )
        assert s == pytest.approx(0.75)

    def test_stopwords_dropped(self):
        # "Will the Heat win?" → tokens after stopword removal = {heat, win}
        # "Heat win" → tokens = {heat, win}
        assert md.jaccard_similarity("Will the Heat win?", "Heat win") == pytest.approx(1.0)

    def test_empty_strings_zero(self):
        assert md.jaccard_similarity("", "anything") == 0.0
        assert md.jaccard_similarity("anything", "") == 0.0


class TestBestMatch:
    def _market(self, *, q="Q", prob=0.5, mid="x"):
        return md.ManifoldMarket(
            market_id=mid, question=q, probability=prob,
            volume=1000, is_resolved=False, close_time_ms=None,
        )

    def test_picks_highest_similarity(self):
        candidates = [
            self._market(q="Will Manchester United win the FA Cup?", mid="far"),
            self._market(q="Trump impeached by House in 2026?", mid="close"),
            self._market(q="Bitcoin to $200k by 2027?", mid="unrelated"),
        ]
        result = md.best_match("Trump impeached 2026?", candidates)
        assert result is not None
        chosen, score = result
        assert chosen.market_id == "close"
        assert score > 0.5

    def test_empty_candidates_returns_none(self):
        assert md.best_match("Q?", []) is None

    def test_returns_score_with_match(self):
        c = self._market(q="exact match", mid="a")
        result = md.best_match("exact match", [c])
        assert result is not None
        assert result[1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 429 retry
# ---------------------------------------------------------------------------

class TestHttpRetry:
    @patch.object(md, "time")
    @patch.object(md._session, "get")
    def test_retries_on_429(self, mock_get, _mock_time):
        first = MagicMock(status_code=429, headers={"Retry-After": "1"})
        second = MagicMock(status_code=200)
        second.json.return_value = [{"id": "x"}]
        second.raise_for_status = lambda: None
        mock_get.side_effect = [first, second]
        result = md._get("search-markets")
        assert result == [{"id": "x"}]
        assert mock_get.call_count == 2
