"""Tests for take-profit, stop-loss, trailing-stop, and time-stop
exit logic in the agentic strategy runner.

The runner's exit-management phase runs between resolution-close and
strategy-open. It marks every open position to current bid, updates
running peaks in ``position_meta``, and fires exits per the configured
``ExitPolicy``. These tests cover each rule in isolation plus the
precedence order, persistence across runner restarts, and the no-op
behavior when ``disable_mgmt=True``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pm.arms import strategy_runner as sr
from pm.ledger import portfolio as pf
from pm.ledger import position_meta as pm
from pm.research import backtest as bt


@pytest.fixture(autouse=True)
def _isolated_profile(tmp_path, monkeypatch):
    """Same isolation as test_strategy_runner, plus the position_meta
    + exits.jsonl files."""
    monkeypatch.setenv("POLYMARKET_PORTFOLIO_FILE",
                       str(tmp_path / "portfolio.json"))
    monkeypatch.setenv("POLYMARKET_FORECASTS_FILE",
                       str(tmp_path / "forecasts.json"))
    monkeypatch.setenv("POLYMARKET_ALERTS_FILE",
                       str(tmp_path / "alerts.json"))
    monkeypatch.setenv("POLYMARKET_POSITION_META_FILE",
                       str(tmp_path / "position_meta.json"))
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "llm_cache.sqlite"))
    # Patch the exits.jsonl path so the runner writes into tmp_path
    monkeypatch.setattr(
        sr, "_exits_log_path",
        lambda profile_name: tmp_path / "exits.jsonl",
    )
    return tmp_path


def _market_row(*, cid="m1", question="Q?", best_bid=0.30, best_ask=0.32,
                category="Crypto"):
    """One live-markets row with the price columns the exit phase reads.
    Mirrors the get_markets() schema; outcome_prices keeps the snapshot
    pipeline happy for the open path."""
    mid = (best_bid + best_ask) / 2
    return {
        "condition_id": cid, "id": cid, "question": question,
        "category": category, "outcome_prices": [mid, 1 - mid],
        "best_bid": best_bid, "best_ask": best_ask,
        "end_date": None,
    }


def _seed_position(cid: str, outcome: str, *, shares: float,
                   entry_price: float, opened_days_ago: float = 0.0,
                   peak_price: float | None = None):
    """Helper: write a buy transaction + matching position_meta entry
    as if it was opened ``opened_days_ago`` days back."""
    pf.add_buy(question="Q?", condition_id=cid, outcome=outcome,
               shares=shares, price=entry_price)
    opened = datetime.now(UTC) - timedelta(days=opened_days_ago)
    pm.record_entry(cid, outcome, entry_price=entry_price, opened_at=opened)
    if peak_price is not None and peak_price > entry_price:
        pm.update_peak(cid, outcome, current_price=peak_price)


# ─── _evaluate_exit pure-function tests ────────────────────────────────


class TestEvaluateExit:
    """Unit tests for the pure exit-rule evaluator (no portfolio I/O)."""

    def _entry(self, *, entry=0.40, opened_at=None, peak=None):
        opened_at = opened_at or datetime.now(UTC)
        return {
            "entry_price": entry,
            "opened_at": opened_at.isoformat(),
            "peak_price": peak if peak is not None else entry,
        }

    def test_take_profit_pct_fires_above_threshold(self):
        policy = sr.ExitPolicy(take_profit_pct=0.50)
        reason = sr._evaluate_exit(
            side_price=0.65, entry=self._entry(entry=0.40),
            policy=policy, now=datetime.now(UTC),
        )
        assert reason == "take_profit_pct"

    def test_take_profit_pct_does_not_fire_below_threshold(self):
        policy = sr.ExitPolicy(take_profit_pct=0.50)
        reason = sr._evaluate_exit(
            side_price=0.55, entry=self._entry(entry=0.40),
            policy=policy, now=datetime.now(UTC),
        )
        assert reason is None  # +37.5% < +50%

    def test_stop_loss_pct_fires_below_threshold(self):
        policy = sr.ExitPolicy(stop_loss_pct=0.40)
        reason = sr._evaluate_exit(
            side_price=0.20, entry=self._entry(entry=0.40),
            policy=policy, now=datetime.now(UTC),
        )
        assert reason == "stop_loss_pct"

    def test_stop_loss_pct_sign_insensitive(self):
        """0.40 and -0.40 should both mean -40%."""
        for s in (0.40, -0.40):
            reason = sr._evaluate_exit(
                side_price=0.20,
                entry=self._entry(entry=0.40),
                policy=sr.ExitPolicy(stop_loss_pct=s),
                now=datetime.now(UTC),
            )
            assert reason == "stop_loss_pct"

    def test_take_profit_price_fires_at_absolute_level(self):
        policy = sr.ExitPolicy(take_profit_price=0.85)
        reason = sr._evaluate_exit(
            side_price=0.86, entry=self._entry(entry=0.40),
            policy=policy, now=datetime.now(UTC),
        )
        assert reason == "take_profit_price"

    def test_trail_stop_fires_when_drawdown_exceeds_threshold(self):
        # Entry 0.40, peaked at 0.80, now back to 0.65 → 18.75% off peak
        policy = sr.ExitPolicy(trail_stop_pct=0.15)
        reason = sr._evaluate_exit(
            side_price=0.65, entry=self._entry(entry=0.40, peak=0.80),
            policy=policy, now=datetime.now(UTC),
        )
        assert reason == "trail_stop"

    def test_trail_stop_fires_when_peak_equals_entry(self):
        """When peak == entry, a drawdown from peak IS a drawdown from
        entry. The trail stop should still protect against loss — without
        it (and no SL configured), the position rides to zero."""
        policy = sr.ExitPolicy(trail_stop_pct=0.10)
        reason = sr._evaluate_exit(
            side_price=0.30,  # 25% below peak/entry
            entry=self._entry(entry=0.40, peak=0.40),
            policy=policy, now=datetime.now(UTC),
        )
        assert reason == "trail_stop"

    def test_time_stop_fires_after_threshold(self):
        old = datetime.now(UTC) - timedelta(days=5)
        policy = sr.ExitPolicy(time_stop_days=3)
        reason = sr._evaluate_exit(
            side_price=0.45,
            entry=self._entry(entry=0.40, opened_at=old),
            policy=policy, now=datetime.now(UTC),
        )
        assert reason == "time_stop"

    def test_stop_loss_precedes_time_stop(self):
        """A deeply-underwater stale position exits via SL, not time stop."""
        old = datetime.now(UTC) - timedelta(days=10)
        policy = sr.ExitPolicy(stop_loss_pct=0.30, time_stop_days=3)
        reason = sr._evaluate_exit(
            side_price=0.20,  # -50% loss
            entry=self._entry(entry=0.40, opened_at=old),
            policy=policy, now=datetime.now(UTC),
        )
        assert reason == "stop_loss_pct"

    def test_disable_mgmt_skipped_at_caller_layer(self):
        """`_evaluate_exit` doesn't read `disable_mgmt` — that gate
        lives in `_manage_open_positions`. Verify here that passing
        a policy with the flag still evaluates normally (the gate is
        elsewhere)."""
        policy = sr.ExitPolicy(disable_mgmt=True, take_profit_pct=0.10)
        reason = sr._evaluate_exit(
            side_price=0.60, entry=self._entry(entry=0.40),
            policy=policy, now=datetime.now(UTC),
        )
        assert reason == "take_profit_pct"

    def test_no_thresholds_set_returns_none(self):
        reason = sr._evaluate_exit(
            side_price=0.99, entry=self._entry(entry=0.40),
            policy=sr.ExitPolicy(), now=datetime.now(UTC),
        )
        assert reason is None


# ─── _position_side_price (paper-sell price selection) ─────────────────


class TestTwoStageTP:
    """The two-stage take-profit: TP hit ARMS a tight trail, doesn't
    sell. Subsequent cycles use that tight trail until reversed."""

    def _entry(self, *, entry=0.40, peak=None, tp_armed=False):
        return {
            "entry_price": entry,
            "opened_at": datetime.now(UTC).isoformat(),
            "peak_price": peak if peak is not None else entry,
            "tp_armed": tp_armed,
        }

    def test_tp_hit_returns_arm_sentinel_when_two_stage_configured(self):
        policy = sr.ExitPolicy(
            take_profit_pct=0.30, tp_to_trail_stop_pct=0.05,
        )
        reason = sr._evaluate_exit(
            side_price=0.65, entry=self._entry(entry=0.40),
            policy=policy, now=datetime.now(UTC),
            ticker="KXPOL-X",
        )
        # Net P&L at 0.40→0.65 is well above 30% → arms instead of
        # firing a sell
        assert reason == "arm_tp"

    def test_tp_hit_without_two_stage_still_sells(self):
        policy = sr.ExitPolicy(take_profit_pct=0.30)
        reason = sr._evaluate_exit(
            side_price=0.65, entry=self._entry(entry=0.40),
            policy=policy, now=datetime.now(UTC),
            ticker="KXPOL-X",
        )
        # Without tp_to_trail_stop_pct, behavior is unchanged
        assert reason == "take_profit_pct"

    def test_armed_position_uses_tight_trail(self):
        """Position was armed at peak=0.70 (entry 0.40, peak 0.70).
        Now price is 0.66 — 5.7% off peak. Tight trail at 5% should fire."""
        policy = sr.ExitPolicy(
            take_profit_pct=0.30, tp_to_trail_stop_pct=0.05,
        )
        reason = sr._evaluate_exit(
            side_price=0.66,
            entry=self._entry(entry=0.40, peak=0.70, tp_armed=True),
            policy=policy, now=datetime.now(UTC),
            ticker="KXPOL-X",
        )
        assert reason == "tp_to_trail"

    def test_armed_position_holds_while_at_peak(self):
        """Armed position currently at peak → no exit, no further
        evaluation downstream either (short-circuits)."""
        policy = sr.ExitPolicy(
            take_profit_pct=0.30, tp_to_trail_stop_pct=0.05,
            trail_stop_pct=0.30,  # would fire if pre-arm trail were used
        )
        reason = sr._evaluate_exit(
            side_price=0.70,
            entry=self._entry(entry=0.40, peak=0.70, tp_armed=True),
            policy=policy, now=datetime.now(UTC),
            ticker="KXPOL-X",
        )
        assert reason is None

    def test_armed_position_still_respects_stop_loss(self):
        """A position that armed at +35% and then crashed should still
        cut on stop-loss."""
        policy = sr.ExitPolicy(
            stop_loss_pct=0.25, take_profit_pct=0.30,
            tp_to_trail_stop_pct=0.05,
        )
        # Entry 0.40, peak 0.70 (was armed), now back to 0.25 — well
        # below stop-loss
        reason = sr._evaluate_exit(
            side_price=0.25,
            entry=self._entry(entry=0.40, peak=0.70, tp_armed=True),
            policy=policy, now=datetime.now(UTC),
            ticker="KXPOL-X",
        )
        assert reason == "stop_loss_pct"

    def test_full_lifecycle_arm_then_ride_then_exit(self, _isolated_profile):
        """End-to-end: open at 0.40, climb to 0.70 (arms), keep
        climbing to 0.80 (peak updates, no exit), then reverse to 0.75
        (5.7% off peak → tp_to_trail fires)."""
        _seed_position("m1", "Yes", shares=100, entry_price=0.40)
        policy = sr.ExitPolicy(
            take_profit_pct=0.30, tp_to_trail_stop_pct=0.05,
        )

        # Cycle 1: price at 0.70 → TP threshold cleared → arm
        df = pd.DataFrame([_market_row(cid="m1",
                                        best_bid=0.70, best_ask=0.72)])
        n, reasons = sr._manage_open_positions(
            profile_name="p", df_markets=df, policy=policy,
            strategy_key="test",
        )
        assert n == 0  # not closed
        assert reasons.get("arm_tp") == 1
        assert pm.get_entry("m1", "Yes")["tp_armed"] is True
        # Position still open
        assert len(pf.get_positions()) == 1

        # Cycle 2: price keeps climbing to 0.80 → peak updates, no exit
        df = pd.DataFrame([_market_row(cid="m1",
                                        best_bid=0.80, best_ask=0.82)])
        n, reasons = sr._manage_open_positions(
            profile_name="p", df_markets=df, policy=policy,
            strategy_key="test",
        )
        assert n == 0
        meta = pm.get_entry("m1", "Yes")
        assert meta["peak_price"] == pytest.approx(0.80)
        assert meta["tp_armed"] is True

        # Cycle 3: price reverses to 0.75 — 6.25% off peak (>5%) → exit
        df = pd.DataFrame([_market_row(cid="m1",
                                        best_bid=0.75, best_ask=0.77)])
        n, reasons = sr._manage_open_positions(
            profile_name="p", df_markets=df, policy=policy,
            strategy_key="test",
        )
        assert n == 1
        assert reasons == {"tp_to_trail": 1}
        assert pf.get_positions().empty
        # Meta cleared
        assert pm.get_entry("m1", "Yes") is None


class TestVolAwareTrail:
    """Volatility-aware trailing stop: drawdown threshold = K × σ_recent."""

    def _entry(self, *, entry=0.40, peak=None):
        return {
            "entry_price": entry,
            "opened_at": datetime.now(UTC).isoformat(),
            "peak_price": peak if peak is not None else entry,
            "tp_armed": False,
        }

    def test_returns_none_when_atr_mult_not_set(self):
        policy = sr.ExitPolicy()
        reason = sr._evaluate_exit(
            side_price=0.50, entry=self._entry(entry=0.40, peak=0.70),
            policy=policy, now=datetime.now(UTC),
            ticker="KXPOL-X", recent_vol_pct=0.05,
        )
        assert reason is None  # No trail configured

    def test_fires_when_drawdown_exceeds_k_sigma(self):
        # σ = 5%, K = 1.5 → threshold = 7.5% drawdown
        # Drawdown = (0.70 - 0.60) / 0.70 = 14.3% > 7.5% → fires
        policy = sr.ExitPolicy(trail_stop_atr_mult=1.5)
        reason = sr._evaluate_exit(
            side_price=0.60, entry=self._entry(entry=0.40, peak=0.70),
            policy=policy, now=datetime.now(UTC),
            ticker="KXPOL-X", recent_vol_pct=0.05,
        )
        assert reason == "trail_stop_atr"

    def test_holds_when_drawdown_below_k_sigma(self):
        # σ = 5%, K = 2.0 → threshold = 10% drawdown
        # Drawdown = (0.70 - 0.67) / 0.70 = 4.3% < 10% → no fire
        policy = sr.ExitPolicy(trail_stop_atr_mult=2.0)
        reason = sr._evaluate_exit(
            side_price=0.67, entry=self._entry(entry=0.40, peak=0.70),
            policy=policy, now=datetime.now(UTC),
            ticker="KXPOL-X", recent_vol_pct=0.05,
        )
        assert reason is None

    def test_higher_vol_means_wider_trail(self):
        """A jumpy market (σ=10%) should give more room than a quiet
        one (σ=2%). Same drawdown should fire in quiet, not jumpy."""
        # Drawdown = 8% (peak 0.70 → current 0.644)
        # K = 2.0; quiet σ=2% → threshold 4% → fires
        # K = 2.0; jumpy σ=10% → threshold 20% → no fire
        policy = sr.ExitPolicy(trail_stop_atr_mult=2.0)
        reason_quiet = sr._evaluate_exit(
            side_price=0.644, entry=self._entry(entry=0.40, peak=0.70),
            policy=policy, now=datetime.now(UTC),
            ticker="KXQUIET", recent_vol_pct=0.02,
        )
        reason_jumpy = sr._evaluate_exit(
            side_price=0.644, entry=self._entry(entry=0.40, peak=0.70),
            policy=policy, now=datetime.now(UTC),
            ticker="KXJUMPY", recent_vol_pct=0.10,
        )
        assert reason_quiet == "trail_stop_atr"
        assert reason_jumpy is None

    def test_falls_back_to_fixed_when_vol_unavailable(self):
        """vol fetch failed (None) but fixed trail is also set → fixed
        trail still works."""
        policy = sr.ExitPolicy(
            trail_stop_pct=0.10, trail_stop_atr_mult=2.0,
        )
        reason = sr._evaluate_exit(
            side_price=0.55, entry=self._entry(entry=0.40, peak=0.70),
            policy=policy, now=datetime.now(UTC),
            ticker="KX-NO-HIST", recent_vol_pct=None,
        )
        # Drawdown = 21.4% > 10% → fixed trail fires
        assert reason == "trail_stop"

    def test_zero_vol_is_treated_as_unavailable(self):
        """If recent_vol_pct is 0 or negative, ignore the vol path
        (don't divide by zero / treat flat history as 'no vol')."""
        policy = sr.ExitPolicy(trail_stop_atr_mult=2.0)
        reason = sr._evaluate_exit(
            side_price=0.10, entry=self._entry(entry=0.40, peak=0.70),
            policy=policy, now=datetime.now(UTC),
            ticker="KXFLAT", recent_vol_pct=0.0,
        )
        # Vol path skipped; no fixed trail set; no fire even though
        # drawdown is enormous (stop-loss would catch it in practice,
        # not configured here)
        assert reason is None


class TestComputeRecentVol:
    """Pure tests of the vol estimator. Pass synthesized DataFrames."""

    def test_returns_none_on_empty_history(self):
        fake = MagicMock()
        fake.get_price_history.return_value = pd.DataFrame()
        assert sr._compute_recent_vol(fake, "X") is None

    def test_returns_none_on_too_few_points(self):
        fake = MagicMock()
        fake.get_price_history.return_value = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=3, freq="1h",
                                        tz="UTC"),
            "price": [0.5, 0.5, 0.5],
        })
        assert sr._compute_recent_vol(fake, "X") is None

    def test_higher_vol_history_returns_higher_sigma(self):
        import numpy as np
        end = pd.Timestamp.now(tz="UTC")
        ts = pd.date_range(end - pd.Timedelta(hours=48), end, periods=48,
                            tz="UTC")
        # Quiet: tiny noise around 0.50
        quiet_prices = 0.50 + 0.005 * np.random.RandomState(42).randn(48)
        # Jumpy: bigger swings
        jumpy_prices = 0.50 + 0.05 * np.random.RandomState(42).randn(48)

        fake_q = MagicMock()
        fake_q.get_price_history.return_value = pd.DataFrame({
            "timestamp": ts, "price": quiet_prices,
        })
        fake_j = MagicMock()
        fake_j.get_price_history.return_value = pd.DataFrame({
            "timestamp": ts, "price": jumpy_prices,
        })
        v_quiet = sr._compute_recent_vol(fake_q, "Q")
        v_jumpy = sr._compute_recent_vol(fake_j, "J")
        assert v_quiet is not None and v_jumpy is not None
        assert v_jumpy > v_quiet * 3  # roughly 10× σ → ~10× returns σ

    def test_handles_provider_error_gracefully(self):
        fake = MagicMock()
        fake.get_price_history.side_effect = RuntimeError("API down")
        assert sr._compute_recent_vol(fake, "X") is None

    def test_no_provider_returns_none(self):
        assert sr._compute_recent_vol(None, "X") is None
        assert sr._compute_recent_vol(MagicMock(), "") is None


class TestVolAwareTrailIntegration:
    """End-to-end: configure a vol-aware trail, run a cycle, verify
    the trail fires (or doesn't) based on synthesized vol."""

    def test_jumpy_market_holds_quiet_market_exits(self, _isolated_profile):
        import numpy as np
        # Two positions, same entry, same current drawdown. The jumpy
        # one's recent history has high vol → wide trail → no exit.
        # The quiet one has low vol → tight trail → exits.
        _seed_position("KXQUIET", "Yes", shares=100, entry_price=0.40,
                       peak_price=0.70)
        _seed_position("KXJUMPY", "Yes", shares=100, entry_price=0.40,
                       peak_price=0.70)

        # Both currently at 0.644 (8% off peak)
        df = pd.DataFrame([
            _market_row(cid="KXQUIET", best_bid=0.644, best_ask=0.65),
            _market_row(cid="KXJUMPY", best_bid=0.644, best_ask=0.65),
        ])

        # Synth histories: quiet σ_returns ≈ 0.02; jumpy ≈ 0.10
        end = pd.Timestamp.now(tz="UTC")
        ts = pd.date_range(end - pd.Timedelta(hours=48), end, periods=48,
                            tz="UTC")
        quiet_h = pd.DataFrame({
            "timestamp": ts,
            "price": 0.50 + 0.01 * np.random.RandomState(1).randn(48),
        })
        jumpy_h = pd.DataFrame({
            "timestamp": ts,
            "price": 0.50 + 0.10 * np.random.RandomState(1).randn(48),
        })

        provider = MagicMock()
        def _hist(ticker, **_):
            if ticker == "KXQUIET":
                return quiet_h
            if ticker == "KXJUMPY":
                return jumpy_h
            return pd.DataFrame()
        provider.get_price_history.side_effect = _hist

        policy = sr.ExitPolicy(trail_stop_atr_mult=2.0)
        n, reasons = sr._manage_open_positions(
            profile_name="p", df_markets=df, policy=policy,
            strategy_key="test", provider=provider,
        )
        # Quiet should exit; jumpy should hold
        assert n == 1
        assert reasons == {"trail_stop_atr": 1}
        positions = pf.get_positions()
        # Only KXJUMPY remains
        assert len(positions) == 1
        assert positions.iloc[0]["condition_id"] == "KXJUMPY"


class TestPositionSidePrice:
    def test_yes_uses_yes_bid(self):
        info = {"yes_bid": 0.65, "yes_ask": 0.70, "yes_mid": 0.675}
        assert sr._position_side_price("Yes", info) == pytest.approx(0.65)

    def test_no_uses_one_minus_yes_ask(self):
        info = {"yes_bid": 0.65, "yes_ask": 0.70, "yes_mid": 0.675}
        # NO bid = 1 - YES ask = 0.30
        assert sr._position_side_price("No", info) == pytest.approx(0.30)

    def test_yes_falls_back_to_mid_when_bid_missing(self):
        info = {"yes_bid": None, "yes_ask": 0.70, "yes_mid": 0.675}
        assert sr._position_side_price("Yes", info) == pytest.approx(0.675)

    def test_returns_none_when_no_price_info(self):
        info = {"yes_bid": None, "yes_ask": None, "yes_mid": None}
        assert sr._position_side_price("Yes", info) is None


# ─── _manage_open_positions (integration with portfolio + meta) ────────


class TestManageOpenPositions:
    def test_disable_mgmt_is_noop(self):
        _seed_position("m1", "Yes", shares=100, entry_price=0.40)
        df = pd.DataFrame([_market_row(cid="m1", best_bid=0.90, best_ask=0.91)])
        n, reasons = sr._manage_open_positions(
            profile_name="p", df_markets=df,
            policy=sr.ExitPolicy(disable_mgmt=True, take_profit_pct=0.10),
            strategy_key="test",
        )
        assert n == 0 and reasons == {}
        # Position still open
        assert len(pf.get_positions()) == 1

    def test_take_profit_pct_closes_position_and_logs(self, _isolated_profile):
        _seed_position("m1", "Yes", shares=100, entry_price=0.40)
        df = pd.DataFrame([_market_row(cid="m1", best_bid=0.70, best_ask=0.72)])
        n, reasons = sr._manage_open_positions(
            profile_name="p", df_markets=df,
            policy=sr.ExitPolicy(take_profit_pct=0.50),
            strategy_key="test",
        )
        assert n == 1
        assert reasons == {"take_profit_pct": 1}
        # Position closed in portfolio
        assert pf.get_positions().empty
        # Realized P&L logged
        realized = pf.get_realized_pnl()
        assert len(realized) == 1
        assert float(realized.iloc[0]["realized_pnl"]) == pytest.approx(
            (0.70 - 0.40) * 100, rel=1e-3,
        )
        # exits.jsonl row written
        exits_path = _isolated_profile / "exits.jsonl"
        assert exits_path.exists()
        # position_meta cleared
        assert pm.get_entry("m1", "Yes") is None

    def test_no_side_position_uses_one_minus_yes_ask(self):
        """A NO position should mark to (1 - yes_ask)."""
        _seed_position("m1", "No", shares=100, entry_price=0.40)
        # YES ask = 0.30 → NO bid = 0.70 → +75% on the NO side
        df = pd.DataFrame([_market_row(cid="m1", best_bid=0.28, best_ask=0.30)])
        n, reasons = sr._manage_open_positions(
            profile_name="p", df_markets=df,
            policy=sr.ExitPolicy(take_profit_pct=0.50),
            strategy_key="test",
        )
        assert n == 1
        assert reasons == {"take_profit_pct": 1}

    def test_trail_stop_uses_persisted_peak(self):
        """The peak must survive a runner cycle that didn't trigger an
        exit — otherwise the trailing stop resets every poll."""
        _seed_position("m1", "Yes", shares=100, entry_price=0.40,
                       peak_price=0.80)
        # Now price is back at 0.65 → 18.75% off peak. Trail at 15%.
        df = pd.DataFrame([_market_row(cid="m1", best_bid=0.65, best_ask=0.67)])
        n, reasons = sr._manage_open_positions(
            profile_name="p", df_markets=df,
            policy=sr.ExitPolicy(trail_stop_pct=0.15),
            strategy_key="test",
        )
        assert n == 1
        assert reasons == {"trail_stop": 1}

    def test_peak_updates_when_price_climbs(self):
        _seed_position("m1", "Yes", shares=100, entry_price=0.40)
        df = pd.DataFrame([_market_row(cid="m1", best_bid=0.55, best_ask=0.57)])
        # No threshold → no exit, just peak update
        sr._manage_open_positions(
            profile_name="p", df_markets=df,
            policy=sr.ExitPolicy(),
            strategy_key="test",
        )
        meta = pm.get_entry("m1", "Yes")
        assert meta is not None
        assert meta["peak_price"] == pytest.approx(0.55)

    def test_missing_market_skips_without_error(self):
        """Position exists, but the market isn't in this cycle's feed
        (e.g. paginated past the limit). Should not raise."""
        _seed_position("m1", "Yes", shares=100, entry_price=0.40)
        df = pd.DataFrame([_market_row(cid="OTHER")])  # m1 absent
        n, reasons = sr._manage_open_positions(
            profile_name="p", df_markets=df,
            policy=sr.ExitPolicy(take_profit_pct=0.10),
            strategy_key="test",
        )
        assert n == 0 and reasons == {}
        # Position untouched
        assert len(pf.get_positions()) == 1

    def test_backfills_meta_when_missing(self):
        """A position created before this code shipped won't have a
        position_meta row. The manager should backfill from
        portfolio.avg_price + added_at on first sight."""
        pf.add_buy(question="Q?", condition_id="m1", outcome="Yes",
                   shares=100, price=0.40)
        # NOTE: no pm.record_entry — simulating legacy data
        assert pm.get_entry("m1", "Yes") is None

        df = pd.DataFrame([_market_row(cid="m1", best_bid=0.50, best_ask=0.52)])
        sr._manage_open_positions(
            profile_name="p", df_markets=df,
            policy=sr.ExitPolicy(),  # no exit, just backfill
            strategy_key="test",
        )
        meta = pm.get_entry("m1", "Yes")
        assert meta is not None
        assert meta["entry_price"] == pytest.approx(0.40)


# ─── Exit phase integration with run_cycle ─────────────────────────────


class TestRunCycleWithExits:
    @patch.object(sr.provider_mod, "get_provider")
    @patch.object(sr.provider_mod, "list_providers", return_value=[])
    def test_full_cycle_with_take_profit(
        self, _list, mock_get_provider, _isolated_profile,
    ):
        """End-to-end: seed an open position, run a cycle with TP=20%,
        position should exit and stats should reflect it."""
        _seed_position("m1", "Yes", shares=100, entry_price=0.40)

        fake = MagicMock()
        # Market priced way up — TP should fire on the existing position
        # but NOT open a new one (no signal returned).
        fake.get_markets.return_value = pd.DataFrame([
            _market_row(cid="m1", best_bid=0.70, best_ask=0.72),
        ])
        mock_get_provider.return_value = fake

        @bt.register_strategy("_runner_tp_test", "test")
        def _strat(snap):
            return None  # no new signals; we just want to test exits

        try:
            stats = sr.run_cycle(
                strategy_key="_runner_tp_test",
                bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
                max_position_pct=0.10, min_gap=0.05, max_markets=10,
                profile_name="test_profile",
                exit_policy=sr.ExitPolicy(take_profit_pct=0.20),
            )
        finally:
            bt.STRATEGIES.pop("_runner_tp_test", None)

        assert stats.managed_exits == 1
        assert stats.exit_reasons == {"take_profit_pct": 1}
        assert stats.positions_opened == 0
        assert pf.get_positions().empty

    @patch.object(sr.provider_mod, "get_provider")
    @patch.object(sr.provider_mod, "list_providers", return_value=[])
    def test_disable_mgmt_legacy_behavior(
        self, _list, mock_get_provider,
    ):
        """With disable_mgmt=True the runner ignores all exit rules
        and behaves like the pre-exits baseline. A deeply-in-profit
        position stays open."""
        _seed_position("m1", "Yes", shares=100, entry_price=0.40)
        fake = MagicMock()
        fake.get_markets.return_value = pd.DataFrame([
            _market_row(cid="m1", best_bid=0.95, best_ask=0.96),
        ])
        mock_get_provider.return_value = fake

        @bt.register_strategy("_runner_disabled_mgmt", "test")
        def _strat(snap):
            return None
        try:
            stats = sr.run_cycle(
                strategy_key="_runner_disabled_mgmt",
                bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
                max_position_pct=0.10, min_gap=0.05, max_markets=10,
                profile_name="test_profile",
                exit_policy=sr.ExitPolicy(
                    disable_mgmt=True, take_profit_pct=0.10,
                ),
            )
        finally:
            bt.STRATEGIES.pop("_runner_disabled_mgmt", None)

        assert stats.managed_exits == 0
        assert len(pf.get_positions()) == 1

    @patch.object(sr.provider_mod, "get_provider")
    def test_resolution_close_takes_precedence(
        self, mock_get_provider, _isolated_profile,
    ):
        """A position whose market resolved between cycles should be
        settled at $1/$0 by the resolve-phase BEFORE the manage-phase
        gets a chance to fire TP/SL. The exits.jsonl reason should be
        resolution_won / resolution_lost, not take_profit_pct."""
        _seed_position("m1", "Yes", shares=100, entry_price=0.40)

        # Provider says market m1 is resolved YES (we win)
        provider = MagicMock()
        provider.name = "fake"
        provider.get_closed_markets.return_value = pd.DataFrame([{
            "condition_id": "m1", "resolved_yes": True,
        }])
        provider.get_markets.return_value = pd.DataFrame([
            # Market still shows up live this cycle priced at $0.70 —
            # would have hit TP if resolve-phase didn't pre-empt.
            _market_row(cid="m1", best_bid=0.70, best_ask=0.72),
        ])

        with patch.object(sr.provider_mod, "list_providers",
                          return_value=[provider]):
            mock_get_provider.return_value = provider

            @bt.register_strategy("_runner_resolve_pre", "test")
            def _strat(snap):
                return None

            try:
                stats = sr.run_cycle(
                    strategy_key="_runner_resolve_pre",
                    bankroll=1000.0, kelly_multiplier=0.5, half_spread=0.0,
                    max_position_pct=0.10, min_gap=0.05, max_markets=10,
                    profile_name="test_profile",
                    exit_policy=sr.ExitPolicy(take_profit_pct=0.20),
                )
            finally:
                bt.STRATEGIES.pop("_runner_resolve_pre", None)

        assert stats.resolved_closed == 1
        assert stats.managed_exits == 0  # resolve pre-empted

        # exits.jsonl row should be resolution_won at $1.00
        import json
        rows = [
            json.loads(line)
            for line in (_isolated_profile / "exits.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["exit_reason"] == "resolution_won"
        assert rows[0]["exit_price"] == 1.0


# ─── position_meta persistence (smoke check across reloads) ────────────


class TestPositionMetaPersistence:
    def test_peak_survives_reload(self, _isolated_profile):
        pm.record_entry("m1", "Yes", entry_price=0.40)
        pm.update_peak("m1", "Yes", current_price=0.75)

        # Re-load from disk
        meta = pm.get_entry("m1", "Yes")
        assert meta["peak_price"] == pytest.approx(0.75)
        assert meta["entry_price"] == pytest.approx(0.40)

    def test_peak_does_not_ratchet_down(self):
        pm.record_entry("m1", "Yes", entry_price=0.40)
        pm.update_peak("m1", "Yes", current_price=0.80)
        pm.update_peak("m1", "Yes", current_price=0.50)
        assert pm.get_entry("m1", "Yes")["peak_price"] == pytest.approx(0.80)

    def test_clear_entry(self):
        pm.record_entry("m1", "Yes", entry_price=0.40)
        assert pm.clear_entry("m1", "Yes") is True
        assert pm.get_entry("m1", "Yes") is None
        # Idempotent: clearing twice is fine
        assert pm.clear_entry("m1", "Yes") is False

    def test_record_entry_idempotent(self):
        """A re-buy on an existing position keeps the original entry
        so the peak/trail math stays interpretable. Average-cost
        accounting lives in portfolio.py, NOT here — this is exit
        bookkeeping only."""
        first = pm.record_entry("m1", "Yes", entry_price=0.40)
        second = pm.record_entry("m1", "Yes", entry_price=0.55)
        assert first["entry_price"] == second["entry_price"] == 0.40


# ─── exits_analysis ────────────────────────────────────────────────────


class TestExitsAnalysis:
    def test_load_exits_empty_file_returns_empty_df(self, _isolated_profile):
        from pm.ledger import exits_analysis as ea
        # Patch the path to a known empty location
        with patch.object(ea, "_exits_path",
                          return_value=Path(_isolated_profile) / "nope.jsonl"):
            df = ea.load_exits(profile="ignored")
        assert df.empty
        assert "exit_reason" in df.columns

    def test_exit_reason_summary(self, _isolated_profile):
        from pm.ledger import exits_analysis as ea
        # Seed two exits via the runner's logger
        sr._log_exit(
            profile_name="p", condition_id="m1", question="Q1",
            outcome="Yes", shares=100, entry_price=0.40, exit_price=0.60,
            peak_price=0.65, opened_at=datetime.now(UTC).isoformat(),
            exit_reason="take_profit_pct", strategy_key="test",
        )
        sr._log_exit(
            profile_name="p", condition_id="m2", question="Q2",
            outcome="No", shares=100, entry_price=0.50, exit_price=0.30,
            peak_price=0.50, opened_at=datetime.now(UTC).isoformat(),
            exit_reason="stop_loss_pct", strategy_key="test",
        )
        with patch.object(ea, "_exits_path",
                          return_value=Path(_isolated_profile) / "exits.jsonl"):
            summary = ea.exit_reason_summary(profile="p")
        assert len(summary) == 2
        reasons = set(summary["exit_reason"])
        assert reasons == {"take_profit_pct", "stop_loss_pct"}

    def test_counterfactual_pnl(self, _isolated_profile):
        from pm.ledger import exits_analysis as ea
        sr._log_exit(
            profile_name="p", condition_id="m1", question="Q",
            outcome="Yes", shares=100, entry_price=0.40, exit_price=0.60,
            peak_price=0.65, opened_at=datetime.now(UTC).isoformat(),
            exit_reason="take_profit_pct", strategy_key="test",
        )
        with patch.object(ea, "_exits_path",
                          return_value=Path(_isolated_profile) / "exits.jsonl"):
            df = ea.load_exits(profile="p")
        # Hold-to-resolution accounting (net of Kalshi fees):
        #   gross_pnl   = (1.0 - 0.40) * 100               = $60.00
        #   entry_fee   = ceil(0.07 * 0.40 * 0.60 * 100*100)/100 = $1.68
        #   exit_fee    = 0.07 * 1.0 * 0.0 * 100           = $0.00
        #   net cf      = 60 - 1.68                        = $58.32
        # Actual exit accounting (already in log):
        #   gross_pnl   = (0.60 - 0.40) * 100              = $20.00
        #   fees_paid   = 1.68 (entry) + 1.68 (exit)       = $3.36
        #   net actual  = 20 - 3.36                        = $16.64
        # alpha = net_actual - net_cf = 16.64 - 58.32      = -$41.68
        with_cf = ea.counterfactual_pnl(df, {"m1": True})
        assert with_cf.iloc[0]["counterfactual_pnl_dollars"] == pytest.approx(58.32)
        assert with_cf.iloc[0]["exit_alpha_dollars"] == pytest.approx(-41.68)
