"""Tennis edges from the tennis sidecar. The bridge matches the YES player's
full name from the question, never the 3-letter code.

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


def _tennis_yes_prob(ticker: str, question: str) -> float | None:
    """WNBA-style thin adapter: the tennis sidecar's P(YES) for a Kalshi
    tennis ticker. None when not a tennis ticker or a player isn't rated."""
    try:
        import tennis as _tennis
        r = _tennis.predict_for_ticker(ticker, question)
        return float(r["yes_prob"]) if r else None
    except Exception as e:  # noqa: BLE001
        log.warning("tennis.predict_for_ticker failed: %s", e)
        return None


def find_tennis_edges(
    df_markets: pd.DataFrame,
    *,
    min_gap: float = 0.05,
    min_days_to_expiry: float = 0.0,
    progress_callback: Any = None,
) -> list[Edge]:
    """Scan tennis markets for the Elo model's disagreement with the venue
    price. ``model_p`` is the tennis sidecar's Elo P(YES)."""
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
        model_p = _tennis_yes_prob(str(snap.condition_id), str(snap.question))
        if model_p is None:
            continue
        gap = model_p - market_p
        if abs(gap) < min_gap:
            continue
        note = (f"Tennis Elo (sidecar v0.1): {snap.question[:60]}; "
                f"model {model_p*100:.1f}% vs market {market_p*100:.1f}%")
        edges_out.append(Edge(
            source="tennis", market_id=snap.condition_id, question=snap.question,
            category=snap.category, market_yes_price=float(market_p),
            model_implied=float(model_p), gap=float(gap),
            direction="YES" if gap > 0 else "NO",
            severity=_severity_for_gap(abs(gap)), note=note,
        ))
    if progress_callback is not None:
        try:
            progress_callback(total, total)
        except Exception:
            pass
    edges_out.sort(key=lambda e: abs(e.gap), reverse=True)
    return edges_out


def strategy_tennis_winprob(
    snap: bt.MarketSnapshot,
    *,
    min_gap: float = 0.05,
) -> bt.Signal | None:
    """Backtest-compatible signal generator. Same sidecar-state caveat as
    the other sport adapters — live ratings only, no historical replay."""
    market_p = snap.latest_price
    if market_p is None:
        return None
    model_p = _tennis_yes_prob(str(snap.condition_id), str(snap.question))
    if model_p is None:
        return None
    gap = model_p - market_p
    if abs(gap) < min_gap:
        return None
    return bt.Signal(direction="YES" if gap > 0 else "NO", probability=model_p)


def _tennis_pred(snap: bt.MarketSnapshot) -> dict | None:
    """Full tennis prediction dict (incl. match counts + ranks) for a
    snapshot, or None. Used by the value strategy's guardrail."""
    try:
        import tennis as _tennis
        return _tennis.predict_for_ticker(str(snap.condition_id), str(snap.question))
    except Exception as e:  # noqa: BLE001
        log.warning("tennis.predict_for_ticker failed: %s", e)
        return None


# Guardrail constants for the value strategy (see docstring).
_TENNIS_MIN_MATCHES = 15      # below this, a player's Elo is small-sample noise
_TENNIS_RANK_TRUST = 10       # don't fade a clear top-10 player on Elo alone
_TENNIS_RANK_GAP = 20         # ...or back a player ranked this much WORSE than
                              #    the favorite they'd fade (K-decay tell)


def strategy_tennis_value(
    snap: bt.MarketSnapshot,
    *,
    min_gap: float = 0.08,
    max_position_prob: float = 0.90,
) -> bt.Signal | None:
    """Tennis VALUE arm — bet the side the Elo model thinks is underpriced
    by ≥ ``min_gap`` vs the market, hold to resolution.

    Unlike WNBA favorite-hold, tennis edges are large and two-directional
    (the Elo model often disagrees with the book on the underdog), so the
    bet is "back the model's mispriced side", not "back favorites".

    **K-decay guardrail (load-bearing).** The Elo model over-rates hot
    small-sample players (a young player on a long win streak can rate near
    a true top-5), which manufactures fake edges — exactly the model's
    *worst* calls. So we REFUSE the bet when, on the side the model wants
    to back: that player has < ``_TENNIS_MIN_MATCHES`` rated matches
    (unstable Elo), OR we'd be *fading* a top-``_TENNIS_RANK_TRUST`` ranked
    opponent purely on an Elo edge while the backed side isn't itself
    comparably ranked. Also skip heavy favorites (> ``max_position_prob``)
    where there's no room / the fee drag dominates.

    Returns the YES/NO signal for the model-favored side, or None."""
    market_p = snap.latest_price
    if market_p is None:
        return None
    pred = _tennis_pred(snap)
    if pred is None:
        return None
    model_p = pred["yes_prob"]
    gap = model_p - market_p
    if abs(gap) < min_gap:
        return None

    backing_yes = gap > 0
    if backing_yes:
        back_matches, back_rank = pred.get("yes_matches"), pred.get("yes_rank")
        fade_rank = pred.get("opp_rank")
        if market_p > max_position_prob:
            return None
    else:
        back_matches, back_rank = pred.get("opp_matches"), pred.get("opp_rank")
        fade_rank = pred.get("yes_rank")
        if (1 - market_p) > max_position_prob:
            return None

    # Guard 1: the backed player's Elo must rest on enough matches.
    if back_matches is not None and back_matches < _TENNIS_MIN_MATCHES:
        return None
    # Guard 2: don't fade a clear top-N player on Elo alone.
    if (fade_rank is not None and fade_rank <= _TENNIS_RANK_TRUST
            and (back_rank is None or back_rank > _TENNIS_RANK_TRUST)):
        return None
    # Guard 3 (K-decay tell): refuse backing a much-lower-ranked player
    # against a much-higher-ranked favorite — that's where the Elo model's
    # hot-streak inflation manufactures fake edges (e.g. #29 Elo ≈ #3). An
    # unranked backed side (rank None) against a ranked fade is also refused.
    if fade_rank is not None:
        if back_rank is None or (back_rank - fade_rank) >= _TENNIS_RANK_GAP:
            return None

    return bt.Signal(direction="YES" if backing_yes else "NO",
                     probability=model_p)


# ---------------------------------------------------------------------------
# Macro source — FRED level series vs. venue threshold questions
# ---------------------------------------------------------------------------
#
# Tier-4 source backed by St. Louis Fed's free public data. Supports
# threshold questions on unemployment, 2y/10y Treasury yields, and the
# Fed funds effective rate. Modeling uses a normal-on-level
# distribution with realized vol — wrong in detail (rates aren't
# Brownian) but right in order-of-magnitude for short horizons. CPI YoY
# and GDP growth need transformations not yet implemented; their
# questions are skipped.


