"""State-layer tests. Uses a temp SQLite path via monkeypatched env var
so production state is never touched."""

from __future__ import annotations

from datetime import UTC

import pytest

from baseball import data, state


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    db = tmp_path / "baseball_test.sqlite"
    monkeypatch.setenv("BASEBALL_STATE_PATH", str(db))
    state.migrate()
    return db


@pytest.fixture
def sample_team():
    return data.Team(
        id=147, abbreviation="NYY", name="New York Yankees",
        short_name="Yankees", league="AL", division="AL East",
    )


@pytest.fixture
def sample_team_2():
    return data.Team(
        id=111, abbreviation="BOS", name="Boston Red Sox",
        short_name="Red Sox", league="AL", division="AL East",
    )


def test_migration_is_idempotent(temp_state):
    state.migrate()
    state.migrate()  # second call should be a no-op
    assert temp_state.exists()


def test_upsert_team_roundtrip(temp_state, sample_team):
    state.upsert_teams([sample_team])
    teams = state.get_teams()
    assert len(teams) == 1
    assert teams[0]["abbreviation"] == "NYY"


def test_team_season_stats_derived_fields(temp_state, sample_team):
    state.upsert_teams([sample_team])
    s = data.TeamSeasonStats(
        team_id=147, season=2026,
        wins=30, losses=20, runs_scored=250, runs_allowed=200, games_played=50,
    )
    state.upsert_team_season(s)
    row = state.get_team_season(147, 2026)
    assert row is not None
    assert row["wins"] == 30
    assert row["pythagorean"] == pytest.approx(s.pythagorean)


def test_activity_log_bounded(temp_state):
    """The 1000-event ring should not grow past 1000."""
    for i in range(1100):
        state.log_activity("test", f"event {i}")
    rows = state.get_recent_activity(limit=2000)
    assert len(rows) <= 1000


def test_settle_prediction_roundtrip(temp_state, sample_team, sample_team_2):
    state.upsert_teams([sample_team, sample_team_2])
    state.record_prediction(
        game_pk=12345, home_win_prob=0.62, home="NYY", away="BOS",
        model_version="0.1", inputs={"foo": "bar"},
    )
    state.settle_prediction(12345, "NYY")
    rows = state.get_predictions_for_game(12345)
    assert len(rows) == 1
    assert rows[0]["actual_winner"] == "NYY"


def test_brier_summary_empty(temp_state):
    assert state.get_brier_summary(2026) == {"n": 0, "brier": None}


def test_brier_calculation(temp_state, sample_team, sample_team_2):
    """Two perfect predictions should yield Brier 0; two opposite-outcome
    predictions at 0.5 should yield 0.25."""
    state.upsert_teams([sample_team, sample_team_2])
    import sqlite3
    from datetime import datetime, timezone
    # Hand-insert games with matching dates so the join works.
    today = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00+00:00")
    season = int(today[:4])

    with sqlite3.connect(temp_state) as conn:
        conn.execute("""INSERT INTO games (game_pk, game_date, status,
                       home_team_id, away_team_id, updated_at)
                       VALUES (?, ?, 'Final', 147, 111, ?)""",
                     (1, today, today))
        conn.execute("""INSERT INTO games (game_pk, game_date, status,
                       home_team_id, away_team_id, updated_at)
                       VALUES (?, ?, 'Final', 147, 111, ?)""",
                     (2, today, today))
        conn.commit()

    state.record_prediction(1, 0.5, "NYY", "BOS", "test", {})
    state.record_prediction(2, 0.5, "NYY", "BOS", "test", {})
    state.settle_prediction(1, "NYY")
    state.settle_prediction(2, "BOS")
    summary = state.get_brier_summary(season)
    assert summary["n"] == 2
    assert summary["brier"] == pytest.approx(0.25, abs=1e-9)


def test_db_size_bytes(temp_state):
    assert state.db_size_bytes() > 0
