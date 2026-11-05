"""Tests for the authenticated kalshi_client functions
(get_balance / get_authed_positions / get_fills / get_settlements).

The signing layer is exercised in test_kalshi_auth.py; here we mock
out ``kalshi_auth.authed_get`` / ``authed_paginate`` and assert the
DataFrame shape + field coercion.
"""

from __future__ import annotations

from unittest.mock import patch

from pm.venue import kalshi_client as kc

# ---------------------------------------------------------------------------
# get_balance
# ---------------------------------------------------------------------------

class TestGetBalance:
    @patch("pm.venue.kalshi_auth.authed_get")
    def test_returns_cents(self, mock_get):
        mock_get.return_value = {"balance": 12345, "payout": 678}
        b = kc.get_balance()
        assert b == {"balance_cents": 12345, "payout_cents": 678}

    @patch("pm.venue.kalshi_auth.authed_get")
    def test_missing_keys_default_to_zero(self, mock_get):
        mock_get.return_value = {}
        assert kc.get_balance() == {"balance_cents": 0, "payout_cents": 0}


# ---------------------------------------------------------------------------
# get_authed_positions
# ---------------------------------------------------------------------------

class TestGetPositions:
    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_empty_returns_empty_dataframe_with_expected_cols(self, mock_pag):
        mock_pag.return_value = []
        df = kc.get_authed_positions()
        assert df.empty
        assert "ticker" in df.columns
        assert "position" in df.columns
        assert "market_exposure_cents" in df.columns

    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_shapes_payload_into_rows(self, mock_pag):
        # Real Kalshi response shape: type-tagged string fields
        # (position_fp, market_exposure_dollars, etc.). Verified against
        # the live API; bare-field test mocks would have hidden the
        # production-data bug.
        mock_pag.return_value = [
            {
                "ticker": "KX-1",
                "position_fp": "10.00",
                "market_exposure_dollars": "6.000000",
                "realized_pnl_dollars": "0.500000",
                "total_traded_dollars": "10.000000",
                "resting_orders_count": 1,
                "last_updated_ts": "2026-05-18T12:00:00Z",
            },
            {
                "ticker": "KX-2",
                "position_fp": "-5.00",
                "market_exposure_dollars": "2.000000",
                "realized_pnl_dollars": "-0.200000",
                "total_traded_dollars": "5.000000",
                "resting_orders_count": 0,
                "last_updated_ts": "2026-05-18T13:00:00Z",
            },
        ]
        df = kc.get_authed_positions()
        assert len(df) == 2
        assert df.iloc[0]["ticker"] == "KX-1"
        assert df.iloc[0]["position"] == 10
        assert df.iloc[0]["market_exposure_cents"] == 600
        assert df.iloc[1]["position"] == -5
        assert df.iloc[1]["realized_pnl_cents"] == -20

    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_non_zero_only_passes_count_filter(self, mock_pag):
        mock_pag.return_value = []
        kc.get_authed_positions(non_zero_only=True)
        # Confirm the filter param was forwarded
        args, kwargs = mock_pag.call_args
        params = kwargs.get("params") or args[1]
        assert params.get("count_filter") == "position"


# ---------------------------------------------------------------------------
# get_fills
# ---------------------------------------------------------------------------

class TestGetFills:
    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_empty_dataframe_has_expected_cols(self, mock_pag):
        mock_pag.return_value = []
        df = kc.get_fills()
        assert df.empty
        for col in ["ticker", "side", "action", "yes_price_cents",
                    "no_price_cents", "is_taker"]:
            assert col in df.columns

    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_fills_sorted_newest_first(self, mock_pag):
        # Real Kalshi response: count_fp + *_price_dollars suffixes.
        mock_pag.return_value = [
            {"ticker": "KX-A", "order_id": "1", "trade_id": "t1",
             "side": "yes", "action": "buy", "count_fp": "10.00",
             "yes_price_dollars": "0.5000", "no_price_dollars": "0.5000",
             "is_taker": True,
             "created_time": "2026-05-18T10:00:00Z"},
            {"ticker": "KX-A", "order_id": "2", "trade_id": "t2",
             "side": "yes", "action": "buy", "count_fp": "5.00",
             "yes_price_dollars": "0.5500", "no_price_dollars": "0.4500",
             "is_taker": False,
             "created_time": "2026-05-18T12:00:00Z"},
        ]
        df = kc.get_fills()
        # Newest first → t2 then t1
        assert df.iloc[0]["trade_id"] == "t2"
        assert df.iloc[1]["trade_id"] == "t1"
        # Values decode correctly
        assert df.iloc[0]["count"] == 5
        assert df.iloc[0]["yes_price_cents"] == 55
        assert df.iloc[1]["yes_price_cents"] == 50

    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_ticker_filter_propagated(self, mock_pag):
        mock_pag.return_value = []
        kc.get_fills(ticker="KX-FOO")
        args, kwargs = mock_pag.call_args
        params = kwargs.get("params") or args[1]
        assert params.get("ticker") == "KX-FOO"


# ---------------------------------------------------------------------------
# get_settlements
# ---------------------------------------------------------------------------

class TestEstimateCashout:
    def test_zero_position_returns_zero(self):
        r = kc.estimate_cashout(position=0, yes_bid=0.5, yes_ask=0.5)
        assert r["cashout_cents"] == 0
        assert r["side"] == ""

    def test_yes_long_tight_spread_uses_mid(self):
        # Hold 100 YES, tight spread 60/62 → mid 61¢ → cash-out $61
        r = kc.estimate_cashout(
            position=100, yes_bid=0.60, yes_ask=0.62,
            cost_basis_cents=5000,
        )
        assert r["side"] == "YES"
        assert r["exit_price_cents"] == 61
        assert r["cashout_cents"] == 6100
        assert r["status"] == "ok"

    def test_no_long_tight_spread_uses_inverted_mid(self):
        # Hold 33 NO, YES tight 40/41 → YES mid 40.5 → NO mid 59.5
        r = kc.estimate_cashout(
            position=-33, yes_bid=0.40, yes_ask=0.41,
            cost_basis_cents=1947,
        )
        assert r["side"] == "NO"
        assert r["exit_price_cents"] in (59, 60)  # rounding
        assert r["status"] == "ok"

    def test_yes_long_wide_spread_with_last_uses_last(self):
        # The real GA LtGov R bug: bid 2¢ ask 97¢ but last trade 99¢.
        # Stale-bid case — should use last_trade, not the bid.
        r = kc.estimate_cashout(
            position=43, yes_bid=0.02, yes_ask=0.97, last_trade=0.99,
            cost_basis_cents=1924,
        )
        assert r["exit_price_cents"] == 99, (
            f"got {r['exit_price_cents']}; wide-spread case should "
            "use last_trade, not the bid"
        )
        assert r["cashout_cents"] == 99 * 43  # = 4257

    def test_no_long_after_price_collapse(self):
        # Hold 33 NO bought at 59¢, market YES=0.4¢, NO bid effectively 100¢
        r = kc.estimate_cashout(
            position=-33, yes_bid=0.002, yes_ask=0.004,
            cost_basis_cents=1947,
        )
        # NO mid = (1 - 0.004 + 1 - 0.002) / 2 = 0.997 → 100¢
        assert r["exit_price_cents"] == 100
        assert r["cashout_cents"] == 3300
        assert r["pnl_cents"] == 1353

    def test_closed_market_falls_back_to_last_trade(self):
        # No bid/ask but last trade exists — closed market awaiting settlement.
        r = kc.estimate_cashout(
            position=100, yes_bid=0.0, yes_ask=0.0, last_trade=0.95,
            cost_basis_cents=5000,
        )
        # status is "ok" now since last_trade is a fine signal; the
        # "closed" status only fires when bid exists but is way stale
        assert r["exit_price_cents"] == 95
        assert r["cashout_cents"] == 9500

    def test_stale_bid_far_below_last_marked_closed(self):
        # Bid 5¢, last 90¢ → market essentially decided, bid is stale
        r = kc.estimate_cashout(
            position=10, yes_bid=0.05, yes_ask=0.0, last_trade=0.90,
            cost_basis_cents=400,
        )
        assert r["exit_price_cents"] == 90
        assert r["status"] == "closed"

    def test_unknown_when_no_bid_no_last(self):
        r = kc.estimate_cashout(
            position=100, yes_bid=0.0, yes_ask=0.0, last_trade=0.0,
            cost_basis_cents=5000,
        )
        assert r["status"] == "unknown"
        assert r["cashout_cents"] == 0
        assert r["pnl_cents"] == -5000   # total loss


class TestGetSettlements:
    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_empty_returns_empty(self, mock_pag):
        mock_pag.return_value = []
        df = kc.get_settlements()
        assert df.empty
        assert "ticker" in df.columns

    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_revenue_integer_treated_as_cents(self, mock_pag):
        """Regression guard: Kalshi's settlement ``revenue`` is an
        integer already in cents (e.g. 3300 = $33.00). Earlier code
        ran ``_dollars_to_cents`` on it and produced $3300."""
        mock_pag.return_value = [
            {"ticker": "T", "market_result": "no",
             "yes_count_fp": "0.00", "no_count_fp": "33.00",
             "revenue": 3300,  # integer cents, NOT a dollar string
             "settled_time": "2026-05-20T00:00:00Z"},
        ]
        df = kc.get_settlements()
        assert df.iloc[0]["revenue_cents"] == 3300, (
            f"got {df.iloc[0]['revenue_cents']} — settlements `revenue` "
            "is already cents, must not pass through _dollars_to_cents")

    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_revenue_dollars_string_converted(self, mock_pag):
        """Some response shapes use ``revenue_dollars`` as a string;
        those DO go through the conversion."""
        mock_pag.return_value = [
            {"ticker": "T", "market_result": "yes",
             "yes_count_fp": "10.00", "no_count_fp": "0.00",
             "revenue_dollars": "10.000000",  # string dollars
             "settled_time": "2026-05-20T00:00:00Z"},
        ]
        df = kc.get_settlements()
        assert df.iloc[0]["revenue_cents"] == 1000

    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_settlements_sorted_newest_first(self, mock_pag):
        mock_pag.return_value = [
            {"ticker": "KX-A", "market_result": "yes",
             "yes_count_fp": "10.00", "no_count_fp": "0.00",
             "revenue_dollars": "10.000000",
             "settled_time": "2026-05-01T00:00:00Z"},
            {"ticker": "KX-B", "market_result": "no",
             "yes_count_fp": "0.00", "no_count_fp": "5.00",
             "revenue_dollars": "5.000000",
             "settled_time": "2026-05-15T00:00:00Z"},
        ]
        df = kc.get_settlements()
        assert df.iloc[0]["ticker"] == "KX-B"
        assert df.iloc[1]["ticker"] == "KX-A"
        # Values decode correctly
        assert df.iloc[0]["no_count"] == 5
        assert df.iloc[0]["revenue_cents"] == 500
        assert df.iloc[1]["yes_count"] == 10
