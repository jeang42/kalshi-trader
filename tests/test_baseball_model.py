"""Pure-function tests for baseball.model. No network, no SQLite."""

from __future__ import annotations

import math

import pytest

from baseball import model


def test_regressed_pythagorean_with_no_games_is_500():
    assert model.regressed_pythagorean(0, 0, 0) == pytest.approx(0.5, abs=1e-9)


def test_regressed_pythagorean_neutral_when_runs_equal():
    """RS = RA → P(win) = 0.5 regardless of game count."""
    assert model.regressed_pythagorean(200, 200, 50) == pytest.approx(0.5)


def test_regressed_pythagorean_prior_pulls_toward_500():
    """A 5-1 team with +20 run diff should NOT read as .800+. The
    Bayesian prior pulls a small sample's extreme record back toward .500."""
    raw_pyth = 30**2 / (30**2 + 10**2)  # ~0.900
    regressed = model.regressed_pythagorean(30, 10, 6)
    assert regressed < raw_pyth
    assert 0.50 < regressed < 0.65   # well off the raw .900


def test_regressed_pythagorean_full_season_close_to_raw():
    """Over 162 games, the prior weight (~40 games) matters less."""
    rs, ra, gp = 800, 600, 162
    raw = rs**2 / (rs**2 + ra**2)
    regressed = model.regressed_pythagorean(rs, ra, gp)
    assert abs(regressed - raw) < 0.05


def test_logodds_round_trip():
    for p in [0.10, 0.30, 0.50, 0.65, 0.95]:
        assert model._logodds_to_prob(model._prob_to_logodds(p)) == pytest.approx(p, abs=1e-9)


def test_logodds_clamps_extremes():
    """0.0 and 1.0 are clamped to avoid log(0)."""
    assert math.isfinite(model._prob_to_logodds(0.0))
    assert math.isfinite(model._prob_to_logodds(1.0))


def test_pitcher_adj_zero_when_era_missing():
    assert model.pitcher_logodds_adj(None) == 0.0


def test_pitcher_adj_disabled_in_v06():
    """SP ERA adjustment is zeroed in v0.6 — all ERAs produce 0 adjustment."""
    assert model.pitcher_logodds_adj(2.50) == pytest.approx(0.0, abs=1e-9)
    assert model.pitcher_logodds_adj(6.00) == pytest.approx(0.0, abs=1e-9)
    assert model.pitcher_logodds_adj(model.LEAGUE_AVG_ERA) == pytest.approx(0.0, abs=1e-9)


def test_equal_teams_neutral_site_50_50():
    pred = model.predict_matchup(
        home_strength=0.5, away_strength=0.5,
        home_field=False,
    )
    assert pred.home_win_prob == pytest.approx(0.5, abs=1e-9)


def test_equal_teams_home_field_above_50():
    """Pure home-field advantage should add ~2pp (halved in v0.6)."""
    pred = model.predict_matchup(home_strength=0.5, away_strength=0.5)
    assert 0.51 < pred.home_win_prob < 0.54


def test_strider_vs_replacement_pulls_against_better_team():
    """With SP disabled in v0.6, pitcher ERA has no effect on outcome.
    Both predictions should be identical."""
    pred = model.predict_matchup(
        home_strength=0.45, away_strength=0.62,
        home_pitcher_era=3.50, away_pitcher_era=2.30,
        home_team_name="MIA", away_team_name="ATL",
    )
    pred_no_pitchers = model.predict_matchup(
        home_strength=0.45, away_strength=0.62,
    )
    assert pred.home_win_prob == pytest.approx(pred_no_pitchers.home_win_prob, abs=1e-9)


def test_bullpen_logodds_adj_zero_when_missing():
    assert model.bullpen_logodds_adj(None) == 0.0


def test_bullpen_logodds_adj_disabled_in_v06():
    """Bullpen ERA adjustment zeroed in v0.6 — all produce 0."""
    assert model.bullpen_logodds_adj(2.50) == pytest.approx(0.0, abs=1e-9)
    assert model.bullpen_logodds_adj(5.50) == pytest.approx(0.0, abs=1e-9)
    assert model.bullpen_logodds_adj(
        model.LEAGUE_AVG_BULLPEN_ERA) == pytest.approx(0.0, abs=1e-9)


def test_predict_matchup_bullpen_no_effect_in_v06():
    """With bullpen disabled, lopsided bullpens don't move the line."""
    pred = model.predict_matchup(
        home_strength=0.5, away_strength=0.5,
        home_field=False,
        home_bullpen_era=2.50,
        away_bullpen_era=5.50,
    )
    assert pred.home_win_prob == pytest.approx(0.5, abs=1e-9)


def test_prediction_inputs_serializable():
    pred = model.predict_matchup(home_strength=0.5, away_strength=0.5)
    inputs = pred.to_inputs()
    import json
    json.dumps(inputs)  # raises if not JSON-serializable
    assert inputs["model_version"] == model.MODEL_VERSION
