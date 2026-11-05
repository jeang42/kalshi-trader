"""Recent-form blend for team strength.

A team going 8-2 over its last 10 with a +2 RD/G is genuinely playing
better than its season .520 record. The season-long regressed
Pythagorean is a strong prior but it's slow — it takes weeks for a
hot stretch to move the rating meaningfully.

This module computes a *recent-window* Pythagorean (last N days, where
N is configurable but defaults to 15) and blends it into the season
strength with a capped weight. Mathematically:

    α = min(recent_games_played / TARGET_GAMES, MAX_WEIGHT)
    blended_strength = α × recent_pythagorean + (1 - α) × season_pythagorean

Why a cap, not naive sample-weighting:
- Recent stats are noisy. A 6-game homestand against a weak division
  can paint a misleading picture.
- The model already has a Bayesian prior baked into ``regressed_pythagorean``
  (40 virtual games at .500). Letting recent dominate would short-circuit
  that prior whenever recent is the only signal we look at.
- 30% cap means recent shifts the line — it doesn't replace the line.

Why 15 days:
- MLB teams play ~6 games/week → ~12-13 games in 15 days, hitting the
  cap naturally.
- Short enough to capture roster changes (call-ups, IL returns) and
  manager adjustments.
- Long enough to dampen a single bad series.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from baseball import state

log = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 15
TARGET_GAMES_FOR_FULL_WEIGHT = 12
MAX_RECENT_WEIGHT = 0.30


def get_recent_team_stats(team_id: int, *, days: int = DEFAULT_WINDOW_DAYS,
                           as_of: datetime | None = None,
                           ) -> dict[str, Any] | None:
    """Per-team aggregate (RS, RA, W, L, games played) from finalized
    games in the last ``days`` days strictly before ``as_of``.

    ``as_of`` defaults to ``datetime.now(timezone.utc)``. Excludes
    games on the as_of day itself so this is lookahead-safe for
    backfill/replay scenarios where we'd be projecting a game whose
    final score is in the database.

    Returns None if the team has no finalized games in the window.
    """
    if as_of is None:
        as_of = datetime.now(UTC)
    window_start = (as_of - timedelta(days=days)).isoformat()
    window_end = as_of.isoformat()

    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        # Aggregate runs scored, runs allowed, wins, losses
        r = conn.execute("""
            SELECT
              SUM(CASE WHEN home_team_id = :tid THEN home_score ELSE away_score END) AS rs,
              SUM(CASE WHEN home_team_id = :tid THEN away_score ELSE home_score END) AS ra,
              SUM(CASE WHEN (home_team_id = :tid AND home_score > away_score) OR
                            (away_team_id = :tid AND away_score > home_score)
                       THEN 1 ELSE 0 END) AS wins,
              COUNT(*) AS gp
            FROM games
            WHERE status = 'Final'
              AND home_score IS NOT NULL
              AND game_date >= :start
              AND game_date < :end
              AND (home_team_id = :tid OR away_team_id = :tid)
        """, {"tid": team_id, "start": window_start, "end": window_end}
        ).fetchone()

    if r is None or r["gp"] is None or int(r["gp"]) == 0:
        return None
    gp = int(r["gp"])
    return {
        "team_id": team_id,
        "window_days": days,
        "as_of": as_of.isoformat(),
        "games_played": gp,
        "runs_scored": int(r["rs"] or 0),
        "runs_allowed": int(r["ra"] or 0),
        "wins": int(r["wins"] or 0),
        "losses": gp - int(r["wins"] or 0),
        "run_diff_per_game": ((r["rs"] or 0) - (r["ra"] or 0)) / gp,
    }


def recent_weight(recent_gp: int) -> float:
    """Compute the blending weight α for a given recent sample size.
    Linear ramp from 0 (no games) to ``MAX_RECENT_WEIGHT`` at
    ``TARGET_GAMES_FOR_FULL_WEIGHT`` games; capped above."""
    if recent_gp <= 0:
        return 0.0
    return min(
        recent_gp / TARGET_GAMES_FOR_FULL_WEIGHT * MAX_RECENT_WEIGHT,
        MAX_RECENT_WEIGHT,
    )


def blended_strength(
    *,
    season_strength: float,
    recent_strength: float | None,
    recent_gp: int,
) -> tuple[float, float]:
    """Blend recent Pythagorean into season strength.

    Returns ``(blended_strength, weight_used)`` so callers can log
    the actual weight (varies per team based on sample size).

    If ``recent_strength`` is None or recent_gp = 0, returns the
    season strength unchanged with weight=0.
    """
    if recent_strength is None or recent_gp <= 0:
        return season_strength, 0.0
    alpha = recent_weight(recent_gp)
    blended = alpha * recent_strength + (1.0 - alpha) * season_strength
    return blended, alpha


def get_recent_form_table(*, days: int = DEFAULT_WINDOW_DAYS,
                            as_of: datetime | None = None,
                            min_gp: int = 3) -> list[dict[str, Any]]:
    """All teams with at least ``min_gp`` games in the window, with
    their W-L record and run differential. Used by the Stats page
    to show "hot/cold" rankings."""
    if as_of is None:
        as_of = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        for t in conn.execute("SELECT id, abbreviation, short_name FROM teams"):
            stats = get_recent_team_stats(int(t["id"]), days=days, as_of=as_of)
            if stats is None or stats["games_played"] < min_gp:
                continue
            out.append({
                "team_id": int(t["id"]),
                "abbreviation": t["abbreviation"],
                "short_name": t["short_name"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "games_played": stats["games_played"],
                "runs_scored": stats["runs_scored"],
                "runs_allowed": stats["runs_allowed"],
                "run_diff_per_game": stats["run_diff_per_game"],
                "win_pct": stats["wins"] / max(stats["games_played"], 1),
            })
    out.sort(key=lambda r: (r["win_pct"], r["run_diff_per_game"]), reverse=True)
    return out
