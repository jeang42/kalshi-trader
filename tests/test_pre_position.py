"""Tests for pre_position._current_game_states() live-game detection.

Regression coverage for the status='Scheduled' bug: the MLB feed leaves
games.status stuck at 'Pre-Game'/'Warmup' (it never sets 'In Progress'),
so the old `WHERE g.status = 'Scheduled'` gate matched nothing once a game
started and the live-baseball daemons (scalpers + pre_position) saw zero
live games. The fix keys "live" off a recent live_plays row instead, and
only uses status to EXCLUDE finished games (Final/Game Over are synced
reliably). These tests pin that behavior, including the 20-minute staleness
window (which requires datetime()-normalized timestamp comparison because
live_plays.ts is ISO-8601 with a 'T'/'Z' that string-compares wrong against
SQLite's space-separated datetime('now')).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from baseball import live, state
from pm.arms import pre_position


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    db = tmp_path / "baseball_test.sqlite"
    monkeypatch.setenv("BASEBALL_STATE_PATH", str(db))
    state.migrate()
    live._ensure_live_schema()
    return db


def _iso(dt: datetime) -> str:
    """ISO-8601 with 'Z', matching baseball.live's stored ts format
    (e.g. '2026-05-21T18:06:11Z')."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_team(conn, team_id, abbr):
    conn.execute(
        "INSERT INTO teams (id, abbreviation, name, short_name, league, "
        "division, updated_at) VALUES (?,?,?,?,?,?,?)",
        (team_id, abbr, f"{abbr} Club", abbr, "AL", "East", _iso(datetime.now(UTC))),
    )


def _add_game(conn, game_pk, home_id, away_id, status):
    conn.execute(
        "INSERT INTO games (game_pk, game_date, status, home_team_id, "
        "away_team_id, updated_at) VALUES (?,?,?,?,?,?)",
        (game_pk, _iso(datetime.now(UTC)), status, home_id, away_id,
         _iso(datetime.now(UTC))),
    )


def _add_play(conn, game_pk, play_id, *, inning, half, age_minutes):
    ts = _iso(datetime.now(UTC) - timedelta(minutes=age_minutes))
    raw = json.dumps({"about": {"inning": inning, "halfInning": half, "outs": 1}})
    conn.execute(
        "INSERT INTO live_plays (game_pk, play_id, ts, half_inning, inning, "
        "event_type, description, home_score_after, away_score_after, "
        "home_win_prob, raw_json, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (game_pk, play_id, ts, half, inning, "Single", "a hit",
         1, 0, 0.5, raw, ts),
    )


@pytest.fixture
def conn(temp_state):
    c = sqlite3.connect(temp_state)
    # Two teams used by every game in these tests.
    _add_team(c, 1, "HOM")
    _add_team(c, 2, "AWY")
    c.commit()
    yield c
    c.close()


def test_recent_play_nonfinal_is_live(conn):
    """A game with a play in the last 20 min and a non-final status is
    returned as live — the core path the old status gate broke."""
    _add_game(conn, 100, 1, 2, "In Progress")
    _add_play(conn, 100, 1, inning=5, half="top", age_minutes=1)
    conn.commit()

    states = pre_position._current_game_states()

    assert [s.game_pk for s in states] == [100]
    s = states[0]
    assert s.inning == 5
    assert s.home_abbr == "HOM"
    assert s.away_abbr == "AWY"


def test_pregame_status_with_recent_play_is_live(conn):
    """REGRESSION: a game whose status is still 'Pre-Game' (the stale value
    the MLB feed leaves) but which has a recent play IS live. The old
    `status = 'Scheduled'` gate wrongly excluded exactly this case."""
    _add_game(conn, 101, 1, 2, "Pre-Game")
    _add_play(conn, 101, 1, inning=2, half="bottom", age_minutes=2)
    conn.commit()

    states = pre_position._current_game_states()

    assert [s.game_pk for s in states] == [101]


def test_warmup_status_with_recent_play_is_live(conn):
    """Same regression for 'Warmup' — the other stale value seen in prod."""
    _add_game(conn, 102, 1, 2, "Warmup")
    _add_play(conn, 102, 1, inning=1, half="top", age_minutes=0)
    conn.commit()

    states = pre_position._current_game_states()

    assert [s.game_pk for s in states] == [102]


def test_stale_play_is_excluded(conn):
    """A game whose only play is older than 20 min is NOT live. This pins
    the datetime()-normalized comparison: live_plays.ts is ISO-8601 with a
    'T'/'Z', so a naive string compare against datetime('now','-20 minutes')
    would let any same-date play through regardless of age."""
    _add_game(conn, 103, 1, 2, "In Progress")
    _add_play(conn, 103, 1, inning=7, half="top", age_minutes=25)
    conn.commit()

    states = pre_position._current_game_states()

    assert states == []


def test_final_game_with_recent_play_is_excluded(conn):
    """A Final game is excluded even with a recent play — status IS reliable
    for finished games, and we must not open into an already-resolved market."""
    _add_game(conn, 104, 1, 2, "Final")
    _add_play(conn, 104, 1, inning=9, half="bottom", age_minutes=1)
    conn.commit()

    states = pre_position._current_game_states()

    assert states == []


def test_no_plays_is_excluded(conn):
    """A scheduled game with no plays at all is not live."""
    _add_game(conn, 105, 1, 2, "Pre-Game")
    conn.commit()

    states = pre_position._current_game_states()

    assert states == []


def test_mixed_slate_returns_only_live(conn):
    """End-to-end: a realistic mix returns exactly the genuinely-live games."""
    _add_game(conn, 200, 1, 2, "In Progress")   # live
    _add_play(conn, 200, 1, inning=4, half="top", age_minutes=1)
    _add_game(conn, 201, 1, 2, "Pre-Game")       # live (stale status)
    _add_play(conn, 201, 1, inning=1, half="bottom", age_minutes=3)
    _add_game(conn, 202, 1, 2, "Final")          # done
    _add_play(conn, 202, 1, inning=9, half="bottom", age_minutes=2)
    _add_game(conn, 203, 1, 2, "In Progress")    # stalled (old play)
    _add_play(conn, 203, 1, inning=6, half="top", age_minutes=40)
    conn.commit()

    states = pre_position._current_game_states()

    assert sorted(s.game_pk for s in states) == [200, 201]
