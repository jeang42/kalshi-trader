"""Tests for the Kalshi fee model and its integration with the
strategy_runner / backtest engines."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pm.arms import strategy_runner as sr
from pm.venue import kalshi_fees as kf


class TestFeeFormula:
    """Anchor tests against published Kalshi formula:
    fee = ceil(0.07 × P × (1-P) × C × 100) / 100"""

    def test_max_fee_at_midpoint(self):
        # At P=0.50, fee is maximized: 0.07 × 0.25 × C = 1.75% × C
        # 100 contracts → $1.75
        assert kf.fee_for_trade(0.50, 100) == pytest.approx(1.75)

    def test_zero_at_boundaries(self):
        # At P=0 or P=1, P(1-P)=0 → no fee
        assert kf.fee_for_trade(0.0, 100) == 0.0
        assert kf.fee_for_trade(1.0, 100) == 0.0

    def test_symmetric_around_05(self):
        """fee(p, C) == fee(1-p, C) because P(1-P) is symmetric."""
        for p in (0.10, 0.25, 0.40):
            assert kf.fee_for_trade(p, 100) == kf.fee_for_trade(1 - p, 100)

    def test_no_contracts_no_fee(self):
        assert kf.fee_for_trade(0.50, 0) == 0.0
        assert kf.fee_for_trade(0.50, -10) == 0.0

    def test_rounds_up_to_nearest_cent(self):
        """ceil semantics: a fee of $0.001 still costs $0.01."""
        # 1 contract at P=0.50: 0.07 × 0.25 × 1 = $0.0175 → ceils to $0.02
        assert kf.fee_for_trade(0.50, 1) == 0.02

    def test_price_clamped_to_valid_range(self):
        """Out-of-range prices clamp to [0, 1] before formula applies."""
        assert kf.fee_for_trade(1.5, 100) == kf.fee_for_trade(1.0, 100)
        assert kf.fee_for_trade(-0.5, 100) == kf.fee_for_trade(0.0, 100)

    def test_custom_fee_rate(self):
        # Sports rate is half — sanity check
        std = kf.fee_for_trade(0.50, 100, fee_rate=0.07)
        sport = kf.fee_for_trade(0.50, 100, fee_rate=0.035)
        # Half the rate → roughly half the fee (within rounding)
        assert abs(sport - std / 2) < 0.02


class TestFeeRateLookup:
    def test_sports_prefixes_get_reduced_rate(self):
        assert kf.fee_rate_for_series("KXMLBGAME-26MAY...") == 0.035
        assert kf.fee_rate_for_series("KXNBA-...") == 0.035
        assert kf.fee_rate_for_series("KXNFL-...") == 0.035

    def test_non_sports_get_standard_rate(self):
        assert kf.fee_rate_for_series("KXNEWPOPE-35-PPAR") == 0.07
        assert kf.fee_rate_for_series("KXTVSEASON-...") == 0.07

    def test_empty_falls_back_to_standard(self):
        assert kf.fee_rate_for_series("") == 0.07
        assert kf.fee_rate_for_series(None) == 0.07  # type: ignore


class TestFeePctOfStake:
    def test_at_midpoint_equals_half_the_rate(self):
        # fee_pct = rate * (1 - p). At p=0.5, that's rate * 0.5 = 3.5%
        assert kf.fee_pct_of_stake(0.50) == pytest.approx(0.035)

    def test_higher_pct_on_long_shots(self):
        # Buying at $0.10: paying 7% × 0.90 = 6.3% of the $10 stake
        # (the fee is $0.63 per 100 contracts; cost basis is $10)
        assert kf.fee_pct_of_stake(0.10) == pytest.approx(0.063)

    def test_lower_pct_on_favorites(self):
        # Buying at $0.90: 7% × 0.10 = 0.7%
        assert kf.fee_pct_of_stake(0.90) == pytest.approx(0.007)


class TestRoundTrip:
    def test_round_trip_sums_both_legs(self):
        entry = kf.fee_for_trade(0.40, 100)
        exit_ = kf.fee_for_trade(0.60, 100)
        rt = kf.round_trip_fees(0.40, 0.60, 100, fee_rate=0.07)
        assert rt == pytest.approx(entry + exit_)

    def test_ticker_dispatches_to_sports_rate(self):
        # Same trade, sports ticker → half the fees
        std = kf.round_trip_fees(0.40, 0.60, 100, ticker="KXNEWPOPE")
        sport = kf.round_trip_fees(0.40, 0.60, 100, ticker="KXMLBGAME-X")
        assert sport < std
        assert sport == pytest.approx(std / 2, abs=0.02)


# ─── strategy_runner integration ───────────────────────────────────────


class TestRunnerNetPnlPct:
    """The exit decision uses _net_pnl_pct (net of Kalshi fees), so a
    gross gain that barely clears the fee drag must NOT fire a 30% TP."""

    def test_gross_pct_exceeds_net_pct_at_midpoint_prices(self):
        # Entry $0.40, exit $0.60 → gross = 50%
        # Entry fee % of cost: 0.07 × 0.60 = 4.2%
        # Exit fee % of cost: 0.07 × 0.60 × 0.40 / 0.40 = 4.2%
        # Net = 50% - 8.4% = 41.6%
        gross_pct = (0.60 - 0.40) / 0.40
        net_pct = sr._net_pnl_pct(0.40, 0.60, ticker="KXPOL-X")
        assert gross_pct == pytest.approx(0.50)
        assert net_pct == pytest.approx(0.416, abs=0.005)
        assert net_pct < gross_pct

    def test_thin_gain_doesnt_clear_fees(self):
        """A 5% gross gain near $0.50 gets eaten by fees."""
        # Entry $0.50, exit $0.525 (5% gross)
        net = sr._net_pnl_pct(0.50, 0.525, ticker="KXPOL-X")
        # Gross 5%, fees ~7% of cost combined → net < 0
        assert net < 0.0

    def test_sports_ticker_has_smaller_fee_drag(self):
        std = sr._net_pnl_pct(0.40, 0.60, ticker="KXPOL-X")
        sport = sr._net_pnl_pct(0.40, 0.60, ticker="KXMLBGAME-X")
        # Sports has half the fee rate, so net retention is better
        assert sport > std

    def test_zero_entry_returns_zero(self):
        assert sr._net_pnl_pct(0.0, 0.5, ticker="X") == 0.0


class TestEvaluateExitUsesNetPct:
    """Confirm the exit-rule evaluator gates on NET P&L, not gross."""

    def _entry(self, *, entry=0.40):
        return {
            "entry_price": entry,
            "opened_at": datetime.now(UTC).isoformat(),
            "peak_price": entry,
        }

    def test_take_profit_fires_only_when_net_clears_threshold(self):
        # Gross gain: (0.60 - 0.40) / 0.40 = 50%
        # Net (KXPOL): ~41.6%
        # TP=0.40 (40%) should fire (net=41.6% > 40%)
        policy = sr.ExitPolicy(take_profit_pct=0.40)
        reason = sr._evaluate_exit(
            side_price=0.60, entry=self._entry(),
            policy=policy, now=datetime.now(UTC),
            ticker="KXPOL-X",
        )
        assert reason == "take_profit_pct"

    def test_take_profit_does_NOT_fire_when_only_gross_clears(self):
        """Strategy says +45% would fire — but net is only +41.6%."""
        policy = sr.ExitPolicy(take_profit_pct=0.45)
        reason = sr._evaluate_exit(
            side_price=0.60, entry=self._entry(),
            policy=policy, now=datetime.now(UTC),
            ticker="KXPOL-X",
        )
        # Gross 50% > 45% but net 41.6% < 45% → no fire
        assert reason is None


# ─── backtest engine integration ───────────────────────────────────────


class TestBacktestFeeFnIntegration:
    """Smoke: fee_fn callable is invoked and its return reduces net P&L."""

    def test_fee_fn_overrides_fee_pct(self):
        import pandas as pd

        from pm.research import backtest as bt

        # Build a one-market dataset: market resolves YES, our strategy
        # bets YES at $0.40 (correctly). Without fees, gross PnL = (1.0
        # - 0.40) × stake / 0.40 = 1.5 × stake.
        end = pd.Timestamp.now(tz="UTC")
        prices = pd.DataFrame({
            "timestamp": pd.date_range(end - pd.Timedelta(days=7), end,
                                        periods=20, tz="UTC"),
            "price": [0.40] * 20,
        })
        snap = bt.MarketSnapshot(
            condition_id="KXPOL-X", question="Q", category="Politics",
            as_of=end - pd.Timedelta(hours=24),
            prices=prices, end_date=end,
        )
        res = bt.Resolution(
            condition_id="KXPOL-X",
            resolved_yes=True, closed_time=end,
        )

        def strat(s):
            return bt.Signal(direction="YES", probability=0.70)

        # Run twice: once with no fees, once with a Kalshi-style fee_fn.
        # The fee_fn version should have lower net P&L.
        no_fee = bt.run_backtest(strat, [(snap, res)],
                                  bankroll=1000.0, kelly_multiplier=0.5,
                                  half_spread=0.0, fee_pct=0.0,
                                  portfolio_constrained=False)

        def kalshi_fee(entry_p, exit_p, contracts, ticker):
            return kf.round_trip_fees(
                entry_price=entry_p, exit_price=exit_p,
                contracts=contracts, ticker=ticker,
            )

        with_fee = bt.run_backtest(strat, [(snap, res)],
                                    bankroll=1000.0, kelly_multiplier=0.5,
                                    half_spread=0.0, fee_pct=0.0,
                                    fee_fn=kalshi_fee,
                                    portfolio_constrained=False)
        # Both backtests fire one trade; fee version pays Kalshi fees on
        # entry (exit is $1, zero fee at boundary)
        assert no_fee.n_trades == 1 and with_fee.n_trades == 1
        # with_fee P&L is strictly lower (fees eaten)
        assert with_fee.total_pnl < no_fee.total_pnl
