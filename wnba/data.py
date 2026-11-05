"""ESPN WNBA data client.

ESPN's public `site.api.espn.com` is free, no-auth, and covers WNBA
scoreboards, teams, standings, and box scores. We use it because (a) the
market app already speaks ESPN for consensus odds (`sports_data.py`), and
(b) there's no equally-clean first-party WNBA stats API. Lower resolution
than a league's own feed but one client for everything.

Endpoints we touch:

* `site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams`
    — all 15 teams (one-shot, cache long).
* `.../wnba/scoreboard?dates=YYYYMMDD`
    — a day's games: matchup, scores, live status, per-team record.
* `apis/v2/sports/basketball/wnba/standings`
    — per-team season pointsFor/pointsAgainst/W/L (the model inputs).
* `.../wnba/summary?event={id}`
    — single-game detail incl. play-by-play (used by the live layer).

This module is **pure HTTP + parsing**. No state, no models. ``wnba/
state.py`` and ``wnba/model.py`` consume what this returns. Mirrors
``baseball/data.py`` deliberately so the sidecar pattern stays uniform —
see ``docs/sports-model-playbook.md``.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import requests

log = logging.getLogger(__name__)

# Two ESPN hosts: the `site/v2` API for scoreboard/teams, and the bare
# `v2` API for standings (different path root). Both under the same domain.
SITE_BASE = os.environ.get(
    "ESPN_WNBA_SITE_BASE",
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba",
)
STANDINGS_BASE = os.environ.get(
    "ESPN_WNBA_STANDINGS_BASE",
    "https://site.api.espn.com/apis/v2/sports/basketball/wnba",
)
HTTP_TIMEOUT = float(os.environ.get("WNBA_HTTP_TIMEOUT", "15"))
RETRIES = 3

_session = requests.Session()
_session.headers.update({"User-Agent": "kalshi-trader-wnba/0.1 (+https://github.com/jeang42/kalshi-trader)"})


# ─── Data types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Team:
    """One WNBA team. ``abbreviation`` (ATL, SEA, LV, …) is the canonical
    code used everywhere downstream. ``id`` is ESPN's internal team id."""

    id: int
    abbreviation: str
    name: str          # "Atlanta Dream"
    short_name: str    # "Dream"
    location: str      # "Atlanta"


@dataclass
class GameSchedule:
    """One game from the scoreboard. ``status`` is ESPN's state value —
    one of "pre" (scheduled), "in" (live), "post" (final). We use ESPN's
    home/away convention directly; home gets the home-field boost."""

    event_id: str               # ESPN's unique event id (string)
    game_date: datetime         # UTC start time
    status: str                 # "pre" | "in" | "post"
    home: Team
    away: Team
    home_score: int | None = None
    away_score: int | None = None


@dataclass
class TeamSeasonStats:
    """Aggregated team season stats feeding the strength model. Pythagorean
    expectancy uses the basketball exponent (set in ``model.py``); here we
    just carry the raw points-for / points-against + record."""

    team_id: int
    season: int
    wins: int
    losses: int
    points_for: int
    points_against: int
    # Derived
    games_played: int = field(init=False)
    win_pct: float = field(init=False)
    point_diff_per_game: float = field(init=False)

    def __post_init__(self) -> None:
        self.games_played = self.wins + self.losses
        gp = max(self.games_played, 1)
        self.win_pct = self.wins / gp
        self.point_diff_per_game = (self.points_for - self.points_against) / gp


# ─── HTTP layer ──────────────────────────────────────────────────────────


def _get(url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET ``url`` with retry on 429/5xx. Raises on persistent failure —
    callers handle (the runner catches and logs)."""
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = _session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = float(r.headers.get("Retry-After", 1.0))
                sleep_s = min(max(retry_after, 0.5), 30.0) * (2 ** attempt)
                log.debug("ESPN WNBA %s → %d; sleeping %.1fs (attempt %d)",
                          url, r.status_code, sleep_s, attempt + 1)
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_exc = e
            time.sleep(0.5 * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"ESPN WNBA {url} failed after {RETRIES} attempts")


# ─── Parsing helpers ─────────────────────────────────────────────────────


def _parse_team(node: dict[str, Any]) -> Team:
    """Build a Team from an ESPN ``team`` dict (shape is consistent across
    the teams list and scoreboard competitors)."""
    return Team(
        id=int(node["id"]),
        abbreviation=str(node.get("abbreviation") or node.get("shortDisplayName") or "").upper(),
        name=str(node.get("displayName") or ""),
        short_name=str(node.get("shortDisplayName") or node.get("name") or ""),
        location=str(node.get("location") or ""),
    )


def _safe_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ─── Public functions ────────────────────────────────────────────────────


def get_teams() -> list[Team]:
    """All WNBA teams. Cheap one-shot; cache as long as you like."""
    payload = _get(f"{SITE_BASE}/teams")
    out: list[Team] = []
    try:
        groups = payload["sports"][0]["leagues"][0]["teams"]
    except (KeyError, IndexError):
        return out
    for g in groups:
        node = g.get("team")
        if node:
            out.append(_parse_team(node))
    return out


def _yyyymmdd(d: date | str | None) -> str | None:
    if d is None:
        return None
    if isinstance(d, str):
        return d.replace("-", "")
    return d.strftime("%Y%m%d")


def get_schedule(game_date: date | str | None = None) -> list[GameSchedule]:
    """Games for ``game_date`` (default: today, ESPN's choice). Returns
    matchups with live status + scores. ESPN dates are YYYYMMDD."""
    params: dict[str, Any] = {}
    ymd = _yyyymmdd(game_date)
    if ymd:
        params["dates"] = ymd
    payload = _get(f"{SITE_BASE}/scoreboard", params=params or None)
    out: list[GameSchedule] = []
    for ev in payload.get("events", []):
        try:
            comp = ev["competitions"][0]
            competitors = comp["competitors"]
        except (KeyError, IndexError):
            continue
        home_t = away_t = None
        home_s = away_s = None
        for c in competitors:
            team = _parse_team(c["team"])
            score = _safe_int(c.get("score"))
            if c.get("homeAway") == "home":
                home_t, home_s = team, score
            else:
                away_t, away_s = team, score
        if home_t is None or away_t is None:
            continue
        # status state: "pre" | "in" | "post"
        try:
            state = ev["status"]["type"]["state"]
        except KeyError:
            state = "pre"
        try:
            dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            dt = datetime.now(UTC)
        out.append(GameSchedule(
            event_id=str(ev.get("id")),
            game_date=dt,
            status=str(state),
            home=home_t,
            away=away_t,
            home_score=home_s,
            away_score=away_s,
        ))
    return out


def get_team_season_stats(season: int) -> dict[int, TeamSeasonStats]:
    """Per-team season points-for / points-against / W-L from the standings
    endpoint. Returns ``{team_id: TeamSeasonStats}``. The model regresses
    these, so early-season volatility is handled downstream."""
    payload = _get(f"{STANDINGS_BASE}/standings", params={"season": season})
    out: dict[int, TeamSeasonStats] = {}
    for entry in _iter_standing_entries(payload):
        team = entry.get("team") or {}
        tid = _safe_int(team.get("id"))
        if tid is None:
            continue
        stats = {s.get("name"): s.get("value") for s in entry.get("stats", [])}
        wins = _safe_int(stats.get("wins")) or 0
        losses = _safe_int(stats.get("losses")) or 0
        pf = _safe_int(stats.get("pointsFor"))
        pa = _safe_int(stats.get("pointsAgainst"))
        if pf is None or pa is None or (wins + losses) == 0:
            continue
        out[tid] = TeamSeasonStats(
            team_id=tid, season=season, wins=wins, losses=losses,
            points_for=pf, points_against=pa,
        )
    return out


def _iter_standing_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """ESPN nests standings under children (conferences) → standings →
    entries. Walk it tolerantly and return every entry."""
    entries: list[dict[str, Any]] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if isinstance(o.get("entries"), list):
                entries.extend(o["entries"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(payload)
    return entries


def get_game_summary(event_id: str) -> dict[str, Any]:
    """Single-game detail (incl. play-by-play) for the live layer.
    Returns the raw ESPN summary payload — the live module parses what it
    needs (current period, clock, score)."""
    return _get(f"{SITE_BASE}/summary", params={"event": event_id})
