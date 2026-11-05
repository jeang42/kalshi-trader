"""WNBA edges from the wnba sidecar. The ticker suffix IS the yes team.

Split out of `edges.py` (P3/R-24, 2026-08-24). Re-exported by
`edges/__init__.py`, so every public name here is still reachable as
`edges.<name>` exactly as before.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from pm.research import backtest as bt

from ._base import UTC, Edge, _row_to_snapshot, _severity_for_gap, log


def _wnba_yes_prob_for_ticker(ticker: str) -> float | None:
    """The WNBA sidecar's P(YES) for a Kalshi ticker. Sidecar's only
    market-app touchpoint; returns None when not a WNBA ticker or the game
    isn't in sidecar state."""
    try:
        import wnba as _wnba
        r = _wnba.predict_for_ticker(ticker)
        return float(r["yes_prob"]) if r else None
    except Exception as e:  # noqa: BLE001
        log.warning("wnba.predict_for_ticker failed: %s", e)
        return None


def find_wnba_edges(
    df_markets: pd.DataFrame,
    *,
    min_gap: float = 0.05,
    min_days_to_expiry: float = 0.0,
    progress_callback: Any = None,
) -> list[Edge]:
    """Scan WNBA markets for the sidecar model's disagreement with the
    venue YES price. ``model_p`` is the WNBA Pythagorean model's
    prediction; matching is by Kalshi ticker via the sidecar's bridge."""
    if df_markets is None or df_markets.empty:
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

        model_p = _wnba_yes_prob_for_ticker(str(snap.condition_id))
        if model_p is None:
            continue

        gap = model_p - market_p
        if abs(gap) < min_gap:
            continue

        note = (f"WNBA model (sidecar v0.1): {snap.question[:60]}; "
                f"model {model_p*100:.1f}% vs market {market_p*100:.1f}%")
        edges_out.append(Edge(
            source="wnba",
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


def strategy_wnba_winprob(
    snap: bt.MarketSnapshot,
    *,
    min_gap: float = 0.05,
) -> bt.Signal | None:
    """Backtest-compatible signal generator for one snapshot. Same caveat
    as ``strategy_baseball_winprob``: sidecar state holds live + recent
    predictions only, so historical walk-forward returns None until a
    schedule-replay layer exists."""
    market_p = snap.latest_price
    if market_p is None:
        return None
    model_p = _wnba_yes_prob_for_ticker(str(snap.condition_id))
    if model_p is None:
        return None
    gap = model_p - market_p
    if abs(gap) < min_gap:
        return None
    return bt.Signal(direction="YES" if gap > 0 else "NO", probability=model_p)


def strategy_wnba_favorite_hold(
    snap: bt.MarketSnapshot,
    *,
    fav_min: float = 0.55,
    fav_max: float = 0.70,
) -> bt.Signal | None:
    """Favorite-hold for WNBA — the favorite-longshot-bias bet that was the
    only positive-EV thread in the baseball data crunch (see
    ``docs/strategies.md`` §17 and ``docs/glossary.md`` 'favorite-longshot
    bias'). Buy the side the model rates a **moderate favorite** (P(YES) in
    ``[fav_min, fav_max]``) and hold to resolution — no edge-vs-market gate,
    no scalp, no flip. The thesis is that the market underprices favorites
    generically, not that we find per-game mispricings.

    Returns a YES signal (buy this side) only when the model's P(YES) for
    the ticker sits in the favorite band; otherwise None. The strategy
    runner holds to resolution by default (no ExitPolicy)."""
    model_p = _wnba_yes_prob_for_ticker(str(snap.condition_id))
    if model_p is None:
        return None
    if fav_min <= model_p <= fav_max:
        return bt.Signal(direction="YES", probability=model_p)
    return None


# ---------------------------------------------------------------------------
# Tennis source — the 3rd sidecar (Elo, individual sport)
# ---------------------------------------------------------------------------
#
# Runs parallel to find_sports_edges for head-to-head Brier. The ticker
# bridge needs the market QUESTION (the YES player's full name lives there,
# not in the 3-letter ticker code), so the adapter passes snap.question to
# the sidecar. See docs/sports-model-playbook.md.


