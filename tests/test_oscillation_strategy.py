"""Tests for the oscillation-based mean-reversion strategy and the
strategy_runner's history-fetch integration."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from pm.arms import strategy_runner as sr
from pm.research import backtest as bt
from pm.research import oscillation_scanner as osc


@pytest.fixture(autouse=True)
def _isolated_profile(tmp_path, monkeypatch):
    """Mirror test_strategy_runner_exits.py's isolation — every test
    gets its own portfolio/forecasts/alerts/meta files."""
    monkeypatch.setenv("POLYMARKET_PORTFOLIO_FILE",
                       str(tmp_path / "portfolio.json"))
    monkeypatch.setenv("POLYMARKET_FORECASTS_FILE",
                       str(tmp_path / "forecasts.json"))
    monkeypatch.setenv("POLYMARKET_ALERTS_FILE",
                       str(tmp_path / "alerts.json"))
    monkeypatch.setenv("POLYMARKET_POSITION_META_FILE",
                       str(tmp_path / "position_meta.json"))
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "llm_cache.sqlite"))
    monkeypatch.setattr(
        sr, "_exits_log_path",
        lambda profile_name: tmp_path / "exits.jsonl",
    )


def _history(prices: list[float], *, days: int = 14) -> pd.DataFrame:
    n = len(prices)
    end = pd.Timestamp.now(tz="UTC")
    ts = pd.date_range(end - pd.Timedelta(days=days), end,
                       periods=n, tz="UTC")
    return pd.DataFrame({"timestamp": ts, "price": prices})


def _snapshot(prices_df: pd.DataFrame, *, cid="m1", question="Q?",
              category="Politics") -> bt.MarketSnapshot:
    """Build a MarketSnapshot for the strategy under test."""
    return bt.MarketSnapshot(
        condition_id=cid, question=question, category=category,
        as_of=datetime.now(UTC),
        prices=prices_df,
        end_date=None,
    )


# ─── strategy_oscillation_revert ───────────────────────────────────────


class TestStrategyOscillationRevert:
    def test_emits_yes_when_price_far_below_mean_in_bouncy_market(self):
        """Sinewave around 0.50 with current price pushed down to 0.30
        → ~3σ below mean → YES signal at (mean + edge).

        Filter thresholds relaxed for test isolation — these tests
        validate the strategy's *logic*, not the calibration of its
        default thresholds (which are tuned for live Kalshi markets)."""
        n = 200
        # 8 cycles over the window → high mean-cross frequency
        base = 0.50 + 0.10 * np.sin(np.linspace(0, 16 * np.pi, n - 1))
        prices = list(base) + [0.30]
        snap = _snapshot(_history(prices))
        sig = osc.strategy_oscillation_revert(
            snap, z_threshold=1.0, min_cross_freq=0.3,
        )
        assert sig is not None
        assert sig.direction == "YES"
        # Probability anchored on mean + edge ≈ 0.50 + 0.05
        assert 0.45 < sig.probability < 0.60

    def test_emits_no_when_price_far_above_mean(self):
        n = 200
        base = 0.50 + 0.10 * np.sin(np.linspace(0, 16 * np.pi, n - 1))
        prices = list(base) + [0.70]  # high outlier
        snap = _snapshot(_history(prices))
        sig = osc.strategy_oscillation_revert(
            snap, z_threshold=1.0, min_cross_freq=0.3,
        )
        assert sig is not None
        assert sig.direction == "NO"

    def test_returns_none_in_trending_market(self):
        """Pure linear trend should fail the range_score filter."""
        prices = list(np.linspace(0.30, 0.70, 200))
        snap = _snapshot(_history(prices))
        sig = osc.strategy_oscillation_revert(
            snap, min_range_score=0.5,
        )
        assert sig is None

    def test_returns_none_when_price_near_mean(self):
        """Bouncy market but current price is at the mean → no signal."""
        n = 200
        base = 0.50 + 0.10 * np.sin(np.linspace(0, 16 * np.pi, n - 1))
        prices = list(base) + [0.50]
        snap = _snapshot(_history(prices))
        sig = osc.strategy_oscillation_revert(
            snap, z_threshold=1.0, min_cross_freq=0.3,
        )
        assert sig is None

    def test_returns_none_for_glued_to_boundary(self):
        prices = [0.97 + 0.005 * np.sin(i) for i in range(100)]
        snap = _snapshot(_history(prices))
        sig = osc.strategy_oscillation_revert(
            snap, min_avg_price=0.05, max_avg_price=0.95,
        )
        assert sig is None

    def test_no_history_returns_none(self):
        snap = bt.MarketSnapshot(
            condition_id="m1", question="Q?", category="C",
            as_of=datetime.now(UTC),
            prices=pd.DataFrame({"timestamp": [], "price": []}),
            end_date=None,
        )
        assert osc.strategy_oscillation_revert(snap) is None


# ─── strategy_runner history-fetch integration ─────────────────────────


class TestRunnerHistoryFetch:
    def test_build_snapshot_fetches_history_when_configured(self):
        """When provider + history_days are passed, the snapshot's
        `prices` column should be populated from get_price_history."""
        fake_provider = MagicMock()
        history = _history([0.50, 0.52, 0.48, 0.51, 0.49, 0.50])
        fake_provider.get_price_history.return_value = history
        row = pd.Series({
            "condition_id": "m1", "question": "Q?", "category": "C",
            "outcome_prices": [0.50, 0.50], "end_date": None,
        })
        snap = sr._build_snapshot_for_market(
            row, provider=fake_provider, history_days=14,
        )
        assert snap is not None
        assert len(snap.prices) == 6
        fake_provider.get_price_history.assert_called_once_with(
            "m1", days=14, fidelity=60,
        )

    def test_build_snapshot_falls_back_on_history_fetch_failure(self):
        fake_provider = MagicMock()
        fake_provider.get_price_history.side_effect = RuntimeError("API down")
        row = pd.Series({
            "condition_id": "m1", "question": "Q?", "category": "C",
            "outcome_prices": [0.40, 0.60], "end_date": None,
        })
        snap = sr._build_snapshot_for_market(
            row, provider=fake_provider, history_days=14,
        )
        # Falls back to the one-row "current price" history
        assert snap is not None
        assert len(snap.prices) == 1
        assert snap.prices.iloc[0]["price"] == pytest.approx(0.40)

    def test_build_snapshot_skips_history_when_days_zero(self):
        """history_days=0 (the default) should keep the legacy
        one-row-history behavior — no extra API calls."""
        fake_provider = MagicMock()
        row = pd.Series({
            "condition_id": "m1", "question": "Q?", "category": "C",
            "outcome_prices": [0.40, 0.60], "end_date": None,
        })
        snap = sr._build_snapshot_for_market(
            row, provider=fake_provider, history_days=0,
        )
        assert snap is not None
        assert len(snap.prices) == 1
        fake_provider.get_price_history.assert_not_called()

    @patch.object(sr.provider_mod, "get_provider")
    @patch.object(sr.provider_mod, "list_providers", return_value=[])
    def test_run_cycle_passes_history_through(
        self, _list, mock_get_provider,
    ):
        """End-to-end: run_cycle with history_days set should populate
        the snapshot history, and the registered strategy should see
        a real history DataFrame."""
        n = 200
        # Bouncy market with a current low price (8 cycles for high
        # mean-crossing frequency)
        base = 0.50 + 0.10 * np.sin(np.linspace(0, 16 * np.pi, n - 1))
        history_prices = list(base) + [0.30]
        fake_history = _history(history_prices)

        fake = MagicMock()
        fake.get_markets.return_value = pd.DataFrame([{
            "condition_id": "m1", "id": "m1", "question": "Q?",
            "category": "Politics",
            "outcome_prices": [0.30, 0.70], "best_bid": 0.29,
            "best_ask": 0.31, "end_date": None,
        }])
        fake.get_price_history.return_value = fake_history
        mock_get_provider.return_value = fake

        # Use the registered oscillation_revert strategy
        try:
            stats = sr.run_cycle(
                strategy_key="oscillation_revert",
                bankroll=1000.0, kelly_multiplier=0.5,
                half_spread=0.0, max_position_pct=0.10,
                min_gap=0.05, max_markets=10,
                profile_name="test_profile",
                history_days=14,
            )
        except Exception:
            raise

        # Should have fired one signal and one buy
        assert stats.signals_emitted == 1
        assert stats.positions_opened == 1
        # Confirm history was actually fetched
        fake.get_price_history.assert_called()


# ─── strategy_oscillation_momentum (inverted) ──────────────────────────


class TestStrategyOscillationMomentum:
    """The inverted variant: bet WITH the direction the market moved.
    Same scanner gates as oscillation_revert; opposite direction;
    probability anchored on current price, not mean."""

    def test_emits_yes_when_price_far_above_mean(self):
        """In a bouncy market with current price pushed UP to 0.70 →
        +3σ above mean → YES (momentum continues up to 1)."""
        n = 200
        base = 0.50 + 0.10 * np.sin(np.linspace(0, 16 * np.pi, n - 1))
        prices = list(base) + [0.70]
        snap = _snapshot(_history(prices))
        sig = osc.strategy_oscillation_momentum(
            snap, z_threshold=1.0, min_cross_freq=0.3,
        )
        assert sig is not None
        assert sig.direction == "YES"
        # Probability anchored on current price + edge ≈ 0.70 + 0.05
        assert sig.probability == pytest.approx(0.75, abs=0.02)

    def test_emits_no_when_price_far_below_mean(self):
        """Current price pushed DOWN to 0.30 → NO (momentum continues
        down to 0). NO-side probability = (1 - 0.30) + 0.05 = 0.75."""
        n = 200
        base = 0.50 + 0.10 * np.sin(np.linspace(0, 16 * np.pi, n - 1))
        prices = list(base) + [0.30]
        snap = _snapshot(_history(prices))
        sig = osc.strategy_oscillation_momentum(
            snap, z_threshold=1.0, min_cross_freq=0.3,
        )
        assert sig is not None
        assert sig.direction == "NO"
        assert sig.probability == pytest.approx(0.75, abs=0.02)

    def test_returns_none_when_price_near_mean(self):
        n = 200
        base = 0.50 + 0.10 * np.sin(np.linspace(0, 16 * np.pi, n - 1))
        prices = list(base) + [0.50]
        snap = _snapshot(_history(prices))
        sig = osc.strategy_oscillation_momentum(
            snap, z_threshold=1.0, min_cross_freq=0.3,
        )
        assert sig is None

    def test_returns_none_in_trending_market(self):
        """Pure trend fails the range_score filter."""
        prices = list(np.linspace(0.30, 0.70, 200))
        snap = _snapshot(_history(prices))
        sig = osc.strategy_oscillation_momentum(snap, min_range_score=0.5)
        assert sig is None

    def test_direction_is_opposite_of_revert(self):
        """The whole point: same trade selection, opposite direction
        from oscillation_revert."""
        n = 200
        # Price 2σ ABOVE mean
        base = 0.50 + 0.10 * np.sin(np.linspace(0, 16 * np.pi, n - 1))
        prices = list(base) + [0.70]
        snap = _snapshot(_history(prices))
        revert_sig = osc.strategy_oscillation_revert(
            snap, z_threshold=1.0, min_cross_freq=0.3,
        )
        momentum_sig = osc.strategy_oscillation_momentum(
            snap, z_threshold=1.0, min_cross_freq=0.3,
        )
        assert revert_sig is not None and momentum_sig is not None
        # Revert says NO (bet against the up-move); momentum says YES
        assert revert_sig.direction == "NO"
        assert momentum_sig.direction == "YES"

    def test_registered_in_strategy_registry(self):
        assert "oscillation_momentum" in bt.STRATEGIES
        name, fn = bt.STRATEGIES["oscillation_momentum"]
        assert fn is osc.strategy_oscillation_momentum
        # Display name flags it as paper-trading
        assert "PAPER" in name
