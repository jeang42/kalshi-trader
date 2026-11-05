"""WNBA sidecar runner — one refresh cycle, cron-able.

Mirrors ``baseball/runner.py`` but simpler (no lineups/pitchers). A cycle:

1. Refresh teams (cheap; rosters change slowly).
2. Refresh per-team season stats (points-for/against, W-L).
3. Pull today's schedule → upsert games.
4. Compute + store a prediction for each of today's games.
5. Settle any now-final games' predictions (for Brier).
6. Stamp ``last_refresh_ts`` in meta.

Run via cron: ``python -m wnba.runner``. Idempotent — safe to run as
often as you like; predictions upsert on event_id.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from wnba import data, model, state

log = logging.getLogger("wnba.runner")

# How many days ahead to pull the schedule. Kalshi lists game markets a few
# days before tip, so we must have those games in state (with predictions)
# before their tickers start trading. 4 days covers the typical listing
# horizon with margin.
SCHEDULE_LOOKAHEAD_DAYS = 4


def _current_season() -> int:
    return datetime.now(UTC).year


def run_cycle(*, force: bool = False) -> dict[str, Any]:
    """One full refresh pass. Returns a stats dict. ``force`` is accepted
    for API symmetry with baseball; this runner has no debounce."""
    started = datetime.now(UTC)
    season = _current_season()
    state.migrate()
    out: dict[str, Any] = {"season": season, "started_at": started.isoformat()}

    # 1. Teams
    try:
        teams = data.get_teams()
        state.upsert_teams(teams)
        out["teams"] = len(teams)
    except Exception as e:  # noqa: BLE001
        log.warning("teams refresh failed: %s", e)
        state.log_activity("error", f"teams refresh failed: {e}")
        out["teams"] = 0

    # 2. Team season stats
    try:
        stats = data.get_team_season_stats(season)
        for st in stats.values():
            state.upsert_team_season(st)
        out["team_stats"] = len(stats)
    except Exception as e:  # noqa: BLE001
        log.warning("team-stats refresh failed: %s", e)
        state.log_activity("error", f"team-stats refresh failed: {e}")
        stats = {}
        out["team_stats"] = 0

    # 3. Schedule (today + lookahead window) + 4. predictions + 5. settle.
    # Pull a forward window so games are in state before their Kalshi
    # markets start trading. Yesterday too, so late-ET finals settle.
    predictions = 0
    settled = 0
    games: list = []
    today = datetime.now(UTC).date()
    seen_ids: set[str] = set()
    for offset in range(-1, SCHEDULE_LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=offset)
        try:
            for g in data.get_schedule(day):
                if g.event_id not in seen_ids:
                    seen_ids.add(g.event_id)
                    games.append(g)
        except Exception as e:  # noqa: BLE001
            log.warning("schedule fetch failed for %s: %s", day, e)
            state.log_activity("error", f"schedule fetch failed {day}: {e}")

    for g in games:
        state.upsert_game(g)

        # settle if final
        if g.status == "post" and g.home_score is not None \
                and g.away_score is not None and g.home_score != g.away_score:
            winner = (g.home.abbreviation if g.home_score > g.away_score
                      else g.away.abbreviation)
            state.settle_prediction(g.event_id, winner)
            settled += 1

        # predict (only when we have both teams' stats)
        h = stats.get(g.home.id)
        a = stats.get(g.away.id)
        if not h or not a:
            continue
        hs = model.regressed_pythagorean(h.points_for, h.points_against,
                                         h.games_played)
        as_ = model.regressed_pythagorean(a.points_for, a.points_against,
                                          a.games_played)
        pred = model.predict_matchup(
            home_strength=hs, away_strength=as_,
            home_team_name=g.home.abbreviation,
            away_team_name=g.away.abbreviation)
        state.record_prediction(
            g.event_id, pred.home_win_prob, g.home.abbreviation,
            g.away.abbreviation, pred.model_version, pred.to_inputs())
        predictions += 1

    out["games"] = len(games)
    out["predictions"] = predictions
    out["settled"] = settled

    state.set_meta("last_refresh_ts", datetime.now(UTC).isoformat())
    elapsed = (datetime.now(UTC) - started).total_seconds()
    out["elapsed_s"] = round(elapsed, 1)
    state.log_activity("refresh", f"cycle ok in {elapsed:.1f}s", out)
    log.info("wnba cycle ok in %.1fs: %s", elapsed, out)
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run_cycle()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
