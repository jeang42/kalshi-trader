"""Tests for the lead-lag analysis module.

Tests use an isolated temp baseball_state.sqlite + book_archive.sqlite
seeded with hand-crafted plays + book snapshots so we exercise the
join logic deterministically.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from baseball import analysis, data, state


@pytest.fixture
def temp_state(tmp_path, monkeypatch):
    db = tmp_path / "baseball_test.sqlite"
    monkeypatch.setenv("BASEBALL_STATE_PATH", str(db))
    state.migrate()
    # Initialize live_plays schema (lives in baseball.live)
    from baseball.live import _ensure_live_schema
    _ensure_live_schema()
    return db


@pytest.fixture
def temp_book_archive(tmp_path, monkeypatch):
    """Build a temp book_archive.sqlite + monkey-patch the
    BOOK_ARCHIVE_PATH constant in analysis to use it."""
    db = tmp_path / "book_test.sqlite"
    # Match the real schema
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE book_snapshots (
                venue TEXT, market_id TEXT, token_id TEXT,
                snapshot_ts INTEGER, book_json TEXT,
                UNIQUE(venue, market_id, token_id, snapshot_ts)
            );
        """)
    monkeypatch.setattr(analysis, "BOOK_ARCHIVE_PATH", str(db))
    return db


def _add_game(home_id: int, away_id: int, *, game_pk: int,
               game_date_utc_iso: str, status: str = "Scheduled") -> None:
    with sqlite3.connect(state._state_path()) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO games (game_pk, game_date, status,
                home_team_id, away_team_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (game_pk, game_date_utc_iso, status,
              home_id, away_id, game_date_utc_iso))


def _add_play(game_pk: int, play_id: int, ts_iso: str,
               event_type: str = "Strikeout") -> None:
    with sqlite3.connect(state._state_path()) as conn:
        conn.execute("""
            INSERT INTO live_plays (game_pk, play_id, ts, event_type,
                                      fetched_at)
            VALUES (?, ?, ?, ?, ?)
        """, (game_pk, play_id, ts_iso, event_type, ts_iso))


def _add_book(db_path, ticker: str, ts: int,
               bid_price: float = 0.50, ask_price: float = 0.52) -> None:
    book_json = json.dumps([
        {"side": "bid", "price": bid_price, "size": 100.0},
        {"side": "ask", "price": ask_price, "size": 100.0},
    ])
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO book_snapshots
            (venue, market_id, token_id, snapshot_ts, book_json)
            VALUES ('kalshi', ?, ?, ?, ?)
        """, (ticker, ticker, ts, book_json))


# ─── tickers_for_game ─────────────────────────────────────────────────────


def test_tickers_for_game_no_game(temp_state, temp_book_archive):
    assert analysis.tickers_for_game(99999) == []


def test_tickers_for_game_no_book_data(temp_state, temp_book_archive):
    """Game in state but no book snapshots → empty list."""
    state.upsert_teams([
        data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East"),
        data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East"),
    ])
    _add_game(home_id=147, away_id=111, game_pk=1,
              game_date_utc_iso="2026-05-23T23:05:00+00:00")
    assert analysis.tickers_for_game(1) == []


def test_tickers_for_game_matches_eastern_date(temp_state, temp_book_archive):
    """Kalshi tickers encode Eastern date+time, not UTC. A 23:05 UTC
    game on 2026-05-23 = 19:05 EDT same date → ticker prefix MAY23.
    A 03:00 UTC game on 2026-05-23 = 23:00 EDT the day before →
    ticker prefix MAY22."""
    state.upsert_teams([
        data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East"),
        data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East"),
    ])
    # UTC 23:05 May 23 = EDT 19:05 May 23
    _add_game(home_id=147, away_id=111, game_pk=2,
              game_date_utc_iso="2026-05-23T23:05:00+00:00")
    _add_book(temp_book_archive,
              "KXMLBGAME-26MAY231905BOSNYY-NYY", ts=1779999999)
    _add_book(temp_book_archive,
              "KXMLBGAME-26MAY231905BOSNYY-BOS", ts=1779999999)
    out = analysis.tickers_for_game(2)
    assert sorted(out) == [
        "KXMLBGAME-26MAY231905BOSNYY-BOS",
        "KXMLBGAME-26MAY231905BOSNYY-NYY",
    ]


def test_tickers_for_game_filters_by_matchup(temp_state, temp_book_archive):
    """Date matches but matchup doesn't → ticker not returned."""
    state.upsert_teams([
        data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East"),
        data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East"),
        data.Team(141, "TOR", "Blue Jays", "Blue Jays", "AL", "AL East"),
    ])
    _add_game(home_id=147, away_id=111, game_pk=3,
              game_date_utc_iso="2026-05-23T23:05:00+00:00")
    # Add a BOSNYY ticker (matches) AND a TORNYY ticker (wrong matchup)
    _add_book(temp_book_archive,
              "KXMLBGAME-26MAY231905BOSNYY-NYY", ts=1779999999)
    _add_book(temp_book_archive,
              "KXMLBGAME-26MAY231905TORNYY-NYY", ts=1779999999)
    out = analysis.tickers_for_game(3)
    assert out == ["KXMLBGAME-26MAY231905BOSNYY-NYY"]


# ─── book_around_play ─────────────────────────────────────────────────────


def test_book_around_play_no_play(temp_state, temp_book_archive):
    """Returns [] when the play doesn't exist in live_plays."""
    assert analysis.book_around_play(99, 99) == []


def test_book_around_play_collects_window(temp_state, temp_book_archive):
    state.upsert_teams([
        data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East"),
        data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East"),
    ])
    _add_game(home_id=147, away_id=111, game_pk=10,
              game_date_utc_iso="2026-05-23T23:05:00+00:00")
    _add_play(game_pk=10, play_id=5,
               ts_iso="2026-05-23T23:30:00+00:00",
               event_type="Home Run")
    # Play happens at ts = 1779999000 (synthetic — uses datetime conversion)
    from datetime import datetime
    play_unix = int(datetime.fromisoformat(
        "2026-05-23T23:30:00+00:00").timestamp())
    # Book points 2 min before, 1 min after, 8 min after (outside 5min window)
    _add_book(temp_book_archive, "KXMLBGAME-26MAY231905BOSNYY-NYY",
              ts=play_unix - 120, bid_price=0.50, ask_price=0.52)
    _add_book(temp_book_archive, "KXMLBGAME-26MAY231905BOSNYY-NYY",
              ts=play_unix + 60, bid_price=0.58, ask_price=0.60)
    _add_book(temp_book_archive, "KXMLBGAME-26MAY231905BOSNYY-NYY",
              ts=play_unix + 480, bid_price=0.65, ask_price=0.67)

    points = analysis.book_around_play(10, 5, window_minutes=5.0)
    # The far-out one (8 min) is outside 5-min window
    assert len(points) == 2
    # Verify signed seconds_from_play
    sigs = sorted(p.seconds_from_play for p in points)
    assert sigs == [-120, 60]
    # Midpoint computed
    assert points[0].mid is not None


# ─── reaction_summary ─────────────────────────────────────────────────────


def test_reaction_summary_no_data_returns_none_pct(temp_state, temp_book_archive):
    """Small sample size → reaction_pct should be None (not falsely 0%)."""
    out = analysis.reaction_summary("Home Run")
    assert out["n_plays"] == 0
    assert out["reaction_pct"] is None
    assert out["median_reaction_seconds"] is None


def test_reaction_summary_small_sample_returns_none_pct(temp_state, temp_book_archive):
    """4 plays with paired books should still return None — below
    threshold for honest stats."""
    state.upsert_teams([
        data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East"),
        data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East"),
    ])
    _add_game(home_id=147, away_id=111, game_pk=20,
              game_date_utc_iso="2026-05-23T23:05:00+00:00")
    play_unix = int(datetime.fromisoformat(
        "2026-05-23T23:30:00+00:00").timestamp())
    for i in range(4):
        _add_play(game_pk=20, play_id=i,
                   ts_iso="2026-05-23T23:30:00+00:00",
                   event_type="Home Run")
    _add_book(temp_book_archive, "KXMLBGAME-26MAY231905BOSNYY-NYY",
              ts=play_unix - 60, bid_price=0.50, ask_price=0.52)
    _add_book(temp_book_archive, "KXMLBGAME-26MAY231905BOSNYY-NYY",
              ts=play_unix + 60, bid_price=0.50, ask_price=0.52)
    # Pre-attribution-fix behavior: pass require_score_change=False
    # to count every Home Run play regardless of whether it changed
    # the score. The default now requires a score change.
    out = analysis.reaction_summary("Home Run", require_score_change=False)
    assert out["n_plays"] == 4
    # n_played_with_book < 5 → reaction_pct stays None
    assert out["reaction_pct"] is None


def test_reaction_summary_detects_real_movement(temp_state, temp_book_archive):
    """5 plays where the book moves ≥2% post-play → reaction_pct=1.0."""
    state.upsert_teams([
        data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East"),
        data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East"),
    ])
    _add_game(home_id=147, away_id=111, game_pk=30,
              game_date_utc_iso="2026-05-23T23:05:00+00:00")
    play_unix = int(datetime.fromisoformat(
        "2026-05-23T23:30:00+00:00").timestamp())
    # 5 distinct plays at slightly different times (so each one's
    # book window is distinct)
    for i in range(5):
        offset = i * 600  # 10 min apart
        ts_iso = (datetime.fromisoformat("2026-05-23T23:30:00+00:00")
                   + timedelta(seconds=offset)).isoformat()
        _add_play(game_pk=30, play_id=i, ts_iso=ts_iso, event_type="Home Run")
        # Book at -1min: mid 0.50; at +30s: mid 0.55 (10% move)
        _add_book(temp_book_archive, "KXMLBGAME-26MAY231905BOSNYY-NYY",
                  ts=play_unix + offset - 60, bid_price=0.49, ask_price=0.51)
        _add_book(temp_book_archive, "KXMLBGAME-26MAY231905BOSNYY-NYY",
                  ts=play_unix + offset + 30, bid_price=0.54, ask_price=0.56)
    # Pass require_score_change=False; the test plays don't set scores
    # so the default attribution filter would exclude all of them.
    out = analysis.reaction_summary("Home Run", min_pct_change=0.02,
                                      require_score_change=False)
    assert out["n_played_with_book"] == 5
    assert out["reaction_pct"] == 1.0
    assert out["median_reaction_seconds"] == 30.0


# ─── coverage_summary ─────────────────────────────────────────────────────


def test_coverage_summary_empty(temp_state, temp_book_archive):
    out = analysis.coverage_summary()
    assert out["total_plays"] == 0
    assert out["total_mlb_book_snapshots"] == 0
    assert out["book_collection_started_ts"] is None


def test_score_changing_plays_filters_non_scoring(temp_state):
    """Only plays where the score changed (vs prior play) should pass."""
    state.upsert_teams([
        data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East"),
        data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East"),
    ])
    _add_game(home_id=147, away_id=111, game_pk=50,
              game_date_utc_iso="2026-05-23T23:05:00+00:00")

    # Play sequence with explicit score progression
    seq = [
        # (play_id, event_type, home_score, away_score)
        (1, "Single",       0, 0),  # prior None → no prev, can't classify
        (2, "Mound Visit",  0, 0),  # no score change vs play 1
        (3, "Home Run",     0, 1),  # SCORE CHANGE (away +1)
        (4, "Mound Visit",  0, 1),  # no score change vs play 3
        (5, "Pitching Substitution", 0, 1),  # no score change
        (6, "Single",       0, 1),  # no score change
        (7, "Sac Fly",      1, 1),  # SCORE CHANGE (home +1)
        (8, "Strikeout",    1, 1),  # no score change
    ]
    import sqlite3
    with sqlite3.connect(state._state_path()) as conn:
        for pid, et, hs, as_ in seq:
            conn.execute("""
                INSERT INTO live_plays (game_pk, play_id, ts, event_type,
                                          home_score_after, away_score_after,
                                          fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (50, pid, f"2026-05-23T23:1{pid}:00+00:00", et, hs, as_,
                  "2026-05-23T23:00:00+00:00"))
        conn.commit()

    from baseball.analysis import _score_changing_plays_of_type
    hrs = _score_changing_plays_of_type("Home Run")
    assert (50, 3) in hrs
    assert len(hrs) == 1

    # Mound Visits should NOT pass the score-change filter
    assert _score_changing_plays_of_type("Mound Visit") == []

    # Sac Fly DID change the score
    assert _score_changing_plays_of_type("Sac Fly") == [(50, 7)]


def test_reaction_summary_default_requires_score_change(temp_state, temp_book_archive):
    """With default require_score_change=True, mound visits get 0
    even if they sit near book moves. Otherwise they'd inflate the
    sample with spurious matches."""
    state.upsert_teams([
        data.Team(147, "NYY", "Yankees", "Yankees", "AL", "AL East"),
        data.Team(111, "BOS", "Red Sox", "Red Sox", "AL", "AL East"),
    ])
    _add_game(home_id=147, away_id=111, game_pk=51,
              game_date_utc_iso="2026-05-23T23:05:00+00:00")
    # 3 Mound Visits, none with score changes
    import sqlite3
    with sqlite3.connect(state._state_path()) as conn:
        for pid in range(1, 4):
            conn.execute("""
                INSERT INTO live_plays (game_pk, play_id, ts, event_type,
                                          home_score_after, away_score_after,
                                          fetched_at)
                VALUES (?, ?, ?, 'Mound Visit', 0, 0, ?)
            """, (51, pid, f"2026-05-23T23:1{pid}:00+00:00",
                  "2026-05-23T23:00:00+00:00"))
        conn.commit()

    out = analysis.reaction_summary("Mound Visit")
    assert out["n_plays"] == 0  # filtered out by score-change requirement

    # Opting out of the filter shows them
    out2 = analysis.reaction_summary("Mound Visit", require_score_change=False)
    assert out2["n_plays"] == 3


def test_parse_situation_real_blob():
    """Decode a representative MLB feed/live play blob."""
    raw = {
        "about": {"inning": 9, "halfInning": "bottom",
                   "captivatingIndex": 99},
        "result": {"event": "Home Run", "homeScore": 4, "awayScore": 3},
        "count": {"outs": 2},
        "matchup": {"postOnFirst": {"id": 1}, "postOnSecond": None,
                     "postOnThird": {"id": 2}},
    }
    import json as _json
    sit = analysis.parse_situation(_json.dumps(raw))
    assert sit.inning == 9
    assert sit.half_inning == "bottom"
    assert sit.outs_after == 2
    assert sit.bases == (True, False, True)  # 1st and 3rd occupied
    assert sit.runners_on == 2
    assert sit.risp is True  # runner on 3rd
    assert not sit.bases_loaded
    assert sit.score_diff == 1  # home leads by 1
    assert sit.is_late
    assert sit.is_close
    assert sit.is_late_and_close
    assert not sit.is_garbage_time
    assert sit.base_out_state == "13_2outs"


def test_parse_situation_empty_robust():
    """All-None blob doesn't crash."""
    sit = analysis.parse_situation(None)
    assert sit.inning is None
    assert sit.bases == (False, False, False)


def test_parse_situation_bases_loaded():
    raw = {"about": {}, "result": {}, "count": {"outs": 1},
            "matchup": {"postOnFirst": True, "postOnSecond": True,
                         "postOnThird": True}}
    import json as _json
    sit = analysis.parse_situation(_json.dumps(raw))
    assert sit.bases_loaded
    assert sit.runners_on == 3
    assert sit.base_out_state == "123_1outs"


def test_late_and_close_with_runner_on_base():
    """Score diff > 1 but tying run on base = clutch."""
    raw = {"about": {"inning": 8, "halfInning": "top"},
            "result": {"homeScore": 5, "awayScore": 2},
            "count": {"outs": 0},
            "matchup": {"postOnFirst": True, "postOnSecond": True,
                         "postOnThird": None}}
    import json as _json
    sit = analysis.parse_situation(_json.dumps(raw))
    # diff=3, runners=2 → tying run on base (diff <= runners_on)
    assert sit.is_late_and_close


def test_garbage_time_classification():
    raw = {"about": {"inning": 9, "halfInning": "bottom"},
            "result": {"homeScore": 10, "awayScore": 1},
            "count": {"outs": 0},
            "matchup": {}}
    import json as _json
    sit = analysis.parse_situation(_json.dumps(raw))
    assert sit.is_garbage_time
    assert not sit.is_late_and_close


def test_coverage_summary_counts(temp_state, temp_book_archive):
    _add_play(game_pk=1, play_id=1, ts_iso="2026-05-23T23:30:00+00:00")
    _add_play(game_pk=1, play_id=2, ts_iso="2026-05-23T23:31:00+00:00")
    _add_book(temp_book_archive, "KXMLBGAME-26MAY231905XYY-YY", ts=100)
    out = analysis.coverage_summary()
    assert out["total_plays"] == 2
    assert out["total_mlb_book_snapshots"] == 1
    assert out["book_collection_started_ts"] == 100
