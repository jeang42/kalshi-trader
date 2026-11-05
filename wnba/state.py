"""Persistent state for the WNBA sidecar.

SQLite store at ``data/shared/wnba_state.sqlite``. WAL +
``synchronous=NORMAL`` per the sidecar protocol's HDD-friendly default.
Mirrors ``baseball/state.py`` but trimmed to what WNBA needs — no
pitchers/lineups/bullpens; basketball has none of that machinery.

Tables
------
* ``teams``        — the 15 WNBA teams (ESPN ids; refreshed weekly).
* ``team_season``  — per-team season points-for/against + record snapshot.
* ``games``        — schedule + results, keyed on ESPN's string event_id.
* ``predictions``  — model win-prob per game + actual result (for Brier).
* ``activity``     — bounded ring of runner events for the control plane.
* ``meta``         — schema version, last refresh, source URLs.

All timestamps are ISO-8601 UTC strings. ``game_pk`` analogue here is
``event_id`` (TEXT) because ESPN ids are strings, not ints.

Env override: ``WNBA_STATE_PATH``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wnba.data import GameSchedule, Team, TeamSeasonStats

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DEFAULT_PATH = Path("data/shared/wnba_state.sqlite")


def _state_path() -> Path:
    """Lazy path resolution — checked on every connection so test
    monkeypatches of the env var take effect without re-importing."""
    return Path(os.environ.get("WNBA_STATE_PATH", str(DEFAULT_PATH)))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        conn.close()


def migrate() -> None:
    """Idempotent schema setup. Called from the runner's startup; safe to
    call from tests or anywhere else."""
    with _connect() as conn:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        if v >= SCHEMA_VERSION:
            return
        log.info("wnba: migrating schema %d → %d", v, SCHEMA_VERSION)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS teams (
                id            INTEGER PRIMARY KEY,
                abbreviation  TEXT NOT NULL,
                name          TEXT NOT NULL,
                short_name    TEXT NOT NULL,
                location      TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_season (
                team_id              INTEGER NOT NULL,
                season               INTEGER NOT NULL,
                wins                 INTEGER NOT NULL,
                losses               INTEGER NOT NULL,
                points_for           INTEGER NOT NULL,
                points_against       INTEGER NOT NULL,
                games_played         INTEGER NOT NULL,
                win_pct              REAL NOT NULL,
                point_diff_per_game  REAL NOT NULL,
                updated_at           TEXT NOT NULL,
                PRIMARY KEY (team_id, season)
            );

            CREATE TABLE IF NOT EXISTS games (
                event_id      TEXT PRIMARY KEY,
                game_date     TEXT NOT NULL,
                status        TEXT NOT NULL,
                home_team_id  INTEGER NOT NULL,
                away_team_id  INTEGER NOT NULL,
                home_score    INTEGER,
                away_score    INTEGER,
                updated_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS games_by_date ON games(game_date);

            CREATE TABLE IF NOT EXISTS predictions (
                event_id       TEXT PRIMARY KEY,
                computed_at    TEXT NOT NULL,
                home_win_prob  REAL NOT NULL,
                home_team      TEXT NOT NULL,
                away_team      TEXT NOT NULL,
                model_version  TEXT NOT NULL,
                inputs         TEXT,
                actual_winner  TEXT
            );

            CREATE TABLE IF NOT EXISTS activity (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                kind        TEXT NOT NULL,
                message     TEXT NOT NULL,
                detail_json TEXT
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# ─── Upserts ─────────────────────────────────────────────────────────────


def upsert_teams(teams: list[Team]) -> None:
    with _connect() as conn:
        for t in teams:
            conn.execute("""
                INSERT INTO teams (id, abbreviation, name, short_name,
                                   location, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    abbreviation=excluded.abbreviation, name=excluded.name,
                    short_name=excluded.short_name, location=excluded.location,
                    updated_at=excluded.updated_at
            """, (t.id, t.abbreviation, t.name, t.short_name, t.location, _now()))


def upsert_team_season(stats: TeamSeasonStats) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO team_season (team_id, season, wins, losses,
                points_for, points_against, games_played, win_pct,
                point_diff_per_game, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_id, season) DO UPDATE SET
                wins=excluded.wins, losses=excluded.losses,
                points_for=excluded.points_for,
                points_against=excluded.points_against,
                games_played=excluded.games_played, win_pct=excluded.win_pct,
                point_diff_per_game=excluded.point_diff_per_game,
                updated_at=excluded.updated_at
        """, (stats.team_id, stats.season, stats.wins, stats.losses,
              stats.points_for, stats.points_against, stats.games_played,
              stats.win_pct, stats.point_diff_per_game, _now()))


def upsert_game(g: GameSchedule) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO games (event_id, game_date, status, home_team_id,
                               away_team_id, home_score, away_score, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                game_date=excluded.game_date, status=excluded.status,
                home_team_id=excluded.home_team_id,
                away_team_id=excluded.away_team_id,
                home_score=excluded.home_score, away_score=excluded.away_score,
                updated_at=excluded.updated_at
        """, (g.event_id, g.game_date.isoformat(), g.status, g.home.id,
              g.away.id, g.home_score, g.away_score, _now()))


def record_prediction(event_id: str, home_win_prob: float, home: str,
                      away: str, model_version: str,
                      inputs: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO predictions (event_id, computed_at, home_win_prob,
                home_team, away_team, model_version, inputs)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                computed_at=excluded.computed_at,
                home_win_prob=excluded.home_win_prob,
                model_version=excluded.model_version, inputs=excluded.inputs
        """, (event_id, _now(), float(home_win_prob), home, away,
              model_version, json.dumps(inputs, default=str)))


def settle_prediction(event_id: str, actual_winner: str) -> None:
    """Backfill ``actual_winner`` for a game's prediction once final."""
    with _connect() as conn:
        conn.execute("""
            UPDATE predictions SET actual_winner = ?
            WHERE event_id = ? AND actual_winner IS NULL
        """, (actual_winner, event_id))


def log_activity(kind: str, message: str,
                 detail: dict[str, Any] | None = None) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO activity (ts, kind, message, detail_json)
            VALUES (?, ?, ?, ?)
        """, (_now(), kind, message, json.dumps(detail) if detail else None))
        # bounded ring — keep last 1000
        conn.execute("""
            DELETE FROM activity WHERE id NOT IN
                (SELECT id FROM activity ORDER BY id DESC LIMIT 1000)
        """)


def set_meta(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))


def get_meta(key: str) -> str | None:
    with _connect() as conn:
        r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None


# ─── Readers ─────────────────────────────────────────────────────────────


def get_teams() -> list[dict[str, Any]]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM teams ORDER BY abbreviation").fetchall()]


def get_team_season(team_id: int, season: int) -> dict[str, Any] | None:
    with _connect() as conn:
        r = conn.execute(
            "SELECT * FROM team_season WHERE team_id=? AND season=?",
            (team_id, season)).fetchone()
        return dict(r) if r else None


def get_all_team_seasons(season: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT ts.*, t.abbreviation, t.name, t.short_name "
            "FROM team_season ts JOIN teams t ON ts.team_id = t.id "
            "WHERE ts.season=?", (season,)).fetchall()]


def get_games_for_date(date_iso_prefix: str) -> list[dict[str, Any]]:
    """Games whose game_date starts with ``date_iso_prefix`` (YYYY-MM-DD),
    joined to team abbreviations."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT g.*, ht.abbreviation AS home_abbr, at.abbreviation AS away_abbr
            FROM games g
            JOIN teams ht ON g.home_team_id = ht.id
            JOIN teams at ON g.away_team_id = at.id
            WHERE g.game_date LIKE ?
            ORDER BY g.game_date
        """, (date_iso_prefix + "%",)).fetchall()]


def get_games_for_eastern_date(date_iso: str) -> list[dict[str, Any]]:
    """Games whose start time falls on ``date_iso`` (YYYY-MM-DD) in **US
    Eastern** time. Kalshi tickers encode the date in Eastern, but we store
    UTC, so a late-ET game (e.g. 8pm ET → next-day UTC) would be missed by
    a naive UTC-prefix match. We scan a ±1-day UTC window and filter by the
    Eastern calendar date in Python. See docs/sports-model-playbook.md §2
    (the timezone gotcha)."""
    from datetime import date as _date
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo
    try:
        target = _date.fromisoformat(date_iso)
    except ValueError:
        return []
    eastern = ZoneInfo("America/New_York")
    lo = (target - _td(days=1)).isoformat()
    hi = (target + _td(days=1)).isoformat()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT g.*, ht.abbreviation AS home_abbr, at.abbreviation AS away_abbr
            FROM games g
            JOIN teams ht ON g.home_team_id = ht.id
            JOIN teams at ON g.away_team_id = at.id
            WHERE g.game_date >= ? AND g.game_date < ?
            ORDER BY g.game_date
        """, (lo, hi + "T23:59:59")).fetchall()
    out = []
    for r in rows:
        try:
            dt = _dt.fromisoformat(r["game_date"])
        except (ValueError, TypeError):
            continue
        if dt.astimezone(eastern).date() == target:
            out.append(dict(r))
    return out


def get_game(event_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM games WHERE event_id=?",
                         (event_id,)).fetchone()
        return dict(r) if r else None


def get_game_with_teams(event_id: str) -> dict[str, Any] | None:
    """One game joined to team abbreviations + names — for the detail page."""
    with _connect() as conn:
        r = conn.execute("""
            SELECT g.*, ht.abbreviation AS home_abbr, ht.name AS home_name,
                   at.abbreviation AS away_abbr, at.name AS away_name
            FROM games g
            JOIN teams ht ON g.home_team_id = ht.id
            JOIN teams at ON g.away_team_id = at.id
            WHERE g.event_id = ?
        """, (event_id,)).fetchone()
        return dict(r) if r else None


def get_recent_final_games(limit: int = 40) -> list[dict[str, Any]]:
    """Most-recent finished games (status 'post'), newest first, joined to
    team abbrevs + any stored prediction's home_win_prob. For the Recent
    Games browser."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT g.*, ht.abbreviation AS home_abbr, at.abbreviation AS away_abbr,
                   p.home_win_prob AS home_win_prob
            FROM games g
            JOIN teams ht ON g.home_team_id = ht.id
            JOIN teams at ON g.away_team_id = at.id
            LEFT JOIN predictions p ON p.event_id = g.event_id
            WHERE g.status = 'post'
            ORDER BY g.game_date DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_predictions_for_game(event_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM predictions WHERE event_id=?", (event_id,)).fetchall()]


def get_recent_activity(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def get_brier_summary(season: int) -> dict[str, Any]:
    """Rolling Brier over settled predictions for the season. The feedback
    signal that drives model changes (see playbook §3)."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT home_win_prob, actual_winner, home_team
            FROM predictions WHERE actual_winner IS NOT NULL
        """).fetchall()
    n = 0
    total = 0.0
    for r in rows:
        actual = 1.0 if r["actual_winner"] == r["home_team"] else 0.0
        total += (r["home_win_prob"] - actual) ** 2
        n += 1
    return {"n": n, "brier": (total / n) if n else None}
