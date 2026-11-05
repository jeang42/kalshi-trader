"""Per-park run/HR factors calibrated from our own play-by-play data.

Park factors are the classic sabermetric adjustment: each MLB venue
has a stable multiplier on offensive output independent of the teams
playing there. Coors Field famously plays ~1.30× for runs (altitude
+ low humidity = ball flies further); Petco Park, Marlins Park, and
Oracle Park play below 1.0× (cool air, marine layer, pitcher-friendly
dimensions).

Why this matters for win-probability
------------------------------------
Park factors mostly affect *total runs scored*, not which team wins —
they inflate both sides equally. BUT they distort team-strength
estimates when one team plays disproportionately at an extreme park.
A Rockies team with a +0.50 RD/G at home (Coors) and -0.20 RD/G on
the road is genuinely weaker than their +0.15 season RD/G suggests,
because Coors inflates the variance of their runs scored and allowed
without revealing skill.

The right use in a Pythagorean-based model:

1. Park-neutralize each team's run production. Compute their RS and
   RA as if every game were at a league-average park.
2. Project a specific game at THAT park's factor.

This module ships (1). Step (2) is a multiplier on the expected run
total — relevant for over/under markets but mostly cancels for win
probability so we leave the v0.1 model's regressed Pythagorean alone
for now, just feeding it park-neutral inputs.

Calibration
-----------
Park factor for a stat (typically runs) at park P:

    PF(P) = (runs at P, both teams summed) / (runs by same teams elsewhere)
            scaled by (games at P) / (games elsewhere)

Cleaned-up form we actually compute:

    home_at_P_avg + visitors_at_P_avg
    --------------------------------- × correction-for-rounding
    home_at_other_avg + visitors_at_other_avg

Where ``visitors_at_other_avg`` is the visiting team's RS/RA per game
at non-P parks. Gives roughly the same answer as the full Bill James
formula for small samples and is much easier to compute from our data.

This is a **first-cut estimate**. Real park factors are usually
multi-year averages with regression toward 1.0 for small samples.
30 days isn't enough for a final number; treat with healthy
skepticism until we have a full season.

Data needed
-----------
- `games` table: home_team_id, away_team_id, home_score, away_score, status='Final'
- That's it. No play-by-play required for the v1 factor (just final scores).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from baseball import state

log = logging.getLogger(__name__)

# Bayesian prior weight. Park factors regress toward 1.0 (league
# average) when sample size is small. 30 "virtual games at neutral"
# absorbs single-park noise and prevents one weird series from making
# a park look like Coors or Petco.
PRIOR_WEIGHT = 30


# ─── Data shapes ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParkFactor:
    """Run-environment multipliers for one park.

    ``runs_factor`` > 1 means the park boosts run production above
    league average. <1 means it suppresses. ``hr_factor`` same shape
    for home runs specifically (when computed from play-by-play).

    ``n_games`` is the sample size that produced these numbers —
    surface in UIs so users know how much to trust the factor."""

    team_id: int
    abbreviation: str
    runs_factor: float
    hr_factor: float | None
    n_games: int
    raw_runs_per_game_home: float
    raw_runs_per_game_away_avg: float


# ─── Computation ─────────────────────────────────────────────────────────


def _league_runs_per_game(conn: sqlite3.Connection) -> float:
    """Average runs scored per team-game across all finalized games.
    Half of total combined RS+RA per game (since each game has 2
    half-games of offensive output)."""
    r = conn.execute("""
        SELECT AVG(COALESCE(home_score, 0) + COALESCE(away_score, 0)) / 2.0
        FROM games
        WHERE status = 'Final' AND home_score IS NOT NULL
    """).fetchone()
    return float(r[0] or 4.5)  # 4.5 R/G is the historical league average


def compute_park_factors(*, min_games: int = 5) -> dict[int, ParkFactor]:
    """Compute run-environment park factors for every team whose home
    park has at least ``min_games`` finalized games in state.

    The team_id is the home team — park is identified by who's home.
    (Teams that change ballparks mid-season would muddy this, but that
    doesn't happen in MLB; ballpark renovations / temporary venues
    occasionally do — they'd register as the same team_id.)
    """
    out: dict[int, ParkFactor] = {}
    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        league_rpg = _league_runs_per_game(conn)

        # Per-park aggregate: total runs (both teams) and game count
        # at this park.
        rows = conn.execute("""
            SELECT g.home_team_id AS team_id,
                   t.abbreviation,
                   COUNT(*) AS n_games_home,
                   AVG(g.home_score + g.away_score) AS rpg_home_park_combined
            FROM games g
            JOIN teams t ON t.id = g.home_team_id
            WHERE g.status = 'Final'
              AND g.home_score IS NOT NULL
              AND g.away_score IS NOT NULL
            GROUP BY g.home_team_id
            HAVING n_games_home >= ?
        """, (min_games,)).fetchall()

        # For each park, also need the "rest of the league baseline" —
        # what those teams (including visitors) scored at OTHER parks.
        # Cheapest correct approach: each park's combined RPG is
        # compared against the league's average combined RPG. League
        # average combined = 2 × league_rpg.
        league_combined_rpg = 2.0 * league_rpg

        for r in rows:
            n = int(r["n_games_home"])
            home_rpg_combined = float(r["rpg_home_park_combined"] or 0)

            # Bayesian regression toward 1.0: blend the raw ratio with
            # a neutral prior weighted by PRIOR_WEIGHT virtual games.
            raw_factor = home_rpg_combined / max(league_combined_rpg, 1e-9)
            regressed = (
                n * raw_factor + PRIOR_WEIGHT * 1.0
            ) / (n + PRIOR_WEIGHT)

            # Compute home + away RPG split for the dataclass — gives
            # the UI two diagnostics
            r_home = conn.execute("""
                SELECT AVG(home_score) FROM games
                WHERE home_team_id = ? AND status = 'Final'
                  AND home_score IS NOT NULL
            """, (r["team_id"],)).fetchone()[0] or 0
            r_visitors_at_park = conn.execute("""
                SELECT AVG(away_score) FROM games
                WHERE home_team_id = ? AND status = 'Final'
                  AND away_score IS NOT NULL
            """, (r["team_id"],)).fetchone()[0] or 0

            out[int(r["team_id"])] = ParkFactor(
                team_id=int(r["team_id"]),
                abbreviation=str(r["abbreviation"]),
                runs_factor=float(regressed),
                hr_factor=None,  # populated by compute_hr_factors below
                n_games=n,
                raw_runs_per_game_home=float(r_home),
                raw_runs_per_game_away_avg=float(r_visitors_at_park),
            )

    # Layer HR factors on top
    hr = compute_hr_factors(min_games=min_games)
    for tid, pf in out.items():
        if tid in hr:
            out[tid] = ParkFactor(
                team_id=pf.team_id, abbreviation=pf.abbreviation,
                runs_factor=pf.runs_factor, hr_factor=hr[tid],
                n_games=pf.n_games,
                raw_runs_per_game_home=pf.raw_runs_per_game_home,
                raw_runs_per_game_away_avg=pf.raw_runs_per_game_away_avg,
            )
    return out


def compute_hr_factors(*, min_games: int = 5) -> dict[int, float]:
    """Per-park HR-rate factor, calibrated from live_plays.

    Same regressed-ratio shape as the runs factor: HRs hit at this
    park (both teams combined) per game vs league average HR/game.

    Returns ``{}`` if the live_plays table doesn't exist yet (sidecar
    initialized but live daemon never run, or test env without the
    table). Falling back to "no HR factor" rather than erroring keeps
    the run-factor path useful even when play-by-play is missing."""
    out: dict[int, float] = {}
    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        # Detect whether the live_plays table exists at all. The
        # schema is created by baseball/live.py's _ensure_live_schema
        # at daemon startup; if the daemon hasn't run, this query
        # would error.
        has_table = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'live_plays'
        """).fetchone()
        if not has_table:
            return out

        # League-average HRs per game (combined both teams). Denominator
        # is total finalized games (NOT just games with HR plays) so
        # that an HR-suppressing park correctly drags the average down.
        total_hrs = conn.execute(
            "SELECT COUNT(*) FROM live_plays WHERE event_type = 'Home Run'"
        ).fetchone()[0]
        total_games = conn.execute(
            "SELECT COUNT(*) FROM games WHERE status = 'Final'"
        ).fetchone()[0]
        if total_games <= 0:
            return out
        league_hr_combined = total_hrs / total_games
        if league_hr_combined <= 0:
            return out  # no HR plays at all — no factor to compute

        # Per-park HRs/game — denominator is total finalized games at
        # this park, so an HR-suppressing park (HRs/game < league avg)
        # shows up correctly even if some games had zero HRs.
        rows = conn.execute("""
            SELECT g.home_team_id AS team_id,
                   (SELECT COUNT(*) FROM live_plays
                    WHERE game_pk IN (
                        SELECT game_pk FROM games
                        WHERE home_team_id = g.home_team_id AND status = 'Final'
                    ) AND event_type = 'Home Run') AS hr_count,
                   COUNT(*) AS n_games
            FROM games g
            WHERE g.status = 'Final'
            GROUP BY g.home_team_id
            HAVING n_games >= ?
        """, (min_games,)).fetchall()

        for r in rows:
            n = int(r["n_games"])
            hr_per_game = float(r["hr_count"]) / max(n, 1)
            raw = hr_per_game / max(league_hr_combined, 1e-9)
            regressed = (n * raw + PRIOR_WEIGHT * 1.0) / (n + PRIOR_WEIGHT)
            out[int(r["team_id"])] = float(regressed)

    return out


# ─── Cached accessor ─────────────────────────────────────────────────────


_CACHE: dict[str, Any] = {}


def get_park_factors(*, refresh: bool = False) -> dict[int, ParkFactor]:
    """Cached version of :func:`compute_park_factors`. Park factors
    are stable over a season — no need to recompute on every page
    load. ``refresh=True`` forces a recompute (used after a backfill
    or by a /refresh endpoint).
    """
    if not refresh and "factors" in _CACHE:
        return _CACHE["factors"]
    factors = compute_park_factors()
    _CACHE["factors"] = factors
    return factors


def get_factor_for_team(team_id: int) -> ParkFactor | None:
    """Convenience accessor — the park factor for a team's home park.
    Returns None if we have too few games to estimate."""
    return get_park_factors().get(int(team_id))


def park_neutralize_strength(
    team_id: int,
    runs_scored: int,
    runs_allowed: int,
    games_played: int,
) -> tuple[int, int]:
    """Return (park-neutralized RS, park-neutralized RA) for use in
    Pythagorean computation. Conservative — only adjusts when we have
    a confident factor; otherwise returns the inputs unchanged.

    Assumption (limitation): the team played roughly half their
    games at home this season. Real fix would track home/road split
    per team-game; this approximation matches what we have data-wise.
    The home half gets divided by the park factor; the road half is
    unchanged. Same shape for RA.
    """
    pf = get_factor_for_team(team_id)
    if pf is None or pf.runs_factor <= 0:
        return runs_scored, runs_allowed
    # Half home, half road (approximation). Park-neutralize the home
    # half by dividing by the factor; combine with road half unchanged.
    home_rs = runs_scored / 2.0
    home_ra = runs_allowed / 2.0
    away_rs = runs_scored - home_rs
    away_ra = runs_allowed - home_ra
    neutralized_rs = (home_rs / pf.runs_factor) + away_rs
    neutralized_ra = (home_ra / pf.runs_factor) + away_ra
    return int(round(neutralized_rs)), int(round(neutralized_ra))
