"""ESPN tennis data client (ATP + WTA).

Tennis is the 3rd sidecar and the first *individual* (non-team) sport, so
the data shape differs from baseball/WNBA — see docs/sports-model-playbook.md.

Where the data lives (all ESPN, free, no-auth):

* Rankings (player universe + names + ids + points):
    `site.api.espn.com/apis/site/v2/sports/tennis/{tour}/rankings`
    — 150 ranked players per tour. We use this for id→name resolution.
* Tournaments in a date range (for backfill + current):
    `sports.core.api.espn.com/v2/sports/tennis/leagues/{tour}/events?dates=YYYYMMDD-YYYYMMDD`
    — returns tournament events ($ref each).
* Matches for a tournament (INLINE on the event):
    the event JSON's ``competitions`` list — each carries date, round,
    winner flag, and athlete ids **inline** (no per-match fetch needed),
    which makes Elo backfill cheap: 1 fetch per tournament → all matches.

``tour`` is "atp" or "wta". This module is pure HTTP + parsing; the Elo
engine (``elo.py``) and state (``state.py``) consume what it returns.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests

log = logging.getLogger(__name__)

SITE_BASE = os.environ.get(
    "ESPN_TENNIS_SITE_BASE",
    "https://site.api.espn.com/apis/site/v2/sports/tennis",
)
CORE_BASE = os.environ.get(
    "ESPN_TENNIS_CORE_BASE",
    "https://sports.core.api.espn.com/v2/sports/tennis/leagues",
)
HTTP_TIMEOUT = float(os.environ.get("TENNIS_HTTP_TIMEOUT", "20"))
RETRIES = 3
TOURS = ("atp", "wta")

_session = requests.Session()
_session.headers.update({"User-Agent": "kalshi-trader-tennis/0.1 (+https://github.com/jeang42/kalshi-trader)"})


# ─── Data types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Player:
    id: int
    name: str
    tour: str            # "atp" | "wta"
    rank: int | None = None
    points: float | None = None


@dataclass(frozen=True)
class Match:
    """One played (or scheduled) match. ``winner_id`` is None until final.
    Athlete ids come inline from the tournament event — names are resolved
    separately via the rankings map (or the athlete endpoint as fallback)."""

    match_id: str
    tour: str
    date: datetime
    p1_id: int
    p2_id: int
    winner_id: int | None
    surface: str          # "clay" | "hard" | "grass" | "unknown"
    tournament: str
    round_name: str = ""


# ─── HTTP ────────────────────────────────────────────────────────────────


def _get(url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = _session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                sleep_s = min(max(float(r.headers.get("Retry-After", 1.0)), 0.5), 30.0) * (2 ** attempt)
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_exc = e
            time.sleep(0.5 * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"ESPN tennis {url} failed after {RETRIES} attempts")


def _safe_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ─── Rankings (player id → name) ─────────────────────────────────────────


def get_rankings(tour: str) -> list[Player]:
    """Ranked players for a tour (≈150). The id→name source."""
    payload = _get(f"{SITE_BASE}/{tour}/rankings")
    out: list[Player] = []
    for rk in payload.get("rankings", []):
        for r in rk.get("ranks", []):
            ath = r.get("athlete") or {}
            pid = _safe_int(ath.get("id"))
            name = ath.get("displayName")
            if pid is None or not name:
                continue
            out.append(Player(
                id=pid, name=name, tour=tour,
                rank=_safe_int(r.get("current")),
                points=(float(r["points"]) if r.get("points") is not None else None),
            ))
        break  # first ranking list only (the singles ladder)
    return out


# ─── Tournaments + matches ───────────────────────────────────────────────


def _surface_from_name(name: str) -> str:
    n = (name or "").lower()
    # Heuristic from tournament name; ESPN doesn't expose surface cleanly.
    if any(k in n for k in ("roland garros", "french", "madrid", "rome",
                            "monte", "clay", "hamburg", "barcelona", "estoril")):
        return "clay"
    if any(k in n for k in ("wimbledon", "queen", "halle", "newport", "grass",
                            "eastbourne", "'s-hertogenbosch", "stuttgart")):
        return "grass"
    if any(k in n for k in ("us open", "australian open", "indian wells",
                            "miami", "cincinnati", "hard", "open")):
        return "hard"
    return "unknown"


def get_tournaments(tour: str, *, start: str, end: str) -> list[dict[str, Any]]:
    """Tournament events in [start, end] (YYYYMMDD). Returns lightweight
    dicts {id, name, date, end_date, ref}."""
    url = f"{CORE_BASE}/{tour}/events"
    payload = _get(url, params={"dates": f"{start}-{end}", "limit": 200})
    out: list[dict[str, Any]] = []
    for it in payload.get("items", []):
        ref = it.get("$ref")
        if ref:
            out.append({"ref": ref})
    return out


def get_matches_for_tournament(ref: str, tour: str) -> list[Match]:
    """Every match for one tournament, parsed from the event's inline
    ``competitions``. Cheap — one fetch yields all matches with winner +
    athlete ids + date inline."""
    ev = _get(ref)
    tname = ev.get("name", "")
    surface = _surface_from_name(tname)
    out: list[Match] = []
    for c in ev.get("competitions", []):
        comps = c.get("competitors") or []
        if len(comps) != 2:
            continue
        ids, winner_id = [], None
        for x in comps:
            ath = x.get("athlete") or {}
            aid = _safe_int(ath.get("id"))
            if aid is None and ath.get("$ref"):
                tail = ath["$ref"].split("/athletes/")[-1].split("?")[0]
                aid = _safe_int(tail)
            if aid is None:
                break
            ids.append(aid)
            if x.get("winner"):
                winner_id = aid
        if len(ids) != 2:
            continue
        try:
            dt = datetime.fromisoformat(str(c.get("date", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            dt = datetime.now(UTC)
        rnd = ""
        rinfo = c.get("round")
        if isinstance(rinfo, dict):
            rnd = str(rinfo.get("displayName") or rinfo.get("name") or "")
        out.append(Match(
            match_id=str(c.get("id")), tour=tour, date=dt,
            p1_id=ids[0], p2_id=ids[1], winner_id=winner_id,
            surface=surface, tournament=tname, round_name=rnd,
        ))
    return out
