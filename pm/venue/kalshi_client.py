"""Kalshi API client + provider.

Kalshi is a CFTC-regulated US-legal prediction market. This module wraps
the public read-only endpoints at `api.elections.kalshi.com` and exposes
a `KalshiProvider` class satisfying the `MarketProvider` Protocol in
`provider.py`.

Schema normalization happens at this layer — the returned DataFrames use
the same column names and value semantics as the Polymarket provider so
the rest of the app (scanners, Kelly sizing, Portfolio, etc.) works
unchanged.

Key Kalshi facts worth remembering:

- **Pagination is cursor-based**, not offset. `_paginate` handles the loop.
- **Prices are already in dollars** (despite my earlier suspicion they'd
  be in cents). Stored as strings like ``"0.7800"`` — `_to_float` handles
  the conversion.
- **Identifier is the ticker** (e.g. ``KXWARMING-50``). Kalshi uses the
  same identifier for the market everywhere — there's no separate
  ``token_id`` like Polymarket. The Protocol's ``token_id`` argument is
  the ticker here.
- **Series ticker = ticker prefix** before the first ``-``. Required by the
  candlesticks URL. ``_series_from_ticker`` extracts it.
- **Categories live on events**, not markets. We enumerate markets via
  ``/events?with_nested_markets=true`` so category is already on each row;
  ``enrich_categories_from_events`` is therefore a near-no-op (just adds
  ``event_title``).
- **Settled markets have ``result`` ∈ {``"yes"``, ``"no"``, ``""``}**;
  ``status`` becomes ``"finalized"`` (not ``"closed"`` or ``"settled"``).
- Candlestick ``period_interval`` only supports ``1``, ``60``, and ``1440``
  minutes. Our generic ``fidelity`` param maps to the closest supported.

No authentication required for any of the endpoints used here.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import pandas as pd
import requests

from pm.core import circuit_breaker

log = logging.getLogger(__name__)

# Env-configurable for testing / future US-version endpoint changes.
KALSHI_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
HTTP_TIMEOUT = float(os.getenv("KALSHI_HTTP_TIMEOUT", "15"))

_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})


# ---------------------------------------------------------------------------
# HTTP layer — same retry/429 semantics as polymarket_client._get
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None, retries: int = 3) -> dict:
    """GET against the Kalshi API — under the ``kalshi`` circuit breaker
    (R-21): after 5 consecutive exhausted attempts the breaker opens and
    this raises ``CircuitOpenError`` (a ``RequestException``) immediately for
    the cooldown instead of hammering a dead or throttling venue every
    cycle. Shared with ``kalshi_auth`` — same host."""
    return circuit_breaker.get("kalshi").call(_get_unguarded, path, params, retries)


def _get_unguarded(path: str, params: dict | None = None, retries: int = 3) -> dict:
    """GET against the Kalshi API with retry on transient errors + 429 backoff.

    `path` is appended to KALSHI_BASE (do NOT include leading slash repeat).
    Returns the parsed JSON dict, or raises requests.HTTPError on terminal
    4xx/5xx (except 429, which is retried up to `retries` times).
    """
    url = f"{KALSHI_BASE.rstrip('/')}/{path.lstrip('/')}"
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last_exc = e
            if attempt == retries - 1:
                raise
            time.sleep(1.5 ** attempt)
            continue

        if r.status_code == 429:
            if attempt == retries - 1:
                r.raise_for_status()
                return {}
            sleep_s = float(r.headers.get("Retry-After", "1") or 1.0)
            sleep_s = min(max(sleep_s, 0.1), 30.0)
            log.debug("Kalshi rate-limited (429); retrying in %.1fs", sleep_s)
            time.sleep(sleep_s)
            continue

        try:
            r.raise_for_status()
        except requests.HTTPError:
            raise
        return r.json() if r.content else {}

    if last_exc:
        raise last_exc
    return {}


def _paginate(
    path: str, params: dict | None, items_key: str, max_items: int,
    *, progress_callback=None,
) -> list[dict]:
    """Cursor-based pagination. Calls `_get` repeatedly, accumulating
    `data[items_key]` until either `max_items` is reached or the cursor
    runs out.

    ``progress_callback(n_fetched, max_items)`` is invoked after every
    page if provided. Exceptions in the callback are swallowed so a
    flaky tqdm/print loop doesn't break the fetch.
    """
    out: list[dict] = []
    p: dict[str, Any] = dict(params or {})
    while len(out) < max_items:
        data = _get(path, params=p)
        items = data.get(items_key) or []
        if not items:
            break
        out.extend(items)
        if progress_callback is not None:
            try:
                progress_callback(len(out), max_items)
            except Exception:
                pass
        cursor = data.get("cursor")
        if not cursor:
            break
        p["cursor"] = cursor
    return out[:max_items]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(x: Any, default: float = 0.0) -> float:
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _series_from_ticker(ticker: str) -> str:
    """Extract the series prefix from a Kalshi market ticker.
    Required by the candlesticks URL.

    >>> _series_from_ticker("KXWARMING-50")
    'KXWARMING'
    >>> _series_from_ticker("KXNEWPOPE-70-PPIZ")
    'KXNEWPOPE'
    >>> _series_from_ticker("")
    ''
    """
    if not ticker:
        return ""
    return ticker.split("-", 1)[0]


def _compose_question(m: dict) -> str:
    """Compose the human-facing binary-question string for a Kalshi
    market. ``title`` is the umbrella event question (often shared
    across many sub-markets — "Who will the next Pope be?"); the
    specific YES proposition lives in ``yes_sub_title`` ("Pierbattista
    Pizzaballa"). Joining them yields an unambiguous binary question
    ("Who will the next Pope be? — Pierbattista Pizzaballa") so the
    user knows what YES means for the sub-market they're looking at.

    Falls back gracefully: when ``yes_sub_title`` is empty, a dupe of
    ``title``, or "yes"/"no" boilerplate, we return ``title`` alone.

    >>> _compose_question({"title": "Will Trump win?", "yes_sub_title": ""})
    'Will Trump win?'
    >>> _compose_question({"title": "Who will the next Pope be?",
    ...                     "yes_sub_title": "Pierbattista Pizzaballa"})
    'Who will the next Pope be? — Pierbattista Pizzaballa'
    >>> _compose_question({"title": "Will Trump win?",
    ...                     "yes_sub_title": "Will Trump win?"})
    'Will Trump win?'
    """
    title = (m.get("title") or "").strip()
    sub = (m.get("yes_sub_title") or "").strip()
    if not sub or sub.lower() in ("yes", "no") or sub == title:
        return title
    return f"{title} — {sub}"


def _market_to_row(m: dict, event_category: str = "", event_title: str = "") -> dict:
    """Normalize one Kalshi market dict to our standard market row schema.

    Pass `event_category` / `event_title` when iterating through events
    with nested markets so categorization is set correctly.
    """
    yes_bid = _to_float(m.get("yes_bid_dollars"))
    yes_ask = _to_float(m.get("yes_ask_dollars"))
    last_price = _to_float(m.get("last_price_dollars"))

    # Mid-quote for YES; clamp to [0, 1] in case of degenerate book
    if yes_bid > 0 and yes_ask > 0:
        yes_mid = (yes_bid + yes_ask) / 2.0
    elif last_price > 0:
        yes_mid = last_price
    elif yes_ask > 0:
        yes_mid = yes_ask / 2.0
    else:
        yes_mid = 0.0
    yes_mid = max(0.0, min(1.0, yes_mid))
    no_mid = max(0.0, min(1.0, 1.0 - yes_mid))

    spread = max(0.0, yes_ask - yes_bid) if (yes_bid > 0 and yes_ask > 0) else 0.0
    ticker = str(m.get("ticker", ""))
    event_ticker = str(m.get("event_ticker", ""))

    # Kalshi has THREE date fields:
    #   - close_time / latest_expiration_time: latest possible resolve
    #     (often months out as a safety backstop)
    #   - expected_expiration_time: when the venue actually expects the
    #     market to resolve (the real answer for "when does this close?")
    #   - expiration_time: identical to close_time in practice
    # We prefer expected_expiration_time when present; falls back to
    # close_time. The fields-and-events scan can omit expected, so
    # treat absence/empty-string as "use close_time".
    expected = m.get("expected_expiration_time") or ""
    end_date = expected or m.get("close_time")

    # Kalshi's `liquidity_dollars` field is broken — returns "0.0000" on
    # every market we've seen, including ones with 11M+ open interest.
    # Fall back to a $-denominated activity proxy from 24h volume and
    # open interest so the scanner filters do something useful. Each
    # contract is worth between $0 and $1 (it's binary), so
    # contracts × mid is an approximate dollar value.
    liquidity = _to_float(m.get("liquidity_dollars"))
    if liquidity <= 0:
        vol_24h_contracts = _to_float(m.get("volume_24h_fp"))
        open_int_contracts = _to_float(m.get("open_interest_fp"))
        # Use the larger of: 24h volume (recent activity) or 10% of
        # open interest (rough estimate of accessible depth). Both
        # are contract counts; multiply by mid for a $ proxy.
        contracts_proxy = max(vol_24h_contracts, open_int_contracts * 0.1)
        liquidity = contracts_proxy * max(yes_mid, 0.01)

    return {
        "id": ticker,
        "condition_id": ticker,       # Kalshi's ticker IS the identifier
        "question": _compose_question(m),
        "category": event_category,
        "event_id": event_ticker,
        "event_title": event_title,
        "volume": _to_float(m.get("volume_fp")),
        "volume_24hr": _to_float(m.get("volume_24h_fp")),
        "liquidity": liquidity,
        "end_date": end_date,
        # Kalshi-specific extras for deep-analysis context. Other
        # callers ignore these; pure additions to the row.
        "close_time": m.get("close_time"),
        "expected_expiration_time": expected,
        "can_close_early": bool(m.get("can_close_early", False)),
        "early_close_condition": m.get("early_close_condition") or "",
        "outcomes": ["Yes", "No"],
        "outcome_prices": [yes_mid, no_mid],
        "active": m.get("status") == "active",
        "spread": spread,
        "best_bid": yes_bid,
        "best_ask": yes_ask,
        "last_trade_price": last_price,
    }


def _add_days_to_resolution(df: pd.DataFrame) -> pd.DataFrame:
    """Compute `days_to_resolution` from `end_date`. Mutates in place but
    returns the df for chaining."""
    if df.empty or "end_date" not in df.columns:
        return df
    df["end_date_parsed"] = pd.to_datetime(df["end_date"], errors="coerce", utc=True)
    now = pd.Timestamp.now(tz="UTC")
    df["days_to_resolution"] = (
        (df["end_date_parsed"] - now).dt.total_seconds() / 86400.0
    ).clip(lower=0).round(1)
    return df


# ---------------------------------------------------------------------------
# Public fetch functions
# ---------------------------------------------------------------------------

def get_markets(limit: int = 500, active: bool = True,
                order: str = "volume24hr") -> pd.DataFrame:
    """Fetch active markets from Kalshi, normalized to our schema.

    Enumerates via ``/events?with_nested_markets=true`` so each market row
    arrives pre-tagged with its parent event's category and title. The
    `order` argument is currently ignored (Kalshi doesn't accept the same
    ordering names as Polymarket); results come back in Kalshi's default
    order, which prioritizes events with active markets.
    """
    status = "open" if active else "settled"
    events = _paginate(
        "events",
        params={"status": status, "with_nested_markets": "true"},
        items_key="events",
        max_items=limit * 2,   # events typically have multiple markets each
    )

    rows: list[dict] = []
    for ev in events:
        ev_cat = ev.get("category") or ""
        ev_title = ev.get("title") or ""
        ev_me = bool(ev.get("mutually_exclusive", False))
        for m in ev.get("markets") or []:
            row = _market_to_row(m, event_category=ev_cat, event_title=ev_title)
            # Kalshi's authoritative one-winner flag — exactly one market in the
            # event resolves YES. The rigorous basis for cross-market arb.
            row["mutually_exclusive"] = ev_me
            rows.append(row)
        if len(rows) >= limit:
            break

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows[:limit])
    df = _add_days_to_resolution(df)
    # Sort by 24h volume descending so the most active markets are first
    if "volume_24hr" in df.columns:
        df = df.sort_values("volume_24hr", ascending=False).reset_index(drop=True)
    return df


_EVENTS_META_TTL_SECONDS = 300
_events_meta_cache: tuple[float, dict[str, tuple[str, str]]] = (0.0, {})


def _events_meta_cached() -> dict[str, tuple[str, str]]:
    """Module-level TTL cache for the ``event_ticker → (category, title)``
    lookup. Refreshed every ``_EVENTS_META_TTL_SECONDS``. The /events
    scan is the slow part of ``get_markets_closing_within`` — 20+ pages
    at 100 events each, which on back-to-back calls (smoke test, Edges
    + Recommendations + Markets all hitting the same fetch path) racks
    up 429s. Caching at module scope means every caller in the process
    shares one scan within the TTL window.

    Returns ``{event_ticker: (category, title)}`` for every active
    event we could pull.
    """
    global _events_meta_cache
    now = time.time()
    ts, meta = _events_meta_cache
    if meta and (now - ts) < _EVENTS_META_TTL_SECONDS:
        return meta
    fresh: dict[str, tuple[str, str]] = {}
    try:
        ev_data = _paginate(
            "events", params={"status": "open"},
            items_key="events", max_items=2000,
        )
        for ev in ev_data:
            t = ev.get("event_ticker") or ev.get("ticker")
            if t:
                fresh[t] = (ev.get("category") or "",
                            ev.get("title") or "")
    except Exception as e:
        log.warning("_events_meta_cached: %s — keeping prior cache", e)
        return meta  # fall back to whatever we had
    _events_meta_cache = (now, fresh)
    return fresh


def get_markets_closing_within(max_days: float, *,
                                 limit: int = 500,
                                 min_days: float = 0.0) -> pd.DataFrame:
    """Active markets closing within ``max_days``. Same row schema as
    ``get_markets`` so downstream code is venue-agnostic.

    Uses Kalshi's ``/markets`` endpoint with ``min_close_ts`` /
    ``max_close_ts`` filters — the only reliable way to surface
    short-dated markets without paginating through thousands of
    long-dated ones first.

    Category data is enriched via a separate ``/events`` lookup (one
    call, cached upstream by ``shared.load_events``). Markets whose
    parent event isn't in that lookup fall through to keyword
    inference inside the row builder.
    """
    import time
    now = int(time.time())
    params = {
        "status": "open",
        "min_close_ts": int(now + min_days * 86400),
        "max_close_ts": int(now + max_days * 86400),
        "limit": min(int(limit), 1000),
    }
    items = _paginate("markets", params=params,
                       items_key="markets", max_items=int(limit))

    if not items:
        return pd.DataFrame()

    # Build event_ticker → (category, title) lookup. The /events scan
    # is the expensive part (20+ pages → 10+ rate-limited retries on
    # back-to-back calls), so we memoize it for a few minutes. Every
    # caller in the same process shares the same lookup, including
    # the smoke test and Streamlit reruns within a TTL window.
    event_tickers = sorted({str(m["event_ticker"]) for m in items
                            if m.get("event_ticker")})
    event_meta = _events_meta_cached() if event_tickers else {}

    # Keyword-based fallback for markets whose parent event isn't in
    # the scan window (Kalshi's per-game / per-candidate events live in
    # the long tail of /events). The closing-soon path would otherwise
    # leave the category filter useless.
    from pm.venue.polymarket_client import _infer_category

    rows: list[dict] = []
    for m in items:
        et = m.get("event_ticker") or ""
        cat, title = event_meta.get(et, ("", ""))
        if not cat:
            cat = _infer_category(_compose_question(m), events=[])
        rows.append(_market_to_row(m, event_category=cat, event_title=title))

    df = pd.DataFrame(rows)
    df = _add_days_to_resolution(df)
    if "days_to_resolution" in df.columns:
        df = df.sort_values("days_to_resolution").reset_index(drop=True)
    return df


def get_market(ticker: str) -> dict:
    """Fetch a single market by ticker. Returns the raw market dict."""
    data = _get(f"markets/{ticker}")
    return data.get("market") or {}


def lookup_markets_by_id(market_or_event_id: str) -> pd.DataFrame:
    """Find a single market by ticker, OR every sub-market of an event.

    Accepts:
      - A market ticker like ``KXKY4R-26-TMAS`` (single market)
      - An event ticker like ``KXKY4R-26`` (every sub-market)
      - A Kalshi URL like
        ``https://kalshi.com/markets/kxky4r/.../kxky4r-26``

    Returns the same row schema as ``get_markets``. Empty DataFrame if
    nothing matches. Used by UI "Look up by ticker / URL" widgets to
    bring specific markets into scope that aren't in the standard
    fetch paths (low-volume, long-dated, or sub-events).
    """
    s = market_or_event_id.strip()
    if not s:
        return pd.DataFrame()

    # Pull a ticker out of a Kalshi URL: the last non-empty path
    # segment is always the event ticker (in lowercase).
    if "kalshi.com" in s.lower():
        import urllib.parse as up
        path = up.urlparse(s).path
        segs = [seg for seg in path.split("/") if seg]
        if segs:
            s = segs[-1]

    # Kalshi tickers are uppercase; URLs use lowercase. Normalize.
    s = s.upper()

    # Heuristic: a market ticker has the event's prefix + an additional
    # `-XYZW` suffix. Try market first; fall through to event lookup
    # if the single-market fetch returns nothing useful.
    try:
        m = get_market(s)
    except Exception:
        m = {}
    if m and m.get("ticker"):
        et = m.get("event_ticker") or ""
        # Best-effort: pick up the event title/category for richer
        # display. Cheap; one call.
        ev_cat, ev_title = "", ""
        if et:
            try:
                ev = _get(f"events/{et}").get("event") or {}
                ev_cat = ev.get("category") or ""
                ev_title = ev.get("title") or ""
            except Exception:
                pass
        from pm.venue.polymarket_client import _infer_category
        cat = ev_cat or _infer_category(_compose_question(m), events=[])
        df = pd.DataFrame([_market_to_row(m, event_category=cat,
                                            event_title=ev_title)])
        return _add_days_to_resolution(df)

    # Try as an event ticker
    try:
        ev_data = _get(f"events/{s}", params={"with_nested_markets": "true"})
    except Exception:
        return pd.DataFrame()
    ev = ev_data.get("event") or {}
    markets = ev_data.get("markets") or ev.get("markets") or []
    if not markets:
        return pd.DataFrame()
    ev_cat = ev.get("category") or ""
    ev_title = ev.get("title") or ""
    rows = [_market_to_row(m, event_category=ev_cat, event_title=ev_title)
            for m in markets]
    df = pd.DataFrame(rows)
    return _add_days_to_resolution(df)


def get_events(limit: int = 500) -> pd.DataFrame:
    """Fetch active events. Returns the normalized event-level DataFrame
    (columns: id, title, slug, category, volume_24hr, active).

    Mostly redundant for Kalshi since `get_markets` already pre-tags each
    market with its event's category. Provided to satisfy the Protocol;
    used by Streamlit's separate event cache.
    """
    events = _paginate(
        "events",
        params={"status": "open"},
        items_key="events",
        max_items=limit,
    )
    if not events:
        return pd.DataFrame(columns=["id", "title", "slug", "category", "volume_24hr", "active"])
    rows = []
    for e in events:
        rows.append({
            "id": str(e.get("event_ticker", "")),
            "title": e.get("title", ""),
            "slug": e.get("series_ticker", ""),
            "category": e.get("category", "") or "",
            "volume_24hr": 0.0,    # not reported at event-level
            "active": True,
        })
    return pd.DataFrame(rows)


def enrich_categories_from_events(
    df_markets: pd.DataFrame, df_events: pd.DataFrame
) -> pd.DataFrame:
    """No-op for Kalshi (markets already carry category from get_markets)
    but ensures `event_title` is populated if it isn't already. Returns
    the (possibly modified) df."""
    if df_markets is None or df_markets.empty:
        return df_markets
    df = df_markets.copy()
    if "event_title" not in df.columns:
        df["event_title"] = ""
    if df_events is None or df_events.empty or "id" not in df_events.columns:
        return df

    title_map = dict(zip(df_events["id"].astype(str), df_events["title"], strict=False))
    eid = df["event_id"].astype(str)
    fill = eid.map(title_map).fillna("")
    df["event_title"] = df["event_title"].where(df["event_title"] != "", fill)
    return df


def _closest_period_interval(fidelity_minutes: int) -> int:
    """Map our generic fidelity (minutes) to Kalshi's supported candle widths.
    Only 1, 60, and 1440 are accepted by the API."""
    if fidelity_minutes <= 1:
        return 1
    if fidelity_minutes < 1440 // 2:  # < 12 hours → hourly
        return 60
    return 1440


def get_price_history(ticker: str, days: int = 30, fidelity: int = 60) -> pd.DataFrame:
    """Return mid-price time series for a Kalshi market.

    Kalshi exposes per-candle yes_bid/yes_ask OHLC; we report the
    midpoint of the candle's close prices as our `price` so the shape
    matches Polymarket's `get_price_history` output exactly.
    """
    series = _series_from_ticker(ticker)
    if not series:
        return pd.DataFrame()
    period = _closest_period_interval(fidelity)
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    try:
        data = _get(
            f"series/{series}/markets/{ticker}/candlesticks",
            params={"period_interval": period, "start_ts": start_ts, "end_ts": end_ts},
        )
    except requests.HTTPError:
        return pd.DataFrame()

    candles = data.get("candlesticks") or []
    if not candles:
        return pd.DataFrame()

    rows = []
    for c in candles:
        yes_bid = c.get("yes_bid") or {}
        yes_ask = c.get("yes_ask") or {}
        bid_close = _to_float(yes_bid.get("close_dollars"))
        ask_close = _to_float(yes_ask.get("close_dollars"))
        if bid_close > 0 and ask_close > 0:
            mid = (bid_close + ask_close) / 2.0
        else:
            mid = max(bid_close, ask_close)
        rows.append({
            "timestamp": pd.to_datetime(c["end_period_ts"], unit="s", utc=True),
            "price": mid,
        })
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


def get_candlesticks(ticker: str, days: int = 30, fidelity: int = 60) -> pd.DataFrame:
    """Full candlestick history — the data behind Kalshi's "chance over
    time" graph, with the **volume + open-interest** columns that
    :func:`get_price_history` discards.

    Use this when you need traded volume or open interest (flow
    analysis, the longshot scalper's liquidity gate, lead-lag studies);
    use ``get_price_history`` when you only need the mid-price series.

    Columns: ``timestamp`` (UTC), ``price`` (mid of yes_bid/yes_ask
    closes — same definition as ``get_price_history``), ``yes_bid_close``,
    ``yes_ask_close``, ``volume`` (contracts traded in the candle),
    ``open_interest`` (contracts open at candle close).

    ``period_interval`` is constrained by Kalshi to 1 / 60 / 1440 min;
    ``fidelity`` maps to the closest. Empty DataFrame on no history or
    HTTP error (mirrors ``get_price_history``).
    """
    series = _series_from_ticker(ticker)
    if not series:
        return pd.DataFrame()
    period = _closest_period_interval(fidelity)
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    try:
        data = _get(
            f"series/{series}/markets/{ticker}/candlesticks",
            params={"period_interval": period, "start_ts": start_ts, "end_ts": end_ts},
        )
    except requests.HTTPError:
        return pd.DataFrame()

    candles = data.get("candlesticks") or []
    if not candles:
        return pd.DataFrame()

    rows = []
    for c in candles:
        yes_bid = c.get("yes_bid") or {}
        yes_ask = c.get("yes_ask") or {}
        bid_close = _to_float(yes_bid.get("close_dollars"))
        ask_close = _to_float(yes_ask.get("close_dollars"))
        if bid_close > 0 and ask_close > 0:
            mid = (bid_close + ask_close) / 2.0
        else:
            mid = max(bid_close, ask_close)
        rows.append({
            "timestamp": pd.to_datetime(c["end_period_ts"], unit="s", utc=True),
            "price": mid,
            "yes_bid_close": bid_close,
            "yes_ask_close": ask_close,
            # Kalshi's fixed-point migration renamed these to *_fp strings; the
            # bare keys are gone, and reading them stored 0 for all 19.4M
            # candle_panel rows unnoticed (found 2026-10-13). Old key kept as
            # a fallback, same pattern as the trades/fills `count_fp` reads.
            "volume": _to_float(c.get("volume_fp", c.get("volume"))),
            "open_interest": _to_float(
                c.get("open_interest_fp", c.get("open_interest"))),
        })
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Executed-trades feed (the "trade prints" in the Kalshi UI sidebar)
# ---------------------------------------------------------------------------
#
# `GET /markets/trades` returns the public tape of executed trades. Unlike
# the candlesticks (aggregated OHLC) and the order book (resting liquidity),
# this is the actual sequence of fills, each tagged with the AGGRESSOR side
# (`taker_side`) — so a burst of yes-taker volume right after a play is the
# repricing itself, visible at tick resolution. No auth. Cursor-paginated
# with optional min_ts/max_ts, which makes it BOTH a live feed and a
# historical backfill source (filling the "book_archive is forward-only"
# gap noted in baseball/analysis.py).

def _normalize_trade(d: dict) -> dict | None:
    """One raw trade dict → normalized row, or None if unparseable.

    Kalshi sends prices as ``*_dollars`` strings and size as ``count_fp``
    (fractional contracts). ``created_time`` is ISO-8601 with millisecond
    precision; we keep the unix-second timestamp for archive keying and
    range queries. ``taker_side`` is the aggressor's outcome side
    ("yes"/"no") — the direction of pressure.
    """
    tid = d.get("trade_id")
    ticker = d.get("ticker")
    if not tid or not ticker:
        return None
    created = d.get("created_time")
    ts_unix: int | None = None
    if created:
        try:
            ts_unix = int(
                pd.Timestamp(created).timestamp(),
            )
        except (ValueError, TypeError):
            ts_unix = None
    if ts_unix is None:
        return None
    # Size: prefer count_fp (fractional) and fall back to integer count.
    count = _to_float(d.get("count_fp"))
    if count <= 0:
        count = _to_float(d.get("count"))
    return {
        "trade_id": str(tid),
        "ticker": str(ticker),
        "ts_unix": ts_unix,
        "count": count,
        "yes_price": _to_float(d.get("yes_price_dollars")),
        "no_price": _to_float(d.get("no_price_dollars")),
        "taker_side": str(d.get("taker_side") or d.get("taker_outcome_side") or ""),
    }


def get_trades(
    ticker: str | None = None,
    *,
    limit: int = 1000,
    min_ts: int | None = None,
    max_ts: int | None = None,
    cursor: str | None = None,
) -> dict:
    """One page of the public executed-trades tape.

    Returns the raw Kalshi response dict (``{"trades": [...],
    "cursor": ...}``). ``ticker`` filters to one market; omit it for the
    cross-market firehose. ``min_ts`` / ``max_ts`` are unix seconds.
    ``limit`` is clamped to Kalshi's [1, 1000]. Public, no auth.
    """
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
    if ticker:
        params["ticker"] = ticker
    if min_ts is not None:
        params["min_ts"] = int(min_ts)
    if max_ts is not None:
        params["max_ts"] = int(max_ts)
    if cursor:
        params["cursor"] = cursor
    return _get("markets/trades", params=params)


def get_all_trades(
    ticker: str,
    *,
    since_ts: int | None = None,
    until_ts: int | None = None,
    max_trades: int = 100_000,
    progress_callback=None,
) -> pd.DataFrame:
    """Every executed trade for ``ticker`` in ``[since_ts, until_ts]``,
    cursor-paginated into one normalized DataFrame.

    Columns: ``trade_id``, ``ticker``, ``ts`` (UTC datetime), ``ts_unix``
    (int seconds), ``count`` (contracts), ``yes_price``, ``no_price``,
    ``taker_side`` ("yes"/"no" aggressor). Sorted oldest-first. Empty
    DataFrame when the market has no trades in the window.

    ``max_trades`` caps the walk so a runaway market can't pull forever;
    raise it for full-history backfills of busy markets.
    """
    params: dict[str, Any] = {"ticker": ticker, "limit": 1000}
    if since_ts is not None:
        params["min_ts"] = int(since_ts)
    if until_ts is not None:
        params["max_ts"] = int(until_ts)
    raw = _paginate(
        "markets/trades", params, "trades", max_trades,
        progress_callback=progress_callback,
    )
    rows = [r for r in (_normalize_trade(d) for d in raw) if r is not None]
    if not rows:
        return pd.DataFrame(
            columns=["trade_id", "ticker", "ts", "ts_unix", "count",
                     "yes_price", "no_price", "taker_side"],
        )
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts_unix"], unit="s", utc=True)
    return df.sort_values("ts_unix").reset_index(drop=True)


def get_order_book(ticker: str) -> dict:
    """Return the raw Kalshi orderbook response.

    Kalshi's response shape differs from Polymarket — it has
    `orderbook_fp.yes_dollars` and `orderbook_fp.no_dollars`, where each is
    a list of ``[price_str, size_float]`` pairs. We adapt it to the
    Polymarket-shaped dict (`{"bids": [...], "asks": [...]}`) so
    `polymarket_client.parse_order_book` works on it unchanged.

    Bids on Kalshi correspond to the YES outcome's bids; asks come from
    the NO side mirrored (selling NO at price p is equivalent to buying
    YES at 1-p, which is what an ask represents on a YES book).
    """
    raw = _get(f"markets/{ticker}/orderbook")
    book = raw.get("orderbook_fp") or {}
    yes_levels = book.get("yes_dollars") or []
    no_levels = book.get("no_dollars") or []

    bids: list[dict] = []
    for level in yes_levels:
        try:
            price = _to_float(level[0])
            size = _to_float(level[1])
        except (IndexError, TypeError, ValueError):
            continue
        if price > 0:
            bids.append({"price": str(price), "size": str(size)})

    asks: list[dict] = []
    for level in no_levels:
        try:
            no_price = _to_float(level[0])
            size = _to_float(level[1])
        except (IndexError, TypeError, ValueError):
            continue
        if no_price > 0:
            # Selling NO at $X = buying YES at $(1-X). That's an ask on the YES book.
            yes_ask_price = 1.0 - no_price
            asks.append({"price": str(yes_ask_price), "size": str(size)})

    # parse_order_book reverses both — Polymarket convention is bids ASC,
    # asks DESC. We need to match that.
    bids.sort(key=lambda x: float(x["price"]))     # ascending
    asks.sort(key=lambda x: float(x["price"]), reverse=True)  # descending
    return {"bids": bids, "asks": asks}


def get_midpoint(ticker: str) -> float | None:
    """Current mid-quote for a Kalshi market via the market detail
    endpoint. Returns None on any failure."""
    try:
        data = _get(f"markets/{ticker}")
    except requests.RequestException:
        return None
    m = data.get("market") or {}
    yes_bid = _to_float(m.get("yes_bid_dollars"))
    yes_ask = _to_float(m.get("yes_ask_dollars"))
    if yes_bid > 0 and yes_ask > 0:
        return (yes_bid + yes_ask) / 2.0
    last = _to_float(m.get("last_price_dollars"))
    return last if last > 0 else None


def get_closed_markets(limit: int = 100) -> pd.DataFrame:
    """Recently-settled Kalshi markets. Same column schema as
    `get_markets` plus `resolved_yes` and `closed_time_dt`."""
    events = _paginate(
        "events",
        params={"status": "settled", "with_nested_markets": "true"},
        items_key="events",
        max_items=limit * 2,
    )

    rows = []
    for ev in events:
        ev_cat = ev.get("category") or ""
        ev_title = ev.get("title") or ""
        for m in ev.get("markets") or []:
            row = _market_to_row(m, event_category=ev_cat, event_title=ev_title)
            result = (m.get("result") or "").lower()
            if result == "yes":
                row["resolved_yes"] = True
            elif result == "no":
                row["resolved_yes"] = False
            else:
                row["resolved_yes"] = None
            row["closed_time"] = m.get("close_time")
            rows.append(row)
        if len(rows) >= limit:
            break

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows[:limit])
    df = _add_days_to_resolution(df)
    df["closed_time_dt"] = pd.to_datetime(df["closed_time"], errors="coerce", utc=True)
    return df


def get_closed_markets_in_range(
    *,
    since_days: float,
    until_days: float = 0.0,
    max_items: int = 10000,
    min_volume: float = 0.0,
    progress_callback=None,
) -> pd.DataFrame:
    """Settled Kalshi markets that closed in a UTC date window relative
    to *now*.

    ``since_days`` is how far back to start (e.g., 90 → 90 days ago);
    ``until_days`` is how far back to stop (default 0 = present). The
    window is therefore ``[now - since_days, now - until_days]`` and
    ``since_days > until_days``.

    Mirrors ``get_markets_closing_within`` but for resolved markets.
    Uses ``/markets`` with ``min_close_ts`` / ``max_close_ts`` filters
    + cursor pagination — this is the only honest way to pull a large
    historical sample. Same row schema as ``get_closed_markets`` plus
    ``closed_time_dt``.

    ``min_volume`` filters out markets that never actually traded.
    **Strongly recommended for backtesting** — Kalshi's ``/markets``
    endpoint includes auto-generated cross-game parlays (the
    ``KXMVESPORTSMULTIGAMEEXTENDED-*`` series) that account for ~99%
    of the settled-market count but have zero volume and no candle
    history. Default 0 returns everything for compatibility.

    Category enrichment uses the cached events lookup. Markets whose
    parent event isn't in the open-events cache fall through to
    keyword inference (since finalized markets often correspond to
    events that closed; the events scan only walks ``status=open``).
    """
    if since_days <= until_days:
        raise ValueError(
            f"since_days ({since_days}) must be > until_days ({until_days})",
        )
    import time
    now = time.time()
    min_ts = now - since_days * 86400
    max_ts = now - until_days * 86400

    # WHY NOT /markets?status=settled WITH min/max_close_ts?
    #
    # The /markets endpoint with status=settled returns ~99% auto-
    # generated synthetic markets (KXMVE* parlays, 15-minute crypto
    # bets) whose JSON omits the `volume_fp` field entirely. The
    # remaining real markets are needle-in-haystack and the volume
    # filter can't surface them because the raw endpoint doesn't
    # report volume on those rows.
    #
    # The /events?status=settled&with_nested_markets=true path returns
    # CURATED settled events with their markets nested. ~84% of these
    # have non-zero volume (i.e., they actually traded). That's the
    # universe we want for backtesting.
    #
    # The events endpoint doesn't accept min_close_ts / max_close_ts
    # natively, so we paginate broadly and filter client-side. Kalshi
    # appears to order settled events by close_time descending, so we
    # can short-circuit pagination once we go past the since_days
    # window.
    log.info(
        "fetching settled events for window [-%.1fd, -%.1fd]",
        since_days, until_days,
    )

    # Walk events broadly. We multiply max_items by an expected events-
    # per-market factor (each event holds ~3-10 markets on average) and
    # cap at 50k events to avoid hammering the API.
    event_cap = min(int(max_items * 2), 50_000)
    events = _paginate(
        "events",
        params={"status": "settled", "with_nested_markets": "true"},
        items_key="events",
        max_items=event_cap,
        progress_callback=progress_callback,
    )
    if not events:
        return pd.DataFrame()
    log.info("fetched %d settled events", len(events))

    # Collect markets, filter by close_time + (optional) volume + valid
    # resolution. Kalshi returns markets with close_time as ISO; convert
    # to timestamp for comparison.
    from pm.venue.polymarket_client import _infer_category
    rows: list[dict] = []
    for ev in events:
        ev_cat = ev.get("category") or ""
        ev_title = ev.get("title") or ""
        for m in ev.get("markets") or []:
            ct = m.get("close_time")
            if not ct:
                continue
            try:
                ts = pd.Timestamp(ct).tz_convert("UTC").timestamp()
            except Exception:
                continue
            if not (min_ts <= ts <= max_ts):
                continue
            cat = ev_cat
            if not cat:
                cat = _infer_category(_compose_question(m), events=[])
            row = _market_to_row(
                m, event_category=cat, event_title=ev_title,
            )
            # Volume filter: events path populates volume_fp reliably,
            # so this actually works.
            if min_volume > 0 and float(row.get("volume", 0) or 0) < min_volume:
                continue
            result = (m.get("result") or "").lower()
            if result == "yes":
                row["resolved_yes"] = True
            elif result == "no":
                row["resolved_yes"] = False
            else:
                row["resolved_yes"] = None
            row["closed_time"] = ct
            rows.append(row)
            if len(rows) >= max_items:
                break
        if len(rows) >= max_items:
            break

    log.info("after filtering: %d markets meet criteria", len(rows))
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = _add_days_to_resolution(df)
    df["closed_time_dt"] = pd.to_datetime(
        df["closed_time"], errors="coerce", utc=True,
    )
    if "closed_time_dt" in df.columns:
        df = df.sort_values(
            "closed_time_dt", ascending=False,
        ).reset_index(drop=True)
    return df


def market_price_at_offset_before_close(
    ticker: str,
    closed_at,
    lead_hours: float,
    fidelity: int = 1440,
    window_days: int = 90,
) -> float | None:
    """Find Kalshi's YES mid-price approximately `lead_hours` before close.
    Used for the market-calibration backfill on the Calibration page.

    Returns None if no candle history is available for that window.
    """
    if closed_at is None:
        return None
    closed_ts = pd.to_datetime(closed_at, utc=True, errors="coerce")
    if pd.isna(closed_ts):
        return None

    target = closed_ts - pd.Timedelta(hours=lead_hours)
    if pd.isna(target):
        return None

    df = get_price_history(ticker, days=window_days, fidelity=fidelity)
    if df.empty:
        return None

    df = df.copy()
    df["delta"] = (df["timestamp"] - target).abs()
    return float(df.loc[df["delta"].idxmin()]["price"])


def get_market_calibration_data(
    df_closed: pd.DataFrame,
    lead_hours: float = 24,
    max_markets: int = 100,
    progress_callback=None,
) -> pd.DataFrame:
    """Same shape as polymarket_client.get_market_calibration_data — for
    each closed market, sample the YES price `lead_hours` before close and
    compare to the resolution."""
    if df_closed is None or df_closed.empty:
        return pd.DataFrame()

    df = df_closed[df_closed["resolved_yes"].notna()].head(max_markets).copy()
    if df.empty:
        return pd.DataFrame()

    rows = []
    total = len(df)
    for i, (_, row) in enumerate(df.iterrows()):
        if progress_callback is not None:
            try:
                progress_callback(i, total)
            except Exception:
                pass

        ticker = row["condition_id"]
        predicted = market_price_at_offset_before_close(
            ticker, row["closed_time_dt"], lead_hours=lead_hours,
        )
        if predicted is None:
            continue

        resolved_yes = bool(row["resolved_yes"])
        actual = 1.0 if resolved_yes else 0.0
        rows.append({
            "condition_id": ticker,
            "question": row["question"],
            "category": row.get("category", ""),
            "predicted_prob": predicted,
            "resolved_yes": resolved_yes,
            "actual": actual,
            "brier_term": (predicted - actual) ** 2,
            "lead_hours": lead_hours,
        })

    if progress_callback is not None:
        try:
            progress_callback(total, total)
        except Exception:
            pass

    if not rows:
        return pd.DataFrame(columns=[
            "condition_id", "question", "category", "predicted_prob",
            "resolved_yes", "actual", "brier_term", "lead_hours",
        ])
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Authenticated portfolio endpoints
# ---------------------------------------------------------------------------
# These wrap kalshi_auth.authed_get and shape the response into the
# normalized DataFrames pages render against. Each one is best-effort:
# if `kalshi_auth.is_configured()` is False the page should display a
# "configure your API key" prompt rather than calling these, but they
# also tolerate a missing key by raising KalshiAuthError so a misuse
# fails loudly rather than silently returning empty data.

# Kalshi's API returns numeric fields with type-tagged suffixes:
#   *_fp       — string-encoded floating point (e.g. "33.00")
#   *_dollars  — string-encoded dollar amount (e.g. "19.470000")
#   *_dollars on prices  — string-encoded price-in-dollars (e.g. "0.5900")
# We were originally reading bare field names ("position", "balance",
# etc.) and getting zero on every field. Helpers below coerce the
# typed fields back to ints in our schema's native units (cents).

def _dollars_to_cents(v: Any) -> int:
    """Kalshi dollar string ("19.470000") → cents int (1947)."""
    if v is None or v == "":
        return 0
    try:
        return int(round(float(v) * 100))
    except (TypeError, ValueError):
        return 0


def _fp_to_int(v: Any) -> int:
    """Kalshi floating-point string ("-33.00") → signed int (-33)."""
    if v is None or v == "":
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def get_balance() -> dict:
    """Return ``{"balance_cents": int, "payout_cents": int}`` — the
    member's settled cash and pending winnings.
    """
    from pm.venue import kalshi_auth as ka
    data = ka.authed_get("portfolio/balance")
    # Kalshi returns either `balance` (int cents) or `balance_dollars`
    # (string dollars) depending on endpoint version. Handle both.
    bal = data.get("balance")
    if bal is None:
        bal = _dollars_to_cents(data.get("balance_dollars"))
    payout = data.get("payout")
    if payout is None:
        payout = _dollars_to_cents(data.get("payout_dollars"))
    return {"balance_cents": int(bal or 0), "payout_cents": int(payout or 0)}


def get_authed_positions(*, limit: int = 200,
                          non_zero_only: bool = True) -> pd.DataFrame:
    """Authenticated positions across markets. One row per held
    contract count.

    Cols: ``ticker``, ``position`` (signed contract count; positive=YES
    long, negative=NO long via Kalshi's convention), ``market_exposure_cents``
    (current $ value if liquidated at last price), ``realized_pnl_cents``,
    ``total_traded_cents``, ``resting_orders_count``, ``last_updated_ts``.
    """
    from pm.venue import kalshi_auth as ka
    params: dict[str, Any] = {"limit": min(int(limit), 200)}
    if non_zero_only:
        params["count_filter"] = "position"
    items = ka.authed_paginate(
        "portfolio/positions", params=params,
        items_key="market_positions", max_items=int(limit),
    )
    if not items:
        return pd.DataFrame(columns=[
            "ticker", "position", "market_exposure_cents",
            "realized_pnl_cents", "total_traded_cents",
            "resting_orders_count", "last_updated_ts",
        ])
    rows = []
    for p in items:
        rows.append({
            "ticker": p.get("ticker", ""),
            "position": _fp_to_int(p.get("position_fp", p.get("position"))),
            "market_exposure_cents":
                _dollars_to_cents(p.get("market_exposure_dollars",
                                          p.get("market_exposure"))),
            "realized_pnl_cents":
                _dollars_to_cents(p.get("realized_pnl_dollars",
                                          p.get("realized_pnl"))),
            "total_traded_cents":
                _dollars_to_cents(p.get("total_traded_dollars",
                                          p.get("total_traded"))),
            "resting_orders_count": int(p.get("resting_orders_count") or 0),
            "last_updated_ts": p.get("last_updated_ts", ""),
        })
    return pd.DataFrame(rows)


def get_fills(*, limit: int = 200, ticker: str | None = None) -> pd.DataFrame:
    """Authenticated fill history. One row per matched trade.

    Cols: ``ticker``, ``order_id``, ``trade_id``, ``side`` ("yes"|"no"),
    ``action`` ("buy"|"sell"), ``count``, ``yes_price_cents``,
    ``no_price_cents``, ``is_taker``, ``created_time``.
    """
    from pm.venue import kalshi_auth as ka
    params: dict[str, Any] = {"limit": min(int(limit), 1000)}
    if ticker:
        params["ticker"] = ticker
    items = ka.authed_paginate(
        "portfolio/fills", params=params,
        items_key="fills", max_items=int(limit),
    )
    if not items:
        return pd.DataFrame(columns=[
            "ticker", "order_id", "trade_id", "side", "action", "count",
            "yes_price_cents", "no_price_cents", "is_taker", "created_time",
        ])
    rows = []
    for f in items:
        rows.append({
            "ticker": f.get("ticker", ""),
            "order_id": f.get("order_id", ""),
            "trade_id": f.get("trade_id", ""),
            "side": f.get("side", ""),
            "action": f.get("action", ""),
            "count": _fp_to_int(f.get("count_fp", f.get("count"))),
            "yes_price_cents":
                _dollars_to_cents(f.get("yes_price_dollars",
                                          f.get("yes_price"))),
            "no_price_cents":
                _dollars_to_cents(f.get("no_price_dollars",
                                          f.get("no_price"))),
            "is_taker": bool(f.get("is_taker", False)),
            "created_time": f.get("created_time", ""),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["created_time_dt"] = pd.to_datetime(
            df["created_time"], errors="coerce", utc=True,
        )
        df = df.sort_values("created_time_dt", ascending=False).reset_index(drop=True)
    return df


def _mark_to_market_price(yes_bid: float | None, yes_ask: float | None,
                            last_trade: float | None) -> tuple[float, str]:
    """Pick the best mark-to-market price for a YES position, plus a
    status tag. Returns (price, status).

    Why not just use the bid: on Kalshi, post-resolution markets and
    one-sided books often have stale bids many cents below the actual
    market consensus. A market with YES last-traded at 99¢ but a 2¢
    resting bid is not "worth 2¢ to me" — that's just nobody bidding
    yet on a market that's about to settle YES. Kalshi's UI uses last
    trade for the position-value display.

    Decision order:
      1. Mid of bid+ask when both are present AND the spread is tight
         (≤ 10¢) — best signal in liquid two-sided books.
      2. Last trade when it exists and either (a) bid is stale (last
         differs from bid by ≥ 10¢) or (b) no bid available.
      3. Bid alone when there's no last trade.
      4. Zero (status="unknown") when nothing is set.
    """
    # Bind narrowed locals rather than boolean flags: a separate `has_x`
    # flag carries no type information, so every later use of yes_bid was
    # still Optional and the arithmetic below was unverifiable.
    bid = yes_bid if (yes_bid is not None and yes_bid > 0) else None
    ask = yes_ask if (yes_ask is not None and yes_ask > 0) else None
    last = last_trade if (last_trade is not None and last_trade > 0) else None

    if bid is not None and ask is not None:
        spread = ask - bid
        if spread <= 0.10:
            return (bid + ask) / 2.0, "ok"
        # Wide spread — defer to last-trade if available
        if last is not None:
            return last, "ok"
        return (bid + ask) / 2.0, "ok"

    if last is not None:
        # One-sided or empty book — last trade is the best signal we
        # have. Common for closed markets awaiting settlement.
        if bid is not None and abs(bid - last) >= 0.10:
            return last, "closed"
        return last, "ok"

    if bid is not None:
        return bid, "ok"

    return 0.0, "unknown"


def estimate_cashout(*, position: int, yes_bid: float | None,
                       yes_ask: float | None,
                       last_trade: float | None = None,
                       cost_basis_cents: int = 0) -> dict:
    """Estimate the mark-to-market value of a position right now.
    Pure function; no I/O.

    The "cash-out" terminology is slightly loose — the actual price
    you'd receive selling immediately is the bid (potentially worse
    than this number on wide-spread markets). But for "what's this
    position worth?" the right answer is closer to the last-trade or
    mid price, not the bid. See ``_mark_to_market_price`` for the
    decision rules.

    Original simple rule (still the case for liquid two-sided books):
      - position > 0 (YES long)  →  sell YES at ``yes_bid``
      - position < 0 (NO long)   →  sell NO at ``no_bid = 1 - yes_ask``

    Returns ``{cashout_cents, exit_price_cents, side, pnl_cents,
    pnl_pct, status}`` where:
      - ``status`` is ``"ok"`` (fresh bid), ``"closed"`` (no bid but
        we have a last-trade fallback), or ``"unknown"`` (no signal).
      - ``pnl_cents`` and ``pnl_pct`` are computed against
        ``cost_basis_cents`` (the user's entry stake).

    Honest behaviour on closed markets: when bids are zero (Kalshi
    pulls quotes once results come in), we fall back to ``last_trade``
    as the implied settlement value and mark ``status="closed"`` so
    the UI can flag "this is expected settlement, not a fresh quote".
    """
    if position == 0:
        return {
            "cashout_cents": 0, "exit_price_cents": 0, "side": "",
            "pnl_cents": 0, "pnl_pct": 0.0, "status": "ok",
        }

    side = "YES" if position > 0 else "NO"
    abs_pos = abs(int(position))

    # Pick mark-to-market price. For YES long this is the YES m2m
    # price; for NO long it's the inverse (1 - YES m2m). Going through
    # the YES-side helper keeps the bid/ask/last logic in one place.
    yes_m2m_price, status = _mark_to_market_price(yes_bid, yes_ask, last_trade)
    if side == "YES":
        exit_price = yes_m2m_price
    else:
        # NO m2m = 1 − YES m2m, recomputed from the inverted view so
        # status reflects what the NO side sees. Symmetric: if YES
        # bid is stale, NO ask is stale, and we want to use last
        # trade (inverted) for NO too.
        no_bid = (1.0 - yes_ask) if yes_ask is not None else None
        no_ask = (1.0 - yes_bid) if yes_bid is not None else None
        no_last = (1.0 - last_trade) if last_trade is not None else None
        exit_price, status = _mark_to_market_price(no_bid, no_ask, no_last)

    # Self-consistent rounding: compute cashout from the rounded
    # exit-price-cents so display (price × count) ties out.
    exit_price_cents = int(round(exit_price * 100))
    cashout_cents = abs_pos * exit_price_cents
    pnl_cents = cashout_cents - int(cost_basis_cents)
    pnl_pct = (pnl_cents / cost_basis_cents * 100) if cost_basis_cents else 0.0

    return {
        "cashout_cents": cashout_cents,
        "exit_price_cents": exit_price_cents,
        "side": side,
        "pnl_cents": pnl_cents,
        "pnl_pct": round(pnl_pct, 1),
        "status": status,
    }


def get_settlements(*, limit: int = 200) -> pd.DataFrame:
    """Authenticated settlement history. One row per resolved market
    the member held a position in.

    Cols: ``ticker``, ``market_result`` ("yes"|"no"|""), ``yes_count``,
    ``no_count``, ``revenue_cents``, ``settled_time``.
    """
    from pm.venue import kalshi_auth as ka
    items = ka.authed_paginate(
        "portfolio/settlements",
        params={"limit": min(int(limit), 1000)},
        items_key="settlements", max_items=int(limit),
    )
    if not items:
        return pd.DataFrame(columns=[
            "ticker", "market_result", "yes_count", "no_count",
            "revenue_cents", "settled_time",
        ])
    rows = []
    for s in items:
        # Settlements quirk: ``revenue`` is an INTEGER already in
        # cents (e.g. 3300 = $33.00), unlike most other Kalshi numeric
        # fields which are dollar-string-suffixed. ``revenue_dollars``
        # also exists in some response shapes — handle both.
        rev_dollars = s.get("revenue_dollars")
        if rev_dollars is not None and rev_dollars != "":
            revenue_cents = _dollars_to_cents(rev_dollars)
        else:
            revenue_cents = int(s.get("revenue") or 0)
        rows.append({
            "ticker": s.get("ticker", ""),
            "market_result": s.get("market_result", ""),
            "yes_count": _fp_to_int(s.get("yes_count_fp", s.get("yes_count"))),
            "no_count": _fp_to_int(s.get("no_count_fp", s.get("no_count"))),
            "revenue_cents": revenue_cents,
            "settled_time": s.get("settled_time", ""),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["settled_time_dt"] = pd.to_datetime(
            df["settled_time"], errors="coerce", utc=True,
        )
        df = df.sort_values("settled_time_dt", ascending=False).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------

class KalshiProvider:
    """Kalshi adapter satisfying the MarketProvider Protocol."""

    name = "kalshi"
    display_name = "Kalshi"

    def get_markets(self, limit: int = 500, active: bool = True,
                    order: str = "volume24hr") -> pd.DataFrame:
        return get_markets(limit=limit, active=active, order=order)

    def get_market(self, market_id: str) -> dict:
        return get_market(market_id)

    def get_events(self, limit: int = 500) -> pd.DataFrame:
        return get_events(limit=limit)

    def enrich_categories_from_events(self, df_markets, df_events):
        return enrich_categories_from_events(df_markets, df_events)

    def get_price_history(self, token_id: str, days: int = 30,
                          fidelity: int = 60) -> pd.DataFrame:
        return get_price_history(token_id, days=days, fidelity=fidelity)

    def get_order_book(self, token_id: str) -> dict:
        return get_order_book(token_id)

    def get_midpoint(self, token_id: str) -> float | None:
        return get_midpoint(token_id)

    def list_outcome_tokens(self, market_id: str) -> list[dict]:
        # Kalshi markets are binary; the ticker IS the identifier and
        # represents the YES side. NO is derived by price-mirror in the
        # order book / midpoint logic, so we only emit YES here.
        return [{"token_id": str(market_id), "outcome": "Yes"}]

    def get_closed_markets(self, limit: int = 100) -> pd.DataFrame:
        return get_closed_markets(limit=limit)

    def get_closed_markets_in_range(
        self, *, since_days: float, until_days: float = 0.0,
        max_items: int = 10000, min_volume: float = 0.0,
        progress_callback=None,
    ) -> pd.DataFrame:
        return get_closed_markets_in_range(
            since_days=since_days, until_days=until_days,
            max_items=max_items, min_volume=min_volume,
            progress_callback=progress_callback,
        )

    def get_markets_closing_within(self, max_days: float, *, limit: int = 500,
                                     min_days: float = 0.0) -> pd.DataFrame:
        return get_markets_closing_within(
            max_days=max_days, limit=limit, min_days=min_days,
        )

    def lookup_markets_by_id(self, market_or_event_id: str) -> pd.DataFrame:
        return lookup_markets_by_id(market_or_event_id)

    def get_market_calibration_data(self, df_closed, lead_hours: float = 24,
                                     max_markets: int = 100,
                                     progress_callback=None) -> pd.DataFrame:
        return get_market_calibration_data(
            df_closed, lead_hours=lead_hours, max_markets=max_markets,
            progress_callback=progress_callback,
        )
