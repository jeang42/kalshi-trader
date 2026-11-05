"""Persistent state for the tennis sidecar.

SQLite at ``data/shared/tennis_state.sqlite`` (env ``TENNIS_STATE_PATH``).
WAL + synchronous=NORMAL per the sidecar protocol. Tennis is individual,
so the schema is player-centric (no teams):

* ``players``    — id, name, tour, current Elo (global + per-surface JSON),
                   matches played. The model reads Elo from here.
* ``matches``    — every processed match (for Elo replay + audit + the
                   Recent Matches UI). Keyed on match_id.
* ``predictions``— our pre-match P(p1 wins) per Kalshi ticker + the actual
                   result, for Brier.
* ``activity``   — bounded ring of runner events.
* ``meta``       — schema version, last refresh.

Elo itself is recomputed by the runner/backfill from the matches table via
``tennis.elo`` and snapshotted into ``players`` — the matches table is the
source of truth, ``players.elo`` is the cache the model/UI read.
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

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_PATH = Path("data/shared/tennis_state.sqlite")


def _state_path() -> Path:
    return Path(os.environ.get("TENNIS_STATE_PATH", str(DEFAULT_PATH)))


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
    with _connect() as conn:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        if v >= SCHEMA_VERSION:
            return
        log.info("tennis: migrating schema %d → %d", v, SCHEMA_VERSION)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                id            INTEGER NOT NULL,
                tour          TEXT NOT NULL,
                name          TEXT NOT NULL,
                rank          INTEGER,
                points        REAL,
                elo           REAL NOT NULL DEFAULT 1500,
                surface_elo   TEXT,          -- JSON {surface: elo}
                matches_played INTEGER NOT NULL DEFAULT 0,
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (id, tour)
            );

            CREATE TABLE IF NOT EXISTS matches (
                match_id      TEXT PRIMARY KEY,
                tour          TEXT NOT NULL,
                date          TEXT NOT NULL,
                p1_id         INTEGER NOT NULL,
                p2_id         INTEGER NOT NULL,
                winner_id     INTEGER,
                surface       TEXT,
                tournament    TEXT,
                round_name    TEXT,
                updated_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS matches_by_date ON matches(date);

            CREATE TABLE IF NOT EXISTS predictions (
                ticker        TEXT PRIMARY KEY,
                computed_at   TEXT NOT NULL,
                p1_win_prob   REAL NOT NULL,
                p1_name       TEXT NOT NULL,
                p2_name       TEXT NOT NULL,
                yes_name      TEXT NOT NULL,
                model_version TEXT NOT NULL,
                inputs        TEXT,
                actual_yes_won INTEGER
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


def upsert_match(m: Any) -> None:
    """Store a tennis.data.Match (idempotent on match_id)."""
    with _connect() as conn:
        conn.execute("""
            INSERT INTO matches (match_id, tour, date, p1_id, p2_id, winner_id,
                surface, tournament, round_name, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                winner_id=excluded.winner_id, surface=excluded.surface,
                tournament=excluded.tournament, round_name=excluded.round_name,
                updated_at=excluded.updated_at
        """, (m.match_id, m.tour, m.date.isoformat(), m.p1_id, m.p2_id,
              m.winner_id, m.surface, m.tournament, m.round_name, _now()))


def upsert_player(*, pid: int, tour: str, name: str, elo: float,
                  surface_elo: dict[str, float] | None = None,
                  rank: int | None = None, points: float | None = None,
                  matches_played: int = 0) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO players (id, tour, name, rank, points, elo,
                surface_elo, matches_played, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, tour) DO UPDATE SET
                name=excluded.name, rank=excluded.rank, points=excluded.points,
                elo=excluded.elo, surface_elo=excluded.surface_elo,
                matches_played=excluded.matches_played, updated_at=excluded.updated_at
        """, (pid, tour, name, rank, points, elo,
              json.dumps(surface_elo or {}), matches_played, _now()))


def record_prediction(ticker: str, p1_win_prob: float, p1_name: str,
                      p2_name: str, yes_name: str, model_version: str,
                      inputs: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO predictions (ticker, computed_at, p1_win_prob,
                p1_name, p2_name, yes_name, model_version, inputs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                computed_at=excluded.computed_at,
                p1_win_prob=excluded.p1_win_prob,
                model_version=excluded.model_version, inputs=excluded.inputs
        """, (ticker, _now(), float(p1_win_prob), p1_name, p2_name, yes_name,
              model_version, json.dumps(inputs, default=str)))


def settle_prediction(ticker: str, yes_won: bool) -> None:
    with _connect() as conn:
        conn.execute("""
            UPDATE predictions SET actual_yes_won = ?
            WHERE ticker = ? AND actual_yes_won IS NULL
        """, (1 if yes_won else 0, ticker))


def log_activity(kind: str, message: str,
                 detail: dict[str, Any] | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO activity (ts, kind, message, detail_json) VALUES (?,?,?,?)",
            (_now(), kind, message, json.dumps(detail) if detail else None))
        conn.execute("""
            DELETE FROM activity WHERE id NOT IN
                (SELECT id FROM activity ORDER BY id DESC LIMIT 1000)
        """)


def set_meta(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))


def get_meta(key: str) -> str | None:
    with _connect() as conn:
        r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None


# ─── Readers ─────────────────────────────────────────────────────────────


def all_matches() -> list[dict[str, Any]]:
    """Every match, chronological — the Elo replay source."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM matches ORDER BY date").fetchall()]


def get_player(pid: int, tour: str) -> dict[str, Any] | None:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM players WHERE id=? AND tour=?",
                         (pid, tour)).fetchone()
        return dict(r) if r else None


def find_player_by_name(name: str, tour: str | None = None) -> list[dict[str, Any]]:
    """Case-insensitive exact name match (the ticker bridge uses this on
    Kalshi's full player names). Returns all matches — caller decides on
    ambiguity."""
    with _connect() as conn:
        if tour:
            rows = conn.execute(
                "SELECT * FROM players WHERE lower(name)=lower(?) AND tour=?",
                (name, tour)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM players WHERE lower(name)=lower(?)",
                (name,)).fetchall()
        return [dict(r) for r in rows]


def ratings_table(tour: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
    with _connect() as conn:
        if tour:
            rows = conn.execute(
                "SELECT * FROM players WHERE tour=? ORDER BY elo DESC LIMIT ?",
                (tour, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM players ORDER BY elo DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]


_MATCH_SELECT = """
    SELECT m.*, p1.name AS p1_name, p2.name AS p2_name,
           p1.elo AS p1_elo, p2.elo AS p2_elo
    FROM matches m
    LEFT JOIN players p1 ON m.p1_id = p1.id AND m.tour = p1.tour
    LEFT JOIN players p2 ON m.p2_id = p2.id AND m.tour = p2.tour
"""


def recent_matches(*, limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            _MATCH_SELECT + " WHERE m.winner_id IS NOT NULL "
            "ORDER BY m.date DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def matches_for_date(date_iso: str) -> list[dict[str, Any]]:
    """All matches whose date falls on ``date_iso`` (YYYY-MM-DD), by date
    prefix on the stored ISO timestamp. Powers the date-navigable Today
    view (past dates show results, today/future show scheduled)."""
    with _connect() as conn:
        rows = conn.execute(
            _MATCH_SELECT + " WHERE m.date LIKE ? ORDER BY m.date",
            (date_iso + "%",)).fetchall()
        return [dict(r) for r in rows]


def finished_matches_in_range(start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Finished matches (winner known, both players named) with date in
    [start_iso, end_iso] inclusive (YYYY-MM-DD prefixes). Used by the
    ticker resolver, which scans a small UTC window around the ticker's
    Eastern date and filters by ET calendar date in Python."""
    with _connect() as conn:
        rows = conn.execute(
            _MATCH_SELECT + " WHERE m.winner_id IS NOT NULL "
            "AND m.date >= ? AND m.date < ? ORDER BY m.date",
            (start_iso, end_iso + "T23:59:59")).fetchall()
        return [dict(r) for r in rows]


def date_bounds() -> tuple[str | None, str | None]:
    """Min/max match date (YYYY-MM-DD) in the table — for nav clamping."""
    with _connect() as conn:
        r = conn.execute("SELECT MIN(date), MAX(date) FROM matches").fetchone()
        lo = r[0][:10] if r and r[0] else None
        hi = r[1][:10] if r and r[1] else None
        return lo, hi


def matches_for_player(pid: int, tour: str, *, limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            _MATCH_SELECT + " WHERE m.tour=? AND (m.p1_id=? OR m.p2_id=?) "
            "ORDER BY m.date DESC LIMIT ?", (tour, pid, pid, limit)).fetchall()
        return [dict(r) for r in rows]


def get_recent_activity(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def get_brier_summary() -> dict[str, Any]:
    with _connect() as conn:
        rows = conn.execute("""
            SELECT p1_win_prob, yes_name, p1_name, actual_yes_won
            FROM predictions WHERE actual_yes_won IS NOT NULL
        """).fetchall()
    n = 0
    total = 0.0
    for r in rows:
        # prediction was P(p1 wins); yes_prob = that if yes==p1 else 1-it
        yes_prob = r["p1_win_prob"] if r["yes_name"] == r["p1_name"] else 1 - r["p1_win_prob"]
        total += (yes_prob - r["actual_yes_won"]) ** 2
        n += 1
    return {"n": n, "brier": (total / n) if n else None}
