"""Regression tests for the venue-boundary order policy (release audit
P0-A / P0-D / P1-B).

These tests deliberately BYPASS the high-level LiveBroker and the UI's
pre_flight_check and call ``place_limit_order`` directly — the point of the
venue-boundary enforcement is that no caller can skip the mandatory caps,
the atomic daily-cap reservation, or the fail-closed audit write.

All venue interaction is mocked at the ``kalshi_auth`` boundary; nothing
here can reach the network (see also the root conftest's socket guard).
"""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import pytest

from pm.venue import kalshi_orders as ko

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def orders_log(tmp_path, monkeypatch):
    p = tmp_path / "orders.log"
    monkeypatch.setenv("ORDERS_LOG_PATH", str(p))
    return p


@pytest.fixture
def trading_enabled(monkeypatch, orders_log):
    monkeypatch.setenv("KALSHI_LIVE_ENABLED", "true")
    monkeypatch.setattr(ko.ka, "is_configured", lambda: True)
    monkeypatch.delenv("LIVE_MAX_STAKE_DOLLARS", raising=False)
    monkeypatch.delenv("LIVE_MAX_ORDERS_PER_DAY", raising=False)
    return orders_log


def _submit_rows(log_path, event="submit"):
    if not log_path.exists():
        return []
    rows = []
    for line in log_path.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("event") == event:
            rows.append(rec)
    return rows


# ---------------------------------------------------------------------------
# P0-A: mandatory caps at the venue boundary (no pre_flight_check run)
# ---------------------------------------------------------------------------


class TestMandatoryStakeCap:
    def test_buy_over_default_cap_is_refused_before_http(self, trading_enabled):
        # $60 > the $50 default; no caller-side pre-flight was run.
        with patch.object(ko.ka, "authed_post") as mock_post:
            with pytest.raises(ko.OrderRejected, match="per-order cap"):
                ko.place_limit_order(ticker="KX-FOO", side="yes", action="buy",
                                     count=120, limit_price_cents=50)
            mock_post.assert_not_called()
        assert _submit_rows(trading_enabled) == []  # no reservation burned

    def test_env_raises_the_cap_explicitly(self, trading_enabled, monkeypatch):
        monkeypatch.setenv("LIVE_MAX_STAKE_DOLLARS", "100")
        with patch.object(ko.ka, "authed_post", return_value={"order": {}}) as mock_post:
            ko.place_limit_order(ticker="KX-FOO", side="yes", action="buy",
                                 count=120, limit_price_cents=50)
            mock_post.assert_called_once()

    def test_sells_are_exempt_from_the_stake_cap(self, trading_enabled):
        # Exiting a large position must not be blocked by its size.
        with patch.object(ko.ka, "authed_post", return_value={"order": {}}) as mock_post:
            ko.place_limit_order(ticker="KX-FOO", side="yes", action="sell",
                                 count=500, limit_price_cents=50)
            mock_post.assert_called_once()

    def test_bad_env_value_falls_back_to_default(self, trading_enabled, monkeypatch):
        monkeypatch.setenv("LIVE_MAX_STAKE_DOLLARS", "lots")
        assert ko.mandatory_caps() == (ko.DEFAULT_MAX_STAKE_DOLLARS,
                                       ko.DEFAULT_MAX_ORDERS_PER_DAY)


class TestMandatoryDailyCap:
    def _fill_log_to_cap(self, log_path, n):
        from datetime import UTC, datetime
        ts = datetime.now(UTC).isoformat()
        with open(log_path, "a") as f:
            for _ in range(n):
                f.write(json.dumps({"ts": ts, "event": "submit"}) + "\n")

    def test_buy_at_cap_is_refused_before_http(self, trading_enabled):
        self._fill_log_to_cap(trading_enabled, ko.DEFAULT_MAX_ORDERS_PER_DAY)
        with patch.object(ko.ka, "authed_post") as mock_post:
            with pytest.raises(ko.OrderRejected, match="daily order cap"):
                ko.place_limit_order(ticker="KX-FOO", side="yes", action="buy",
                                     count=10, limit_price_cents=50)
            mock_post.assert_not_called()

    def test_sell_also_counts_against_the_daily_cap(self, trading_enabled):
        # Matches pre_flight_check's existing semantics: every order placed
        # through place_limit_order counts; the anti-runaway cap is about
        # order volume, not direction.
        self._fill_log_to_cap(trading_enabled, ko.DEFAULT_MAX_ORDERS_PER_DAY)
        with patch.object(ko.ka, "authed_post") as mock_post:
            with pytest.raises(ko.OrderRejected, match="daily order cap"):
                ko.place_limit_order(ticker="KX-FOO", side="yes", action="sell",
                                     count=10, limit_price_cents=50)
            mock_post.assert_not_called()

    def test_take_profit_and_cashout_are_not_blocked_at_the_cap(self, trading_enabled):
        # tp_submit / cashout_submit rows are deliberately not counted and
        # the risk-reducing exits are not blocked by the cap.
        self._fill_log_to_cap(trading_enabled, ko.DEFAULT_MAX_ORDERS_PER_DAY)
        with patch.object(ko.ka, "authed_post", return_value={"order": {}}):
            ko.place_take_profit(ticker="KX-FOO", held_side="yes", count=5,
                                 sell_price_cents=80)
            ko.cash_out_position(ticker="KX-FOO", held_side="yes", count=5,
                                 current_bid_cents=40)

    def test_env_raises_the_daily_cap(self, trading_enabled, monkeypatch):
        self._fill_log_to_cap(trading_enabled, ko.DEFAULT_MAX_ORDERS_PER_DAY)
        monkeypatch.setenv("LIVE_MAX_ORDERS_PER_DAY",
                           str(ko.DEFAULT_MAX_ORDERS_PER_DAY + 5))
        with patch.object(ko.ka, "authed_post", return_value={"order": {}}) as mock_post:
            ko.place_limit_order(ticker="KX-FOO", side="yes", action="buy",
                                 count=10, limit_price_cents=50)
            mock_post.assert_called_once()


# ---------------------------------------------------------------------------
# P0-D: the daily-cap check + submit audit is one atomic reservation
# ---------------------------------------------------------------------------


class TestConcurrentSubmission:
    def test_concurrent_submitters_cannot_exceed_the_daily_cap(
            self, trading_enabled, monkeypatch):
        """10 threads race for a cap of 5. Without the submission lock, all
        10 read a below-cap count and all 10 submit (the pre-fix behavior);
        with the check-and-reserve under the lock, exactly 5 clear."""
        cap = 5
        monkeypatch.setenv("LIVE_MAX_ORDERS_PER_DAY", str(cap))
        start = threading.Barrier(10)
        results: list[str] = []
        results_lock = threading.Lock()

        def submit():
            start.wait()
            try:
                ko.place_limit_order(ticker="KX-RACE", side="yes",
                                     action="buy", count=2,
                                     limit_price_cents=50)
                outcome = "accepted"
            except ko.OrderRejected as e:
                outcome = "rejected" if "daily order cap" in str(e) else f"other: {e}"
            with results_lock:
                results.append(outcome)

        # One patch installed before the threads start — patching per-thread
        # would race the unpatch and corrupt the module for later tests.
        with patch.object(ko.ka, "authed_post",
                          return_value={"order": {"order_id": "o"}}):
            threads = [threading.Thread(target=submit) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        assert sorted(results) == ["accepted"] * cap + ["rejected"] * 5
        # The audit log agrees: exactly `cap` reservations were written.
        assert len(_submit_rows(trading_enabled)) == cap


# ---------------------------------------------------------------------------
# P1-B: audit persistence is fail-closed for submissions
# ---------------------------------------------------------------------------


@pytest.fixture
def unwritable_audit(tmp_path, monkeypatch):
    """ORDERS_LOG_PATH whose parent cannot be created (parent is a file)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("ORDERS_LOG_PATH", str(blocker / "orders.log"))
    monkeypatch.setenv("KALSHI_LIVE_ENABLED", "true")
    monkeypatch.setattr(ko.ka, "is_configured", lambda: True)


class TestAuditFailClosed:
    def test_buy_refused_when_audit_log_unwritable(self, unwritable_audit):
        with patch.object(ko.ka, "authed_post") as mock_post:
            with pytest.raises(ko.OrderRejected):
                ko.place_limit_order(ticker="KX-FOO", side="yes", action="buy",
                                     count=10, limit_price_cents=50)
            mock_post.assert_not_called()

    def test_take_profit_refused_when_audit_log_unwritable(self, unwritable_audit):
        with patch.object(ko.ka, "authed_post") as mock_post:
            with pytest.raises(ko.OrderRejected):
                ko.place_take_profit(ticker="KX-FOO", held_side="yes",
                                     count=5, sell_price_cents=80)
            mock_post.assert_not_called()

    def test_cashout_refused_when_audit_log_unwritable(self, unwritable_audit):
        with patch.object(ko.ka, "authed_post") as mock_post:
            with pytest.raises(ko.OrderRejected):
                ko.cash_out_position(ticker="KX-FOO", held_side="yes",
                                     count=5, current_bid_cents=40)
            mock_post.assert_not_called()

    def test_cancel_still_proceeds_when_audit_log_unwritable(self, unwritable_audit):
        # Refusing to CANCEL because the log is unwritable would be
        # risk-increasing: the resting order would stay on the book.
        with patch.object(ko.ka, "authed_delete", return_value={}) as mock_del:
            ko.cancel_order("some-order-id")
            mock_del.assert_called_once()


