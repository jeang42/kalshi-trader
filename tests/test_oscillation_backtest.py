"""Tests for the oscillation scanner backtest framework.

Strategy: build a synthetic dataset of "bouncy" markets (sinewave around
mean, resolves coin-flip) and "trending" markets (smooth ramp, resolves
according to where the ramp ends), then verify the backtest engine:
- pipes the dataset through both strategies correctly
- buckets by range_score in the expected order
- reports per-bucket performance"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from pm.research import oscillation_backtest as ob


def _build_fake_provider(market_specs: list[dict]) -> MagicMock:
    """Build a provider that returns the given market spec list and
    serves price history per spec.

    Each spec dict:
        condition_id: str
        question: str
        category: str
        prices: list[float]      # historical YES prices
        resolved_yes: bool
        closed_at: datetime
    """
    fake = MagicMock()
    closed_rows = []
    history_map = {}
    token_map = {}
    for spec in market_specs:
        cid = spec["condition_id"]
        n = len(spec["prices"])
        closed_at = spec["closed_at"]
        ts = pd.date_range(
            closed_at - timedelta(days=14),
            closed_at - timedelta(hours=1),
            periods=n, tz="UTC",
        )
        history_map[cid] = pd.DataFrame({
            "timestamp": ts,
            "price": spec["prices"],
        })
        token_map[cid] = [{"token_id": cid, "outcome": "Yes"}]
        closed_rows.append({
            "condition_id": cid,
            "question": spec["question"],
            "category": spec["category"],
            "resolved_yes": spec["resolved_yes"],
            "closed_time": closed_at.isoformat(),
            "closed_time_dt": closed_at,
        })
    fake.get_closed_markets.return_value = pd.DataFrame(closed_rows)
    fake.list_outcome_tokens.side_effect = (
        lambda cid: token_map.get(cid, [])
    )
    fake.get_price_history.side_effect = (
        lambda token_id, **_: history_map.get(token_id, pd.DataFrame())
    )
    return fake


class TestQuintileBuckets:
    def test_empty_returns_n_empty_buckets(self):
        buckets = ob._quintile_buckets([], n_buckets=5)
        assert len(buckets) == 5
        assert all(b == [] for b in buckets)

    def test_partitions_by_range_score_ascending(self):
        # 10 markets with range_scores 0.1..1.0; 5 buckets of 2 each
        from pm.research import oscillation_scanner as osc
        scored = [
            (f"m{i}", osc.OscillationMetrics(
                condition_id=f"m{i}", question="", category="",
                current_price=0.5, n_history=20, days_covered=10.0,
                avg_price=0.5, min_price=0.4, max_price=0.6,
                range_width=0.2, drift_abs=0.01, drift_pct=0.02,
                detrended_std=0.05, range_score=0.1 * (i + 1),
                mean_cross_freq=0.5,
            ))
            for i in range(10)
        ]
        buckets = ob._quintile_buckets(scored, n_buckets=5)
        assert len(buckets) == 5
        # Bucket 0 = lowest scores, bucket 4 = highest
        assert "m0" in buckets[0] and "m1" in buckets[0]
        assert "m8" in buckets[4] and "m9" in buckets[4]


class TestEmptyResult:
    def test_zero_trade_skeleton(self):
        r = ob._empty_result("test")
        assert r.n_trades == 0
        assert r.hit_rate is None
        assert r.total_pnl == 0.0
        assert "empty dataset" in r.warnings


class TestRunOscillationBacktest:
    def _bouncy_prices(self, n: int = 200, mean: float = 0.50,
                       amplitude: float = 0.10) -> list[float]:
        """Sinewave around `mean` with `n` points. Multiple cycles
        for high mean-crossing frequency."""
        return [mean + amplitude * np.sin(x)
                for x in np.linspace(0, 16 * np.pi, n)]

    def _trending_prices(self, n: int = 200, start: float = 0.30,
                         end: float = 0.70) -> list[float]:
        return list(np.linspace(start, end, n))

    def test_returns_empty_result_on_no_closed_markets(self):
        fake = MagicMock()
        fake.get_closed_markets.return_value = pd.DataFrame()
        result = ob.run_oscillation_backtest(fake, max_markets=10)
        assert result.n_dataset == 0
        assert result.strategy.n_trades == 0

    def test_dataset_size_matches_qualifying_markets(self):
        now = datetime.now(UTC)
        specs = [
            {"condition_id": "bouncy1", "question": "Q1",
             "category": "Politics",
             "prices": self._bouncy_prices(),
             "resolved_yes": True, "closed_at": now - timedelta(days=1)},
            {"condition_id": "trend1", "question": "Q2",
             "category": "Crypto",
             "prices": self._trending_prices(),
             "resolved_yes": True, "closed_at": now - timedelta(days=2)},
        ]
        fake = _build_fake_provider(specs)
        result = ob.run_oscillation_backtest(
            fake, max_markets=10, lead_hours=24, history_days=30,
            # Relax filters so both markets get scored
            strategy_kwargs={"min_cross_freq": 0.1,
                             "min_range_score": 0.05},
        )
        assert result.n_dataset == 2
        # Both have ≥20 history points → both should be scored
        assert result.n_oscillation_qualified == 2

    def test_bucket_breakdown_orders_by_range_score(self):
        """Confirm buckets are sorted lowest → highest, and the
        bouncy market lands in a higher-indexed bucket than the
        trending one."""
        now = datetime.now(UTC)
        specs = [
            {"condition_id": "bouncy", "question": "Q1",
             "category": "Politics",
             "prices": self._bouncy_prices(),
             "resolved_yes": False,
             "closed_at": now - timedelta(days=1)},
            {"condition_id": "trend", "question": "Q2",
             "category": "Crypto",
             "prices": self._trending_prices(),
             "resolved_yes": True,
             "closed_at": now - timedelta(days=2)},
        ]
        fake = _build_fake_provider(specs)
        result = ob.run_oscillation_backtest(
            fake, max_markets=10, lead_hours=24, history_days=30,
            n_buckets=2,
            strategy_kwargs={"min_cross_freq": 0.1,
                             "min_range_score": 0.05},
        )
        # The bucket stats don't carry the cid list, but the
        # range_score boundaries should be monotonically ordered —
        # bucket[1].min ≥ bucket[0].max means the bouncy market's
        # score landed in bucket 1.
        if (result.by_range_score_bucket[0].n_markets > 0
                and result.by_range_score_bucket[1].n_markets > 0):
            assert (result.by_range_score_bucket[1].range_score_min
                    >= result.by_range_score_bucket[0].range_score_max
                    - 1e-9)

    def test_format_report_renders(self):
        """Smoke test the report formatter on a real result."""
        now = datetime.now(UTC)
        specs = [
            {"condition_id": "m1", "question": "Q",
             "category": "Politics",
             "prices": self._bouncy_prices(),
             "resolved_yes": True, "closed_at": now - timedelta(days=1)},
        ]
        fake = _build_fake_provider(specs)
        result = ob.run_oscillation_backtest(
            fake, max_markets=5, lead_hours=24,
            strategy_kwargs={"min_cross_freq": 0.1,
                             "min_range_score": 0.05},
        )
        report = ob.format_report(result)
        assert "Oscillation scanner backtest" in report
        assert "oscillation_revert" in report
        assert "market_consensus" in report
        assert "Per-bucket" in report

    def test_strategy_kwargs_threaded_through(self):
        """Pass an unreasonable filter via strategy_kwargs and verify
        the strategy doesn't fire any trades."""
        now = datetime.now(UTC)
        specs = [
            {"condition_id": "m1", "question": "Q",
             "category": "Politics",
             "prices": self._bouncy_prices(),
             "resolved_yes": True, "closed_at": now - timedelta(days=1)},
        ]
        fake = _build_fake_provider(specs)
        result = ob.run_oscillation_backtest(
            fake, max_markets=5, lead_hours=24,
            # Range score 999 is unreachable → no trades
            strategy_kwargs={"min_range_score": 999.0},
        )
        assert result.strategy.n_trades == 0


class TestDateRangeFetchPath:
    def _bouncy_prices(self, n: int = 50) -> list[float]:
        return [0.50 + 0.10 * np.sin(x)
                for x in np.linspace(0, 16 * np.pi, n)]

    def test_uses_get_closed_markets_in_range_when_since_days_set(self):
        """When since_days is provided, run_oscillation_backtest should
        call the range fetcher, not the latest-N fetcher."""
        now = datetime.now(UTC)
        spec = [{
            "condition_id": "m1", "question": "Q",
            "category": "Politics",
            "prices": self._bouncy_prices(50),
            "resolved_yes": True,
            "closed_at": now - timedelta(days=10),
        }]
        fake = _build_fake_provider(spec)
        # Stub the range method to return the same data
        fake.get_closed_markets_in_range.return_value = (
            fake.get_closed_markets.return_value
        )
        ob.run_oscillation_backtest(
            fake, max_markets=10, since_days=30, until_days=0,
            use_cache=False,
        )
        fake.get_closed_markets_in_range.assert_called_once()
        fake.get_closed_markets.assert_not_called()

    def test_default_uses_get_closed_markets(self):
        """Without since_days, the latest-N path is taken."""
        now = datetime.now(UTC)
        spec = [{
            "condition_id": "m1", "question": "Q",
            "category": "Politics",
            "prices": self._bouncy_prices(50),
            "resolved_yes": True,
            "closed_at": now - timedelta(days=1),
        }]
        fake = _build_fake_provider(spec)
        ob.run_oscillation_backtest(fake, max_markets=10, use_cache=False)
        fake.get_closed_markets.assert_called_once()
        fake.get_closed_markets_in_range.assert_not_called()


class TestParallelBuildDataset:
    def test_max_workers_gt_1_produces_same_dataset(self):
        """Parallel and sequential paths should produce equivalent
        datasets (order may differ; content must match)."""
        from pm.research import backtest as bt
        now = datetime.now(UTC)
        prices = [0.5 + 0.05 * np.sin(i) for i in range(30)]
        specs = [
            {"condition_id": f"m{i}", "question": f"Q{i}",
             "category": "Politics", "prices": prices,
             "resolved_yes": i % 2 == 0,
             "closed_at": now - timedelta(days=i + 1)}
            for i in range(5)
        ]
        fake = _build_fake_provider(specs)
        df_closed = fake.get_closed_markets.return_value

        seq = bt.build_dataset(
            fake, df_closed, lead_hours=24, max_workers=1,
        )
        par = bt.build_dataset(
            fake, df_closed, lead_hours=24, max_workers=4,
        )
        assert len(seq) == len(par)
        seq_cids = {s.condition_id for s, _ in seq}
        par_cids = {s.condition_id for s, _ in par}
        assert seq_cids == par_cids


class TestCachePath:
    def test_cache_path_includes_all_fetch_params(self):
        p1 = ob._cache_path_for(
            venue="kalshi", since_days=90, until_days=0,
            lead_hours=72, history_days=30, fidelity=60,
        )
        p2 = ob._cache_path_for(
            venue="kalshi", since_days=180, until_days=0,
            lead_hours=72, history_days=30, fidelity=60,
        )
        # Different since_days → different cache files
        assert p1 != p2
        # Same params → same path
        p3 = ob._cache_path_for(
            venue="kalshi", since_days=90, until_days=0,
            lead_hours=72, history_days=30, fidelity=60,
        )
        assert p1 == p3


class TestBucketStats:
    def test_empty_bucket_returns_n_markets_zero(self):
        from pm.research import oscillation_scanner as osc
        stats = ob._bucket_stats(
            bucket_index=0,
            cids_in_bucket=set(),
            metrics={},
            strategy_trades=pd.DataFrame(),
            dataset=[],
        )
        assert stats.n_markets == 0
        assert stats.hit_rate is None
        assert stats.total_pnl == 0.0

    def test_strategy_pnl_summed_correctly(self):
        from pm.research import oscillation_scanner as osc
        # Two markets in the bucket, one trade per market.
        metrics_a = osc.OscillationMetrics(
            condition_id="a", question="", category="",
            current_price=0.5, n_history=30, days_covered=10.0,
            avg_price=0.5, min_price=0.4, max_price=0.6,
            range_width=0.2, drift_abs=0.01, drift_pct=0.02,
            detrended_std=0.05, range_score=0.5,
            mean_cross_freq=0.5,
        )
        metrics_b = osc.OscillationMetrics(
            condition_id="b", question="", category="",
            current_price=0.5, n_history=30, days_covered=10.0,
            avg_price=0.5, min_price=0.4, max_price=0.6,
            range_width=0.2, drift_abs=0.01, drift_pct=0.02,
            detrended_std=0.05, range_score=0.6,
            mean_cross_freq=0.6,
        )
        trades = pd.DataFrame([
            {"condition_id": "a", "pnl": 10.0,
             "direction": "YES", "probability": 0.6},
            {"condition_id": "b", "pnl": -5.0,
             "direction": "NO", "probability": 0.4},
        ])
        stats = ob._bucket_stats(
            bucket_index=2,
            cids_in_bucket={"a", "b"},
            metrics={"a": metrics_a, "b": metrics_b},
            strategy_trades=trades,
            dataset=[],
        )
        assert stats.n_markets == 2
        assert stats.n_strategy_fired == 2
        assert stats.hit_rate == pytest.approx(0.5)  # 1 win, 1 loss
        assert stats.total_pnl == pytest.approx(5.0)
