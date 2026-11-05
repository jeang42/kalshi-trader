"""Public-surface tests for baseball/__init__.py — health, stats,
recent_activity, and the Sidecar-protocol registration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

import baseball
from baseball import data, state
from pm.core import sidecars


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    db = tmp_path / "baseball_test.sqlite"
    monkeypatch.setenv("BASEBALL_STATE_PATH", str(db))
    state.migrate()
    return db


def test_registered_with_sidecar_registry():
    """Importing the package should have called register_sidecar."""
    assert sidecars.get_sidecar("baseball") is not None
    found = [s.name for s in sidecars.list_sidecars()]
    assert "baseball" in found


def test_health_degraded_when_never_refreshed(temp_state):
    h = baseball.health()
    assert h.status == "degraded"
    assert "never refreshed" in h.detail.lower()


def test_health_ok_after_recent_refresh(temp_state):
    state.set_meta("last_refresh_ts",
                    datetime.now(UTC).isoformat())
    h = baseball.health()
    assert h.status == "ok"


def test_health_down_when_stale(temp_state):
    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    state.set_meta("last_refresh_ts", long_ago)
    h = baseball.health()
    assert h.status == "down"
    assert "stale" in h.detail.lower()


def test_health_degraded_when_errors(temp_state):
    state.set_meta("last_refresh_ts",
                    datetime.now(UTC).isoformat())
    state.log_activity("error", "test failure")
    h = baseball.health()
    assert h.status == "degraded"
    assert h.errors_24h >= 1


def test_stats_shape(temp_state):
    s = baseball.stats()
    assert set(s.keys()) >= {"season", "teams_loaded", "teams_rated",
                              "db_bytes", "brier_n", "brier_score"}


def test_recent_activity_empty_on_fresh_db(temp_state):
    assert baseball.recent_activity() == []


def test_recent_activity_returns_events(temp_state):
    state.log_activity("fetch", "test event")
    events = baseball.recent_activity(limit=5)
    assert len(events) == 1
    assert events[0].kind == "fetch"
    assert isinstance(events[0], sidecars.SidecarActivityEvent)


def test_predict_game_returns_none_for_unknown_team(temp_state):
    assert baseball.predict_game("XXX", "YYY") is None


def test_team_ratings_table_empty_when_no_data(temp_state):
    assert baseball.team_ratings_table() == []


def test_predict_game_uses_stored_prediction_when_available(temp_state):
    # Set up: one team pair with stats + a stored prediction for today.
    nyy = data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East")
    bos = data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East")
    state.upsert_teams([nyy, bos])
    season = datetime.now(UTC).year
    state.upsert_team_season(data.TeamSeasonStats(147, season, 30, 20, 250, 200, 50))
    state.upsert_team_season(data.TeamSeasonStats(111, season, 25, 25, 220, 220, 50))

    g = data.GameSchedule(
        game_pk=9999,
        game_date=datetime.now(UTC),
        status="Scheduled",
        home=nyy, away=bos,
    )
    state.upsert_game(g)
    state.record_prediction(9999, 0.65, "NYY", "BOS", "test", {"k": "v"})

    pred = baseball.predict_game("NYY", "BOS")
    assert pred is not None
    assert pred["source"] == "stored"
    assert pred["home_win_prob"] == 0.65


def test_predict_for_ticker_returns_yes_perspective(temp_state):
    """The ticker's YES team is encoded in the suffix. The helper should
    flip the home_win_prob when YES is the away team."""
    nyy = data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East")
    bos = data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East")
    state.upsert_teams([nyy, bos])
    season = datetime.now(UTC).year
    state.upsert_team_season(data.TeamSeasonStats(147, season, 30, 20, 250, 200, 50))
    state.upsert_team_season(data.TeamSeasonStats(111, season, 25, 25, 220, 220, 50))

    # Ticker date — pick a future date so it doesn't collide with real games
    g = data.GameSchedule(
        game_pk=9999,
        game_date=datetime(2030, 5, 23, 16, 10, tzinfo=UTC),
        status="Scheduled", home=bos, away=nyy,
    )
    state.upsert_game(g)
    state.record_prediction(9999, 0.42, "BOS", "NYY", "test", {})

    import baseball

    # YES=BOS (home) → matches stored 0.42
    home_yes = baseball.predict_for_ticker("KXMLBGAME-30MAY231610NYYBOS-BOS")
    assert home_yes is not None
    assert home_yes["yes_team"] == "BOS"
    assert home_yes["yes_side"] == "home"
    assert home_yes["yes_prob"] == pytest.approx(0.42, abs=1e-9)

    # YES=NYY (away) → 1 - 0.42 = 0.58
    away_yes = baseball.predict_for_ticker("KXMLBGAME-30MAY231610NYYBOS-NYY")
    assert away_yes is not None
    assert away_yes["yes_team"] == "NYY"
    assert away_yes["yes_side"] == "away"
    assert away_yes["yes_prob"] == pytest.approx(0.58, abs=1e-9)


def test_predict_for_ticker_rejects_non_mlb(temp_state):
    import baseball
    assert baseball.predict_for_ticker("KXPRES-2024-DEM") is None
    assert baseball.predict_for_ticker("") is None
    assert baseball.predict_for_ticker("not-a-ticker") is None


def test_predict_game_falls_back_when_no_stored_prediction(temp_state):
    nyy = data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East")
    bos = data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East")
    state.upsert_teams([nyy, bos])
    season = datetime.now(UTC).year
    state.upsert_team_season(data.TeamSeasonStats(147, season, 30, 20, 250, 200, 50))
    state.upsert_team_season(data.TeamSeasonStats(111, season, 25, 25, 220, 220, 50))

    pred = baseball.predict_game("NYY", "BOS")
    assert pred is not None
    assert pred["source"] == "fallback"
    assert 0.0 < pred["home_win_prob"] < 1.0
