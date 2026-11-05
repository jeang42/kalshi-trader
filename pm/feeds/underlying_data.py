"""Commodity / index / FX underlying quotes — Yahoo Finance chart endpoint.

Purpose: extend the near-close z-instrumentation (spot-distance at entry,
docs/edge-research-log.md 2026-08-07) beyond crypto to the commodity/finance
classes — `back_nearclose`'s LARGEST flow. Crypto has Coinbase
(`crypto_data`); everything else Kalshi writes threshold markets on (gold,
silver, nat-gas, copper, WTI, S&P, Dow, USDJPY, treasury yields) has no spot
client in this repo until now.

Why Yahoo's v8 chart API: no key, plain requests, one endpoint covers futures
(`GC=F`), indices (`^GSPC`), FX (`JPY=X`) and yields (`^TYX`), and it serves
both a current quote and intraday 60m history (the hourly-vol scale the
z-feature needs). It is UNOFFICIAL — treat every response defensively; a shape
change must degrade to None, never crash an arm's cycle.

Two ways this feed differs from crypto, both load-bearing:

1. **Staleness.** These markets CLOSE (nights for indices, weekends for
   everything). A Friday-close quote is meaningless for a Saturday-settling
   market, so every quote carries its exchange timestamp and consumers must
   enforce a max age (`fade_nearclose.z_for_entry` refuses quotes older than
   ``MAX_SPOT_AGE_S``). Verified at build time: on a Saturday all ten symbols
   returned ~20-hour-old quotes — silently computing z from those would have
   poisoned the whole commodity sample.
2. **Scale traps.** Symbols were verified one-by-one against real Kalshi
   strikes at build time (GC=F 4399 vs gold strikes ~4300; CL=F 78.2 vs WTI
   75-80; ^GSPC 7757 vs INX ~7700; ^TYX 5.21 vs KXUST30A strike 5.19 —
   i.e. yields quote UNSCALED here). Yields additionally pass through a
   0-25 sanity band because a silent scale change would produce huge,
   plausible-looking, wrong z values.

Keys of SYMBOLS are `fade_nearclose.asset_of` outputs, so the join is one
dict lookup. Env: UNDERLYING_HTTP_TIMEOUT.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime

import pandas as pd
import requests

from pm.core import circuit_breaker

log = logging.getLogger(__name__)

_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
HTTP_TIMEOUT = float(os.getenv("UNDERLYING_HTTP_TIMEOUT", "15"))

# asset_of() root -> (yahoo symbol, sanity band or None)
SYMBOLS: dict[str, tuple[str, tuple[float, float] | None]] = {
    "GOLD":   ("GC=F", None),
    "SILVER": ("SI=F", None),
    "NATGAS": ("NG=F", None),
    "COPPER": ("HG=F", None),
    "WTI":    ("CL=F", None),        # excluded from back entries; kept for studies
    "BRENT":  ("BZ=F", None),
    "INX":    ("^GSPC", None),
    "DJI":    ("^DJI", None),
    "NASDAQ": ("^NDX", None),        # excluded from back entries; kept for studies
    "USDJPY": ("JPY=X", None),
    "EURUSD": ("EURUSD=X", None),
    # Treasury yields: verified UNSCALED at build time (^TYX 5.21 vs Kalshi
    # strike 5.19), but Yahoo has historically served these x10 — the sanity
    # band turns a silent scale flip into Nones instead of wrong z values.
    "KXUST30A": ("^TYX", (0.0, 25.0)),
    "KXUST10A": ("^TNX", (0.0, 25.0)),
    "KXUST5A":  ("^FVX", (0.0, 25.0)),
}

_session = requests.Session()
# Yahoo intermittently 429s the default python UA
_session.headers.update({"User-Agent": "Mozilla/5.0 (polymarket-analyzer)"})


def supported(root: str) -> bool:
    """Does this asset_of() root have a quote source here?

    >>> supported("GOLD")
    True
    >>> supported("SOL")            # crypto lives in crypto_data, not here
    False
    >>> supported("KXRAIN")
    False
    """
    return str(root) in SYMBOLS


def _chart(symbol: str, *, interval: str, range_: str) -> dict | None:
    """One chart call -> the `result[0]` dict, or None on any failure/shape
    surprise. Unofficial API: everything is optional until proven present."""
    def _fetch() -> dict | None:
        r = _session.get(f"{_BASE}/{symbol}",
                         params={"interval": interval, "range": range_},
                         timeout=HTTP_TIMEOUT)
        if r.status_code == 429:
            log.warning("Yahoo 429 for %s", symbol)
            return None
        r.raise_for_status()
        result = (r.json().get("chart") or {}).get("result") or []
        return result[0] if result else None

    try:
        return circuit_breaker.get("yahoo").call(_fetch)   # R-21
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        log.warning("chart(%s) failed: %s", symbol, e)
        return None


def get_spot(root: str) -> tuple[float, float] | None:
    """(price, quote_age_seconds) for an asset root, or None.

    The age is the caller's problem BY DESIGN: only the caller knows its
    horizon (a 20-min-old gold quote is fine at a 2h lead; a 20-HOUR-old
    weekend quote is not), so this function never silently drops stale data —
    it reports honestly and lets policy live at the use site.
    """
    entry = SYMBOLS.get(str(root))
    if entry is None:
        return None
    symbol, band = entry
    data = _chart(symbol, interval="1m", range_="1d")
    meta = (data or {}).get("meta") or {}
    price, qtime = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if price is None or qtime is None:
        return None
    price = float(price)
    if band and not (band[0] <= price <= band[1]):
        log.warning("get_spot(%s): %s=%s outside sanity band %s — refusing",
                    root, symbol, price, band)
        return None
    age = max(0.0, datetime.now(UTC).timestamp() - float(qtime))
    return price, age


_SIGMA_CACHE: dict[str, tuple[float, float]] = {}    # root -> (fetched_at, sigma)
_SIGMA_TTL_S = 6 * 3600


def hourly_sigma(root: str, *, days: int = 14) -> float | None:
    """Std-dev of hourly pct returns over recent 60m candles, cached ~6h.

    Same role as crypto_data.hourly_sigma. Sessioned markets yield fewer
    candles per calendar day than 24/7 crypto (indices ~7/day); the std of
    the hourly returns that DO exist is still the right per-trading-hour
    scale, and near-close markets only settle during trading hours anyway.
    """
    key = str(root)
    if key not in SYMBOLS:
        return None
    now = time.time()
    hit = _SIGMA_CACHE.get(key)
    if hit and now - hit[0] < _SIGMA_TTL_S:
        return hit[1]
    data = _chart(SYMBOLS[key][0], interval="60m", range_=f"{max(2, days)}d")
    try:
        closes = (data or {})["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return None
    s = pd.Series([c for c in closes if c is not None], dtype=float)
    if len(s) < 24:
        return None
    sig = float(s.pct_change().dropna().std())
    if not sig or sig != sig:
        return None
    _SIGMA_CACHE[key] = (now, sig)
    return sig
