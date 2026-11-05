"""External-data edge detection — Tier 4 of the recommendations roadmap.

This module compares a venue's market-implied probability to an external
reference value and surfaces the gap as an *edge candidate*. It is
deliberately advisory: an "edge" here is a flag for the user to inspect,
not a recommendation. The design-doc rule still applies — a new edge
source ships as a recommendation only after a Tier 3b backtest shows
its predictions beat the market on the relevant category.

First source shipped: ``crypto_spot``. It parses crypto-threshold
questions ("Will BTC reach $X by DATE?"), pulls current spot + realized
volatility from Coinbase, and computes a lognormal terminal probability
to compare against the market's YES price. The same logic is exposed as
a backtest strategy (registered in ``backtest.STRATEGIES``) so the gate
can be measured the same way as the existing baselines.

Why both pieces in the same module:
- The live edge surfaces in the Edges page.
- The same parsing + modelling logic runs inside the backtest engine
  via ``strategy_crypto_spot_lognormal``.
Keeping them colocated means there's exactly one place to fix when
parser rules or modelling assumptions change.

---

Split into a package (P3/R-24, 2026-08-24). `edges.py` was 2,175 lines and
imported by nearly everything; it is now `edges/` — this file holding the
framework (the `Edge` output type lives in `_base`, the source registry and
`find_all_edges` live here) plus one module per source.

**The public API is unchanged.** Every name that was importable as
`edges.<name>` before the split still is, because this module re-exports all
of them below. `import edges` continues to trigger the Tier-3b strategy
registration side effect that `app.py` and `strategy_runner.py` rely on,
because importing the source modules is what runs their
`@backtest.register_strategy` decorators.

One deliberate narrowing: `edges.py` used to leak its own implementation
imports as attributes (`edges.sports_data`, `edges.re`, `edges.datetime`, …).
Those were never API — nothing in the tree referenced them, verified before the
split — and they are gone. Every name that was actually used still resolves.

**If you patch an edges INTERNAL in a test, patch it at its real home** —
`edges.baseball._baseball_games_for_date`, not `edges._baseball_games_for_date`.
The latter now names this module's re-export, and rebinding it would not
affect the caller inside `edges/baseball.py`. Patching a name that lives here
(`edges.SOURCES`) is unaffected.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from pm.ledger import calibration as cal
from pm.research import backtest as bt

from ._base import UTC, Edge, _row_to_snapshot, _severity_for_gap, edges_to_dataframe, log
from .baseball import (
    _KALSHI_MLB_TICKER,
    _MONTH_ABBR,
    _baseball_games_for_date,
    _baseball_team_aliases,
    _baseball_today_schedule,
    _baseball_winprob_for_side,
    _match_baseball_game,
    _parse_kalshi_mlb_ticker,
    find_baseball_edges,
    sports_consensus_for_ticker,
    strategy_baseball_winprob,
)
from .crypto import (
    _ABOVE_VERBS,
    _AMOUNT_PATTERN,
    _ASSET_ALIASES,
    _DOWN_TO_PATTERN,
    _TERMINAL_PATTERN,
    ParsedCryptoQuestion,
    _detect_asset,
    _model_prob,
    _parse_amount,
    find_crypto_edges,
    parse_crypto_question,
    strategy_crypto_spot_lognormal,
)
from .llm import find_llm_edges, strategy_llm_forecast
from .macro import (
    _MACRO_ABOVE_VERBS,
    _MACRO_AMOUNT_PATTERN,
    _SERIES_PATTERNS,
    ParsedMacroQuestion,
    _macro_model_prob,
    find_macro_edges,
    parse_macro_question,
    strategy_macro_threshold,
)
from .manifold import find_manifold_edges, strategy_manifold_community
from .sports import (
    _DERIVATIVE_KEYWORDS,
    _find_first_alias,
    _match_event,
    _team_aliases,
    _winprob_for_side,
    find_sports_edges,
    strategy_sports_winprob,
)
from .tennis import (
    _TENNIS_MIN_MATCHES,
    _TENNIS_RANK_GAP,
    _TENNIS_RANK_TRUST,
    _tennis_pred,
    _tennis_yes_prob,
    find_tennis_edges,
    strategy_tennis_value,
    strategy_tennis_winprob,
)
from .wnba import (
    _wnba_yes_prob_for_ticker,
    find_wnba_edges,
    strategy_wnba_favorite_hold,
    strategy_wnba_winprob,
)


def _source_tag(edge: Edge) -> str:
    """Canonical Calibration ``source`` tag for an Edge.

    ``llm`` edges include the specific model in their note string;
    pull it out so different models track separate Brier scores.
    """
    if edge.source != "llm":
        return edge.source
    # find_llm_edges builds notes as f"{model}: {reasoning}". Split on
    # the literal ": " (colon-space) separator. Model names can contain
    # bare colons (e.g. "llama3.1:latest") but never a colon-followed-
    # by-space, so the first occurrence cleanly separates model from
    # reasoning. Keep this in sync with the find_llm_edges format.
    if edge.note and ": " in edge.note:
        return f"llm:{edge.note.split(': ', 1)[0].strip()}"
    return "llm"


def log_edges_to_calibration(edges_to_log: list[Edge]) -> int:
    """Bulk-record a list of Edges as Calibration forecasts so the
    existing Brier scoring picks them up. Returns the number recorded.

    ``your_probability`` becomes the model's implied probability;
    ``market_price`` is the venue's YES price at scan time. ``source``
    is namespaced by the edge source so the Calibration page can show
    per-source Brier (user vs each LLM vs crypto_spot).
    """
    n = 0
    for e in edges_to_log:
        try:
            cal.add_forecast(
                question=e.question,
                condition_id=e.market_id,
                outcome="Yes",
                market_price=float(e.market_yes_price),
                your_probability=float(e.model_implied),
                source=_source_tag(e),
                notes=e.note,
            )
            n += 1
        except (ValueError, TypeError) as err:
            log.warning("skipping edge for %s: %s", e.market_id, err)
    return n


# ---------------------------------------------------------------------------
# Source registry — the Edges page iterates this so a new source ships
# by adding a row here, no UI changes required.
# ---------------------------------------------------------------------------

EdgeFinder = Any  # callable[df_markets, **kwargs] -> list[Edge]

SOURCES: dict[str, tuple[str, EdgeFinder]] = {
    "crypto_spot": ("Crypto spot (lognormal)",          find_crypto_edges),
    "llm":         ("Local LLM (Ollama)",               find_llm_edges),
    "sports":      ("Sports (ESPN consensus)",          find_sports_edges),
    "baseball":    ("Baseball model (sidecar)",         find_baseball_edges),
    "wnba":        ("WNBA model (sidecar)",             find_wnba_edges),
    "tennis":      ("Tennis Elo (sidecar)",            find_tennis_edges),
    "manifold":    ("Manifold community (politics+)",   find_manifold_edges),
    "macro":       ("Macro (FRED normal-on-level)",     find_macro_edges),
}


def list_sources() -> list[tuple[str, str]]:
    """Source registry as [(key, display_name)] pairs."""
    return [(k, v[0]) for k, v in SOURCES.items()]


def get_finder(key: str) -> EdgeFinder:
    """Look up a source's finder by registry key."""
    return SOURCES[key][1]


def find_all_edges(
    df_markets: pd.DataFrame,
    *,
    sources: list[str] | None = None,
    min_gap: float = 0.10,
    min_days_to_expiry: float = 1.0,
    per_source_kwargs: dict[str, dict] | None = None,
    on_source_start: Any = None,
    on_source_done: Any = None,
) -> dict[str, list[Edge]]:
    """Run every (or every named) registered source against the same
    market set and return ``{source_key: [Edge, ...]}``.

    Per-source kwargs override the global ``min_gap`` /
    ``min_days_to_expiry`` defaults when present. Sources that raise
    are caught and logged — one broken source must never break the
    whole recommendations scan. ``on_source_start(key)`` /
    ``on_source_done(key, n_edges)`` callbacks let the UI render
    progress without coupling it to the finder set.
    """
    keys = sources if sources is not None else [k for k, _ in list_sources()]
    out: dict[str, list[Edge]] = {}
    base = {"min_gap": float(min_gap),
            "min_days_to_expiry": float(min_days_to_expiry)}
    extra = per_source_kwargs or {}
    for key in keys:
        if key not in SOURCES:
            log.warning("find_all_edges: unknown source %s — skipping", key)
            continue
        if on_source_start is not None:
            try:
                on_source_start(key)
            except Exception:
                pass
        finder = SOURCES[key][1]
        # Only pass kwargs the finder accepts; safer than guessing
        # the signature for sources with different parameter sets
        # (e.g. crypto's vol_window_days, manifold's min_similarity).
        import inspect
        sig = inspect.signature(finder)
        kwargs: dict[str, Any] = {}
        for k, v in {**base, **extra.get(key, {})}.items():
            if k in sig.parameters:
                kwargs[k] = v
        try:
            out[key] = list(finder(df_markets, **kwargs))
        except Exception as e:
            log.warning("find_all_edges: %s failed: %s", key, e)
            out[key] = []
        if on_source_done is not None:
            try:
                on_source_done(key, len(out[key]))
            except Exception:
                pass
    return out


def edges_severity(e: Edge) -> float:
    """Numeric severity score for ranking the unified Recommendations
    list. Currently just |gap|; could be enhanced later with
    Brier-weighted source confidence."""
    try:
        return abs(float(e.gap))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Register the crypto strategy with the Tier 3b backtest engine so it
# appears in the Backtest page alongside the in-process baselines.
# ---------------------------------------------------------------------------

bt.STRATEGIES["crypto_spot_lognormal"] = (
    "Crypto spot — lognormal (external)",
    strategy_crypto_spot_lognormal,
)

bt.STRATEGIES["llm_forecast"] = (
    "LLM forecast — local Ollama (external)",
    strategy_llm_forecast,
)

bt.STRATEGIES["sports_winprob"] = (
    "Sports win-prob — ESPN consensus (external)",
    strategy_sports_winprob,
)

bt.STRATEGIES["baseball_winprob"] = (
    "Baseball model — sidecar (external)",
    strategy_baseball_winprob,
)

bt.STRATEGIES["wnba_winprob"] = (
    "WNBA model — sidecar (external)",
    strategy_wnba_winprob,
)

bt.STRATEGIES["wnba_favorite_hold"] = (
    "WNBA favorite-hold — buy moderate favorites, hold (external)",
    strategy_wnba_favorite_hold,
)

bt.STRATEGIES["tennis_winprob"] = (
    "Tennis Elo — sidecar (external)",
    strategy_tennis_winprob,
)

bt.STRATEGIES["tennis_value"] = (
    "Tennis Elo value — bet model-mispriced side, K-decay guarded (external)",
    strategy_tennis_value,
)

bt.STRATEGIES["manifold_community"] = (
    "Manifold community — Jaccard match (external)",
    strategy_manifold_community,
)

bt.STRATEGIES["macro_threshold"] = (
    "Macro threshold — FRED normal-on-level (external)",
    strategy_macro_threshold,
)


__all__ = [
    "_SERIES_PATTERNS",
    "_ASSET_ALIASES",
    "UTC",
    "Edge",
    "ParsedCryptoQuestion",
    "parse_crypto_question",
    "strategy_crypto_spot_lognormal",
    "strategy_llm_forecast",
    "strategy_sports_winprob",
    "strategy_baseball_winprob",
    "find_baseball_edges",
    "strategy_wnba_winprob",
    "strategy_wnba_favorite_hold",
    "find_wnba_edges",
    "strategy_tennis_winprob",
    "strategy_tennis_value",
    "find_tennis_edges",
    "strategy_manifold_community",
    "strategy_macro_threshold",
    "find_crypto_edges",
    "find_llm_edges",
    "find_sports_edges",
    "find_manifold_edges",
    "find_macro_edges",
    "ParsedMacroQuestion",
    "parse_macro_question",
    "edges_to_dataframe",
    "log_edges_to_calibration",
    "list_sources",
    "get_finder",
    "SOURCES",
]


__all__ = [
    "Edge",
    "EdgeFinder",
    "ParsedCryptoQuestion",
    "ParsedMacroQuestion",
    "SOURCES",
    "_ABOVE_VERBS",
    "_AMOUNT_PATTERN",
    "_DERIVATIVE_KEYWORDS",
    "_DOWN_TO_PATTERN",
    "_KALSHI_MLB_TICKER",
    "_MACRO_ABOVE_VERBS",
    "_MACRO_AMOUNT_PATTERN",
    "_MONTH_ABBR",
    "_TENNIS_MIN_MATCHES",
    "_TENNIS_RANK_GAP",
    "_TENNIS_RANK_TRUST",
    "_TERMINAL_PATTERN",
    "_baseball_games_for_date",
    "_baseball_team_aliases",
    "_baseball_today_schedule",
    "_baseball_winprob_for_side",
    "_detect_asset",
    "_find_first_alias",
    "_macro_model_prob",
    "_match_baseball_game",
    "_match_event",
    "_model_prob",
    "_parse_amount",
    "_parse_kalshi_mlb_ticker",
    "_row_to_snapshot",
    "_severity_for_gap",
    "_team_aliases",
    "_tennis_pred",
    "_tennis_yes_prob",
    "_winprob_for_side",
    "_wnba_yes_prob_for_ticker",
    "edges_severity",
    "edges_to_dataframe",
    "find_all_edges",
    "find_baseball_edges",
    "find_crypto_edges",
    "find_llm_edges",
    "find_macro_edges",
    "find_manifold_edges",
    "find_sports_edges",
    "find_tennis_edges",
    "find_wnba_edges",
    "get_finder",
    "list_sources",
    "log_edges_to_calibration",
    "parse_crypto_question",
    "parse_macro_question",
    "sports_consensus_for_ticker",
    "strategy_baseball_winprob",
    "strategy_crypto_spot_lognormal",
    "strategy_llm_forecast",
    "strategy_macro_threshold",
    "strategy_manifold_community",
    "strategy_sports_winprob",
    "strategy_tennis_value",
    "strategy_tennis_winprob",
    "strategy_wnba_favorite_hold",
    "strategy_wnba_winprob",
]
