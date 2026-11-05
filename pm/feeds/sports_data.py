"""Sports win-probability data layer — ESPN public site API.

Why ESPN: their site.api endpoints are unauthenticated, US-accessible,
and return current/upcoming events plus a ``pickcenter`` array of
consensus betting-market odds (DraftKings is the default provider). The
``winPercentage`` field is often null but the ``moneyLine`` is reliably
present, so we derive an implied probability from the moneyline and
remove the bookmaker's vig to get a fair win probability per team.

The edge candidate is the gap between this *betting-market* probability
and the *prediction-market* probability (Polymarket / Kalshi). When the
two diverge, one venue is mispriced relative to the other — usually
because Polymarket lags news the sportsbooks have already priced in.

Endpoints used:
- ``GET /apis/site/v2/sports/{sport}/{league}/scoreboard`` — current
  and upcoming events for a league.
- ``GET /apis/site/v2/sports/{sport}/{league}/summary?event={id}`` —
  full event detail including ``pickcenter`` (odds).

Both endpoints are public and CORS-enabled (used by espn.com itself).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import requests

from pm.core import circuit_breaker

log = logging.getLogger(__name__)

ESPN_BASE = os.getenv("ESPN_API_BASE", "https://site.api.espn.com/apis/site/v2")
HTTP_TIMEOUT = float(os.getenv("ESPN_HTTP_TIMEOUT", "15"))

# Leagues we look up. Keys are display names used in the UI; values are
# (sport, league) tuples for ESPN URL paths. Order matters — we scan
# leagues in this order when matching a market question.
LEAGUES: dict[str, tuple[str, str]] = {
    "MLB": ("baseball", "mlb"),
    "NBA": ("basketball", "nba"),
    "NFL": ("football", "nfl"),
    "NHL": ("hockey", "nhl"),
    "WNBA": ("basketball", "wnba"),
}

_session = requests.Session()
_session.headers.update({"User-Agent": "polymarket-analyzer/1.0"})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TeamSide:
    """One side of a scheduled / live / final event."""

    name: str           # ESPN displayName, e.g. "Los Angeles Lakers"
    short_name: str     # nickname, e.g. "Lakers"
    abbreviation: str   # e.g. "LAL"
    is_home: bool


@dataclass(frozen=True)
class SportsEvent:
    """A single ESPN event with consensus betting-market probabilities."""

    league: str         # "NBA" / "MLB" / ...
    event_id: str
    name: str           # ESPN full name, e.g. "Lakers at Heat"
    state: str          # "pre" | "in" | "post"
    home: TeamSide
    away: TeamSide
    # Vig-removed fair probabilities derived from moneyline. None when
    # ESPN's pickcenter is absent or missing moneylines (most common
    # for events that haven't opened betting yet).
    home_winprob: float | None
    away_winprob: float | None


# ---------------------------------------------------------------------------
# HTTP wrapper
# ---------------------------------------------------------------------------

def _get(path: str, *, params: dict | None = None, retries: int = 2) -> object:
    """Under the ``espn`` circuit breaker (R-21): fails fast with
    ``CircuitOpenError`` while the dependency is down."""
    return circuit_breaker.get("espn").call(_get_unguarded, path, params=params,
                                             retries=retries)


def _get_unguarded(path: str, *, params: dict | None = None, retries: int = 2) -> object:
    url = f"{ESPN_BASE}/{path.lstrip('/')}"
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                sleep_s = max(0.1, min(30.0, float(r.headers.get("Retry-After", "1"))))
                log.warning("ESPN 429; retrying in %.1fs", sleep_s)
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            break
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Moneyline → vig-free probability
# ---------------------------------------------------------------------------

def moneyline_to_prob(ml: float | int | None) -> float | None:
    """American-odds moneyline → implied probability (with vig).

    >>> round(moneyline_to_prob(-200), 4)
    0.6667
    >>> round(moneyline_to_prob(150), 4)
    0.4
    >>> moneyline_to_prob(None) is None
    True
    """
    if ml is None:
        return None
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    if ml < 0:
        return -ml / (-ml + 100.0)
    if ml > 0:
        return 100.0 / (ml + 100.0)
    return None  # 0 moneyline is nonsensical


def devig(p_home: float | None, p_away: float | None) -> tuple[float | None, float | None]:
    """Remove the bookmaker's vig by normalizing two probabilities to sum
    to 1. Returns (None, None) if either input is None.

    >>> # -205 home, +170 away → ~67% / ~37% raw → ~64% / ~36% fair
    >>> h, a = devig(0.672, 0.370)
    >>> round(h, 3), round(a, 3)
    (0.645, 0.355)
    >>> devig(None, 0.5)
    (None, None)
    """
    if p_home is None or p_away is None:
        return (None, None)
    total = p_home + p_away
    if total <= 0:
        return (None, None)
    return (p_home / total, p_away / total)


# ---------------------------------------------------------------------------
# Event parsing from raw ESPN JSON
# ---------------------------------------------------------------------------

def _team_side(comp: dict) -> TeamSide | None:
    team = comp.get("team", {})
    name = team.get("displayName")
    if not name:
        return None
    return TeamSide(
        name=str(name),
        short_name=str(team.get("shortDisplayName") or team.get("name") or name),
        abbreviation=str(team.get("abbreviation") or ""),
        is_home=str(comp.get("homeAway", "")).lower() == "home",
    )


def _event_from_raw(raw: dict, *, league: str, summary: dict | None = None) -> SportsEvent | None:
    """Parse one event from a scoreboard or summary response. Returns
    None if the shape is unexpected (defensive — ESPN's schema drifts).
    """
    comp = (raw.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    if len(competitors) != 2:
        return None
    sides = [_team_side(c) for c in competitors]
    if any(s is None for s in sides):
        return None
    home = next((s for s in sides if s and s.is_home), None)
    away = next((s for s in sides if s and not s.is_home), None)
    if home is None or away is None:
        return None

    # Pickcenter lives on the summary endpoint, not on scoreboard rows —
    # use summary when caller supplied it, otherwise leave probabilities None.
    home_p_raw: float | None = None
    away_p_raw: float | None = None
    pickcenter = (summary or {}).get("pickcenter") or []
    if pickcenter:
        pc = pickcenter[0]
        home_p_raw = moneyline_to_prob(
            (pc.get("homeTeamOdds") or {}).get("moneyLine"),
        )
        away_p_raw = moneyline_to_prob(
            (pc.get("awayTeamOdds") or {}).get("moneyLine"),
        )
    home_p, away_p = devig(home_p_raw, away_p_raw)

    state = (
        ((raw.get("status") or {}).get("type") or {}).get("state") or ""
    ).lower()

    return SportsEvent(
        league=league,
        event_id=str(raw.get("id", "")),
        name=str(raw.get("name") or ""),
        state=state,
        home=home,    # type: ignore[arg-type]
        away=away,    # type: ignore[arg-type]
        home_winprob=home_p,
        away_winprob=away_p,
    )


# ---------------------------------------------------------------------------
# Scoreboard + summary
# ---------------------------------------------------------------------------

def get_scoreboard(league: str) -> list[dict]:
    """Raw scoreboard events for a league key. Empty list on failure or
    unknown league."""
    pair = LEAGUES.get(league.upper())
    if pair is None:
        return []
    sport, lg = pair
    try:
        data = _get(f"sports/{sport}/{lg}/scoreboard")
    except requests.RequestException as e:
        log.warning("get_scoreboard(%s) failed: %s", league, e)
        return []
    if not isinstance(data, dict):
        return []
    return list(data.get("events") or [])


def get_event_summary(league: str, event_id: str) -> dict | None:
    """Full summary for one event — includes ``pickcenter``."""
    pair = LEAGUES.get(league.upper())
    if pair is None:
        return None
    sport, lg = pair
    try:
        return _get(f"sports/{sport}/{lg}/summary", params={"event": event_id})  # type: ignore[return-value]
    except requests.RequestException as e:
        log.warning("get_event_summary(%s, %s) failed: %s", league, event_id, e)
        return None


# Short-TTL cache for get_events_with_probabilities. ESPN consensus lines
# move on the order of minutes, not seconds, but fast callers (the live
# scalper's per-ticker gate, the Edges page re-render loop) can hit this
# many times in quick succession — each call is a scoreboard fetch + one
# summary fetch per event, so an uncached fast loop would hammer ESPN.
# One fetch per league per TTL window protects ESPN and is plenty fresh.
_events_cache: dict[str, tuple[float, list[SportsEvent]]] = {}
EVENTS_CACHE_TTL = float(os.getenv("SPORTS_EVENTS_CACHE_TTL", "60"))


def get_events_with_probabilities(
    league: str, *, use_cache: bool = True,
) -> list[SportsEvent]:
    """All scoreboard events for a league with their vig-free win
    probabilities filled in via the summary endpoint.

    Skips events whose summary fetch fails or whose pickcenter is empty
    — those just come back with ``home_winprob = away_winprob = None``.

    Cached per league for ``EVENTS_CACHE_TTL`` seconds (default 60).
    ``use_cache=False`` forces a fresh fetch (and refreshes the cache).
    """
    key = league.upper()
    now = time.time()
    if use_cache:
        hit = _events_cache.get(key)
        if hit is not None and (now - hit[0]) < EVENTS_CACHE_TTL:
            return hit[1]

    out: list[SportsEvent] = []
    for raw in get_scoreboard(league):
        eid = str(raw.get("id", ""))
        if not eid:
            continue
        summary = get_event_summary(league, eid)
        ev = _event_from_raw(raw, league=league, summary=summary)
        if ev is not None:
            out.append(ev)
    _events_cache[key] = (now, out)
    return out


__all__ = [
    "TeamSide",
    "SportsEvent",
    "LEAGUES",
    "moneyline_to_prob",
    "devig",
    "get_scoreboard",
    "get_event_summary",
    "get_events_with_probabilities",
]


if __name__ == "__main__":
    # Smoke when run directly.
    for league in LEAGUES:
        events = get_events_with_probabilities(league)
        with_odds = [e for e in events if e.home_winprob is not None]
        print(f"{league}: {len(events)} events, {len(with_odds)} with odds")
        for e in with_odds[:2]:
            # mypy: with_odds was filtered to non-None home_winprob; assert
            # the away side too so the f-string arithmetic typechecks.
            assert e.home_winprob is not None and e.away_winprob is not None
            print(f"  {e.name} [{e.state}]  home {e.home_winprob*100:.1f}% / "
                  f"away {e.away_winprob*100:.1f}%")
