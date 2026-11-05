"""Baseball prediction sidecar — public surface.

This module IS the sidecar (per ``sidecars.py`` protocol). It exposes
both the operational layer (``health`` / ``stats`` / ``recent_activity``)
that the control plane introspects, AND the domain layer
(``predict_game``, ``team_ratings_table``, ``today_schedule``) that the
market app uses to build edges + populate the UI.

Import discipline (per docs/sidecar-protocol.md): nothing in this package
imports ``kalshi_client``, ``provider``, etc. The market app reaches in
via this module's public API only.

Public domain API
-----------------
* :func:`predict_game(home, away, date=None)` — point estimate of
  P(home wins). Returns the most recent stored prediction if one exists
  for today, otherwise computes on the fly from state.
* :func:`team_ratings_table(season=None)` — every team's current
  strength, sorted best-first. For the UI standings table.
* :func:`today_schedule()` — today's slate with predictions attached.

Operational API (Sidecar protocol)
----------------------------------
* :data:`NAME` / :data:`DISPLAY_NAME` — registry identifiers
* :func:`health` — green/yellow/red status + last-refresh timestamp
* :func:`stats` — row counts, db size, season Brier
* :func:`recent_activity(limit=20)` — runner event ring buffer
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from baseball import data as data  # re-exported: consumers use baseball.data.*
from baseball import model, state
from pm.core.sidecars import (
    SidecarActivityEvent,
    SidecarHealth,
    register_sidecar,
    safe_health,
)

log = logging.getLogger(__name__)

NAME = "baseball"
DISPLAY_NAME = "⚾ Baseball"

_MIN_AB_FOR_LINEUP_AVG = 50


def _fallback_avg_lineup_ops(game_pk: int, side: str, season: int) -> float | None:
    """Average OPS of the posted lineup — mirrors runner._avg_lineup_ops."""
    ids = state.get_lineup(game_pk, side)
    if not ids:
        return None
    opss: list[float] = []
    for pid in ids:
        b = state.get_batter(int(pid), season)
        if b is None or b.get("ops") is None:
            continue
        if (b.get("at_bats") or 0) < _MIN_AB_FOR_LINEUP_AVG:
            continue
        try:
            opss.append(float(b["ops"]))
        except (TypeError, ValueError):
            continue
    return (sum(opss) / len(opss)) if len(opss) >= 5 else None

# Stale-data threshold — if the last successful refresh is older than this,
# the sidecar reports "degraded". Cron is nightly so 36h gives a safety
# margin of one missed nightly window before we alarm.
STALE_AFTER_HOURS = 36


# ─── Operational layer (Sidecar protocol) ────────────────────────────────


def _compute_health() -> SidecarHealth:
    last_ts_iso = state.get_meta("last_refresh_ts")
    if not last_ts_iso:
        return SidecarHealth(
            status="degraded",
            detail="never refreshed — run `python -m baseball.runner`",
        )
    try:
        last = datetime.fromisoformat(last_ts_iso)
    except ValueError:
        return SidecarHealth(
            status="degraded", detail=f"unparseable last_refresh_ts: {last_ts_iso}",
        )
    age = datetime.now(UTC) - last
    errors_24h = state.error_count_since(
        (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    )

    if age.total_seconds() > STALE_AFTER_HOURS * 3600:
        return SidecarHealth(
            status="down",
            last_update_ts=last,
            errors_24h=errors_24h,
            detail=f"stale: last refresh {age} ago",
        )
    if errors_24h > 0:
        return SidecarHealth(
            status="degraded",
            last_update_ts=last,
            errors_24h=errors_24h,
            detail=f"{errors_24h} error(s) in last 24h",
        )
    return SidecarHealth(
        status="ok",
        last_update_ts=last,
        errors_24h=0,
        detail="all systems nominal",
    )


def health() -> SidecarHealth:
    return safe_health(_compute_health)


def stats() -> dict[str, Any]:
    """Row counts + db size + season Brier. Cheap; reads only counts."""
    season = _current_season()
    teams = state.get_teams()
    seasons = state.get_all_team_seasons(season)
    brier = state.get_brier_summary(season)
    out = {
        "season": season,
        "teams_loaded": len(teams),
        "teams_rated": len(seasons),
        "db_bytes": state.db_size_bytes(),
        "brier_n": brier["n"],
        "brier_score": brier["brier"],
    }
    # Live-collector stats — only added if the live module has been
    # imported / its schema initialized. Avoids forcing the import on
    # consumers that don't care about live data.
    try:
        from baseball import live as _live
        out.update(_live.live_plays_summary())
    except Exception:  # noqa: BLE001 — live is optional
        pass
    return out


def recent_activity(limit: int = 20) -> list[SidecarActivityEvent]:
    rows = state.get_recent_activity(limit=limit)
    out: list[SidecarActivityEvent] = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["ts"])
        except (ValueError, TypeError):
            ts = datetime.now(UTC)
        out.append(SidecarActivityEvent(
            ts=ts,
            kind=r.get("kind", "unknown"),
            message=r.get("message", ""),
            detail=r.get("detail") or {},
        ))
    return out


# ─── Domain layer ────────────────────────────────────────────────────────


def _current_season() -> int:
    today = datetime.now(UTC).date()
    return today.year if today.month >= 3 else today.year - 1


def _team_id_for(abbr_or_name: str) -> int | None:
    """Resolve a team identifier (3-letter abbreviation or short name)
    to MLB ID. Case-insensitive."""
    key = abbr_or_name.strip().upper()
    for t in state.get_teams():
        if t["abbreviation"].upper() == key:
            return int(t["id"])
        if t["short_name"].upper() == key or t["name"].upper() == key:
            return int(t["id"])
    return None


def predict_game(
    home: str,
    away: str,
    game_date: datetime | None = None,
) -> dict[str, Any] | None:
    """P(``home`` beats ``away``) on the given date.

    Returns a dict::

        {"home_win_prob": 0.58, "home_team": "NYY", "away_team": "BOS",
         "inputs": {...}, "as_of": "..."}

    Or ``None`` if we can't resolve the teams or lack the data to predict.

    Preferred path: look up an already-computed prediction in state.
    Fallback: compute fresh from team season stats + pitcher stats.
    """
    season = _current_season()
    state.migrate()

    home_id = _team_id_for(home)
    away_id = _team_id_for(away)
    if home_id is None or away_id is None:
        log.warning("predict_game: couldn't resolve %r vs %r", home, away)
        return None

    if game_date is None:
        game_date = datetime.now(UTC)

    # Try the most recent stored prediction for the matching game date.
    date_prefix = game_date.date().isoformat()
    for g in state.get_games_for_date(date_prefix):
        if g["home_team_id"] == home_id and g["away_team_id"] == away_id:
            stored = state.get_predictions_for_game(g["game_pk"])
            if stored:
                row = stored[0]
                return {
                    "home_win_prob": row["home_win_prob"],
                    "home_team": row["home_team"],
                    "away_team": row["away_team"],
                    "as_of": row["computed_at"],
                    "inputs": row["inputs"],
                    "source": "stored",
                }

    # Fallback — compute on the fly. Useful for ad-hoc queries.
    # Mirrors the runner's full pipeline: park-neutralized Pythagorean,
    # recent-form blend, SP ERA (regressed), bullpen ERA, lineup OPS.
    h_stats = state.get_team_season(home_id, season)
    a_stats = state.get_team_season(away_id, season)
    if not h_stats or not a_stats:
        return None

    from baseball import park_factors as _pf
    from baseball import recent_form as _rf

    h_rs, h_ra = _pf.park_neutralize_strength(
        home_id, h_stats["runs_scored"], h_stats["runs_allowed"],
        h_stats["games_played"],
    )
    a_rs, a_ra = _pf.park_neutralize_strength(
        away_id, a_stats["runs_scored"], a_stats["runs_allowed"],
        a_stats["games_played"],
    )
    h_season = model.regressed_pythagorean(h_rs, h_ra, h_stats["games_played"])
    a_season = model.regressed_pythagorean(a_rs, a_ra, a_stats["games_played"])

    h_recent = _rf.get_recent_team_stats(home_id)
    a_recent = _rf.get_recent_team_stats(away_id)
    h_rec_strength = a_rec_strength = None
    if h_recent:
        hrs, hra = _pf.park_neutralize_strength(
            home_id, h_recent["runs_scored"], h_recent["runs_allowed"],
            h_recent["games_played"],
        )
        h_rec_strength = model.regressed_pythagorean(hrs, hra, h_recent["games_played"])
    if a_recent:
        ars, ara = _pf.park_neutralize_strength(
            away_id, a_recent["runs_scored"], a_recent["runs_allowed"],
            a_recent["games_played"],
        )
        a_rec_strength = model.regressed_pythagorean(ars, ara, a_recent["games_played"])
    home_strength, _ = _rf.blended_strength(
        season_strength=h_season, recent_strength=h_rec_strength,
        recent_gp=(h_recent["games_played"] if h_recent else 0),
    )
    away_strength, _ = _rf.blended_strength(
        season_strength=a_season, recent_strength=a_rec_strength,
        recent_gp=(a_recent["games_played"] if a_recent else 0),
    )

    # Pitcher, bullpen, lineup — match the runner's full feature set.
    # Look up the game row to get pitcher IDs and game_pk for lineups.
    hp_era = ap_era = hp_ip = ap_ip = None
    h_bp_era = a_bp_era = None
    h_lineup_ops = a_lineup_ops = None
    matched_game = None
    for g in state.get_games_for_date(date_prefix):
        if g["home_team_id"] == home_id and g["away_team_id"] == away_id:
            matched_game = g
            break
    if matched_game:
        if matched_game.get("home_pitcher_id"):
            hp = state.get_pitcher(matched_game["home_pitcher_id"])
            if hp:
                hp_era = hp["era"]
                hp_ip = hp.get("innings_pitched")
        if matched_game.get("away_pitcher_id"):
            ap = state.get_pitcher(matched_game["away_pitcher_id"])
            if ap:
                ap_era = ap["era"]
                ap_ip = ap.get("innings_pitched")
        h_bullpen = state.get_bullpen(home_id, season)
        a_bullpen = state.get_bullpen(away_id, season)
        h_bp_era = h_bullpen["era"] if h_bullpen else None
        a_bp_era = a_bullpen["era"] if a_bullpen else None
        h_lineup_ops = _fallback_avg_lineup_ops(matched_game["game_pk"], "home", season)
        a_lineup_ops = _fallback_avg_lineup_ops(matched_game["game_pk"], "away", season)

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
        home_team_name=h_stats["abbreviation"] if "abbreviation" in h_stats else home,
        away_team_name=a_stats["abbreviation"] if "abbreviation" in a_stats else away,
    )
    return {
        "home_win_prob": pred.home_win_prob,
        "home_team": home,
        "away_team": away,
        "as_of": datetime.now(UTC).isoformat(),
        "inputs": pred.to_inputs(),
        "source": "fallback",
    }


def team_ratings_table(season: int | None = None) -> list[dict[str, Any]]:
    """Every rated team for the season, sorted by regressed Pythagorean."""
    if season is None:
        season = _current_season()
    rows = state.get_all_team_seasons(season)
    out: list[dict[str, Any]] = []
    for r in rows:
        strength = model.regressed_pythagorean(
            r["runs_scored"], r["runs_allowed"], r["games_played"],
        )
        out.append({
            "team_id": r["team_id"],
            "abbreviation": r["abbreviation"],
            "name": r["name"],
            "short_name": r["short_name"],
            "league": r["league"],
            "division": r["division"],
            "wins": r["wins"],
            "losses": r["losses"],
            "win_pct": r["win_pct"],
            "pythagorean": r["pythagorean"],
            "run_diff_per_game": r["run_diff_per_game"],
            "strength": strength,
        })
    out.sort(key=lambda d: d["strength"], reverse=True)
    return out


def today_schedule(tz_name: str | None = None) -> list[dict[str, Any]]:
    """Today's games with predictions attached. UI's main page consumes this.

    "Today" is computed in the LOCAL timezone (``tz_name``, defaults to
    America/Chicago) — not UTC — so a Central viewer sees their local
    calendar day's games, including late-evening starts that cross
    midnight UTC. Override the default by passing a timezone name or
    setting ``BASEBALL_DISPLAY_TZ`` in the environment."""
    import os
    from datetime import tzinfo as _tzinfo
    from zoneinfo import ZoneInfo

    # Bind a new local rather than reassigning the parameter: `tz_name` is
    # declared `str | None`, and mypy keeps a parameter's DECLARED type across
    # reassignment, so every later use stayed Optional. An explicit branch
    # rather than `or` — mypy does not narrow the left operand of `or` here
    # (verified: `tz_name or os.environ.get(k, default)` reveals as str|None).
    zone: str = tz_name if tz_name else os.environ.get(
        "BASEBALL_DISPLAY_TZ", "America/Chicago")
    tz: _tzinfo
    try:
        tz = ZoneInfo(zone)
    except Exception:  # noqa: BLE001
        tz = UTC
    today_local = datetime.now(tz).date().isoformat()
    games = state.get_games_for_local_date(today_local, zone)
    for g in games:
        preds = state.get_predictions_for_game(g["game_pk"])
        if preds:
            g["home_win_prob"] = preds[0]["home_win_prob"]
            g["prediction_as_of"] = preds[0]["computed_at"]
        else:
            g["home_win_prob"] = None
            g["prediction_as_of"] = None
    return games


def predict_for_ticker(ticker: str) -> dict[str, Any] | None:
    """Convenience wrapper for market-app pages: given a Kalshi MLB ticker,
    return the baseball model's view of P(YES on this ticker).

    Returns a dict::

        {"yes_prob": 0.54, "home_win_prob": 0.46, "home": "MIA",
         "away": "NYM", "yes_team": "NYM", "yes_side": "away",
         "game_date": "2026-05-23", "source": "stored"|"fallback"}

    or ``None`` when the ticker isn't a recognized Kalshi MLB ticker, or
    when the relevant game/teams aren't loaded into sidecar state.

    Pages that want "the model says X for this market" should call this
    rather than reach into the matcher/state directly — keeps the
    ticker-format knowledge in one place.
    """
    import re
    from datetime import date as _date

    # Reuse the parser shape from edges.py without importing edges (which
    # would create a market-app → sidecar import cycle even though
    # edges.py imports baseball). Duplicating the regex is cheap and
    # keeps the sidecar self-contained.
    m = re.match(
        r"^KXMLBGAME-(?P<yy>\d{2})(?P<mmm>[A-Z]{3})(?P<dd>\d{2})"
        r"\d{4}(?P<matchup>[A-Z]+)-(?P<yes_team>[A-Z]{2,4})$",
        ticker or "",
    )
    if m is None:
        return None
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    try:
        game_date = _date(2000 + int(m.group("yy")),
                           months[m.group("mmm")],
                           int(m.group("dd"))).isoformat()
    except (KeyError, ValueError):
        return None
    matchup = m.group("matchup")
    yes_abbr = m.group("yes_team").upper()

    state.migrate()
    games = state.get_games_for_date(game_date)
    if not games:
        return None
    for g in games:
        home = str(g.get("home_abbr", "")).upper()
        away = str(g.get("away_abbr", "")).upper()
        if not (home in matchup and away in matchup):
            continue
        if yes_abbr == home:
            yes_side = "home"
            opp_abbr = away
        elif yes_abbr == away:
            yes_side = "away"
            opp_abbr = home
        else:
            continue
        # We need P(yes_team wins). Use the most-recent stored prediction
        # for the matched game_pk if any exist; otherwise fresh-compute
        # via predict_game (which itself falls back to season stats).
        # NOTE: `g.get("home_win_prob")` only works for rows that came
        # via today_schedule() which attaches it; the get_games_for_date
        # join doesn't, so we look it up explicitly.
        home_p: float | None = None
        source = "stored"
        stored = state.get_predictions_for_game(int(g["game_pk"]))
        if stored:
            home_p = float(stored[0]["home_win_prob"])
        if home_p is None:
            pred = predict_game(home, away)
            if pred is None:
                continue
            home_p = float(pred["home_win_prob"])
            source = "fallback"
        yes_prob = float(home_p) if yes_side == "home" else 1.0 - float(home_p)
        return {
            "yes_prob": yes_prob,
            "home_win_prob": float(home_p),
            "home": home, "away": away,
            "yes_team": yes_abbr, "yes_side": yes_side,
            "opponent": opp_abbr,
            "game_date": game_date,
            "source": source,
        }
    return None


def resolve_pending_tickers(tickers: list[str]) -> dict[str, bool]:
    """Sidecar protocol hook (see ``sidecars.resolve_pending_tickers``):
    short-circuit Kalshi's 4-day settlement window by resolving MLB
    Kalshi tickers as soon as we know the final score.

    For each Kalshi MLB ticker passed in:

    1. Parse ticker → ``(game_date, yes_team, matchup_str)``.
    2. Look up games on that date in sidecar state.
    3. Find the game whose home + away match the ticker's matchup.
    4. If status is "Final" and we have both scores → compute the
       winning side and return ``True`` iff ``yes_team`` won.

    Tickers we can't confidently resolve (game not final, no score,
    ticker malformed, sidecar state not loaded) are simply omitted
    from the returned dict — the auto-resolver falls back to the
    venue's settlement feed for those.

    **Kalshi tickers encode date+time in EASTERN time**, not UTC. The
    "26MAY22" prefix means May 22 ET; the corresponding UTC range is
    May 22 04:00 UTC → May 23 04:00 UTC (during EDT). The lookup uses
    ``state.get_games_for_local_date(d, "America/New_York")`` so a
    21:40 ET game on May 22 (UTC May 23 01:40) gets correctly matched
    against the right state row, not yesterday's same-team game.

    This method MUST NOT raise. Per-ticker exceptions are caught;
    a top-level failure returns ``{}``.
    """
    import re
    from datetime import date as _date

    out: dict[str, bool] = {}
    if not tickers:
        return out

    try:
        state.migrate()
    except Exception:  # noqa: BLE001 — degrade gracefully
        return out

    # Same regex as predict_for_ticker. Kept duplicated so this method
    # doesn't depend on call order with the rest of the module.
    rgx = re.compile(
        r"^KXMLBGAME-(?P<yy>\d{2})(?P<mmm>[A-Z]{3})(?P<dd>\d{2})"
        r"\d{4}(?P<matchup>[A-Z]+)-(?P<yes_team>[A-Z]{2,4})$"
    )
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

    # Group tickers by date so we only hit the games query once per
    # date — much cheaper than per-ticker when 30+ tickers share a date.
    # The date parsed from the ticker is EASTERN (venue local); the
    # query uses get_games_for_local_date which converts to a UTC range.
    by_date: dict[str, list[tuple[str, str, str]]] = {}
    for t in tickers:
        m = rgx.match(t or "")
        if m is None:
            continue
        try:
            d = _date(2000 + int(m.group("yy")),
                      months[m.group("mmm")],
                      int(m.group("dd"))).isoformat()
        except (KeyError, ValueError):
            continue
        by_date.setdefault(d, []).append(
            (t, m.group("matchup").upper(), m.group("yes_team").upper())
        )

    for game_date, entries in by_date.items():
        try:
            # game_date is Eastern (ticker convention); resolver looks up
            # games whose UTC start time falls within ET game_date's
            # calendar day.
            games = state.get_games_for_local_date(
                game_date, "America/New_York",
            )
        except Exception:  # noqa: BLE001
            continue
        for ticker, matchup, yes_abbr in entries:
            try:
                for g in games:
                    home = str(g.get("home_abbr", "")).upper()
                    away = str(g.get("away_abbr", "")).upper()
                    if not (home in matchup and away in matchup):
                        continue
                    if g.get("status") != "Final":
                        break  # found the game but it isn't done yet
                    hs, as_ = g.get("home_score"), g.get("away_score")
                    if hs is None or as_ is None:
                        break
                    winner = home if hs > as_ else away
                    out[ticker] = (winner == yes_abbr)
                    break
            except Exception:  # noqa: BLE001 — per-ticker failure
                continue
    return out


def refresh_all(force: bool = False) -> dict[str, Any]:
    """Trigger a refresh cycle. Used by the web UI's 'Refresh now' button.

    Long-running (~30s for a typical day's slate). Callers that don't
    want to block should spawn the runner as a subprocess instead."""
    from baseball.runner import run_cycle
    return run_cycle(force=force)


# ─── Registration ────────────────────────────────────────────────────────


register_sidecar(__name__)
