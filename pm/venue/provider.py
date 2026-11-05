"""Market-data provider abstraction.

The app started as a Polymarket-specific tool. Once the question of "what
do we use that's legal for US persons?" came up (Kalshi, eventually
Polymarket US), the right answer was to put a thin abstraction between the
UI/analysis layer and any specific market venue so they can be swapped or
compared.

Architecture:

    pages / shared.py
            │
            ▼
    provider.get_provider()
            │
            ▼
    ┌──── MarketProvider Protocol ────┐
    │                                  │
    ▼                                  ▼
    PolymarketProvider           KalshiProvider   (Phase 2)
            │                            │
            ▼                            ▼
    polymarket_client.py         kalshi_client.py (Phase 2)

The **provider boundary is where the network calls happen.** Provider
methods return DataFrames with a normalized schema so pure helpers
(scanner functions, Kelly sizing, order-book parsing) work identically
regardless of which provider produced the data.

Active provider is selected via the `MARKET_PROVIDER` env var (default
"kalshi" — Polymarket excludes US persons, so Kalshi is the safer
out-of-the-box choice). A future UI sidebar selector can simply call
`set_active_provider()` and call `st.rerun()`.

The pure helpers (`parse_order_book`, `scan_*`, `kelly_sizing`,
`enrich_categories_from_events`, etc.) are still imported directly from
`polymarket_client` because their inputs are already-normalized DataFrames
— they don't care which provider was the source. They'll be renamed /
moved if and when we need to disambiguate, but for now there's no win in
moving them.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

import pandas as pd

log = logging.getLogger(__name__)

# Active provider is selected via env var. Kalshi is the default because
# it's the US-legal venue (Polymarket excludes US persons). Override with
# `MARKET_PROVIDER=polymarket` in .env or your shell environment.
ACTIVE_PROVIDER_ENV = "MARKET_PROVIDER"
DEFAULT_PROVIDER = "kalshi"


@runtime_checkable
class MarketProvider(Protocol):
    """Common interface every market-data provider implements.

    Provider methods returning DataFrames must produce a normalized schema
    (column names + types) so that downstream analysis is provider-agnostic.
    See the docstrings on each method for the required columns.
    """

    # Short, lowercase identifier used in URLs / env vars / settings
    name: str
    # Human-readable name shown in the UI
    display_name: str

    def get_markets(
        self,
        limit: int = 500,
        active: bool = True,
        order: str = "volume24hr",
    ) -> pd.DataFrame:
        """Return a DataFrame of currently-active markets.

        Required columns: id, condition_id, question, category, event_id,
        volume, volume_24hr, liquidity, end_date, outcomes (list[str]),
        outcome_prices (list[float|None]), active, spread, best_bid,
        best_ask, last_trade_price, days_to_resolution.
        """
        ...

    def get_market(self, market_id: str) -> dict:
        """Return full provider-specific market detail for one market id.
        Schema is provider-specific; callers typically only need the
        token-id list to fetch price history."""
        ...

    def get_events(self, limit: int = 500) -> pd.DataFrame:
        """Return a DataFrame of events (groupings of related markets).

        Required columns: id (str), title, slug, category, volume_24hr,
        active. `category` should be derived from provider tags where
        available; empty string otherwise."""
        ...

    def enrich_categories_from_events(
        self, df_markets: pd.DataFrame, df_events: pd.DataFrame
    ) -> pd.DataFrame:
        """Override the `category` column on `df_markets` with the
        authoritative tag-derived category from `df_events`, joined on
        `event_id`. Adds an `event_title` column too. Returns the modified
        DataFrame; non-destructive (copies)."""
        ...

    def get_price_history(
        self, token_id: str, days: int = 30, fidelity: int = 60,
    ) -> pd.DataFrame:
        """Return price-time-series for one outcome token.

        Required columns: timestamp (UTC datetime), price (float in [0, 1]).
        Empty DataFrame on no data."""
        ...

    def get_order_book(self, token_id: str) -> dict:
        """Return raw order-book response. Parse with `parse_order_book`
        to get a tidy DataFrame."""
        ...

    def list_outcome_tokens(self, market_id: str) -> list[dict]:
        """Tokens (or token-equivalents) for one market, one per outcome we
        want to chart / fetch an order book for.

        Each dict has `token_id` (the string passed to `get_price_history` /
        `get_order_book` / `get_midpoint`) and `outcome` (the human label,
        e.g. "Yes", "Trump", "Manchester City"). On binary venues that
        derive the NO side from the YES price (Kalshi), only the YES
        token is returned — the NO side is reconstructed by downstream
        adapters where needed. Empty list if the market isn't
        order-book-enabled."""
        ...

    def get_midpoint(self, token_id: str) -> float | None:
        """Current mid-market price for one token, or None if unavailable."""
        ...

    def get_closed_markets(self, limit: int = 100) -> pd.DataFrame:
        """Recently-resolved markets. Same column schema as `get_markets`
        plus `resolved_yes` (bool|None) and `closed_time_dt`."""
        ...

    def get_closed_markets_in_range(
        self, *, since_days: float, until_days: float = 0.0,
        max_items: int = 10000, min_volume: float = 0.0,
        progress_callback=None,
    ) -> pd.DataFrame:
        """Resolved markets that closed in the window
        ``[now − since_days, now − until_days]`` (UTC). Same column
        schema as ``get_closed_markets``. Cursor-paginated up to
        ``max_items``. ``min_volume`` filters out markets that never
        traded — strongly recommended for backtesting since auto-
        generated synthetics dominate the raw count.

        Used to build large historical datasets for backtesting —
        ``get_closed_markets(limit=N)`` only gives you the latest N,
        which caps at ~100 useful markets. This walks the whole
        window in one go.

        Default impl falls back to ``get_closed_markets(limit=max_items)``
        for venues that haven't implemented native time filtering.
        Kalshi overrides with the real ``/markets?status=settled``
        + ``min_close_ts``/``max_close_ts`` query + min_volume filter."""
        return self.get_closed_markets(limit=max_items)

    def lookup_markets_by_id(self, market_or_event_id: str) -> pd.DataFrame:
        """Find a single market by ticker, OR every sub-market of an
        event. Accepts a market ID, an event ID, or a venue URL.
        Returns the same row schema as ``get_markets``. Empty when
        nothing matches. Used by UI "Look up by ticker/URL" widgets
        to bring specific markets into scope that aren't surfaced by
        the default fetch paths (low-volume, long-dated, niche events)."""
        ...

    def get_markets_closing_within(
        self, max_days: float, *, limit: int = 500,
        min_days: float = 0.0,
    ) -> pd.DataFrame:
        """Active markets closing within ``max_days``. Same row schema
        as ``get_markets``. Used by pages that want short-dated markets
        without paginating through thousands of long-dated ones first
        — the default volume-ordered fetch can hide all near-term
        markets when the top of the venue is dominated by long-dated
        marquee events.

        Default impl falls back to filtering ``get_markets`` output;
        venues with native time filtering (Kalshi) should override.
        """
        df = self.get_markets(limit=max(limit, 500))
        if "days_to_resolution" not in df.columns:
            return df
        m = (df["days_to_resolution"].fillna(-1) >= min_days) & \
            (df["days_to_resolution"].fillna(1e9) <= max_days)
        return df[m].sort_values("days_to_resolution").reset_index(drop=True).head(limit)

    def get_market_calibration_data(
        self,
        df_closed: pd.DataFrame,
        lead_hours: float = 24,
        max_markets: int = 100,
        progress_callback=None,
    ) -> pd.DataFrame:
        """For each closed market in `df_closed`, fetch its YES price at
        `lead_hours` before resolution and compare to the outcome.

        Required columns: condition_id, question, category, predicted_prob,
        resolved_yes, actual (0.0 or 1.0), brier_term, lead_hours."""
        ...


# ---------------------------------------------------------------------------
# Polymarket adapter — thin shim over polymarket_client.py
# ---------------------------------------------------------------------------

class PolymarketProvider:
    """Adapter satisfying MarketProvider by delegating to polymarket_client."""

    name = "polymarket"
    display_name = "Polymarket"

    # All methods are thin wrappers. Logic stays in polymarket_client so the
    # diff against the pre-abstraction code is minimal.

    def get_markets(self, limit: int = 500, active: bool = True,
                    order: str = "volume24hr") -> pd.DataFrame:
        from pm.venue import polymarket_client as pm
        return pm.get_markets(limit=limit, active=active, order=order)

    def get_market(self, market_id: str) -> dict:
        from pm.venue import polymarket_client as pm
        return pm.get_market(market_id)

    def get_events(self, limit: int = 500) -> pd.DataFrame:
        from pm.venue import polymarket_client as pm
        return pm.get_events(limit=limit)

    def enrich_categories_from_events(self, df_markets, df_events):
        from pm.venue import polymarket_client as pm
        return pm.enrich_categories_from_events(df_markets, df_events)

    def get_price_history(self, token_id: str, days: int = 30,
                          fidelity: int = 60) -> pd.DataFrame:
        from pm.venue import polymarket_client as pm
        return pm.get_price_history(token_id, days=days, fidelity=fidelity)

    def get_order_book(self, token_id: str) -> dict:
        from pm.venue import polymarket_client as pm
        return pm.get_order_book(token_id)

    def list_outcome_tokens(self, market_id: str) -> list[dict]:
        from pm.venue import polymarket_client as pm
        detail = pm.get_market(market_id)
        clob_ids = pm.parse_json_field(detail.get("clobTokenIds"))
        outcomes = pm.parse_json_field(detail.get("outcomes")) or []
        return [
            {
                "token_id": str(tid),
                "outcome": outcomes[i] if i < len(outcomes) else f"Outcome {i}",
            }
            for i, tid in enumerate(clob_ids)
        ]

    def get_midpoint(self, token_id: str) -> float | None:
        from pm.venue import polymarket_client as pm
        return pm.get_midpoint(token_id)

    def get_closed_markets(self, limit: int = 100) -> pd.DataFrame:
        from pm.venue import polymarket_client as pm
        return pm.get_closed_markets(limit=limit)

    def get_closed_markets_in_range(
        self, *, since_days: float, until_days: float = 0.0,
        max_items: int = 10000, min_volume: float = 0.0,
        progress_callback=None,
    ) -> pd.DataFrame:
        # Polymarket's get_closed_markets doesn't support date-range
        # filtering natively. Fall back to fetching latest N and
        # filtering client-side. Not as efficient as Kalshi's native
        # query but correct.
        df = self.get_closed_markets(limit=max_items)
        if df.empty or "closed_time_dt" not in df.columns:
            return df
        import time
        now = time.time()
        min_ts = pd.Timestamp(now - since_days * 86400, unit="s", tz="UTC")
        max_ts = pd.Timestamp(now - until_days * 86400, unit="s", tz="UTC")
        df = df[
            (df["closed_time_dt"] >= min_ts)
            & (df["closed_time_dt"] <= max_ts)
        ]
        if min_volume > 0 and "volume" in df.columns:
            df = df[df["volume"] >= min_volume]
        return df.reset_index(drop=True)

    def lookup_markets_by_id(self, market_or_event_id: str) -> pd.DataFrame:
        # Polymarket conditions are hex strings (0x...). No event-wrapping
        # like Kalshi — a single ID maps to a single market.
        from pm.venue import polymarket_client as pm
        s = market_or_event_id.strip()
        if not s:
            return pd.DataFrame()
        try:
            m = pm.get_market(s)
        except Exception:
            return pd.DataFrame()
        if not m or not m.get("conditionId"):
            return pd.DataFrame()
        # Use the standard transform from get_markets via a one-row df.
        df = pm.get_markets(limit=500)
        return df[df["condition_id"] == s].reset_index(drop=True)

    def get_markets_closing_within(self, max_days: float, *,
                                     limit: int = 500,
                                     min_days: float = 0.0) -> pd.DataFrame:
        # Polymarket's Gamma /markets doesn't expose close-time filters
        # in the same way Kalshi does. Fall back to over-fetching then
        # client-side filtering. Cheap for limit=500; if it gets used
        # heavily we'll need an `endDateMin` / `endDateMax` query path.
        df = self.get_markets(limit=max(int(limit), 500))
        if df.empty or "days_to_resolution" not in df.columns:
            return df
        mask = (df["days_to_resolution"].fillna(-1) >= float(min_days)) & \
               (df["days_to_resolution"].fillna(1e9) <= float(max_days))
        return df[mask].sort_values("days_to_resolution") \
                       .reset_index(drop=True).head(int(limit))

    def get_market_calibration_data(self, df_closed, lead_hours: float = 24,
                                     max_markets: int = 100,
                                     progress_callback=None) -> pd.DataFrame:
        from pm.venue import polymarket_client as pm
        return pm.get_market_calibration_data(
            df_closed, lead_hours=lead_hours, max_markets=max_markets,
            progress_callback=progress_callback,
        )


# ---------------------------------------------------------------------------
# Registry and selection
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, MarketProvider] = {
    "polymarket": PolymarketProvider(),
}

# Register Kalshi at module-import time. Imported here at the bottom so the
# PolymarketProvider class is defined first; kalshi_client doesn't import
# from provider, so there's no cycle.
try:
    from pm.venue.kalshi_client import KalshiProvider as _KalshiProvider
    _PROVIDERS["kalshi"] = _KalshiProvider()
except ImportError:
    # Kalshi support is optional — running without it just means the
    # provider selector won't list it.
    log.warning("kalshi_client not importable; Kalshi provider unavailable")


def register_provider(provider: MarketProvider) -> None:
    """Register an additional provider implementation. Called once per
    provider, typically from `kalshi_client.py` (Phase 2) at import time."""
    _PROVIDERS[provider.name] = provider
    log.info("Registered market provider: %s", provider.name)


def list_provider_names() -> list[str]:
    """Names of all registered providers, in registration order."""
    return list(_PROVIDERS.keys())


def list_providers() -> list[MarketProvider]:
    """Provider instances. UI uses this to build a selector with display names."""
    return list(_PROVIDERS.values())


def active_provider_name() -> str:
    """Currently selected provider name, from env var or default."""
    name = os.getenv(ACTIVE_PROVIDER_ENV, DEFAULT_PROVIDER)
    if name not in _PROVIDERS:
        log.warning("Unknown provider %r, falling back to %s", name, DEFAULT_PROVIDER)
        return DEFAULT_PROVIDER
    return name


def get_provider(name: str | None = None) -> MarketProvider:
    """Get a provider instance by name. Returns the active provider if `name`
    is None. Raises ValueError for unknown names."""
    if name is None:
        name = active_provider_name()
    if name not in _PROVIDERS:
        raise ValueError(
            f"Unknown provider: {name!r}. Available: {list_provider_names()}"
        )
    return _PROVIDERS[name]


def set_active_provider(name: str) -> None:
    """Set the active provider via env var. Affects subsequent `get_provider()`
    calls within this Python process. For persistence across restarts, set
    `MARKET_PROVIDER` in `.env` or your shell environment."""
    if name not in _PROVIDERS:
        raise ValueError(
            f"Unknown provider: {name!r}. Available: {list_provider_names()}"
        )
    os.environ[ACTIVE_PROVIDER_ENV] = name
    log.info("Active provider → %s", name)
