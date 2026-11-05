"""Local-LLM edges — Ollama forecasts a market, the gap against the venue
price becomes an edge. No paid API.

Split out of `edges.py` (P3/R-24, 2026-08-24). Re-exported by
`edges/__init__.py`, so every public name here is still reachable as
`edges.<name>` exactly as before.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from pm.feeds import llm_data
from pm.research import backtest as bt

from ._base import UTC, Edge, _row_to_snapshot, _severity_for_gap


def strategy_llm_forecast(
    snap: bt.MarketSnapshot,
    *,
    model: str = "llama3.1:latest",
    min_gap: float = 0.10,
    min_days_to_expiry: float = 1.0,
) -> bt.Signal | None:
    """Ask the local LLM for a YES probability and emit a Kelly-eligible
    signal when it disagrees with the market by at least ``min_gap``.

    Slow: one HTTP call per snapshot, latency dominated by model size.
    Cache the dataset on the Backtest page; this strategy is best
    measured at small N (≤50 markets) until you trust it.
    """
    market_p = snap.latest_price
    if market_p is None:
        return None

    if snap.end_date is not None:
        days = (snap.end_date - snap.as_of).total_seconds() / 86_400.0
        if days < min_days_to_expiry:
            return None
    else:
        days = None

    forecast = llm_data.get_forecast(
        question=snap.question,
        model=model,
        category=snap.category or None,
        market_yes_price=market_p,
        days_to_deadline=days,
        as_of=snap.as_of,
    )
    if forecast is None:
        return None

    gap = forecast.probability - market_p
    if abs(gap) < min_gap:
        return None
    return bt.Signal(direction="YES" if gap > 0 else "NO",
                     probability=forecast.probability)


def find_llm_edges(
    df_markets: pd.DataFrame,
    *,
    min_gap: float = 0.10,
    model: str = "llama3.1:latest",
    max_markets: int = 25,
    min_days_to_expiry: float = 1.0,
    progress_callback: Any = None,
) -> list[Edge]:
    """Score up to ``max_markets`` from the active venue with the local
    LLM and emit edges where its probability disagrees with the market
    by at least ``min_gap``.

    Slow — one HTTP call per market. Default ``max_markets=25`` keeps a
    fresh scan under a minute on small models. Sort by volume_24hr so
    we score the most-liquid markets first; thin markets are noise.
    """
    if df_markets is None or df_markets.empty:
        return []

    # Most-liquid first so we don't burn LLM time on dead markets.
    df = df_markets.copy()
    if "volume_24hr" in df.columns:
        df = df.sort_values("volume_24hr", ascending=False)
    df = df.head(max_markets)

    now = datetime.now(UTC)
    edges_out: list[Edge] = []
    total = len(df)

    for i, (_, row) in enumerate(df.iterrows()):
        if progress_callback is not None:
            try:
                progress_callback(i, total)
            except Exception:
                pass

        snap = _row_to_snapshot(row, as_of=now)
        market_p = snap.latest_price
        if market_p is None:
            continue
        if snap.end_date is not None:
            days = (snap.end_date - now).total_seconds() / 86_400.0
            if days < min_days_to_expiry:
                continue
        else:
            days = None

        forecast = llm_data.get_forecast(
            question=snap.question,
            model=model,
            category=snap.category or None,
            market_yes_price=market_p,
            days_to_deadline=days,
            as_of=now,
        )
        if forecast is None:
            continue

        gap = forecast.probability - market_p
        if abs(gap) < min_gap:
            continue

        note = (
            f"{model}: {forecast.reasoning[:200]}"
            if forecast.reasoning else f"{model}"
        )
        edges_out.append(Edge(
            source="llm",
            market_id=snap.condition_id,
            question=snap.question,
            category=snap.category,
            market_yes_price=float(market_p),
            model_implied=float(forecast.probability),
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


# ---------------------------------------------------------------------------
# Sports source — ESPN consensus-betting-market vs venue YES price
# ---------------------------------------------------------------------------
#
# Match Polymarket sports markets to ESPN scheduled events by team name,
# then compare the venue YES price to ESPN's pickcenter-derived fair win
# probability (moneyline → implied → vig-removed). The premise: when
# Polymarket prices a game's outcome out of line with the consensus
# sportsbook market, one of them is lagging news the other has priced
# in.
#
# The Polymarket convention we assume: in a question like "Team A vs.
# Team B" the YES side is Team A (the first team in the question
# title). When the question can't be cleanly classified this way we
# skip rather than guess.


