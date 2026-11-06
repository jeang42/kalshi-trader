"""Baseball edges — the MLB ticker bridge plus the baseball sidecar's model.
Also home to `sports_consensus_for_ticker`, the cross-boundary adapter.

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

from ._base import UTC, Edge, _row_to_snapshot, _severity_for_gap, log
from .sports import _DERIVATIVE_KEYWORDS, _find_first_alias


def _baseball_team_aliases(*candidates: str) -> set[str]:
    """Lowercase alias set from a team's abbreviation / name / short_name."""
    return {c.strip().lower() for c in candidates if c and c.strip()}


# Kalshi MLB markets ship two per game (one per team as YES), with the
# YES team encoded as the ticker's last segment and the game date in
# the middle segment: e.g.
#   KXMLBGAME-26MAY231610NYMMIA-NYM  → date=2026-05-23, YES=NYM
#   KXMLBGAME-26MAY231610NYMMIA-MIA  → date=2026-05-23, YES=MIA
# Generic "first team in the question is YES" alias matching would tag
# both markets identically AND can't disambiguate when one team plays
# multiple games in our schedule window. Parse the ticker for date +
# YES team; fall back to alias logic only for non-Kalshi MLB markets.
_KALSHI_MLB_TICKER = re.compile(
    r"^KXMLBGAME-(?P<yy>\d{2})(?P<mmm>[A-Z]{3})(?P<dd>\d{2})"
    r"\d{4}(?P<matchup>[A-Z]+)-(?P<yes_team>[A-Z]{2,4})$"
)
_MONTH_ABBR = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def _parse_kalshi_mlb_ticker(ticker: str) -> tuple[str, str, str] | None:
    """Returns ``(iso_date, matchup_str, yes_team_abbr)`` or None.

    ``matchup_str`` is the concatenated team-abbreviation segment (e.g.
    ``"ATHSD"`` for an A's @ Padres game). It isn't a clean split into
    two abbreviations because they're variable-length, but we use it as
    a containment check ("is the opponent's abbreviation a substring of
    matchup_str?") to disambiguate when one team plays multiple games
    on the same UTC date — see ``_match_baseball_game`` path 1.
    """
    m = _KALSHI_MLB_TICKER.match(ticker or "")
    if m is None:
        return None
    try:
        year = 2000 + int(m.group("yy"))
        month = _MONTH_ABBR[m.group("mmm")]
        day = int(m.group("dd"))
        from datetime import date as _date
        iso = _date(year, month, day).isoformat()
    except (KeyError, ValueError):
        return None
    return iso, m.group("matchup").upper(), m.group("yes_team").upper()


def sports_consensus_for_ticker(
    ticker: str, *, league: str = "MLB",
) -> float | None:
    """De-vigged sportsbook **consensus** P(YES) for a Kalshi sports ticker,
    from ESPN's live pickcenter. Currently MLB-only.

    Parses the ``KXMLBGAME`` ticker for date + matchup + YES team, then
    matches the live ESPN event by team abbreviation and returns the YES
    side's vig-free win probability. None when there's no usable consensus
    (game pre-open / finished / no moneyline, or a team whose Kalshi
    abbreviation doesn't align with ESPN's — those just skip rather than
    mismatch).

    This is the "de-vigged consensus/closing line" benchmark from
    ``docs/prior-art-survey.md`` candidate #1 — the sharpest cheap baseline
    for baseball. Consumed by the close-game scalper as
    ``--forecast-source consensus``.
    """
    parsed = _parse_kalshi_mlb_ticker(ticker)
    if parsed is None:
        return None
    _date_iso, matchup, yes_team = parsed
    try:
        events = sports_data.get_events_with_probabilities(league)
    except Exception as e:  # noqa: BLE001
        log.warning("sports consensus fetch failed: %s", e)
        return None
    for ev in events:
        if ev.state == "post" or ev.home_winprob is None or ev.away_winprob is None:
            continue
        h = (ev.home.abbreviation or "").upper()
        a = (ev.away.abbreviation or "").upper()
        if not (h and a and h in matchup and a in matchup):
            continue
        if yes_team == h:
            return float(ev.home_winprob)
        if yes_team == a:
            return float(ev.away_winprob)
    return None


def _baseball_games_for_date(date_iso: str) -> list[dict]:
    """Look up games for a specific date directly from sidecar state.
    Returns ``[]`` if the sidecar isn't loaded for that date — caller
    should treat that as "no edge available" rather than retry. Date
    coverage is governed by how many days the runner refreshes."""
    try:
        import baseball.state as _bstate
    except ImportError:
        return []
    try:
        return _bstate.get_games_for_date(date_iso)
    except Exception as e:  # noqa: BLE001
        log.warning("baseball.state.get_games_for_date(%s) failed: %s", date_iso, e)
        return []


def _match_baseball_game(
    question: str,
    games: list[dict],
    *,
    market_id: str = "",
) -> tuple[dict, str] | None:
    """Return ``(game, yes_side)`` where yes_side ∈ {"home","away"} if
    we can confidently match the market to exactly one game.

    Two paths:

    1. **Kalshi MLB ticker** — explicit YES team and date in the ticker.
       Date is decoded from the ticker; we look up games on *that* day
       (so we don't accidentally match the same team's game on a
       different day, which would silently use the wrong matchup). YES
       team's abbreviation tells us which side is YES.

    2. **Generic alias fallback** — for venues that don't use the Kalshi
       MLB ticker shape (Polymarket, future venues). The team appearing
       first in the question text is YES, same convention as
       ``find_sports_edges``.

    Skips derivative-market keywords (O/U total runs, run-line spreads,
    first-to-N, props). Ambiguous matches return None rather than guess.
    """
    if _DERIVATIVE_KEYWORDS.search(question):
        return None

    # Path 1: Kalshi MLB ticker — fully unambiguous
    parsed = _parse_kalshi_mlb_ticker(market_id or "")
    if parsed is not None:
        date_iso, matchup_str, yes_abbr = parsed
        # Pull games for the exact date the ticker references. If the
        # sidecar hasn't loaded that date yet (date outside the runner's
        # refresh window), we get []. That's the honest failure mode —
        # better than matching a wrong same-team game on a different day.
        date_games = _baseball_games_for_date(date_iso)
        for g in date_games:
            home = str(g.get("home_abbr", "")).upper()
            away = str(g.get("away_abbr", "")).upper()
            # Require BOTH teams from the schedule to appear in the
            # ticker's matchup segment. Otherwise a team's day with two
            # games (rare, but happens — doubleheader, or odd MLB API
            # date assignment) can falsely match the wrong opponent.
            if not (home in matchup_str and away in matchup_str):
                continue
            if home == yes_abbr:
                return g, "home"
            if away == yes_abbr:
                return g, "away"
        return None

    # Path 2: generic alias fallback (live schedule context only)
    candidates: list[tuple[dict, str]] = []
    for g in games:
        home_aliases = _baseball_team_aliases(
            g.get("home_abbr", ""), g.get("home_name", ""),
        )
        away_aliases = _baseball_team_aliases(
            g.get("away_abbr", ""), g.get("away_name", ""),
        )
        home_idx = _find_first_alias(question, home_aliases)
        away_idx = _find_first_alias(question, away_aliases)
        if home_idx is None or away_idx is None:
            continue
        yes_side = "home" if home_idx < away_idx else "away"
        candidates.append((g, yes_side))

    if len(candidates) != 1:
        return None
    return candidates[0]


def _baseball_winprob_for_side(game: dict, side: str) -> float | None:
    """The model's P(side wins) for the matched game. Pulls the stored
    prediction first; falls back to on-demand compute via
    ``baseball.predict_game``. Returns None if no prediction can be
    produced (rare — usually means team_season stats haven't been refreshed)."""
    home_p = game.get("home_win_prob")
    if home_p is None:
        # Lazy import — must NOT be at module top, since users may have
        # the baseball sidecar disabled / not initialized. Failing this
        # import shouldn't poison the whole edges module.
        try:
            import baseball as _baseball
            pred = _baseball.predict_game(
                home=str(game.get("home_abbr", "")),
                away=str(game.get("away_abbr", "")),
            )
        except ImportError:
            log.debug("baseball sidecar not importable; skipping")
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("baseball.predict_game failed: %s", e)
            return None
        if pred is None:
            return None
        home_p = pred.get("home_win_prob")
    if home_p is None:
        return None
    return float(home_p) if side == "home" else 1.0 - float(home_p)


def _baseball_today_schedule() -> list[dict]:
    """Wraps baseball.today_schedule() so an unconfigured sidecar (no DB
    yet) doesn't take the whole edges run down. Returns [] on any error."""
    try:
        import baseball as _baseball
    except ImportError:
        return []
    try:
        return _baseball.today_schedule()
    except Exception as e:  # noqa: BLE001
        log.warning("baseball.today_schedule() failed: %s", e)
        return []


def find_baseball_edges(
    df_markets: pd.DataFrame,
    *,
    min_gap: float = 0.05,
    min_days_to_expiry: float = 0.0,
    progress_callback: Any = None,
) -> list[Edge]:
    """Scan MLB markets for our model's disagreement with the venue YES
    price. Same matching shape as ``find_sports_edges``; the difference
    is that here ``model_p`` is the baseball sidecar's prediction, not
    ESPN's consensus.

    Markets with no probable starters (early-week lookahead, bullpen
    games) still get edges — the model defaults to league-average SP for
    the missing side, which is the right thing to do (no SP info means
    no SP-driven edge claim either direction).
    """
    if df_markets is None or df_markets.empty:
        return []

    games = _baseball_today_schedule()
    # Drop finalized games — model edge has no meaning post-resolution.
    games = [g for g in games if g.get("status") != "Final"]
    if not games:
        return []

    now = datetime.now(UTC)
    edges_out: list[Edge] = []
    total = len(df_markets)

    for j, (_, row) in enumerate(df_markets.iterrows()):
        if progress_callback is not None:
            try:
                progress_callback(j, total)
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

        match = _match_baseball_game(snap.question, games, market_id=snap.condition_id)
        if match is None:
            continue
        game, yes_side = match
        model_p = _baseball_winprob_for_side(game, yes_side)
        if model_p is None:
            continue

        gap = model_p - market_p
        if abs(gap) < min_gap:
            continue

        yes_team = (game.get("home_name") if yes_side == "home"
                    else game.get("away_name")) or "?"
        away_pitcher = game.get("away_pitcher_name") or "TBD"
        home_pitcher = game.get("home_pitcher_name") or "TBD"
        note = (
            f"MLB: {game.get('away_abbr','?')} @ {game.get('home_abbr','?')} "
            f"({away_pitcher} vs {home_pitcher}); YES={yes_team}; "
            f"model {model_p*100:.1f}% (baseball sidecar v0.1)"
        )
        edges_out.append(Edge(
            source="baseball",
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


def strategy_baseball_winprob(
    snap: bt.MarketSnapshot,
    *,
    min_gap: float = 0.05,
) -> bt.Signal | None:
    """Backtest-compatible signal generator. Same matching as
    :func:`find_baseball_edges` but for one snapshot at a time.

    **Backtest caveat** (same as ``strategy_sports_winprob``): the
    sidecar's state only contains live + recent predictions. Historical
    walk-forward needs a replay layer over MLB's archived schedule
    endpoints, which isn't built yet. Until then, this strategy returns
    None for any market whose game isn't in today's slate.
    """
    market_p = snap.latest_price
    if market_p is None:
        return None

    # For Kalshi MLB tickers, the matcher looks up games by ticker-date
    # directly — no need for today's-schedule. For non-Kalshi markets the
    # alias fallback path uses today's games. Pass both; don't early-return
    # on empty today_schedule because the ticker path doesn't need it.
    games = [g for g in _baseball_today_schedule() if g.get("status") != "Final"]

    match = _match_baseball_game(snap.question, games, market_id=snap.condition_id)
    if match is None:
        return None
    game, yes_side = match
    model_p = _baseball_winprob_for_side(game, yes_side)
    if model_p is None:
        return None

    gap = model_p - market_p
    if abs(gap) < min_gap:
        return None
    return bt.Signal(direction="YES" if gap > 0 else "NO", probability=model_p)


# ---------------------------------------------------------------------------
# WNBA source — the 2nd sport sidecar (basketball Pythagorean model)
# ---------------------------------------------------------------------------
#
# Runs in parallel to find_sports_edges (ESPN consensus) on the same WNBA
# games, so the Calibration page gets head-to-head Brier (our model vs
# consensus). The ticker bridge lives in the sidecar (wnba.predict_for_
# ticker), so this adapter is thin — it just maps a Kalshi ticker to the
# model's P(YES) and compares to the venue price. Mirrors the baseball
# adapter; see docs/sports-model-playbook.md §6.
