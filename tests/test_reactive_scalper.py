"""Tests for the reactive scalper's pure decision logic."""

from __future__ import annotations

from datetime import UTC, datetime

from pm.arms import reactive_scalper as rs


class TestQualifies:
    C = rs.ReactiveConfig(wpa_threshold=0.10, min_inning=6)

    def test_big_swing_late_qualifies(self):
        assert rs.qualifies(0.35, 9, self.C)
        assert rs.qualifies(-0.30, 8, self.C)   # big negative (fielding side)

    def test_small_swing_rejected(self):
        assert not rs.qualifies(0.04, 9, self.C)

    def test_too_early_rejected(self):
        assert not rs.qualifies(0.35, 3, self.C)

    def test_threshold_is_absolute(self):
        assert rs.qualifies(-0.11, 7, self.C)
        assert not rs.qualifies(-0.09, 7, self.C)


class TestBattingSide:
    def test_top_is_away(self):
        assert rs.batting_side("top") == "away"
        assert rs.batting_side("Top 7th") == "away"

    def test_bottom_is_home(self):
        assert rs.batting_side("bottom") == "home"


class TestBeneficiaryFromWpa:
    def test_positive_helps_batting(self):
        assert rs.beneficiary_from_wpa(0.3, "bottom") == "home"   # home batting
        assert rs.beneficiary_from_wpa(0.3, "top") == "away"      # away batting

    def test_negative_helps_fielding(self):
        assert rs.beneficiary_from_wpa(-0.3, "bottom") == "away"  # home hurt
        assert rs.beneficiary_from_wpa(-0.3, "top") == "home"     # away hurt


class TestEvaluateExit:
    C = rs.ReactiveConfig(take_profit_pct=0.10, hold_seconds=150.0)
    OPENED = "2026-06-05T00:00:00+00:00"

    def _e(self, entry=0.50):
        return {"entry_price": entry, "opened_at": self.OPENED}

    def test_take_profit_on_pop(self):
        # 0.50 → 0.62 is well past +10% net even after fees.
        r = rs.evaluate_exit(entry=self._e(), side_price=0.62,
                             now=datetime(2026, 6, 5, 0, 0, 20, tzinfo=UTC),
                             cfg=self.C, ticker="KXMLBGAME-X-NYM")
        assert r == "take_profit"

    def test_time_stop_when_flat_after_window(self):
        r = rs.evaluate_exit(entry=self._e(), side_price=0.505,
                             now=datetime(2026, 6, 5, 0, 5, 0, tzinfo=UTC),
                             cfg=self.C, ticker="KXMLBGAME-X-NYM")
        assert r == "time_stop"

    def test_hold_when_flat_inside_window(self):
        r = rs.evaluate_exit(entry=self._e(), side_price=0.505,
                             now=datetime(2026, 6, 5, 0, 1, 0, tzinfo=UTC),
                             cfg=self.C, ticker="KXMLBGAME-X-NYM")
        assert r is None

    def test_take_profit_precedes_time_stop(self):
        # big pop AND past the window → take-profit wins
        r = rs.evaluate_exit(entry=self._e(), side_price=0.70,
                             now=datetime(2026, 6, 5, 0, 10, 0, tzinfo=UTC),
                             cfg=self.C, ticker="KXMLBGAME-X-NYM")
        assert r == "take_profit"

    def test_zero_entry_guard(self):
        r = rs.evaluate_exit(entry={"entry_price": 0, "opened_at": self.OPENED},
                             side_price=0.9, now=datetime.now(UTC),
                             cfg=self.C, ticker="X")
        assert r is None


class TestPlayAge:
    def test_recent_play_small_age(self):
        now = datetime(2026, 6, 5, 0, 1, 0, tzinfo=UTC)
        assert rs._play_age_s("2026-06-05T00:00:30.000Z", now) == 30.0

    def test_garbage_ts_is_huge(self):
        assert rs._play_age_s("not-a-date", datetime.now(UTC)) > 1e8


class TestActiveProfilePinning:
    """Regression: run_cycle must pin POLYMARKET_ACTIVE_PROFILE so the live
    owner-lock resolves to this arm's profile (else it silently goes paper)."""

    def test_pins_active_profile_for_broker(self, monkeypatch):
        import os

        import pandas as pd

        from pm.core import profiles
        seen = {}

        def fake_get_broker(profile):
            seen["active"] = os.environ.get(profiles.ACTIVE_PROFILE_ENV)

            class _B:
                is_live = False

                def get_positions(self):
                    return pd.DataFrame()

                def available_cash(self, b):
                    return 0.0

            return _B()

        monkeypatch.setattr(rs.execution, "get_broker", fake_get_broker)
        monkeypatch.setattr(rs.emergency_stop, "is_active", lambda: False)
        monkeypatch.setattr(rs.profiles, "initialize_profiles", lambda *a, **k: None)
        monkeypatch.setattr(rs.profiles, "list_profiles", lambda: ["agentic_reactive"])
        monkeypatch.setattr(rs, "_pin_profile_env", lambda n: {})
        monkeypatch.setattr(rs, "_restore_profile_env", lambda s: None)
        monkeypatch.setattr(rs, "_close_resolved", lambda **k: 0)
        monkeypatch.setattr(rs, "_manage", lambda **k: 0)
        monkeypatch.setattr(rs, "_react", lambda **k: (0, ""))
        rs.run_cycle(rs.ReactiveConfig(profile_name="agentic_reactive"))
        assert seen["active"] == "agentic_reactive"


class TestPriceFloor:
    """A benefiting side priced below min_entry_price is a deep underdog that
    ticked up — skip it (every sub-floor buy in the live canary went to $0)."""

    def _run(self, monkeypatch, ask, floor=0.25):
        import pandas as pd
        cfg = rs.ReactiveConfig(min_entry_price=floor)
        bought = []

        class _B:
            is_live = False
            def get_positions(self):
                return pd.DataFrame()
            def buy(self, **kw):
                bought.append(kw)
                return rs.execution.OrderResult(
                    ok=True, status="paper", ticker=kw["ticker"], side="yes",
                    action="buy", requested_count=kw["count"],
                    filled_count=kw["count"], avg_fill_price=kw["price"])

        play = {"ts": "2026-06-07T04:00:00+00:00", "game_pk": 1, "play_id": 1,
                "inning": 8, "wpa": 0.3, "side": "home", "qualifies": True}
        monkeypatch.setattr(rs, "_load_watermark", lambda p: "")
        monkeypatch.setattr(rs, "_new_high_wpa_plays", lambda wm, c: [play])
        monkeypatch.setattr(rs, "_benefiting_ticker", lambda gp, s: "KX-T")
        monkeypatch.setattr(rs, "_quote", lambda tk, **k: (ask - 0.02, ask))
        monkeypatch.setattr(rs, "_play_age_s", lambda *a: 5.0)
        monkeypatch.setattr(rs.pm, "record_entry", lambda *a, **k: None)
        opened, _ = rs._react(profile_name="t", broker=_B(), cfg=cfg,
                              remaining_cash=1000.0)
        return opened, bought

    def test_sub_floor_skipped(self, monkeypatch):
        opened, bought = self._run(monkeypatch, ask=0.12)
        assert opened == 0 and bought == []

    def test_above_floor_bought(self, monkeypatch):
        opened, bought = self._run(monkeypatch, ask=0.50)
        assert opened == 1 and len(bought) == 1


class TestHoldMode:
    """React-and-hold: same entry, no scalp exit, ride to resolution."""

    def test_hold_skips_manage(self, monkeypatch):
        import pandas as pd
        calls = {"manage": 0, "react": 0, "resolved": 0}

        class _B:
            is_live = False
            def get_positions(self): return pd.DataFrame()
            def available_cash(self, b): return b

        monkeypatch.setattr(rs.execution, "get_broker", lambda p: _B())
        monkeypatch.setattr(rs.emergency_stop, "is_active", lambda: False)
        monkeypatch.setattr(rs.profiles, "initialize_profiles", lambda *a, **k: None)
        monkeypatch.setattr(rs.profiles, "list_profiles", lambda: ["agentic_reactive_hold"])
        monkeypatch.setattr(rs, "_pin_profile_env", lambda n: {})
        monkeypatch.setattr(rs, "_restore_profile_env", lambda s: None)
        monkeypatch.setattr(rs, "_close_resolved",
                            lambda **k: calls.__setitem__("resolved", calls["resolved"] + 1) or 0)
        monkeypatch.setattr(rs, "_manage",
                            lambda **k: calls.__setitem__("manage", calls["manage"] + 1) or 0)
        monkeypatch.setattr(rs, "_react",
                            lambda **k: (calls.__setitem__("react", calls["react"] + 1) or 0, ""))
        rs.run_cycle(rs.ReactiveConfig(profile_name="agentic_reactive_hold",
                                       hold_to_resolution=True))
        assert calls["manage"] == 0          # no scalp exit in hold mode
        assert calls["react"] == 1 and calls["resolved"] == 1

    def test_scalp_mode_still_manages(self, monkeypatch):
        import pandas as pd
        calls = {"manage": 0}

        class _B:
            is_live = False
            def get_positions(self): return pd.DataFrame()
            def available_cash(self, b): return b

        monkeypatch.setattr(rs.execution, "get_broker", lambda p: _B())
        monkeypatch.setattr(rs.emergency_stop, "is_active", lambda: False)
        monkeypatch.setattr(rs.profiles, "initialize_profiles", lambda *a, **k: None)
        monkeypatch.setattr(rs.profiles, "list_profiles", lambda: ["agentic_reactive"])
        monkeypatch.setattr(rs, "_pin_profile_env", lambda n: {})
        monkeypatch.setattr(rs, "_restore_profile_env", lambda s: None)
        monkeypatch.setattr(rs, "_close_resolved", lambda **k: 0)
        monkeypatch.setattr(rs, "_manage",
                            lambda **k: calls.__setitem__("manage", calls["manage"] + 1) or 0)
        monkeypatch.setattr(rs, "_react", lambda **k: (0, ""))
        rs.run_cycle(rs.ReactiveConfig(hold_to_resolution=False))
        assert calls["manage"] == 1          # scalp mode manages

    def test_resolved_guard_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rs.profiles, "profile_dir", lambda p: tmp_path)
        monkeypatch.setattr(rs.profiles, "active_profile", lambda: "p")
        assert rs._is_resolved("KX-ATL") is False
        rs._mark_resolved("KX-ATL")
        assert rs._is_resolved("KX-ATL") is True
        assert rs._is_resolved(None) is False


class TestDepthGate:
    def _book(self, asks):
        import pandas as pd
        rows = [{"side": "ask", "price": p, "size": s} for p, s in asks]
        rows.append({"side": "bid", "price": 0.40, "size": 99999})
        return pd.DataFrame(rows)

    def test_depth_within_cents(self):
        # best ask 0.50; within 3c → 0.50 + 0.52 levels (100+200); 0.60 excluded.
        b = self._book([(0.50, 100), (0.52, 200), (0.60, 999)])
        assert rs._ask_depth_contracts(b, within_cents=0.03) == 300

    def test_depth_none_when_no_asks(self):
        import pandas as pd
        assert rs._ask_depth_contracts(pd.DataFrame()) is None

    def test_gate_skips_deep_book(self, monkeypatch):
        import pandas as pd
        bought = []

        class _B:
            is_live = False
            def get_positions(self):
                return pd.DataFrame()
            def buy(self, **k):
                bought.append(k)
                return rs.execution.OrderResult(
                    ok=True, status="paper", ticker=k["ticker"], side="yes",
                    action="buy", requested_count=k["count"],
                    filled_count=k["count"], avg_fill_price=k["price"])

        play = {"ts": "2026-06-20T04:00:00+00:00", "game_pk": 1, "play_id": 1,
                "inning": 8, "wpa": 0.3, "side": "home", "qualifies": True}
        monkeypatch.setattr(rs, "_load_watermark", lambda p: "")
        monkeypatch.setattr(rs, "_new_high_wpa_plays", lambda wm, c: [play])
        monkeypatch.setattr(rs, "_benefiting_ticker", lambda gp, s: "KX-T")
        monkeypatch.setattr(rs, "_quote", lambda tk, **k: (0.48, 0.50))
        monkeypatch.setattr(rs, "_play_age_s", lambda *a: 5.0)
        monkeypatch.setattr(rs, "_is_resolved", lambda t: False)
        monkeypatch.setattr(rs.pm, "record_entry", lambda *a, **k: None)
        cfg = rs.ReactiveConfig(max_entry_depth=60000.0)

        # deep book → skip
        monkeypatch.setattr(rs, "_entry_depth", lambda *a, **k: 200000.0)
        opened, _ = rs._react(profile_name="t", broker=_B(), cfg=cfg,
                              remaining_cash=1000.0)
        assert opened == 0 and bought == []

        # thin book → buy
        monkeypatch.setattr(rs, "_entry_depth", lambda *a, **k: 20000.0)
        opened, _ = rs._react(profile_name="t", broker=_B(), cfg=cfg,
                              remaining_cash=1000.0)
        assert opened == 1


class TestTieredStake:
    def test_legacy_when_no_base(self):
        cfg = rs.ReactiveConfig(bankroll=1000, max_position_pct=0.05)
        assert rs._target_stake(cfg, depth=5000) == 50.0   # bankroll*pct

    def test_base_when_not_thin_enough(self):
        cfg = rs.ReactiveConfig(base_stake_dollars=5.0, high_conf_stake_dollars=10.0,
                                high_conf_depth=20000.0)
        assert rs._target_stake(cfg, depth=40000) == 5.0   # passes gate but not thinnest

    def test_high_conf_for_thinnest(self):
        cfg = rs.ReactiveConfig(base_stake_dollars=5.0, high_conf_stake_dollars=10.0,
                                high_conf_depth=20000.0)
        assert rs._target_stake(cfg, depth=15000) == 10.0  # thinnest → high conf

    def test_base_when_depth_unknown(self):
        cfg = rs.ReactiveConfig(base_stake_dollars=5.0, high_conf_stake_dollars=10.0)
        assert rs._target_stake(cfg, depth=None) == 5.0    # no depth → base

    def test_base_only_no_high_conf(self):
        cfg = rs.ReactiveConfig(base_stake_dollars=5.0)    # high_conf unset
        assert rs._target_stake(cfg, depth=1000) == 5.0    # never bumps
