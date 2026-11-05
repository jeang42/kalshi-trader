"""Manifold Markets community-forecast data layer.

Why Manifold, not Metaculus or 538/RCP for the "politics" Tier-4 source:

- **538 was shut down** by Disney in March 2025. The site is archived but
  no longer maintained — useful for backtesting historical elections,
  useless for live forecasting.
- **RCP polling** is paywalled.
- **Metaculus** now requires API authentication (`api.metaculus.com`
  returns HTTP 403 to unauthenticated requests as of Q1 2025).
- **Manifold Markets** has a free, no-auth public API with natural-
  language search and direct binary probabilities. Their community
  is a calibrated forecasting crowd similar to Metaculus, with heavy
  politics coverage plus geopolitics, science, etc. as bonus.

This is honestly a **cross-platform comparison**, not a polling
aggregator: we're comparing one prediction market's price to another
prediction market's community probability. When the two disagree by
a wide margin, *one* of them is mispriced — often Polymarket lagging
news that Manifold's smaller, more-engaged community priced in faster.

Endpoints used:
- ``GET /v0/search-markets`` — natural-language search with relevance
  ranking. The matching workhorse.
- ``GET /v0/market/{id}`` — fetch one market's full detail when we need
  more than the search result exposes.

Both are public and unauthenticated.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass

import requests

from pm.core import circuit_breaker

log = logging.getLogger(__name__)

MANIFOLD_BASE = os.getenv("MANIFOLD_API_BASE", "https://api.manifold.markets/v0")
HTTP_TIMEOUT = float(os.getenv("MANIFOLD_HTTP_TIMEOUT", "15"))

_session = requests.Session()
_session.headers.update({"User-Agent": "polymarket-analyzer/1.0"})


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ManifoldMarket:
    """A binary Manifold market with its current community probability."""

    market_id: str
    question: str
    probability: float
    volume: float
    is_resolved: bool
    close_time_ms: int | None  # JSON timestamp in milliseconds; None if perpetual


# ---------------------------------------------------------------------------
# HTTP wrapper
# ---------------------------------------------------------------------------

def _get(path: str, *, params: dict | None = None, retries: int = 2) -> object:
    """Under the ``manifold`` circuit breaker (R-21): fails fast with
    ``CircuitOpenError`` while the dependency is down."""
    return circuit_breaker.get("manifold").call(_get_unguarded, path, params=params,
                                             retries=retries)


def _get_unguarded(path: str, *, params: dict | None = None, retries: int = 2) -> object:
    url = f"{MANIFOLD_BASE}/{path.lstrip('/')}"
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                sleep_s = max(0.1, min(30.0, float(r.headers.get("Retry-After", "1"))))
                log.warning("Manifold 429; retrying in %.1fs", sleep_s)
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            break
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Market parsing
# ---------------------------------------------------------------------------

def _row_to_market(row: dict) -> ManifoldMarket | None:
    """Parse one /search-markets or /market response into a normalized
    dataclass. Returns None for non-binary markets (multiple-choice,
    free-response, numeric) — those don't have a single probability we
    can compare against a binary YES quote.
    """
    if row.get("outcomeType") != "BINARY":
        return None
    prob = row.get("probability")
    if prob is None:
        return None
    try:
        prob_f = float(prob)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= prob_f <= 1.0):
        return None
    return ManifoldMarket(
        market_id=str(row.get("id", "")),
        question=str(row.get("question", "")),
        probability=prob_f,
        volume=float(row.get("volume", 0) or 0),
        is_resolved=bool(row.get("isResolved", False)),
        close_time_ms=row.get("closeTime"),
    )


# ---------------------------------------------------------------------------
# Search + single-market lookup
# ---------------------------------------------------------------------------

def search_markets(
    query: str, *, limit: int = 5, only_open: bool = True,
) -> list[ManifoldMarket]:
    """Natural-language search. Returns up to ``limit`` binary markets
    ranked by Manifold's relevance scoring. Non-binary results and
    parse failures are filtered out silently, so the returned list may
    be shorter than ``limit``.
    """
    if not query.strip():
        return []
    params: dict = {"term": query, "limit": limit}
    if only_open:
        params["filter"] = "open"
    try:
        data = _get("search-markets", params=params)
    except requests.RequestException as e:
        log.warning("Manifold search failed for %r: %s", query[:60], e)
        return []
    if not isinstance(data, list):
        return []
    out: list[ManifoldMarket] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        m = _row_to_market(row)
        if m is not None:
            out.append(m)
    return out


def get_market(market_id: str) -> ManifoldMarket | None:
    """Fetch one market by Manifold ID. Returns None for non-binary,
    parse failures, or transport errors."""
    try:
        data = _get(f"market/{market_id}")
    except requests.RequestException as e:
        log.warning("Manifold get_market(%s) failed: %s", market_id, e)
        return None
    if not isinstance(data, dict):
        return None
    return _row_to_market(data)


# ---------------------------------------------------------------------------
# Token-overlap similarity (Jaccard on content tokens)
# ---------------------------------------------------------------------------

# Words that carry no semantic weight for question matching. Lowercase.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "of", "for",
    "to", "in", "on", "at", "by", "with", "from", "into", "is", "are",
    "was", "were", "be", "been", "being", "will", "would", "shall",
    "should", "can", "could", "may", "might", "do", "does", "did",
    "have", "has", "had", "this", "that", "these", "those", "it", "its",
    "any", "all", "some", "no", "not", "than", "what", "which", "who",
    "whom", "whose", "when", "where", "why", "how",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Lowercase content words, stopwords removed, length ≥ 2."""
    return {
        t for t in _TOKEN_RE.findall(text.lower())
        if len(t) >= 2 and t not in _STOPWORDS
    }


def jaccard_similarity(a: str, b: str) -> float:
    """Token Jaccard on the two strings. 0.0 if no overlap or both empty.

    >>> # Stopwords dropped; {trump, impeached, 2026} shared out of
    >>> # {wil, trump, impeached, house, 2026} → 3/5.
    >>> round(jaccard_similarity("Will Trump be impeached in 2026?",
    ...                            "Wil Trump be impeached by the House in 2026?"), 3)
    0.6
    >>> jaccard_similarity("Bitcoin to $200k", "Heat vs. Lakers")
    0.0
    >>> jaccard_similarity("", "anything")
    0.0
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union


def best_match(
    query: str, candidates: list[ManifoldMarket],
) -> tuple[ManifoldMarket, float] | None:
    """Highest-similarity (market, score) pair. Returns None when
    ``candidates`` is empty."""
    if not candidates:
        return None
    scored = [(m, jaccard_similarity(query, m.question)) for m in candidates]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[0]


__all__ = [
    "ManifoldMarket",
    "search_markets",
    "get_market",
    "jaccard_similarity",
    "best_match",
]


if __name__ == "__main__":
    # Smoke when run directly.
    out = search_markets("Trump impeached in 2026", limit=3)
    print(f"Top-3 matches for impeachment query: {len(out)}")
    for m in out:
        print(f"  {m.question[:80]}")
        print(f"     prob={m.probability:.3f} vol=${m.volume:,.0f}")
