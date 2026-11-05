"""Kalshi trading fee calculation.

Per-trade fees on Kalshi are *non-linear* in price — highest near $0.50,
lowest near the boundaries ($0.05 and $0.95). A flat percentage of stake
doesn't approximate this well, so naïve backtests systematically over-
or under-estimate realized P&L depending on where their trades cluster.

Official formula (from kalshi.com/legal/trading-fees-and-rebates):

    fee_dollars = ceil(0.07 × P × (1 - P) × C × 100) / 100

Where:
- ``P`` is the trade price in dollars ([0.01, 0.99])
- ``C`` is the number of contracts traded
- ``0.07`` is the standard fee rate; some series get a reduced rate
  (Sports/MLB are 0.035 at time of writing — Kalshi adjusts these
  periodically, so the rate is configurable)
- Rounded UP to the nearest cent per trade

The fee is charged on both buy and sell legs of a round-trip, so total
fees per closed position are roughly ``2 × fee_for_trade(avg_price, C)``.

Public surface:

    fee_for_trade(price, contracts, *, fee_rate=0.07) -> float
        Dollar fee for one trade. Pure function.

    fee_pct_of_stake(price, *, fee_rate=0.07) -> float
        Equivalent percentage of stake — for use in backtest engines
        that consume a fee_pct parameter. ``fee / stake`` where
        ``stake = price × contracts``.

    fee_rate_for_series(series_ticker) -> float
        Lookup: returns 0.035 for sports series, 0.07 otherwise. The
        list of reduced-fee series is small and currently hardcoded;
        update SPORTS_PREFIXES when Kalshi expands the program.

    round_trip_fees(entry_price, exit_price, contracts, *, fee_rate=0.07)
        Convenience: returns total fees on entry + exit. Used by the
        agentic runner's exit logger.
"""

from __future__ import annotations

import math

# Kalshi's reduced-fee series. As of 2026-05, sports/baseball markets
# carry a 3.5% fee rate vs the standard 7%. This list is conservative —
# when in doubt we use the standard rate, which over-estimates fees and
# is the safer direction (we report SLIGHTLY worse-than-real P&L
# rather than rosier-than-real).
SPORTS_PREFIXES: tuple[str, ...] = (
    "KXMLB", "KXNBA", "KXNFL", "KXNHL", "KXWNBA", "KXMLS",
    "KXMVESPORTS",
)

STANDARD_FEE_RATE = 0.07
SPORTS_FEE_RATE = 0.035


def fee_rate_for_series(ticker_or_series: str) -> float:
    """Return the per-trade fee rate for a Kalshi ticker or series.

    >>> fee_rate_for_series("KXMLBGAME-26MAY222140COLAZ-AZ")
    0.035
    >>> fee_rate_for_series("KXNEWPOPE-35-PPAR")
    0.07
    >>> fee_rate_for_series("")
    0.07
    """
    if not ticker_or_series:
        return STANDARD_FEE_RATE
    upper = ticker_or_series.upper()
    for prefix in SPORTS_PREFIXES:
        if upper.startswith(prefix):
            return SPORTS_FEE_RATE
    return STANDARD_FEE_RATE


def fee_for_trade(
    price: float, contracts: float, *,
    fee_rate: float = STANDARD_FEE_RATE,
) -> float:
    """Compute the dollar fee for one Kalshi trade.

    Per Kalshi's published formula: ``ceil(rate × P × (1-P) × C × 100) / 100``.
    The rounding-up-to-the-nearest-cent matters at small contract counts
    but is negligible at scale.

    >>> # 100 contracts at $0.50: max fee ≈ 7%·0.25·100 = $1.75
    >>> round(fee_for_trade(0.50, 100), 4)
    1.75
    >>> # Same contracts at $0.10: fee ≈ 7%·0.09·100 = $0.63
    >>> round(fee_for_trade(0.10, 100), 4)
    0.63
    >>> # Same at $0.05: fee ≈ 7%·0.0475·100 = $0.34 → round up to 0.34
    >>> round(fee_for_trade(0.05, 100), 4)
    0.34
    >>> # Boundary cases — no contracts, no fee
    >>> fee_for_trade(0.5, 0)
    0.0
    >>> # Out-of-range prices clamped to [0, 1] before fee calc
    >>> fee_for_trade(1.5, 100) == fee_for_trade(1.0, 100)
    True
    """
    if contracts <= 0:
        return 0.0
    p = max(0.0, min(1.0, float(price)))
    raw_cents = fee_rate * p * (1.0 - p) * float(contracts) * 100.0
    # Float-rep gotcha: 0.07 * 0.5 * 0.5 * 100 * 100 lands at
    # 175.00000000000003, which math.ceil rounds up to 176. Snap to
    # 9 decimal places first — that's far more precision than fees
    # ever need and immune to the canonical floats-aren't-exact issue.
    return math.ceil(round(raw_cents, 9)) / 100.0


def fee_pct_of_stake(
    price: float, *,
    fee_rate: float = STANDARD_FEE_RATE,
) -> float:
    """Equivalent percentage of stake for a trade at ``price``.

    Useful when a backtest engine consumes a flat ``fee_pct`` parameter
    — convert your *typical* trade price to its Kalshi-equivalent fee
    percentage. NOT a substitute for per-trade fee calc when price
    varies widely across the trade set.

    Stake = price × contracts, so:
        fee_pct = fee / stake = rate × (1 - p) × contracts / contracts
                              = rate × (1 - p)

    Note: this drops the ceiling rounding (a fractional-cent issue
    that only matters on tiny trades), so it's a good approximation
    for backtesting at $100+ stakes.

    >>> # At $0.50, fee_pct = 7% × 0.50 = 3.5% of stake
    >>> round(fee_pct_of_stake(0.50), 4)
    0.035
    >>> # At $0.10, fee_pct = 7% × 0.90 = 6.3% of stake (HIGHER as a
    >>> # percentage — long-shot stakes are smaller per contract)
    >>> round(fee_pct_of_stake(0.10), 4)
    0.063
    """
    p = max(0.0, min(1.0, float(price)))
    return fee_rate * (1.0 - p)


def round_trip_fees(
    entry_price: float, exit_price: float, contracts: float, *,
    ticker: str = "", fee_rate: float | None = None,
) -> float:
    """Total fees on a complete round trip (buy + sell).

    Used by the agentic runner's exit logger: takes the entry fill
    price + the exit fill price + position size and returns the fee
    we should deduct from realized P&L.

    ``ticker`` is used to look up the series-specific fee rate when
    ``fee_rate`` is None. Pass the rate directly to override (useful
    for tests).
    """
    rate = fee_rate if fee_rate is not None else fee_rate_for_series(ticker)
    return (
        fee_for_trade(entry_price, contracts, fee_rate=rate)
        + fee_for_trade(exit_price, contracts, fee_rate=rate)
    )
