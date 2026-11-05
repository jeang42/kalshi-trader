"""Tests for the WNBA sidecar: pure model, ticker bridge, state, data
parsing. No network — data tests monkeypatch the HTTP layer."""

from __future__ import annotations

import pytest

from wnba import data, model

# ─── model (pure) ──────────────────────────────────────────────────────────

def test_regressed_pythagorean_no_games_is_500():
    assert model.regressed_pythagorean(0, 0, 0) == pytest.approx(0.5, abs=1e-9)


def test_regressed_pythagorean_equal_points_is_500():
    assert model.regressed_pythagorean(800, 800, 10) == pytest.approx(0.5)


def test_regressed_pythagorean_prior_pulls_toward_500():
    """A small-sample blowout team shouldn't read as near-1.0 — the prior
    regresses it. With the high basketball exponent the raw ratio is
    extreme, so the regression effect must be visible."""
    raw = (90.0 ** model.PYTHAGOREAN_EXPONENT) / (
        90.0 ** model.PYTHAGOREAN_EXPONENT + 80.0 ** model.PYTHAGOREAN_EXPONENT)
    # one game, 90-80: regressed should be well below the raw single-game ratio
    regressed = model.regressed_pythagorean(90, 80, 1)
    assert regressed < raw
    assert 0.5 < regressed < 0.62


def test_home_court_on_even_teams_is_about_55pct():
    """Two equal-strength teams: home court alone should give ~0.55."""
    p = model.predict_matchup(home_strength=0.5, away_strength=0.5)
    assert p.home_win_prob == pytest.approx(0.55, abs=0.005)


def test_neutral_site_removes_home_edge():
    p = model.predict_matchup(home_strength=0.5, away_strength=0.5,
                              home_field=False)
    assert p.home_win_prob == pytest.approx(0.5, abs=1e-9)
    assert "neutral site" in p.notes


def test_stronger_home_team_favored():
    p = model.predict_matchup(home_strength=0.65, away_strength=0.45)
    assert p.home_win_prob > 0.65


def test_to_inputs_is_serializable():
    import json
    p = model.predict_matchup(home_strength=0.6, away_strength=0.4)
    json.dumps(p.to_inputs())  # must not raise


# ─── ticker bridge + abbr map ──────────────────────────────────────────────

def test_parse_ticker_basic():
    import wnba
    assert wnba._parse_ticker("KXWNBAGAME-26JUN03TORNY-NY") == ("2026-06-03", "NY")


def test_parse_ticker_maps_kalshi_abbr_to_espn():
    import wnba
    # Kalshi CONN → ESPN CON ; Kalshi PDX → ESPN POR
    assert wnba._parse_ticker("KXWNBAGAME-26JUN02CONNATL-CONN") == ("2026-06-02", "CON")
    assert wnba._parse_ticker("KXWNBAGAME-26JUN02PDXGS-PDX") == ("2026-06-02", "POR")


def test_parse_ticker_passthrough_unmapped_abbr():
    import wnba
    assert wnba._parse_ticker("KXWNBAGAME-26JUN02LVLA-LV") == ("2026-06-02", "LV")


def test_parse_ticker_rejects_garbage():
    import wnba
    assert wnba._parse_ticker("NOT-A-TICKER") is None
    assert wnba._parse_ticker("") is None
    assert wnba._parse_ticker("KXMLBGAME-26JUN02NYMMIA-NYM") is None  # wrong sport


# ─── state (real SQLite at a temp path) ────────────────────────────────────

@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("WNBA_STATE_PATH", str(tmp_path / "wnba.sqlite"))
    from wnba import state
    state.migrate()
    return state


def test_migrate_idempotent(temp_state):
    temp_state.migrate()  # second call must not raise
    assert temp_state.get_teams() == []


def test_team_and_prediction_roundtrip(temp_state):
    s = temp_state
    s.upsert_teams([data.Team(id=1, abbreviation="NY", name="New York Liberty",
                              short_name="Liberty", location="New York")])
    stats = data.TeamSeasonStats(team_id=1, season=2026, wins=6, losses=2,
                                 points_for=680, points_against=620)
    s.upsert_team_season(stats)
    assert len(s.get_all_team_seasons(2026)) == 1
    s.record_prediction("EVT1", 0.62, "NY", "ATL", "0.1.0", {"x": 1})
    rows = s.get_predictions_for_game("EVT1")
    assert rows and rows[0]["home_win_prob"] == pytest.approx(0.62)


def test_brier_summary_after_settle(temp_state):
    s = temp_state
    s.record_prediction("EVT1", 0.70, "NY", "ATL", "0.1.0", {})
    s.settle_prediction("EVT1", "NY")  # home won; predicted 0.70
    summary = s.get_brier_summary(2026)
    assert summary["n"] == 1
    assert summary["brier"] == pytest.approx((0.70 - 1.0) ** 2)


# ─── data parsing (mocked HTTP) ────────────────────────────────────────────

def test_get_schedule_parses_competitors(monkeypatch):
    fake = {"events": [{
        "id": "401856953", "date": "2026-06-02T00:00Z",
        "status": {"type": {"state": "in"}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "score": "48",
             "team": {"id": "6", "abbreviation": "DAL",
                      "displayName": "Dallas Wings", "shortDisplayName": "Wings",
                      "location": "Dallas"}},
            {"homeAway": "away", "score": "29",
             "team": {"id": "14", "abbreviation": "SEA",
                      "displayName": "Seattle Storm", "shortDisplayName": "Storm",
                      "location": "Seattle"}},
        ]}],
    }]}
    monkeypatch.setattr(data, "_get", lambda *a, **k: fake)
    games = data.get_schedule()
    assert len(games) == 1
    g = games[0]
    assert g.home.abbreviation == "DAL" and g.away.abbreviation == "SEA"
    assert g.home_score == 48 and g.away_score == 29
    assert g.status == "in"


def test_get_team_season_stats_parses_standings(monkeypatch):
    fake = {"children": [{"standings": {"entries": [{
        "team": {"id": "20"},
        "stats": [
            {"name": "wins", "value": 5}, {"name": "losses", "value": 2},
            {"name": "pointsFor", "value": 587},
            {"name": "pointsAgainst", "value": 558},
        ],
    }]}}]}
    monkeypatch.setattr(data, "_get", lambda *a, **k: fake)
    stats = data.get_team_season_stats(2026)
    assert 20 in stats
    s = stats[20]
    assert s.wins == 5 and s.points_for == 587 and s.games_played == 7
