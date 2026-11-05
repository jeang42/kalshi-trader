"""Persistent state for the baseball sidecar.

SQLite store at ``data/shared/baseball_state.sqlite``. WAL +
``synchronous=NORMAL`` per the protocol's HDD-friendly default.

Tables
------
* ``teams``           — full roster of MLB teams; refreshed weekly
* ``team_season``     — per-team season stats, snapshot per refresh
* ``pitchers``        — pitcher season stats, snapshot per refresh
* ``games``           — schedule + results (game_pk is the natural key)
* ``predictions``     — model's win-probability output per game; kept
                        alongside the actual result so we can compute
                        Brier rolling-up over the season
* ``activity``        — bounded ring of runner events for the control plane
* ``meta``            — schema version, last refresh ts, source URLs

All timestamps stored as ISO-8601 UTC strings. Python dataclasses
serialize to JSON for any nested fields (raw API payloads we want
to keep around).

Why SQLite, not Parquet/Postgres
---------------------------------
SQLite handles WAL-mode multi-reader well, the dataset is small
(30 teams × ~180 days × handful of stats ≈ KB-scale, not GB), and
the rest of the project already standardizes on SQLite for sidecar
storage (`book_archive.sqlite`, `llm_cache.sqlite`). Postgres would
be more capability than this needs.
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
from typing import TYPE_CHECKING, Any

from baseball.data import GameSchedule, Pitcher, Team, TeamSeasonStats

if TYPE_CHECKING:  # resolves the "data.Batter" string annotations below
    from baseball import data

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4  # v4: predictions dedup — one row per game_pk

DEFAULT_PATH = Path("data/shared/baseball_state.sqlite")


def _state_path() -> Path:
    """Lazy path resolution — checked on every connection so test
    monkeypatches of the env var take effect without re-importing.
    Mirrors the pattern in ``portfolio.py``, ``calibration.py``, etc."""
    return Path(os.environ.get("BASEBALL_STATE_PATH", str(DEFAULT_PATH)))


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        conn.close()


def migrate() -> None:
    """Idempotent schema setup. Called from the runner's startup;
    safe to call from tests or anywhere else."""
    with _connect() as conn:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        if v >= SCHEMA_VERSION:
            return
        log.info("baseball: migrating schema %d → %d", v, SCHEMA_VERSION)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS teams (
                id            INTEGER PRIMARY KEY,
                abbreviation  TEXT NOT NULL,
                name          TEXT NOT NULL,
                short_name    TEXT NOT NULL,
                league        TEXT NOT NULL,
                division      TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_season (
                team_id            INTEGER NOT NULL,
                season             INTEGER NOT NULL,
                wins               INTEGER NOT NULL,
                losses             INTEGER NOT NULL,
                runs_scored        INTEGER NOT NULL,
                runs_allowed       INTEGER NOT NULL,
                games_played       INTEGER NOT NULL,
                win_pct            REAL NOT NULL,
                pythagorean        REAL NOT NULL,
                run_diff_per_game  REAL NOT NULL,
                updated_at         TEXT NOT NULL,
                PRIMARY KEY (team_id, season)
            );

            CREATE TABLE IF NOT EXISTS batters (
                id           INTEGER NOT NULL,
                season       INTEGER NOT NULL,
                name         TEXT NOT NULL,
                ops          REAL,
                avg          REAL,
                home_runs    INTEGER,
                at_bats      INTEGER,
                updated_at   TEXT NOT NULL,
                PRIMARY KEY (id, season)
            );

            CREATE TABLE IF NOT EXISTS lineups (
                game_pk      INTEGER NOT NULL,
                side         TEXT NOT NULL,
                player_ids   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                PRIMARY KEY (game_pk, side)
            );

            CREATE TABLE IF NOT EXISTS bullpens (
                team_id            INTEGER NOT NULL,
                season             INTEGER NOT NULL,
                era                REAL,
                whip               REAL,
                innings_pitched    REAL,
                strikeouts_per_9   REAL,
                updated_at         TEXT NOT NULL,
                PRIMARY KEY (team_id, season)
            );

            CREATE TABLE IF NOT EXISTS pitchers (
                id                  INTEGER PRIMARY KEY,
                name                TEXT NOT NULL,
                season              INTEGER NOT NULL,
                era                 REAL,
                whip                REAL,
                innings_pitched     REAL,
                strikeouts_per_9    REAL,
                updated_at          TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS games (
                game_pk         INTEGER PRIMARY KEY,
                game_date       TEXT NOT NULL,
                status          TEXT NOT NULL,
                home_team_id    INTEGER NOT NULL,
                away_team_id    INTEGER NOT NULL,
                home_pitcher_id INTEGER,
                away_pitcher_id INTEGER,
                home_score      INTEGER,
                away_score      INTEGER,
                updated_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS games_by_date ON games(game_date);

            CREATE TABLE IF NOT EXISTS predictions (
                game_pk           INTEGER PRIMARY KEY,
                computed_at       TEXT NOT NULL,
                home_win_prob     REAL NOT NULL,
                home_team         TEXT NOT NULL,
                away_team         TEXT NOT NULL,
                model_version     TEXT NOT NULL,
                inputs            TEXT NOT NULL,
                actual_winner     TEXT
            );

            CREATE TABLE IF NOT EXISTS activity (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                kind        TEXT NOT NULL,
                message     TEXT NOT NULL,
                detail_json TEXT
            );
            CREATE INDEX IF NOT EXISTS activity_by_ts ON activity(ts DESC);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # v4: deduplicate predictions — one row per game_pk.
        # The old PK was (game_pk, computed_at) which caused the runner
        # to insert a new row every cycle (~108 dupes per game). Rebuild
        # with game_pk as sole PK, keeping only the latest row per game.
        if v < 4:
            has_old_pk = conn.execute("""
                SELECT COUNT(*) FROM pragma_table_info('predictions')
                WHERE name = 'computed_at' AND pk > 0
            """).fetchone()[0]
            if has_old_pk:
                conn.executescript("""
                    CREATE TABLE predictions_new (
                        game_pk       INTEGER PRIMARY KEY,
                        computed_at   TEXT NOT NULL,
                        home_win_prob REAL NOT NULL,
                        home_team     TEXT NOT NULL,
                        away_team     TEXT NOT NULL,
                        model_version TEXT NOT NULL,
                        inputs        TEXT NOT NULL,
                        actual_winner TEXT
                    );
                    INSERT INTO predictions_new
                        SELECT game_pk, MAX(computed_at), home_win_prob,
                               home_team, away_team, model_version, inputs,
                               actual_winner
                        FROM predictions
                        GROUP BY game_pk
                        HAVING computed_at = MAX(computed_at);
                    DROP TABLE predictions;
                    ALTER TABLE predictions_new RENAME TO predictions;
                """)
                log.info("baseball: v4 migration — deduplicated predictions "
                         "table (game_pk is now sole PK)")

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# ─── Write helpers ────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def upsert_teams(teams: list[Team]) -> None:
    with _connect() as conn:
        for t in teams:
            conn.execute("""
                INSERT INTO teams (id, abbreviation, name, short_name, league, division, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    abbreviation=excluded.abbreviation,
                    name=excluded.name,
                    short_name=excluded.short_name,
                    league=excluded.league,
                    division=excluded.division,
                    updated_at=excluded.updated_at
            """, (t.id, t.abbreviation, t.name, t.short_name, t.league, t.division, _now()))


def upsert_team_season(stats: TeamSeasonStats) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO team_season (team_id, season, wins, losses, runs_scored,
                                       runs_allowed, games_played, win_pct,
                                       pythagorean, run_diff_per_game, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_id, season) DO UPDATE SET
                wins=excluded.wins, losses=excluded.losses,
                runs_scored=excluded.runs_scored,
                runs_allowed=excluded.runs_allowed,
                games_played=excluded.games_played,
                win_pct=excluded.win_pct,
                pythagorean=excluded.pythagorean,
                run_diff_per_game=excluded.run_diff_per_game,
                updated_at=excluded.updated_at
        """, (stats.team_id, stats.season, stats.wins, stats.losses,
              stats.runs_scored, stats.runs_allowed, stats.games_played,
              stats.win_pct, stats.pythagorean, stats.run_diff_per_game, _now()))


def upsert_batter(b: data.Batter) -> None:
    """Persist a batter's season hitting line. Idempotent on (id, season)."""
    with _connect() as conn:
        conn.execute("""
            INSERT INTO batters (id, season, name, ops, avg, home_runs, at_bats, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, season) DO UPDATE SET
                name=excluded.name, ops=excluded.ops, avg=excluded.avg,
                home_runs=excluded.home_runs, at_bats=excluded.at_bats,
                updated_at=excluded.updated_at
        """, (b.id, b.season, b.name, b.ops, b.avg,
              b.home_runs, b.at_bats, _now()))


def get_batter(player_id: int, season: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM batters WHERE id = ? AND season = ?",
            (player_id, season),
        ).fetchone()
        return dict(row) if row else None


def upsert_lineup(game_pk: int, side: str, player_ids: list[int]) -> None:
    """Persist a posted lineup. ``side`` is ``"home"`` or ``"away"``."""
    if side not in ("home", "away"):
        raise ValueError(f"side must be 'home' or 'away', got {side!r}")
    with _connect() as conn:
        conn.execute("""
            INSERT INTO lineups (game_pk, side, player_ids, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(game_pk, side) DO UPDATE SET
                player_ids=excluded.player_ids,
                updated_at=excluded.updated_at
        """, (game_pk, side, json.dumps(player_ids), _now()))


def get_lineup(game_pk: int, side: str) -> list[int] | None:
    """Return the lineup player_ids for one side, or None if not posted yet."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT player_ids FROM lineups WHERE game_pk = ? AND side = ?",
            (game_pk, side),
        ).fetchone()
        if row is None:
            return None
        try:
            return list(json.loads(row["player_ids"]))
        except (json.JSONDecodeError, TypeError):
            return None


def upsert_bullpen(bs: data.BullpenStats) -> None:
    """Persist a team's bullpen aggregate. Idempotent on (team_id, season)."""
    with _connect() as conn:
        conn.execute("""
            INSERT INTO bullpens (team_id, season, era, whip,
                                    innings_pitched, strikeouts_per_9, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_id, season) DO UPDATE SET
                era=excluded.era, whip=excluded.whip,
                innings_pitched=excluded.innings_pitched,
                strikeouts_per_9=excluded.strikeouts_per_9,
                updated_at=excluded.updated_at
        """, (bs.team_id, bs.season, bs.era, bs.whip,
              bs.innings_pitched, bs.strikeouts_per_9, _now()))


def get_bullpen(team_id: int, season: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM bullpens WHERE team_id = ? AND season = ?",
            (team_id, season),
        ).fetchone()
        return dict(row) if row else None


def upsert_pitcher(pitcher: Pitcher, season: int) -> None:
    if pitcher.id is None:
        return
    with _connect() as conn:
        conn.execute("""
            INSERT INTO pitchers (id, name, season, era, whip, innings_pitched,
                                    strikeouts_per_9, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, season=excluded.season,
                era=excluded.era, whip=excluded.whip,
                innings_pitched=excluded.innings_pitched,
                strikeouts_per_9=excluded.strikeouts_per_9,
                updated_at=excluded.updated_at
        """, (pitcher.id, pitcher.name, season, pitcher.era, pitcher.whip,
              pitcher.innings_pitched, pitcher.strikeouts_per_9, _now()))


def upsert_game(g: GameSchedule) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO games (game_pk, game_date, status, home_team_id,
                               away_team_id, home_pitcher_id, away_pitcher_id,
                               home_score, away_score, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_pk) DO UPDATE SET
                game_date=excluded.game_date, status=excluded.status,
                home_team_id=excluded.home_team_id,
                away_team_id=excluded.away_team_id,
                home_pitcher_id=excluded.home_pitcher_id,
                away_pitcher_id=excluded.away_pitcher_id,
                home_score=excluded.home_score, away_score=excluded.away_score,
                updated_at=excluded.updated_at
        """, (g.game_pk, g.game_date.isoformat(), g.status,
              g.home.id, g.away.id,
              g.home_pitcher.id if g.home_pitcher else None,
              g.away_pitcher.id if g.away_pitcher else None,
              g.home_score, g.away_score, _now()))


def record_prediction(game_pk: int, home_win_prob: float, home: str,
                       away: str, model_version: str,
                       inputs: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO predictions (game_pk, computed_at, home_win_prob,
                                       home_team, away_team, model_version, inputs)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_pk) DO UPDATE SET
                computed_at = excluded.computed_at,
                home_win_prob = excluded.home_win_prob,
                model_version = excluded.model_version,
                inputs = excluded.inputs
        """, (game_pk, _now(), float(home_win_prob), home, away,
              model_version, json.dumps(inputs, default=str)))


def settle_prediction(game_pk: int, actual_winner: str) -> None:
    """Backfill the actual_winner column for every prediction row that
    targeted ``game_pk``. Called from the runner after final scores
    arrive."""
    with _connect() as conn:
        conn.execute("""
            UPDATE predictions SET actual_winner = ?
            WHERE game_pk = ? AND actual_winner IS NULL
        """, (actual_winner, game_pk))


def log_activity(kind: str, message: str, detail: dict[str, Any] | None = None) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO activity (ts, kind, message, detail_json)
            VALUES (?, ?, ?, ?)
        """, (_now(), kind, message, json.dumps(detail) if detail else None))
        # Bounded ring — keep last 1000 events
        conn.execute("""
            DELETE FROM activity WHERE id IN (
                SELECT id FROM activity ORDER BY id DESC LIMIT -1 OFFSET 1000
            )
        """)


def set_meta(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))


# ─── Read helpers ─────────────────────────────────────────────────────────


def get_meta(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def get_teams() -> list[dict[str, Any]]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM teams ORDER BY abbreviation").fetchall()]


def get_team_season(team_id: int, season: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM team_season WHERE team_id = ? AND season = ?",
            (team_id, season),
        ).fetchone()
        return dict(row) if row else None


def get_all_team_seasons(season: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("""
            SELECT ts.*, t.abbreviation, t.name, t.short_name, t.league, t.division
            FROM team_season ts JOIN teams t ON ts.team_id = t.id
            WHERE ts.season = ?
            ORDER BY ts.pythagorean DESC
        """, (season,)).fetchall()
        return [dict(r) for r in rows]


def get_pitcher(pitcher_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM pitchers WHERE id = ?",
                            (pitcher_id,)).fetchone()
        return dict(row) if row else None


def get_games_for_date(date_iso_prefix: str) -> list[dict[str, Any]]:
    """Games whose game_date STARTS with ``date_iso_prefix`` (YYYY-MM-DD).
    Treats date_iso_prefix as a UTC date — direct string prefix match.

    For the user-facing "what games happen on local-date X" query,
    use :func:`get_games_for_local_date` instead — late-night-local
    games cross midnight UTC and would be misplaced by a UTC-prefix
    match."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT g.*,
                   ht.abbreviation AS home_abbr, ht.short_name AS home_name,
                   at.abbreviation AS away_abbr, at.short_name AS away_name,
                   hp.name AS home_pitcher_name, ap.name AS away_pitcher_name,
                   hp.era AS home_pitcher_era, ap.era AS away_pitcher_era
            FROM games g
            JOIN teams ht ON g.home_team_id = ht.id
            JOIN teams at ON g.away_team_id = at.id
            LEFT JOIN pitchers hp ON g.home_pitcher_id = hp.id
            LEFT JOIN pitchers ap ON g.away_pitcher_id = ap.id
            WHERE g.game_date LIKE ?
            ORDER BY g.game_date
        """, (date_iso_prefix + "%",)).fetchall()
        return [dict(r) for r in rows]


def get_games_for_local_date(date_iso: str, tz_name: str = "America/Chicago"
                              ) -> list[dict[str, Any]]:
    """Games whose start time falls within the LOCAL calendar day
    ``date_iso`` in timezone ``tz_name``. Right call for the user-facing
    "today's slate" view — captures late-evening US games that have a
    UTC start time on the calendar day after the local date.

    e.g. an 8:38pm CDT game on May 21 has game_date 01:38 UTC May 22,
    and belongs on the user's "May 21" slate. A UTC-prefix query
    would miss it; this UTC-range query catches it.
    """
    from datetime import datetime as _dt
    from datetime import time as _t
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    d = _dt.fromisoformat(date_iso).date() if "T" in date_iso else (
        _dt.fromisoformat(date_iso + "T00:00:00").date()
    )
    start_local = _dt.combine(d, _t(0, 0), tzinfo=tz)
    end_local = start_local + _td(days=1)
    start_utc = start_local.astimezone(UTC).isoformat()
    end_utc = end_local.astimezone(UTC).isoformat()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT g.*,
                   ht.abbreviation AS home_abbr, ht.short_name AS home_name,
                   at.abbreviation AS away_abbr, at.short_name AS away_name,
                   hp.name AS home_pitcher_name, ap.name AS away_pitcher_name,
                   hp.era AS home_pitcher_era, ap.era AS away_pitcher_era
            FROM games g
            JOIN teams ht ON g.home_team_id = ht.id
            JOIN teams at ON g.away_team_id = at.id
            LEFT JOIN pitchers hp ON g.home_pitcher_id = hp.id
            LEFT JOIN pitchers ap ON g.away_pitcher_id = ap.id
            WHERE g.game_date >= ? AND g.game_date < ?
            ORDER BY g.game_date
        """, (start_utc, end_utc)).fetchall()
        return [dict(r) for r in rows]


def get_recent_activity(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("detail_json"):
                try:
                    d["detail"] = json.loads(d["detail_json"])
                except json.JSONDecodeError:
                    d["detail"] = {}
            else:
                d["detail"] = {}
            out.append(d)
        return out


def get_predictions_for_game(game_pk: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM predictions WHERE game_pk = ?
            ORDER BY computed_at DESC
        """, (game_pk,)).fetchall()
        return [dict(r) for r in rows]


def get_brier_summary(season: int) -> dict[str, Any]:
    """Brier score over all *settled* predictions for the season.

    One row per game_pk (the v4 schema enforces this; the GROUP BY
    is a safety net for pre-migration databases).
    """
    with _connect() as conn:
        rows = conn.execute("""
            SELECT p.game_pk, p.home_win_prob, p.actual_winner, p.home_team
            FROM predictions p JOIN games g USING (game_pk)
            WHERE g.game_date LIKE ? AND p.actual_winner IS NOT NULL
            GROUP BY p.game_pk
        """, (f"{season}-%",)).fetchall()
    if not rows:
        return {"n": 0, "brier": None}
    brier_sum = 0.0
    for r in rows:
        actual = 1.0 if r["actual_winner"] == r["home_team"] else 0.0
        brier_sum += (float(r["home_win_prob"]) - actual) ** 2
    return {"n": len(rows), "brier": brier_sum / len(rows)}


def db_size_bytes() -> int:
    p = _state_path()
    return p.stat().st_size if p.exists() else 0


def error_count_since(since_iso: str) -> int:
    with _connect() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS n FROM activity
            WHERE kind = 'error' AND ts >= ?
        """, (since_iso,)).fetchone()
        return int(row["n"]) if row else 0
