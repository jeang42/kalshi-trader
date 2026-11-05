"""Generic sports edges from the ESPN de-vigged consensus. Team-alias matching
lives here; `baseball.py` reuses its alias helpers.

Split out of `edges.py` (P3/R-24, 2026-08-24). Re-exported by
`edges/__init__.py`, so every public name here is still reachable as
`edges.<name>` exactly as before.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pandas as pd

from pm.feeds import sports_data
from pm.research import backtest as bt

from ._base import UTC, Edge, _row_to_snapshot, _severity_for_gap


def _team_aliases(side: sports_data.TeamSide) -> set[str]:
    """All lowercase variants we'll match against question text."""
    out = {side.name.lower(), side.short_name.lower()}
    if side.abbreviation:
        out.add(side.abbreviation.lower())
    return {a for a in out if a}


def _find_first_alias(text: str, aliases: set[str]) -> int | None:
    """Earliest 0-based index of any alias in ``text`` (case-insensitive,
    word-bounded). None when none of the aliases appear."""
    text_lower = text.lower()
    earliest: int | None = None
    for a in aliases:
        # Use a simple `\b...\b` word-boundary so e.g. "lal" doesn't match
        # inside "lala" — abbreviations are short enough to collide
        # otherwise.
        m = re.search(rf"\b{re.escape(a)}\b", text_lower)
        if m is None:
            continue
        if earliest is None or m.start() < earliest:
            earliest = m.start()
    return earliest


# Derivative-market keywords that signal the question is NOT a
# moneyline / head-to-head bet. "Lakers vs. Heat" can be the title of
# an O/U (total points), spread, or prop market — the ESPN moneyline
# probability doesn't apply to those. Skip rather than mismatch.
_DERIVATIVE_KEYWORDS = re.compile(
    r"\b(o\s*/\s*u|over\s*/\s*under|over\b|under\b|spread|"
    r"point\s*spread|prop|first\s*to\s*score|total\s*points?|"
    r"win\s+by|cover|handicap|alt\s*line|moneyline)\b",
    re.IGNORECASE,
)


def _match_event(question: str, events: list[sports_data.SportsEvent]
                  ) -> tuple[sports_data.SportsEvent, str] | None:
    """If ``question`` contains both sides of exactly one event, return
    ``(event, yes_side)`` where ``yes_side`` is "home" or "away" — the
    side that appeared first in the question text (the YES convention).

    Returns None for derivative markets (O/U, spread, props) — the ESPN
    moneyline probability is meaningless there.
    """
    if _DERIVATIVE_KEYWORDS.search(question):
        return None
    candidates = []
    for ev in events:
        home_aliases = _team_aliases(ev.home)
        away_aliases = _team_aliases(ev.away)
        home_idx = _find_first_alias(question, home_aliases)
        away_idx = _find_first_alias(question, away_aliases)
        if home_idx is None or away_idx is None:
            continue
        yes_side = "home" if home_idx < away_idx else "away"
        candidates.append((ev, yes_side))

    # If more than one event matches, we can't pick — ambiguous. Skip.
    if len(candidates) != 1:
        return None
    return candidates[0]


def _winprob_for_side(event: sports_data.SportsEvent, side: str) -> float | None:
    return event.home_winprob if side == "home" else event.away_winprob


def find_sports_edges(
    df_markets: pd.DataFrame,
    *,
    min_gap: float = 0.05,
    leagues: list[str] | None = None,
    min_days_to_expiry: float = 0.0,
    progress_callback: Any = None,
) -> list[Edge]:
    """Scan Sports-category markets for matches against ESPN scheduled
    events, then emit edges where the venue YES price disagrees with
    the consensus-sportsbook fair probability by ``min_gap`` or more.

    ``leagues`` restricts which ESPN leagues to fetch (None → all
    configured). ``min_days_to_expiry=0.0`` (the default) is looser
    than the crypto / LLM sources because sports markets are inherently
    short-dated — most resolve within hours.
    """
    if df_markets is None or df_markets.empty:
        return []

    leagues_to_check = leagues or list(sports_data.LEAGUES.keys())
    # Pre-fetch all events with probabilities. One pre-game event per
    # league usually has good odds; in-progress games have shifting odds
    # but we still use them. Final games are skipped here.
    all_events: list[sports_data.SportsEvent] = []
    n_l = len(leagues_to_check)
    for i, league in enumerate(leagues_to_check):
        if progress_callback is not None:
            try:
                progress_callback(i, n_l + len(df_markets))
            except Exception:
                pass
        for ev in sports_data.get_events_with_probabilities(league):
            if ev.state == "post":  # final — settled already
                continue
            if ev.home_winprob is None or ev.away_winprob is None:
                continue
            all_events.append(ev)

    if not all_events:
        return []

    now = datetime.now(UTC)
    edges_out: list[Edge] = []
    total = n_l + len(df_markets)

    for j, (_, row) in enumerate(df_markets.iterrows()):
        if progress_callback is not None:
            try:
                progress_callback(n_l + j, total)
            except Exception:
                pass

        snap = _row_to_snapshot(row, as_of=now)
        market_p = snap.latest_price
        if market_p is None:
            continue
        if snap.end_date is not None and min_days_to_expiry > 0:
            days = (snap.end_date - now).total_seconds() / 86_400.0
            if days < min_days_to_expiry:
                continue

        match = _match_event(snap.question, all_events)
        if match is None:
            continue
        event, yes_side = match
        model_p = _winprob_for_side(event, yes_side)
        if model_p is None:
            continue

        gap = model_p - market_p
        if abs(gap) < min_gap:
            continue

        yes_team = event.home.name if yes_side == "home" else event.away.name
        note = (
            f"{event.league}: {event.name} [{event.state}]; YES={yes_team}; "
            f"consensus {model_p*100:.1f}% (DraftKings, vig-free)"
        )
        edges_out.append(Edge(
            source="sports",
            market_id=snap.condition_id,
            question=snap.question,
            category=snap.category,
            market_yes_price=float(market_p),
            model_implied=float(model_p),
            gap=float(gap),
            direction="YES" if gap > 0 else "NO",
            severity=_severity_for_gap(abs(gap)),
            note=note,
        ))

    if progress_callback is not None:
        try:
            progress_callback(total, total)
        except Exception:
            pass

    edges_out.sort(key=lambda e: abs(e.gap), reverse=True)
    return edges_out


def strategy_sports_winprob(
    snap: bt.MarketSnapshot,
    *,
    min_gap: float = 0.05,
    leagues: list[str] | None = None,
) -> bt.Signal | None:
    """Backtest-compatible signal generator. Same matching logic as
    :func:`find_sports_edges` but for one snapshot at a time.

    **Caveat for backtesting**: ESPN's scoreboard is a *live* endpoint
    — it returns games happening now, not games that were happening at
    ``snap.as_of``. There's no closed-form historical scoreboard
    available without paid data, so this strategy will return None for
    every closed market in a backtest run. The Tier 3b gate is
    effectively unusable for sports until we add an archival data
    source; live edges from :func:`find_sports_edges` still work.
    """
    market_p = snap.latest_price
    if market_p is None:
        return None

    leagues_to_check = leagues or list(sports_data.LEAGUES.keys())
    all_events: list[sports_data.SportsEvent] = []
    for league in leagues_to_check:
        for ev in sports_data.get_events_with_probabilities(league):
            if ev.state == "post":
                continue
            if ev.home_winprob is None or ev.away_winprob is None:
                continue
            all_events.append(ev)
    if not all_events:
        return None

    match = _match_event(snap.question, all_events)
    if match is None:
        return None
    event, yes_side = match
    model_p = _winprob_for_side(event, yes_side)
    if model_p is None:
        return None

    gap = model_p - market_p
    if abs(gap) < min_gap:
        return None
    return bt.Signal(direction="YES" if gap > 0 else "NO", probability=model_p)


# ---------------------------------------------------------------------------
# Baseball source — our own model (the baseball/ sidecar) vs. venue YES
# ---------------------------------------------------------------------------
#
# Runs in parallel to find_sports_edges on MLB markets. ESPN's sportsbook
# consensus is the *market's* baseline (vig-removed moneyline); the
# baseball sidecar produces *our model's* baseline (regressed Pythagorean
# + SP-ERA log-odds). Two parallel sources tagged source="sports" and
# source="baseball" produce head-to-head Brier on the Calibration page
# so the question "does our model beat the consensus market?" has a real
# answer. If yes → promote to live triggers. If no → improve the model
# or just trust the consensus.
#
# This adapter is the ONLY market-app place that imports `baseball`. The
# sidecar itself never imports back — protects the directional dependency.


