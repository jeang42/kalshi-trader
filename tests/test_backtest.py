"""Tests for the backtest framework."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from pm.research import backtest as bt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ts_series(start: datetime, n: int, step_hours: float = 1.0) -> pd.Series:
    return pd.Series([start + timedelta(hours=step_hours * i) for i in range(n)])


def _price_history(prices: list[float], start: datetime) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": _ts_series(start, len(prices)),
        "price": prices,
    })


@pytest.fixture
def closed_df():
    """A 3-market closed_df like get_closed_markets() returns."""
    base_close = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    return pd.DataFrame([
        {
            "condition_id": "mkt-A",
            "question": "A?",
            "category": "Sports",
            "resolved_yes": True,
            "closed_time_dt": base_close,
        },
        {
            "condition_id": "mkt-B",
            "question": "B?",
            "category": "Politics",
            "resolved_yes": False,
            "closed_time_dt": base_close - timedelta(days=2),
        },
        {
            "condition_id": "mkt-C",
            "question": "C?",
            "category": "Sports",
            "resolved_yes": None,   # unresolved — should be skipped
            "closed_time_dt": base_close - timedelta(days=5),
        },
    ])


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.list_outcome_tokens.side_effect = lambda cid: [{"token_id": f"tok-{cid}", "outcome": "Yes"}]
    return p


# ---------------------------------------------------------------------------
# MarketSnapshot
# ---------------------------------------------------------------------------

class TestMarketSnapshot:
    def test_latest_price_returns_last_row(self):
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        prices = _price_history([0.3, 0.4, 0.55], as_of - timedelta(hours=3))
        snap = bt.MarketSnapshot("x", "q", "Sports", as_of, prices)
        assert snap.latest_price == pytest.approx(0.55)

    def test_latest_price_none_on_empty(self):
        snap = bt.MarketSnapshot(
            "x", "q", "Sports",
            datetime(2026, 5, 1, tzinfo=UTC),
            pd.DataFrame(columns=["timestamp", "price"]),
        )
        assert snap.latest_price is None

    def test_price_at_lookback(self):
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        # 5 hourly samples landing at as_of - 5h .. as_of - 1h
        prices = _price_history([0.10, 0.20, 0.30, 0.40, 0.50],
                                start=as_of - timedelta(hours=5))
        snap = bt.MarketSnapshot("x", "q", "Sports", as_of, prices)
        # 3h ago is at index 2 (price 0.30); nearest at-or-before
        assert snap.price_at_lookback(3) == pytest.approx(0.30)
        # 0.5h ago — nearest at-or-before is the most recent sample
        assert snap.price_at_lookback(0.5) == pytest.approx(0.50)
        # 10h ago — out of range, before all samples
        assert snap.price_at_lookback(10) is None


# ---------------------------------------------------------------------------
# build_dataset — including lookahead-bias prevention
# ---------------------------------------------------------------------------

class TestBuildDataset:
    def test_skips_unresolved_markets(self, closed_df, mock_provider):
        mock_provider.get_price_history.return_value = _price_history(
            [0.4, 0.5, 0.6], datetime(2026, 4, 25, tzinfo=UTC),
        )
        ds = bt.build_dataset(mock_provider, closed_df, lead_hours=12)
        cids = {snap.condition_id for snap, _ in ds}
        # mkt-C has resolved_yes=None and must be skipped
        assert "mkt-C" not in cids
        assert {"mkt-A", "mkt-B"} <= cids

    def test_snapshot_prices_strictly_before_as_of(self, closed_df, mock_provider):
        # Construct a history that has a candle exactly at as_of and beyond
        closed_at = closed_df.iloc[0]["closed_time_dt"]
        as_of = closed_at - timedelta(hours=24)
        prices = pd.DataFrame({
            "timestamp": [
                as_of - timedelta(hours=2),
                as_of - timedelta(hours=1),
                as_of,                          # exactly at as_of — must be excluded
                as_of + timedelta(hours=1),     # after as_of — must be excluded
            ],
            "price": [0.30, 0.40, 0.50, 0.60],
        })
        mock_provider.get_price_history.return_value = prices

        ds = bt.build_dataset(
            mock_provider, closed_df.head(1), lead_hours=24,
        )
        assert len(ds) == 1
        snap, _ = ds[0]
        # Only the two strictly-before-as_of samples survive
        assert list(snap.prices["price"]) == [0.30, 0.40]

    def test_resolution_carries_outcome_not_in_snapshot(self, closed_df, mock_provider):
        mock_provider.get_price_history.return_value = _price_history(
            [0.4, 0.5], datetime(2026, 4, 25, tzinfo=UTC),
        )
        ds = bt.build_dataset(mock_provider, closed_df, lead_hours=12)
        for snap, res in ds:
            assert not hasattr(snap, "resolved_yes")
            assert res.resolved_yes in (True, False)

    def test_skips_markets_with_no_price_history(self, closed_df, mock_provider):
        mock_provider.get_price_history.return_value = pd.DataFrame(columns=["timestamp", "price"])
        ds = bt.build_dataset(mock_provider, closed_df, lead_hours=12)
        assert ds == []

    def test_skips_markets_with_no_outcome_tokens(self, closed_df, mock_provider):
        mock_provider.list_outcome_tokens.side_effect = lambda cid: []
        ds = bt.build_dataset(mock_provider, closed_df, lead_hours=12)
        assert ds == []

    def test_max_markets_caps_dataset_size(self, closed_df, mock_provider):
        mock_provider.get_price_history.return_value = _price_history(
            [0.4, 0.5], datetime(2026, 4, 25, tzinfo=UTC),
        )
        ds = bt.build_dataset(mock_provider, closed_df, lead_hours=12, max_markets=1)
        # max_markets is applied *before* the resolution filter — so when the
        # head(1) row is resolved (mkt-A), we get one snapshot.
        assert len(ds) == 1

    def test_progress_callback_invoked(self, closed_df, mock_provider):
        mock_provider.get_price_history.return_value = _price_history(
            [0.4, 0.5], datetime(2026, 4, 25, tzinfo=UTC),
        )
        calls: list[tuple[int, int]] = []
        bt.build_dataset(
            mock_provider, closed_df, lead_hours=12,
            progress_callback=lambda i, n: calls.append((i, n)),
        )
        assert calls, "progress_callback should fire at least once"
        # Final call reports completion
        assert calls[-1][0] == calls[-1][1]


# ---------------------------------------------------------------------------
# Strategy library
# ---------------------------------------------------------------------------

class TestStrategies:
    def _snap(self, prices: list[float]) -> bt.MarketSnapshot:
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        return bt.MarketSnapshot(
            "x", "q", "Sports", as_of,
            _price_history(prices, start=as_of - timedelta(hours=len(prices))),
        )

    def test_market_consensus_picks_favorite(self):
        sig = bt.strategy_market_consensus(self._snap([0.30, 0.40, 0.70]))
        assert sig is not None
        assert sig.direction == "YES"
        # Probability is latest + edge, clamped
        assert sig.probability > 0.70

    def test_market_consensus_takes_no_when_below_half(self):
        sig = bt.strategy_market_consensus(self._snap([0.4, 0.3, 0.2]))
        assert sig is not None
        assert sig.direction == "NO"

    def test_market_consensus_none_on_empty_history(self):
        snap = bt.MarketSnapshot(
            "x", "q", "Sports",
            datetime(2026, 5, 1, tzinfo=UTC),
            pd.DataFrame(columns=["timestamp", "price"]),
        )
        assert bt.strategy_market_consensus(snap) is None

    def test_always_yes_flat_emits_signal_with_override(self):
        sig = bt.strategy_always_yes_flat(self._snap([0.3, 0.4, 0.5]))
        assert sig is not None
        assert sig.direction == "YES"
        assert sig.fraction_override == pytest.approx(0.01)

    def test_momentum_fires_on_upward_move(self):
        # 24-hourly samples climbing from 0.30 to 0.60 — strong YES move
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        prices = pd.DataFrame({
            "timestamp": [as_of - timedelta(hours=h) for h in range(96, 0, -1)],
            "price": [0.30 + (96 - h) * (0.30 / 96) for h in range(96, 0, -1)],
        })
        snap = bt.MarketSnapshot("x", "q", "Sports", as_of, prices)
        sig = bt.strategy_momentum(snap, lookback_hours=72, threshold=0.05)
        assert sig is not None
        assert sig.direction == "YES"

    def test_momentum_fires_on_downward_move(self):
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        prices = pd.DataFrame({
            "timestamp": [as_of - timedelta(hours=h) for h in range(96, 0, -1)],
            "price": [0.70 - (96 - h) * (0.30 / 96) for h in range(96, 0, -1)],
        })
        snap = bt.MarketSnapshot("x", "q", "Sports", as_of, prices)
        sig = bt.strategy_momentum(snap, lookback_hours=72, threshold=0.05)
        assert sig is not None
        assert sig.direction == "NO"

    def test_momentum_none_when_flat(self):
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        prices = pd.DataFrame({
            "timestamp": [as_of - timedelta(hours=h) for h in range(96, 0, -1)],
            "price": [0.50] * 96,
        })
        snap = bt.MarketSnapshot("x", "q", "Sports", as_of, prices)
        assert bt.strategy_momentum(snap, lookback_hours=72, threshold=0.05) is None

    def test_mean_reversion_high_extreme(self):
        sig = bt.strategy_mean_reversion(self._snap([0.50, 0.70, 0.90]), high=0.85, low=0.15)
        assert sig is not None
        assert sig.direction == "NO"

    def test_mean_reversion_low_extreme(self):
        sig = bt.strategy_mean_reversion(self._snap([0.20, 0.10, 0.05]), high=0.85, low=0.15)
        assert sig is not None
        assert sig.direction == "YES"

    def test_mean_reversion_none_in_band(self):
        sig = bt.strategy_mean_reversion(self._snap([0.40, 0.45, 0.50]), high=0.85, low=0.15)
        assert sig is None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_list_strategies_returns_pairs(self):
        items = bt.list_strategies()
        assert all(len(item) == 2 for item in items)
        keys = {k for k, _ in items}
        assert {"market_consensus", "momentum", "mean_reversion", "always_yes_flat"} <= keys

    def test_get_strategy_returns_callable(self):
        s = bt.get_strategy("momentum")
        assert callable(s)

    def test_get_strategy_unknown_raises(self):
        with pytest.raises(KeyError):
            bt.get_strategy("totally-fake")


# ---------------------------------------------------------------------------
# PnL accounting
# ---------------------------------------------------------------------------

class TestPnL:
    def _make_dataset(self, market_price: float, resolved_yes: bool,
                      *, category: str = "Sports") -> list:
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        snap = bt.MarketSnapshot(
            "x", "q", category, as_of,
            _price_history([market_price], start=as_of - timedelta(hours=1)),
        )
        res = bt.Resolution("x", resolved_yes, as_of + timedelta(hours=12))
        return [(snap, res)]

    def test_yes_bet_winning_payoff_is_correct(self):
        # YES at $0.40, resolves YES, half-Kelly, your_prob=0.50
        # kelly fraction (full) = (0.5 - 0.4) / (1 - 0.4) = 1/6 ≈ 0.1667
        # half kelly = 0.0833 → stake on $1000 bankroll = $83.33
        # payoff = stake * (1 - entry) / entry = 83.33 * 0.6 / 0.4 = 125.0
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50)

        # half_spread=0 to recover the original "ideal mid-fill" PnL
        # math these tests document.
        result = bt.run_backtest(
            strat, self._make_dataset(0.40, True),
            bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
        )
        assert result.n_trades == 1
        assert result.n_wins == 1
        assert result.trades.iloc[0]["pnl"] == pytest.approx(125.0, rel=1e-3)

    def test_yes_bet_losing_payoff_is_minus_stake(self):
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50)
        result = bt.run_backtest(
            strat, self._make_dataset(0.40, False),
            bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
        )
        # Stake ≈ 83.33; loss = -stake
        assert result.trades.iloc[0]["pnl"] == pytest.approx(-83.33, rel=1e-3)

    def test_no_bet_winning_payoff(self):
        # NO at market YES=0.70 → entry = 1-0.70 = 0.30, your_prob YES=0.50
        # kelly NO fraction = (0.7 - 0.5) / 0.7 ≈ 0.2857
        # half = 0.1429 → stake = $142.86
        # win payoff = stake * (1-0.3)/0.3 = 142.86 * 7/3 ≈ 333.33
        def strat(snap):
            return bt.Signal(direction="NO", probability=0.50)
        result = bt.run_backtest(
            strat, self._make_dataset(0.70, False),
            bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
        )
        assert result.n_wins == 1
        assert result.trades.iloc[0]["pnl"] == pytest.approx(333.33, rel=1e-3)

    def test_signal_disagreeing_with_kelly_skips_trade(self):
        # Market at 0.30, our prob 0.20 → Kelly says NO. If our signal says
        # YES anyway, no edge in that direction → skipped.
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.20)
        result = bt.run_backtest(
            strat, self._make_dataset(0.30, True),
            bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
        )
        assert result.n_trades == 0

    def test_fraction_override_skips_kelly(self):
        # Even with no edge, fraction_override forces a flat 5% bet
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50, fraction_override=0.05)
        result = bt.run_backtest(
            strat, self._make_dataset(0.50, True),
            bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
        )
        assert result.n_trades == 1
        # 5% of $1000 = $50 stake; win at price 0.5 → pnl = 50 * 0.5/0.5 = 50
        assert result.trades.iloc[0]["stake"] == pytest.approx(50.0)
        assert result.trades.iloc[0]["pnl"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Transaction costs (half-spread + per-trade fee)
# ---------------------------------------------------------------------------

class TestTransactionCosts:
    def _make_dataset(self, market_price: float, resolved_yes: bool,
                      *, category: str = "Sports") -> list:
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        snap = bt.MarketSnapshot(
            "x", "q", category, as_of,
            _price_history([market_price], start=as_of - timedelta(hours=1)),
        )
        res = bt.Resolution("x", resolved_yes, as_of + timedelta(hours=12))
        return [(snap, res)]

    def test_half_spread_reduces_payoff_on_win(self):
        # YES bet at 0.40 with 0.01 half-spread → effective entry 0.41.
        # Compared to a zero-spread run, payoff is smaller, fraction is
        # smaller, AND the spread_cost column is populated.
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50)
        ds = self._make_dataset(0.40, True)
        zero = bt.run_backtest(strat, ds, bankroll=1000, kelly_multiplier=0.5,
                                half_spread=0.0)
        costly = bt.run_backtest(strat, ds, bankroll=1000, kelly_multiplier=0.5,
                                  half_spread=0.01)
        assert costly.trades.iloc[0]["pnl"] < zero.trades.iloc[0]["pnl"]
        assert costly.trades.iloc[0]["spread_cost"] > 0
        # The implied spread cost rolls up at the result level too
        assert costly.implied_spread_cost > 0
        assert costly.half_spread == pytest.approx(0.01)

    def test_spread_eats_edge_skips_trade(self):
        # Your prob 0.42, market 0.40 → tiny edge; 0.05 half-spread
        # → effective entry 0.45 → edge becomes negative → no trade.
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.42)
        ds = self._make_dataset(0.40, True)
        result = bt.run_backtest(strat, ds, bankroll=1000, kelly_multiplier=0.5,
                                  half_spread=0.05)
        assert result.n_trades == 0

    def test_fee_pct_deducts_from_pnl(self):
        # 1% fee on a $50 stake → $0.50 deducted from pnl
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50,
                             fraction_override=0.05)
        ds = self._make_dataset(0.50, True)
        result = bt.run_backtest(strat, ds, bankroll=1000, kelly_multiplier=0.5,
                                  half_spread=0.0, fee_pct=0.01)
        trade = result.trades.iloc[0]
        # Without fee: pnl = 50 (50 stake × 0.5/0.5). With 1% fee: 50 - 0.5 = 49.5
        assert trade["fee"] == pytest.approx(0.5)
        assert trade["pnl"] == pytest.approx(49.5)
        assert result.total_fees == pytest.approx(0.5)
        assert result.fee_pct == pytest.approx(0.01)

    def test_zero_costs_matches_old_behaviour(self):
        # With half_spread=0 and fee_pct=0, the spread_cost column is
        # zero and pnl == gross_pnl. This is the "ideal-fill" baseline
        # all the pre-cost tests assume.
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50,
                             fraction_override=0.05)
        ds = self._make_dataset(0.50, True)
        result = bt.run_backtest(strat, ds, bankroll=1000, kelly_multiplier=0.5,
                                  half_spread=0.0, fee_pct=0.0)
        trade = result.trades.iloc[0]
        assert trade["spread_cost"] == pytest.approx(0.0)
        assert trade["fee"] == pytest.approx(0.0)
        assert trade["pnl"] == pytest.approx(trade["gross_pnl"])
        assert result.implied_spread_cost == pytest.approx(0.0)
        assert result.total_fees == pytest.approx(0.0)

    def test_default_costs_are_realistic_not_zero(self):
        # The default run_backtest call has non-zero half_spread (0.005)
        # so a strategy that nominally has zero edge can't sneak by with
        # an "ideal mid-fill" assumption.
        def strat(snap):
            # 0.5 prob on a 0.5 market → zero edge → no trade with any
            # nonzero half_spread.
            return bt.Signal(direction="YES", probability=0.50)
        ds = self._make_dataset(0.50, True)
        result = bt.run_backtest(strat, ds, bankroll=1000, kelly_multiplier=0.5)
        assert result.n_trades == 0
        assert result.half_spread > 0


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def _dataset(self, scenarios: list[tuple[float, bool, str]]):
        """scenarios = [(market_yes_price, resolved_yes, category), ...]"""
        ds = []
        base = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        for i, (p, r, cat) in enumerate(scenarios):
            as_of = base - timedelta(days=i)
            snap = bt.MarketSnapshot(
                f"m{i}", f"q{i}", cat, as_of,
                _price_history([p], start=as_of - timedelta(hours=1)),
            )
            res = bt.Resolution(f"m{i}", r, as_of + timedelta(hours=12))
            ds.append((snap, res))
        return ds

    def test_empty_strategy_returns_zero_trades(self):
        ds = self._dataset([(0.5, True, "Sports")])
        result = bt.run_backtest(lambda s: None, ds)
        assert result.n_trades == 0
        assert result.hit_rate is None
        assert result.roi is None
        assert "0 trades" in result.warnings[0]

    def test_hit_rate_and_ci(self):
        # 3 wins / 4 trades = 75% hit rate
        ds = self._dataset([
            (0.30, True, "Sports"),
            (0.30, True, "Sports"),
            (0.30, True, "Sports"),
            (0.30, False, "Sports"),
        ])
        def strat(snap):
            return bt.Signal("YES", probability=0.50, fraction_override=0.01)
        result = bt.run_backtest(strat, ds)
        assert result.n_trades == 4
        assert result.hit_rate == pytest.approx(0.75)
        lo, hi = result.hit_rate_ci
        assert 0.0 <= lo < 0.75 < hi <= 1.0

    def test_per_category_breakdown(self):
        ds = self._dataset([
            (0.30, True,  "Sports"),
            (0.30, False, "Sports"),
            (0.30, True,  "Politics"),
        ])
        def strat(snap):
            return bt.Signal("YES", probability=0.50, fraction_override=0.01)
        result = bt.run_backtest(strat, ds)
        assert set(result.by_category.keys()) == {"Sports", "Politics"}
        assert result.by_category["Sports"]["n_trades"] == 2
        assert result.by_category["Sports"]["hit_rate"] == pytest.approx(0.5)
        assert result.by_category["Politics"]["n_trades"] == 1

    def test_sample_size_warning_under_10(self):
        ds = self._dataset([(0.30, True, "Sports")])
        def strat(snap):
            return bt.Signal("YES", probability=0.50, fraction_override=0.01)
        result = bt.run_backtest(strat, ds)
        assert any("noise" in w.lower() for w in result.warnings)

    def test_negative_roi_warning(self):
        ds = self._dataset([
            (0.30, False, "Sports"),
            (0.30, False, "Sports"),
        ])
        def strat(snap):
            return bt.Signal("YES", probability=0.50, fraction_override=0.01)
        result = bt.run_backtest(strat, ds)
        assert any("losing money" in w for w in result.warnings)

    def test_equity_curve_sorted_by_closed_time(self):
        ds = self._dataset([
            (0.30, True, "Sports"),
            (0.30, True, "Sports"),
            (0.30, True, "Sports"),
        ])
        def strat(snap):
            return bt.Signal("YES", probability=0.50, fraction_override=0.01)
        result = bt.run_backtest(strat, ds)
        eq = result.equity_curve
        assert list(eq["closed_time"]) == sorted(eq["closed_time"])
        # Cumulative pnl strictly increases when every trade wins
        diffs = eq["cumulative_pnl"].diff().dropna()
        assert (diffs > 0).all()

    def test_max_drawdown_tracks_peak_to_trough(self):
        # 3 wins then 2 losses on equal stakes — drawdown is the loss block
        ds = self._dataset([
            (0.30, True,  "Sports"),
            (0.30, True,  "Sports"),
            (0.30, True,  "Sports"),
            (0.30, False, "Sports"),
            (0.30, False, "Sports"),
        ])
        def strat(snap):
            return bt.Signal("YES", probability=0.50, fraction_override=0.01)
        result = bt.run_backtest(strat, ds)
        # Wins each pay 0.01*$1000 * (0.7/0.3) ≈ $23.33; losses each cost $10.
        # Max drawdown ≈ -$20.
        assert result.max_drawdown == pytest.approx(-20.0, abs=0.5)

    def test_market_baseline_brier(self):
        # Build a dataset with known latest prices and outcomes.
        # market 0: latest_price=0.30, resolves YES → term = (0.30-1)^2 = 0.49
        # market 1: latest_price=0.70, resolves NO  → term = (0.70-0)^2 = 0.49
        # mean Brier = 0.49
        ds = self._dataset([
            (0.30, True,  "Sports"),
            (0.70, False, "Sports"),
        ])
        brier, n = bt.market_baseline_brier(ds)
        assert n == 2
        assert brier == pytest.approx(0.49)

    def test_market_baseline_brier_empty(self):
        brier, n = bt.market_baseline_brier([])
        assert brier is None
        assert n == 0

    def test_brier_score_matches_manual(self):
        # 2 trades, prob=0.5; one wins, one loses → mean brier = 0.25
        ds = self._dataset([
            (0.30, True,  "Sports"),
            (0.30, False, "Sports"),
        ])
        def strat(snap):
            return bt.Signal("YES", probability=0.50, fraction_override=0.01)
        result = bt.run_backtest(strat, ds)
        assert result.brier_score == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Walk-forward engine (Tier 3b v2)
# ---------------------------------------------------------------------------

class TestWalkForward:
    """Walk forward at fixed steps, mark-to-market on signal flip,
    resolution exit at $1/$0."""

    def _dataset(self, prices: list[float], resolved_yes: bool,
                 *, hours_between=24.0, category="Sports"):
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        # Build a price history of `len(prices)` candles ending just
        # before as_of. Resolution is 12h after as_of.
        n = len(prices)
        start = as_of - timedelta(hours=hours_between * (n - 1)) - timedelta(hours=1)
        df = pd.DataFrame({
            "timestamp": [start + timedelta(hours=hours_between * i) for i in range(n)],
            "price": prices,
        })
        snap = bt.MarketSnapshot(
            "x", "q", category, as_of, df,
            end_date=as_of + timedelta(hours=12),
        )
        res = bt.Resolution("x", resolved_yes, as_of + timedelta(hours=12))
        return [(snap, res)]

    def test_no_signal_emits_no_trades(self):
        result = bt.run_backtest_walk_forward(
            lambda s: None,
            self._dataset([0.3, 0.4, 0.5, 0.6, 0.7], True),
            half_spread=0.0,
        )
        assert result.n_trades == 0
        assert "0 trades" in " ".join(result.warnings)

    def test_persistent_yes_signal_one_trade_held_to_resolution(self):
        """Strategy that always says YES → one entry at first eval step,
        held all the way through to resolution. Exactly one trade."""
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.99,
                             fraction_override=0.05)

        result = bt.run_backtest_walk_forward(
            strat, self._dataset([0.30, 0.40, 0.50, 0.60, 0.70], True),
            half_spread=0.0,
        )
        assert result.n_trades == 1
        row = result.trades.iloc[0]
        assert row["direction"] == "YES"
        assert row["exit_reason"] == "resolution"
        assert bool(row["won"]) is True
        # Stake = 5% of $1000 = $50. Entry mark is whatever the first
        # eval step sees (depends on slicing); win pays $1/share. We
        # just assert positive PnL rather than pin the exact value.
        assert row["pnl"] > 0

    def test_signal_flip_produces_two_trades(self):
        """Strategy flips YES → NO. Should produce two trades:
        first exits at signal_flip, second at resolution."""
        call = {"n": 0}

        def strat(snap):
            call["n"] += 1
            if call["n"] <= 2:
                return bt.Signal(direction="YES", probability=0.99,
                                 fraction_override=0.05)
            return bt.Signal(direction="NO", probability=0.99,
                             fraction_override=0.05)

        result = bt.run_backtest_walk_forward(
            strat, self._dataset([0.30, 0.40, 0.50, 0.60, 0.70], False),
            half_spread=0.0,
        )
        assert result.n_trades == 2
        first = result.trades.iloc[0]
        assert first["direction"] == "YES"
        assert first["exit_reason"] == "signal_flip"
        second = result.trades.iloc[1]
        assert second["direction"] == "NO"
        assert second["exit_reason"] == "resolution"
        assert bool(second["won"]) is True

    def test_kelly_zero_skips_open(self):
        """Edge collapses (your_prob < market_yes) → no trade."""
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.40)

        result = bt.run_backtest_walk_forward(
            strat, self._dataset([0.50, 0.50, 0.50], True),
            half_spread=0.0,
        )
        assert result.n_trades == 0

    def test_holding_period_is_populated(self):
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.99,
                             fraction_override=0.05)

        result = bt.run_backtest_walk_forward(
            strat, self._dataset([0.30, 0.40, 0.50, 0.60, 0.70], True),
            half_spread=0.0, eval_step_hours=24.0,
        )
        assert result.n_trades == 1
        assert result.trades.iloc[0]["holding_period_days"] > 0

    def test_lookahead_safety_strict_before(self):
        """Each strategy call must receive prices strictly before its
        as_of timestamp."""
        seen_lengths: list[int] = []

        def strat(snap):
            seen_lengths.append(len(snap.prices))
            return None

        bt.run_backtest_walk_forward(
            strat, self._dataset([0.30, 0.40, 0.50, 0.60, 0.70, 0.80], True),
            half_spread=0.0, eval_step_hours=24.0,
        )
        assert seen_lengths
        assert max(seen_lengths) < 6   # never sees the full 6-row history

    def test_half_spread_reduces_exit_proceeds(self):
        call = {"n": 0}

        def strat(snap):
            call["n"] += 1
            if call["n"] == 1:
                return bt.Signal(direction="YES", probability=0.99,
                                 fraction_override=0.05)
            return bt.Signal(direction="NO", probability=0.99,
                             fraction_override=0.05)

        ds = self._dataset([0.30, 0.40, 0.60, 0.70, 0.80], False)
        zero = bt.run_backtest_walk_forward(strat, ds, half_spread=0.0)
        call["n"] = 0
        costly = bt.run_backtest_walk_forward(strat, ds, half_spread=0.02)
        z_first = zero.trades.iloc[0]["pnl"]
        c_first = costly.trades.iloc[0]["pnl"]
        assert c_first < z_first
        assert costly.implied_spread_cost > 0

    def test_fee_pct_applied_to_walk_forward_trades(self):
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.99,
                             fraction_override=0.05)

        result = bt.run_backtest_walk_forward(
            strat, self._dataset([0.50, 0.50, 0.50], True),
            half_spread=0.0, fee_pct=0.01,
        )
        assert result.n_trades == 1
        row = result.trades.iloc[0]
        assert row["fee"] == pytest.approx(0.5)
        assert result.total_fees == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Portfolio-level bankroll constraint
# ---------------------------------------------------------------------------

class TestPortfolioConstraint:
    """Single-shot and walk-forward both track available cash across
    the run when `portfolio_constrained=True` (the default)."""

    def _two_overlapping_markets(self, *, bankroll: float):
        """Two markets with simultaneous as_of times. Each one wants the
        full bankroll. Constrained: second gets clipped/skipped."""
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        prices = _price_history([0.30], start=as_of - timedelta(hours=1))
        snap_a = bt.MarketSnapshot("a", "qA", "Sports", as_of, prices,
                                    end_date=as_of + timedelta(days=10))
        snap_b = bt.MarketSnapshot("b", "qB", "Sports", as_of, prices,
                                    end_date=as_of + timedelta(days=10))
        res_a = bt.Resolution("a", True, as_of + timedelta(days=10))
        res_b = bt.Resolution("b", True, as_of + timedelta(days=10))
        return [(snap_a, res_a), (snap_b, res_b)]

    def test_single_shot_constrained_caps_overlapping_stake(self):
        # Strategy demands 100% of bankroll on each. With constraint,
        # market A consumes all cash; market B gets skipped because the
        # two close at the same time (no cash released between them).
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50,
                             fraction_override=1.0)

        ds = self._two_overlapping_markets(bankroll=1000)
        constrained = bt.run_backtest(
            strat, ds, bankroll=1000, kelly_multiplier=1.0,
            half_spread=0.0, portfolio_constrained=True,
        )
        # Only one trade fires — the second has $0 cash.
        assert constrained.n_trades == 1
        assert constrained.n_skipped_for_cash == 1
        # Unconstrained: both fire at full bankroll, simulating a
        # combined exposure of 200% of bankroll.
        unconstrained = bt.run_backtest(
            strat, ds, bankroll=1000, kelly_multiplier=1.0,
            half_spread=0.0, portfolio_constrained=False,
        )
        assert unconstrained.n_trades == 2
        assert unconstrained.n_skipped_for_cash == 0

    def test_min_cash_tracked(self):
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50,
                             fraction_override=0.30)

        ds = self._two_overlapping_markets(bankroll=1000)
        result = bt.run_backtest(
            strat, ds, bankroll=1000, kelly_multiplier=1.0,
            half_spread=0.0, portfolio_constrained=True,
        )
        # After two 30% bets, $400 cash should remain at the trough.
        assert result.min_cash_seen == pytest.approx(400.0)

    def test_clip_counts_when_smaller_than_desired(self):
        # Bankroll $100; two 60% stakes desired = $60 each. First fills
        # at $60; second wants $60 but only $40 is left — clipped, not
        # skipped.
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50,
                             fraction_override=0.60)

        ds = self._two_overlapping_markets(bankroll=100)
        result = bt.run_backtest(
            strat, ds, bankroll=100, kelly_multiplier=1.0,
            half_spread=0.0, portfolio_constrained=True,
        )
        assert result.n_trades == 2
        assert result.n_clipped_for_cash == 1
        assert result.n_skipped_for_cash == 0
        # Second trade's stake is the clipped value ($40)
        assert result.trades.iloc[1]["stake"] == pytest.approx(40.0)

    def test_unconstrained_flag_disables_constraint(self):
        # Same setup as the cap test, with the constraint off → both
        # fire at the desired stake regardless of cash.
        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50,
                             fraction_override=1.0)

        ds = self._two_overlapping_markets(bankroll=1000)
        result = bt.run_backtest(
            strat, ds, bankroll=1000, kelly_multiplier=1.0,
            half_spread=0.0, portfolio_constrained=False,
        )
        assert result.portfolio_constrained is False
        assert result.n_trades == 2
        # Both got the full $1000
        assert result.trades["stake"].iloc[0] == pytest.approx(1000.0)
        assert result.trades["stake"].iloc[1] == pytest.approx(1000.0)

    def test_walk_forward_constrained_shares_cash_across_markets(self):
        # Two parallel markets each spanning the same 5-day window.
        # Strategy fires YES at full bankroll on every eval point.
        # With constraint: market A opens first, consumes all cash;
        # market B can't open until A resolves. With the per-market
        # eval cadence being daily and resolution being at the same
        # time for both, B never opens.
        as_of = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        prices = pd.DataFrame({
            "timestamp": [as_of - timedelta(hours=24 * i) for i in range(5, 0, -1)],
            "price": [0.30, 0.32, 0.34, 0.36, 0.38],
        })
        snap_a = bt.MarketSnapshot("a", "qA", "Sports", as_of, prices,
                                    end_date=as_of + timedelta(hours=12))
        snap_b = bt.MarketSnapshot("b", "qB", "Sports", as_of, prices,
                                    end_date=as_of + timedelta(hours=12))
        res_a = bt.Resolution("a", True, as_of + timedelta(hours=12))
        res_b = bt.Resolution("b", True, as_of + timedelta(hours=12))

        def strat(snap):
            return bt.Signal(direction="YES", probability=0.99,
                             fraction_override=1.0)

        # Constrained
        constrained = bt.run_backtest_walk_forward(
            strat, [(snap_a, res_a), (snap_b, res_b)],
            bankroll=1000, kelly_multiplier=1.0, half_spread=0.0,
            eval_step_hours=24.0, portfolio_constrained=True,
        )
        # One of the markets gets a trade; the other is starved
        assert constrained.n_trades == 1
        assert constrained.n_skipped_for_cash >= 1
        assert constrained.portfolio_constrained is True

        # Unconstrained: both markets get their trade at full stake
        unconstrained = bt.run_backtest_walk_forward(
            strat, [(snap_a, res_a), (snap_b, res_b)],
            bankroll=1000, kelly_multiplier=1.0, half_spread=0.0,
            eval_step_hours=24.0, portfolio_constrained=False,
        )
        assert unconstrained.n_trades == 2
        assert unconstrained.n_skipped_for_cash == 0

    def test_cash_returns_on_resolution(self):
        # Two markets with NON-overlapping windows. A resolves before
        # B's as_of, so B should see the cash returned and not be
        # clipped.
        as_of_a = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        prices_a = _price_history([0.30], start=as_of_a - timedelta(hours=1))
        snap_a = bt.MarketSnapshot("a", "qA", "Sports", as_of_a, prices_a,
                                    end_date=as_of_a + timedelta(hours=12))
        res_a = bt.Resolution("a", True, as_of_a + timedelta(hours=12))

        # Market B is opened 1 day after A's resolution
        as_of_b = res_a.closed_time + timedelta(days=1)
        prices_b = _price_history([0.30], start=as_of_b - timedelta(hours=1))
        snap_b = bt.MarketSnapshot("b", "qB", "Sports", as_of_b, prices_b,
                                    end_date=as_of_b + timedelta(days=1))
        res_b = bt.Resolution("b", True, as_of_b + timedelta(days=1))

        def strat(snap):
            return bt.Signal(direction="YES", probability=0.50,
                             fraction_override=1.0)

        result = bt.run_backtest(
            strat, [(snap_a, res_a), (snap_b, res_b)],
            bankroll=1000, kelly_multiplier=1.0, half_spread=0.0,
            portfolio_constrained=True,
        )
        # Both trades fire; B's stake is whatever cash A returned
        # (initial stake + win payoff).
        assert result.n_trades == 2
        assert result.n_skipped_for_cash == 0
        # A won at price 0.30: payoff = stake × (1 - 0.30) / 0.30 = $2333.33
        # → cash on hand for B is $1000 + $2333.33 ≈ $3333.33
        # B's desired stake is 100% of bankroll = $1000, which fits.
        assert result.trades.iloc[1]["stake"] == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# User-defined plugin system (register_strategy + discover_user_strategies)
# ---------------------------------------------------------------------------

class TestRegisterStrategy:
    def setup_method(self):
        # Snapshot the registry so we can restore after each test
        self._snapshot = dict(bt.STRATEGIES)

    def teardown_method(self):
        bt.STRATEGIES.clear()
        bt.STRATEGIES.update(self._snapshot)

    def test_decorator_registers_function(self):
        @bt.register_strategy("test_x1", "Test X1")
        def my_strat(snap):
            return None

        assert "test_x1" in bt.STRATEGIES
        display, fn = bt.STRATEGIES["test_x1"]
        assert display == "Test X1"
        assert fn is my_strat

    def test_decorator_returns_function_unchanged(self):
        @bt.register_strategy("test_x2", "Test X2")
        def my_strat(snap):
            return "marker"
        # Function is still directly callable (useful for unit tests)
        assert my_strat(None) == "marker"

    def test_display_name_defaults_to_function_name(self):
        @bt.register_strategy("test_x3")
        def named_callable(snap):
            return None
        display, _ = bt.STRATEGIES["test_x3"]
        assert display == "named_callable"

    def test_overriding_existing_key_warns_but_succeeds(self, caplog):
        @bt.register_strategy("test_x4", "First")
        def first(snap):
            return None
        with caplog.at_level("WARNING"):
            @bt.register_strategy("test_x4", "Second")
            def second(snap):
                return None
        assert bt.STRATEGIES["test_x4"][0] == "Second"
        assert any("overriding" in r.message for r in caplog.records)

    def test_empty_key_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            bt.register_strategy("")


class TestDiscoverUserStrategies:
    def setup_method(self):
        self._snapshot = dict(bt.STRATEGIES)

    def teardown_method(self):
        bt.STRATEGIES.clear()
        bt.STRATEGIES.update(self._snapshot)

    def test_missing_dir_returns_zero(self, tmp_path):
        # Point at a path that doesn't exist
        n = bt.discover_user_strategies(
            package="nonexistent_pkg", search_dir=tmp_path / "missing",
        )
        assert n == 0

    def test_discovers_and_registers_one_strategy(self, tmp_path, monkeypatch):
        # Build a tiny throwaway package
        pkg = tmp_path / "throwaway_strats"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "my_plugin.py").write_text(
            "from pm.research import backtest as bt\n"
            "@bt.register_strategy('test_disc1', 'Disc 1')\n"
            "def s(snap): return None\n"
        )
        # Make tmp_path importable
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            n = bt.discover_user_strategies(
                package="throwaway_strats", search_dir=pkg,
            )
        finally:
            sys.path.remove(str(tmp_path))
            # Clean up sys.modules so the next test doesn't see cached pkg
            for k in list(sys.modules):
                if k.startswith("throwaway_strats"):
                    del sys.modules[k]
        assert n == 1
        assert "test_disc1" in bt.STRATEGIES

    def test_broken_plugin_is_logged_and_skipped(self, tmp_path, caplog):
        pkg = tmp_path / "broken_strats"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "good.py").write_text(
            "from pm.research import backtest as bt\n"
            "@bt.register_strategy('good_one', 'Good')\n"
            "def s(snap): return None\n"
        )
        (pkg / "broken.py").write_text("raise RuntimeError('intentional')\n")

        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            with caplog.at_level("WARNING"):
                n = bt.discover_user_strategies(
                    package="broken_strats", search_dir=pkg,
                )
        finally:
            sys.path.remove(str(tmp_path))
            for k in list(sys.modules):
                if k.startswith("broken_strats"):
                    del sys.modules[k]
        # One good + one broken → 1 loaded (broken counted as skipped, not crash)
        assert n == 1
        assert "good_one" in bt.STRATEGIES
        assert any("failed to load" in r.message for r in caplog.records)

    def test_underscore_prefixed_modules_skipped(self, tmp_path):
        # Files starting with _ are helpers, not plugins
        pkg = tmp_path / "us_strats"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "_helper.py").write_text(
            "from pm.research import backtest as bt\n"
            "@bt.register_strategy('should_not_load', 'Bad')\n"
            "def s(snap): return None\n"
        )
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            n = bt.discover_user_strategies(
                package="us_strats", search_dir=pkg,
            )
        finally:
            sys.path.remove(str(tmp_path))
            for k in list(sys.modules):
                if k.startswith("us_strats"):
                    del sys.modules[k]
        assert n == 0
        assert "should_not_load" not in bt.STRATEGIES

    def test_idempotent_reimport(self, tmp_path):
        # Calling discover twice doesn't re-trigger the decorator side
        # effects in a way that breaks (no double-warnings, no error).
        pkg = tmp_path / "idem_strats"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "p.py").write_text(
            "from pm.research import backtest as bt\n"
            "@bt.register_strategy('idem1', 'I')\n"
            "def s(snap): return None\n"
        )
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            n1 = bt.discover_user_strategies(
                package="idem_strats", search_dir=pkg,
            )
            n2 = bt.discover_user_strategies(
                package="idem_strats", search_dir=pkg,
            )
        finally:
            sys.path.remove(str(tmp_path))
            for k in list(sys.modules):
                if k.startswith("idem_strats"):
                    del sys.modules[k]
        # Module was already cached on the second call — Python's
        # import system makes it a no-op. Both calls report success.
        assert n1 == 1
        assert n2 == 1
        assert "idem1" in bt.STRATEGIES


# ---------------------------------------------------------------------------
# user_strategies/favorite_grind.py — exercised as a plugin
# ---------------------------------------------------------------------------

class TestFavoriteGrindPlugin:
    """The shipped example/sample plugin. Acts as a smoke test that the
    discovery pipeline finds it AND that its decision boundaries
    behave the way the docstring claims."""

    def setup_method(self):
        # Ensure the plugin is loaded — discovery is idempotent so this
        # is safe regardless of prior test state.
        bt.discover_user_strategies()

    def _snap(self, price: float) -> bt.MarketSnapshot:
        as_of = datetime(2026, 5, 1, tzinfo=UTC)
        return bt.MarketSnapshot(
            "x", "q", "Sports", as_of,
            _price_history([price], start=as_of - timedelta(hours=1)),
        )

    def test_registered_via_discovery(self):
        assert "favorite_grind" in bt.STRATEGIES
        display, _ = bt.STRATEGIES["favorite_grind"]
        assert "favorite" in display.lower()

    def test_fires_yes_on_high_favorite(self):
        fn = bt.get_strategy("favorite_grind")
        sig = fn(self._snap(0.92))
        assert sig is not None
        assert sig.direction == "YES"
        # Default assumed_edge=0.02 → probability = 0.92 + 0.02 = 0.94
        assert sig.probability == pytest.approx(0.94)

    def test_fires_no_on_extreme_low(self):
        fn = bt.get_strategy("favorite_grind")
        sig = fn(self._snap(0.08))   # YES priced very low → bet NO
        assert sig is not None
        assert sig.direction == "NO"
        # YES-side probability stored is 0.08 − 0.02 = 0.06
        assert sig.probability == pytest.approx(0.06)

    def test_no_signal_in_middle(self):
        fn = bt.get_strategy("favorite_grind")
        # Default threshold=0.85; 0.50 is not in either tail
        assert fn(self._snap(0.50)) is None
        assert fn(self._snap(0.80)) is None
        assert fn(self._snap(0.20)) is None

    def test_threshold_parameter_respected(self):
        fn = bt.get_strategy("favorite_grind")
        # Tighten threshold to 0.95 → 0.92 no longer qualifies
        assert fn(self._snap(0.92), threshold=0.95) is None
        # And 0.96 does
        sig = fn(self._snap(0.96), threshold=0.95)
        assert sig is not None
        assert sig.direction == "YES"

    def test_clamps_probability_to_unit_interval(self):
        fn = bt.get_strategy("favorite_grind")
        # 0.99 + 0.02 would be 1.01 → clamped to 0.99
        sig = fn(self._snap(0.99), assumed_edge=0.05)
        assert sig is not None
        assert sig.probability <= 0.99


# ---------------------------------------------------------------------------
# Market-baseline Brier gate (carried on BacktestResult)
# ---------------------------------------------------------------------------

class TestMarketBaselineGate:
    """The shippability gate: the strategy's Brier must strictly beat the
    Brier of "just trust the decision-time market price" scored over the
    same trades the strategy made."""

    AS_OF = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    def _market(self, cid, price, resolved_yes, *, category="Sports"):
        snap = bt.MarketSnapshot(
            cid, f"{cid}?", category, self.AS_OF,
            _price_history([price], start=self.AS_OF - timedelta(hours=1)),
        )
        res = bt.Resolution(cid, resolved_yes, self.AS_OF + timedelta(hours=12))
        return (snap, res)

    def _run(self, strategy, dataset, **kw):
        kw.setdefault("bankroll", 1000.0)
        kw.setdefault("kelly_multiplier", 0.5)
        kw.setdefault("half_spread", 0.0)
        return bt.run_backtest(strategy, dataset, **kw)

    def test_gate_passes_when_strategy_out_forecasts_the_price(self):
        # A: market 0.80 resolves YES → baseline term 0.04
        # B: market 0.30 resolves NO  → baseline term 0.09   (mean 0.065)
        # Strategy: 0.9 on A (term 0.01), 0.1 on B (term 0.01) → Brier 0.01
        dataset = [self._market("A", 0.80, True),
                   self._market("B", 0.30, False)]
        probs = {"A": 0.9, "B": 0.1}

        def strat(snap):
            p = probs[snap.condition_id]
            d = "YES" if p > snap.latest_price else "NO"
            return bt.Signal(direction=d, probability=p)

        r = self._run(strat, dataset)
        assert r.n_trades == 2
        assert r.brier_score == pytest.approx(0.01)
        assert r.market_baseline_brier == pytest.approx(0.065)
        assert r.market_baseline_n == 2
        assert r.beats_market_baseline is True
        assert not any("GATE FAILED" in w for w in r.warnings)

    def test_gate_fails_when_strategy_worse_than_price(self):
        # Flat 0.5 forecaster: Brier 0.25 vs baseline 0.065 → fail + warning.
        dataset = [self._market("A", 0.80, True),
                   self._market("B", 0.30, False)]

        def strat(snap):
            d = "NO" if snap.latest_price > 0.5 else "YES"
            return bt.Signal(direction=d, probability=0.5)

        r = self._run(strat, dataset)
        assert r.n_trades == 2
        assert r.brier_score == pytest.approx(0.25)
        assert r.market_baseline_brier == pytest.approx(0.065)
        assert r.beats_market_baseline is False
        assert any("GATE FAILED" in w for w in r.warnings)

    def test_tying_the_market_does_not_pass(self):
        # Engineered exact tie: strategy and baseline share the same two
        # Brier terms {0.04, 0.09}, swapped between the two markets.
        # A: price 0.8, resolves YES (baseline 0.04); strategy 0.7 → NO fires,
        #    strategy term 0.09.
        # B: price 0.7, resolves YES (baseline 0.09); strategy 0.8 → YES fires,
        #    strategy term 0.04.
        dataset = [self._market("A", 0.80, True),
                   self._market("B", 0.70, True)]
        probs = {"A": 0.7, "B": 0.8}

        def strat(snap):
            p = probs[snap.condition_id]
            d = "YES" if p > snap.latest_price else "NO"
            return bt.Signal(direction=d, probability=p)

        r = self._run(strat, dataset)
        assert r.n_trades == 2
        assert r.brier_score == pytest.approx(r.market_baseline_brier)
        assert r.beats_market_baseline is False  # strict <: a tie is no edge

    def test_zero_trades_leaves_gate_unset(self):
        def strat(snap):
            return None

        r = self._run(strat, [self._market("A", 0.50, True)])
        assert r.n_trades == 0
        assert r.market_baseline_brier is None
        assert r.market_baseline_n == 0
        assert r.beats_market_baseline is None
        assert not any("GATE FAILED" in w for w in r.warnings)

    def test_baseline_matches_trade_columns_exactly(self):
        # The result field must equal the column computation over the
        # trades the engine actually recorded — no silent sample drift.
        dataset = [self._market("A", 0.80, True),
                   self._market("B", 0.30, False)]

        def strat(snap):
            d = "NO" if snap.latest_price > 0.5 else "YES"
            return bt.Signal(direction=d, probability=0.5)

        r = self._run(strat, dataset)
        expected = float(
            ((r.trades["market_yes_price"] - r.trades["actual"]) ** 2).mean()
        )
        assert r.market_baseline_brier == pytest.approx(expected)
        assert r.market_baseline_n == len(r.trades)

    def test_by_category_carries_market_brier(self):
        dataset = [self._market("A", 0.80, True, category="Sports"),
                   self._market("B", 0.30, False, category="Politics")]
        probs = {"A": 0.9, "B": 0.1}

        def strat(snap):
            p = probs[snap.condition_id]
            d = "YES" if p > snap.latest_price else "NO"
            return bt.Signal(direction=d, probability=p)

        r = self._run(strat, dataset)
        assert r.by_category["Sports"]["market_brier"] == pytest.approx(0.04)
        assert r.by_category["Politics"]["market_brier"] == pytest.approx(0.09)

    def test_walk_forward_carries_gate_and_agrees_with_trades(self):
        as_of = self.AS_OF
        prices = _price_history([0.4] * 6, start=as_of - timedelta(hours=6))
        snap = bt.MarketSnapshot("W", "w?", "Sports", as_of, prices)
        res = bt.Resolution("W", True, as_of + timedelta(hours=2))

        def strat(snap):
            return bt.Signal(direction="YES", probability=0.6)

        r = bt.run_backtest_walk_forward(
            strat, [(snap, res)],
            bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
            eval_step_hours=1.0,
        )
        assert r.n_trades >= 1
        expected = float(
            ((r.trades["market_yes_price"] - r.trades["actual"]) ** 2).mean()
        )
        assert r.market_baseline_brier == pytest.approx(expected)
        assert r.market_baseline_n == len(r.trades)
        assert r.beats_market_baseline is (r.brier_score < r.market_baseline_brier)

    def test_walk_forward_zero_trades_leaves_gate_unset(self):
        as_of = self.AS_OF
        prices = _price_history([0.4] * 6, start=as_of - timedelta(hours=6))
        snap = bt.MarketSnapshot("W", "w?", "Sports", as_of, prices)
        res = bt.Resolution("W", True, as_of + timedelta(hours=2))

        def strat(snap):
            return None

        r = bt.run_backtest_walk_forward(
            strat, [(snap, res)],
            bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
            eval_step_hours=1.0,
        )
        assert r.n_trades == 0
        assert r.market_baseline_brier is None
        assert r.market_baseline_n == 0
        assert r.beats_market_baseline is None


# ---------------------------------------------------------------------------
# The extracted aggregation helpers.
#
# These four came out of `run_backtest` (329 lines) and
# `run_backtest_walk_forward` (212). The by-category table and the two
# warning groups were duplicated between them — the category block
# byte-for-byte — so they were two places to fix one bug. The fee resolver
# was a 22-line branch in the middle of the trade loop whose fallback arm
# (a `fee_fn` that raises) had no coverage at all, reachable only by running
# a whole backtest.
#
# Equivalence for the extraction itself was established by fingerprinting
# every field of every BacktestResult across 29 configurations (both
# engines, both portfolio modes, three cost settings, both fee paths, the
# empty-result path) before and after: byte-identical. These tests pin the
# pieces directly, which that fingerprint cannot do once it is deleted.
# ---------------------------------------------------------------------------

class TestTradeFee:

    def test_flat_fee_pct_is_a_fraction_of_stake(self):
        assert bt._trade_fee(100.0, 0.4, True, fee_pct=0.02, fee_fn=None,
                             condition_id="C") == pytest.approx(2.0)

    def test_fee_fn_overrides_fee_pct_and_sees_contracts_not_dollars(self):
        seen = {}

        def fee_fn(entry, exit_, contracts, ticker):
            seen.update(entry=entry, exit=exit_, contracts=contracts,
                        ticker=ticker)
            return 1.23

        fee = bt._trade_fee(50.0, 0.25, True, fee_pct=0.9, fee_fn=fee_fn,
                            condition_id="TICK")
        assert fee == pytest.approx(1.23), "fee_fn must override fee_pct"
        assert seen == {"entry": 0.25, "exit": 1.0, "contracts": 200.0,
                        "ticker": "TICK"}

    def test_exit_price_is_zero_on_a_loss(self):
        """`won` is strategy-side: the winning side settles at $1 either way,
        so a loss is exit 0.0 regardless of direction."""
        got = {}
        bt._trade_fee(10.0, 0.5, False, fee_pct=0.0,
                      fee_fn=lambda e, x, c, t: got.setdefault("exit", x) or 0.0,
                      condition_id="C")
        assert got["exit"] == 0.0

    def test_zero_entry_price_does_not_divide_by_zero(self):
        got = {}
        bt._trade_fee(10.0, 0.0, True, fee_pct=0.0,
                      fee_fn=lambda e, x, c, t: got.setdefault("c", c) or 0.0,
                      condition_id="C")
        assert got["c"] == pytest.approx(10.0 / 1e-9)

    def test_raising_fee_fn_falls_back_to_fee_pct_and_warns(self, caplog):
        """Previously unreachable in tests: this arm was the only uncovered
        branch in the fee path. A backtest with approximate fees beats no
        backtest, so a broken fee_fn must not abort the run."""
        def boom(entry, exit_, contracts, ticker):
            raise ValueError("no fee schedule for this ticker")

        with caplog.at_level("WARNING"):
            fee = bt._trade_fee(200.0, 0.5, True, fee_pct=0.03, fee_fn=boom,
                                condition_id="KXBAD")
        assert fee == pytest.approx(6.0), "must fall back to stake * fee_pct"
        assert "fee_fn raised on KXBAD" in caplog.text
        assert "no fee schedule" in caplog.text, \
            "the underlying error must reach the log, not be swallowed"

    def test_raising_fee_fn_does_not_abort_a_whole_backtest(self):
        """The property the fallback exists for, end to end."""
        as_of = datetime(2026, 3, 1, tzinfo=UTC)
        prices = _price_history([0.3, 0.3, 0.3, 0.3], start=as_of - timedelta(hours=4))
        pairs = [(bt.MarketSnapshot(f"C{i}", "q?", "Sports", as_of, prices),
                  bt.Resolution(f"C{i}", True, as_of + timedelta(days=1)))
                 for i in range(3)]

        def boom(entry, exit_, contracts, ticker):
            raise RuntimeError("boom")

        r = bt.run_backtest(
            lambda s: bt.Signal("YES", 0.8), pairs,
            bankroll=1000.0, half_spread=0.0, fee_pct=0.01, fee_fn=boom)
        assert r.n_trades == 3
        assert r.total_fees == pytest.approx(float(r.trades["stake"].sum()) * 0.01)


class TestSummarizeByCategory:

    @staticmethod
    def _trades(rows):
        return pd.DataFrame(rows)

    def test_groups_and_computes_each_field(self):
        out = bt._summarize_by_category(self._trades([
            # cat, won, stake, pnl, brier_term, market price, actual
            {"category": "Sports", "won": True, "stake": 100.0, "pnl": 50.0,
             "brier_term": 0.04, "market_yes_price": 0.6, "actual": 1.0},
            {"category": "Sports", "won": False, "stake": 100.0, "pnl": -100.0,
             "brier_term": 0.16, "market_yes_price": 0.8, "actual": 0.0},
            {"category": "Politics", "won": True, "stake": 50.0, "pnl": 25.0,
             "brier_term": 0.09, "market_yes_price": 0.7, "actual": 1.0},
        ]))
        assert set(out) == {"Sports", "Politics"}
        s = out["Sports"]
        assert s["n_trades"] == 2
        assert s["hit_rate"] == pytest.approx(0.5)
        assert s["total_pnl"] == pytest.approx(-50.0)
        assert s["roi"] == pytest.approx(-50.0 / 200.0)
        assert s["brier"] == pytest.approx(0.10)
        # market Brier: ((0.6-1)^2 + (0.8-0)^2) / 2
        assert s["market_brier"] == pytest.approx((0.16 + 0.64) / 2)
        assert out["Politics"]["n_trades"] == 1

    def test_blank_category_becomes_unknown_rather_than_being_dropped(self):
        out = bt._summarize_by_category(self._trades([
            {"category": "", "won": True, "stake": 10.0, "pnl": 5.0,
             "brier_term": 0.01, "market_yes_price": 0.5, "actual": 1.0},
        ]))
        assert list(out) == ["Unknown"]
        assert out["Unknown"]["n_trades"] == 1

    def test_zero_stake_category_gives_none_roi_not_a_zero_division(self):
        out = bt._summarize_by_category(self._trades([
            {"category": "Sports", "won": False, "stake": 0.0, "pnl": 0.0,
             "brier_term": 0.25, "market_yes_price": 0.5, "actual": 0.0},
        ]))
        assert out["Sports"]["roi"] is None

    def test_both_engines_produce_the_same_table_shape(self):
        """The block was duplicated byte-for-byte; keep the two in step."""
        as_of = datetime(2026, 4, 1, tzinfo=UTC)
        prices = _price_history([0.4] * 8, start=as_of - timedelta(hours=8))
        pairs = [(bt.MarketSnapshot(f"C{i}", "q?", c, as_of, prices),
                  bt.Resolution(f"C{i}", i % 2 == 0, as_of + timedelta(days=1)))
                 for i, c in enumerate(["Sports", "Politics", "Sports"])]
        kw = dict(bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0)
        a = bt.run_backtest(lambda s: bt.Signal("YES", 0.75), pairs, **kw)
        b = bt.run_backtest_walk_forward(
            lambda s: bt.Signal("YES", 0.75), pairs, eval_step_hours=1.0, **kw)
        assert set(a.by_category) == set(b.by_category)
        for cat in a.by_category:
            assert set(a.by_category[cat]) == set(b.by_category[cat])


class TestWarningGroups:

    def test_quality_warnings_flags_negative_roi(self):
        ws = bt._quality_warnings(n_trades=500, roi=-0.12, brier=0.2,
                                  mb_brier=None, mb_n=0, beats_baseline=None)
        assert any("losing money" in w and "-12.0%" in w for w in ws)

    def test_quality_warnings_silent_when_everything_is_fine(self):
        assert bt._quality_warnings(
            n_trades=500, roi=0.08, brier=0.15, mb_brier=0.2, mb_n=500,
            beats_baseline=True) == []

    def test_gate_failed_names_the_numbers_and_the_consequence(self):
        ws = bt._quality_warnings(n_trades=400, roi=0.05, brier=0.2100,
                                  mb_brier=0.1900, mb_n=400,
                                  beats_baseline=False)
        gate = [w for w in ws if "GATE FAILED" in w]
        assert len(gate) == 1
        assert "0.2100" in gate[0] and "0.1900" in gate[0] and "400" in gate[0]
        assert "not skill" in gate[0]

    def test_gate_warning_absent_when_baseline_is_unknown(self):
        """`None` means "no comparable baseline", which is not a failure."""
        ws = bt._quality_warnings(n_trades=400, roi=0.05, brier=0.2,
                                  mb_brier=None, mb_n=0, beats_baseline=None)
        assert not any("GATE FAILED" in w for w in ws)

    def test_quality_warning_order_is_sample_then_roi_then_gate(self):
        ws = bt._quality_warnings(n_trades=3, roi=-0.5, brier=0.3,
                                  mb_brier=0.1, mb_n=3, beats_baseline=False)
        assert len(ws) == 3
        assert "losing money" in ws[1] and "GATE FAILED" in ws[2]

    def test_cash_warnings_are_silent_when_unconstrained(self):
        """The counters are not meaningful with the constraint off, so
        reporting them there would be reporting noise."""
        assert bt._cash_constraint_warnings(
            portfolio_constrained=False, n_skipped_for_cash=9,
            n_clipped=9) == []

    def test_cash_warnings_report_skipped_then_clipped(self):
        ws = bt._cash_constraint_warnings(
            portfolio_constrained=True, n_skipped_for_cash=4, n_clipped=7)
        assert len(ws) == 2
        assert "4 trade(s) skipped" in ws[0]
        assert "7 trade(s) clipped" in ws[1]

    def test_cash_warnings_omit_a_group_with_a_zero_count(self):
        ws = bt._cash_constraint_warnings(
            portfolio_constrained=True, n_skipped_for_cash=0, n_clipped=2)
        assert len(ws) == 1 and "clipped" in ws[0]

    def test_walk_forward_keeps_signal_flip_between_the_two_groups(self):
        """Ordering is what the UI renders, and the extraction had to preserve
        it: quality warnings, then this engine's own, then cash."""
        as_of = datetime(2026, 5, 1, tzinfo=UTC)
        prices = _price_history([0.5, 0.6, 0.4, 0.6, 0.4, 0.6],
                                start=as_of - timedelta(hours=6))
        pairs = [(bt.MarketSnapshot("C0", "q?", "Sports", as_of, prices),
                  bt.Resolution("C0", False, as_of + timedelta(days=2)))]

        def flipper(snap):
            p = snap.latest_price
            return bt.Signal("YES", 0.9) if p and p < 0.5 else bt.Signal("NO", 0.9)

        r = bt.run_backtest_walk_forward(
            flipper, pairs, bankroll=1000.0, kelly_multiplier=0.5,
            half_spread=0.0, eval_step_hours=1.0)
        # The fixture is built to produce flip exits; assert that rather than
        # guarding on it, so the ordering check cannot quietly go vacuous.
        assert list(r.trades["exit_reason"]) == ["signal_flip", "signal_flip"]
        flips = [i for i, w in enumerate(r.warnings) if "signal flip" in w]
        assert len(flips) == 1, f"expected one flip warning in {r.warnings}"
        i = flips[0]
        assert any("GATE FAILED" in w for w in r.warnings[:i]), \
            "quality warnings must precede the signal-flip warning"
        assert not any("GATE FAILED" in w for w in r.warnings[i + 1:])
        assert not any("trade(s) skipped" in w or "trade(s) clipped" in w
                       for w in r.warnings[:i]), \
            "cash warnings must follow the signal-flip warning"
