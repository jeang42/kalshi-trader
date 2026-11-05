"""Offline tests for the WNBA live layer: parsing + WP-swing detection."""

from __future__ import annotations

from pm.live import wnba_live as wl


def _comp(gid, home_ab, home_sc, home_win, away_ab, away_sc, away_win,
          state, period=4, clock="2:00"):
    return {
        "id": gid,
        "status": {"type": {"state": state}, "period": period,
                   "displayClock": clock},
        "competitors": [
            {"homeAway": "home", "score": home_sc, "winner": home_win,
             "team": {"abbreviation": home_ab}},
            {"homeAway": "away", "score": away_sc, "winner": away_win,
             "team": {"abbreviation": away_ab}},
        ],
    }


class TestParse:
    def test_parse_live_game(self):
        g = wl.parse_game(_comp("1", "NY", 80, False, "ATL", 78, False, "in"),
                          home_winprob=0.62)
        assert g.home_abbr == "NY" and g.away_abbr == "ATL"
        assert g.home_score == 80 and g.away_score == 78
        assert g.home_winprob == 0.62 and g.state == "in" and g.winner is None

    def test_parse_final_winner(self):
        g = wl.parse_game(_comp("2", "NY", 90, True, "ATL", 85, False, "post"),
                          home_winprob=1.0)
        assert g.winner == "NY" and g.state == "post"

    def test_parse_rejects_bad(self):
        assert wl.parse_game({"competitors": [{}]}, home_winprob=0.5) is None

    def test_latest_home_winprob(self):
        s = {"winprobability": [{"homeWinPercentage": 0.5},
                                {"homeWinPercentage": 0.71}]}
        assert wl.latest_home_winprob(s) == 0.71
        assert wl.latest_home_winprob({"winprobability": []}) is None
        assert wl.latest_home_winprob({}) is None


def _g(gid, hwp, state="in", winner=None, home="NY", away="ATL",
       hs=80, as_=78):
    return wl.LiveGame(gid, home, away, state, hs, as_, hwp, 4, "2:00", winner)


class TestDetect:
    def test_swing_toward_home(self):
        ev = wl.detect_events(_g("1", 0.50), _g("1", 0.65), wp_threshold=0.10)
        assert len(ev) == 1 and ev[0].kind == "wp_swing"
        assert ev[0].beneficiary == "NY" and ev[0].wp_delta == 0.15

    def test_swing_toward_away(self):
        ev = wl.detect_events(_g("1", 0.60), _g("1", 0.45), wp_threshold=0.10)
        assert ev[0].beneficiary == "ATL" and ev[0].wp_delta == -0.15

    def test_small_move_no_event(self):
        assert wl.detect_events(_g("1", 0.60), _g("1", 0.66),
                                wp_threshold=0.10) == []

    def test_game_final_event(self):
        ev = wl.detect_events(_g("1", 0.9, state="in"),
                              _g("1", 1.0, state="post", winner="NY"))
        kinds = {e.kind: e.beneficiary for e in ev}
        assert kinds.get("game") == "NY"

    def test_different_game_ignored(self):
        assert wl.detect_events(_g("a", 0.5), _g("b", 0.9)) == []

    def test_none_winprob_no_swing(self):
        assert wl.detect_events(_g("1", None), _g("1", 0.7)) == []
