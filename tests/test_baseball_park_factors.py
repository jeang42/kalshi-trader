"""Tests for the park-factor module — calibration math + integration."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from baseball import data, park_factors, state


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    db = tmp_path / "baseball_test.sqlite"
    monkeypatch.setenv("BASEBALL_STATE_PATH", str(db))
    state.migrate()
    # park_factors reads from live_plays too — initialize that schema.
    from baseball.live import _ensure_live_schema
    _ensure_live_schema()
    park_factors._CACHE.clear()
    return db


def _make_team(team_id: int, abbr: str) -> data.Team:
    return data.Team(
        id=team_id, abbreviation=abbr, name=abbr, short_name=abbr,
        league="AL", division="AL East",
    )


def _add_final_game(game_pk: int, home_id: int, away_id: int,
                     home_score: int, away_score: int,
                     game_date_iso: str = "2026-05-01T18:00:00+00:00") -> None:
    """Helper — directly insert a finalized game into the test DB."""
    import sqlite3
    with sqlite3.connect(state._state_path()) as conn:
        conn.execute("""
            INSERT INTO games (game_pk, game_date, status,
                                home_team_id, away_team_id,
                                home_score, away_score, updated_at)
            VALUES (?, ?, 'Final', ?, ?, ?, ?, ?)
        """, (game_pk, game_date_iso, home_id, away_id,
              home_score, away_score, game_date_iso))
        conn.commit()


def test_no_games_returns_empty(temp_state):
    """No games loaded → no park factors."""
    assert park_factors.compute_park_factors() == {}


def test_neutral_park_returns_factor_one(temp_state):
    """When the park's combined RPG matches the league average, factor
    should regress all the way to 1.0 (since regressed prior is 1.0 too)."""
    state.upsert_teams([_make_team(100, "A"), _make_team(200, "B")])
    # All games are 5-4 finals at the same park (id=100)
    # League average combined RPG = 9.0; park combined RPG = 9.0 → ratio 1.0
    for pk in range(1, 11):
        _add_final_game(pk, home_id=100, away_id=200, home_score=5, away_score=4)
    out = park_factors.compute_park_factors(min_games=5)
    assert 100 in out
    assert out[100].runs_factor == pytest.approx(1.0, abs=0.05)


def test_high_runs_park_factor_above_one(temp_state):
    """A park where combined RPG > league average → factor > 1, but
    regressed by the 30-game prior."""
    state.upsert_teams([_make_team(100, "HOT"), _make_team(200, "OPP")])
    # 10 games at park 100 averaging 16 combined RPG (very high)
    # Other parks: 10 games at park 200 averaging 8 combined RPG (league-avg-ish)
    for pk in range(1, 11):
        _add_final_game(pk, home_id=100, away_id=200, home_score=10, away_score=6)
    for pk in range(11, 21):
        _add_final_game(pk, home_id=200, away_id=100, home_score=4, away_score=4)

    out = park_factors.compute_park_factors(min_games=5)
    assert out[100].runs_factor > 1.0
    # But not as high as the raw ratio because of the 30-game prior
    assert out[100].runs_factor < 1.4


def test_min_games_filter(temp_state):
    """Parks with fewer than min_games are omitted."""
    state.upsert_teams([_make_team(100, "A"), _make_team(200, "B")])
    _add_final_game(1, 100, 200, 5, 4)
    _add_final_game(2, 100, 200, 6, 3)
    out = park_factors.compute_park_factors(min_games=5)
    assert 100 not in out  # only 2 games, below threshold


def test_park_neutralize_strength_no_factor_passthrough(temp_state):
    """Team without an estimated park factor → inputs returned unchanged."""
    rs, ra = park_factors.park_neutralize_strength(
        team_id=999,  # no team data
        runs_scored=250, runs_allowed=200, games_played=50,
    )
    assert (rs, ra) == (250, 200)


def test_park_neutralize_strength_inflated_park_lowers_inputs(temp_state):
    """A team that plays in a hitter-friendly park has inflated RS+RA.
    Park-neutralizing should lower BOTH numbers proportionally — RS and
    RA scale together because the park inflates both."""
    state.upsert_teams([_make_team(100, "COL"), _make_team(200, "OPP")])
    # Make Coors-like park: all home games 12-10 (combined 22, league avg ~9)
    for pk in range(1, 16):
        _add_final_game(pk, 100, 200, 12, 10)
    # 30+ road games to give the other team plenty of road data — but we
    # only need games at OTHER parks for the league average calc.
    for pk in range(16, 46):
        _add_final_game(pk, 200, 100, 4, 4)

    rs, ra = park_factors.park_neutralize_strength(
        team_id=100, runs_scored=300, runs_allowed=300, games_played=50,
    )
    # Park factor should be > 1, so home-half divided by >1 = smaller
    assert rs < 300
    assert ra < 300
    # Symmetric scaling
    assert abs((rs - ra) - 0) < 5


def test_get_park_factors_cached(temp_state):
    """Repeat calls return cached result without recomputing."""
    state.upsert_teams([_make_team(100, "A"), _make_team(200, "B")])
    for pk in range(1, 11):
        _add_final_game(pk, 100, 200, 5, 4)

    a = park_factors.get_park_factors()
    b = park_factors.get_park_factors()
    assert a is b  # same object — cache hit

    c = park_factors.get_park_factors(refresh=True)
    # Different object after refresh (recomputed)
    assert a is not c


def test_hr_factors_from_live_plays(temp_state):
    """HR factor calibrated from the live_plays table. Needs games at
    MULTIPLE parks to have a meaningful league baseline."""
    state.upsert_teams([_make_team(100, "HR_PARK"),
                          _make_team(200, "PITCH_PARK")])
    # 10 games at park 100 (HR-friendly)
    for pk in range(1, 11):
        _add_final_game(pk, 100, 200, 5, 4)
    # 10 games at park 200 (HR-suppressing)
    for pk in range(11, 21):
        _add_final_game(pk, 200, 100, 4, 4)

    # 3 HRs per game at park 100; 0 HRs at park 200
    import sqlite3
    with sqlite3.connect(state._state_path()) as conn:
        for game_pk in range(1, 11):
            for i in range(3):
                conn.execute("""
                    INSERT INTO live_plays
                      (game_pk, play_id, ts, event_type, fetched_at)
                    VALUES (?, ?, '2026-05-01T18:00:00Z', 'Home Run',
                             '2026-05-01T18:00:00Z')
                """, (game_pk, i))
        conn.commit()

    hr = park_factors.compute_hr_factors(min_games=5)
    # League HR/game = 30 HRs / 20 games = 1.5
    # Park 100 = 3.0 / 1.5 = 2.0 raw, regressed → > 1
    # Park 200 has no HR plays → not in the result (HAVING n_games >= 5
    # filters by games with HR plays, not just games)
    assert hr.get(100) is not None
    assert hr[100] > 1.0


def test_get_factor_for_team_returns_none_when_no_data(temp_state):
    """Cheap accessor for one team. None means we have no estimate."""
    assert park_factors.get_factor_for_team(999) is None
