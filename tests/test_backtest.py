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
