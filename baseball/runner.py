"""Cron-able runner for the baseball sidecar.

Schedule
--------
Nightly via cron::

    15 4 * * *  flock -n /tmp/baseball_runner.lock \
                  -c 'cd /home/USER/Coding/kalshi-trader && \
                      .venv/bin/python -m baseball.runner >> data/shared/baseball.log 2>&1'

Modes
-----
* ``--once`` (default): one refresh cycle, exits.
* ``--loop``: re-runs every ``--interval`` seconds. For dev / debugging.
* ``--force``: ignore "already-fresh" skips. Used by the web UI's
  "Refresh now" button.

What one cycle does
-------------------
1. Refresh team list (cheap, runs every time).
2. Refresh per-team season stats (30 small calls).
3. Pull today + tomorrow's schedule.
4. Refresh stats for every probable starter we see.
5. Compute and store a fresh prediction for every scheduled game.
6. Settle yesterday's predictions if final scores are in.
7. Update ``meta.last_refresh_ts``.

Everything is idempotent — running twice has no ill effect beyond
wasted API calls.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime, timedelta

from baseball import data, model, state

log = logging.getLogger("baseball.runner")


def _current_season() -> int:
    """MLB season = calendar year (regular season Mar-Sep, postseason Oct).
    Off-season Jan-Feb still refers to the previous year's stats."""
    today = datetime.now(UTC).date()
    return today.year if today.month >= 3 else today.year - 1


def refresh_teams() -> int:
    teams = data.get_teams()
    state.upsert_teams(teams)
    state.log_activity("fetch", f"refreshed {len(teams)} teams")
    return len(teams)


def refresh_team_stats(season: int) -> int:
    """Refresh per-team season aggregate stats AND bullpen-only stats.
    Bullpen is a separate sitCodes=rp API call per team; we batch both
    refreshes here so a single nightly cycle covers both."""
    teams = data.get_teams()
    n = 0
    n_bullpens = 0
    for t in teams:
        stats = data.get_team_season_stats(t.id, season)
        if stats is not None:
            state.upsert_team_season(stats)
            n += 1
        bullpen = data.get_team_bullpen_stats(t.id, season)
        if bullpen is not None:
            state.upsert_bullpen(bullpen)
            n_bullpens += 1
    state.log_activity(
        "compute",
        f"refreshed {n}/{len(teams)} team season stats, "
        f"{n_bullpens}/{len(teams)} bullpens",
        {"season": season},
    )
    return n


def refresh_lineups_and_batters(season: int, days: int = 2) -> tuple[int, int]:
    """Pull posted lineups for today + the next ``days-1`` days, persist
    them, and fetch season hitting stats for every player ID we see.

    Default `days=2` — lineups post 60-90 min before first pitch so
    games more than ~24h out almost never have lineups. Larger windows
    just cost API calls for empty payloads.

    Pulls UTC dates spanning ``[yesterday, today, today+1, ...]`` to
    catch both: (a) games on UTC yesterday whose lineups are still
    being used for in-progress play, and (b) future-day games whose
    lineups become visible as first pitch approaches. Without
    yesterday, late-evening US games (whose game_date is UTC tomorrow)
    can be missed when the cron runs just after midnight UTC.

    Returns ``(n_lineups_persisted, n_batters_refreshed)``.
    """
    today = datetime.now(UTC).date()
    n_lineups = 0
    pids: set[int] = set()
    for offset in range(-1, days):  # yesterday + today + future
        day = today + timedelta(days=offset)
        try:
            lineups = data.get_lineups_for_date(day)
        except Exception as e:  # noqa: BLE001
            log.warning("lineup fetch failed for %s: %s", day, e)
            continue
        for game_pk, sides in lineups.items():
            for side, ids in sides.items():
                if not ids:
                    continue
                state.upsert_lineup(int(game_pk), side, ids)
                n_lineups += 1
                pids.update(int(p) for p in ids)

    n_batters = 0
    for pid in pids:
        b = data.get_batter_season_stats(pid, season)
        if b is None:
            continue
        state.upsert_batter(b)
        n_batters += 1

    state.log_activity(
        "fetch",
        f"refreshed {n_lineups} lineup(s), {n_batters} batter(s)",
        {"days": days},
    )
    return n_lineups, n_batters


def refresh_schedule_and_pitchers(season: int, days: int = 7) -> tuple[int, int]:
    """Refresh today + the next ``days-1`` days of schedule, plus every
    probable starter we discover.

    Default 7 days because Kalshi MLB markets are listed up to about a
    week out; the edges adapter needs games on those dates loaded into
    state to match a Kalshi ticker to a game. If you only need
    today-only ratings, drop this back to 2 — but the edges path won't
    find anything past tomorrow."""
    today = datetime.now(UTC).date()
    all_games: list[data.GameSchedule] = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        games = data.get_schedule(day)
        for g in games:
            state.upsert_game(g)
            all_games.append(g)

    pitcher_ids: set[int] = set()
    for g in all_games:
        if g.home_pitcher and g.home_pitcher.id is not None:
            pitcher_ids.add(g.home_pitcher.id)
        if g.away_pitcher and g.away_pitcher.id is not None:
            pitcher_ids.add(g.away_pitcher.id)

    n_pitchers = 0
    for pid in pitcher_ids:
        p = data.get_pitcher_season_stats(pid, season)
        if p is None:
            continue
        state.upsert_pitcher(p, season)
        n_pitchers += 1

    state.log_activity(
        "fetch",
        f"refreshed {len(all_games)} games + {n_pitchers}/{len(pitcher_ids)} pitchers",
        {"days": days},
    )
    return len(all_games), n_pitchers


_MIN_AB_FOR_LINEUP_AVG = 50  # filter out tiny-sample noise


def _avg_lineup_ops(game_pk: int, side: str, season: int) -> float | None:
    """Average season OPS of the posted lineup. Filters out batters
    with fewer than ``_MIN_AB_FOR_LINEUP_AVG`` at-bats — small samples
    have extreme OPS values that distort the lineup average (a
    recent call-up with 0.150 in 12 AB shouldn't drag a star-heavy
    lineup down).

    Returns None if no lineup persisted, or if fewer than 5 batters
    in the lineup have a usable OPS (rest treated as league-avg by
    exclusion; better than imputing)."""
    ids = state.get_lineup(int(game_pk), side)
    if not ids:
        return None
    opss: list[float] = []
    for pid in ids:
        b = state.get_batter(int(pid), season)
        if b is None or b.get("ops") is None:
            continue
        ab = b.get("at_bats") or 0
        if ab < _MIN_AB_FOR_LINEUP_AVG:
            continue
        try:
            opss.append(float(b["ops"]))
        except (TypeError, ValueError):
            continue
    if len(opss) < 5:  # need at least half a lineup with reliable stats
        return None
    return sum(opss) / len(opss)


def compute_predictions(season: int, days: int = 7) -> int:
    """For every scheduled (non-final) game in the next ``days`` days,
    compute a fresh prediction. Predictions are append-only (one row
    per (game_pk, computed_at)) so we can track how the line moves as
    starters get announced / scratched.

    Default 7 days to match refresh_schedule_and_pitchers; the edges
    adapter relies on predictions being present for any Kalshi MLB
    ticker date it might encounter."""
    today = datetime.now(UTC).date()
    n = 0
    for offset in range(days):
        day = (today + timedelta(days=offset)).isoformat()
        for g in state.get_games_for_date(day):
            if g["status"] == "Final":
                continue

            home_stats = state.get_team_season(g["home_team_id"], season)
            away_stats = state.get_team_season(g["away_team_id"], season)
            if not home_stats or not away_stats:
                continue

            # Park-neutralize each team's run production before
            # Pythagorean — a team with Coors as its home park has
            # inflated RS+RA that misrepresents its skill. We divide
            # the (assumed-half) home half by the park factor.
            # No-op when the team's park factor isn't estimated yet.
            from baseball import park_factors as _pf
            from baseball import recent_form as _rf
            h_rs, h_ra = _pf.park_neutralize_strength(
                g["home_team_id"],
                home_stats["runs_scored"], home_stats["runs_allowed"],
                home_stats["games_played"],
            )
            a_rs, a_ra = _pf.park_neutralize_strength(
                g["away_team_id"],
                away_stats["runs_scored"], away_stats["runs_allowed"],
                away_stats["games_played"],
            )

            home_strength_season = model.regressed_pythagorean(
                h_rs, h_ra, home_stats["games_played"],
            )
            away_strength_season = model.regressed_pythagorean(
                a_rs, a_ra, away_stats["games_played"],
            )

            # Recent-form blend: last 15 days, weighted up to 30%.
            # Park-neutralize the recent window too for consistency.
            h_recent = _rf.get_recent_team_stats(g["home_team_id"])
            a_recent = _rf.get_recent_team_stats(g["away_team_id"])
            h_recent_strength: float | None = None
            a_recent_strength: float | None = None
            if h_recent:
                hr_rs, hr_ra = _pf.park_neutralize_strength(
                    g["home_team_id"],
                    h_recent["runs_scored"], h_recent["runs_allowed"],
                    h_recent["games_played"],
                )
                h_recent_strength = model.regressed_pythagorean(
                    hr_rs, hr_ra, h_recent["games_played"],
                )
            if a_recent:
                ar_rs, ar_ra = _pf.park_neutralize_strength(
                    g["away_team_id"],
                    a_recent["runs_scored"], a_recent["runs_allowed"],
                    a_recent["games_played"],
                )
                a_recent_strength = model.regressed_pythagorean(
                    ar_rs, ar_ra, a_recent["games_played"],
                )
            home_strength, h_wt = _rf.blended_strength(
                season_strength=home_strength_season,
                recent_strength=h_recent_strength,
                recent_gp=(h_recent["games_played"] if h_recent else 0),
            )
            away_strength, a_wt = _rf.blended_strength(
                season_strength=away_strength_season,
                recent_strength=a_recent_strength,
                recent_gp=(a_recent["games_played"] if a_recent else 0),
            )

            hp_era = ap_era = None
            hp_ip = ap_ip = None
            if g["home_pitcher_id"]:
                hp = state.get_pitcher(g["home_pitcher_id"])
                if hp:
                    hp_era = hp["era"]
                    hp_ip = hp.get("innings_pitched")
            if g["away_pitcher_id"]:
                ap = state.get_pitcher(g["away_pitcher_id"])
                if ap:
                    ap_era = ap["era"]
                    ap_ip = ap.get("innings_pitched")

            # Bullpen ERAs — None defaults to league-average treatment
            h_bullpen = state.get_bullpen(g["home_team_id"], season)
            a_bullpen = state.get_bullpen(g["away_team_id"], season)
            h_bp_era = h_bullpen["era"] if h_bullpen else None
            a_bp_era = a_bullpen["era"] if a_bullpen else None

            # Posted lineups — average OPS of the batting order. None
            # if the lineup hasn't been posted yet (model treats as
            # league-average baseline). When a lineup is partial, we
            # average just the batters we have stats for.
            h_lineup_ops = _avg_lineup_ops(g["game_pk"], "home", season)
            a_lineup_ops = _avg_lineup_ops(g["game_pk"], "away", season)

            pred = model.predict_matchup(
                home_strength=home_strength,
                away_strength=away_strength,
                home_pitcher_era=hp_era,
                away_pitcher_era=ap_era,
                home_pitcher_ip=hp_ip,
                away_pitcher_ip=ap_ip,
                home_bullpen_era=h_bp_era,
                away_bullpen_era=a_bp_era,
                home_lineup_avg_ops=h_lineup_ops,
                away_lineup_avg_ops=a_lineup_ops,
                home_team_name=g["home_abbr"],
                away_team_name=g["away_abbr"],
            )
            state.record_prediction(
                game_pk=g["game_pk"],
                home_win_prob=pred.home_win_prob,
                home=g["home_abbr"],
                away=g["away_abbr"],
                model_version=pred.model_version,
                inputs=pred.to_inputs(),
            )
            n += 1
    state.log_activity("compute", f"computed {n} predictions",
                        {"season": season})
    return n


def settle_finals(season: int) -> int:
    """Walk yesterday + today's finals, record winners on the predictions
    table. Brier rolls up from there."""
    today = datetime.now(UTC).date()
    n = 0
    for offset in (-1, 0):
        day = (today + timedelta(days=offset)).isoformat()
        for g in state.get_games_for_date(day):
            if g["status"] != "Final":
                continue
            if g["home_score"] is None or g["away_score"] is None:
                continue
            winner = (g["home_abbr"]
                      if g["home_score"] > g["away_score"]
                      else g["away_abbr"])
            state.settle_prediction(g["game_pk"], winner)
            n += 1
    if n:
        state.log_activity("settle", f"settled {n} finals")
    return n


def run_cycle(*, force: bool = False) -> dict:
    """One refresh pass. Returns stats."""
    state.migrate()
    season = _current_season()
    t0 = time.time()
    out: dict = {"season": season, "started_at": datetime.now(UTC).isoformat()}
    try:
        out["teams"] = refresh_teams()
        out["team_stats"] = refresh_team_stats(season)
        n_games, n_pitchers = refresh_schedule_and_pitchers(season)
        out["games"] = n_games
        out["pitchers"] = n_pitchers
        # Lineups (today + tomorrow only — they post 60-90min pre-game)
        n_lineups, n_batters = refresh_lineups_and_batters(season)
        out["lineups"] = n_lineups
        out["batters"] = n_batters
        out["predictions"] = compute_predictions(season)
        out["settled"] = settle_finals(season)
        out["elapsed_s"] = round(time.time() - t0, 1)
        state.set_meta("last_refresh_ts", datetime.now(UTC).isoformat())
        state.set_meta("last_refresh_summary", str(out))
        log.info("cycle ok in %.1fs: %s", out["elapsed_s"], out)
    except Exception as e:  # noqa: BLE001 — runner shouldn't die
        out["error"] = f"{type(e).__name__}: {e}"
        state.log_activity("error", out["error"])
        log.exception("baseball cycle failed")
    return out


def run_lineups_only_cycle() -> dict:
    """Lightweight cycle for the in-day lineup refresh cron.

    Lineups post ~60-90 min before first pitch, well after the nightly
    runner. This mode pulls just lineups + batter stats + recomputes
    predictions — skips the heavy team-stats / schedule refresh."""
    state.migrate()
    season = _current_season()
    t0 = time.time()
    out: dict = {"season": season,
                  "started_at": datetime.now(UTC).isoformat(),
                  "mode": "lineups_only"}
    try:
        n_lineups, n_batters = refresh_lineups_and_batters(season)
        out["lineups"] = n_lineups
        out["batters"] = n_batters
        out["predictions"] = compute_predictions(season)
        out["elapsed_s"] = round(time.time() - t0, 1)
        log.info("lineups-only cycle ok in %.1fs: %s",
                 out["elapsed_s"], out)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        state.log_activity("error", out["error"])
        log.exception("lineups-only cycle failed")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loop", action="store_true",
                     help="Re-run every --interval seconds. Default: single cycle.")
    ap.add_argument("--interval", type=int, default=900,
                     help="Loop interval in seconds (default 900 = 15 min).")
    ap.add_argument("--force", action="store_true",
                     help="(reserved for future skip-if-fresh logic)")
    ap.add_argument("--lineups-only", action="store_true",
                     help="Skip the heavy nightly refresh. Just pull "
                          "lineups + batter stats + recompute predictions. "
                          "Designed for in-day cron firing every 30 min as "
                          "lineups post 60-90min before first pitch.")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    _cycle_fn = run_lineups_only_cycle if args.lineups_only else (
        lambda: run_cycle(force=args.force))

    if not args.loop:
        out = _cycle_fn()
        return 1 if "error" in out else 0

    while True:
        try:
            _cycle_fn()
        except KeyboardInterrupt:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
