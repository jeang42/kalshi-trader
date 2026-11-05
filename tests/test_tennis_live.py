"""Offline tests for the tennis live layer: parsing + event detection."""

from __future__ import annotations

from pm.live import tennis_live as tl


def _comp(id_, n1, ls1, win1, n2, ls2, win2, state):
    return {
        "id": id_, "status": {"type": {"state": state}},
        "round": {"displayName": "Final"},
        "competitors": [
            {"athlete": {"displayName": n1}, "linescores": ls1, "winner": win1},
            {"athlete": {"displayName": n2}, "linescores": ls2, "winner": win2},
        ],
    }


class TestParse:
    def test_sets_and_games(self):
        # 1 completed set (won) + an in-progress set with 3 games.
        assert tl._sets_and_games(
            [{"value": 6, "winner": True}, {"value": 3}]) == (1, 3)
        assert tl._sets_and_games([]) == (0, 0)
        assert tl._sets_and_games(None) == (0, 0)

    def test_parse_competition(self):
        comp = _comp("m1", "Carlos Alcaraz", [{"value": 6, "winner": True},
                                              {"value": 4}], False,
                     "Jannik Sinner", [{"value": 4, "winner": False},
                                       {"value": 5}], False, "in")
        m = tl.parse_competition(comp, tour="atp", tournament="Wimbledon")
        assert m.p1 == "Carlos Alcaraz" and m.p2 == "Jannik Sinner"
        assert m.p1_sets == 1 and m.p2_sets == 0
        assert m.p1_games == 4 and m.p2_games == 5 and m.state == "in"
        assert m.winner is None

    def test_parse_final_winner(self):
        comp = _comp("m2", "A", [{"value": 6, "winner": True},
                                 {"value": 6, "winner": True}], True,
                     "B", [{"value": 3}, {"value": 4}], False, "post")
        m = tl.parse_competition(comp, tour="wta", tournament="T")
        assert m.winner == "A" and m.p1_sets == 2 and m.state == "post"

    def test_parse_rejects_non_two_player(self):
        assert tl.parse_competition({"competitors": [{}]}, tour="atp",
                                    tournament="T") is None


def _m(mid, s1, s2, g1=0, g2=0, state="in", winner=None,
       p1="A", p2="B"):
    return tl.LiveMatch(mid, "atp", "T", "R16", state, p1, p2,
                        s1, s2, g1, g2, winner)


class TestDetectEvents:
    def test_set_won_by_p1(self):
        ev = tl.detect_events(_m("x", 0, 0, 5, 4), _m("x", 1, 0, 0, 0))
        assert len(ev) == 1 and ev[0].kind == "set" and ev[0].beneficiary == "A"

    def test_set_won_by_p2(self):
        ev = tl.detect_events(_m("x", 1, 0), _m("x", 1, 1))
        assert ev[0].beneficiary == "B" and ev[0].kind == "set"

    def test_match_won(self):
        ev = tl.detect_events(_m("x", 1, 1, state="in"),
                              _m("x", 2, 1, state="post", winner="A"))
        kinds = {e.kind: e.beneficiary for e in ev}
        assert kinds.get("set") == "A" and kinds.get("match") == "A"

    def test_no_change_no_events(self):
        assert tl.detect_events(_m("x", 1, 0, 3, 2), _m("x", 1, 0, 3, 2)) == []

    def test_different_match_ignored(self):
        assert tl.detect_events(_m("a", 0, 0), _m("b", 1, 0)) == []


class TestBreakDetection:
    def test_up_a_break_p1(self):
        # 1-1 -> 3-1 in the current set: p1 crossed to +2 (a break occurred).
        ev = tl.detect_events(_m("x", 0, 0, 1, 1), _m("x", 0, 0, 3, 1))
        brk = [e for e in ev if e.kind == "break"]
        assert len(brk) == 1 and brk[0].beneficiary == "A"

    def test_up_a_break_p2(self):
        ev = tl.detect_events(_m("x", 0, 0, 2, 2), _m("x", 0, 0, 2, 4))
        brk = [e for e in ev if e.kind == "break"]
        assert len(brk) == 1 and brk[0].beneficiary == "B"

    def test_hold_no_break(self):
        # 2-2 -> 3-2 (lead only +1, consistent with a hold) → no break event.
        assert tl.detect_events(_m("x", 0, 0, 2, 2), _m("x", 0, 0, 3, 2)) == []

    def test_no_refire_when_lead_extends(self):
        # already +2 (3-1) -> +3 (4-1): break already signalled, don't re-fire.
        assert tl.detect_events(_m("x", 0, 0, 3, 1), _m("x", 0, 0, 4, 1)) == []

    def test_set_rollover_not_a_break(self):
        # set completes (games reset, set count up): emit set, NOT a phantom
        # break from the cross-set game comparison.
        ev = tl.detect_events(_m("x", 0, 0, 5, 4), _m("x", 1, 0, 0, 0))
        kinds = [e.kind for e in ev]
        assert "set" in kinds and "break" not in kinds

    def test_break_back_to_level_no_event(self):
        # +2 -> level (opponent broke back): MVP doesn't emit on lead shrinking
        # (break-back is a future refinement) — just no spurious p1 break.
        ev = tl.detect_events(_m("x", 0, 0, 3, 1), _m("x", 0, 0, 3, 3))
        assert [e for e in ev if e.kind == "break"] == []
