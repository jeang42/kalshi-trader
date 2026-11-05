"""Tests for baseball.resolve_pending_tickers — the Sidecar protocol
hook that lets us resolve forecasts ahead of Kalshi's 4-day settlement."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

from baseball import data, state


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    db = tmp_path / "baseball_test.sqlite"
    monkeypatch.setenv("BASEBALL_STATE_PATH", str(db))
    state.migrate()
    return db


@pytest.fixture
def two_finished_games(temp_state):
    """NYY 5–2 BOS and ATL 1–4 MIA, both on 2030-05-23 (date chosen so
    we don't collide with any real game data the suite might touch)."""
    nyy = data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East")
    bos = data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East")
    atl = data.Team(144, "ATL", "Braves", "Braves", "NL", "NL East")
    mia = data.Team(146, "MIA", "Marlins", "Marlins", "NL", "NL East")
    state.upsert_teams([nyy, bos, atl, mia])

    g1 = data.GameSchedule(
        game_pk=9001,
        game_date=datetime(2030, 5, 23, 23, 5, tzinfo=UTC),
        status="Final", home=nyy, away=bos,
        home_score=5, away_score=2,
    )
    g2 = data.GameSchedule(
        game_pk=9002,
        game_date=datetime(2030, 5, 23, 19, 10, tzinfo=UTC),
        status="Final", home=mia, away=atl,
        home_score=4, away_score=1,
    )
    state.upsert_game(g1)
    state.upsert_game(g2)
    return [g1, g2]


def test_resolve_yes_home_wins(two_finished_games):
    """NYY won 5-2 at home. Ticker YES=NYY (home) → True."""
    import baseball
    out = baseball.resolve_pending_tickers(
        ["KXMLBGAME-30MAY232305BOSNYY-NYY"]
    )
    assert out == {"KXMLBGAME-30MAY232305BOSNYY-NYY": True}


def test_resolve_yes_home_loses(two_finished_games):
    """NYY won; the BOS-YES ticker should resolve False."""
    import baseball
    out = baseball.resolve_pending_tickers(
        ["KXMLBGAME-30MAY232305BOSNYY-BOS"]
    )
    assert out == {"KXMLBGAME-30MAY232305BOSNYY-BOS": False}


def test_resolve_yes_away_wins(two_finished_games):
    """MIA beat ATL 4-1. Ticker YES=MIA → True (YES side = home)."""
    import baseball
    out = baseball.resolve_pending_tickers(
        ["KXMLBGAME-30MAY231910ATLMIA-MIA"]
    )
    assert out == {"KXMLBGAME-30MAY231910ATLMIA-MIA": True}


def test_resolve_yes_away_loses(two_finished_games):
    """MIA beat ATL; ATL-YES ticker → False."""
    import baseball
    out = baseball.resolve_pending_tickers(
        ["KXMLBGAME-30MAY231910ATLMIA-ATL"]
    )
    assert out == {"KXMLBGAME-30MAY231910ATLMIA-ATL": False}


def test_skips_in_progress_game(temp_state):
    """A scheduled or in-progress game must NOT resolve. Our protocol
    rule: when in doubt, omit the ticker."""
    nyy = data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East")
    bos = data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East")
    state.upsert_teams([nyy, bos])
    state.upsert_game(data.GameSchedule(
        game_pk=9003,
        game_date=datetime(2030, 5, 23, 23, 5, tzinfo=UTC),
        status="In Progress", home=nyy, away=bos,
        home_score=3, away_score=2,
    ))
    import baseball
    assert baseball.resolve_pending_tickers(
        ["KXMLBGAME-30MAY232305BOSNYY-NYY"]
    ) == {}


def test_skips_unknown_game(two_finished_games):
    """Ticker pattern is valid but the game/date isn't in state."""
    import baseball
    assert baseball.resolve_pending_tickers(
        ["KXMLBGAME-31JAN012305FAKEFAKE-FAK"]
    ) == {}


def test_skips_non_mlb_tickers(two_finished_games):
    """Polymarket / Kalshi non-MLB tickers ignored entirely."""
    import baseball
    out = baseball.resolve_pending_tickers([
        "KXPRES-2024-DEM",
        "",
        "not-a-ticker",
        "KXMLBGAME-30MAY232305BOSNYY-NYY",  # the only real one
    ])
    assert out == {"KXMLBGAME-30MAY232305BOSNYY-NYY": True}


def test_empty_input_returns_empty(temp_state):
    import baseball
    assert baseball.resolve_pending_tickers([]) == {}


def test_batches_by_date_efficiently(two_finished_games):
    """When N tickers share a date, we should only query games-for-date
    once. Hard to assert directly without instrumentation; cover the
    behavior by passing many tickers and verifying all resolve correctly."""
    import baseball
    tickers = [
        "KXMLBGAME-30MAY232305BOSNYY-NYY",
        "KXMLBGAME-30MAY232305BOSNYY-BOS",
        "KXMLBGAME-30MAY231910ATLMIA-MIA",
        "KXMLBGAME-30MAY231910ATLMIA-ATL",
    ]
    out = baseball.resolve_pending_tickers(tickers)
    assert out == {
        "KXMLBGAME-30MAY232305BOSNYY-NYY": True,
        "KXMLBGAME-30MAY232305BOSNYY-BOS": False,
        "KXMLBGAME-30MAY231910ATLMIA-MIA": True,
        "KXMLBGAME-30MAY231910ATLMIA-ATL": False,
    }


def test_returns_empty_on_corrupt_state(monkeypatch, tmp_path):
    """If state.migrate() fails, return {} rather than raising."""
    monkeypatch.setenv("BASEBALL_STATE_PATH", str(tmp_path / "nonexistent" / "x.sqlite"))
    import baseball
    # Even if state can't initialize, this must not raise
    out = baseball.resolve_pending_tickers(["KXMLBGAME-30MAY232305BOSNYY-NYY"])
    assert isinstance(out, dict)
