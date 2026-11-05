"""Tests for the crypto-data layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from pm.feeds import crypto_data as cd

# ---------------------------------------------------------------------------
# get_spot_price
# ---------------------------------------------------------------------------

class TestGetSpotPrice:
    @patch.object(cd, "_get")
    def test_returns_float(self, mock_get):
        mock_get.return_value = {"price": "76887.58"}
        assert cd.get_spot_price("BTC") == pytest.approx(76887.58)
        mock_get.assert_called_once_with("products/BTC-USD/ticker")

    def test_unknown_asset_returns_none(self):
        assert cd.get_spot_price("FAKECOIN") is None

    @patch.object(cd, "_get")
    def test_http_failure_returns_none(self, mock_get):
        import requests
        mock_get.side_effect = requests.RequestException("503")
        assert cd.get_spot_price("BTC") is None


# ---------------------------------------------------------------------------
# get_klines (Coinbase candle format)
# ---------------------------------------------------------------------------

class TestGetKlines:
    def _candles(self, n: int, start_ts: int):
        # Coinbase format: [time(s), low, high, open, close, volume]
        return [
            [start_ts + i * 86400, 90.0, 110.0, 95.0, 100.0 + i, 1000.0]
            for i in range(n)
        ]

    @patch.object(cd, "_get")
    def test_returns_normalized_columns(self, mock_get):
        start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
        mock_get.return_value = self._candles(5, start)
        df = cd.get_klines("BTC", days=5)
        assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
        assert len(df) == 5
        assert df["close"].iloc[0] == pytest.approx(100.0)
        assert df["close"].iloc[-1] == pytest.approx(104.0)

    @patch.object(cd, "_get")
    def test_unknown_asset_returns_empty(self, mock_get):
        df = cd.get_klines("FAKECOIN", days=5)
        assert df.empty
        mock_get.assert_not_called()

    @patch.object(cd, "_get")
    def test_pagination_for_long_windows(self, mock_get):
        # 700 days needs 3 chunks (300 + 300 + 100)
        start = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
        mock_get.side_effect = [
            self._candles(300, start + 86400 * 400),
            self._candles(300, start + 86400 * 100),
            self._candles(100, start),
        ]
        df = cd.get_klines("BTC", days=700)
        assert mock_get.call_count == 3
        # Deduplicated, sorted ascending
        assert df["timestamp"].is_monotonic_increasing

    @patch.object(cd, "_get")
    def test_empty_response_returns_empty(self, mock_get):
        mock_get.return_value = []
        assert cd.get_klines("BTC", days=5).empty


# ---------------------------------------------------------------------------
# spot_at — historical close at-or-before a timestamp
# ---------------------------------------------------------------------------

class TestSpotAt:
    @patch.object(cd, "get_klines")
    def test_returns_close_at_or_before(self, mock_klines):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        mock_klines.return_value = pd.DataFrame({
            "timestamp": [base + timedelta(days=i) for i in range(10)],
            "open": [100.0] * 10, "high": [110.0] * 10, "low": [90.0] * 10,
            "close": [100.0 + i for i in range(10)],
            "volume": [1000.0] * 10,
        })
        # 3.5 days in → at-or-before is day 3 (close 103)
        assert cd.spot_at("BTC", base + timedelta(days=3, hours=12)) == pytest.approx(103.0)
        # Exactly on day 5 → close 105
        assert cd.spot_at("BTC", base + timedelta(days=5)) == pytest.approx(105.0)
        # Before all data → None
        assert cd.spot_at("BTC", base - timedelta(days=1)) is None

    @patch.object(cd, "get_klines")
    def test_empty_returns_none(self, mock_klines):
        mock_klines.return_value = pd.DataFrame()
        assert cd.spot_at("BTC", datetime(2026, 1, 1, tzinfo=UTC)) is None


# ---------------------------------------------------------------------------
# realized_volatility
# ---------------------------------------------------------------------------

class TestRealizedVolatility:
    def test_zero_for_constant_prices(self):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        df = pd.DataFrame({
            "timestamp": [base + timedelta(days=i) for i in range(35)],
            "open": [100.0] * 35, "high": [100.0] * 35, "low": [100.0] * 35,
            "close": [100.0] * 35, "volume": [1000.0] * 35,
        })
        # Constant prices → daily std = 0 → return None (we reject zero vol)
        assert cd.realized_volatility(df, window=30) is None

    def test_positive_for_varying_prices(self):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        prices = [100.0, 105.0, 95.0, 110.0, 90.0] * 10  # 50 alternating
        df = pd.DataFrame({
            "timestamp": [base + timedelta(days=i) for i in range(50)],
            "open": prices, "high": prices, "low": prices,
            "close": prices, "volume": [1000.0] * 50,
        })
        v = cd.realized_volatility(df, window=30)
        assert v is not None
        # Annualized vol of 5-10% daily swings is multi-hundred percent
        assert v > 1.0

    def test_insufficient_data_returns_none(self):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        df = pd.DataFrame({
            "timestamp": [base + timedelta(days=i) for i in range(5)],
            "open": [100.0] * 5, "high": [100.0] * 5, "low": [100.0] * 5,
            "close": [100.0, 101.0, 102.0, 103.0, 104.0], "volume": [1000.0] * 5,
        })
        assert cd.realized_volatility(df, window=30) is None


# ---------------------------------------------------------------------------
# Lognormal terminal probability
# ---------------------------------------------------------------------------

class TestLognormal:
    def test_at_the_money_high_vol_long_time(self):
        # Spot = strike, no time → undefined-ish; require positive time
        assert cd.lognormal_above(100, 100, 0.5, 0) is None

    def test_above_returns_high_when_deep_itm(self):
        # Spot $200, strike $100, low vol — almost certainly above
        p = cd.lognormal_above(200, 100, 0.1, 365)
        assert p is not None and p > 0.95

    def test_above_returns_low_when_deep_otm(self):
        # Spot $100, strike $200, low vol — almost certainly below
        p = cd.lognormal_above(100, 200, 0.1, 365)
        assert p is not None and p < 0.05

    def test_below_complements_above(self):
        a = cd.lognormal_above(100, 110, 0.4, 90)
        b = cd.lognormal_below(100, 110, 0.4, 90)
        assert a is not None and b is not None
        assert a + b == pytest.approx(1.0)

    def test_rejects_nonsensical_inputs(self):
        assert cd.lognormal_above(0, 100, 0.5, 365) is None
        assert cd.lognormal_above(100, 0, 0.5, 365) is None
        assert cd.lognormal_above(100, 100, 0, 365) is None
        assert cd.lognormal_above(100, 100, 0.5, 0) is None


# ---------------------------------------------------------------------------
# Barrier-hit probabilities (the model the live edges actually use)
# ---------------------------------------------------------------------------

class TestBarrier:
    def test_above_already_at_barrier_is_one(self):
        assert cd.barrier_hit_above(110, 100, 0.5, 30) == 1.0
        assert cd.barrier_hit_above(100, 100, 0.5, 30) == 1.0

    def test_below_already_at_barrier_is_one(self):
        assert cd.barrier_hit_below(90, 100, 0.5, 30) == 1.0
        assert cd.barrier_hit_below(100, 100, 0.5, 30) == 1.0

    def test_above_dominates_lognormal_above(self):
        # The barrier (any-touch) probability must be ≥ the terminal one.
        spot, strike, vol, days = 100, 110, 0.5, 180
        b = cd.barrier_hit_above(spot, strike, vol, days)
        t = cd.lognormal_above(spot, strike, vol, days)
        assert b is not None and t is not None
        assert b >= t

    def test_below_dominates_lognormal_below(self):
        spot, strike, vol, days = 100, 90, 0.5, 180
        b = cd.barrier_hit_below(spot, strike, vol, days)
        t = cd.lognormal_below(spot, strike, vol, days)
        assert b is not None and t is not None
        assert b >= t

    def test_above_in_zero_one_band(self):
        p = cd.barrier_hit_above(100, 200, 0.4, 90)
        assert p is not None and 0.0 <= p <= 1.0

    def test_above_far_otm_low_vol_near_zero(self):
        p = cd.barrier_hit_above(100, 500, 0.1, 7)
        assert p is not None and p < 0.001

    def test_rejects_nonsensical_inputs(self):
        assert cd.barrier_hit_above(0, 100, 0.5, 365) is None
        assert cd.barrier_hit_above(100, 0, 0.5, 365) is None
        assert cd.barrier_hit_above(100, 100, 0, 365) is None
        assert cd.barrier_hit_above(100, 100, 0.5, 0) is None
        assert cd.barrier_hit_below(0, 100, 0.5, 365) is None
        assert cd.barrier_hit_below(100, 0, 0.5, 365) is None


# ---------------------------------------------------------------------------
# Module-level smoke test (doctest discovery for the lognormal section)
# is exercised by pytest --doctest-modules.
# ---------------------------------------------------------------------------
