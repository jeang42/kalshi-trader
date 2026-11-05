"""Tests for the agentic strategy runner."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pm.arms import strategy_runner as sr
from pm.ledger import calibration as cal
from pm.ledger import portfolio as pf
from pm.research import backtest as bt


@pytest.fixture(autouse=True)
def _isolated_profile(tmp_path, monkeypatch):
    """Redirect every per-profile JSON file to a tempdir + redirect
    the LLM cache so runner test runs don't pollute each other or
    real on-disk state."""
    monkeypatch.setenv("POLYMARKET_PORTFOLIO_FILE",
                       str(tmp_path / "portfolio.json"))
    monkeypatch.setenv("POLYMARKET_FORECASTS_FILE",
                       str(tmp_path / "forecasts.json"))
    monkeypatch.setenv("POLYMARKET_ALERTS_FILE",
                       str(tmp_path / "alerts.json"))
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "llm_cache.sqlite"))


def _market_row(*, cid="m1", question="Q?", yes_price=0.30,
                category="Crypto"):
    return {
        "condition_id": cid, "id": cid, "question": question,
        "category": category, "outcome_prices": [yes_price, 1 - yes_price],
        "end_date": None,
    }


# ---------------------------------------------------------------------------
# Snapshot building
# ---------------------------------------------------------------------------

class TestBuildSnapshotForMarket:
    def test_basic(self):
        snap = sr._build_snapshot_for_market(pd.Series(_market_row()))
        assert snap is not None
        assert snap.condition_id == "m1"
        assert snap.latest_price == pytest.approx(0.30)

    def test_no_outcome_prices_returns_none(self):
        row = _market_row()
        row["outcome_prices"] = []
        assert sr._build_snapshot_for_market(pd.Series(row)) is None


# ---------------------------------------------------------------------------
# run_cycle — open positions for flagged signals
# ---------------------------------------------------------------------------

class TestRunCycleOpenPath:
    @patch.object(sr.provider_mod, "get_provider")
    @patch.object(sr.provider_mod, "list_providers", return_value=[])
    def test_emits_buy_and_forecast_on_signal(self, _list, mock_get_provider):
        # Build a fake provider with one market priced at 0.30
        fake_provider = MagicMock()
        fake_provider.get_markets.return_value = pd.DataFrame([
            _market_row(cid="m1", yes_price=0.30),
        ])
        mock_get_provider.return_value = fake_provider

        # Register a deterministic test strategy that always says YES
        # at probability 0.50 (above the 0.30 market → real edge)
        @bt.register_strategy("_runner_test_buy", "test buy")
        def _strat(snap):
            return bt.Signal(direction="YES", probability=0.50)

        try:
            stats = sr.run_cycle(
                strategy_key="_runner_test_buy",
                bankroll=1000.0, kelly_multiplier=0.5,
                half_spread=0.0, max_position_pct=0.10,
                min_gap=0.05, max_markets=10,
                profile_name="test_profile",
            )
        finally:
            bt.STRATEGIES.pop("_runner_test_buy", None)

        assert stats.signals_emitted == 1
        assert stats.positions_opened == 1
        assert stats.skipped_no_edge == 0

        # The buy landed in the portfolio
        positions = pf.get_positions()
        assert len(positions) == 1
        assert positions.iloc[0]["outcome"] == "Yes"

        # The forecast landed in the calibration log
        forecasts = cal.get_forecasts()
        assert len(forecasts) == 1
        # Source is namespaced by strategy key
        assert forecasts.iloc[0]["source"] == "agentic:_runner_test_buy"

    @patch.object(sr.provider_mod, "get_provider")
    @patch.object(sr.provider_mod, "list_providers", return_value=[])
    def test_skips_existing_open_position(self, _list, mock_get_provider):
        # Pre-seed the portfolio with an open position on m1
        pf.add_buy(question="Q?", condition_id="m1", outcome="Yes",
                   shares=10.0, price=0.30)

        fake = MagicMock()
        fake.get_markets.return_value = pd.DataFrame([
            _market_row(cid="m1", yes_price=0.30),
        ])
        mock_get_provider.return_value = fake

        @bt.register_strategy("_runner_test_skip", "test skip")
        def _strat(snap):
            return bt.Signal(direction="YES", probability=0.50)

        try:
            stats = sr.run_cycle(
                strategy_key="_runner_test_skip", bankroll=1000.0,
                kelly_multiplier=0.5, half_spread=0.0,
                max_position_pct=0.10, min_gap=0.05, max_markets=10,
                profile_name="test_profile",
            )
        finally:
            bt.STRATEGIES.pop("_runner_test_skip", None)

        assert stats.skipped_existing == 1
        assert stats.positions_opened == 0
        # Still just the original position
        assert len(pf.get_positions()) == 1

    @patch.object(sr.provider_mod, "get_provider")
    @patch.object(sr.provider_mod, "list_providers", return_value=[])
    def test_min_gap_filters_thin_edges(self, _list, mock_get_provider):
        fake = MagicMock()
        fake.get_markets.return_value = pd.DataFrame([
            _market_row(cid="m1", yes_price=0.50),
        ])
        mock_get_provider.return_value = fake

        @bt.register_strategy("_runner_thin_edge", "thin")
        def _strat(snap):
            # 0.52 vs 0.50 = 2pp gap, below the default 5pp threshold
            return bt.Signal(direction="YES", probability=0.52)

        try:
            stats = sr.run_cycle(
                strategy_key="_runner_thin_edge", bankroll=1000.0,
                kelly_multiplier=0.5, half_spread=0.0,
                max_position_pct=0.10, min_gap=0.05, max_markets=10,
                profile_name="test_profile",
            )
        finally:
            bt.STRATEGIES.pop("_runner_thin_edge", None)

        assert stats.signals_emitted == 1
        assert stats.positions_opened == 0
        assert stats.skipped_no_edge == 1

    @patch.object(sr.provider_mod, "get_provider")
    @patch.object(sr.provider_mod, "list_providers", return_value=[])
    def test_max_position_pct_caps_stake(self, _list, mock_get_provider):
        # Kelly says 100% but cap is 10% of $1000 = $100
        fake = MagicMock()
        fake.get_markets.return_value = pd.DataFrame([
            _market_row(cid="m1", yes_price=0.40),
        ])
        mock_get_provider.return_value = fake

        @bt.register_strategy("_runner_cap", "cap")
        def _strat(snap):
            return bt.Signal(direction="YES", probability=0.99)  # huge edge

        try:
            sr.run_cycle(
                strategy_key="_runner_cap", bankroll=1000.0,
                kelly_multiplier=1.0, half_spread=0.0,
                max_position_pct=0.10, min_gap=0.05, max_markets=10,
                profile_name="test_profile",
            )
        finally:
            bt.STRATEGIES.pop("_runner_cap", None)

        positions = pf.get_positions()
        # 10% cap × $1000 = $100 stake; shares = 100 / 0.40 = 250
        assert positions.iloc[0]["shares"] == pytest.approx(250.0, rel=1e-3)


# ---------------------------------------------------------------------------
# run_cycle — close resolved positions
# ---------------------------------------------------------------------------

class TestRunCycleResolutionPath:
    @patch.object(sr.provider_mod, "list_providers")
    @patch.object(sr.provider_mod, "get_provider")
    def test_closes_open_position_at_resolution_won(
        self, mock_get_provider, mock_list,
    ):
        # Open position on m1
        pf.add_buy(question="Q?", condition_id="m1", outcome="Yes",
                   shares=100.0, price=0.30)

        # Provider for the open-loop returns no markets (so we just
        # exercise the close path)
        fake_active = MagicMock()
        fake_active.get_markets.return_value = pd.DataFrame()
        mock_get_provider.return_value = fake_active

        # The list_providers path is used for the close phase — its
        # get_closed_markets says m1 resolved YES.
        fake_closed = MagicMock()
        fake_closed.name = "fakevenue"
        fake_closed.get_closed_markets.return_value = pd.DataFrame([{
            "condition_id": "m1", "resolved_yes": True,
            "closed_time_dt": pd.Timestamp("2026-05-15", tz="UTC"),
            "question": "Q?",
        }])
        mock_list.return_value = [fake_closed]

        stats = sr.run_cycle(
            strategy_key="market_consensus",
            bankroll=1000.0, kelly_multiplier=0.5,
            half_spread=0.0, max_position_pct=0.10,
            min_gap=0.05, max_markets=10, profile_name="test_profile",
        )

        assert stats.resolved_closed == 1
        # Position is now closed — get_positions returns nothing
        # because the sell zeroed out the holding
        positions = pf.get_positions()
        assert positions.empty

        # And realized PnL reflects the win: 100 shares × ($1 − $0.30) = $70
        realized = pf.get_realized_pnl()
        assert realized.iloc[0]["realized_pnl"] == pytest.approx(70.0, rel=1e-3)

    @patch.object(sr.provider_mod, "list_providers")
    @patch.object(sr.provider_mod, "get_provider")
    def test_closes_at_zero_when_direction_lost(
        self, mock_get_provider, mock_list,
    ):
        pf.add_buy(question="Q?", condition_id="m1", outcome="Yes",
                   shares=100.0, price=0.30)

        fake_active = MagicMock()
        fake_active.get_markets.return_value = pd.DataFrame()
        mock_get_provider.return_value = fake_active

        fake_closed = MagicMock()
        fake_closed.name = "fakevenue"
        # YES position, market resolved NO → loss
        fake_closed.get_closed_markets.return_value = pd.DataFrame([{
            "condition_id": "m1", "resolved_yes": False,
            "closed_time_dt": pd.Timestamp("2026-05-15", tz="UTC"),
            "question": "Q?",
        }])
        mock_list.return_value = [fake_closed]

        sr.run_cycle(
            strategy_key="market_consensus", bankroll=1000.0,
            kelly_multiplier=0.5, half_spread=0.0,
            max_position_pct=0.10, min_gap=0.05, max_markets=10,
            profile_name="test_profile",
        )

        # Sell at $0; realized PnL = -stake = -$30
        realized = pf.get_realized_pnl()
        assert realized.iloc[0]["realized_pnl"] == pytest.approx(-30.0, rel=1e-3)


# ---------------------------------------------------------------------------
# Agentic-profile naming convention
# ---------------------------------------------------------------------------

class TestIsAgenticProfile:
    """The Settings page uses this to render the runner-banner. Multiple
    parallel experiments can each have their own profile via the
    `agentic_*` prefix convention."""

    def test_bare_agentic(self):
        from pm.core import profiles
        assert profiles.is_agentic_profile("agentic")

    def test_prefixed_variants(self):
        from pm.core import profiles
        assert profiles.is_agentic_profile("agentic_favorites")
        assert profiles.is_agentic_profile("agentic_grind")
        assert profiles.is_agentic_profile("agentic_llm")

    def test_non_agentic_profiles(self):
        from pm.core import profiles
        assert not profiles.is_agentic_profile("jeff")
        assert not profiles.is_agentic_profile("sandbox")
        assert not profiles.is_agentic_profile("dex")
        # Lookalike but not the prefix
        assert not profiles.is_agentic_profile("agent")
        assert not profiles.is_agentic_profile("agenticfavorites")


# ---------------------------------------------------------------------------
# --strategy-arg CLI helper
# ---------------------------------------------------------------------------

class TestParseStrategyArgs:
    def test_empty_list(self):
        assert sr._parse_strategy_args([]) == {}

    def test_int_coerced(self):
        # Whole-number values that fit int should parse as int
        assert sr._parse_strategy_args(["max_markets=25"]) == {"max_markets": 25}

    def test_float_coerced(self):
        # Decimal values parse as float
        assert sr._parse_strategy_args(["min_gap=0.15"]) == {"min_gap": 0.15}

    def test_bool_coerced_both_cases(self):
        assert sr._parse_strategy_args(["llm_verify=true"]) == {"llm_verify": True}
        assert sr._parse_strategy_args(["llm_verify=False"]) == {"llm_verify": False}

    def test_string_fallthrough(self):
        # Model names like "qwen2.5:14b" don't parse as int/float/bool
        # → stay strings. The colon is the gotcha that catches a naive
        # implementation.
        assert sr._parse_strategy_args(["model=qwen2.5:14b"]) == {
            "model": "qwen2.5:14b",
        }

    def test_multiple_args(self):
        out = sr._parse_strategy_args([
            "model=llama3.1:latest",
            "min_gap=0.15",
            "max_markets=10",
        ])
        assert out == {
            "model": "llama3.1:latest",
            "min_gap": 0.15,
            "max_markets": 10,
        }

    def test_bare_key_is_true(self):
        # `--strategy-arg llm_verify` (no value) → llm_verify=True
        assert sr._parse_strategy_args(["llm_verify"]) == {"llm_verify": True}

    def test_empty_key_skipped(self):
        # "=value" with no key shouldn't crash; just drop it
        assert sr._parse_strategy_args(["=value"]) == {}

    def test_whitespace_trimmed(self):
        assert sr._parse_strategy_args([" model = qwen "]) == {"model": "qwen"}


class TestRunCycleStrategyKwargs:
    """The runner binds --strategy-arg values into the strategy call via
    functools.partial. Verify kwargs actually reach the strategy."""

    @patch.object(sr.provider_mod, "get_provider")
    @patch.object(sr.provider_mod, "list_providers", return_value=[])
    def test_kwargs_threaded_into_strategy(self, _list, mock_get_provider):
        fake = MagicMock()
        fake.get_markets.return_value = pd.DataFrame([
            _market_row(cid="m1", yes_price=0.30),
        ])
        mock_get_provider.return_value = fake

        captured: dict = {}

        @bt.register_strategy("_runner_kwarg_capture", "kwarg test")
        def _strat(snap, *, my_param=42, my_flag=False):
            captured["my_param"] = my_param
            captured["my_flag"] = my_flag
            return bt.Signal(direction="YES", probability=0.50)

        try:
            sr.run_cycle(
                strategy_key="_runner_kwarg_capture",
                bankroll=1000.0, kelly_multiplier=0.5,
                half_spread=0.0, max_position_pct=0.10,
                min_gap=0.05, max_markets=10,
                profile_name="test_profile",
                strategy_kwargs={"my_param": 99, "my_flag": True},
            )
        finally:
            bt.STRATEGIES.pop("_runner_kwarg_capture", None)

        assert captured == {"my_param": 99, "my_flag": True}


# ---------------------------------------------------------------------------
# Profile save/restore — regression guard for the bug where cron-fired
# runs were silently flipping the Streamlit UI's active_profile
# ---------------------------------------------------------------------------

class TestMainRestoresActiveProfile:
    def test_active_profile_restored_after_single_cycle(
        self, tmp_path, monkeypatch
    ):
        """`main()` must restore the prior active_profile after a single
        non-loop cycle, even if `run_cycle` succeeded mid-run."""
        from pm.arms import strategy_runner as sr
        from pm.core import profiles

        # Isolated profiles root + clean state
        monkeypatch.setattr(profiles, "PROFILES_ROOT", tmp_path)
        monkeypatch.setattr(profiles, "ACTIVE_PROFILE_FILE",
                             tmp_path / ".active_profile")
        for name in profiles.DEFAULT_PROFILES:
            (tmp_path / name).mkdir(parents=True, exist_ok=True)
        profiles.set_active_profile("jeff")
        assert profiles.active_profile() == "jeff"

        # Stub run_cycle so we don't touch the network
        import datetime
        monkeypatch.setattr(sr, "run_cycle",
                             lambda **kw: sr.RunnerStats(
                                 cycle_started=datetime.datetime.now(
                                     datetime.UTC).isoformat()))
        # Avoid plugin discovery side-effects
        monkeypatch.setattr(sr.bt, "discover_user_strategies", lambda: 0)

        # Single cycle on a different profile
        monkeypatch.setattr(sys, "argv", [
            "strategy_runner", "--strategy", "market_consensus",
            "--profile", "sandbox",
        ])
        rc = sr.main()
        assert rc == 0

        # CRITICAL: prior selection survived
        assert profiles.active_profile() == "jeff", (
            "main() leaked the runner's profile into the shared "
            "active_profile file — cron will steal the UI's selection."
        )

    def test_active_profile_restored_even_on_cycle_failure(
        self, tmp_path, monkeypatch
    ):
        """If `run_cycle` raises, the profile still gets restored."""
        from pm.arms import strategy_runner as sr
        from pm.core import profiles

        monkeypatch.setattr(profiles, "PROFILES_ROOT", tmp_path)
        monkeypatch.setattr(profiles, "ACTIVE_PROFILE_FILE",
                             tmp_path / ".active_profile")
        for name in profiles.DEFAULT_PROFILES:
            (tmp_path / name).mkdir(parents=True, exist_ok=True)
        profiles.set_active_profile("jeff")

        def boom(**kw):
            raise RuntimeError("simulated network failure")

        monkeypatch.setattr(sr, "run_cycle", boom)
        monkeypatch.setattr(sr.bt, "discover_user_strategies", lambda: 0)

        monkeypatch.setattr(sys, "argv", [
            "strategy_runner", "--strategy", "market_consensus",
            "--profile", "sandbox",
        ])
        rc = sr.main()
        assert rc == 1  # cycle failure → exit 1
        # But the profile MUST still be restored.
        assert profiles.active_profile() == "jeff"
