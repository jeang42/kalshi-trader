"""MLB Stats API client.

The MLB Stats API (`statsapi.mlb.com`) is free, public, no-auth, and
comprehensive. Same source that powers MLB.com itself. Heavily used by
Baseball Savant, FanGraphs, Baseball-Reference for live game data.

Endpoints we touch (all under `/api/v1`):

* `/schedule?sportId=1&date=YYYY-MM-DD` — day's slate with probable pitchers
* `/teams?sportId=1` — all 30 MLB teams (one-shot, cache forever)
* `/teams/{id}/stats?...` — season-to-date team stats
* `/people/{id}/stats?stats=season&season=YYYY&group=pitching` — pitcher stats

Rate limits aren't published; we behave well anyway (small per-call sleep
when paginating, exponential backoff on 429/5xx, single requests Session).

This module is **pure HTTP + parsing**. No state, no models. The
``baseball/state.py`` and ``baseball/model.py`` layers consume what
``data.py`` returns.
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

BASE_URL = os.environ.get("MLB_STATS_API_BASE", "https://statsapi.mlb.com/api/v1")
HTTP_TIMEOUT = float(os.environ.get("MLB_HTTP_TIMEOUT", "15"))
RETRIES = 3

_session = requests.Session()
_session.headers.update({"User-Agent": "kalshi-trader-baseball/0.1 (+https://github.com/jeang42/kalshi-trader)"})


# ─── Data types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Team:
    """One MLB team. `abbreviation` is the canonical 3-letter code we use
    everywhere downstream (NYY, LAD, SFG, …). `id` is MLB's internal ID,
    used for stats lookups."""

    id: int
    abbreviation: str
    name: str          # "New York Yankees"
    short_name: str    # "Yankees"
    league: str        # "AL" | "NL"
    division: str      # "AL East", "NL West", …


@dataclass
class Pitcher:
    """A probable starting pitcher. ERA / WHIP / IP populated when we've
    pulled season stats. ``None`` means the pitcher hasn't been announced
    yet (bullpen game) or we haven't fetched their stats yet."""

    id: int | None
    name: str
    era: float | None = None
    whip: float | None = None
    innings_pitched: float | None = None
    strikeouts_per_9: float | None = None


@dataclass
class GameSchedule:
    """One scheduled game from the schedule endpoint. ``status`` is the
    raw MLB API state ("Scheduled" / "In Progress" / "Final" / "Postponed").

    We use the API's home/away convention directly — home gets the
    home-field advantage in the model."""

    game_pk: int                # MLB's unique game identifier
    game_date: datetime         # UTC start time
    status: str
    home: Team
    away: Team
    home_pitcher: Pitcher | None = None
    away_pitcher: Pitcher | None = None
    home_score: int | None = None
    away_score: int | None = None


@dataclass
class TeamSeasonStats:
    """Aggregated team-level season stats we feed into the strength model.

    All rate stats are vs the season-average baseline. Pythagorean
    expectancy gives the model a quick "true talent" estimate that
    regresses better than raw W/L for small samples.
    """

    team_id: int
    season: int
    wins: int
    losses: int
    runs_scored: int
    runs_allowed: int
    games_played: int
    # Derived
    win_pct: float = field(init=False)
    pythagorean: float = field(init=False)  # Bill James 2.0 exponent
    run_diff_per_game: float = field(init=False)

    def __post_init__(self) -> None:
        gp = max(self.games_played, 1)
        self.win_pct = self.wins / max(self.wins + self.losses, 1)
        rs2 = self.runs_scored ** 2
        ra2 = self.runs_allowed ** 2
        self.pythagorean = rs2 / max(rs2 + ra2, 1)
        self.run_diff_per_game = (self.runs_scored - self.runs_allowed) / gp


# ─── HTTP layer ──────────────────────────────────────────────────────────


def _get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET ``BASE_URL/path`` with retry on 429/5xx. Raises on persistent
    failure — callers should handle (the runner catches and logs)."""
    url = f"{BASE_URL}{path}"
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = _session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = float(r.headers.get("Retry-After", 1.0))
                sleep_s = min(max(retry_after, 0.5), 30.0) * (2 ** attempt)
                log.debug("MLB API %s → %d; sleeping %.1fs (attempt %d)",
                          path, r.status_code, sleep_s, attempt + 1)
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_exc = e
            time.sleep(0.5 * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"MLB API {path} failed after {RETRIES} attempts")


# ─── Public functions ────────────────────────────────────────────────────


def get_teams() -> list[Team]:
    """All 30 MLB teams. Cheap one-shot; cache as long as you like —
    expansion teams come every couple decades."""
    payload = _get("/teams", params={"sportId": 1, "activeStatus": "Y"})
    teams: list[Team] = []
    for t in payload.get("teams", []):
        if t.get("sport", {}).get("id") != 1:
            continue
        teams.append(Team(
            id=int(t["id"]),
            abbreviation=t.get("abbreviation", "").upper(),
            name=t.get("name", ""),
            short_name=t.get("teamName", ""),
            league=t.get("league", {}).get("abbreviation", ""),
            division=t.get("division", {}).get("name", ""),
        ))
    return teams


def _team_from_schedule_blob(side: dict[str, Any], team_index: dict[int, Team]) -> Team:
    """Extract Team from the schedule's compact home/away blob. Falls
    back to a synthesized Team if the blob references a team we didn't
    see in /teams (shouldn't happen in practice; defensive only)."""
    tid = int(side["team"]["id"])
    if tid in team_index:
        return team_index[tid]
    # Defensive fallback — synthesize a minimal Team so we never blow up.
    return Team(
        id=tid,
        abbreviation=side["team"].get("abbreviation", "??"),
        name=side["team"].get("name", "Unknown"),
        short_name=side["team"].get("teamName", "Unknown"),
        league="",
        division="",
    )


def _pitcher_from_blob(blob: dict[str, Any] | None) -> Pitcher | None:
    """Parse a probable-pitcher blob. Returns None if no pitcher field
    or pitcher hasn't been announced (rare but happens)."""
    if not blob or "id" not in blob:
        return None
    return Pitcher(id=int(blob["id"]), name=blob.get("fullName", ""))


def get_schedule(game_date: date | str | None = None) -> list[GameSchedule]:
    """All MLB games on ``game_date`` (defaults to today, UTC). Returns
    `[]` on off days. The schedule endpoint includes probable starters
    when known — usually announced ~24-48h before first pitch."""
    if game_date is None:
        game_date = datetime.now(UTC).date()
    if isinstance(game_date, date):
        date_str = game_date.isoformat()
    else:
        date_str = game_date
    payload = _get("/schedule", params={
        "sportId": 1,
        "date": date_str,
        "hydrate": "probablePitcher,linescore",
    })
    team_index = {t.id: t for t in get_teams()}
    games: list[GameSchedule] = []
    for day in payload.get("dates", []):
        for g in day.get("games", []):
            try:
                home_side = g["teams"]["home"]
                away_side = g["teams"]["away"]
                home = _team_from_schedule_blob(home_side, team_index)
                away = _team_from_schedule_blob(away_side, team_index)
                games.append(GameSchedule(
                    game_pk=int(g["gamePk"]),
                    game_date=_parse_iso(g.get("gameDate")),
                    status=g.get("status", {}).get("detailedState", "Unknown"),
                    home=home,
                    away=away,
                    home_pitcher=_pitcher_from_blob(home_side.get("probablePitcher")),
                    away_pitcher=_pitcher_from_blob(away_side.get("probablePitcher")),
                    home_score=home_side.get("score"),
                    away_score=away_side.get("score"),
                ))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("skipping malformed schedule game: %s", e)
                continue
    return games


def get_team_season_stats(team_id: int, season: int) -> TeamSeasonStats | None:
    """Season-to-date hitting + pitching aggregate for one team."""
    try:
        payload = _get(f"/teams/{team_id}/stats", params={
            "season": season,
            "stats": "season",
            "group": "hitting,pitching",
        })
    except requests.RequestException as e:
        log.warning("team stats fetch failed (team=%s season=%s): %s",
                    team_id, season, e)
        return None

    runs_scored = 0
    runs_allowed = 0
    games_played = 0
    wins = 0
    losses = 0
    for grp in payload.get("stats", []):
        group_name = grp.get("group", {}).get("displayName", "")
        for split in grp.get("splits", []):
            stat = split.get("stat", {})
            if group_name == "hitting":
                runs_scored = int(stat.get("runs", 0))
                games_played = max(games_played, int(stat.get("gamesPlayed", 0)))
            elif group_name == "pitching":
                runs_allowed = int(stat.get("runs", 0))
                wins = int(stat.get("wins", 0))
                losses = int(stat.get("losses", 0))
                games_played = max(games_played, int(stat.get("gamesPlayed", 0)))
    if games_played == 0:
        return None
    return TeamSeasonStats(
        team_id=team_id,
        season=season,
        wins=wins,
        losses=losses,
        runs_scored=runs_scored,
        runs_allowed=runs_allowed,
        games_played=games_played,
    )


@dataclass(frozen=True)
class Batter:
    """One hitter's season line. ``ops`` is the headline stat — sum of
    on-base + slugging, a serviceable single-number offensive metric.
    ``None`` OPS means stats weren't returned (rookie call-up with 0
    AB, pitcher hitting, etc.); model treats as league average."""
    id: int
    name: str
    season: int
    ops: float | None
    avg: float | None
    home_runs: int | None
    at_bats: int | None


def get_batter_season_stats(player_id: int, season: int) -> Batter | None:
    """Pull season hitting stats. Returns None on transport failure.

    Same retry / 429 semantics as the rest of ``data.py`` (via _get).
    Pitchers calling this endpoint typically return zero stats — the
    caller should accept None / 0-AB gracefully."""
    try:
        payload = _get(f"/people/{player_id}/stats", params={
            "stats": "season", "season": season, "group": "hitting",
        })
    except requests.RequestException as e:
        log.warning("batter stats fetch failed (pid=%s): %s", player_id, e)
        return None

    name = ""
    ops = avg = None
    hr = ab = None
    for grp in payload.get("stats", []):
        for split in grp.get("splits", []):
            stat = split.get("stat", {})
            player = split.get("player", {}) or {}
            name = player.get("fullName", "") or name
            try:
                ops = float(stat.get("ops")) if stat.get("ops") else None
            except (TypeError, ValueError):
                ops = None
            try:
                avg = float(stat.get("avg")) if stat.get("avg") else None
            except (TypeError, ValueError):
                avg = None
            try:
                hr = int(stat.get("homeRuns")) if stat.get("homeRuns") is not None else None
            except (TypeError, ValueError):
                hr = None
            try:
                ab = int(stat.get("atBats")) if stat.get("atBats") is not None else None
            except (TypeError, ValueError):
                ab = None
            break  # first split is the regular-season aggregate
    return Batter(id=player_id, name=name, season=season,
                   ops=ops, avg=avg, home_runs=hr, at_bats=ab)


@dataclass(frozen=True)
class Lineup:
    """One posted batting lineup for a game side. ``player_ids`` is
    typically 9 entries (DH leagues) or 8 (when the pitcher hits in
    interleague NL parks). Order is the batting order; index 0 = leadoff."""
    game_pk: int
    side: str  # "home" | "away"
    player_ids: list[int]


def get_lineups_for_date(game_date: date | str | None = None
                          ) -> dict[int, dict[str, list[int]]]:
    """Pull posted lineups for every game on ``game_date`` (defaults to
    today UTC). Returns ``{game_pk: {"home": [pid, ...], "away": [...]}}``
    only for games where the lineup HAS been posted.

    Lineups typically appear 60-90 min before first pitch. For games
    further out, the homePlayers/awayPlayers arrays are empty and
    those games are simply omitted from the returned dict."""
    if game_date is None:
        game_date = datetime.now(UTC).date()
    if isinstance(game_date, date):
        date_str = game_date.isoformat()
    else:
        date_str = str(game_date)
    payload = _get("/schedule", params={
        "sportId": 1, "date": date_str, "hydrate": "lineups",
    })
    out: dict[int, dict[str, list[int]]] = {}
    for day in payload.get("dates", []):
        for g in day.get("games", []):
            game_pk = g.get("gamePk")
            if game_pk is None:
                continue
            lineups = g.get("lineups", {}) or {}
            home = [
                int(p["id"]) for p in lineups.get("homePlayers", [])
                if p.get("id") is not None
            ]
            away = [
                int(p["id"]) for p in lineups.get("awayPlayers", [])
                if p.get("id") is not None
            ]
            if home or away:
                out[int(game_pk)] = {"home": home, "away": away}
    return out


@dataclass(frozen=True)
class BullpenStats:
    """Team bullpen aggregate for the season — reliever-filtered (sitCodes=rp)
    via MLB Stats API. Calibrates the bullpen log-odds adjustment in
    ``model.predict_matchup``. ``None`` ERA means the team hasn't used
    its bullpen enough this season to estimate; treat as league-average."""
    team_id: int
    season: int
    era: float | None
    whip: float | None
    innings_pitched: float | None
    strikeouts_per_9: float | None


def get_team_bullpen_stats(team_id: int, season: int) -> BullpenStats | None:
    """Pull bullpen-filtered (reliever-only) team pitching stats from
    MLB Stats API. Uses ``sitCodes=rp`` which the API exposes as the
    SP/RP role-split filter.

    Returns ``None`` on transport failure — callers should treat that
    as "use the league-average bullpen baseline" rather than crashing.
    """
    try:
        payload = _get(f"/teams/{team_id}/stats", params={
            "season": season,
            "stats": "statSplits",
            "group": "pitching",
            "sitCodes": "rp",
            "gameType": "R",
        })
    except requests.RequestException as e:
        log.warning("bullpen stats fetch failed (team=%s season=%s): %s",
                    team_id, season, e)
        return None

    era = whip = ip = k9 = None
    for grp in payload.get("stats", []):
        for split in grp.get("splits", []):
            stat = split.get("stat", {})
            for key, dest_name in (
                ("era", "era"), ("whip", "whip"),
                ("inningsPitched", "ip"),
                ("strikeoutsPer9Inn", "k9"),
            ):
                raw = stat.get(key)
                try:
                    val = float(raw) if raw is not None else None
                except (TypeError, ValueError):
                    val = None
                if val is not None and val != val:  # NaN
                    val = None
                if dest_name == "era":
                    era = val
                elif dest_name == "whip":
                    whip = val
                elif dest_name == "ip":
                    ip = val
                elif dest_name == "k9":
                    k9 = val
            break  # first split is the aggregate; we don't need per-pitcher
    return BullpenStats(
        team_id=team_id, season=season,
        era=era, whip=whip, innings_pitched=ip, strikeouts_per_9=k9,
    )


def get_pitcher_season_stats(pitcher_id: int, season: int) -> Pitcher | None:
    """Pitcher's season stats. Returns a Pitcher with ERA / WHIP / IP / K9
    populated. None on failure — caller falls back to league-average."""
    try:
        payload = _get(f"/people/{pitcher_id}/stats", params={
            "stats": "season", "season": season, "group": "pitching",
        })
    except requests.RequestException as e:
        log.warning("pitcher stats fetch failed (pid=%s): %s", pitcher_id, e)
        return None

    name = ""
    era = whip = ip = k9 = None
    for grp in payload.get("stats", []):
        for split in grp.get("splits", []):
            stat = split.get("stat", {})
            player = split.get("player", {}) or {}
            name = player.get("fullName", "") or name
            try:
                era = float(stat.get("era", "nan"))
            except (TypeError, ValueError):
                era = None
            try:
                whip = float(stat.get("whip", "nan"))
            except (TypeError, ValueError):
                whip = None
            try:
                ip = float(stat.get("inningsPitched", "0") or 0)
            except (TypeError, ValueError):
                ip = None
            try:
                k9 = float(stat.get("strikeoutsPer9Inn", "nan"))
            except (TypeError, ValueError):
                k9 = None
            if era is not None and (era != era):  # NaN guard
                era = None
            if whip is not None and (whip != whip):
                whip = None
            if k9 is not None and (k9 != k9):
                k9 = None
            break
    return Pitcher(id=pitcher_id, name=name, era=era, whip=whip,
                   innings_pitched=ip, strikeouts_per_9=k9)


# ─── Helpers ─────────────────────────────────────────────────────────────


def _parse_iso(s: str | None) -> datetime:
    """MLB API returns ISO-8601 like '2026-05-20T23:05:00Z'. Pandas-free
    parse so the data module has no heavy deps."""
    if not s:
        return datetime.now(UTC)
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(UTC)
