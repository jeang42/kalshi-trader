"""Macro edges — FRED series against a normal-on-level model.

Split out of `edges.py` (P3/R-24, 2026-08-24). Re-exported by
`edges/__init__.py`, so every public name here is still reachable as
`edges.<name>` exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from pm.feeds import macro_data
from pm.research import backtest as bt

from ._base import UTC, Edge, _row_to_snapshot, _severity_for_gap


@dataclass(frozen=True)
class ParsedMacroQuestion:
    series_key: str        # registry key in macro_data.SERIES
    threshold: float       # interpreted in the series' native units (%)
    direction: str         # "above" | "below"
    deadline: datetime


# Series-key detection. Keys are FRED series; values are regex patterns
# tried in order against the lowercased question. First match wins.
_SERIES_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("unemployment", re.compile(r"\b(unemployment|jobless\s+rate|u-?3\b)\b")),
    ("10y_treasury", re.compile(
        r"\b(10[-\s]?year|10[-\s]?yr|10y)\b.*\b(treasury|yield|rate|tnote|t-note|note)\b"
        r"|\b(treasury|yield|rate)\b.*\b(10[-\s]?year|10[-\s]?yr|10y)\b")),
    ("2y_treasury", re.compile(
        r"\b(2[-\s]?year|2[-\s]?yr|2y)\b.*\b(treasury|yield|rate|tnote|t-note|note)\b"
        r"|\b(treasury|yield|rate)\b.*\b(2[-\s]?year|2[-\s]?yr|2y)\b")),
    ("fed_funds", re.compile(
        r"\b(fed\s+funds|federal\s+funds|fed\s+rate|fed\s+funds\s+rate)\b")),
]

# Threshold + direction patterns. We look for an "above/below/exceed/..."
# preposition immediately before a number with optional "%".
_MACRO_AMOUNT_PATTERN = re.compile(
    r"(above|below|under|over|exceed|reach|hit|surpass|cross|"
    r"top|drop\s+to|fall\s+to|fall\s+below|rise\s+to|rise\s+above)\s+"
    r"\$?([\d.]+)\s*(?:%|pct|percent)?",
    re.IGNORECASE,
)

_MACRO_ABOVE_VERBS = {"above", "over", "exceed", "reach", "hit",
                      "surpass", "cross", "top", "rise to", "rise above"}


def parse_macro_question(
    question: str, *, deadline: datetime | None,
) -> ParsedMacroQuestion | None:
    """Best-effort macro-threshold parser. Returns None when the question
    can't be confidently classified.

    >>> from datetime import datetime, timezone
    >>> d = datetime(2026, 8, 1, tzinfo=timezone.utc)
    >>> q = parse_macro_question("Will unemployment exceed 5% by Aug 2026?",
    ...                            deadline=d)
    >>> q.series_key, q.direction, q.threshold
    ('unemployment', 'above', 5.0)

    >>> q = parse_macro_question("Will the 10-year Treasury yield fall below 3.5% by July?",
    ...                            deadline=d)
    >>> q.series_key, q.direction, q.threshold
    ('10y_treasury', 'below', 3.5)

    >>> parse_macro_question("Will Bitcoin reach $200k?", deadline=d) is None
    True
    """
    if deadline is None:
        return None
    q_lower = question.lower()

    series_key: str | None = None
    for key, pat in _SERIES_PATTERNS:
        if pat.search(q_lower):
            series_key = key
            break
    if series_key is None:
        return None

    m = _MACRO_AMOUNT_PATTERN.search(q_lower)
    if not m:
        return None
    word = m.group(1).lower().strip()
    try:
        threshold = float(m.group(2))
    except ValueError:
        return None
    if threshold <= 0:
        return None

    direction = "above" if word in _MACRO_ABOVE_VERBS else "below"
    return ParsedMacroQuestion(
        series_key=series_key, threshold=threshold,
        direction=direction, deadline=deadline,
    )


def _macro_model_prob(
    parsed: ParsedMacroQuestion, *, as_of: datetime | None = None,
) -> tuple[float | None, str]:
    """Probability + a single-line explanation note. Returns (None, reason)
    when the model can't compute one.

    ``as_of`` controls the "current time" used for the level lookup, the
    realized-vol window, and the days-to-deadline calculation. Defaults
    to ``now`` for live edge surfacing; backtest callers pass
    ``snap.as_of`` so the model only sees data that would have been
    known at the snapshot's evaluation time.
    """
    meta = macro_data.SERIES.get(parsed.series_key)
    if meta is None:
        return (None, "unsupported series")

    eval_time = as_of if as_of is not None else datetime.now(UTC)
    if as_of is None:
        latest = macro_data.latest_value(meta.series_id)
    else:
        latest = macro_data.value_at(meta.series_id, eval_time)
    if latest is None:
        return (None, "no recent observations")
    level, observed_at = latest

    if as_of is None:
        vol = macro_data.realized_volatility_for(parsed.series_key, window=24)
    else:
        vol = macro_data.realized_volatility_at(
            parsed.series_key, eval_time, window=24,
        )
    if vol is None:
        return (None, "insufficient history for vol")

    days = (parsed.deadline - eval_time).total_seconds() / 86_400.0
    if days <= 0:
        return (None, "deadline already passed")

    if parsed.direction == "above":
        p = macro_data.normal_above(level, parsed.threshold, vol, days)
    else:
        p = macro_data.normal_below(level, parsed.threshold, vol, days)
    if p is None:
        return (None, "model rejected inputs")

    note = (
        f"{meta.series_id} latest {level:.2f}% on {observed_at.date()}; "
        f"threshold {parsed.threshold:.2f}% {parsed.direction}; "
        f"~{days:.0f}d to deadline; vol {vol:.2f}/yr (normal-on-level)"
    )
    return (p, note)


def strategy_macro_threshold(
    snap: bt.MarketSnapshot,
    *,
    min_gap: float = 0.10,
    min_days_to_expiry: float = 1.0,
) -> bt.Signal | None:
    """Tier-3b strategy hook for macro-threshold questions.

    Uses ``macro_data.value_at(snap.as_of)`` and
    ``macro_data.realized_volatility_at(snap.as_of)`` so the model only
    sees observations that would have been published before the
    snapshot's evaluation time. The small remaining lookahead — FRED's
    ``observation_date`` is the data point's date-of-applicability, not
    its publication date — is documented on ``value_at`` itself.
    """
    if snap.end_date is None:
        return None
    parsed = parse_macro_question(snap.question, deadline=snap.end_date)
    if parsed is None:
        return None

    days = (parsed.deadline - snap.as_of).total_seconds() / 86_400.0
    if days < min_days_to_expiry:
        return None

    model_p, _note = _macro_model_prob(parsed, as_of=snap.as_of)
    if model_p is None:
        return None

    market_p = snap.latest_price
    if market_p is None:
        return None
    gap = model_p - market_p
    if abs(gap) < min_gap:
        return None
    return bt.Signal(direction="YES" if gap > 0 else "NO", probability=model_p)


def find_macro_edges(
    df_markets: pd.DataFrame,
    *,
    min_gap: float = 0.10,
    min_days_to_expiry: float = 1.0,
) -> list[Edge]:
    """Scan venue markets for macro-threshold questions where the FRED-
    derived normal-on-level probability disagrees with the market by
    ``min_gap`` or more.
    """
    if df_markets is None or df_markets.empty:
        return []

    now = datetime.now(UTC)
    edges_out: list[Edge] = []

    for _, row in df_markets.iterrows():
        snap = _row_to_snapshot(row, as_of=now)
        if snap.end_date is None:
            continue
        parsed = parse_macro_question(snap.question, deadline=snap.end_date)
        if parsed is None:
            continue
        days = (snap.end_date - now).total_seconds() / 86_400.0
        if days < min_days_to_expiry:
            continue

        market_p = snap.latest_price
        if market_p is None:
            continue
        model_p, note = _macro_model_prob(parsed)
        if model_p is None:
            continue

        gap = model_p - market_p
        if abs(gap) < min_gap:
            continue

        edges_out.append(Edge(
            source="macro",
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

    edges_out.sort(key=lambda e: abs(e.gap), reverse=True)
    return edges_out


# ---------------------------------------------------------------------------
# Manifold Markets cross-platform comparison (the "politics" Tier-4 wedge,
# but works for any category Manifold covers)
# ---------------------------------------------------------------------------
#
# Premise: when the active venue's YES price disagrees with Manifold's
# community probability by a wide margin on the *same* underlying
# question, one of them is mispriced. Manifold's smaller, more-engaged
# community often prices in news faster than Polymarket's larger but
# more passive crowd.
#
# Matching workhorse:
#   1. Search Manifold's `/v0/search-markets` with the venue question
#      as the query.
#   2. Pick the top binary result by Manifold's relevance ranking.
#   3. Compute a Jaccard token-similarity floor between the two
#      questions. Anything below the floor we treat as "search returned
#      noise" and skip.
#   4. If the surviving match's probability disagrees with the venue
#      YES by ``min_gap`` or more, emit an Edge.
