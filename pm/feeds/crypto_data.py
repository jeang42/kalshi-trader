"""Crypto spot-price data layer — Coinbase Exchange public endpoints.

Why Coinbase Exchange (formerly Coinbase Pro) instead of Binance: the
``api.binance.com`` market-data endpoints return HTTP 451 to US IPs
(the entire domain is geo-blocked, not just trading). Coinbase
Exchange's market-data endpoints are public, US-accessible, no API key
needed, and cover every major USD pair we care about.

Endpoints we use:
- ``GET /products/<product>/ticker`` — current price
- ``GET /products/<product>/candles`` — historical OHLC, max 300 candles
  per request, so we paginate by start/end timestamps for longer windows

The lognormal terminal-probability model in :func:`lognormal_above` is
deliberately simple: Black-Scholes-style under a risk-neutral measure
with ``r=0`` (rates are negligible at the time horizons we care about).
It is **not** an option-pricing engine — it's a clean reference value
for "given current spot, realized volatility, and time-to-expiry, what
should the market be pricing this binary?"
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import UTC, datetime, timedelta

import pandas as pd
import requests

from pm.core import circuit_breaker

log = logging.getLogger(__name__)

UTC = UTC

COINBASE_BASE = os.getenv("COINBASE_API_BASE", "https://api.exchange.coinbase.com")
HTTP_TIMEOUT = float(os.getenv("CRYPTO_HTTP_TIMEOUT", "15"))

# Asset → Coinbase product (USD pair).
SYMBOLS: dict[str, str] = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "DOGE": "DOGE-USD",
    "XRP": "XRP-USD",
    "ADA": "ADA-USD",
}

# Coinbase capabilities: max 300 candles per /candles request.
_MAX_CANDLES_PER_REQUEST = 300

_session = requests.Session()
_session.headers.update({"User-Agent": "polymarket-analyzer/1.0"})


def _get(path: str, *, params: dict | None = None, retries: int = 2) -> object:
    """Under the ``coinbase`` circuit breaker (R-21): fails fast with
    ``CircuitOpenError`` while the dependency is down."""
    return circuit_breaker.get("coinbase").call(_get_unguarded, path, params=params,
                                             retries=retries)


def _get_unguarded(path: str, *, params: dict | None = None, retries: int = 2) -> object:
    """Minimal HTTP wrapper with the same 429 backoff pattern used in
    polymarket_client and kalshi_client."""
    url = f"{COINBASE_BASE}/{path.lstrip('/')}"
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                sleep_s = max(0.1, min(30.0, float(r.headers.get("Retry-After", "1"))))
                log.warning("Coinbase 429; retrying in %.1fs", sleep_s)
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
# Spot
# ---------------------------------------------------------------------------

def get_spot_price(asset: str) -> float | None:
    """Current spot USD price for an asset symbol (e.g. ``"BTC"``).
    Returns None for unknown assets or on transient HTTP failure."""
    product = SYMBOLS.get(asset.upper())
    if product is None:
        return None
    try:
        data = _get(f"products/{product}/ticker")
        return float(data["price"])  # type: ignore[index, call-overload]
    except (requests.RequestException, KeyError, ValueError, TypeError) as e:
        log.warning("get_spot_price(%s) failed: %s", asset, e)
        return None


# ---------------------------------------------------------------------------
# Historical klines (OHLC)
# ---------------------------------------------------------------------------

def get_klines(asset: str, *, days: int = 90) -> pd.DataFrame:
    """Daily OHLC candles for the last ``days``. Empty DataFrame on
    failure or unknown asset.

    Coinbase caps each ``/candles`` response at 300 rows. For ``days >
    300`` we paginate by passing successive ``start``/``end`` windows.

    Columns: ``timestamp`` (UTC datetime), ``open``, ``high``, ``low``,
    ``close``, ``volume`` (all floats).
    """
    product = SYMBOLS.get(asset.upper())
    if product is None:
        return pd.DataFrame()

    days = max(1, days)
    end = datetime.now(UTC)
    rows: list[dict] = []
    remaining = days
    cursor = end

    while remaining > 0:
        chunk_days = min(_MAX_CANDLES_PER_REQUEST, remaining)
        start = cursor - timedelta(days=chunk_days)
        try:
            data = _get(
                f"products/{product}/candles",
                params={
                    "granularity": 86400,
                    "start": start.isoformat(),
                    "end": cursor.isoformat(),
                },
            )
        except requests.RequestException as e:
            log.warning("get_klines(%s) failed: %s", asset, e)
            break

        if not isinstance(data, list) or not data:
            break

        # Coinbase candle format: [time(s), low, high, open, close, volume]
        for c in data:
            rows.append({
                "timestamp": datetime.fromtimestamp(c[0], tz=UTC),
                "low":    float(c[1]),
                "high":   float(c[2]),
                "open":   float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })

        cursor = start
        remaining -= chunk_days

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).drop_duplicates(subset="timestamp")
    return df.sort_values("timestamp").reset_index(drop=True)[
        ["timestamp", "open", "high", "low", "close", "volume"]
    ]


def get_hourly_closes(asset: str, *, hours: int = 336) -> pd.DataFrame:
    """Hourly close prices for the last ``hours`` (default 14 days).

    Exists because ``get_klines`` is hardcoded to daily granularity, and the
    near-close arms' z-feature needs HOURLY realized vol — daily returns are
    the wrong scale for markets settling within ~2 hours (the 2026-08-07 study
    initially used daily candles by accident and its vol was ~5× off).

    Columns: ``timestamp`` (UTC datetime), ``close`` (float).
    """
    product = SYMBOLS.get(asset.upper())
    if product is None:
        return pd.DataFrame()
    hours = max(2, hours)
    end = datetime.now(UTC)
    rows: list[dict] = []
    cursor = end
    while (end - cursor) < timedelta(hours=hours):
        start = cursor - timedelta(hours=_MAX_CANDLES_PER_REQUEST)
        try:
            data = _get(
                f"products/{product}/candles",
                params={"granularity": 3600,
                        "start": start.isoformat(), "end": cursor.isoformat()},
            )
        except requests.RequestException as e:
            log.warning("get_hourly_closes(%s) failed: %s", asset, e)
            break
        if not isinstance(data, list) or not data:
            break
        for c in data:
            rows.append({"timestamp": datetime.fromtimestamp(c[0], tz=UTC),
                         "close": float(c[4])})
        cursor = start
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset="timestamp")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df.tail(hours).reset_index(drop=True)


_HOURLY_SIGMA_CACHE: dict[str, tuple[float, float]] = {}   # asset -> (ts, sigma)
_HOURLY_SIGMA_TTL_S = 6 * 3600


def hourly_sigma(asset: str, *, hours: int = 336) -> float | None:
    """Std-dev of hourly pct returns over the last ``hours`` — the vol scale
    for the near-close z-feature. Cached ~6h per asset (long-lived daemons call
    this every cycle; realized vol at this window moves slowly). None on
    unknown asset / fetch failure / too little history."""
    key = asset.upper()
    now = time.time()
    hit = _HOURLY_SIGMA_CACHE.get(key)
    if hit and now - hit[0] < _HOURLY_SIGMA_TTL_S:
        return hit[1]
    df = get_hourly_closes(key, hours=hours)
    if df.empty or len(df) < 48:
        return None
    sig = float(df["close"].pct_change().dropna().std())
    if not sig or sig != sig:      # 0.0 or NaN → useless
        return None
    _HOURLY_SIGMA_CACHE[key] = (now, sig)
    return sig


def spot_at(asset: str, ts: datetime, *, history_window_days: int = 365) -> float | None:
    """Historical close at-or-before ``ts``. Used by the backtest engine
    to look up "what was the spot price at the as_of of this market
    snapshot."

    For points in the future or beyond the cached window, returns None.
    Caller is expected to cache the underlying kline series.
    """
    df = get_klines(asset, days=history_window_days)
    if df.empty:
        return None
    eligible = df[df["timestamp"] <= ts]
    if eligible.empty:
        return None
    return float(eligible["close"].iloc[-1])


# ---------------------------------------------------------------------------
# Realized volatility (annualized)
# ---------------------------------------------------------------------------

def realized_volatility(df_klines: pd.DataFrame, *, window: int = 30) -> float | None:
    """Annualized realized volatility from the last ``window`` daily closes.

    Standard log-return std-dev × √365 — the simplest defensible
    volatility estimator. Returns None when there isn't enough data.
    """
    if df_klines.empty or len(df_klines) < window + 1:
        return None
    closes = df_klines["close"].iloc[-(window + 1):]
    log_returns = (closes / closes.shift(1)).dropna().apply(math.log)
    if log_returns.empty:
        return None
    daily_std = float(log_returns.std())
    if daily_std <= 0:
        return None
    return daily_std * math.sqrt(365.0)


def realized_volatility_at(
    asset: str, ts: datetime, *, window: int = 30, history_window_days: int = 365,
) -> float | None:
    """Annualized realized vol over the ``window`` days ending at ``ts``.
    Used by the backtest engine — uses only data strictly before ``ts``.
    """
    df = get_klines(asset, days=history_window_days)
    if df.empty:
        return None
    before = df[df["timestamp"] < ts]
    if len(before) < window + 1:
        return None
    return realized_volatility(before.tail(window + 1), window=window)


# ---------------------------------------------------------------------------
# Lognormal terminal probability (Black-Scholes style, r=0)
# ---------------------------------------------------------------------------

def _normal_cdf(x: float) -> float:
    """Standard-normal CDF via ``math.erf`` — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def lognormal_above(
    spot: float, strike: float, vol_annualized: float, days_to_expiry: float,
) -> float | None:
    """``P(S_T >= strike)`` under a lognormal terminal distribution.

    ``vol_annualized`` is annualized realized vol (e.g. 0.65 = 65%/yr).
    Returns None on inputs that don't make physical sense (non-positive
    spot/strike/vol or non-positive time).

    Formula (risk-neutral, ``r = q = 0``)::

        T   = days_to_expiry / 365
        d2  = (ln(spot/strike) - 0.5 σ² T) / (σ √T)
        P   = N(d2)

    >>> # Spot $100, strike $100, 50% vol, 1 year. NOT 50%: with r = q = 0
    >>> # the lognormal median sits below spot (−½σ²T drift), so
    >>> # P(above) = N(−0.25) ≈ 0.40.
    >>> round(lognormal_above(100, 100, 0.5, 365), 2)
    0.4
    >>> # Spot $100, strike $200, no time left → 0
    >>> lognormal_above(100, 200, 0.5, 0)
    >>> # Spot $200, strike $100, lots of time, low vol → ~100%
    >>> round(lognormal_above(200, 100, 0.1, 365), 2)
    1.0
    """
    if spot <= 0 or strike <= 0 or vol_annualized <= 0 or days_to_expiry <= 0:
        return None
    t_years = days_to_expiry / 365.0
    sigma_sqrt_t = vol_annualized * math.sqrt(t_years)
    d2 = (math.log(spot / strike) - 0.5 * vol_annualized * vol_annualized * t_years) / sigma_sqrt_t
    return _normal_cdf(d2)


def lognormal_below(
    spot: float, strike: float, vol_annualized: float, days_to_expiry: float,
) -> float | None:
    """``P(S_T <= strike)`` — complement of :func:`lognormal_above`."""
    p = lognormal_above(spot, strike, vol_annualized, days_to_expiry)
    if p is None:
        return None
    return 1.0 - p


# ---------------------------------------------------------------------------
# One-touch barrier probabilities (reflection-principle GBM)
# ---------------------------------------------------------------------------
#
# Most prediction-market crypto questions ("Will BTC reach $X by DATE?")
# settle on whether the spot price *ever touches* the threshold before
# the deadline — they are **barrier**, not **terminal**, questions. A
# terminal lognormal model systematically underprices them when the
# strike is out-of-the-money but the path can wander through it.
#
# For GBM ``dS/S = σ dW`` (risk-neutral, ``r=q=0``) the closed-form
# one-touch probabilities are derived via the reflection principle
# applied to Brownian motion with drift ``-σ/2`` on the log-price scale.

def barrier_hit_above(
    spot: float, strike: float, vol_annualized: float, days_to_expiry: float,
) -> float | None:
    """``P(max_{t∈[0,T]} S_t ≥ strike | S_0 = spot)`` for ``strike > spot``.

    If the spot is already at or above the strike, returns 1.0.
    Returns None on inputs that aren't physically meaningful.

    >>> # Already above — definitely hit
    >>> barrier_hit_above(110, 100, 0.5, 30)
    1.0
    >>> # Spot $100, strike $110, 50% vol, 1 year — comfortably possible
    >>> p = barrier_hit_above(100, 110, 0.5, 365)
    >>> 0.50 < p < 0.85
    True
    """
    if spot <= 0 or strike <= 0 or vol_annualized <= 0 or days_to_expiry <= 0:
        return None
    if spot >= strike:
        return 1.0
    t = days_to_expiry / 365.0
    sigma_sqrt_t = vol_annualized * math.sqrt(t)
    half_var_t = 0.5 * vol_annualized * vol_annualized * t
    log_ratio = math.log(spot / strike)        # negative when spot < strike
    d1 = (log_ratio - half_var_t) / sigma_sqrt_t
    d2 = (log_ratio + half_var_t) / sigma_sqrt_t
    return _normal_cdf(d1) + (spot / strike) * _normal_cdf(d2)


def barrier_hit_below(
    spot: float, strike: float, vol_annualized: float, days_to_expiry: float,
) -> float | None:
    """``P(min_{t∈[0,T]} S_t ≤ strike | S_0 = spot)`` for ``strike < spot``.

    If the spot is already at or below the strike, returns 1.0.
    Returns None on inputs that aren't physically meaningful.

    >>> # Already below — definitely hit
    >>> barrier_hit_below(90, 100, 0.5, 30)
    1.0
    >>> # Spot $100, strike $90, 50% vol, 1 year. Reflection principle under
    >>> # the risk-neutral (−½σ²) drift — the drift pulls the path DOWN, so
    >>> # a lower barrier is likelier than the symmetric intuition suggests.
    >>> round(barrier_hit_below(100, 90, 0.5, 365), 3)
    0.874
    """
    if spot <= 0 or strike <= 0 or vol_annualized <= 0 or days_to_expiry <= 0:
        return None
    if spot <= strike:
        return 1.0
    t = days_to_expiry / 365.0
    sigma_sqrt_t = vol_annualized * math.sqrt(t)
    half_var_t = 0.5 * vol_annualized * vol_annualized * t
    log_ratio = math.log(strike / spot)        # negative when strike < spot
    d_a = (log_ratio + half_var_t) / sigma_sqrt_t
    d_b = (log_ratio - half_var_t) / sigma_sqrt_t
    return _normal_cdf(d_a) + (spot / strike) * _normal_cdf(d_b)


__all__ = [
    "SYMBOLS",
    "get_spot_price",
    "get_klines",
    "spot_at",
    "realized_volatility",
    "realized_volatility_at",
    "lognormal_above",
    "lognormal_below",
    "barrier_hit_above",
    "barrier_hit_below",
]


if __name__ == "__main__":
    # Smoke when run directly.
    print("BTC spot:", get_spot_price("BTC"))
    df = get_klines("BTC", days=60)
    print(f"BTC klines: {len(df)} rows")
    if not df.empty:
        print("BTC 30-day annualized vol:", realized_volatility(df, window=30))
        print("BTC P(>= $200k in 180 days):",
              lognormal_above(float(df["close"].iloc[-1]), 200_000,
                              realized_volatility(df, window=30) or 0.6, 180))
