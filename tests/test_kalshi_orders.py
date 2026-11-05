"""Tests for kalshi_orders — pre_flight_check, place_limit_order,
cancel_order, and the audit log.

The HTTP layer (signing, retries) is exercised in test_kalshi_auth.py.
Here we mock ``kalshi_auth.authed_post`` / ``authed_delete`` /
``authed_paginate`` and assert behaviour at the orders-module level.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import requests

from pm.venue import kalshi_orders as ko

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def orders_log(tmp_path, monkeypatch):
    """Each test gets its own audit log file."""
    p = tmp_path / "orders.log"
    monkeypatch.setenv("ORDERS_LOG_PATH", str(p))
    return p


@pytest.fixture
def trading_enabled(monkeypatch, orders_log):
    """Pretend trading is enabled. Doesn't actually load keys —
    place_limit_order / cancel_order are mocked at the auth-call
    boundary."""
    monkeypatch.setenv("KALSHI_LIVE_ENABLED", "true")
    monkeypatch.setattr(ko.ka, "is_configured", lambda: True)
    return orders_log


# ---------------------------------------------------------------------------
# is_live_enabled / is_trading_enabled
# ---------------------------------------------------------------------------

class TestEnableFlags:
    def test_unset_is_disabled(self, monkeypatch):
        monkeypatch.delenv("KALSHI_LIVE_ENABLED", raising=False)
        assert ko.is_live_enabled() is False

    def test_truthy_values_enable(self, monkeypatch):
        for v in ("true", "1", "yes", "ON", "True"):
            monkeypatch.setenv("KALSHI_LIVE_ENABLED", v)
            assert ko.is_live_enabled() is True

    def test_falsy_values_disable(self, monkeypatch):
        for v in ("false", "0", "no", "", " "):
            monkeypatch.setenv("KALSHI_LIVE_ENABLED", v)
            assert ko.is_live_enabled() is False

    def test_trading_requires_both(self, monkeypatch):
        monkeypatch.setattr(ko.ka, "is_configured", lambda: True)
        monkeypatch.setenv("KALSHI_LIVE_ENABLED", "false")
        assert ko.is_trading_enabled() is False
        monkeypatch.setenv("KALSHI_LIVE_ENABLED", "true")
        assert ko.is_trading_enabled() is True
        monkeypatch.setattr(ko.ka, "is_configured", lambda: False)
        assert ko.is_trading_enabled() is False


# ---------------------------------------------------------------------------
# pre_flight_check
# ---------------------------------------------------------------------------

def _good_args(**overrides):
    """Baseline pre_flight_check call where everything is fine."""
    args = dict(
        ticker="KX-FOO", side="yes", count=10, limit_price_cents=50,
        current_mid_cents=50, bankroll=1000.0, in_drawdown_pause=False,
        todays_order_count=0,
    )
    args.update(overrides)
    return args


class TestPreFlight:
    def test_clean_passes(self, trading_enabled):
        issues = ko.pre_flight_check(**_good_args())
        assert issues == []

    def test_blocks_when_trading_disabled(self, monkeypatch, orders_log):
        # No fixture — trading explicitly off
        monkeypatch.setattr(ko.ka, "is_configured", lambda: True)
        monkeypatch.delenv("KALSHI_LIVE_ENABLED", raising=False)
        issues = ko.pre_flight_check(**_good_args())
        assert any("Live trading is disabled" in m for s, m in issues
                   if s == "block")

    def test_blocks_when_drawdown_paused(self, trading_enabled):
        issues = ko.pre_flight_check(**_good_args(in_drawdown_pause=True))
        assert any("Drawdown circuit breaker" in m for s, m in issues
                   if s == "block")

    def test_blocks_fat_finger_price(self, trading_enabled):
        # limit 95¢ vs mid 50¢ → 45¢ away, way beyond default 10¢ cap
        issues = ko.pre_flight_check(**_good_args(limit_price_cents=95))
        assert any("away from the current mid" in m for s, m in issues
                   if s == "block")

    def test_blocks_stake_cap(self, trading_enabled):
        # 200 contracts × 50¢ = $100, above the $50 default cap
        issues = ko.pre_flight_check(**_good_args(count=200))
        assert any("exceeds the per-order cap" in m for s, m in issues
                   if s == "block")

    def test_blocks_daily_cap(self, trading_enabled):
        issues = ko.pre_flight_check(**_good_args(todays_order_count=10))
        assert any("Daily order cap reached" in m for s, m in issues
                   if s == "block")

    def test_blocks_invalid_price(self, trading_enabled):
        for bad in (0, 100, -1):
            issues = ko.pre_flight_check(**_good_args(limit_price_cents=bad))
            assert any(s == "block" for s, _ in issues)

    def test_blocks_invalid_count(self, trading_enabled):
        issues = ko.pre_flight_check(**_good_args(count=0))
        assert any("Count must be positive" in m for s, m in issues
                   if s == "block")

    def test_warns_on_large_pct_of_bankroll(self, trading_enabled):
        # 80 contracts × 50¢ = $40 on a $200 bankroll = 20% → warn
        issues = ko.pre_flight_check(**_good_args(count=80, bankroll=200.0))
        warns = [m for s, m in issues if s == "warn"]
        assert any("% of bankroll" in m for m in warns)


# ---------------------------------------------------------------------------
# place_limit_order
# ---------------------------------------------------------------------------

class TestPlaceLimitOrder:
    @patch("pm.venue.kalshi_auth.authed_post")
    def test_happy_path_sends_correct_body(self, mock_post, trading_enabled):
        mock_post.return_value = {"order": {"order_id": "abc"}}
        resp = ko.place_limit_order(
            ticker="KX-FOO", side="yes", action="buy",
            count=5, limit_price_cents=45,
        )
        assert resp == {"order": {"order_id": "abc"}}
        path, body = mock_post.call_args[0]
        # v2 order endpoint + YES-leg bid/ask schema.
        assert path == "portfolio/events/orders"
        assert body["ticker"] == "KX-FOO"
        assert body["side"] == "bid"          # buy YES
        assert body["count"] == "5"           # fixed-point string
        assert body["price"] == "0.45"        # dollar string, not cents
        assert "action" not in body
        assert "yes_price" not in body and "no_price" not in body
        # time_in_force + self_trade_prevention_type are required on v2.
        assert body["time_in_force"] == "good_till_canceled"
        assert body["self_trade_prevention_type"] == "taker_at_cross"
        assert "client_order_id" in body and body["client_order_id"]

    @patch("pm.venue.kalshi_auth.authed_post")
    def test_no_side_maps_to_complement_yes_price(self, mock_post,
                                                  trading_enabled):
        # buy NO @30 == sell YES @70 → ask at 0.70
        mock_post.return_value = {}
        ko.place_limit_order(
            ticker="KX-FOO", side="no", action="buy",
            count=5, limit_price_cents=30,
        )
        _, body = mock_post.call_args[0]
        assert body["side"] == "ask"
        assert body["price"] == "0.70"

    def test_rejects_when_trading_disabled(self, monkeypatch, orders_log):
        monkeypatch.setattr(ko.ka, "is_configured", lambda: True)
        monkeypatch.delenv("KALSHI_LIVE_ENABLED", raising=False)
        with pytest.raises(ko.OrderRejected, match="live trading"):
            ko.place_limit_order(
                ticker="KX-FOO", side="yes", action="buy",
                count=5, limit_price_cents=50,
            )

    def test_rejects_bad_side(self, trading_enabled):
        with pytest.raises(ko.OrderRejected, match="side"):
            ko.place_limit_order(
                ticker="KX-FOO", side="maybe", action="buy",
                count=5, limit_price_cents=50,
            )

    def test_rejects_bad_price(self, trading_enabled):
        with pytest.raises(ko.OrderRejected, match=r"limit_price_cents"):
            ko.place_limit_order(
                ticker="KX-FOO", side="yes", action="buy",
                count=5, limit_price_cents=0,
            )

    @patch("pm.venue.kalshi_auth.authed_post")
    def test_audit_log_records_submit_and_accepted(self, mock_post,
                                                    trading_enabled,
                                                    orders_log):
        mock_post.return_value = {"order_id": "ok"}
        ko.place_limit_order(
            ticker="KX-FOO", side="yes", action="buy",
            count=5, limit_price_cents=50,
        )
        contents = orders_log.read_text().strip().splitlines()
        events = [json.loads(line)["event"] for line in contents]
        assert "submit" in events
        assert "accepted" in events

    @patch("pm.venue.kalshi_auth.authed_post")
    def test_audit_log_records_error_on_api_failure(self, mock_post,
                                                     trading_enabled,
                                                     orders_log):
        mock_post.side_effect = requests.HTTPError("400 insufficient_balance")
        with pytest.raises(requests.HTTPError):
            ko.place_limit_order(
                ticker="KX-FOO", side="yes", action="buy",
                count=5, limit_price_cents=50,
            )
        contents = orders_log.read_text().strip().splitlines()
        events = [json.loads(line)["event"] for line in contents]
        assert "submit" in events
        assert "error" in events
        assert "accepted" not in events


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

class TestCashOutPosition:
    @patch("pm.venue.kalshi_auth.authed_post")
    def test_yes_long_sells_at_bid(self, mock_post, trading_enabled):
        mock_post.return_value = {"order": {"order_id": "co-1"}}
        ko.cash_out_position(
            ticker="KX-FOO", held_side="yes", count=43,
            current_bid_cents=45,
        )
        _, body = mock_post.call_args[0]
        # sell YES @45 → ask at 0.45
        assert body["side"] == "ask"
        assert body["price"] == "0.45"
        assert body["count"] == "43"

    @patch("pm.venue.kalshi_auth.authed_post")
    def test_no_long_sells_no_at_no_bid(self, mock_post, trading_enabled):
        mock_post.return_value = {}
        ko.cash_out_position(
            ticker="KX-FOO", held_side="no", count=33,
            current_bid_cents=99,
        )
        _, body = mock_post.call_args[0]
        # sell NO @99 == buy YES @1 → bid at 0.01
        assert body["side"] == "bid"
        assert body["price"] == "0.01"

    def test_rejects_zero_bid(self, trading_enabled):
        # Refuse to dump into an empty book
        with pytest.raises(ko.OrderRejected, match=r"current_bid_cents"):
            ko.cash_out_position(
                ticker="KX-FOO", held_side="yes", count=10,
                current_bid_cents=0,
            )

    def test_rejects_bad_count(self, trading_enabled):
        with pytest.raises(ko.OrderRejected, match="count"):
            ko.cash_out_position(
                ticker="KX-FOO", held_side="yes", count=0,
                current_bid_cents=50,
            )

    def test_rejects_when_disabled(self, monkeypatch):
        monkeypatch.setattr(ko.ka, "is_configured", lambda: True)
        monkeypatch.delenv("KALSHI_LIVE_ENABLED", raising=False)
        with pytest.raises(ko.OrderRejected, match="live trading"):
            ko.cash_out_position(
                ticker="KX-FOO", held_side="yes", count=10,
                current_bid_cents=50,
            )

    @patch("pm.venue.kalshi_auth.authed_post")
    def test_audit_log_cashout_events(self, mock_post, trading_enabled, orders_log):
        mock_post.return_value = {"order_id": "x"}
        ko.cash_out_position(
            ticker="KX-FOO", held_side="yes", count=10,
            current_bid_cents=50,
        )
        contents = orders_log.read_text().strip().splitlines()
        events = [json.loads(line)["event"] for line in contents]
        assert "cashout_submit" in events
        assert "cashout_accepted" in events


class TestPlaceTakeProfit:
    @patch("pm.venue.kalshi_auth.authed_post")
    def test_yes_long_sells_yes_at_higher_price(self, mock_post, trading_enabled):
        mock_post.return_value = {"order": {"order_id": "tp-abc"}}
        resp = ko.place_take_profit(
            ticker="KX-FOO", held_side="yes", count=43,
            sell_price_cents=99, current_bid_cents=45,
        )
        assert resp["order"]["order_id"] == "tp-abc"
        path, body = mock_post.call_args[0]
        assert path == "portfolio/events/orders"
        # sell YES @99 → ask at 0.99
        assert body["side"] == "ask"
        assert body["price"] == "0.99"
        assert body["count"] == "43"
        assert "client_order_id" in body

    @patch("pm.venue.kalshi_auth.authed_post")
    def test_no_long_sells_no_at_higher_no_price(self, mock_post, trading_enabled):
        mock_post.return_value = {}
        ko.place_take_profit(
            ticker="KX-FOO", held_side="no", count=33,
            sell_price_cents=95, current_bid_cents=60,
        )
        _, body = mock_post.call_args[0]
        # sell NO @95 == buy YES @5 → bid at 0.05
        assert body["side"] == "bid"
        assert body["price"] == "0.05"

    def test_rejects_when_trading_disabled(self, monkeypatch, orders_log):
        monkeypatch.setattr(ko.ka, "is_configured", lambda: True)
        monkeypatch.delenv("KALSHI_LIVE_ENABLED", raising=False)
        with pytest.raises(ko.OrderRejected, match="live trading"):
            ko.place_take_profit(
                ticker="KX-FOO", held_side="yes", count=10,
                sell_price_cents=99,
            )

    def test_rejects_price_at_or_below_current_bid(self, trading_enabled):
        with pytest.raises(ko.OrderRejected, match="below the current bid"):
            ko.place_take_profit(
                ticker="KX-FOO", held_side="yes", count=10,
                sell_price_cents=45, current_bid_cents=45,
            )

    def test_buffer_enforced(self, trading_enabled):
        # bid 50, sell 51 with buffer 2 → rejected
        with pytest.raises(ko.OrderRejected, match="below the current bid"):
            ko.place_take_profit(
                ticker="KX-FOO", held_side="yes", count=10,
                sell_price_cents=51, current_bid_cents=50,
                min_buffer_cents=2,
            )

    @patch("pm.venue.kalshi_auth.authed_post")
    def test_skip_bid_check_when_not_supplied(self, mock_post, trading_enabled):
        # current_bid_cents=None → buffer check skipped
        mock_post.return_value = {}
        ko.place_take_profit(
            ticker="KX-FOO", held_side="yes", count=10,
            sell_price_cents=50, current_bid_cents=None,
        )
        mock_post.assert_called_once()

    def test_rejects_bad_side(self, trading_enabled):
        with pytest.raises(ko.OrderRejected, match="held_side"):
            ko.place_take_profit(
                ticker="KX-FOO", held_side="maybe", count=10,
                sell_price_cents=99,
            )

    def test_rejects_bad_count(self, trading_enabled):
        with pytest.raises(ko.OrderRejected, match="count"):
            ko.place_take_profit(
                ticker="KX-FOO", held_side="yes", count=0,
                sell_price_cents=99,
            )

    def test_rejects_bad_price(self, trading_enabled):
        with pytest.raises(ko.OrderRejected, match="sell_price_cents"):
            ko.place_take_profit(
                ticker="KX-FOO", held_side="yes", count=10,
                sell_price_cents=100,
            )

    @patch("pm.venue.kalshi_auth.authed_post")
    def test_audit_log_tp_events(self, mock_post, trading_enabled, orders_log):
        mock_post.return_value = {"order_id": "x"}
        ko.place_take_profit(
            ticker="KX-FOO", held_side="yes", count=10,
            sell_price_cents=99,
        )
        contents = orders_log.read_text().strip().splitlines()
        events = [json.loads(line)["event"] for line in contents]
        assert "tp_submit" in events
        assert "tp_accepted" in events

    @patch("pm.venue.kalshi_auth.authed_post")
    def test_audit_log_records_error(self, mock_post, trading_enabled, orders_log):
        mock_post.side_effect = requests.HTTPError("400 invalid_parameters")
        with pytest.raises(requests.HTTPError):
            ko.place_take_profit(
                ticker="KX-FOO", held_side="yes", count=10,
                sell_price_cents=99,
            )
        contents = orders_log.read_text().strip().splitlines()
        events = [json.loads(line)["event"] for line in contents]
        assert "tp_submit" in events
        assert "tp_error" in events
        assert "tp_accepted" not in events


class TestCancelOrder:
    @patch("pm.venue.kalshi_auth.authed_delete")
    def test_cancel_happy_path(self, mock_del, trading_enabled, orders_log):
        mock_del.return_value = {"order_id": "abc", "status": "cancelled"}
        resp = ko.cancel_order("abc")
        assert resp["status"] == "cancelled"
        mock_del.assert_called_once_with("portfolio/events/orders/abc")

    def test_cancel_rejects_when_disabled(self, monkeypatch):
        monkeypatch.setattr(ko.ka, "is_configured", lambda: True)
        monkeypatch.delenv("KALSHI_LIVE_ENABLED", raising=False)
        with pytest.raises(ko.OrderRejected):
            ko.cancel_order("abc")


# ---------------------------------------------------------------------------
# _today_submit_count
# ---------------------------------------------------------------------------

class TestTodayCount:
    def test_count_zero_on_missing_log(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ORDERS_LOG_PATH", str(tmp_path / "nope.log"))
        assert ko._today_submit_count() == 0

    @patch("pm.venue.kalshi_auth.authed_post")
    def test_count_increments_after_submit(self, mock_post, trading_enabled,
                                            orders_log):
        mock_post.return_value = {}
        assert ko._today_submit_count() == 0
        ko.place_limit_order(
            ticker="KX-A", side="yes", action="buy",
            count=1, limit_price_cents=50,
        )
        ko.place_limit_order(
            ticker="KX-B", side="yes", action="buy",
            count=1, limit_price_cents=50,
        )
        assert ko._today_submit_count() == 2


# ---------------------------------------------------------------------------
# get_resting_orders
# ---------------------------------------------------------------------------

class TestRestingOrders:
    def test_returns_empty_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(ko.ka, "is_configured", lambda: False)
        df = ko.get_resting_orders()
        assert df.empty

    @patch("pm.venue.kalshi_auth.authed_paginate")
    def test_normalizes_rows(self, mock_pag, monkeypatch):
        monkeypatch.setattr(ko.ka, "is_configured", lambda: True)
        mock_pag.return_value = [
            {
                "order_id": "abc", "client_order_id": "polymarket-x",
                "ticker": "KX-FOO", "side": "yes", "action": "buy",
                "status": "resting", "remaining_count": 5,
                "queue_position": 2, "yes_price": 45, "no_price": 0,
                "created_time": "2026-05-18T10:00:00Z",
            },
        ]
        df = ko.get_resting_orders()
        assert len(df) == 1
        assert df.iloc[0]["order_id"] == "abc"
        assert df.iloc[0]["yes_price_cents"] == 45
