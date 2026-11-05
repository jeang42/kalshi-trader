"""Tests for the drawdown circuit breaker on the live execution path.

Until 2026-08-08 ``LiveBroker._place`` passed ``in_drawdown_pause=False`` with
a TODO, so the one guardrail designed to halt a *losing* automated arm guarded
only the manual UI. These cover the wiring and, specifically, the fail-closed
behaviour — the property that makes the breaker trustworthy rather than
decorative.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from pm.trading import execution

# Bound at import time, so it survives the autouse fixture that rebinds
# execution.account_equity_dollars for every other test.
from pm.trading.execution import account_equity_dollars as real_account_equity


class TestEvaluateDrawdown:
    def test_first_observation_sets_the_peak(self):
        assert execution.evaluate_drawdown(100.0, None, 0.15) == (100.0, False)

    def test_new_high_ratchets_the_peak(self):
        assert execution.evaluate_drawdown(120.0, 100.0, 0.15) == (120.0, False)

    def test_peak_never_ratchets_down(self):
        peak, _ = execution.evaluate_drawdown(50.0, 100.0, 0.15)
        assert peak == 100.0

    def test_shallow_drawdown_does_not_pause(self):
        assert execution.evaluate_drawdown(90.0, 100.0, 0.15) == (100.0, False)

    def test_threshold_is_inclusive(self):
        _, paused = execution.evaluate_drawdown(85.0, 100.0, 0.15)
        assert paused is True

    def test_deep_drawdown_pauses(self):
        _, paused = execution.evaluate_drawdown(10.0, 100.0, 0.15)
        assert paused is True

    def test_zero_or_negative_peak_cannot_pause(self):
        assert execution.evaluate_drawdown(10.0, 0.0, 0.15) == (10.0, False)

    def test_negative_threshold_is_treated_as_magnitude(self):
        _, paused = execution.evaluate_drawdown(80.0, 100.0, -0.15)
        assert paused is True


class TestAccountEquity:
    def test_sums_cash_payout_and_market_exposure(self):
        import pandas as pd
        with patch("pm.venue.kalshi_client.get_balance",
                   return_value={"balance_cents": 5000, "payout_cents": 1000}), \
             patch("pm.venue.kalshi_client.get_authed_positions",
                   return_value=pd.DataFrame([{"market_exposure_cents": 2500}])):
            assert real_account_equity() == pytest.approx(85.0)

    def test_returns_none_when_balance_is_unavailable(self):
        """None, not a guess: a breaker that cannot see the account must not
        report 'all fine'."""
        with patch("pm.venue.kalshi_client.get_balance", side_effect=RuntimeError("down")):
            assert real_account_equity() is None

    def test_returns_none_when_positions_are_unavailable(self):
        with patch("pm.venue.kalshi_client.get_balance",
                   return_value={"balance_cents": 100, "payout_cents": 0}), \
             patch("pm.venue.kalshi_client.get_authed_positions",
                   side_effect=RuntimeError("down")):
            assert real_account_equity() is None

    def test_empty_position_frame_counts_as_zero_exposure(self):
        import pandas as pd
        with patch("pm.venue.kalshi_client.get_balance",
                   return_value={"balance_cents": 2500, "payout_cents": 0}), \
             patch("pm.venue.kalshi_client.get_authed_positions",
                   return_value=pd.DataFrame()):
            assert real_account_equity() == pytest.approx(25.0)


class TestInDrawdownPause:
    @pytest.fixture
    def broker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LIVE_EQUITY_STATE_FILE", str(tmp_path / "equity.json"))
        return execution.LiveBroker(execution.LiveExecConfig(drawdown_pause_pct=0.15))

    def test_disabled_by_config_never_pauses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LIVE_EQUITY_STATE_FILE", str(tmp_path / "e.json"))
        b = execution.LiveBroker(execution.LiveExecConfig(enforce_drawdown=False))
        with patch.object(execution, "account_equity_dollars", return_value=1.0):
            assert b._in_drawdown_pause() is False

    def test_first_call_records_the_peak_and_allows_trading(self, broker, tmp_path):
        with patch.object(execution, "account_equity_dollars", return_value=100.0):
            assert broker._in_drawdown_pause() is False
        from pm.core import atomic_io
        state = atomic_io.read_json(tmp_path / "equity.json", {})
        assert state["peak_equity_dollars"] == 100.0

    def test_pauses_once_equity_falls_past_the_threshold(self, broker):
        with patch.object(execution, "account_equity_dollars", return_value=100.0):
            broker._in_drawdown_pause()
        with patch.object(execution, "account_equity_dollars", return_value=80.0):
            assert broker._in_drawdown_pause() is True

    def test_peak_persists_across_broker_instances(self, tmp_path, monkeypatch):
        """A restart must not reset the breaker — otherwise the crash-loop the
        ledger fix prevents would also have been a way to defeat it."""
        monkeypatch.setenv("LIVE_EQUITY_STATE_FILE", str(tmp_path / "e.json"))
        cfg = execution.LiveExecConfig(drawdown_pause_pct=0.15)
        with patch.object(execution, "account_equity_dollars", return_value=200.0):
            execution.LiveBroker(cfg)._in_drawdown_pause()
        with patch.object(execution, "account_equity_dollars", return_value=150.0):
            assert execution.LiveBroker(cfg)._in_drawdown_pause() is True

    def test_fails_closed_when_equity_is_unknown(self, broker):
        """Not knowing is not the same as being fine. Mirrors
        emergency_stop.is_active() reporting ACTIVE on OSError."""
        with patch.object(execution, "account_equity_dollars", return_value=None):
            assert broker._in_drawdown_pause() is True

    def test_fails_closed_when_the_state_file_is_unusable(self, tmp_path, monkeypatch):
        # Point the state path at a directory: every read/write raises.
        bad = tmp_path / "state_dir"
        bad.mkdir()
        monkeypatch.setenv("LIVE_EQUITY_STATE_FILE", str(bad))
        b = execution.LiveBroker(execution.LiveExecConfig(drawdown_pause_pct=0.15))
        with patch.object(execution, "account_equity_dollars", return_value=100.0):
            assert b._in_drawdown_pause() is True

    def test_recovery_above_the_threshold_resumes_trading(self, broker):
        with patch.object(execution, "account_equity_dollars", return_value=100.0):
            broker._in_drawdown_pause()
        with patch.object(execution, "account_equity_dollars", return_value=80.0):
            assert broker._in_drawdown_pause() is True
        with patch.object(execution, "account_equity_dollars", return_value=95.0):
            assert broker._in_drawdown_pause() is False


class TestPreFlightWiring:
    def test_the_breaker_is_actually_fed_to_pre_flight(self, tmp_path, monkeypatch):
        """The regression that matters: the value must reach pre_flight_check
        rather than the literal False it carried for eight weeks."""
        monkeypatch.setenv("LIVE_EQUITY_STATE_FILE", str(tmp_path / "e.json"))
        broker = execution.LiveBroker(execution.LiveExecConfig())
        captured = {}

        def fake_pre_flight(**kwargs):
            captured.update(kwargs)
            return [("block", "stop here")]

        from pm.venue import kalshi_orders as ko
        with patch.object(ko, "pre_flight_check", side_effect=fake_pre_flight), \
             patch.object(ko, "get_resting_orders", return_value=None), \
             patch.object(ko, "_today_submit_count", return_value=0), \
             patch.object(broker, "available_cash", return_value=100.0), \
             patch.object(broker, "_in_drawdown_pause", return_value=True):
            res = broker.buy(question="q", ticker="TKR", outcome="yes",
                             count=1, price=0.5)

        assert captured.get("in_drawdown_pause") is True
        assert res.status == "rejected"


class TestOrphanReconciliation:
    """An exception from the submission does NOT mean the order did not land.

    A read timeout after the venue accepted looks identical, at the call site,
    to an outright rejection. Treating both as "nothing happened" leaves a real
    position nobody manages — absent from position_meta, never exited.
    """

    @pytest.fixture
    def broker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LIVE_EQUITY_STATE_FILE", str(tmp_path / "e.json"))
        return execution.LiveBroker(execution.LiveExecConfig())

    def _mock_venue(self, monkeypatch, *, resting=None, fills=None, raise_on_place=True):
        from pm.venue import kalshi_client as kc
        from pm.venue import kalshi_orders as ko
        monkeypatch.setattr(ko, "get_resting_orders",
                            lambda *a, **k: resting if resting is not None else pd.DataFrame())
        monkeypatch.setattr(ko, "_today_submit_count", lambda: 0)
        monkeypatch.setattr(ko, "pre_flight_check", lambda **k: [])
        if raise_on_place:
            def boom(**kwargs):
                raise TimeoutError("read timed out after the venue accepted")
            monkeypatch.setattr(ko, "place_limit_order", boom)
        monkeypatch.setattr(kc, "get_fills",
                            lambda **k: fills if fills is not None else pd.DataFrame())

    def test_adopts_a_resting_order_that_carries_our_client_id(self, broker, monkeypatch):
        captured_coid = {}
        from pm.venue import kalshi_orders as ko
        real_make = ko._make_client_order_id

        def spy(ticker):
            coid = real_make(ticker)
            captured_coid["v"] = coid
            return coid
        monkeypatch.setattr(ko, "_make_client_order_id", spy)

        # First call: empty resting (the pre-flight duplicate check), then the
        # order shows up after the failed submission.
        calls = {"n": 0}

        def resting(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return pd.DataFrame()
            return pd.DataFrame([{"order_id": "OID-RECOVERED",
                                  "client_order_id": captured_coid["v"]}])
        monkeypatch.setattr(ko, "get_resting_orders", resting)
        monkeypatch.setattr(ko, "_today_submit_count", lambda: 0)
        monkeypatch.setattr(ko, "pre_flight_check", lambda **k: [])

        def boom(**kwargs):
            raise TimeoutError("read timed out")
        monkeypatch.setattr(ko, "place_limit_order", boom)
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(kc, "get_fills", lambda **k: pd.DataFrame())
        # The adopted order shows no fills, so _place cancels its unfilled
        # remainder. Until 2026-08-17 this was NOT mocked: the test issued a
        # real authenticated DELETE to Kalshi (404) and wrote cancel_submit /
        # cancel_error rows into the LIVE orders.log — 70 of them, invisible
        # to the data/ tripwire because it did not watch orders.log. The root
        # conftest now redirects ORDERS_LOG_PATH and blocks sockets outright.
        cancelled = []
        monkeypatch.setattr(ko, "cancel_order", lambda oid: cancelled.append(oid))

        with patch.object(broker, "available_cash", return_value=100.0), \
             patch.object(broker, "_in_drawdown_pause", return_value=False):
            res = broker.buy(question="Q", ticker="TKR", outcome="yes",
                             count=1, price=0.50)
        assert res.order_id == "OID-RECOVERED"
        assert res.status != "error", "an adopted order must not be reported as a failure"
        assert "adopted" in res.detail
        assert cancelled == ["OID-RECOVERED"], "the unfilled remainder of an adopted order is cancelled"

    def test_reports_error_when_the_order_genuinely_did_not_land(self, broker, monkeypatch):
        self._mock_venue(monkeypatch)
        with patch.object(broker, "available_cash", return_value=100.0), \
             patch.object(broker, "_in_drawdown_pause", return_value=False):
            res = broker.buy(question="Q", ticker="TKR", outcome="yes",
                             count=1, price=0.50)
        assert res.status == "error"
        assert res.order_id is None

    def test_reconcile_never_raises_when_the_venue_is_unreachable(self, broker, monkeypatch):
        """This runs on the error path; a failure here must not replace the
        original error with a less informative one."""
        from pm.venue import kalshi_client as kc
        from pm.venue import kalshi_orders as ko
        monkeypatch.setattr(ko, "get_resting_orders",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        monkeypatch.setattr(kc, "get_fills",
                            lambda **k: (_ for _ in ()).throw(RuntimeError("down")))
        assert broker._reconcile_after_error("TKR", "coid-123") is None

    def test_finds_the_order_in_fills_when_it_already_executed(self, broker, monkeypatch):
        from pm.venue import kalshi_client as kc
        from pm.venue import kalshi_orders as ko
        monkeypatch.setattr(ko, "get_resting_orders", lambda *a, **k: pd.DataFrame())
        monkeypatch.setattr(kc, "get_fills", lambda **k: pd.DataFrame(
            [{"order_id": "OID-FILLED", "client_order_id": "coid-123"}]))
        assert broker._reconcile_after_error("TKR", "coid-123") == "OID-FILLED"

    def test_ignores_orders_belonging_to_someone_else(self, broker, monkeypatch):
        """Several arms and hand-trading share one account; adopting another
        party's order would be worse than reporting the error."""
        from pm.venue import kalshi_client as kc
        from pm.venue import kalshi_orders as ko
        monkeypatch.setattr(ko, "get_resting_orders", lambda *a, **k: pd.DataFrame(
            [{"order_id": "SOMEONE-ELSE", "client_order_id": "different-coid"}]))
        monkeypatch.setattr(kc, "get_fills", lambda **k: pd.DataFrame())
        assert broker._reconcile_after_error("TKR", "coid-123") is None
