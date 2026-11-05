"""Tests for the oscillation scanner.

Strategy: synthesize price series whose behavior we know analytically,
then verify the scanner's metrics rank them correctly. A pure linear
trend should score near zero; a pure oscillation should score high;
random-walk-with-drift should land in between."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from pm.research import oscillation_scanner as osc


def _series(prices: list[float], *, start="2026-01-01", freq="1h") -> pd.DataFrame:
    """Build a price-history DataFrame from a list of prices."""
    ts = pd.date_range(start, periods=len(prices), freq=freq, tz="UTC")
    return pd.DataFrame({"timestamp": ts, "price": prices})


class TestComputeMetrics:
    def test_returns_none_when_too_short(self):
        # min_history_points default is 20 — only 5 points here
        df = _series([0.5] * 5)
        assert osc.compute_metrics(df) is None

    def test_flat_series_has_zero_drift_and_zero_std(self):
        df = _series([0.5] * 40)
        m = osc.compute_metrics(df)
        assert m is not None
        assert m.drift_abs == pytest.approx(0.0, abs=1e-9)
        assert m.detrended_std == pytest.approx(0.0, abs=1e-9)
        assert m.mean_cross_freq == pytest.approx(0.0, abs=1e-9)

    def test_pure_trend_has_low_range_score(self):
        """Smooth linear ramp from 0.30 → 0.70. Drift dominates."""
        prices = list(np.linspace(0.30, 0.70, 40))
        m = osc.compute_metrics(_series(prices))
        assert m is not None
        assert m.range_score < 0.05, (
            f"linear trend should be drift-dominated, got "
            f"range_score={m.range_score}"
        )

    def test_pure_oscillation_has_high_range_score(self):
        """Sine wave around 0.50 with zero drift — pure oscillation.
        With drift ≈ 0 the epsilon in the denominator caps the score
        but it should still be much larger than a trending series."""
        t = np.linspace(0, 4 * np.pi, 200)  # ~2 full cycles
        prices = 0.50 + 0.15 * np.sin(t)
        m = osc.compute_metrics(_series(list(prices)))
        assert m is not None
        # 4π over 200 hourly points = ~8.3 days; ~4 sign changes →
        # ~0.5/day. Threshold loose enough to survive sampling-phase
        # edge effects (the polyfit picks up a small slope from
        # uneven start/end phase).
        assert m.mean_cross_freq > 0.3, (
            f"sine wave should cross its mean repeatedly, got "
            f"mean_cross_freq={m.mean_cross_freq}/day"
        )
        # And dwarfs a trending series's score. Using a moderate
        # threshold here — the strict ordering test below verifies
        # the trend-vs-oscillation gap quantitatively.
        assert m.range_score > 0.4

    def test_oscillation_score_exceeds_trend_score(self):
        """The whole point: a mean-reverting market should rank above
        a trending one when both have the same total price range."""
        n = 100
        # Trend: 0.30 → 0.70 (range = 0.40)
        trend_prices = list(np.linspace(0.30, 0.70, n))
        trend = osc.compute_metrics(_series(trend_prices))

        # Oscillation: same total range, but bouncy
        t = np.linspace(0, 4 * np.pi, n)
        osc_prices = list(0.50 + 0.20 * np.sin(t))
        oscill = osc.compute_metrics(_series(osc_prices))

        assert trend is not None and oscill is not None
        assert oscill.range_score > trend.range_score * 5, (
            f"oscillation should dominate; trend={trend.range_score}, "
            f"oscill={oscill.range_score}"
        )

    def test_metadata_passthrough(self):
        df = _series([0.4 + 0.05 * np.sin(i) for i in range(40)])
        m = osc.compute_metrics(
            df, condition_id="cid-1", question="Q?",
            category="Crypto", min_history_points=20,
        )
        assert m is not None
        assert m.condition_id == "cid-1"
        assert m.question == "Q?"
        assert m.category == "Crypto"

    def test_current_price_is_last(self):
        prices = [0.30 + 0.001 * i for i in range(40)]
        m = osc.compute_metrics(_series(prices))
        assert m is not None
        assert m.current_price == pytest.approx(prices[-1])

    def test_min_max_range_width(self):
        prices = [0.30, 0.50, 0.70, 0.40] * 10
        m = osc.compute_metrics(_series(prices))
        assert m is not None
        assert m.min_price == pytest.approx(0.30)
        assert m.max_price == pytest.approx(0.70)
        assert m.range_width == pytest.approx(0.40)


class TestScanMarkets:
    def _make_provider(self, history_map: dict[str, pd.DataFrame]):
        """Build a fake provider whose get_price_history serves from a dict."""
        fake = MagicMock()
        fake.get_price_history.side_effect = (
            lambda cid, **_: history_map.get(cid, pd.DataFrame())
        )
        return fake

    def test_empty_markets_returns_empty_df(self):
        df = osc.scan_markets(MagicMock(), pd.DataFrame())
        assert df.empty

    def test_ranks_bouncy_above_trending(self):
        df_markets = pd.DataFrame([
            {"condition_id": "trend", "question": "trending",
             "category": "Crypto"},
            {"condition_id": "bouncy", "question": "bouncy",
             "category": "Crypto"},
        ])
        n = 100
        history = {
            "trend": _series(list(np.linspace(0.30, 0.70, n))),
            "bouncy": _series(list(
                0.50 + 0.15 * np.sin(np.linspace(0, 4 * np.pi, n))
            )),
        }
        provider = self._make_provider(history)
        df = osc.scan_markets(provider, df_markets, days=30,
                              min_history_points=20)
        assert len(df) == 2
        # Bouncy comes first (highest range_score)
        assert df.iloc[0]["condition_id"] == "bouncy"
        assert df.iloc[1]["condition_id"] == "trend"

    def test_filters_boundary_markets(self):
        """A market glued at 0.99 has tiny std but the score is
        meaningless. min_avg_price/max_avg_price should drop it."""
        df_markets = pd.DataFrame([
            {"condition_id": "near_one", "question": "q", "category": "C"},
        ])
        n = 40
        history = {"near_one": _series([0.98 + 0.001 * i for i in range(n)])}
        provider = self._make_provider(history)
        df = osc.scan_markets(provider, df_markets,
                              min_avg_price=0.05, max_avg_price=0.95)
        assert df.empty

    def test_handles_provider_error_per_market(self):
        """One broken market doesn't poison the rest of the scan."""
        df_markets = pd.DataFrame([
            {"condition_id": "broken", "question": "q1", "category": "C"},
            {"condition_id": "ok", "question": "q2", "category": "C"},
        ])
        fake = MagicMock()
        def _hist(cid, **_):
            if cid == "broken":
                raise RuntimeError("API down")
            return _series([0.50 + 0.05 * np.sin(i) for i in range(40)])
        fake.get_price_history.side_effect = _hist
        df = osc.scan_markets(fake, df_markets)
        assert len(df) == 1
        assert df.iloc[0]["condition_id"] == "ok"

    def test_progress_callback_invoked(self):
        df_markets = pd.DataFrame([
            {"condition_id": "m1", "question": "q", "category": "C"},
            {"condition_id": "m2", "question": "q", "category": "C"},
        ])
        history = {
            "m1": _series([0.50 + 0.05 * np.sin(i) for i in range(40)]),
            "m2": _series([0.50 + 0.05 * np.sin(i) for i in range(40)]),
        }
        provider = self._make_provider(history)
        calls: list[tuple[int, int]] = []
        osc.scan_markets(provider, df_markets,
                         progress_callback=lambda i, t: calls.append((i, t)))
        assert calls == [(1, 2), (2, 2)]

    def test_respects_max_markets(self):
        df_markets = pd.DataFrame([
            {"condition_id": f"m{i}", "question": "q", "category": "C"}
            for i in range(10)
        ])
        history = {
            f"m{i}": _series([0.50 + 0.05 * np.sin(j) for j in range(40)])
            for i in range(10)
        }
        provider = self._make_provider(history)
        df = osc.scan_markets(provider, df_markets, max_markets=3)
        assert len(df) == 3
        assert set(df["condition_id"]) <= {"m0", "m1", "m2"}
