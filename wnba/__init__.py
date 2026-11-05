"""WNBA prediction sidecar — the 2nd sidecar, built from the baseball
template per ``docs/sports-model-playbook.md``.

Public domain surface (what the market app may call):

* :func:`predict_game` — P(home beats away) on a date.
* :func:`predict_for_ticker` — model's P(YES) for a Kalshi WNBA ticker.
* :func:`today_schedule` — today's games + predictions joined.
* :func:`team_ratings_table` — all teams by regressed strength.
* :func:`resolve_pending_tickers` — fast resolution hook for auto-resolver.

Plus the operational :data:`NAME` / :data:`DISPLAY_NAME` + ``health`` /
``stats`` / ``recent_activity`` Sidecar-protocol surface.

**Ticker format (decoded from live Kalshi):**
``KXWNBAGAME-{YY}{MON}{DD}{AWAYHOME}-{YESTEAM}`` — e.g.
``KXWNBAGAME-26JUN03TORNY-NY`` (Toronto @ New York, YES on NY). Unlike
MLB, there is **no HHMM time component**, and the ``{AWAYHOME}`` blob is
ambiguous to split (variable-length abbrevs concatenated, e.g.
``CONNATL`` = CONN+ATL). We sidestep that entirely: the **market ticker's
suffix is the YES team**, so we match on (date, yes_team) and never parse
the blob.

**Abbreviation mismatch (Kalshi vs ESPN):** Kalshi uses ``CONN``/``PDX``
where ESPN uses ``CON``/``POR``. ``_KALSHI_TO_ESPN`` maps them; unmapped
abbrevs pass through unchanged. Extend the map if Kalshi adds variants.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from datetime import date as _date
from typing import Any

from pm.core.sidecars import (
    SidecarActivityEvent,
    SidecarHealth,
    register_sidecar,
    safe_health,
)
from wnba import model, state

log = logging.getLogger(__name__)

NAME = "wnba"
DISPLAY_NAME = "🏀 WNBA"

STALE_AFTER_HOURS = 36  # season plays most nights; a >36h gap = problem

# Kalshi yes-team suffix → ESPN abbreviation. Only the genuine mismatches
# need entries; everything else passes through unchanged.
_KALSHI_TO_ESPN = {
    "CONN": "CON",   # Connecticut Sun
    "PDX": "POR",    # Portland (ESPN uses POR)
}

_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

# Ticker regex: prefix-date-blob-YESTEAM. We capture the date + yes team;
# the blob is matched but discarded (see module docstring).
_TICKER_RGX = re.compile(
    r"^KXWNBAGAME-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})"
    r"(?P<blob>[A-Z]+)-(?P<yes>[A-Z]{2,4})$"
)


def _current_season() -> int:
    return datetime.now(UTC).year


def _kalshi_abbr_to_espn(abbr: str) -> str:
    return _KALSHI_TO_ESPN.get(abbr.upper(), abbr.upper())


def _parse_ticker(ticker: str) -> tuple[str, str] | None:
    """``KXWNBAGAME-26JUN03TORNY-NY`` → ``("2026-06-03", "NY")`` with the
    yes-team mapped to its ESPN abbr. None if the ticker is malformed."""
    m = _TICKER_RGX.match(ticker or "")
    if m is None:
        return None
    try:
        d = _date(2000 + int(m.group("yy")), _MONTHS[m.group("mon")],
                  int(m.group("dd"))).isoformat()
    except (KeyError, ValueError):
        return None
    return d, _kalshi_abbr_to_espn(m.group("yes"))


def _team_id_for(abbr: str) -> int | None:
    abbr = abbr.upper()
    for t in state.get_teams():
        if str(t.get("abbreviation", "")).upper() == abbr:
            return int(t["id"])
    return None


def _abbr_for_id(team_id: int) -> str | None:
    for t in state.get_teams():
        if int(t["id"]) == team_id:
            return str(t["abbreviation"])
    return None


# ─── Prediction surface ──────────────────────────────────────────────────


def predict_game(home: str, away: str,
                 game_date: datetime | None = None) -> dict[str, Any] | None:
    """P(``home`` beats ``away``). Prefers a stored prediction for the
    date; falls back to computing fresh from team season stats.

    Returns ``{"home_win_prob", "home_team", "away_team", "inputs",
    "as_of", "source"}`` or None when teams/stats aren't loaded.
    """
    season = _current_season()
    state.migrate()
    home_id = _team_id_for(home)
    away_id = _team_id_for(away)
    if home_id is None or away_id is None:
        log.warning("predict_game: couldn't resolve %r vs %r", home, away)
        return None

    if game_date is not None:
        prefix = game_date.date().isoformat()
        for g in state.get_games_for_date(prefix):
            if g["home_team_id"] == home_id and g["away_team_id"] == away_id:
                stored = state.get_predictions_for_game(g["event_id"])
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

    h = state.get_team_season(home_id, season)
    a = state.get_team_season(away_id, season)
    if not h or not a:
        return None
    hs = model.regressed_pythagorean(h["points_for"], h["points_against"],
                                     h["games_played"])
    as_ = model.regressed_pythagorean(a["points_for"], a["points_against"],
                                      a["games_played"])
    home_abbr = _abbr_for_id(home_id) or home
    away_abbr = _abbr_for_id(away_id) or away
    pred = model.predict_matchup(home_strength=hs, away_strength=as_,
                                 home_team_name=home_abbr,
                                 away_team_name=away_abbr)
    return {
        "home_win_prob": pred.home_win_prob,
        "home_team": home_abbr,
        "away_team": away_abbr,
        "as_of": datetime.now(UTC).isoformat(),
        "inputs": pred.to_inputs(),
        "source": "fallback",
    }


def predict_for_ticker(ticker: str) -> dict[str, Any] | None:
    """Given a Kalshi WNBA ticker, return the model's P(YES on this side).

    ``{"yes_prob", "home_win_prob", "home", "away", "yes_team",
    "yes_side", "opponent", "game_date", "source"}`` or None if
    unrecognized / not loaded. The ticker suffix IS the yes team, so we
    match on (date, yes_team) — no matchup-blob parsing.
    """
    parsed = _parse_ticker(ticker)
    if parsed is None:
        return None
    game_date, yes_abbr = parsed
    state.migrate()
    # Ticker date is US Eastern (Kalshi convention); match accordingly.
    for g in state.get_games_for_eastern_date(game_date):
        home = str(g.get("home_abbr", "")).upper()
        away = str(g.get("away_abbr", "")).upper()
        if yes_abbr == home:
            yes_side, opp = "home", away
        elif yes_abbr == away:
            yes_side, opp = "away", home
        else:
            continue
        stored = state.get_predictions_for_game(g["event_id"])
        if not stored:
            return None
        hp = float(stored[0]["home_win_prob"])
        yes_prob = hp if yes_side == "home" else 1.0 - hp
        return {
            "yes_prob": yes_prob,
            "home_win_prob": hp,
            "home": home, "away": away,
            "yes_team": yes_abbr, "yes_side": yes_side, "opponent": opp,
            "game_date": game_date, "source": "stored",
        }
    return None


def team_ratings_table(season: int | None = None) -> list[dict[str, Any]]:
    """All rated teams sorted by regressed strength desc."""
    season = season or _current_season()
    state.migrate()
    out = []
    for r in state.get_all_team_seasons(season):
        strength = model.regressed_pythagorean(
            r["points_for"], r["points_against"], r["games_played"])
        out.append({
            "abbreviation": r.get("abbreviation"), "name": r.get("name"),
            "wins": r["wins"], "losses": r["losses"],
            "win_pct": r["win_pct"], "strength": round(strength, 4),
            "point_diff_per_game": round(r["point_diff_per_game"], 2),
        })
    out.sort(key=lambda x: x["strength"], reverse=True)
    return out


def today_schedule(tz_name: str | None = None) -> list[dict[str, Any]]:
    """Today's games joined with any stored prediction."""
    state.migrate()
    today = datetime.now(UTC).date().isoformat()
    out = []
    for g in state.get_games_for_date(today):
        preds = state.get_predictions_for_game(g["event_id"])
        hp = preds[0]["home_win_prob"] if preds else None
        out.append({
            "event_id": g["event_id"],
            "home_abbr": g.get("home_abbr"), "away_abbr": g.get("away_abbr"),
            "status": g.get("status"),
            "home_score": g.get("home_score"), "away_score": g.get("away_score"),
            "home_win_prob": hp,
        })
    return out


def resolve_pending_tickers(tickers: list[str]) -> dict[str, bool]:
    """Sidecar protocol hook: resolve Kalshi WNBA tickers as soon as we
    have the final score, short-circuiting Kalshi's settlement window.

    Returns ``{ticker: yes_won}`` for tickers whose game is final with a
    known score. Tickers we can't confidently resolve are omitted. MUST
    NOT raise.
    """
    out: dict[str, bool] = {}
    if not tickers:
        return out
    try:
        state.migrate()
    except Exception:  # noqa: BLE001
        return out

    by_date: dict[str, list[tuple[str, str]]] = {}
    for t in tickers:
        parsed = _parse_ticker(t)
        if parsed is None:
            continue
        d, yes = parsed
        by_date.setdefault(d, []).append((t, yes))

    for game_date, entries in by_date.items():
        try:
            games = state.get_games_for_eastern_date(game_date)
        except Exception:  # noqa: BLE001
            continue
        for ticker, yes_abbr in entries:
            try:
                for g in games:
                    home = str(g.get("home_abbr", "")).upper()
                    away = str(g.get("away_abbr", "")).upper()
                    if yes_abbr not in (home, away):
                        continue
                    if str(g.get("status")) != "post":
                        break  # found the game, not final yet
                    hs, as_ = g.get("home_score"), g.get("away_score")
                    if hs is None or as_ is None:
                        break
                    if hs == as_:
                        break  # no ties in basketball; treat as unknown
                    home_won = hs > as_
                    yes_won = home_won if yes_abbr == home else not home_won
                    out[ticker] = bool(yes_won)
                    break
            except Exception:  # noqa: BLE001
                continue
    return out


def refresh_all(force: bool = False) -> dict[str, Any]:
    """Run one runner cycle (blocking). Convenience for the UI/manual use."""
    from wnba import runner
    return runner.run_cycle(force=force)


# ─── Sidecar operational surface ─────────────────────────────────────────


def _compute_health() -> SidecarHealth:
    state.migrate()
    last_iso = state.get_meta("last_refresh_ts")
    if not last_iso:
        return SidecarHealth(status="degraded",
                             detail="never refreshed — run `python -m wnba.runner`")
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return SidecarHealth(status="degraded",
                             detail=f"unparseable last_refresh_ts: {last_iso}")
    age_h = (datetime.now(UTC) - last).total_seconds() / 3600
    if age_h > STALE_AFTER_HOURS:
        return SidecarHealth(status="down", last_update_ts=last,
                             detail=f"stale: last refresh {age_h:.0f}h ago")
    return SidecarHealth(status="ok", last_update_ts=last,
                         detail="all systems nominal")


def health() -> SidecarHealth:
    return safe_health(_compute_health)


def stats() -> dict[str, Any]:
    state.migrate()
    season = _current_season()
    brier = state.get_brier_summary(season)
    return {
        "teams": len(state.get_teams()),
        "rated_teams": len(state.get_all_team_seasons(season)),
        "season": season,
        "brier_n": brier["n"],
        "brier": brier["brier"],
    }


def recent_activity(limit: int = 20) -> list[SidecarActivityEvent]:
    import json
    state.migrate()
    out = []
    for r in state.get_recent_activity(limit):
        detail = {}
        if r.get("detail_json"):
            try:
                detail = json.loads(r["detail_json"])
            except (ValueError, TypeError):
                detail = {}
        try:
            ts = datetime.fromisoformat(r["ts"])
        except (ValueError, TypeError):
            ts = datetime.now(UTC)
        out.append(SidecarActivityEvent(
            ts=ts, kind=r["kind"], message=r["message"], detail=detail))
    return out


register_sidecar(__name__)
