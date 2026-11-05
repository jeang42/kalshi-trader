"""Tests for the live collector — schema, dedup, extraction.

No network: ``fetch_live_feed`` and the in-progress discovery are mocked.
The extraction logic operates on a hand-built MLB-shaped JSON blob so
we exercise the real parsing path without hitting the API."""

from __future__ import annotations

import pytest

from baseball import live, state


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    db = tmp_path / "baseball_test.sqlite"
    monkeypatch.setenv("BASEBALL_STATE_PATH", str(db))
    state.migrate()
    live._ensure_live_schema()
    return db


@pytest.fixture
def fake_feed():
    """Minimal MLB-shaped feed with two plays."""
    return {
        "gameData": {"status": {"detailedState": "In Progress"}},
        "liveData": {"plays": {"allPlays": [
                {
                    "about": {
                        "atBatIndex": 0, "halfInning": "top", "inning": 1,
                        "startTime": "2026-05-21T18:00:00Z",
                        "endTime":   "2026-05-21T18:03:21Z",
                        "homeTeamWinProbability": 50.2,
                    },
                    "result": {
                        "event": "Strikeout",
                        "description": "Mookie Betts strikes out swinging.",
                        "homeScore": 0, "awayScore": 0,
                    },
                },
                {
                    "about": {
                        "atBatIndex": 1, "halfInning": "top", "inning": 1,
                        "endTime": "2026-05-21T18:06:11Z",
                        "homeTeamWinProbability": 50.5,
                    },
                    "result": {
                        "event": "Home Run",
                        "description": "Freddie Freeman homers (12) on a fly ball to right.",
                        "homeScore": 0, "awayScore": 1,
                    },
                },
            ]
        }},
    }


def test_extract_plays_basic(fake_feed):
    plays = live._extract_plays(fake_feed, game_pk=123)
    assert len(plays) == 2
    assert plays[0]["event_type"] == "Strikeout"
    assert plays[1]["event_type"] == "Home Run"
    assert plays[1]["away_score_after"] == 1
    assert plays[0]["home_win_prob"] == 50.2
    assert plays[1]["raw"] is not None  # raw blob preserved


def test_extract_plays_skips_missing_atbat_index():
    feed = {"liveData": {"plays": {"allPlays": [
        {"about": {}, "result": {"event": "Walk"}},  # no atBatIndex
    ]}}}
    assert live._extract_plays(feed, 1) == []


def test_extract_plays_empty():
    assert live._extract_plays({}, 1) == []
    assert live._extract_plays({"liveData": {}}, 1) == []


def test_is_game_live_true():
    assert live._is_game_live({"gameData": {"status": {"detailedState": "In Progress"}}})


def test_is_game_live_false_for_final():
    assert not live._is_game_live({"gameData": {"status": {"detailedState": "Final"}}})


def test_is_game_live_scheduled_with_plays():
    """Pre-game transition: status still 'Scheduled' but plays exist."""
    feed = {"gameData": {"status": {"detailedState": "Scheduled"}},
            "liveData": {"plays": {"allPlays": [{"about": {"atBatIndex": 0}, "result": {}}]}}}
    assert live._is_game_live(feed)


def test_is_game_live_scheduled_no_plays():
    feed = {"gameData": {"status": {"detailedState": "Scheduled"}},
            "liveData": {"plays": {"allPlays": []}}}
    assert not live._is_game_live(feed)


def test_insert_and_dedup_plays(temp_state, fake_feed):
    """Same play_id should not double-insert."""
    plays = live._extract_plays(fake_feed, 100)
    for p in plays:
        live._insert_play(p)
    # Try again
    for p in plays:
        live._insert_play(p)
    rows = live.get_recent_plays(100)
    assert len(rows) == 2  # not 4


def test_last_play_id_advances(temp_state):
    """Highest persisted play_id should be tracked correctly."""
    assert live._last_play_id(99) == -1
    live._insert_play({
        "game_pk": 99, "play_id": 5, "ts": "2026-05-21T18:00:00Z",
        "half_inning": "top", "inning": 1, "event_type": "Single",
        "description": "x", "home_score_after": 0, "away_score_after": 1,
        "home_win_prob": None, "raw": {},
    })
    assert live._last_play_id(99) == 5
    live._insert_play({
        "game_pk": 99, "play_id": 10, "ts": "2026-05-21T18:05:00Z",
        "half_inning": "bottom", "inning": 1, "event_type": "Walk",
        "description": "y", "home_score_after": 0, "away_score_after": 1,
        "home_win_prob": None, "raw": {},
    })
    assert live._last_play_id(99) == 10


def test_get_recent_plays_chronological(temp_state):
    """Should return oldest-first (chronological)."""
    for pid in [3, 1, 2]:
        live._insert_play({
            "game_pk": 55, "play_id": pid, "ts": f"2026-05-21T18:0{pid}:00Z",
            "half_inning": "top", "inning": 1, "event_type": "x",
            "description": "", "home_score_after": 0, "away_score_after": 0,
            "home_win_prob": None, "raw": {},
        })
    rows = live.get_recent_plays(55, limit=10)
    play_ids = [r["play_id"] for r in rows]
    assert play_ids == [1, 2, 3]


def test_live_plays_summary_empty(temp_state):
    s = live.live_plays_summary()
    assert s == {"plays_last_24h": 0, "games_last_24h": 0, "total_plays": 0}


def test_live_plays_summary_counts(temp_state, fake_feed):
    plays = live._extract_plays(fake_feed, 200)
    for p in plays:
        live._insert_play(p)
    s = live.live_plays_summary()
    assert s["total_plays"] == 2
    assert s["plays_last_24h"] == 2
    assert s["games_last_24h"] == 1
