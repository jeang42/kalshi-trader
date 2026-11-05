"""Tests for the live-gate inspector.

The consistency checks are pure functions over a resolved report, so they can
be tested without touching credentials, the network, or the environment.
"""

from __future__ import annotations

from pm.trading import live_gates as lg


def _profile(name="p", **kw):
    defaults = dict(
        profile=name, exists=True, in_allow_list=False, is_owner=False,
        auth_configured=False, trading_enabled=False, broker="paper",
        arm_flag=None, arm_flag_on=None,
    )
    defaults.update(kw)
    return lg.ProfileGates(**defaults)


def _report(profiles, **kw):
    defaults = dict(
        live_enabled=True, owner_profile="owner", owner_profile_exists=True,
        allow_list=[], emergency_stop_active=False, profiles=profiles,
    )
    defaults.update(kw)
    return lg.GateReport(**defaults)


class TestCanPlaceOrders:
    def test_paper_broker_never_places_orders(self):
        assert not _profile(broker="paper").can_place_orders

    def test_live_broker_without_an_arm_flag_can_place(self):
        assert _profile(broker="live").can_place_orders

    def test_live_broker_with_its_switch_off_cannot_place(self):
        """The state the canary was left in during the P0 pass: authorized,
        but its own flag off. Reporting this as armed would be wrong."""
        p = _profile(broker="live", arm_flag="NEARCLOSE_CANARY_ENABLED",
                     arm_flag_on=False)
        assert not p.can_place_orders

    def test_live_broker_with_its_switch_on_can_place(self):
        p = _profile(broker="live", arm_flag="NEARCLOSE_CANARY_ENABLED",
                     arm_flag_on=True)
        assert p.can_place_orders


class TestArmed:
    def test_armed_lists_only_profiles_that_can_actually_place(self):
        report = _report([
            _profile("a", broker="live", arm_flag="F", arm_flag_on=True),
            _profile("b", broker="live", arm_flag="F", arm_flag_on=False),
            _profile("c", broker="paper"),
        ])
        assert report.armed == ["a"]

    def test_nothing_armed_when_all_paper(self):
        assert _report([_profile("a"), _profile("b")]).armed == []


class TestFindProblems:
    def test_clean_configuration_has_no_problems(self):
        problems, notes = lg.find_problems(_report([_profile("a")]))
        assert problems == []
        assert notes == []

    def test_owner_profile_that_does_not_exist_is_a_problem(self):
        report = _report([_profile("a")], owner_profile="ghost",
                         owner_profile_exists=False)
        problems, _ = lg.find_problems(report)
        assert any("ghost" in p for p in problems)

    def test_allow_listed_profile_that_does_not_exist_is_a_problem(self):
        report = _report([_profile("ghost", exists=False, in_allow_list=True)])
        problems, _ = lg.find_problems(report)
        assert any("LIVE_TRADING_PROFILES" in p for p in problems)

    def test_one_boolean_from_live_is_a_note_not_a_problem(self):
        """Deliberate most of the time (an arm parked between runs), so it must
        not fail the exit code — but it must still be visible, because an
        allow-list entry outliving its purpose is how a falsified strategy
        stays armed."""
        report = _report([_profile("a", broker="live", arm_flag="FLAG",
                                   arm_flag_on=False, in_allow_list=True)])
        problems, notes = lg.find_problems(report)
        assert problems == []
        assert any("FLAG" in n for n in notes)

    def test_tripped_drawdown_breaker_is_a_problem(self):
        report = _report([_profile("a")], drawdown={
            "paused": True, "equity_dollars": 80.0, "peak_equity_dollars": 100.0})
        problems, _ = lg.find_problems(report)
        assert any("drawdown" in p.lower() for p in problems)

    def test_untripped_drawdown_breaker_is_silent(self):
        report = _report([_profile("a")], drawdown={
            "paused": False, "equity_dollars": 99.0, "peak_equity_dollars": 100.0})
        problems, _ = lg.find_problems(report)
        assert problems == []


class TestFormatReport:
    def test_banner_states_armed_profiles(self):
        report = _report([_profile("armed_one", broker="live")])
        assert "ARMED" in lg.format_report(report)
        assert "armed_one" in lg.format_report(report)

    def test_banner_states_not_armed_when_nothing_can_trade(self):
        assert "NOT ARMED" in lg.format_report(_report([_profile("a")]))

    def test_missing_owner_profile_is_called_out_inline(self):
        report = _report([_profile("a")], owner_profile="ghost",
                         owner_profile_exists=False)
        assert "DOES NOT EXIST" in lg.format_report(report)

    def test_renders_both_problems_and_notes(self):
        report = _report(
            [_profile("a", broker="live", arm_flag="FLAG", arm_flag_on=False)],
            owner_profile="ghost", owner_profile_exists=False)
        report.problems, report.notes = lg.find_problems(report)
        text = lg.format_report(report)
        assert "PROBLEMS" in text
        assert "NOTES" in text


class TestAsProfile:
    def test_pins_and_restores_the_active_profile(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_ACTIVE_PROFILE", "before")
        with lg._as_profile("during"):
            import os
            assert os.environ["POLYMARKET_ACTIVE_PROFILE"] == "during"
        import os
        assert os.environ["POLYMARKET_ACTIVE_PROFILE"] == "before"

    def test_removes_the_variable_when_it_was_unset(self, monkeypatch):
        monkeypatch.delenv("POLYMARKET_ACTIVE_PROFILE", raising=False)
        with lg._as_profile("during"):
            pass
        import os
        assert "POLYMARKET_ACTIVE_PROFILE" not in os.environ


class TestAllowList:
    def test_parses_and_strips(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING_PROFILES", " a , b ,, c ")
        assert lg._allow_list() == ["a", "b", "c"]

    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("LIVE_TRADING_PROFILES", raising=False)
        assert lg._allow_list() == []
