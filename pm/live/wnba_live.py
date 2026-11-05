"""WNBA live layer — in-game win-prob swing detection (ESPN).

Step 1 of react-and-hold for WNBA (3rd sport; scaling the validated MLB latency
edge). The WNBA sidecar is pre-game only; this adds the LIVE feed.

The lucky break: ESPN's basketball/wnba **summary** endpoint exposes a
`winprobability` array (one `homeWinPercentage` per play) — so unlike MLB (we
computed WPA from situation) and tennis (no serve/point data at all), ESPN does
the in-game win-prob modeling for us. A "high-leverage swing" is simply a jump
in homeWinPercentage between polls, with an unambiguous beneficiary (home if WP
rose, away if it fell). That's the direct analog of a high-WPA MLB play.

This module is the detector. Next increments: a capture instrument to MEASURE
whether the Kalshi book lags these swings (don't assume MLB's edge transfers),
then wire to a react-and-hold arm via the WNBA ticker bridge. Read-only, no
trades, no persistence. Mirrors `tennis_live.py`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import requests

from pm.core import logctx

log = logging.getLogger(__name__)

SITE_BASE = os.environ.get(
    "ESPN_WNBA_SITE_BASE",
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("WNBA_HTTP_TIMEOUT", "15"))


@dataclass
class LiveGame:
    game_id: str
    home_abbr: str
    away_abbr: str
    state: str                  # "pre" | "in" | "post"
    home_score: int
    away_score: int
    home_winprob: float | None  # 0..1, ESPN's live model (None pre-game)
    period: int
    clock: str
    winner: str | None = None   # abbr of winner, if final


@dataclass
class WnbaEvent:
    game_id: str
    kind: str                   # "wp_swing" | "game"
    beneficiary: str            # abbr of the team whose win-prob rose
    detail: str = ""
    teams: tuple[str, str] = field(default_factory=lambda: ("", ""))
    wp_delta: float = 0.0


# ─── Pure parsing + detection (testable, no network) ───────────────────────

def parse_game(competition: dict, *, home_winprob: float | None) -> LiveGame | None:
    """Parse one ESPN scoreboard competition + a home win-prob into a LiveGame.
    None if not a readable two-team game."""
    comps = competition.get("competitors") or []
    if len(comps) != 2:
        return None

    def side(ha: str) -> dict | None:
        for c in comps:
            if c.get("homeAway") == ha:
                return c
        return None

    home, away = side("home"), side("away")
    if not home or not away:
        return None

    def abbr(c: dict) -> str:
        return str((c.get("team") or {}).get("abbreviation") or "").upper()

    def score(c: dict) -> int:
        try:
            return int(c.get("score") or 0)
        except (TypeError, ValueError):
            return 0

    status = (competition.get("status") or {}).get("type") or {}
    state = str(status.get("state") or "").lower()
    period = int((competition.get("status") or {}).get("period") or 0)
    clock = str((competition.get("status") or {}).get("displayClock") or "")
    winner = None
    if state == "post":
        if home.get("winner"):
            winner = abbr(home)
        elif away.get("winner"):
            winner = abbr(away)
    return LiveGame(
        game_id=str(competition.get("id") or ""),
        home_abbr=abbr(home), away_abbr=abbr(away), state=state,
        home_score=score(home), away_score=score(away),
        home_winprob=home_winprob, period=period, clock=clock, winner=winner)


def latest_home_winprob(summary: dict) -> float | None:
    """Most recent homeWinPercentage from a summary's winprobability array."""
    wp = summary.get("winprobability") or []
    if not wp:
        return None
    last = wp[-1]
    v = last.get("homeWinPercentage")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def detect_events(prev: LiveGame, curr: LiveGame, *,
                  wp_threshold: float = 0.10) -> list[WnbaEvent]:
    """High-leverage events between two snapshots of the SAME game: a WP_SWING
    (home win-prob moved by >= wp_threshold since last poll — beneficiary is the
    side it moved toward) and GAME (final). The WP swing is the direct analog of
    a high-WPA MLB play."""
    if prev is None or curr is None or prev.game_id != curr.game_id:
        return []
    out: list[WnbaEvent] = []
    teams = (curr.home_abbr, curr.away_abbr)

    if (prev.home_winprob is not None and curr.home_winprob is not None
            and curr.state == "in"):
        delta = curr.home_winprob - prev.home_winprob
        if abs(delta) >= wp_threshold:
            ben = curr.home_abbr if delta > 0 else curr.away_abbr
            out.append(WnbaEvent(curr.game_id, "wp_swing", ben,
                                 detail=f"home WP {prev.home_winprob:.2f}→{curr.home_winprob:.2f}",
                                 teams=teams, wp_delta=round(delta, 4)))

    if curr.state == "post" and prev.state != "post" and curr.winner:
        out.append(WnbaEvent(curr.game_id, "game", curr.winner,
                             detail="final", teams=teams))
    return out


# ─── Live fetch (network) ──────────────────────────────────────────────────

def _get(url: str, **params) -> dict:
    r = requests.get(url, params=params or None, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def live_games(*, include_recent_final: bool = False) -> list[LiveGame]:
    """In-progress WNBA games with ESPN's live home win-prob. Fetches the
    summary (which carries winprobability) only for games that are live."""
    want = {"in", "post"} if include_recent_final else {"in"}
    out: list[LiveGame] = []
    try:
        sb = _get(f"{SITE_BASE}/scoreboard")
    except Exception as e:  # noqa: BLE001
        log.warning("WNBA scoreboard fetch failed: %s", e)
        return out
    for ev in sb.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        state = str((((comp.get("status") or {}).get("type") or {})
                     .get("state")) or "").lower()
        if state not in want:
            continue
        wp = None
        if state == "in":  # only the live games need the heavier summary call
            try:
                summ = _get(f"{SITE_BASE}/summary", event=ev.get("id"))
                wp = latest_home_winprob(summ)
            except Exception as e:  # noqa: BLE001
                log.debug("WNBA summary fetch failed for %s: %s", ev.get("id"), e)
        g = parse_game(comp, home_winprob=wp)
        if g:
            out.append(g)
    return out


def main() -> int:
    logctx.setup()
    games = live_games(include_recent_final=True)
    live = [g for g in games if g.state == "in"]
    print(f"WNBA games: {len(live)} in-progress / {len(games)} (incl. recent final)")
    for g in games:
        wp = f"{g.home_winprob:.2f}" if g.home_winprob is not None else "—"
        print(f"  [{g.state:4}] {g.away_abbr}@{g.home_abbr} "
              f"{g.away_score}-{g.home_score} Q{g.period} {g.clock} "
              f"homeWP={wp}" + (f"  WON: {g.winner}" if g.winner else ""))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
