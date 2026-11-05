"""Tests for the execution adapter (paper vs live broker + gating).

LiveBroker is exercised entirely with monkeypatched kalshi_orders /
kalshi_client functions — never touches the network or real credentials.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pm.trading import execution
from pm.trading.execution import LiveBroker, LiveExecConfig, OrderResult, PaperBroker

# ─── PaperBroker: must be byte-for-byte today's behavior ──────────────────


class TestPaperBroker:
    def test_buy_writes_portfolio(self, tmp_path, monkeypatch):
        pfile = tmp_path / "portfolio.json"
        monkeypatch.setenv("POLYMARKET_PORTFOLIO_FILE", str(pfile))
        from pm.ledger import portfolio as pf
        b = PaperBroker()
        res = b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
                    count=10.0, price=0.40)
        assert res.ok and res.status == "paper"
        assert res.filled_count == 10.0 and res.avg_fill_price == 0.40
        pos = pf.get_positions()
        assert len(pos) == 1
        assert str(pos.iloc[0]["condition_id"]) == "KXMLBGAME-X-NYM"

    def test_sell_writes_portfolio_and_reports_fill(self, tmp_path, monkeypatch):
        # Covered only via the retired longshot scalper's tests before
        # 2026-08-17; a paper sell must reduce the position and report a
        # full fill at the requested price, exactly like the buy path.
        pfile = tmp_path / "portfolio.json"
        monkeypatch.setenv("POLYMARKET_PORTFOLIO_FILE", str(pfile))
        from pm.ledger import portfolio as pf
        b = PaperBroker()
        b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
              count=10.0, price=0.40)
        res = b.sell(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
                     count=4.0, price=0.55)
        assert res.ok and res.status == "paper" and res.action == "sell"
        assert res.filled_count == 4.0 and res.avg_fill_price == 0.55
        pos = pf.get_positions()
        assert len(pos) == 1 and float(pos.iloc[0]["shares"]) == pytest.approx(6.0)
        assert not b.get_positions().empty

    def test_available_cash_subtracts_deployed(self, tmp_path, monkeypatch):
        pfile = tmp_path / "portfolio.json"
        monkeypatch.setenv("POLYMARKET_PORTFOLIO_FILE", str(pfile))
        b = PaperBroker()
        b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
              count=100.0, price=0.50)  # $50 deployed
        assert b.available_cash(1000.0) == pytest.approx(950.0)

    def test_not_live_flags(self):
        b = PaperBroker()
        assert b.is_live is False
        assert b.closes_settle_on_venue is False


# ─── Factory gating: three independent switches ───────────────────────────


class TestGating:
    def test_default_is_paper(self, monkeypatch):
        monkeypatch.delenv(execution.LIVE_PROFILES_ENV, raising=False)
        assert isinstance(execution.get_broker("agentic_favorite_hold"),
                          PaperBroker)

    def test_live_requires_allowlist(self, monkeypatch):
        # Even with trading enabled + no e-stop, an un-listed profile is paper.
        monkeypatch.setattr("pm.venue.kalshi_orders.is_trading_enabled", lambda: True)
        monkeypatch.setattr("pm.core.emergency_stop.is_active", lambda: False)
        monkeypatch.setenv(execution.LIVE_PROFILES_ENV, "some_other_profile")
        assert isinstance(execution.get_broker("agentic_favorite_hold"),
                          PaperBroker)

    def test_live_requires_trading_enabled(self, monkeypatch):
        monkeypatch.setattr("pm.venue.kalshi_orders.is_trading_enabled", lambda: False)
        monkeypatch.setattr("pm.core.emergency_stop.is_active", lambda: False)
        monkeypatch.setenv(execution.LIVE_PROFILES_ENV, "agentic_favorite_hold")
        assert isinstance(execution.get_broker("agentic_favorite_hold"),
                          PaperBroker)

    def test_emergency_stop_forces_paper(self, monkeypatch):
        monkeypatch.setattr("pm.venue.kalshi_orders.is_trading_enabled", lambda: True)
        monkeypatch.setattr("pm.core.emergency_stop.is_active", lambda: True)
        monkeypatch.setenv(execution.LIVE_PROFILES_ENV, "agentic_favorite_hold")
        assert isinstance(execution.get_broker("agentic_favorite_hold"),
                          PaperBroker)

    def test_all_gates_pass_returns_live(self, monkeypatch):
        monkeypatch.setattr("pm.venue.kalshi_orders.is_trading_enabled", lambda: True)
        monkeypatch.setattr("pm.core.emergency_stop.is_active", lambda: False)
        monkeypatch.setenv(execution.LIVE_PROFILES_ENV,
                           "agentic_favorite_hold,other")
        assert isinstance(execution.get_broker("agentic_favorite_hold"),
                          LiveBroker)

    def test_live_max_stake_from_env(self, monkeypatch):
        monkeypatch.setattr("pm.venue.kalshi_orders.is_trading_enabled", lambda: True)
        monkeypatch.setattr("pm.core.emergency_stop.is_active", lambda: False)
        monkeypatch.setenv(execution.LIVE_PROFILES_ENV, "agentic_favorite_hold")
        monkeypatch.setenv("LIVE_MAX_STAKE_DOLLARS", "2")
        b = execution.get_broker("agentic_favorite_hold")
        assert isinstance(b, LiveBroker)
        assert b.cfg.max_stake_dollars == 2.0

    def test_live_max_stake_bad_env_falls_back(self, monkeypatch):
        monkeypatch.setattr("pm.venue.kalshi_orders.is_trading_enabled", lambda: True)
        monkeypatch.setattr("pm.core.emergency_stop.is_active", lambda: False)
        monkeypatch.setenv(execution.LIVE_PROFILES_ENV, "agentic_favorite_hold")
        monkeypatch.setenv("LIVE_MAX_STAKE_DOLLARS", "not-a-number")
        b = execution.get_broker("agentic_favorite_hold")
        assert b.cfg.max_stake_dollars == LiveExecConfig().max_stake_dollars

    def test_live_max_orders_per_day_from_env(self, monkeypatch):
        monkeypatch.setattr("pm.venue.kalshi_orders.is_trading_enabled", lambda: True)
        monkeypatch.setattr("pm.core.emergency_stop.is_active", lambda: False)
        monkeypatch.setenv(execution.LIVE_PROFILES_ENV, "agentic_reactive")
        monkeypatch.setenv("LIVE_MAX_ORDERS_PER_DAY", "100")
        b = execution.get_broker("agentic_reactive")
        assert b.cfg.max_orders_per_day == 100  # high-freq arm not capped at 10

    def test_max_orders_default_zero_uses_preflight_default(self):
        assert LiveExecConfig().max_orders_per_day == 0  # 0 = pre_flight default


# ─── LiveBroker order placement (mocked venue) ────────────────────────────


def _mock_ko(monkeypatch, *, resting_empty=True, place_resp=None,
             preflight_issues=None, captured=None):
    """Install a fake kalshi_orders module surface."""
    from pm.venue import kalshi_orders as ko
    monkeypatch.setattr(ko, "get_resting_orders",
                        lambda *a, **k: pd.DataFrame() if resting_empty
                        else pd.DataFrame([{"order_id": "OLD"}]))
    monkeypatch.setattr(ko, "_today_submit_count", lambda: 0)
    monkeypatch.setattr(ko, "pre_flight_check",
                        lambda **k: (preflight_issues or []))

    def _place(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return place_resp or {"order": {"order_id": "OID123"}}
    monkeypatch.setattr(ko, "place_limit_order", _place)
    cancels = []
    monkeypatch.setattr(ko, "cancel_order", lambda oid: cancels.append(oid))
    return cancels


def _mock_fills(monkeypatch, fills_df):
    from pm.venue import kalshi_client as kc
    monkeypatch.setattr(kc, "get_fills", lambda **k: fills_df)


class TestLiveBuy:
    def test_blocked_by_preflight_places_nothing(self, monkeypatch):
        captured = []
        _mock_ko(monkeypatch, captured=captured,
                 preflight_issues=[("block", "stake too big")])
        b = LiveBroker()
        res = b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
                    count=10, price=0.40, current_mid=0.40)
        assert res.status == "rejected" and not res.ok
        assert captured == []  # never placed

    def test_full_fill(self, monkeypatch):
        _mock_ko(monkeypatch)
        fills = pd.DataFrame([{"order_id": "OID123", "count": 10,
                               "yes_price_cents": 41}])
        _mock_fills(monkeypatch, fills)
        b = LiveBroker(LiveExecConfig(fill_timeout_s=0.1, poll_interval_s=0.01))
        res = b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
                    count=10, price=0.40, current_mid=0.40)
        assert res.ok and res.status == "filled"
        assert res.filled_count == 10
        assert res.avg_fill_price == pytest.approx(0.41)

    def test_partial_fill_cancels_remainder(self, monkeypatch):
        cancels = _mock_ko(monkeypatch)
        fills = pd.DataFrame([{"order_id": "OID123", "count": 4,
                               "yes_price_cents": 40}])
        _mock_fills(monkeypatch, fills)
        b = LiveBroker(LiveExecConfig(fill_timeout_s=0.05, poll_interval_s=0.01))
        res = b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
                    count=10, price=0.40, current_mid=0.40)
        assert res.status == "partial" and res.filled_count == 4
        assert cancels == ["OID123"]  # remainder cancelled

    def test_zero_fill_is_resting_then_cancelled(self, monkeypatch):
        cancels = _mock_ko(monkeypatch)
        _mock_fills(monkeypatch, pd.DataFrame())
        b = LiveBroker(LiveExecConfig(fill_timeout_s=0.05, poll_interval_s=0.01))
        res = b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
                    count=10, price=0.40, current_mid=0.40)
        assert res.status == "resting" and res.filled_count == 0
        assert cancels == ["OID123"]

    def test_existing_resting_order_skips(self, monkeypatch):
        captured = []
        _mock_ko(monkeypatch, resting_empty=False, captured=captured)
        b = LiveBroker()
        res = b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
                    count=10, price=0.40, current_mid=0.40)
        assert res.status == "resting"
        assert captured == []  # idempotent — did not place a 2nd order

    def test_fractional_count_floored(self, monkeypatch):
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        _mock_fills(monkeypatch, pd.DataFrame([{"order_id": "OID123",
                                                "count": 3, "yes_price_cents": 40}]))
        b = LiveBroker(LiveExecConfig(fill_timeout_s=0.05, poll_interval_s=0.01))
        b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
              count=3.9, price=0.40, current_mid=0.40)
        assert captured[0]["count"] == 3  # floored to whole contracts

    def test_buy_resized_to_max_stake(self, monkeypatch):
        # The daemon asks for ~83 contracts ($50 at 60c); a $2 cap must
        # RESIZE the buy to fit (floor(200/60)=3), not reject it — otherwise
        # the min-size canary places nothing.
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        _mock_fills(monkeypatch, pd.DataFrame([{"order_id": "OID123",
                                                "count": 3, "yes_price_cents": 60}]))
        b = LiveBroker(LiveExecConfig(max_stake_dollars=2.0,
                                      fill_timeout_s=0.05, poll_interval_s=0.01))
        b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
              count=83, price=0.60, current_mid=0.60)
        assert captured[0]["count"] == 3       # 3 * 60c = $1.80 <= $2

    def test_buy_rejected_when_cap_below_one_contract(self, monkeypatch):
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        b = LiveBroker(LiveExecConfig(max_stake_dollars=0.30))
        res = b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
                    count=10, price=0.60, current_mid=0.60)
        assert res.status == "rejected"
        assert captured == []                   # nothing placed

    def test_sell_not_resized_by_cap(self, monkeypatch):
        # A sell is an EXIT — must close the full position, never clamped to
        # the stake cap (which would leave a partial position open).
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        _mock_fills(monkeypatch, pd.DataFrame([{"order_id": "OID123",
                                                "count": 83, "yes_price_cents": 55}]))
        b = LiveBroker(LiveExecConfig(max_stake_dollars=2.0,
                                      fill_timeout_s=0.05, poll_interval_s=0.01))
        b.sell(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
               count=83, price=0.55, current_mid=0.55, marketable=True)
        assert captured[0]["count"] == 83       # full position, not clamped

    def test_sub_one_contract_rejected(self, monkeypatch):
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        b = LiveBroker()
        res = b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
                    count=0.5, price=0.40, current_mid=0.40)
        assert res.status == "rejected"
        assert captured == []

    def test_marketable_cross_added(self, monkeypatch):
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        _mock_fills(monkeypatch, pd.DataFrame([{"order_id": "OID123",
                                                "count": 10, "yes_price_cents": 42}]))
        b = LiveBroker(LiveExecConfig(marketable_cross_cents=1,
                                      fill_timeout_s=0.05, poll_interval_s=0.01))
        b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
              count=10, price=0.40, current_mid=0.40)
        assert captured[0]["limit_price_cents"] == 41  # 40 + 1 cross

    def test_default_entry_crosses_spread(self, monkeypatch):
        # Regression: the default must CROSS the spread (>=1), not rest at the
        # stale ask. At 0 the live reaction arm rested ~33% of buys unfilled.
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        _mock_fills(monkeypatch, pd.DataFrame([{"order_id": "OID123",
                                                "count": 10, "yes_price_cents": 41}]))
        b = LiveBroker(LiveExecConfig(fill_timeout_s=0.05, poll_interval_s=0.01))
        assert b.cfg.marketable_cross_cents == 1
        b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
              count=10, price=0.40, current_mid=0.40)
        assert captured[0]["limit_price_cents"] == 41  # 40 + 1 default cross

    def test_env_overrides_marketable_cross(self, monkeypatch):
        from pm.trading import execution as ex
        monkeypatch.setenv("LIVE_MARKETABLE_CROSS_CENTS", "2")
        assert ex._live_cfg_from_env().marketable_cross_cents == 2
        monkeypatch.setenv("LIVE_MARKETABLE_CROSS_CENTS", "garbage")
        assert ex._live_cfg_from_env().marketable_cross_cents == 1  # bad -> default

    def test_price_clamped_to_band(self, monkeypatch):
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        _mock_fills(monkeypatch, pd.DataFrame([{"order_id": "OID123",
                                                "count": 1, "yes_price_cents": 99}]))
        b = LiveBroker(LiveExecConfig(fill_timeout_s=0.05, poll_interval_s=0.01))
        b.buy(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
              count=1, price=0.999, current_mid=0.99)
        assert captured[0]["limit_price_cents"] == 99  # clamped, not 100

    def test_marketable_exit_crosses_down_to_fill(self, monkeypatch):
        # A managed exit (marketable sell) must cross BELOW the quoted bid by
        # exit_cross_cents so it fills even if the book moved — this is how the
        # live stop actually exits (Kalshi has no stop order).
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        _mock_fills(monkeypatch, pd.DataFrame([{"order_id": "OID123",
                                                "count": 10, "yes_price_cents": 35}]))
        b = LiveBroker(LiveExecConfig(exit_cross_cents=5,
                                      fill_timeout_s=0.05, poll_interval_s=0.01))
        b.sell(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
               count=10, price=0.40, current_mid=0.40, marketable=True)
        assert captured[0]["action"] == "sell"
        assert captured[0]["limit_price_cents"] == 35  # 40 - 5 cross

    def test_non_marketable_sell_rests_at_price(self, monkeypatch):
        # A non-marketable sell (e.g. a resting take-profit) sits at its price.
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        _mock_fills(monkeypatch, pd.DataFrame([{"order_id": "OID123",
                                                "count": 10, "yes_price_cents": 50}]))
        b = LiveBroker(LiveExecConfig(exit_cross_cents=5,
                                      fill_timeout_s=0.05, poll_interval_s=0.01))
        b.sell(question="Q", ticker="KXMLBGAME-X-NYM", outcome="Yes",
               count=10, price=0.50, current_mid=0.50, marketable=False)
        assert captured[0]["limit_price_cents"] == 50  # no exit cross


# ─── Resting cash-out limit (take-profit) + cancel + fill check ───────────


class TestRestingTakeProfit:
    def test_places_resting_sell(self, monkeypatch):
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        b = LiveBroker()
        res = b.place_resting_take_profit(ticker="KXMLBGAME-X-NYM",
                                          outcome="Yes", count=3, tp_price=0.75)
        assert res.status == "resting" and res.order_id == "OID123"
        assert captured[0]["action"] == "sell"
        assert captured[0]["count"] == 3
        assert captured[0]["limit_price_cents"] == 75   # rests above market

    def test_rejects_sub_one_contract(self, monkeypatch):
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        b = LiveBroker()
        res = b.place_resting_take_profit(ticker="T", outcome="Yes",
                                          count=0.4, tp_price=0.75)
        assert res.status == "rejected" and captured == []

    def test_tp_price_clamped(self, monkeypatch):
        captured = []
        _mock_ko(monkeypatch, captured=captured)
        b = LiveBroker()
        b.place_resting_take_profit(ticker="T", outcome="Yes",
                                    count=2, tp_price=1.20)
        assert captured[0]["limit_price_cents"] == 99   # clamped to band

    def test_cancel_calls_kalshi(self, monkeypatch):
        cancels = _mock_ko(monkeypatch)
        b = LiveBroker()
        assert b.cancel("OID9") is True
        assert cancels == ["OID9"]
        assert b.cancel("") is False                     # nothing to cancel

    def test_fills_for_order(self, monkeypatch):
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(kc, "get_fills", lambda **k: pd.DataFrame(
            [{"order_id": "OID9", "count": 3, "yes_price_cents": 75}]))
        b = LiveBroker()
        cnt, avg = b.fills_for_order(ticker="T", order_id="OID9")
        assert cnt == 3 and avg == pytest.approx(0.75)

    def test_fills_for_order_none_when_no_match(self, monkeypatch):
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(kc, "get_fills", lambda **k: pd.DataFrame())
        b = LiveBroker()
        assert b.fills_for_order(ticker="T", order_id="OID9") == (0.0, None)

    def test_paper_broker_noops(self):
        b = PaperBroker()
        assert b.rests_take_profit is False
        r = b.place_resting_take_profit(ticker="T", outcome="Yes",
                                        count=3, tp_price=0.75)
        assert r.order_id is None and r.status == "paper"
        assert b.cancel("anything") is True
        assert b.fills_for_order(ticker="T", order_id="x") == (0.0, None)


# ─── LiveBroker state truth from the venue (mocked) ───────────────────────


class TestLiveState:
    def test_get_positions_normalized(self, monkeypatch):
        from pm.venue import kalshi_client as kc
        raw = pd.DataFrame([
            {"ticker": "KXMLBGAME-X-NYM", "position": 100,
             "market_exposure_cents": 5500, "last_updated_ts": "t"},
            {"ticker": "KXMLBGAME-Y-BOS", "position": -50,
             "market_exposure_cents": 2000, "last_updated_ts": "t"},
            {"ticker": "KXFLAT", "position": 0,
             "market_exposure_cents": 0, "last_updated_ts": "t"},
        ])
        monkeypatch.setattr(kc, "get_authed_positions", lambda **k: raw)
        # scope off → pure normalization of the raw venue rows
        b = LiveBroker(LiveExecConfig(scope_to_own_positions=False))
        pos = b.get_positions()
        assert set(pos["condition_id"]) == {"KXMLBGAME-X-NYM", "KXMLBGAME-Y-BOS"}
        nym = pos[pos["condition_id"] == "KXMLBGAME-X-NYM"].iloc[0]
        assert nym["outcome"] == "Yes" and nym["shares"] == 100
        assert nym["avg_price"] == pytest.approx(0.55)
        bos = pos[pos["condition_id"] == "KXMLBGAME-Y-BOS"].iloc[0]
        assert bos["outcome"] == "No" and bos["shares"] == 50  # abs of -50

    def test_available_cash_is_min_of_bankroll_and_balance(self, monkeypatch):
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(kc, "get_balance",
                            lambda: {"balance_cents": 7300, "payout_cents": 0})
        monkeypatch.setattr(kc, "get_authed_positions",
                            lambda **k: pd.DataFrame())  # nothing deployed
        b = LiveBroker()
        assert b.available_cash(1000.0) == pytest.approx(73.0)  # balance binds
        assert b.available_cash(50.0) == pytest.approx(50.0)    # bankroll binds

    def test_available_cash_subtracts_own_deployed(self, monkeypatch):
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(kc, "get_balance",
                            lambda: {"balance_cents": 100000, "payout_cents": 0})
        # one of this arm's positions ($30 exposure); scope it in via meta
        monkeypatch.setattr(kc, "get_authed_positions", lambda **k: pd.DataFrame([
            {"ticker": "MINE", "position": 50, "market_exposure_cents": 3000,
             "last_updated_ts": "t"}]))
        monkeypatch.setattr("pm.ledger.position_meta.all_entries",
                            lambda: {"MINE|Yes": {"entry_price": 0.6}})
        b = LiveBroker()  # bankroll 100, $30 deployed → $70 left (balance huge)
        assert b.available_cash(100.0) == pytest.approx(70.0)


class TestPositionScoping:
    def _raw(self):
        return pd.DataFrame([
            {"ticker": "MINE-A", "position": 10, "market_exposure_cents": 600,
             "last_updated_ts": "t"},
            {"ticker": "THEIRS-B", "position": 20, "market_exposure_cents": 1200,
             "last_updated_ts": "t"},
            {"ticker": "MANUAL-C", "position": 5, "market_exposure_cents": 300,
             "last_updated_ts": "t"},
        ])

    def test_scopes_to_own_meta_tickers(self, monkeypatch):
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(kc, "get_authed_positions", lambda **k: self._raw())
        # this arm only opened MINE-A (and a since-closed key with no venue row)
        monkeypatch.setattr("pm.ledger.position_meta.all_entries",
                            lambda: {"MINE-A|Yes": {}, "GONE-Z|Yes": {}})
        b = LiveBroker()  # scope on by default
        pos = b.get_positions()
        assert list(pos["condition_id"]) == ["MINE-A"]   # not THEIRS-B / MANUAL-C

    def test_scope_off_returns_whole_account(self, monkeypatch):
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(kc, "get_authed_positions", lambda **k: self._raw())
        monkeypatch.setattr("pm.ledger.position_meta.all_entries", lambda: {"MINE-A|Yes": {}})
        b = LiveBroker(LiveExecConfig(scope_to_own_positions=False))
        assert set(b.get_positions()["condition_id"]) == {
            "MINE-A", "THEIRS-B", "MANUAL-C"}

    def test_empty_meta_scopes_to_nothing(self, monkeypatch):
        from pm.venue import kalshi_client as kc
        monkeypatch.setattr(kc, "get_authed_positions", lambda **k: self._raw())
        monkeypatch.setattr("pm.ledger.position_meta.all_entries", lambda: {})
        b = LiveBroker()
        assert b.get_positions().empty   # arm tracks nothing → manages nothing

    def test_balance_fetch_failure_deploys_nothing(self, monkeypatch):
        from pm.venue import kalshi_client as kc

        def _boom():
            raise RuntimeError("network")
        monkeypatch.setattr(kc, "get_balance", _boom)
        b = LiveBroker()
        assert b.available_cash(1000.0) == 0.0  # fail-safe: no cash confirmed

    def test_positions_fetch_failure_returns_empty(self, monkeypatch):
        from pm.venue import kalshi_client as kc

        def _boom(**k):
            raise RuntimeError("network")
        monkeypatch.setattr(kc, "get_authed_positions", _boom)
        b = LiveBroker()
        assert b.get_positions().empty


# ─── OrderResult basics ───────────────────────────────────────────────────


def test_order_result_defaults():
    r = OrderResult(ok=True, status="paper", ticker="T", side="yes",
                    action="buy", requested_count=5)
    assert r.filled_count == 0.0 and r.issues == []
