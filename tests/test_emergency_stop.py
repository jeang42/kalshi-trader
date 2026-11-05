"""Tests for the emergency-stop kill switch + its integrations with
strategy_runner and kalshi_orders."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from pm.core import emergency_stop as es


@pytest.fixture(autouse=True)
def _flag_in_tmp(tmp_path, monkeypatch):
    """Redirect the flag file to a tempdir per test."""
    flag = tmp_path / ".emergency_stop"
    monkeypatch.setenv("POLYMARKET_EMERGENCY_STOP_FILE", str(flag))
    yield flag
    if flag.exists():
        flag.unlink()


class TestFlagLifecycle:
    def test_inactive_when_no_flag(self, _flag_in_tmp):
        assert not es.is_active()
        assert es.get_status() is None

    def test_activate_creates_file(self, _flag_in_tmp):
        es.activate("test")
        assert _flag_in_tmp.exists()
        assert es.is_active()

    def test_status_returns_since_and_reason(self, _flag_in_tmp):
        es.activate("market is crashing")
        st = es.get_status()
        assert st is not None
        assert st["active"] is True
        assert st["reason"] == "market is crashing"
        # since is an ISO timestamp
        assert "T" in st["since"] and st["since"].endswith("+00:00")

    def test_activate_without_reason(self, _flag_in_tmp):
        es.activate()
        st = es.get_status()
        assert st["reason"] == ""

    def test_activate_is_idempotent(self, _flag_in_tmp):
        es.activate("first")
        es.activate("second")  # updates reason
        st = es.get_status()
        assert st["reason"] == "second"

    def test_deactivate_removes_flag(self, _flag_in_tmp):
        es.activate("test")
        assert es.deactivate() is True
        assert not _flag_in_tmp.exists()
        assert not es.is_active()

    def test_deactivate_when_inactive_is_noop(self, _flag_in_tmp):
        assert es.deactivate() is False  # nothing to remove

    def test_malformed_flag_file_is_still_active(self, _flag_in_tmp):
        """Even a garbled flag file should be treated as active —
        safe-default behavior."""
        _flag_in_tmp.write_text("")  # empty file
        assert es.is_active() is True
        st = es.get_status()
        assert st["active"] is True
        assert st["since"] == ""
        assert st["reason"] == ""


class TestCheckOrRaise:
    def test_no_raise_when_inactive(self, _flag_in_tmp):
        # Doesn't raise
        es.check_or_raise(context="test")

    def test_raises_when_active(self, _flag_in_tmp):
        es.activate("the test reason")
        with pytest.raises(es.EmergencyStopActive) as exc:
            es.check_or_raise(context="placing order")
        msg = str(exc.value)
        assert "placing order" in msg
        assert "the test reason" in msg

    def test_emergency_stop_active_inherits_runtime_error(self):
        # `except Exception` catches it (and except RuntimeError too)
        assert issubclass(es.EmergencyStopActive, RuntimeError)


# ─── strategy_runner integration ───────────────────────────────────────


class TestStrategyRunnerIntegration:
    """The runner should short-circuit run_cycle when the flag is up."""

    def test_run_cycle_short_circuits_when_active(self, _flag_in_tmp,
                                                    monkeypatch):
        from pm.arms import strategy_runner as sr
        # Isolate profile-aware paths to tmp
        monkeypatch.setenv("POLYMARKET_PORTFOLIO_FILE",
                           str(_flag_in_tmp.parent / "portfolio.json"))
        monkeypatch.setenv("POLYMARKET_FORECASTS_FILE",
                           str(_flag_in_tmp.parent / "forecasts.json"))
        monkeypatch.setenv("POLYMARKET_ALERTS_FILE",
                           str(_flag_in_tmp.parent / "alerts.json"))
        monkeypatch.setenv("POLYMARKET_POSITION_META_FILE",
                           str(_flag_in_tmp.parent / "position_meta.json"))

        es.activate("integration test")

        # Provider mocks — we expect ZERO calls because the cycle
        # short-circuits before any provider lookup.
        fake_provider = MagicMock()
        with patch.object(sr.provider_mod, "get_provider",
                          return_value=fake_provider), \
             patch.object(sr.provider_mod, "list_providers",
                          return_value=[]):
            stats = sr.run_cycle(
                strategy_key="market_consensus",
                bankroll=1000.0, kelly_multiplier=0.5,
                half_spread=0.0, max_position_pct=0.10,
                min_gap=0.05, max_markets=10,
                profile_name="test_profile",
            )

        # Cycle returned empty stats — zero markets scanned, zero opens
        assert stats.markets_scanned == 0
        assert stats.positions_opened == 0
        assert stats.resolved_closed == 0
        # No provider methods called
        fake_provider.get_markets.assert_not_called()
        fake_provider.get_closed_markets.assert_not_called()


# ─── kalshi_orders integration ─────────────────────────────────────────


class TestKalshiOrdersIntegration:
    """place_limit_order should refuse when the flag is up, BEFORE
    consulting is_trading_enabled (so the error message is the more
    actionable 'emergency stop' one)."""

    def test_place_limit_order_refuses_when_active(self, _flag_in_tmp,
                                                     monkeypatch):
        from pm.venue import kalshi_orders as ko
        # Force live trading off so we know any error MUST be the
        # emergency-stop one (not the live-trading-disabled one).
        monkeypatch.setenv("KALSHI_LIVE_ENABLED", "true")
        # But emergency stop is active → must refuse FIRST
        es.activate("market is crashing")

        with pytest.raises(ko.OrderRejected) as exc:
            ko.place_limit_order(
                ticker="KXTEST-X", side="yes", action="buy",
                count=1, limit_price_cents=50,
            )
        msg = str(exc.value)
        assert "EMERGENCY STOP" in msg
        assert "market is crashing" in msg
        # Must NOT have fallen through to the live-trading check
        assert "KALSHI_LIVE_ENABLED" not in msg


# ─── Streamlit banner (smoke; no real Streamlit render) ────────────────


class TestEmergencyStopBanner:
    """Banner helper is a no-op when inactive, calls st.error when
    active. Can't render Streamlit in a unit test, but we can patch
    `st` and verify the call shape."""

    def test_banner_noop_when_inactive(self, _flag_in_tmp, monkeypatch):
        from pm.ui import shared
        fake_st = MagicMock()
        monkeypatch.setattr(shared, "st", fake_st)
        shared.emergency_stop_banner()
        fake_st.error.assert_not_called()
        fake_st.info.assert_not_called()
        fake_st.warning.assert_not_called()

    def test_banner_renders_when_active(self, _flag_in_tmp, monkeypatch):
        from pm.ui import shared
        es.activate("test reason")
        fake_st = MagicMock()
        monkeypatch.setattr(shared, "st", fake_st)
        shared.emergency_stop_banner()
        fake_st.error.assert_called_once()
        # The rendered message contains the reason
        msg = fake_st.error.call_args[0][0]
        assert "EMERGENCY STOP ACTIVE" in msg
        assert "test reason" in msg


# ─── CLI smoke tests ───────────────────────────────────────────────────


class TestCLI:
    def test_status_inactive(self, _flag_in_tmp, capsys):
        rc = es.main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NOT ACTIVE" in out

    def test_status_active(self, _flag_in_tmp, capsys):
        es.activate("manual")
        rc = es.main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "EMERGENCY STOP ACTIVE" in out
        assert "manual" in out

    def test_activate_and_deactivate(self, _flag_in_tmp, capsys):
        rc = es.main(["activate", "--reason", "via cli"])
        assert rc == 0 and es.is_active()
        rc = es.main(["deactivate"])
        assert rc == 0 and not es.is_active()
