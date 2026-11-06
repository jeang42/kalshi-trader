"""Manifold community-probability edges. Matching is direction-blind; every
flag needs eyeballing.

Split out of `edges.py` (P3/R-24, 2026-08-24). Re-exported by
`edges/__init__.py`, so every public name here is still reachable as
`edges.<name>` exactly as before.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from pm.feeds import llm_matcher, manifold_data
from pm.research import backtest as bt

from ._base import UTC, Edge, _row_to_snapshot, _severity_for_gap


def strategy_manifold_community(
    snap: bt.MarketSnapshot,
    *,
    min_gap: float = 0.10,
    min_similarity: float = 0.30,
    min_volume: float = 100.0,
    min_days_to_expiry: float = 1.0,
    llm_verify: bool = False,
    llm_verify_model: str = "llama3.1:latest",
    llm_min_confidence: float = 0.6,
) -> bt.Signal | None:
    """Tier-3b strategy hook for the Manifold source.

    **Caveat for backtesting**: Manifold's `/search-markets` is a *live*
    endpoint — it returns the current community probability, not the
    probability as of ``snap.as_of``. Historical predictions are
    available per-market via a separate API but the search-based
    matching loop here doesn't use them, so backtest Brier reflects
    "if today's Manifold community had been asked this when the market
    was open" — not strictly faithful. The strategy is registered so
    the gate exists; honest backtest interpretation needs an
    archival-prediction follow-up.
    """
    market_p = snap.latest_price
    if market_p is None:
        return None

    if snap.end_date is not None:
        days = (snap.end_date - snap.as_of).total_seconds() / 86_400.0
        if days < min_days_to_expiry:
            return None

    candidates = manifold_data.search_markets(snap.question, limit=5)
    candidates = [m for m in candidates if not m.is_resolved and m.volume >= min_volume]
    match = manifold_data.best_match(snap.question, candidates)
    if match is None:
        return None
    market, similarity = match
    if similarity < min_similarity:
        return None

    # Optional LLM verification — catches the direction-blindness
    # failure mode Jaccard can't detect.
    if llm_verify:
        verdict = llm_matcher.verify_match(
            snap.question, market.question, model=llm_verify_model,
        )
        if verdict is None:
            return None  # LLM unreachable / unparseable → don't fabricate
        if not verdict.same_question:
            return None
        if verdict.confidence < llm_min_confidence:
            return None

    gap = market.probability - market_p
    if abs(gap) < min_gap:
        return None
    return bt.Signal(direction="YES" if gap > 0 else "NO",
                     probability=market.probability)


def find_manifold_edges(
    df_markets: pd.DataFrame,
    *,
    min_gap: float = 0.10,
    min_similarity: float = 0.30,
    min_volume: float = 100.0,
    max_markets: int = 30,
    min_days_to_expiry: float = 1.0,
    llm_verify: bool = False,
    llm_verify_model: str = "llama3.1:latest",
    llm_min_confidence: float = 0.6,
    progress_callback: Any = None,
) -> list[Edge]:
    """Cross-platform edge scan against Manifold.

    Slow — one HTTP search per scanned venue market. Default
    ``max_markets=30`` keeps a fresh run under a minute. Sorts by
    ``volume_24hr`` so the most-liquid (least-noisy) venue markets get
    scored first.

    ``min_similarity`` is the Jaccard floor on the question-vs-best-
    match comparison. 0.30 is permissive but catches most genuine
    matches; lower it if you're losing legitimate hits, raise it if
    you're getting noise.

    ``min_volume`` (Manifold-side mana) filters out thin Manifold
    markets where the community probability is dominated by one or
    two traders. 100 is a sane default; tune in the UI.
    """
    if df_markets is None or df_markets.empty:
        return []

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

        candidates = manifold_data.search_markets(snap.question, limit=5)
        candidates = [m for m in candidates
                      if not m.is_resolved and m.volume >= min_volume]
        match = manifold_data.best_match(snap.question, candidates)
        if match is None:
            continue
        manifold_market, similarity = match
        if similarity < min_similarity:
            continue

        # Optional LLM verification — catches the direction-blindness
        # failure mode Jaccard can't detect. Done before the gap check
        # so we don't waste an LLM call on candidates that wouldn't
        # produce an edge anyway... actually do it AFTER the gap check
        # for the same reason in reverse: many candidates produce no
        # gap and don't need verification at all.
        verdict_summary = ""
        gap = manifold_market.probability - market_p
        if abs(gap) < min_gap:
            continue

        if llm_verify:
            verdict = llm_matcher.verify_match(
                snap.question, manifold_market.question,
                model=llm_verify_model,
            )
            if verdict is None:
                continue  # LLM unreachable — refuse to emit a flag
            if not verdict.same_question:
                continue
            if verdict.confidence < llm_min_confidence:
                continue
            verdict_summary = (
                f" | LLM-verified ({verdict.model}, conf "
                f"{verdict.confidence:.2f}): {verdict.reasoning[:120]}"
            )

        note = (
            f"Manifold: \"{manifold_market.question[:80]}\" "
            f"(prob {manifold_market.probability*100:.1f}%, "
            f"vol ${manifold_market.volume:,.0f}, "
            f"similarity {similarity:.2f}){verdict_summary}"
        )
        edges_out.append(Edge(
            source="manifold",
            market_id=snap.condition_id,
            question=snap.question,
            category=snap.category,
            market_yes_price=float(market_p),
            model_implied=float(manifold_market.probability),
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
# Bulk logging into the Calibration page's forecast store
# ---------------------------------------------------------------------------
