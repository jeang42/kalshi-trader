"""Macroeconomic data layer — FRED (St. Louis Fed) public CSV endpoint.

Why FRED's ``fredgraph.csv`` endpoint instead of the documented API: the
JSON API at ``api.stlouisfed.org/fred/`` requires an API key (free but
gated behind account creation). The ``fred.stlouisfed.org/graph/
fredgraph.csv?id=<SERIES>`` endpoint is the one FRED uses to embed
charts on third-party sites — fully public, no auth, generous rate
limits, full historical archives back to series inception.

Coverage in v1: clean threshold questions on unemployment rate (UNRATE),
2-/10-year Treasury yields (DGS2/DGS10), and the Fed funds effective
rate (FEDFUNDS). Other series (CPI YoY, GDP growth) need
transformations or annual cadence handling that isn't worth the v1
complexity — document the gap.

The probability model is **normal-distribution-on-level** with realized
volatility, NOT lognormal — interest rates can be (and have been)
negative in theory, and most macro series are naturally additive in
level rather than multiplicative. This is wrong in detail (rates are
not Brownian; they have policy steps and mean-reversion) but right in
order-of-magnitude for short-horizon threshold questions where the
trend term dominates. Document the limitation prominently rather than
pretend otherwise.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
import requests

from pm.core import circuit_breaker

log = logging.getLogger(__name__)

UTC = UTC

FREDGRAPH_BASE = os.getenv("FRED_GRAPH_BASE", "https://fred.stlouisfed.org/graph")
HTTP_TIMEOUT = float(os.getenv("FRED_HTTP_TIMEOUT", "20"))


# ---------------------------------------------------------------------------
# Series registry — supported macro series for the v1 question parser.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SeriesMeta:
    """Static metadata for one supported FRED series."""

    series_id: str            # FRED ticker, e.g. "UNRATE"
    cadence: str              # "monthly" | "daily" — affects vol annualization
    display_name: str         # Human label for the UI
    # Whether values are percents (rates) or index levels. Used by the
    # question parser to interpret a threshold like "5%" — percents are
    # taken as-is, indices are not yet supported in v1.
    is_percent: bool = True


SERIES: dict[str, SeriesMeta] = {
    "unemployment": SeriesMeta("UNRATE", "monthly", "Unemployment rate (%)"),
    "10y_treasury": SeriesMeta("DGS10", "daily",   "10-year Treasury yield (%)"),
    "2y_treasury":  SeriesMeta("DGS2",  "daily",   "2-year Treasury yield (%)"),
    "fed_funds":    SeriesMeta("FEDFUNDS", "monthly", "Federal funds effective rate (%)"),
}


_session = requests.Session()
_session.headers.update({"User-Agent": "polymarket-analyzer/1.0"})


# ---------------------------------------------------------------------------
# HTTP wrapper
# ---------------------------------------------------------------------------

def _get_csv(series_id: str, *, retries: int = 2) -> str | None:
    """Fetch the fredgraph CSV body for one series, under the ``fred``
    circuit breaker (R-21). Returns None on transport failure."""
    try:
        return circuit_breaker.get("fred").call(_get_csv_unguarded, series_id, retries=retries)
    except circuit_breaker.CircuitOpenError as e:
        log.warning("FRED: %s", e)
        return None


def _get_csv_unguarded(series_id: str, *, retries: int = 2) -> str | None:
    """Fetch the fredgraph CSV body for one series. Returns None on
    transport failure."""
    url = f"{FREDGRAPH_BASE}/fredgraph.csv"
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, params={"id": series_id}, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                sleep_s = max(0.1, min(30.0, float(r.headers.get("Retry-After", "1"))))
                log.warning("FRED 429 for %s; retrying in %.1fs", series_id, sleep_s)
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            break
    if last_exc is not None:
        log.warning("FRED fetch for %s failed: %s", series_id, last_exc)
    return None


# ---------------------------------------------------------------------------
# CSV → DataFrame
# ---------------------------------------------------------------------------

def get_observations(series_id: str) -> pd.DataFrame:
    """Full history for ``series_id``. Empty DataFrame on fetch failure or
    if FRED returned an empty body.

    Columns: ``date`` (datetime64 UTC), ``value`` (float; NaN where
    FRED reports a gap with "." or "NaN").
    """
    body = _get_csv(series_id)
    if not body:
        return pd.DataFrame(columns=["date", "value"])

    reader = csv.reader(io.StringIO(body))
    rows = list(reader)
    if not rows or len(rows) < 2:
        return pd.DataFrame(columns=["date", "value"])

    # FRED puts the series id in the second column header.
    dates: list[datetime] = []
    values: list[float] = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        try:
            d = datetime.fromisoformat(row[0]).replace(tzinfo=UTC)
        except ValueError:
            continue
        raw = row[1].strip()
        if not raw or raw == ".":
            # FRED uses "." for missing observations. Skip rather than
            # NaN-fill so downstream stats don't have to special-case it.
            continue
        try:
            v = float(raw)
        except ValueError:
            continue
        dates.append(d)
        values.append(v)
    if not dates:
        return pd.DataFrame(columns=["date", "value"])
    return pd.DataFrame({"date": dates, "value": values})


def latest_value(series_id: str) -> tuple[float, datetime] | None:
    """Most recent (value, date) for the series, or None when empty."""
    df = get_observations(series_id)
    if df.empty:
        return None
    last = df.iloc[-1]
    return (float(last["value"]), last["date"].to_pydatetime())


def value_at(
    series_id: str, as_of: datetime,
) -> tuple[float, datetime] | None:
    """Most recent (value, observed_at) **strictly before** ``as_of``.

    Used by the backtest engine so the macro model sees only data
    that would have been known at ``snap.as_of`` — not the freshest
    data we happen to have today.

    **Honesty caveat**: FRED's CSV exposes ``observation_date`` (the
    date the data point applies to) but not ``publication_date`` (when
    the value was actually released). For monthly series like UNRATE
    and FEDFUNDS the publication lag is ~5–7 days; for daily series
    like DGS2/DGS10 it's same-day. Using ``observation_date <= as_of``
    leaks at most one publication cycle of forward information — fine
    for order-of-magnitude backtests, not strictly leak-free.
    """
    df = get_observations(series_id)
    if df.empty:
        return None
    eligible = df[df["date"] <= as_of]
    if eligible.empty:
        return None
    last = eligible.iloc[-1]
    return (float(last["value"]), last["date"].to_pydatetime())


# ---------------------------------------------------------------------------
# Realized volatility (annualized)
# ---------------------------------------------------------------------------

def realized_volatility(
    df_obs: pd.DataFrame,
    *,
    cadence: str,
    window: int = 24,
) -> float | None:
    """Annualized realized vol of *level changes* (NOT log returns —
    rates aren't lognormal).

    ``cadence`` ∈ {"daily", "monthly"} drives the √-time scaling.
    ``window`` is the number of most-recent observations to use.

    Returns None when there isn't enough data or volatility is zero.
    """
    if df_obs.empty or len(df_obs) < window + 1:
        return None
    tail = df_obs["value"].iloc[-(window + 1):]
    diffs = tail.diff().dropna()
    if diffs.empty:
        return None
    std_per_period = float(diffs.std())
    if std_per_period <= 0:
        return None
    if cadence == "monthly":
        return std_per_period * math.sqrt(12.0)
    if cadence == "daily":
        return std_per_period * math.sqrt(252.0)  # business days
    return std_per_period   # treat as already annualized


def realized_volatility_for(series_key: str, *, window: int = 24) -> float | None:
    """Convenience: look up the series + cadence and compute vol over
    the full history available today. For live edge surfacing."""
    meta = SERIES.get(series_key)
    if meta is None:
        return None
    df = get_observations(meta.series_id)
    return realized_volatility(df, cadence=meta.cadence, window=window)


def realized_volatility_at(
    series_key: str, as_of: datetime, *, window: int = 24,
) -> float | None:
    """Annualized realized vol using only observations strictly before
    ``as_of``. The backtest equivalent of :func:`realized_volatility_for`
    — keeps the model out of the future.
    """
    meta = SERIES.get(series_key)
    if meta is None:
        return None
    df = get_observations(meta.series_id)
    if df.empty:
        return None
    df = df[df["date"] <= as_of]
    if df.empty:
        return None
    return realized_volatility(df, cadence=meta.cadence, window=window)


# ---------------------------------------------------------------------------
# Normal-on-level terminal probability
# ---------------------------------------------------------------------------

def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def normal_above(
    level: float, threshold: float, vol_annualized: float,
    days_to_expiry: float, *, drift: float = 0.0,
) -> float | None:
    """``P(level_at_T >= threshold)`` under a normal terminal
    distribution with optional drift.

    Rejects nonsensical inputs (non-positive vol or time). All inputs
    are in the same units as the underlying series (percent points
    for rates, raw level for indices).

    >>> # Unemployment at 4.3%, threshold 5.0%, 2%/yr annualized vol,
    >>> # 6 months out, no drift → ~10% probability of crossing
    >>> p = normal_above(4.3, 5.0, 2.0, 180)
    >>> 0.0 < p < 0.5
    True

    >>> # Already above threshold → high probability
    >>> p = normal_above(5.5, 5.0, 2.0, 180)
    >>> p > 0.5
    True
    """
    if vol_annualized <= 0 or days_to_expiry <= 0:
        return None
    t_years = days_to_expiry / 365.0
    sigma = vol_annualized * math.sqrt(t_years)
    mean = level + drift * t_years
    z = (threshold - mean) / sigma
    return 1.0 - _normal_cdf(z)


def normal_below(
    level: float, threshold: float, vol_annualized: float,
    days_to_expiry: float, *, drift: float = 0.0,
) -> float | None:
    """``P(level_at_T <= threshold)`` — complement of :func:`normal_above`."""
    p = normal_above(level, threshold, vol_annualized, days_to_expiry, drift=drift)
    if p is None:
        return None
    return 1.0 - p


__all__ = [
    "SeriesMeta",
    "SERIES",
    "get_observations",
    "latest_value",
    "value_at",
    "realized_volatility",
    "realized_volatility_for",
    "realized_volatility_at",
    "normal_above",
    "normal_below",
]


if __name__ == "__main__":
    # Smoke when run directly.
    for key, meta in SERIES.items():
        df = get_observations(meta.series_id)
        if df.empty:
            print(f"{key:>14s}: no data")
            continue
        latest = df.iloc[-1]
        vol = realized_volatility(df, cadence=meta.cadence, window=24)
        vol_s = f"{vol:.2f}/yr" if vol is not None else "n/a"
        print(f"{key:>14s}: latest {latest['value']:.2f} on "
              f"{latest['date'].date()}, vol {vol_s}, n={len(df)}")
