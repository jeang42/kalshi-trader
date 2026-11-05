"""Pre-event lottery scanner — cheap markets resolving soon.

The thesis (un-tested as of v1):

Markets priced under ``$0.10`` with a known catalyst landing inside the
next ~24h carry asymmetric payoff. A few will be "stuck" near a stale
quote because nobody is paying attention to the long-shot leg; one of
those mispriced ones occasionally resolves YES and pays $1, returning
20× on the entry. The strategy is a portfolio of small bets across
many such markets — accept a high per-trade loss rate in exchange for
the convexity of the few wins.

**The strategy is NEGATIVE-EV per trade IF markets are correctly
calibrated.** A market at $0.05 implies 5% resolution probability; you
pay $0.05 to win $1.00 with 5% chance → expected value = $0.05 − $0.05
= $0.00 pre-fee, and Kalshi entry fees at $0.05 are ~$0.0033 per
contract ≈ 6.7% of stake. So the basket strategy only makes money if
markets *systematically over-discount tails* (i.e., true probability >
implied probability for cheap, near-resolution markets). That's an
empirical question — this scanner + strategy is the apparatus to test
it.

If you want a non-test version: pair this scanner with the existing
edge-finding strategies (``crypto_spot_lognormal``, ``baseball_winprob``,
``llm_forecast``) by passing their model probabilities as the "claimed
edge" instead of the flat-0.02 default. Then the strategy is "bet on
sub-$0.10 markets where MY model says higher" — a price-filtered
edge-finder, which is much more defensible.

Public surface:

    scan_lottery_candidates(provider, *, max_price=0.10, max_days=1.5,
                             categories=None, min_volume=100.0,
                             max_markets=500) -> pd.DataFrame
        Active markets meeting the lottery filter. Sorted ascending by
        price (cheapest first). Empty when nothing qualifies.

    strategy_pre_event_lottery(snap, *, max_price=0.10, max_days=1.5,
                                edge=0.02, categories=None) -> Signal | None
        Backtest-shape strategy. Bets YES at probability
        ``current_price + edge`` when the snapshot's market qualifies.
        Registered in ``backtest.STRATEGIES`` as ``pre_event_lottery``.

CLI: ``python -m pre_event_scanner --max-price 0.10 --max-days 1.5``.

Cost: one ``get_markets_closing_within`` call (Kalshi has native time
filtering, so this is cheap — one cursor-paginated request). No
per-market history fetch.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable
from typing import Any

import pandas as pd

from pm.core import logctx

log = logging.getLogger(__name__)


# Categories where the lottery thesis is most plausible: known catalyst
# (game ends, polls close, expiry happens) within hours. Includes
# Entertainment because tracker markets (album sales thresholds, box-
# office numbers, etc.) live there and have natural lottery shape —
# many sub-$0.10 legs on rising thresholds, each with a hard deadline.
# Excluded by default: "Other" — that bucket is dominated by Kalshi's
# auto-generated KXMVE* parlay synthetics that have no real liquidity.
# Pass --categories explicitly to include "Other" when needed.
DEFAULT_CATEGORIES = (
    "Sports", "Politics", "Economics", "Crypto", "Climate",
    "Entertainment",
)


def scan_lottery_candidates(
    provider: Any,
    *,
    max_price: float = 0.10,
    max_days: float = 1.5,
    categories: Iterable[str] | None = None,
    min_volume: float = 100.0,
    max_markets: int = 500,
) -> pd.DataFrame:
    """Return active markets that pass the pre-event lottery filter.

    Filter (all must hold):
        - ``best_ask`` (or ``outcome_prices[0]`` fallback) ≤ ``max_price``
        - ``days_to_resolution`` ≤ ``max_days``
        - ``category`` ∈ ``categories`` (or all when None)
        - ``volume`` ≥ ``min_volume`` — avoid completely dead markets
          where the price is just stale, not a real opportunity

    Sorted by ``best_ask`` ascending (cheapest entry first).
    """
    if categories is None:
        categories = DEFAULT_CATEGORIES
    cats = {c.strip() for c in categories if c}

    try:
        df = provider.get_markets_closing_within(
            max_days=max_days, limit=max_markets,
        )
    except Exception as e:
        log.warning("get_markets_closing_within failed: %s", e)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()

    # The price column we want is the buyer's-side ASK (what we'd pay
    # to enter YES right now). When best_ask is missing, fall back to
    # outcome_prices[0] (the YES mid from get_markets); that's a less
    # honest estimate of fill cost but better than nothing.
    def _entry_price(row: pd.Series) -> float | None:
        ba = row.get("best_ask")
        if ba is not None and pd.notna(ba):
            try:
                return float(ba)
            except (TypeError, ValueError):
                pass
        op = row.get("outcome_prices") or []
        if isinstance(op, list) and op and op[0] is not None:
            try:
                return float(op[0])
            except (TypeError, ValueError):
                return None
        return None

    df = df.copy()
    df["entry_price"] = df.apply(_entry_price, axis=1)
    filtered = df[
        df["entry_price"].notna()
        & (df["entry_price"] <= max_price)
        & (df["entry_price"] > 0)
        & df["days_to_resolution"].notna()
        & (df["days_to_resolution"] <= max_days)
        & (df["days_to_resolution"] >= 0)
        & df["category"].isin(cats)
        & (df["volume"].fillna(0) >= min_volume)
    ].sort_values("entry_price").reset_index(drop=True)

    return filtered


def strategy_pre_event_lottery(
    snap: Any,  # backtest.MarketSnapshot at runtime
    *,
    max_price: float = 0.10,
    max_days: float = 1.5,
    edge: float = 0.02,
    categories: Iterable[str] | None = None,
) -> Any:  # backtest.Signal | None
    """Backtest-shape strategy: bet YES in markets that pass the
    lottery filter.

    Filter (must all pass at snap.as_of):
        - latest YES price ≤ max_price (we're buying the cheap leg)
        - time-to-end_date ≤ max_days (catalyst is imminent)
        - category in ``categories`` (skip slow-resolution markets)

    Signal: ``YES at probability = current_price + edge``. ``edge`` is
    the claimed Kelly edge — a small bias toward higher-than-market
    probability. **The default 0.02 is intentionally small** because
    the strategy is a basket test, not a conviction bet. If you want
    to test "are markets under-pricing tails by 2pp?", run with the
    default. Larger edges should be paired with an actual model
    (baseball, LLM, etc.).

    Returns None if the market doesn't qualify (most markets won't).
    """
    if snap is None or snap.prices is None or snap.prices.empty:
        return None
    from pm.research import backtest as bt

    current = snap.latest_price
    if current is None or current <= 0 or current > max_price:
        return None

    # Category gate — skip unless the snap's category is in the
    # allowed set. Backtest snapshots carry the category from the
    # original market metadata.
    cats = set(categories) if categories is not None else set(DEFAULT_CATEGORIES)
    snap_cat = (snap.category or "").strip()
    if snap_cat and snap_cat not in cats:
        return None

    # Time gate — must resolve within max_days of as_of.
    if snap.end_date is None:
        return None
    days_remaining = (snap.end_date - snap.as_of).total_seconds() / 86400.0
    if days_remaining < 0 or days_remaining > max_days:
        return None

    # Probability anchor: current + edge, capped to a sane range so
    # Kelly doesn't go bananas on rounding artifacts.
    prob = max(0.01, min(0.99, current + edge))
    return bt.Signal(direction="YES", probability=prob)


# Register with the backtest strategy registry.
try:
    from pm.research import backtest as _bt
    if "pre_event_lottery" not in _bt.STRATEGIES:
        _bt.STRATEGIES["pre_event_lottery"] = (
            "Pre-event lottery (sub-$0.10, imminent catalyst — UNTESTED)",
            strategy_pre_event_lottery,
        )
except ImportError:  # pragma: no cover
    pass


def concentration_summary(df: pd.DataFrame) -> dict:
    """Diagnostics for an event-level concentration warning.

    The lottery thesis assumes the basket is **diversified across
    independent catalysts**. If 90% of qualifying markets are sub-
    legs of one event (e.g., 20 Drake-album threshold tickers from
    one release), the "basket" is really one bet wearing many hats.

    Returns a dict with:
        n_markets         total in the filter
        top_series        most-concentrated series ticker prefix
        top_series_share  fraction of the basket that's in top_series
        n_series          distinct series prefixes
    """
    if df.empty:
        return {"n_markets": 0, "top_series": "", "top_series_share": 0.0,
                "n_series": 0}
    # Series prefix = ticker up to second hyphen (Kalshi pattern:
    # SERIES-EVENT-MARKET). When ambiguous, fall back to the first
    # hyphen.
    def _series(ticker: str) -> str:
        parts = ticker.split("-")
        if len(parts) >= 2:
            return parts[0]
        return ticker

    series = df["condition_id"].astype(str).apply(_series)
    counts = series.value_counts()
    top = counts.index[0]
    return {
        "n_markets": int(len(df)),
        "top_series": str(top),
        "top_series_share": float(counts.iloc[0]) / float(len(df)),
        "n_series": int(counts.size),
    }


def _format_table(df: pd.DataFrame, *, top: int = 30) -> str:
    if df.empty:
        return "(no lottery candidates matched the filter)"
    cols = [
        "entry_price", "days_to_resolution", "volume", "category",
        "condition_id", "question",
    ]
    view = df.head(top)[cols].copy()
    view["question"] = view["question"].astype(str).str.slice(0, 70)
    view["condition_id"] = view["condition_id"].astype(str).str.slice(0, 25)
    return view.to_string(index=True, float_format=lambda v: f"{v:.4f}")


def main(argv: list[str] | None = None) -> int:
    """CLI: list today's pre-event lottery candidates from the active provider."""
    p = argparse.ArgumentParser(
        description="Scan for cheap markets with an imminent catalyst. "
                    "Sub-$0.10 by default, resolving within 1.5 days, "
                    "in catalyst-driven categories.",
    )
    p.add_argument("--max-price", type=float, default=0.10,
                   help="Maximum YES entry price (default $0.10).")
    p.add_argument("--max-days", type=float, default=1.5,
                   help="Maximum days to resolution (default 1.5).")
    p.add_argument("--min-volume", type=float, default=100.0,
                   help="Minimum lifetime volume (default 100). Filters "
                        "out completely dead long-tail markets.")
    p.add_argument("--max-markets", type=int, default=500,
                   help="Cap on markets pulled before filtering.")
    p.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES),
                   help="Comma-separated category whitelist. Default "
                        f"= {','.join(DEFAULT_CATEGORIES)}.")
    p.add_argument("--top", type=int, default=30,
                   help="How many candidates to display (cheapest first).")
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup(args.log_level.upper())

    from pm.venue import provider as provider_mod
    provider = provider_mod.get_provider()
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    df = scan_lottery_candidates(
        provider, max_price=args.max_price, max_days=args.max_days,
        categories=categories, min_volume=args.min_volume,
        max_markets=args.max_markets,
    )
    print(f"Lottery candidates ({len(df)} matched filter):")
    print(_format_table(df, top=args.top))
    if not df.empty:
        conc = concentration_summary(df)
        print()
        print(f"Concentration: {conc['n_markets']} markets across "
              f"{conc['n_series']} series. "
              f"Top series '{conc['top_series']}' = "
              f"{conc['top_series_share']:.0%} of the basket.")
        if conc["top_series_share"] > 0.5:
            print("⚠️  >50% of the basket is one series — the lottery "
                  "thesis assumes independent catalysts; basket bets "
                  "here mostly resolve the SAME way.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
