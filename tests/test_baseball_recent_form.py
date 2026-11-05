"""Tests for the recent-form blend: windowed queries + weight ramp +
blended strength composition."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from baseball import data, recent_form, state


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    db = tmp_path / "baseball_test.sqlite"
    monkeypatch.setenv("BASEBALL_STATE_PATH", str(db))
    state.migrate()
    return db


def _make_team(team_id: int, abbr: str) -> data.Team:
    return data.Team(
        id=team_id, abbreviation=abbr, name=abbr, short_name=abbr,
        league="AL", division="AL East",
    )


def _add_game(game_pk: int, home_id: int, away_id: int,
               home_score: int, away_score: int,
               days_ago: int = 1) -> None:
    """Insert a finalized game N days before now."""
    import sqlite3
    ts = (datetime.now(UTC)
          - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(state._state_path()) as conn:
        conn.execute("""
            INSERT INTO games (game_pk, game_date, status,
                                home_team_id, away_team_id,
                                home_score, away_score, updated_at)
            VALUES (?, ?, 'Final', ?, ?, ?, ?, ?)
        """, (game_pk, ts, home_id, away_id, home_score, away_score, ts))
        conn.commit()


def test_no_games_returns_none(temp_state):
    assert recent_form.get_recent_team_stats(team_id=999) is None


def test_returns_window_aggregate(temp_state):
    """5 games in the last 7 days for team 100: 3 wins, 2 losses,
    RS=25, RA=15."""
    state.upsert_teams([_make_team(100, "A"), _make_team(200, "B")])
    # All home games for team 100
    _add_game(1, 100, 200, home_score=6, away_score=3, days_ago=1)  # W
    _add_game(2, 100, 200, home_score=5, away_score=4, days_ago=2)  # W
    _add_game(3, 100, 200, home_score=2, away_score=5, days_ago=3)  # L
    _add_game(4, 100, 200, home_score=8, away_score=2, days_ago=4)  # W
    _add_game(5, 100, 200, home_score=4, away_score=6, days_ago=5)  # L

    r = recent_form.get_recent_team_stats(100)
    assert r is not None
    assert r["games_played"] == 5
    assert r["wins"] == 3
    assert r["losses"] == 2
    assert r["runs_scored"] == 25  # 6+5+2+8+4
    assert r["runs_allowed"] == 20  # 3+4+5+2+6
    assert r["run_diff_per_game"] == pytest.approx(1.0)


def test_window_excludes_old_games(temp_state):
    """Games older than the window don't count."""
    state.upsert_teams([_make_team(100, "A"), _make_team(200, "B")])
    _add_game(1, 100, 200, 10, 0, days_ago=1)  # in window
    _add_game(2, 100, 200, 0, 10, days_ago=20) # out of window

    r = recent_form.get_recent_team_stats(100, days=15)
    assert r["games_played"] == 1
    assert r["runs_scored"] == 10


def test_window_handles_away_games(temp_state):
    """Aggregation correctly attributes RS/RA for away appearances."""
    state.upsert_teams([_make_team(100, "A"), _make_team(200, "B")])
    _add_game(1, 200, 100, home_score=2, away_score=7, days_ago=1)  # team 100 won as away, scored 7

    r = recent_form.get_recent_team_stats(100)
    assert r["wins"] == 1
    assert r["runs_scored"] == 7
    assert r["runs_allowed"] == 2


def test_recent_weight_ramp():
    """Weight ramps linearly from 0 to MAX_RECENT_WEIGHT, capped."""
    assert recent_form.recent_weight(0) == 0.0
    assert recent_form.recent_weight(-5) == 0.0
    # Midway: 6 games → half of MAX
    assert recent_form.recent_weight(6) == pytest.approx(0.15)
    # Full cap
    assert recent_form.recent_weight(12) == pytest.approx(0.30)
    # Cap holds
    assert recent_form.recent_weight(100) == pytest.approx(0.30)


def test_blended_strength_no_recent_returns_season(temp_state):
    """Empty recent → season returned unchanged, weight=0."""
    s, w = recent_form.blended_strength(
        season_strength=0.55, recent_strength=None, recent_gp=0,
    )
    assert s == 0.55
    assert w == 0.0


def test_blended_strength_full_weight():
    """12+ recent games + recent=0.60 + season=0.50 → 0.5*0.30 + 0.5*0.70 = 0.53"""
    s, w = recent_form.blended_strength(
        season_strength=0.50, recent_strength=0.60, recent_gp=15,
    )
    assert w == pytest.approx(0.30)
    assert s == pytest.approx(0.50 * 0.70 + 0.60 * 0.30)


def test_blended_strength_proportional_weight():
    """6 recent games → half-weight blend."""
    s, w = recent_form.blended_strength(
        season_strength=0.50, recent_strength=0.70, recent_gp=6,
    )
    assert w == pytest.approx(0.15)
    assert s == pytest.approx(0.50 * 0.85 + 0.70 * 0.15)


def test_get_recent_form_table_filters_min_gp(temp_state):
    state.upsert_teams([_make_team(100, "A"), _make_team(200, "B")])
    _add_game(1, 100, 200, 5, 4, days_ago=1)  # only 1 game for team 100

    rows = recent_form.get_recent_form_table(min_gp=3)
    assert not any(r["abbreviation"] == "A" for r in rows)

    rows2 = recent_form.get_recent_form_table(min_gp=1)
    assert any(r["abbreviation"] == "A" for r in rows2)


def test_get_recent_form_table_sorts_hot_first(temp_state):
    state.upsert_teams([_make_team(100, "HOT"), _make_team(200, "COLD")])
    # HOT: 4 wins
    for pk, da in [(1, 1), (2, 2), (3, 3), (4, 4)]:
        _add_game(pk, 100, 200, 5, 1, days_ago=da)
    # COLD has lost the symmetric games against HOT — already inserted.
    # Need games where COLD plays SOMEONE else to be counted independently.

    rows = recent_form.get_recent_form_table(min_gp=2)
    # HOT should come first (4-0 vs 0-4)
    assert rows[0]["abbreviation"] == "HOT"
    assert rows[-1]["abbreviation"] == "COLD"


def test_blended_strength_returns_alpha():
    """The function returns (blended, alpha) so callers can audit."""
    _, alpha = recent_form.blended_strength(
        season_strength=0.5, recent_strength=0.7, recent_gp=12,
    )
    assert alpha == pytest.approx(0.30)
