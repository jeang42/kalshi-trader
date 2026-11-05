"""Tennis live layer — in-match state + high-leverage event detection (ESPN).

Step 1 of standing up react-and-hold for tennis (the proven MLB latency edge,
generalized). The tennis sidecar is pre-match only; this adds the LIVE feed the
reaction thesis needs: poll ESPN, find in-progress matches, and diff their
set/game linescores between polls to detect high-leverage swings (a set won, a
break of serve) with an unambiguous beneficiary. A break/set is a large,
discrete win-prob move with obvious direction — so tennis barely needs a model.

This module is the detector. The next increments wire it to a capture instrument
(event + Kalshi price at detection + forward, to MEASURE whether the book lags
tennis events like it lags MLB plays) and then to the react-and-hold arm. It
does NOT trade and (by itself) does not persist.

Data: ESPN tennis is tournament-structured — scoreboard → tournament →
`groupings` → `competitions` (the individual matches), each competitor carrying
`athlete.displayName` + per-set `linescores` ({value=games, winner, tiebreak}).
Read-only, free, no auth (same endpoints as the tennis sidecar's `data.py`).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import requests

from pm.core import logctx

log = logging.getLogger(__name__)

SITE_BASE = os.environ.get(
    "ESPN_TENNIS_SITE_BASE",
    "https://site.api.espn.com/apis/site/v2/sports/tennis").rstrip("/")
TOURS = ("atp", "wta")
HTTP_TIMEOUT = float(os.environ.get("TENNIS_HTTP_TIMEOUT", "20"))


@dataclass
class LiveMatch:
    match_id: str
    tour: str
    tournament: str
    round: str
    state: str                 # "pre" | "in" | "post"
    p1: str                    # player 1 display name
    p2: str
    p1_sets: int               # sets won so far
    p2_sets: int
    p1_games: int              # games in the current (in-progress) set
    p2_games: int
    winner: str | None = None  # display name of match winner, if final


@dataclass
class TennisEvent:
    match_id: str
    kind: str                  # "set" | "match" | "break"
    beneficiary: str           # display name of the player who gained
    detail: str = ""
    tournament: str = ""
    players: tuple[str, str] = field(default_factory=lambda: ("", ""))


# ─── Pure parsing + detection (testable, no network) ───────────────────────

def _sets_and_games(linescores: list[dict] | None) -> tuple[int, int]:
    """(sets_won, games_in_current_set) for one competitor's linescores.
    A completed set carries winner=true; the in-progress set (last entry, no
    winner) contributes the current game count."""
    if not linescores:
        return 0, 0
    sets_won = sum(1 for ls in linescores if (ls or {}).get("winner"))
    last = linescores[-1] or {}
    cur_games = 0 if last.get("winner") else int(last.get("value") or 0)
    return sets_won, cur_games


def parse_competition(comp: dict, *, tour: str, tournament: str) -> LiveMatch | None:
    """Parse one ESPN competition (a match) into a LiveMatch. None if it's not a
    two-player singles match we can read."""
    comps = comp.get("competitors") or []
    if len(comps) != 2:
        return None

    def name(c: dict) -> str:
        ath = c.get("athlete") or {}
        return str(ath.get("displayName") or ath.get("shortName") or "").strip()

    c1, c2 = comps[0], comps[1]
    n1, n2 = name(c1), name(c2)
    if not n1 or not n2:
        return None
    s1, g1 = _sets_and_games(c1.get("linescores"))
    s2, g2 = _sets_and_games(c2.get("linescores"))
    state = (((comp.get("status") or {}).get("type") or {}).get("state") or "").lower()
    winner = None
    if c1.get("winner"):
        winner = n1
    elif c2.get("winner"):
        winner = n2
    rnd = ""
    note = comp.get("round") or comp.get("type") or {}
    if isinstance(note, dict):
        rnd = str(note.get("displayName") or note.get("name") or "")
    return LiveMatch(
        match_id=str(comp.get("id") or ""), tour=tour, tournament=tournament,
        round=rnd, state=state, p1=n1, p2=n2, p1_sets=s1, p2_sets=s2,
        p1_games=g1, p2_games=g2, winner=winner)


def detect_events(prev: LiveMatch, curr: LiveMatch) -> list[TennisEvent]:
    """High-leverage events between two snapshots of the SAME match: SET won,
    MATCH won, and BREAK (a player going up a break in the current set) — all
    large, unambiguous win-prob swings with a clear beneficiary.

    BREAK detection is serve-free: ESPN exposes no server/point data, but a
    player crossing to a >=2-game lead in a set GUARANTEES a break happened
    (from level, holds alternate the lead by <=1, so reaching +2 required
    winning a game against serve) and represents a real set advantage. We only
    test it WITHIN a set (set count unchanged) so a set-completion rollover
    doesn't masquerade as a break — that's covered by the set event."""
    if prev is None or curr is None or prev.match_id != curr.match_id:
        return []
    out: list[TennisEvent] = []
    pl = (curr.p1, curr.p2)

    # A set was completed in favour of a player (their set count went up).
    if curr.p1_sets > prev.p1_sets:
        out.append(TennisEvent(curr.match_id, "set", curr.p1,
                               detail=f"set {curr.p1_sets}-{curr.p2_sets}",
                               tournament=curr.tournament, players=pl))
    if curr.p2_sets > prev.p2_sets:
        out.append(TennisEvent(curr.match_id, "set", curr.p2,
                               detail=f"set {curr.p2_sets}-{curr.p1_sets}",
                               tournament=curr.tournament, players=pl))

    # BREAK: same set, a player crosses into a >=2-game lead (was below it).
    same_set = (prev.p1_sets + prev.p2_sets) == (curr.p1_sets + curr.p2_sets)
    if same_set:
        prev_lead = prev.p1_games - prev.p2_games
        curr_lead = curr.p1_games - curr.p2_games
        if curr_lead >= 2 and prev_lead < 2:
            out.append(TennisEvent(curr.match_id, "break", curr.p1,
                                   detail=f"up a break {curr.p1_games}-{curr.p2_games}",
                                   tournament=curr.tournament, players=pl))
        if curr_lead <= -2 and prev_lead > -2:
            out.append(TennisEvent(curr.match_id, "break", curr.p2,
                                   detail=f"up a break {curr.p2_games}-{curr.p1_games}",
                                   tournament=curr.tournament, players=pl))

    # Match just went final.
    if curr.state == "post" and prev.state != "post" and curr.winner:
        out.append(TennisEvent(curr.match_id, "match", curr.winner,
                               detail="match", tournament=curr.tournament,
                               players=pl))
    return out


# ─── Live fetch (network) ──────────────────────────────────────────────────

def _get(url: str) -> dict:
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def live_matches(*, include_recent_final: bool = False) -> list[LiveMatch]:
    """Every in-progress singles match across ATP+WTA. With
    include_recent_final, also returns matches that just went final (useful for
    detecting the match-won event on the next poll)."""
    want = {"in", "post"} if include_recent_final else {"in"}
    out: list[LiveMatch] = []
    for tour in TOURS:
        try:
            data = _get(f"{SITE_BASE}/{tour}/scoreboard")
        except Exception as e:  # noqa: BLE001
            log.warning("tennis scoreboard fetch failed (%s): %s", tour, e)
            continue
        for ev in data.get("events") or []:
            tournament = str(ev.get("name") or "")
            for grp in ev.get("groupings") or []:
                for comp in grp.get("competitions") or []:
                    m = parse_competition(comp, tour=tour, tournament=tournament)
                    if m and m.state in want:
                        out.append(m)
    return out


def main() -> int:
    logctx.setup()
    matches = live_matches(include_recent_final=True)
    live = [m for m in matches if m.state == "in"]
    print(f"live tennis matches: {len(live)} (in-progress) / {len(matches)} "
          f"(incl. recent final)")
    for m in matches[:20]:
        print(f"  [{m.state:4}] {m.tour} {m.tournament[:24]:24} "
              f"{m.p1[:18]:18} {m.p1_sets}({m.p1_games}) - "
              f"{m.p2_sets}({m.p2_games}) {m.p2[:18]:18}"
              + (f"  WON: {m.winner}" if m.winner else ""))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
