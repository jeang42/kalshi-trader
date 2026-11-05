"""Tests for the Kalshi provider — mocked HTTP responses, no network."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pm.venue import kalshi_client as kc
from pm.venue import polymarket_client as pm
from pm.venue import provider as mp

# ---------------------------------------------------------------------------
# Sample raw Kalshi responses (shape verified against the live API)
# ---------------------------------------------------------------------------

MOCK_EVENT = {
    "event_ticker": "KXWARMING-50",
    "series_ticker": "KXWARMING",
    "title": "Will the world pass 2°C before 2050?",
    "category": "Climate and Weather",
    "markets": [
        {
            "ticker": "KXWARMING-50",
            "event_ticker": "KXWARMING-50",
            "title": "Will the world pass 2°C before 2050?",
            "status": "active",
            "close_time": "2050-01-01T04:59:00Z",
            "yes_bid_dollars": "0.7600",
            "yes_ask_dollars": "0.8100",
            "no_bid_dollars": "0.1900",
            "no_ask_dollars": "0.2400",
            "last_price_dollars": "0.7800",
            "volume_fp": 13347.68,
            "volume_24h_fp": 0.0,
            "liquidity_dollars": "0.0000",
        },
    ],
}

MOCK_EVENT_TWO_MARKETS = {
    "event_ticker": "KXNEWPOPE-70",
    "series_ticker": "KXNEWPOPE",
    "title": "Who will the next Pope be?",
    "category": "Elections",
    "markets": [
        {
            "ticker": "KXNEWPOPE-70-PPIZ",
            "event_ticker": "KXNEWPOPE-70",
            "title": "Pope Pius",
            "status": "active",
            "close_time": "2030-01-01T00:00:00Z",
            "yes_bid_dollars": "0.0340",
            "yes_ask_dollars": "0.0500",
            "no_bid_dollars": "0.9400",
            "no_ask_dollars": "0.9600",
            "last_price_dollars": "0.0400",
            "volume_fp": 1500.0,
            "volume_24h_fp": 100.0,
            "liquidity_dollars": "200.0",
        },
        {
            "ticker": "KXNEWPOPE-70-XXX",
            "event_ticker": "KXNEWPOPE-70",
            "title": "Other candidate",
            "status": "active",
            "close_time": "2030-01-01T00:00:00Z",
            "yes_bid_dollars": "0.0100",
            "yes_ask_dollars": "0.0150",
            "no_bid_dollars": "0.9800",
            "no_ask_dollars": "0.9900",
            "last_price_dollars": "0.0100",
            "volume_fp": 500.0,
            "volume_24h_fp": 50.0,
            "liquidity_dollars": "100.0",
        },
    ],
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_series_from_ticker(self):
        assert kc._series_from_ticker("KXWARMING-50") == "KXWARMING"
        assert kc._series_from_ticker("KXNEWPOPE-70-PPIZ") == "KXNEWPOPE"
        assert kc._series_from_ticker("") == ""
        assert kc._series_from_ticker("NODASH") == "NODASH"

    def test_to_float(self):
        assert kc._to_float("0.7800") == pytest.approx(0.78)
        assert kc._to_float(0.5) == 0.5
        assert kc._to_float(None) == 0.0
        assert kc._to_float("") == 0.0
        assert kc._to_float("not a number") == 0.0
        assert kc._to_float("not a number", default=-1.0) == -1.0

    def test_compose_question_binary_market(self):
        # No sub-title → use title as-is
        assert kc._compose_question(
            {"title": "Will Trump win?", "yes_sub_title": ""}
        ) == "Will Trump win?"

    def test_compose_question_multi_outcome_event(self):
        # Sub-market under "Who will the next Pope be?" with the
        # specific candidate in yes_sub_title — must compose to show
        # what YES means for THIS sub-market.
        assert kc._compose_question(
            {"title": "Who will the next Pope be?",
             "yes_sub_title": "Pierbattista Pizzaballa"}
        ) == "Who will the next Pope be? — Pierbattista Pizzaballa"

    def test_compose_question_strips_boilerplate_subtitles(self):
        # "yes" / "no" are useless to display
        assert kc._compose_question(
            {"title": "Will it rain?", "yes_sub_title": "yes"}
        ) == "Will it rain?"
        # Duplicate of title is also useless
        assert kc._compose_question(
            {"title": "Will it rain?", "yes_sub_title": "Will it rain?"}
        ) == "Will it rain?"

    def test_closest_period_interval(self):
        assert kc._closest_period_interval(1) == 1
        assert kc._closest_period_interval(60) == 60
        assert kc._closest_period_interval(15) == 60      # rounds up to hourly
        assert kc._closest_period_interval(1440) == 1440
        assert kc._closest_period_interval(720) == 1440   # ≥ 12h → daily

    def test_market_to_row_normalises_prices(self):
        row = kc._market_to_row(MOCK_EVENT["markets"][0],
                                 event_category="Climate and Weather",
                                 event_title=MOCK_EVENT["title"])
        # Mid is (0.76 + 0.81) / 2 = 0.785
        assert row["outcome_prices"][0] == pytest.approx(0.785)
        assert row["outcome_prices"][1] == pytest.approx(1 - 0.785)
        # Bid/ask passed through
        assert row["best_bid"] == pytest.approx(0.76)
        assert row["best_ask"] == pytest.approx(0.81)
        # Spread = ask - bid
        assert row["spread"] == pytest.approx(0.05)
        # Identifier maps to ticker
        assert row["id"] == "KXWARMING-50"
        assert row["condition_id"] == "KXWARMING-50"
        # Event info propagated
        assert row["event_id"] == "KXWARMING-50"
        assert row["category"] == "Climate and Weather"
        # Outcomes always Yes/No for Kalshi binary
        assert row["outcomes"] == ["Yes", "No"]

    def test_market_to_row_degenerate_book_falls_back_to_last(self):
        """When yes_bid or yes_ask is 0, mid should fall back to last_price."""
        m = {
            "ticker": "T1",
            "event_ticker": "E1",
            "title": "Q",
            "status": "active",
            "yes_bid_dollars": "0",
            "yes_ask_dollars": "0",
            "no_bid_dollars": "0",
            "no_ask_dollars": "0",
            "last_price_dollars": "0.4200",
            "volume_fp": 0,
            "volume_24h_fp": 0,
            "liquidity_dollars": "0",
        }
        row = kc._market_to_row(m)
        assert row["outcome_prices"][0] == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class TestPagination:
    @patch.object(kc, "_get")
    def test_single_page(self, mock_get):
        mock_get.return_value = {"events": [{"a": 1}, {"a": 2}], "cursor": ""}
        result = kc._paginate("events", {"status": "open"}, "events", max_items=10)
        assert len(result) == 2

    @patch.object(kc, "_get")
    def test_multiple_pages_until_cursor_exhausted(self, mock_get):
        mock_get.side_effect = [
            {"events": [{"a": 1}, {"a": 2}], "cursor": "abc"},
            {"events": [{"a": 3}, {"a": 4}], "cursor": "def"},
            {"events": [{"a": 5}], "cursor": ""},
        ]
        result = kc._paginate("events", None, "events", max_items=100)
        assert len(result) == 5

    @patch.object(kc, "_get")
    def test_stops_at_max_items(self, mock_get):
        mock_get.side_effect = [
            {"events": [{"a": i} for i in range(50)], "cursor": "abc"},
            {"events": [{"a": i} for i in range(50)], "cursor": "def"},
        ]
        result = kc._paginate("events", None, "events", max_items=30)
        assert len(result) == 30

    @patch.object(kc, "_get")
    def test_empty_page_breaks(self, mock_get):
        mock_get.return_value = {"events": [], "cursor": "still-here"}
        result = kc._paginate("events", None, "events", max_items=10)
        assert result == []


# ---------------------------------------------------------------------------
# get_markets — full integration through the pagination layer
# ---------------------------------------------------------------------------

class TestGetMarkets:
    @patch.object(kc, "_get")
    def test_returns_normalised_dataframe(self, mock_get):
        mock_get.return_value = {
            "events": [MOCK_EVENT, MOCK_EVENT_TWO_MARKETS],
            "cursor": "",
        }
        df = kc.get_markets(limit=10)
        # 1 market from KXWARMING + 2 from KXNEWPOPE
        assert len(df) == 3
        # Schema columns we depend on downstream
        for col in ["id", "condition_id", "question", "category", "event_id",
                    "volume", "volume_24hr", "liquidity", "outcomes",
                    "outcome_prices", "spread", "best_bid", "best_ask",
                    "last_trade_price", "days_to_resolution"]:
            assert col in df.columns, f"missing column {col}"

    @patch.object(kc, "_get")
    def test_categories_inherited_from_event(self, mock_get):
        mock_get.return_value = {
            "events": [MOCK_EVENT, MOCK_EVENT_TWO_MARKETS],
            "cursor": "",
        }
        df = kc.get_markets(limit=10)
        # Both KXNEWPOPE markets should have Elections; KXWARMING gets Climate and Weather
        cats = set(df.loc[df["event_id"] == "KXNEWPOPE-70", "category"])
        assert cats == {"Elections"}
        warming = df.loc[df["event_id"] == "KXWARMING-50", "category"].iloc[0]
        assert warming == "Climate and Weather"

    @patch.object(kc, "_get")
    def test_sorted_by_volume_24hr_desc(self, mock_get):
        mock_get.return_value = {
            "events": [MOCK_EVENT, MOCK_EVENT_TWO_MARKETS],
            "cursor": "",
        }
        df = kc.get_markets(limit=10)
        vols = df["volume_24hr"].tolist()
        assert vols == sorted(vols, reverse=True)

    @patch.object(kc, "_get")
    def test_empty(self, mock_get):
        mock_get.return_value = {"events": [], "cursor": ""}
        df = kc.get_markets(limit=10)
        assert df.empty


# ---------------------------------------------------------------------------
# get_closed_markets — adds `resolved_yes` from `result` field
# ---------------------------------------------------------------------------

class TestGetClosedMarkets:
    @patch.object(kc, "_get")
    def test_extracts_resolved_yes_from_result(self, mock_get):
        closed_event = {
            "event_ticker": "KXC-1",
            "series_ticker": "KXC",
            "title": "Closed event",
            "category": "Sports",
            "markets": [
                {**MOCK_EVENT["markets"][0], "ticker": "KXC-YES", "result": "yes",
                 "status": "finalized"},
                {**MOCK_EVENT["markets"][0], "ticker": "KXC-NO", "result": "no",
                 "status": "finalized"},
                {**MOCK_EVENT["markets"][0], "ticker": "KXC-UNRESOLVED", "result": "",
                 "status": "finalized"},
            ],
        }
        mock_get.return_value = {"events": [closed_event], "cursor": ""}
        df = kc.get_closed_markets(limit=10)
        assert len(df) == 3
        resolved = dict(zip(df["condition_id"], df["resolved_yes"], strict=False))
        # bool() coerces numpy bool / None to plain Python for the comparison
        assert bool(resolved["KXC-YES"]) is True
        assert bool(resolved["KXC-NO"]) is False
        assert resolved["KXC-UNRESOLVED"] is None

    @patch.object(kc, "_get")
    def test_closed_time_dt_parsed(self, mock_get):
        mock_get.return_value = {"events": [MOCK_EVENT], "cursor": ""}
        df = kc.get_closed_markets(limit=10)
        # Even from the open MOCK_EVENT, closed_time_dt is computed from close_time
        assert "closed_time_dt" in df.columns


# ---------------------------------------------------------------------------
# get_price_history — candlesticks endpoint
# ---------------------------------------------------------------------------

class TestGetPriceHistory:
    @patch.object(kc, "_get")
    def test_returns_midpoint_timeseries(self, mock_get):
        mock_get.return_value = {
            "candlesticks": [
                {
                    "end_period_ts": 1746000000,
                    "yes_bid": {"close_dollars": "0.50"},
                    "yes_ask": {"close_dollars": "0.60"},
                },
                {
                    "end_period_ts": 1746086400,
                    "yes_bid": {"close_dollars": "0.55"},
                    "yes_ask": {"close_dollars": "0.65"},
                },
            ]
        }
        df = kc.get_price_history("KXWARMING-50", days=2, fidelity=1440)
        assert len(df) == 2
        # First candle mid = (0.50 + 0.60) / 2 = 0.55
        assert df.iloc[0]["price"] == pytest.approx(0.55)
        assert df.iloc[1]["price"] == pytest.approx(0.60)

    @patch.object(kc, "_get")
    def test_empty_candles(self, mock_get):
        mock_get.return_value = {"candlesticks": []}
        df = kc.get_price_history("KXWARMING-50", days=2)
        assert df.empty

    def test_no_ticker_returns_empty(self):
        df = kc.get_price_history("", days=2)
        assert df.empty


# ---------------------------------------------------------------------------
# Order book — Kalshi → Polymarket shape adaptation
# ---------------------------------------------------------------------------

class TestGetOrderBook:
    @patch.object(kc, "_get")
    def test_yes_levels_become_bids(self, mock_get):
        mock_get.return_value = {
            "orderbook_fp": {
                # Each level is [price_str, size]
                "yes_dollars": [["0.50", 100], ["0.55", 50]],
                "no_dollars": [],
            }
        }
        book = kc.get_order_book("T")
        assert len(book["bids"]) == 2
        # Bids sorted ASC (Polymarket convention; parse_order_book reverses)
        assert float(book["bids"][0]["price"]) < float(book["bids"][1]["price"])
        assert book["asks"] == []

    @patch.object(kc, "_get")
    def test_no_levels_become_asks_with_mirrored_prices(self, mock_get):
        """Selling NO @ X is equivalent to buying YES @ (1-X). So NO orderbook
        levels appear as asks on the YES book at price (1 - no_price)."""
        mock_get.return_value = {
            "orderbook_fp": {
                "yes_dollars": [],
                "no_dollars": [["0.40", 200]],   # selling NO @ 0.40
            }
        }
        book = kc.get_order_book("T")
        assert len(book["asks"]) == 1
        # YES ask = 1 - 0.40 = 0.60
        assert float(book["asks"][0]["price"]) == pytest.approx(0.60)

    @patch.object(kc, "_get")
    def test_pipes_through_parse_order_book(self, mock_get):
        """End-to-end: a Kalshi raw response → parse_order_book → stats."""
        mock_get.return_value = {
            "orderbook_fp": {
                "yes_dollars": [["0.50", 100], ["0.55", 50]],
                "no_dollars": [["0.30", 200]],   # → ask at 0.70
            }
        }
        book = kc.get_order_book("T")
        df = pm.parse_order_book(book)
        stats = pm.order_book_stats(df)
        # Best bid is highest YES bid; best ask is lowest YES ask
        assert stats["best_bid"] == pytest.approx(0.55)
        assert stats["best_ask"] == pytest.approx(0.70)
        assert stats["bid_levels"] == 2
        assert stats["ask_levels"] == 1


# ---------------------------------------------------------------------------
# get_midpoint
# ---------------------------------------------------------------------------

class TestGetMidpoint:
    @patch.object(kc, "_get")
    def test_returns_midpoint_when_bid_ask_present(self, mock_get):
        mock_get.return_value = {
            "market": {"yes_bid_dollars": "0.50", "yes_ask_dollars": "0.60"},
        }
        assert kc.get_midpoint("T") == pytest.approx(0.55)

    @patch.object(kc, "_get")
    def test_falls_back_to_last_price(self, mock_get):
        mock_get.return_value = {
            "market": {"yes_bid_dollars": "0", "yes_ask_dollars": "0",
                       "last_price_dollars": "0.4200"},
        }
        assert kc.get_midpoint("T") == pytest.approx(0.42)

    @patch.object(kc, "_get")
    def test_returns_none_when_no_data(self, mock_get):
        mock_get.return_value = {
            "market": {"yes_bid_dollars": "0", "yes_ask_dollars": "0",
                       "last_price_dollars": "0"},
        }
        assert kc.get_midpoint("T") is None


# ---------------------------------------------------------------------------
# Registration into the provider registry
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_kalshi_registered(self):
        assert "kalshi" in mp.list_provider_names()

    def test_kalshi_satisfies_protocol(self):
        provider = mp.get_provider("kalshi")
        assert isinstance(provider, mp.MarketProvider)

    def test_kalshi_has_expected_metadata(self):
        provider = mp.get_provider("kalshi")
        assert provider.name == "kalshi"
        assert provider.display_name == "Kalshi"

    def test_list_outcome_tokens_returns_yes_for_ticker(self):
        # Kalshi binary markets: the ticker is the YES identifier and the
        # NO side is derived by price mirror in the order book / midpoint
        # logic — so exactly one token entry, labelled "Yes".
        tokens = mp.get_provider("kalshi").list_outcome_tokens("KXWARMING-50")
        assert tokens == [{"token_id": "KXWARMING-50", "outcome": "Yes"}]


# ---------------------------------------------------------------------------
# 429 retry behaviour (inherited from the same pattern as polymarket_client)
# ---------------------------------------------------------------------------

class TestKalshi429Backoff:
    @patch("pm.venue.kalshi_client._SESSION.get")
    @patch("pm.venue.kalshi_client.time.sleep")
    def test_retries_on_429(self, mock_sleep, mock_session_get):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "2"}

        ok = MagicMock()
        ok.status_code = 200
        ok.raise_for_status.return_value = None
        ok.content = b'{"ok": true}'
        ok.json.return_value = {"ok": True}

        mock_session_get.side_effect = [rate_limited, ok]
        result = kc._get("events")
        assert result == {"ok": True}
        assert mock_session_get.call_count == 2
        mock_sleep.assert_called_once()


# ---------------------------------------------------------------------------
# get_markets_closing_within — short-dated fetch path
# ---------------------------------------------------------------------------

class TestGetMarketsClosingWithin:
    @patch.object(kc, "_paginate")
    def test_passes_time_bounds_to_markets_endpoint(self, mock_pag):
        # First call: /markets — return 2 markets
        # Second call: /events — return 0 (irrelevant for this test)
        mock_pag.side_effect = [
            [
                {"ticker": "KX-1", "event_ticker": "EV1",
                 "title": "Q1", "status": "active",
                 "yes_bid_dollars": "0.50", "yes_ask_dollars": "0.52",
                 "close_time": "2026-06-01T00:00:00Z"},
                {"ticker": "KX-2", "event_ticker": "EV1",
                 "title": "Q2", "status": "active",
                 "yes_bid_dollars": "0.30", "yes_ask_dollars": "0.32",
                 "close_time": "2026-06-01T00:00:00Z"},
            ],
            [],  # events lookup returns nothing
        ]
        df = kc.get_markets_closing_within(7, limit=10)
        assert len(df) == 2
        # First call should hit "markets" with min/max_close_ts
        first = mock_pag.call_args_list[0]
        assert first[0][0] == "markets"
        params = first[1]["params"]
        assert "min_close_ts" in params and "max_close_ts" in params
        assert params["max_close_ts"] > params["min_close_ts"]

    @patch.object(kc, "_paginate")
    def test_returns_empty_dataframe_when_no_markets(self, mock_pag):
        mock_pag.return_value = []
        df = kc.get_markets_closing_within(7)
        assert df.empty

    @patch.object(kc, "_paginate")
    def test_sorted_closing_soonest_first(self, mock_pag):
        # Two markets with different close times — close_time_dt should
        # be parsed and rows sorted ascending.
        #
        # Close times MUST be relative to now, not literals. This test used
        # hard-coded 2026-05-20 / 2026-06-30 and silently began failing the day
        # the later one passed: _add_days_to_resolution clamps negatives with
        # .clip(lower=0), so once both dates were in the past both rows became
        # 0.0, the stable sort preserved input order, and the assertion
        # inverted. The production code was correct the whole time; the fixture
        # had an expiry date. It went unnoticed for five weeks because nothing
        # had been pushed, so CI never ran.
        now = datetime.now(UTC)
        late = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        early = (now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        mock_pag.side_effect = [
            [
                {"ticker": "KX-LATE", "event_ticker": "EV1",
                 "title": "Q late", "status": "active",
                 "yes_bid_dollars": "0.50", "yes_ask_dollars": "0.50",
                 "close_time": late},
                {"ticker": "KX-EARLY", "event_ticker": "EV2",
                 "title": "Q early", "status": "active",
                 "yes_bid_dollars": "0.50", "yes_ask_dollars": "0.50",
                 "close_time": early},
            ],
            [],
        ]
        df = kc.get_markets_closing_within(60)
        # Sort is by days_to_resolution ascending → KX-EARLY first
        assert df.iloc[0]["id"] == "KX-EARLY"
        assert df.iloc[1]["id"] == "KX-LATE"

    @patch.object(kc, "_paginate")
    def test_past_close_times_all_clamp_to_zero(self, mock_pag):
        """Pin the clamping behaviour that made the fixture above rot.

        Everything already closed reports 0.0 days rather than a negative, so
        callers filtering on `days_to_resolution <= N` cannot be surprised by
        sign — and so the next person to write a fixture sees why literal past
        dates make ordering meaningless.
        """
        past = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        older = (datetime.now(UTC) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        mock_pag.side_effect = [
            [
                {"ticker": "KX-A", "event_ticker": "EV1", "title": "A",
                 "status": "active", "yes_bid_dollars": "0.50",
                 "yes_ask_dollars": "0.50", "close_time": past},
                {"ticker": "KX-B", "event_ticker": "EV2", "title": "B",
                 "status": "active", "yes_bid_dollars": "0.50",
                 "yes_ask_dollars": "0.50", "close_time": older},
            ],
            [],
        ]
        df = kc.get_markets_closing_within(60)
        assert list(df["days_to_resolution"]) == [0.0, 0.0]

    @patch.object(kc, "_paginate")
    def test_event_category_joined_when_match(self, mock_pag):
        mock_pag.side_effect = [
            [{"ticker": "KX-1", "event_ticker": "GIRO",
              "title": "Q", "status": "active",
              "yes_bid_dollars": "0.5", "yes_ask_dollars": "0.5",
              "close_time": "2026-06-01T00:00:00Z"}],
            [{"event_ticker": "GIRO", "category": "Sports",
              "title": "Giro d'Italia winner"}],
        ]
        df = kc.get_markets_closing_within(60)
        assert df.iloc[0]["category"] == "Sports"
        assert df.iloc[0]["event_title"] == "Giro d'Italia winner"
