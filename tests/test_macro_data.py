"""Tests for the FRED macro-data client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from pm.feeds import macro_data as md

# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

class TestGetObservations:
    @patch.object(md, "_get_csv")
    def test_parses_fred_csv(self, mock_get):
        mock_get.return_value = (
            "observation_date,UNRATE\n"
            "2026-01-01,4.0\n"
            "2026-02-01,4.1\n"
            "2026-03-01,4.2\n"
            "2026-04-01,4.3\n"
        )
        df = md.get_observations("UNRATE")
        assert len(df) == 4
        assert list(df.columns) == ["date", "value"]
        assert df.iloc[-1]["value"] == pytest.approx(4.3)
        assert df.iloc[-1]["date"].date().isoformat() == "2026-04-01"

    @patch.object(md, "_get_csv")
    def test_skips_missing_observations(self, mock_get):
        # FRED encodes gaps as "."
        mock_get.return_value = (
            "observation_date,DGS10\n"
            "2026-01-01,4.0\n"
            "2026-01-02,.\n"
            "2026-01-03,4.1\n"
        )
        df = md.get_observations("DGS10")
        assert len(df) == 2

    @patch.object(md, "_get_csv", return_value=None)
    def test_fetch_failure_returns_empty(self, _g):
        df = md.get_observations("UNRATE")
        assert df.empty

    @patch.object(md, "_get_csv", return_value="")
    def test_empty_body_returns_empty(self, _g):
        df = md.get_observations("UNRATE")
        assert df.empty

    @patch.object(md, "_get_csv")
    def test_header_only_returns_empty(self, mock_get):
        mock_get.return_value = "observation_date,UNRATE\n"
        df = md.get_observations("UNRATE")
        assert df.empty


# ---------------------------------------------------------------------------
# latest_value
# ---------------------------------------------------------------------------

class TestLatestValue:
    @patch.object(md, "get_observations")
    def test_returns_last_row(self, mock_obs):
        mock_obs.return_value = pd.DataFrame({
            "date": [datetime(2026, 3, 1, tzinfo=UTC),
                     datetime(2026, 4, 1, tzinfo=UTC)],
            "value": [4.1, 4.3],
        })
        result = md.latest_value("UNRATE")
        assert result is not None
        value, when = result
        assert value == pytest.approx(4.3)
        assert when.date().isoformat() == "2026-04-01"

    @patch.object(md, "get_observations", return_value=pd.DataFrame())
    def test_empty_returns_none(self, _o):
        assert md.latest_value("UNRATE") is None


class TestValueAt:
    """Historical at-or-before lookup used by the backtest strategy."""

    @patch.object(md, "get_observations")
    def test_returns_at_or_before(self, mock_obs):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        mock_obs.return_value = pd.DataFrame({
            "date": [base + timedelta(days=i * 30) for i in range(6)],
            "value": [4.0, 4.1, 4.2, 4.3, 4.4, 4.5],
        })
        # 3 months in → at-or-before is the 4th sample (4.2)
        v = md.value_at("UNRATE", base + timedelta(days=75))
        assert v is not None
        value, when = v
        # 75 days lands between the day-60 and day-90 samples → at-or-before
        # is day-60 (value 4.2)
        assert value == pytest.approx(4.2)
        assert when.date().isoformat() == (base + timedelta(days=60)).date().isoformat()

    @patch.object(md, "get_observations")
    def test_exactly_on_observation_includes_it(self, mock_obs):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        mock_obs.return_value = pd.DataFrame({
            "date": [base, base + timedelta(days=30)],
            "value": [4.0, 4.1],
        })
        # as_of exactly equals the day-30 observation → returns it
        v = md.value_at("UNRATE", base + timedelta(days=30))
        assert v is not None
        assert v[0] == pytest.approx(4.1)

    @patch.object(md, "get_observations")
    def test_before_all_observations_returns_none(self, mock_obs):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        mock_obs.return_value = pd.DataFrame({
            "date": [base + timedelta(days=30)],
            "value": [4.0],
        })
        # as_of is BEFORE the only observation → no historical value
        v = md.value_at("UNRATE", base)
        assert v is None

    @patch.object(md, "get_observations")
    def test_does_not_include_future_observations(self, mock_obs):
        # The whole point: this is what makes backtests honest. If FRED
        # has data through 2026-06-01 but the snapshot's as_of is
        # 2026-03-01, value_at must return the March data, not June.
        base = datetime(2026, 1, 1, tzinfo=UTC)
        mock_obs.return_value = pd.DataFrame({
            "date": [base + timedelta(days=i * 30) for i in range(6)],
            "value": [4.0, 4.1, 4.2, 4.3, 4.4, 4.5],
        })
        # Snapshot at day 90 must NOT see day-120, day-150, day-180.
        v = md.value_at("UNRATE", base + timedelta(days=90))
        assert v is not None
        # The day-90 sample exists in the fixture → at-or-before returns it
        assert v[0] == pytest.approx(4.3)
        assert v[0] < 4.4   # explicitly NOT seeing future data

    @patch.object(md, "get_observations", return_value=pd.DataFrame())
    def test_empty_returns_none(self, _o):
        assert md.value_at("UNRATE", datetime(2026, 1, 1, tzinfo=UTC)) is None


class TestRealizedVolatilityAt:
    @patch.object(md, "get_observations")
    def test_uses_only_pre_as_of_data(self, mock_obs):
        # Build a series with low-vol pre-2026 and high-vol post-2026.
        # Vol computed as_of=2026-01-01 should reflect ONLY the low-vol
        # window, not the future spike.
        base = datetime(2024, 1, 1, tzinfo=UTC)
        dates = [base + timedelta(days=i * 30) for i in range(40)]
        # First 24 samples: stable around 4.0. Next 16: wild swings.
        values = [4.0 + 0.01 * (i % 2) for i in range(24)] + \
                 [4.0 + 1.0 * (i % 2) for i in range(16)]
        mock_obs.return_value = pd.DataFrame({"date": dates, "value": values})

        # as_of in the calm window → low vol
        as_of_calm = base + timedelta(days=24 * 30 - 1)
        v_calm = md.realized_volatility_at("unemployment", as_of_calm, window=20)
        # as_of well into the wild window → high vol
        as_of_wild = base + timedelta(days=38 * 30)
        v_wild = md.realized_volatility_at("unemployment", as_of_wild, window=20)

        assert v_calm is not None and v_wild is not None
        assert v_wild > 10 * v_calm   # wild regime is dramatically more volatile

    def test_unknown_series_returns_none(self):
        assert md.realized_volatility_at(
            "totally-fake", datetime(2026, 1, 1, tzinfo=UTC),
        ) is None

    @patch.object(md, "get_observations", return_value=pd.DataFrame())
    def test_empty_returns_none(self, _o):
        assert md.realized_volatility_at(
            "unemployment", datetime(2026, 1, 1, tzinfo=UTC),
        ) is None


# ---------------------------------------------------------------------------
# Realized volatility
# ---------------------------------------------------------------------------

class TestRealizedVolatility:
    def _df(self, values: list[float]) -> pd.DataFrame:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        return pd.DataFrame({
            "date": [base + timedelta(days=i * 30) for i in range(len(values))],
            "value": values,
        })

    def test_zero_diffs_returns_none(self):
        df = self._df([4.0] * 30)
        assert md.realized_volatility(df, cadence="monthly", window=24) is None

    def test_monthly_annualization(self):
        # Alternating +0.1 / -0.1 monthly → std ≈ 0.1, ×√12 ≈ 0.346
        df = self._df([4.0 + 0.1 * (i % 2) for i in range(30)])
        v = md.realized_volatility(df, cadence="monthly", window=24)
        assert v is not None
        assert 0.30 < v < 0.40

    def test_daily_annualization(self):
        df = self._df([4.0 + 0.1 * (i % 2) for i in range(30)])
        v_d = md.realized_volatility(df, cadence="daily", window=24)
        v_m = md.realized_volatility(df, cadence="monthly", window=24)
        assert v_d is not None and v_m is not None
        # √252 / √12 ≈ 4.58 — daily annualization is ~4.6× larger
        assert v_d / v_m == pytest.approx(4.58, abs=0.05)

    def test_insufficient_data_returns_none(self):
        df = self._df([4.0, 4.1, 4.2])
        assert md.realized_volatility(df, cadence="monthly", window=24) is None


# ---------------------------------------------------------------------------
# Normal-on-level probabilities
# ---------------------------------------------------------------------------

class TestNormalAbove:
    def test_above_threshold_returns_high_prob(self):
        # Level $5.5, threshold $5.0, 2% vol, 6 months — should be > 50%
        p = md.normal_above(5.5, 5.0, 2.0, 180)
        assert p is not None and p > 0.5

    def test_below_threshold_returns_low_prob(self):
        # Level $4.3, threshold $5.0, 2% vol, 6 months — should be < 50%
        p = md.normal_above(4.3, 5.0, 2.0, 180)
        assert p is not None and p < 0.5

    def test_at_threshold_with_no_drift_returns_half(self):
        p = md.normal_above(5.0, 5.0, 2.0, 180)
        assert p == pytest.approx(0.5, abs=1e-6)

    def test_below_complements_above(self):
        a = md.normal_above(4.5, 5.0, 2.0, 180)
        b = md.normal_below(4.5, 5.0, 2.0, 180)
        assert a is not None and b is not None
        assert a + b == pytest.approx(1.0)

    def test_drift_shifts_distribution(self):
        # Upward drift pushes mass past the threshold
        p_no_drift = md.normal_above(4.5, 5.0, 2.0, 180)
        p_with_drift = md.normal_above(4.5, 5.0, 2.0, 180, drift=2.0)
        assert p_with_drift is not None and p_no_drift is not None
        assert p_with_drift > p_no_drift

    def test_rejects_nonsense_inputs(self):
        assert md.normal_above(4.5, 5.0, 0, 180) is None
        assert md.normal_above(4.5, 5.0, -1, 180) is None
        assert md.normal_above(4.5, 5.0, 2.0, 0) is None
        assert md.normal_above(4.5, 5.0, 2.0, -10) is None


# ---------------------------------------------------------------------------
# Series registry
# ---------------------------------------------------------------------------

class TestSeriesRegistry:
    def test_known_keys(self):
        assert {"unemployment", "10y_treasury", "2y_treasury", "fed_funds"} <= set(md.SERIES.keys())

    def test_each_meta_has_required_fields(self):
        for meta in md.SERIES.values():
            assert meta.series_id
            assert meta.cadence in {"monthly", "daily"}
            assert meta.display_name
