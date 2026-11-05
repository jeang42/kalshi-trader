"""Tests for pure functions in polymarket_client.

API-calling functions (get_markets, get_market, get_price_history, etc.) are
tested with mocked HTTP responses so no network is required.
"""

import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pm.venue.polymarket_client import (
    _category_from_event_tags,
    _infer_category,
    _parse_prices,
    enrich_categories_from_events,
    get_closed_markets,
    get_events,
    get_market,
    get_market_calibration_data,
    get_markets,
    get_order_book,
    get_price_history,
    order_book_stats,
    parse_json_field,
    parse_order_book,
    scan_cross_market_arb,
    scan_multi_outcome_gaps,
    scan_narrow_spreads,
    scan_price_divergence,
)

# ---------------------------------------------------------------------------
# Category inference
# ---------------------------------------------------------------------------

class TestInferCategory:
    def test_crypto_keyword(self):
        assert _infer_category("Will Bitcoin hit $100k?", []) == "Crypto"

    def test_crypto_ticker(self):
        assert _infer_category("BTC price above $80k by Friday?", []) == "Crypto"

    def test_sports_keyword(self):
        assert _infer_category("NBA Finals Game 7 winner", []) == "Sports"

    def test_sports_series_slug_overrides_keywords(self):
        # question has no sports keywords, but seriesSlug is authoritative
        assert _infer_category("Will the home team win?", [{"seriesSlug": "nba"}]) == "Sports"

    def test_sports_series_slug_partial_match(self):
        # nhl-2026 contains "nhl"
        assert _infer_category("A game", [{"seriesSlug": "nhl-2026"}]) == "Sports"

    def test_politics_keyword(self):
        assert _infer_category("Will Trump sign the bill?", []) == "Politics"

    def test_finance_keyword(self):
        assert _infer_category("Will the Fed cut rates in June?", []) == "Finance"

    def test_tech_keyword(self):
        assert _infer_category("Will OpenAI release GPT-5?", []) == "Tech"

    def test_geopolitics_keyword(self):
        assert _infer_category("Will Russia invade by March?", []) == "Geopolitics"

    def test_entertainment_keyword(self):
        assert _infer_category("Will the album debut at #1?", []) == "Entertainment"

    def test_fallthrough_to_other(self):
        assert _infer_category("Something completely unrelated", []) == "Other"

    def test_case_insensitive(self):
        assert _infer_category("BITCOIN PRICE PREDICTION", []) == "Crypto"

    def test_first_rule_wins(self):
        # "blockchain" is Crypto; also mentions "ai" which is Tech — Crypto rule comes first
        assert _infer_category("Will blockchain AI tokens rise?", []) == "Crypto"

    def test_events_none_handled(self):
        # events=None should not raise
        assert _infer_category("Some question", None) == "Other"

    def test_event_without_series_slug(self):
        assert _infer_category("Some game", [{"id": "123"}]) == "Other"


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

class TestParseJsonField:
    def test_json_string(self):
        assert parse_json_field('["Yes", "No"]') == ["Yes", "No"]

    def test_list_passthrough(self):
        assert parse_json_field(["Yes", "No"]) == ["Yes", "No"]

    def test_none(self):
        assert parse_json_field(None) == []

    def test_malformed_string(self):
        assert parse_json_field("not json") == []

    def test_empty_string(self):
        assert parse_json_field("") == []

    def test_non_list_value_returns_empty(self):
        assert parse_json_field(42) == []
        assert parse_json_field({"a": 1}) == []


class TestParsePrices:
    def test_json_string(self):
        assert _parse_prices('["0.4", "0.6"]') == [0.4, 0.6]

    def test_plain_list(self):
        assert _parse_prices([0.4, 0.6]) == [0.4, 0.6]

    def test_list_of_strings(self):
        assert _parse_prices(["0.35", "0.65"]) == [0.35, 0.65]

    def test_empty_string(self):
        assert _parse_prices("[]") == []

    def test_none(self):
        assert _parse_prices(None) == []

    def test_malformed_string(self):
        assert _parse_prices("not json") == []

    def test_three_outcomes(self):
        result = _parse_prices('["0.50", "0.30", "0.20"]')
        assert result == pytest.approx([0.50, 0.30, 0.20])


# ---------------------------------------------------------------------------
# Scanner: narrow spreads
# ---------------------------------------------------------------------------

class TestScanNarrowSpreads:
    def test_returns_markets_below_threshold(self, sample_markets_df):
        result = scan_narrow_spreads(sample_markets_df, max_spread=0.005, min_liquidity=500)
        assert len(result) >= 1
        assert all(result["spread"] <= 0.005)

    def test_excludes_low_liquidity(self, sample_markets_df):
        result = scan_narrow_spreads(sample_markets_df, max_spread=0.10, min_liquidity=1_000)
        assert all(result["liquidity"] >= 1_000)

    def test_sorted_by_spread(self, sample_markets_df):
        result = scan_narrow_spreads(sample_markets_df, max_spread=0.01, min_liquidity=0)
        spreads = result["spread"].tolist()
        assert spreads == sorted(spreads)

    def test_required_columns_present(self, sample_markets_df):
        result = scan_narrow_spreads(sample_markets_df, max_spread=0.01, min_liquidity=0)
        for col in ["question", "category", "yes_price", "spread", "best_bid", "best_ask"]:
            assert col in result.columns

    def test_empty_when_no_match(self, sample_markets_df):
        result = scan_narrow_spreads(sample_markets_df, max_spread=0.0001, min_liquidity=0)
        assert result.empty

    def test_excludes_zero_spread(self, sample_markets_df):
        # zero spread rows should be excluded (market with no book)
        df = sample_markets_df.copy()
        df.loc[0, "spread"] = 0.0
        result = scan_narrow_spreads(df, max_spread=0.01, min_liquidity=0)
        assert not any(result["spread"] == 0.0)


# ---------------------------------------------------------------------------
# Scanner: price divergence
# ---------------------------------------------------------------------------

class TestScanPriceDivergence:
    def test_finds_divergent_market(self, sample_markets_df):
        # "Will candidate A win?" has mid=0.60, last_trade=0.75 → divergence=0.15
        result = scan_price_divergence(sample_markets_df, min_divergence=0.10, min_liquidity=0)
        assert len(result) >= 1
        assert any("candidate" in q for q in result["question"])

    def test_multi_outcome_markets_excluded(self, sample_markets_df):
        """Multi-outcome markets are filtered out because lastTradePrice is a
        single per-market value and can't be meaningfully compared against
        outcome_prices[0] when there are 3+ outcomes."""
        # The "who wins the race?" row has 3 outcomes [Alice, Bob, Carol]
        result = scan_price_divergence(sample_markets_df, min_divergence=0.0, min_liquidity=0)
        for q in result["question"]:
            assert "race" not in q  # the 3-outcome market should not appear

    def test_excludes_low_divergence(self, sample_markets_df):
        result = scan_price_divergence(sample_markets_df, min_divergence=0.50, min_liquidity=0)
        assert result.empty

    def test_excludes_low_liquidity(self, sample_markets_df):
        result = scan_price_divergence(sample_markets_df, min_divergence=0.05, min_liquidity=100_000)
        assert result.empty

    def test_sorted_descending_by_divergence(self, sample_markets_df):
        result = scan_price_divergence(sample_markets_df, min_divergence=0.0, min_liquidity=0)
        divs = result["divergence"].tolist()
        assert divs == sorted(divs, reverse=True)

    def test_required_columns(self, sample_markets_df):
        result = scan_price_divergence(sample_markets_df, min_divergence=0.0, min_liquidity=0)
        for col in ["question", "mid_price", "last_trade_price", "divergence"]:
            assert col in result.columns


# ---------------------------------------------------------------------------
# Scanner: multi-outcome gaps
# ---------------------------------------------------------------------------

class TestScanMultiOutcomeGaps:
    def test_finds_gap(self, sample_markets_df):
        # "who wins the race?" sums to 0.90 — gap of 0.10
        result = scan_multi_outcome_gaps(sample_markets_df, min_deviation=0.05, min_liquidity=0)
        assert len(result) == 1
        assert "race" in result.iloc[0]["question"]

    def test_deviation_value(self, sample_markets_df):
        result = scan_multi_outcome_gaps(sample_markets_df, min_deviation=0.0, min_liquidity=0)
        row = result[result["question"].str.contains("race")].iloc[0]
        assert row["sum"] == pytest.approx(0.90, abs=0.001)
        assert row["deviation"] == pytest.approx(-0.10, abs=0.001)

    def test_binary_markets_excluded(self, sample_markets_df):
        # All binary markets should be filtered out (need 3+ outcomes)
        result = scan_multi_outcome_gaps(sample_markets_df, min_deviation=0.0, min_liquidity=0)
        for q in result["question"]:
            assert "Bitcoin" not in q
            assert "candidate" not in q

    def test_excludes_low_liquidity(self, sample_markets_df):
        result = scan_multi_outcome_gaps(sample_markets_df, min_deviation=0.0, min_liquidity=100_000)
        assert result.empty


# ---------------------------------------------------------------------------
# API functions — mocked HTTP
# ---------------------------------------------------------------------------

MOCK_MARKET = {
    "id": "1",
    "conditionId": "0xabc123",
    "question": "Will Bitcoin hit $100k?",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.42", "0.58"]',
    "volume": "1000000",
    "volume24hr": "50000",
    "liquidity": "10000",
    "endDate": "2026-12-31T00:00:00Z",
    "active": True,
    "spread": "0.002",
    "bestBid": "0.419",
    "bestAsk": "0.421",
    "lastTradePrice": "0.42",
    "events": [],
}

MOCK_MARKET_DETAIL = {
    **MOCK_MARKET,
    "clobTokenIds": '["98022490269692409998", "53831553061883006530"]',
    "tokens": None,
}


class TestGetMarkets:
    @patch("pm.venue.polymarket_client._get")
    def test_returns_dataframe(self, mock_get):
        mock_get.return_value = [MOCK_MARKET]
        df = get_markets(limit=1)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1

    @patch("pm.venue.polymarket_client._get")
    def test_columns_present(self, mock_get):
        mock_get.return_value = [MOCK_MARKET]
        df = get_markets(limit=1)
        for col in ["condition_id", "question", "category", "volume_24hr",
                    "liquidity", "spread", "best_bid", "best_ask", "last_trade_price"]:
            assert col in df.columns

    @patch("pm.venue.polymarket_client._get")
    def test_category_inferred(self, mock_get):
        mock_get.return_value = [MOCK_MARKET]
        df = get_markets(limit=1)
        assert df.iloc[0]["category"] == "Crypto"

    @patch("pm.venue.polymarket_client._get")
    def test_outcomes_parsed_to_list(self, mock_get):
        """Contract: outcomes/outcome_prices are ALWAYS native Python lists,
        never JSON strings. Regression test for the bug where downstream code
        iterated them character-by-character."""
        mock_get.return_value = [MOCK_MARKET]
        df = get_markets(limit=1)
        row = df.iloc[0]
        assert isinstance(row["outcomes"], list)
        assert row["outcomes"] == ["Yes", "No"]
        assert isinstance(row["outcome_prices"], list)
        assert row["outcome_prices"] == [pytest.approx(0.42), pytest.approx(0.58)]

    @patch("pm.venue.polymarket_client._get")
    def test_outcomes_iterate_safely(self, mock_get):
        """The bug we just fixed: zip(outcomes, prices) would iterate characters."""
        mock_get.return_value = [MOCK_MARKET]
        df = get_markets(limit=1)
        row = df.iloc[0]
        pairs = list(zip(row["outcomes"], row["outcome_prices"], strict=True))
        assert pairs == [("Yes", pytest.approx(0.42)), ("No", pytest.approx(0.58))]

    @patch("pm.venue.polymarket_client._get")
    def test_days_to_resolution_computed(self, mock_get):
        """end_date is converted to days_to_resolution."""
        import datetime as _dt
        # Build a market that ends 10 days from now
        far_future = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=10)).isoformat()
        market = {**MOCK_MARKET, "endDate": far_future}
        mock_get.return_value = [market]
        df = get_markets(limit=1)
        # ~10 days, allow some slack for test execution
        assert 9.5 <= df.iloc[0]["days_to_resolution"] <= 10.5

    @patch("pm.venue.polymarket_client._get")
    def test_days_to_resolution_clipped_at_zero(self, mock_get):
        """Past-due end_dates show 0 days, not negative."""
        market = {**MOCK_MARKET, "endDate": "2020-01-01T00:00:00Z"}
        mock_get.return_value = [market]
        df = get_markets(limit=1)
        assert df.iloc[0]["days_to_resolution"] == 0

    @patch("pm.venue.polymarket_client._get")
    def test_days_to_resolution_handles_invalid_date(self, mock_get):
        market = {**MOCK_MARKET, "endDate": "not a date"}
        mock_get.return_value = [market]
        df = get_markets(limit=1)
        # NaT → NaN after the subtraction; we just check it doesn't crash
        assert "days_to_resolution" in df.columns

    @patch("pm.venue.polymarket_client._get")
    def test_empty_api_response(self, mock_get):
        mock_get.return_value = []
        df = get_markets(limit=10)
        assert df.empty

    @patch("pm.venue.polymarket_client._get")
    def test_paginates_until_limit(self, mock_get):
        # Return full pages until we've collected `limit` rows
        mock_get.side_effect = [
            [MOCK_MARKET] * 100,  # page 1
            [MOCK_MARKET] * 50,   # page 2 (partial — stops here)
        ]
        df = get_markets(limit=150)
        assert len(df) == 150

    @patch("pm.venue.polymarket_client._get")
    def test_stops_on_short_page(self, mock_get):
        mock_get.side_effect = [
            [MOCK_MARKET] * 30,   # only 30 results — no more pages
        ]
        df = get_markets(limit=100)
        assert len(df) == 30


class TestGetMarket:
    @patch("pm.venue.polymarket_client._get")
    def test_returns_dict(self, mock_get):
        mock_get.return_value = [MOCK_MARKET_DETAIL]
        result = get_market("0xabc123")
        assert isinstance(result, dict)
        assert result["conditionId"] == "0xabc123"

    @patch("pm.venue.polymarket_client._get")
    def test_empty_response_returns_empty_dict(self, mock_get):
        mock_get.return_value = []
        result = get_market("0xnonexistent")
        assert result == {}

    @patch("pm.venue.polymarket_client._get")
    def test_uses_condition_ids_snake_case(self, mock_get):
        """Regression: the API filter is `condition_ids` (plural, snake_case).
        The similar `conditionId` (camelCase) is silently ignored. Without this
        being correct, every call returns the same default first market."""
        mock_get.return_value = [MOCK_MARKET_DETAIL]
        get_market("0xabc123")
        # Inspect the call to ensure the right param name was used
        call = mock_get.call_args
        # _get is called as _get(url, params={...})
        args, kwargs = call
        params = kwargs.get("params") or (args[1] if len(args) > 1 else {})
        assert "condition_ids" in params, (
            f"Expected 'condition_ids' filter, got: {list(params.keys())}"
        )
        assert params["condition_ids"] == "0xabc123"

    @patch("pm.venue.polymarket_client._get")
    def test_rejects_mismatched_cid(self, mock_get):
        """If the API returns a market with a different conditionId than we
        asked for, we return {} rather than silently mislead callers."""
        wrong = {**MOCK_MARKET_DETAIL, "conditionId": "0xWRONG"}
        mock_get.return_value = [wrong]
        result = get_market("0xabc123")
        assert result == {}


class TestGetPriceHistory:
    @patch("pm.venue.polymarket_client._get")
    def test_returns_dataframe(self, mock_get):
        mock_get.return_value = {
            "history": [
                {"t": 1746835206, "p": 0.40},
                {"t": 1746838806, "p": 0.42},
                {"t": 1746842406, "p": 0.41},
            ]
        }
        df = get_price_history("98022490269692409998", days=7, fidelity=60)
        assert len(df) == 3
        assert list(df.columns) == ["timestamp", "price"]

    @patch("pm.venue.polymarket_client._get")
    def test_prices_are_floats(self, mock_get):
        mock_get.return_value = {"history": [{"t": 1746835206, "p": 0.42}]}
        df = get_price_history("token123", days=7)
        assert df["price"].dtype == float

    @patch("pm.venue.polymarket_client._get")
    def test_sorted_by_timestamp(self, mock_get):
        mock_get.return_value = {
            "history": [
                {"t": 1746842406, "p": 0.41},
                {"t": 1746835206, "p": 0.40},   # earlier, listed second
                {"t": 1746838806, "p": 0.42},
            ]
        }
        df = get_price_history("token123", days=7)
        assert df["timestamp"].is_monotonic_increasing

    @patch("pm.venue.polymarket_client._get")
    def test_empty_history_returns_empty_df(self, mock_get):
        mock_get.return_value = {"history": []}
        df = get_price_history("token123", days=7)
        assert df.empty

    @patch("pm.venue.polymarket_client._get")
    def test_http_error_returns_empty_df(self, mock_get):
        import requests
        mock_get.side_effect = requests.HTTPError("400 Bad Request")
        df = get_price_history("bad_token", days=7)
        assert df.empty


# ---------------------------------------------------------------------------
# Retry behavior in _get
# ---------------------------------------------------------------------------

class TestGetRetry:
    @patch("pm.venue.polymarket_client.SESSION.get")
    @patch("pm.venue.polymarket_client.time.sleep")        # don't actually sleep in tests
    def test_retries_on_transient_error(self, mock_sleep, mock_session_get):
        import requests

        from pm.venue.polymarket_client import _get

        # First call fails with ConnectionError, second succeeds
        good_response = MagicMock()
        good_response.json.return_value = {"ok": True}
        good_response.raise_for_status.return_value = None
        mock_session_get.side_effect = [
            requests.ConnectionError("network blip"),
            good_response,
        ]
        result = _get("https://example.com")
        assert result == {"ok": True}
        assert mock_session_get.call_count == 2
        mock_sleep.assert_called_once()

    @patch("pm.venue.polymarket_client.SESSION.get")
    @patch("pm.venue.polymarket_client.time.sleep")
    def test_does_not_retry_http_errors(self, mock_sleep, mock_session_get):
        import requests

        from pm.venue.polymarket_client import _get

        # HTTPError should propagate immediately, no retry
        bad = MagicMock()
        bad.raise_for_status.side_effect = requests.HTTPError("400")
        mock_session_get.return_value = bad
        with pytest.raises(requests.HTTPError):
            _get("https://example.com")
        assert mock_session_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("pm.venue.polymarket_client.SESSION.get")
    @patch("pm.venue.polymarket_client.time.sleep")
    def test_gives_up_after_max_retries(self, mock_sleep, mock_session_get):
        import requests

        from pm.venue.polymarket_client import _get
        mock_session_get.side_effect = requests.ConnectionError("always down")
        with pytest.raises(requests.ConnectionError):
            _get("https://example.com", retries=3)
        assert mock_session_get.call_count == 3

    @patch("pm.venue.polymarket_client.SESSION.get")
    @patch("pm.venue.polymarket_client.time.sleep")
    def test_retries_on_429_respecting_retry_after(self, mock_sleep, mock_session_get):
        """429 should NOT raise immediately — sleep per Retry-After then retry."""
        from pm.venue.polymarket_client import _get

        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "2"}

        ok = MagicMock()
        ok.status_code = 200
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"ok": True}

        mock_session_get.side_effect = [rate_limited, ok]
        assert _get("https://example.com") == {"ok": True}
        assert mock_session_get.call_count == 2
        mock_sleep.assert_called_once_with(pytest.approx(2.0))

    @patch("pm.venue.polymarket_client.SESSION.get")
    @patch("pm.venue.polymarket_client.time.sleep")
    def test_429_without_retry_after_uses_default(self, mock_sleep, mock_session_get):
        from pm.venue.polymarket_client import _get
        rl = MagicMock()
        rl.status_code = 429
        rl.headers = {}     # no Retry-After

        ok = MagicMock()
        ok.status_code = 200
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"ok": True}

        mock_session_get.side_effect = [rl, ok]
        _get("https://example.com")
        slept_for = mock_sleep.call_args[0][0]
        assert slept_for == pytest.approx(1.0)

    @patch("pm.venue.polymarket_client.SESSION.get")
    @patch("pm.venue.polymarket_client.time.sleep")
    def test_429_clamps_retry_after_at_30s(self, mock_sleep, mock_session_get):
        """A server returning Retry-After: 9999 shouldn't hang the UI."""
        import requests

        from pm.venue.polymarket_client import _get

        rl = MagicMock()
        rl.status_code = 429
        rl.headers = {"Retry-After": "9999"}
        rl.raise_for_status.side_effect = requests.HTTPError("429")

        mock_session_get.return_value = rl
        with pytest.raises(requests.HTTPError):
            _get("https://example.com", retries=2)

        # Every sleep was capped at 30s
        for call in mock_sleep.call_args_list:
            assert call[0][0] <= 30.0

    @patch("pm.venue.polymarket_client.SESSION.get")
    @patch("pm.venue.polymarket_client.time.sleep")
    def test_429_eventually_raises_after_retries_exhausted(self, mock_sleep, mock_session_get):
        import requests

        from pm.venue.polymarket_client import _get

        rl = MagicMock()
        rl.status_code = 429
        rl.headers = {"Retry-After": "0.1"}
        rl.raise_for_status.side_effect = requests.HTTPError("429")

        mock_session_get.return_value = rl
        with pytest.raises(requests.HTTPError):
            _get("https://example.com", retries=3)
        assert mock_session_get.call_count == 3


# ---------------------------------------------------------------------------
# Events + category enrichment
# ---------------------------------------------------------------------------

class TestCategoryFromEventTags:
    def test_first_matching_tag_wins(self):
        assert _category_from_event_tags(
            [{"label": "Soccer"}, {"label": "Sports"}]
        ) == "Sports"

    def test_crypto_tag(self):
        assert _category_from_event_tags([{"label": "Bitcoin"}]) == "Crypto"

    def test_case_insensitive(self):
        assert _category_from_event_tags([{"label": "BITCOIN"}]) == "Crypto"

    def test_json_string_input(self):
        assert _category_from_event_tags('[{"label": "Sports"}]') == "Sports"

    def test_no_match_returns_none(self):
        assert _category_from_event_tags([{"label": "Random thing"}]) is None

    def test_none_input(self):
        assert _category_from_event_tags(None) is None

    def test_malformed_input(self):
        assert _category_from_event_tags("not json") is None
        assert _category_from_event_tags(42) is None

    def test_skips_non_dict_entries(self):
        assert _category_from_event_tags(["string", {"label": "Crypto"}]) == "Crypto"


MOCK_EVENT = {
    "id": "30615",
    "title": "2026 FIFA World Cup Winner",
    "slug": "fifa-world-cup-2026",
    "tags": '[{"label": "Soccer"}, {"label": "Sports"}]',
    "volume24hr": "150000",
    "active": True,
}


class TestGetEvents:
    @patch("pm.venue.polymarket_client._get")
    def test_returns_dataframe(self, mock_get):
        mock_get.return_value = [MOCK_EVENT]
        df = get_events(limit=1)
        assert len(df) == 1
        assert df.iloc[0]["category"] == "Sports"

    @patch("pm.venue.polymarket_client._get")
    def test_empty_returns_empty_with_columns(self, mock_get):
        mock_get.return_value = []
        df = get_events(limit=10)
        for col in ["id", "title", "category"]:
            assert col in df.columns

    @patch("pm.venue.polymarket_client._get")
    def test_paginates(self, mock_get):
        mock_get.side_effect = [
            [MOCK_EVENT] * 100,
            [MOCK_EVENT] * 50,
        ]
        df = get_events(limit=150)
        assert len(df) == 150

    @patch("pm.venue.polymarket_client._get")
    def test_category_empty_when_no_tag_match(self, mock_get):
        bad = {**MOCK_EVENT, "tags": '[{"label": "Hide From New"}]'}
        mock_get.return_value = [bad]
        df = get_events(limit=1)
        assert df.iloc[0]["category"] == ""


class TestEnrichCategoriesFromEvents:
    def test_override_from_event(self):
        markets = pd.DataFrame([
            {"event_id": "1", "category": "Other", "question": "Q"},
        ])
        events = pd.DataFrame([
            {"id": "1", "title": "E", "category": "Sports"},
        ])
        out = enrich_categories_from_events(markets, events)
        assert out.iloc[0]["category"] == "Sports"
        assert out.iloc[0]["event_title"] == "E"

    def test_no_event_match_keeps_keyword_category(self):
        markets = pd.DataFrame([
            {"event_id": "99", "category": "Crypto", "question": "Q"},
        ])
        events = pd.DataFrame([
            {"id": "1", "title": "E", "category": "Sports"},
        ])
        out = enrich_categories_from_events(markets, events)
        # event_id 99 not in events → keep "Crypto" from keyword inference
        assert out.iloc[0]["category"] == "Crypto"

    def test_event_with_empty_category_keeps_keyword(self):
        markets = pd.DataFrame([
            {"event_id": "1", "category": "Crypto", "question": "Q"},
        ])
        events = pd.DataFrame([
            {"id": "1", "title": "E", "category": ""},
        ])
        out = enrich_categories_from_events(markets, events)
        # event found but no category → keep keyword
        assert out.iloc[0]["category"] == "Crypto"

    def test_empty_event_id_keeps_keyword(self):
        markets = pd.DataFrame([
            {"event_id": "", "category": "Sports", "question": "Q"},
        ])
        events = pd.DataFrame([
            {"id": "1", "title": "E", "category": "Crypto"},
        ])
        out = enrich_categories_from_events(markets, events)
        assert out.iloc[0]["category"] == "Sports"

    def test_empty_markets_returns_unchanged(self):
        empty = pd.DataFrame(columns=["event_id", "category"])
        events = pd.DataFrame([{"id": "1", "title": "E", "category": "Sports"}])
        out = enrich_categories_from_events(empty, events)
        assert out.empty

    def test_empty_events_adds_event_title_column(self):
        markets = pd.DataFrame([
            {"event_id": "1", "category": "Crypto", "question": "Q"},
        ])
        out = enrich_categories_from_events(markets, pd.DataFrame())
        assert "event_title" in out.columns
        assert out.iloc[0]["category"] == "Crypto"  # unchanged

    def test_get_markets_now_includes_event_id(self):
        """Regression guard: enrichment depends on get_markets producing event_id."""
        with patch("pm.venue.polymarket_client._get") as mock_get:
            mock_get.return_value = [{
                **MOCK_MARKET,
                "events": [{"id": "42", "ticker": "x", "slug": "y"}],
            }]
            df = get_markets(limit=1)
            assert df.iloc[0]["event_id"] == "42"


# ---------------------------------------------------------------------------
# Order book parsing
# ---------------------------------------------------------------------------

# Real-shape sample: bids ASC, asks DESC, like the live API
_RAW_BOOK = {
    "bids": [
        {"price": "0.40", "size": "100"},
        {"price": "0.42", "size": "75"},
        {"price": "0.45", "size": "50"},   # best (highest) bid at END
    ],
    "asks": [
        {"price": "0.60", "size": "200"},
        {"price": "0.57", "size": "80"},
        {"price": "0.55", "size": "30"},   # best (lowest) ask at END
    ],
}


class TestParseOrderBook:
    def test_bid_side_sorted_best_first(self):
        df = parse_order_book(_RAW_BOOK)
        bids = df[df["side"] == "bid"]
        # Best bid (highest) first
        assert bids.iloc[0]["price"] == pytest.approx(0.45)
        assert bids.iloc[-1]["price"] == pytest.approx(0.40)

    def test_ask_side_sorted_best_first(self):
        df = parse_order_book(_RAW_BOOK)
        asks = df[df["side"] == "ask"]
        # Best ask (lowest) first
        assert asks.iloc[0]["price"] == pytest.approx(0.55)
        assert asks.iloc[-1]["price"] == pytest.approx(0.60)

    def test_cumulative_size_computed_from_best_outward(self):
        df = parse_order_book(_RAW_BOOK)
        bids = df[df["side"] == "bid"].reset_index(drop=True)
        assert bids.iloc[0]["cumulative_size"] == pytest.approx(50)
        assert bids.iloc[1]["cumulative_size"] == pytest.approx(125)
        assert bids.iloc[2]["cumulative_size"] == pytest.approx(225)

        asks = df[df["side"] == "ask"].reset_index(drop=True)
        assert asks.iloc[0]["cumulative_size"] == pytest.approx(30)
        assert asks.iloc[1]["cumulative_size"] == pytest.approx(110)
        assert asks.iloc[2]["cumulative_size"] == pytest.approx(310)

    def test_empty_book(self):
        df = parse_order_book({"bids": [], "asks": []})
        assert df.empty
        for col in ["side", "price", "size", "cumulative_size"]:
            assert col in df.columns

    def test_only_bids_side(self):
        df = parse_order_book({"bids": [{"price": "0.5", "size": "10"}], "asks": []})
        assert (df["side"] == "bid").all()

    def test_only_asks_side(self):
        df = parse_order_book({"bids": [], "asks": [{"price": "0.5", "size": "10"}]})
        assert (df["side"] == "ask").all()

    def test_malformed_level_skipped(self):
        # One bad row, one good row — the bad one is silently dropped
        df = parse_order_book({
            "bids": [{"price": "not a number", "size": "10"},
                     {"price": "0.5", "size": "100"}],
            "asks": [],
        })
        bids = df[df["side"] == "bid"]
        assert len(bids) == 1
        assert bids.iloc[0]["price"] == pytest.approx(0.5)

    def test_non_dict_input(self):
        assert parse_order_book(None).empty
        assert parse_order_book("string").empty
        assert parse_order_book([]).empty


class TestOrderBookStats:
    def test_empty_book(self):
        s = order_book_stats(pd.DataFrame())
        assert s["best_bid"] is None
        assert s["best_ask"] is None
        assert s["spread"] is None
        assert s["total_bid_size"] == 0.0

    def test_full_book(self):
        df = parse_order_book(_RAW_BOOK)
        s = order_book_stats(df)
        assert s["best_bid"] == pytest.approx(0.45)
        assert s["best_ask"] == pytest.approx(0.55)
        assert s["spread"] == pytest.approx(0.10)
        assert s["total_bid_size"] == pytest.approx(225)
        assert s["total_ask_size"] == pytest.approx(310)
        assert s["bid_levels"] == 3
        assert s["ask_levels"] == 3

    def test_one_sided_book(self):
        df = parse_order_book({"bids": [{"price": "0.5", "size": "10"}], "asks": []})
        s = order_book_stats(df)
        assert s["best_bid"] == pytest.approx(0.5)
        assert s["best_ask"] is None
        assert s["spread"] is None


class TestGetOrderBook:
    @patch("pm.venue.polymarket_client._get")
    def test_returns_raw_response(self, mock_get):
        mock_get.return_value = _RAW_BOOK
        result = get_order_book("token_123")
        assert result == _RAW_BOOK

    @patch("pm.venue.polymarket_client._get")
    def test_passes_token_id_param(self, mock_get):
        mock_get.return_value = _RAW_BOOK
        get_order_book("token_abc")
        call = mock_get.call_args
        args, kwargs = call
        params = kwargs.get("params") or (args[1] if len(args) > 1 else {})
        assert params.get("token_id") == "token_abc"


# ---------------------------------------------------------------------------
# Cross-market arbitrage scanner (Tier 2)
# ---------------------------------------------------------------------------

def _arb_market(event_id, question, yes_price, liquidity=5000, category="Sports",
                event_title="Test Event"):
    """Build one market row for cross-market arb tests."""
    return {
        "event_id": event_id,
        "event_title": event_title,
        "question": question,
        "category": category,
        "outcomes": ["Yes", "No"],
        "outcome_prices": [yes_price, 1 - yes_price],
        "liquidity": liquidity,
        "volume_24hr": liquidity * 0.1,
    }


class TestScanCrossMarketArb:
    def test_empty_input(self):
        assert scan_cross_market_arb(pd.DataFrame()).empty

    def test_no_event_id_column(self):
        df = pd.DataFrame([{"question": "Q", "outcome_prices": [0.5, 0.5]}])
        assert scan_cross_market_arb(df).empty

    def test_perfect_sum_excluded(self):
        """Event whose YES prices sum to exactly 1.0 — no arb, no row."""
        df = pd.DataFrame([
            _arb_market("E1", "A wins", 0.40),
            _arb_market("E1", "B wins", 0.35),
            _arb_market("E1", "C wins", 0.25),
        ])
        assert scan_cross_market_arb(df, min_deviation=0.01).empty

    def test_underpriced_event_flagged(self):
        """Three sub-markets summing to 0.90 → underpriced; implied edge ≈ 11.1%."""
        df = pd.DataFrame([
            _arb_market("E1", "A wins", 0.40),
            _arb_market("E1", "B wins", 0.30),
            _arb_market("E1", "C wins", 0.20),
        ])
        result = scan_cross_market_arb(df, min_markets=3, min_total_liquidity=0)
        assert len(result) == 1
        row = result.iloc[0]
        assert row["n_markets"] == 3
        assert row["sum_yes_prices"] == pytest.approx(0.9)
        assert row["deviation"] == pytest.approx(-0.1)
        # implied_edge = (1 - 0.9) / 0.9 ≈ 0.1111
        assert row["implied_edge_pct"] == pytest.approx(0.1111, rel=1e-3)
        assert row["event_title"] == "Test Event"

    def test_overpriced_event_flagged(self):
        df = pd.DataFrame([
            _arb_market("E1", "A wins", 0.50),
            _arb_market("E1", "B wins", 0.40),
            _arb_market("E1", "C wins", 0.20),   # sum 1.10
        ])
        result = scan_cross_market_arb(df, min_markets=3, min_total_liquidity=0)
        assert len(result) == 1
        assert result.iloc[0]["sum_yes_prices"] == pytest.approx(1.10)
        # implied_edge_pct for sum > 1 is negative
        assert result.iloc[0]["implied_edge_pct"] < 0

    def test_min_markets_filter(self):
        """Events below min_markets are skipped — small events are noise."""
        df = pd.DataFrame([
            _arb_market("E1", "A wins", 0.45),
            _arb_market("E1", "B wins", 0.45),   # 2 markets, sum 0.90
        ])
        assert scan_cross_market_arb(df, min_markets=3).empty

    def test_min_liquidity_filter(self):
        df = pd.DataFrame([
            _arb_market("E1", "A wins", 0.30, liquidity=100),
            _arb_market("E1", "B wins", 0.30, liquidity=100),
            _arb_market("E1", "C wins", 0.20, liquidity=100),
        ])
        # Total liquidity 300 < 1000 → filtered out
        assert scan_cross_market_arb(df, min_markets=3, min_total_liquidity=1000).empty

    def test_plausible_band_excludes_non_exclusive_events(self):
        """Sum way outside the band → clearly not a mutually-exclusive event."""
        df = pd.DataFrame([
            _arb_market("E1", "Will inflation > 5%?", 0.30),
            _arb_market("E1", "Will unemployment > 4%?", 0.45),
            _arb_market("E1", "Will the Fed cut rates?", 0.55),
            # Sum 1.30 — but these aren't exclusive! With plausible_band
            # tight enough, this gets filtered.
        ])
        result = scan_cross_market_arb(
            df, min_markets=3, min_total_liquidity=0,
            plausible_band=(0.9, 1.1),
        )
        assert result.empty

    def test_skips_markets_without_event_id(self):
        df = pd.DataFrame([
            _arb_market("", "Standalone A", 0.40),
            _arb_market("", "Standalone B", 0.30),
            _arb_market("", "Standalone C", 0.20),
        ])
        assert scan_cross_market_arb(df, min_markets=3).empty

    def test_min_deviation_filter(self):
        """Sum exactly at the edge of min_deviation should be included; just below should not."""
        df = pd.DataFrame([
            _arb_market("E1", "A", 0.33),
            _arb_market("E1", "B", 0.33),
            _arb_market("E1", "C", 0.33),    # sum 0.99 → deviation -0.01
        ])
        # min_deviation 0.02 → 0.01 is below, filtered out
        assert scan_cross_market_arb(df, min_markets=3, min_total_liquidity=0,
                                      min_deviation=0.02).empty
        # min_deviation 0.005 → 0.01 is above, included
        result = scan_cross_market_arb(df, min_markets=3, min_total_liquidity=0,
                                       min_deviation=0.005)
        assert len(result) == 1

    def test_two_events_sorted_by_abs_deviation(self):
        df = pd.DataFrame([
            # Event 1: sum 0.92 (deviation -0.08)
            _arb_market("E1", "A1", 0.45, event_title="Event 1"),
            _arb_market("E1", "A2", 0.30, event_title="Event 1"),
            _arb_market("E1", "A3", 0.17, event_title="Event 1"),
            # Event 2: sum 1.20 (deviation +0.20)
            _arb_market("E2", "B1", 0.50, event_title="Event 2"),
            _arb_market("E2", "B2", 0.40, event_title="Event 2"),
            _arb_market("E2", "B3", 0.30, event_title="Event 2"),
        ])
        result = scan_cross_market_arb(df, min_markets=3, min_total_liquidity=0)
        assert len(result) == 2
        # Larger absolute deviation first
        assert result.iloc[0]["event_title"] == "Event 2"
        assert result.iloc[1]["event_title"] == "Event 1"

    def test_markets_sample_truncated_and_listed(self):
        df = pd.DataFrame([
            _arb_market("E1", "Will France win the 2026 FIFA World Cup", 0.40),
            _arb_market("E1", "Will Brazil win the 2026 FIFA World Cup", 0.30),
            _arb_market("E1", "Will Argentina win the 2026 FIFA World Cup", 0.20),
        ])
        result = scan_cross_market_arb(df, min_markets=3, min_total_liquidity=0)
        sample = result.iloc[0]["markets_sample"]
        assert "France" in sample
        assert "0.400" in sample
        # Long questions get truncated
        assert all(len(s) <= 50 for s in sample.split(", "))

    def test_missing_yes_price_skipped(self):
        """A row with None for outcome_prices[0] should be skipped, not crash."""
        df = pd.DataFrame([
            {"event_id": "E1", "question": "A", "outcomes": ["Yes", "No"],
             "outcome_prices": [None, None], "liquidity": 1000,
             "volume_24hr": 100, "category": "Sports", "event_title": "T"},
            _arb_market("E1", "B", 0.40),
            _arb_market("E1", "C", 0.30),
            _arb_market("E1", "D", 0.20),
        ])
        result = scan_cross_market_arb(df, min_markets=3, min_total_liquidity=0)
        # The bad row is dropped; remaining 3 markets sum to 0.90
        assert len(result) == 1
        assert result.iloc[0]["n_markets"] == 3
        assert result.iloc[0]["sum_yes_prices"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Closed markets + market calibration (Tier 3a)
# ---------------------------------------------------------------------------

MOCK_CLOSED_YES_WON = {
    "id": "100",
    "conditionId": "0xclose1",
    "question": "Did Alice win?",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["1", "0"]',
    "clobTokenIds": '["77777", "88888"]',
    "volume": "5000",
    "volume24hr": "0",
    "liquidity": "0",
    "endDate": "2026-05-10T00:00:00Z",
    "closedTime": "2026-05-10T15:00:00Z",
    "active": False,
    "spread": "0",
    "bestBid": "0",
    "bestAsk": "1",
    "lastTradePrice": "1",
    "events": [],
}

MOCK_CLOSED_NO_WON = {
    **MOCK_CLOSED_YES_WON,
    "id": "101",
    "conditionId": "0xclose2",
    "question": "Did Bob win?",
    "outcomePrices": '["0", "1"]',
    "clobTokenIds": '["99999", "00000"]',
    "closedTime": "2026-05-11T15:00:00Z",
}

MOCK_CLOSED_AMBIGUOUS = {
    **MOCK_CLOSED_YES_WON,
    "id": "102",
    "conditionId": "0xclose3",
    "question": "Ambiguous?",
    "outcomePrices": '["0.5", "0.5"]',   # not a clean resolution
}


class TestGetClosedMarkets:
    @patch("pm.venue.polymarket_client._get")
    def test_returns_dataframe_with_resolved_yes(self, mock_get):
        mock_get.return_value = [MOCK_CLOSED_YES_WON, MOCK_CLOSED_NO_WON]
        df = get_closed_markets(limit=10)
        assert len(df) == 2
        # bool() coerces numpy bool to plain bool for `is` comparison
        assert bool(df.iloc[0]["resolved_yes"]) is True
        assert bool(df.iloc[1]["resolved_yes"]) is False

    @patch("pm.venue.polymarket_client._get")
    def test_ambiguous_resolution_is_none(self, mock_get):
        """Markets that didn't resolve to clean 0/1 outcomes get
        resolved_yes=None and are excluded downstream."""
        mock_get.return_value = [MOCK_CLOSED_AMBIGUOUS]
        df = get_closed_markets(limit=10)
        assert df.iloc[0]["resolved_yes"] is None

    @patch("pm.venue.polymarket_client._get")
    def test_includes_closed_time_parsed(self, mock_get):
        mock_get.return_value = [MOCK_CLOSED_YES_WON]
        df = get_closed_markets(limit=10)
        assert "closed_time_dt" in df.columns
        assert pd.notna(df.iloc[0]["closed_time_dt"])

    @patch("pm.venue.polymarket_client._get")
    def test_passes_closed_true_to_api(self, mock_get):
        mock_get.return_value = []
        get_closed_markets(limit=10)
        call = mock_get.call_args
        args, kwargs = call
        params = kwargs.get("params") or (args[1] if len(args) > 1 else {})
        assert params["closed"] == "true"
        assert params["active"] == "false"

    @patch("pm.venue.polymarket_client._get")
    def test_empty_response(self, mock_get):
        mock_get.return_value = []
        df = get_closed_markets(limit=10)
        assert df.empty


class TestGetMarketCalibrationData:
    @patch("pm.venue.polymarket_client._get")
    def test_well_calibrated_market(self, mock_get):
        """If the YES price 24h before close was 0.8 and YES won (resolved_yes=True),
        the brier_term is (0.8 - 1.0)^2 = 0.04."""
        df_closed = pd.DataFrame([{
            "condition_id": "0xc1",
            "question": "Q",
            "category": "Sports",
            "closed_time_dt": pd.Timestamp("2026-05-10T15:00:00", tz="UTC"),
            "resolved_yes": True,
        }])
        # _get is called twice per market: once for get_market (detail) and
        # once for get_price_history. Sequence the mock accordingly.
        mock_get.side_effect = [
            # get_market returns market detail with clobTokenIds
            [{"conditionId": "0xc1",
              "clobTokenIds": '["77777"]'}],
            # get_price_history returns one candle at T-24h with price 0.8
            {"history": [
                {"t": int(pd.Timestamp("2026-05-09T15:00:00", tz="UTC").timestamp()),
                 "p": 0.8},
            ]},
        ]
        df = get_market_calibration_data(df_closed, lead_hours=24)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["predicted_prob"] == pytest.approx(0.8)
        assert row["actual"] == 1.0
        assert row["brier_term"] == pytest.approx(0.04)

    @patch("pm.venue.polymarket_client._get")
    def test_market_without_history_skipped(self, mock_get):
        df_closed = pd.DataFrame([{
            "condition_id": "0xc1",
            "question": "Q",
            "category": "Sports",
            "closed_time_dt": pd.Timestamp("2026-05-10T15:00:00", tz="UTC"),
            "resolved_yes": True,
        }])
        mock_get.side_effect = [
            [{"conditionId": "0xc1", "clobTokenIds": '["77777"]'}],
            {"history": []},   # no history available
        ]
        df = get_market_calibration_data(df_closed, lead_hours=24)
        assert df.empty

    @patch("pm.venue.polymarket_client._get")
    def test_markets_without_resolution_skipped(self, mock_get):
        df_closed = pd.DataFrame([{
            "condition_id": "0xambiguous",
            "question": "Q",
            "category": "Sports",
            "closed_time_dt": pd.Timestamp("2026-05-10T15:00:00", tz="UTC"),
            "resolved_yes": None,
        }])
        # _get should never be called — the row is filtered before fetching
        df = get_market_calibration_data(df_closed, lead_hours=24)
        assert df.empty
        mock_get.assert_not_called()

    @patch("pm.venue.polymarket_client._get")
    def test_progress_callback_invoked(self, mock_get):
        df_closed = pd.DataFrame([{
            "condition_id": "0xc1",
            "question": "Q", "category": "Sports",
            "closed_time_dt": pd.Timestamp("2026-05-10T15:00:00", tz="UTC"),
            "resolved_yes": True,
        }])
        mock_get.side_effect = [
            [{"conditionId": "0xc1", "clobTokenIds": '["77777"]'}],
            {"history": [{"t": int(pd.Timestamp("2026-05-09T15:00:00", tz="UTC").timestamp()),
                          "p": 0.5}]},
        ]
        calls = []
        def cb(i, total):
            calls.append((i, total))
        get_market_calibration_data(df_closed, lead_hours=24, progress_callback=cb)
        # At minimum called once at start and once at end
        assert len(calls) >= 2

    @patch("pm.venue.polymarket_client._get")
    def test_empty_input(self, mock_get):
        assert get_market_calibration_data(pd.DataFrame()).empty
