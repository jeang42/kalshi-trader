"""Backtest framework — Tier 3b of the recommendations roadmap.

A strategy is a function ``MarketSnapshot -> Signal | None``. The engine
applies it to historical resolved markets and reports PnL / hit rate /
Sharpe / Brier / drawdown / per-category breakdown. **This is the gate**:
no alpha strategy ships without a measured backtest that beats the
market on its category.

Single-shot evaluation model (v1):
- For each closed market we pick one evaluation timestamp at
  ``as_of = closed_time - lead_hours``.
- Strategy sees the price history *strictly before* ``as_of`` — the
  ``Resolution`` object is held by the engine, not the snapshot, so
  strategies cannot peek at the outcome.
- One trade per market. Hold to resolution. PnL is computed from the
  entry price and the binary outcome using the same accounting as a real
  Polymarket / Kalshi YES/NO share.

Walk-forward (multi-trade, mark-to-market) is intentionally deferred —
the single-shot model is enough to compare strategies and gates the
LLM/external-data work that comes next.

Why the dataset / engine split:
- ``build_dataset`` does the slow part — one HTTP round-trip per market
  to fetch its price history. Cache it.
- ``run_backtest`` is pure-Python over the cached dataset, so users can
  iterate on strategy parameters without re-fetching.
"""

from __future__ import annotations

import importlib
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from pm import REPO_ROOT

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketSnapshot:
    """Everything a strategy is allowed to see at ``as_of``.

    Crucially: ``prices`` is truncated to rows strictly before ``as_of``.
    The resolution (outcome + close time) is NOT exposed — that lives in
    the parallel ``Resolution`` the engine holds.
    """

    condition_id: str
    question: str
    category: str
    as_of: datetime
    prices: pd.DataFrame  # cols: timestamp (datetime64[ns, UTC]), price (float)
    end_date: datetime | None = None  # market's stated deadline — public at `as_of`

    @property
    def latest_price(self) -> float | None:
        """Most recent YES price strictly before ``as_of``, or None if the
        snapshot has no usable history."""
        if self.prices.empty:
            return None
        return float(self.prices["price"].iloc[-1])

    def price_at_lookback(self, hours: float) -> float | None:
        """YES price ``hours`` before ``as_of`` (nearest sample at-or-before
        that point). Returns None if the history doesn't reach back far
        enough."""
        if self.prices.empty:
            return None
        cutoff = self.as_of - timedelta(hours=hours)
        eligible = self.prices[self.prices["timestamp"] <= cutoff]
        if eligible.empty:
            return None
        return float(eligible["price"].iloc[-1])


@dataclass(frozen=True)
class Resolution:
    """Ground truth for one market. Held by the engine; strategies never
    see this."""

    condition_id: str
    resolved_yes: bool
    closed_time: datetime


@dataclass(frozen=True)
class Signal:
    """What a strategy returns when it wants to bet on a market.

    ``probability`` is the strategy's YES-probability estimate, used both
    for Kelly sizing and for the Brier score. ``direction`` is which side
    of the binary to take.

    ``fraction_override`` is the escape hatch for flat-sized baselines
    ("always bet 1% on YES") — when set, the engine skips Kelly entirely
    and uses this fraction-of-bankroll directly. Leave ``None`` for the
    default Kelly path.
    """

    direction: str  # "YES" or "NO"
    probability: float
    fraction_override: float | None = None


@dataclass
class BacktestResult:
    """Aggregated statistics from one backtest run."""

    strategy_name: str
    n_markets_evaluated: int
    n_trades: int
    n_wins: int
    hit_rate: float | None
    hit_rate_ci: tuple[float, float] | None  # 95% Wilson CI
    total_stake: float
    total_pnl: float
    roi: float | None
    mean_pnl_per_trade: float | None
    pnl_std: float | None
    sharpe_per_trade: float | None
    max_drawdown: float
    brier_score: float | None
    # Transaction-cost accounting. half_spread is in absolute price units
    # per side; fee_pct is fraction-of-stake. total_costs sums all fees
    # paid (spread cost is already absorbed into trade PnL via the
    # effective entry price, so it's reported separately as
    # implied_spread_cost for visibility).
    half_spread: float
    fee_pct: float
    total_fees: float
    implied_spread_cost: float
    # Portfolio-level bankroll accounting. When `portfolio_constrained`
    # is True, the engine tracks available cash through the run and
    # caps each trade's stake at min(desired, cash). `min_cash_seen` is
    # the trough of the cash trajectory ($0 = ran out completely at some
    # point); `n_clipped_for_cash` counts trades whose actual stake was
    # smaller than Kelly desired; `n_skipped_for_cash` counts trades
    # that wanted to fire but got 0 stake because cash was exhausted.
    portfolio_constrained: bool
    min_cash_seen: float
    n_clipped_for_cash: int
    n_skipped_for_cash: int
    trades: pd.DataFrame
    equity_curve: pd.DataFrame   # cols: closed_time, cumulative_pnl
    by_category: dict[str, dict[str, Any]]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Dataset construction
# ---------------------------------------------------------------------------

def _truncate_prices_before(df_prices: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """Return only the rows strictly before ``as_of``.

    Strictly-before (not ``<=``) so that a strategy evaluating at exactly
    the candle boundary doesn't get to see that candle's data — that
    would be lookahead bias on a price the strategy "could have known"
    only by being lucky with timing.
    """
    if df_prices.empty:
        return df_prices
    return df_prices[df_prices["timestamp"] < as_of].reset_index(drop=True)


def _fetch_one_history(
    row, provider: Any, lead_hours: float,
    history_days: int, fidelity: int,
) -> tuple[MarketSnapshot, Resolution] | None:
    """Worker for build_dataset's parallel path. Pure I/O — must be
    thread-safe (the provider methods all are, since they just hit
    HTTP endpoints with no shared mutable state)."""
    cid = row["condition_id"]
    closed_at = row["closed_time_dt"]
    if pd.isna(closed_at):
        return None
    as_of = closed_at - timedelta(hours=lead_hours)
    try:
        tokens = provider.list_outcome_tokens(cid)
    except Exception as e:
        log.warning("list_outcome_tokens(%s) failed: %s", cid, e)
        return None
    if not tokens:
        return None
    yes_token = tokens[0]["token_id"]
    try:
        df_hist = provider.get_price_history(
            yes_token, days=history_days, fidelity=fidelity,
        )
    except Exception as e:
        log.warning("get_price_history(%s) failed: %s", yes_token, e)
        return None
    if df_hist.empty:
        return None
    df_hist = df_hist.sort_values("timestamp").reset_index(drop=True)
    df_pre = _truncate_prices_before(df_hist, as_of)
    if df_pre.empty:
        return None
    snapshot = MarketSnapshot(
        condition_id=str(cid),
        question=str(row.get("question", "")),
        category=str(row.get("category", "") or ""),
        as_of=as_of,
        prices=df_pre,
        end_date=closed_at,
    )
    resolution = Resolution(
        condition_id=str(cid),
        resolved_yes=bool(row["resolved_yes"]),
        closed_time=closed_at,
    )
    return (snapshot, resolution)


def build_dataset(
    provider: Any,
    df_closed: pd.DataFrame,
    *,
    lead_hours: float = 24,
    history_days: int = 90,
    fidelity: int = 60,
    max_markets: int | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    max_workers: int = 1,
) -> list[tuple[MarketSnapshot, Resolution]]:
    """Build the (snapshot, resolution) pairs used by ``run_backtest``.

    Slow — one HTTP call per market to fetch its price history. Cache
    the result. Decoupled from strategy execution so users can iterate
    on strategy parameters without re-fetching.

    Markets without a clean resolution or without enough history are
    silently skipped — sample size is reported via the ``BacktestResult``.

    The provider is passed in (not looked up via ``get_provider()``) so
    the engine is testable with mocks.

    ``max_workers``: when > 1, fetches are dispatched via a thread pool.
    Big speedup on large historical datasets (e.g. 8 workers turns a
    20-min fetch into ~3 min). Default 1 preserves sequential behavior
    for tests and small jobs. Be considerate of rate limits — Kalshi's
    ``_get`` already handles 429s, but spamming with 32 workers will
    just produce more retries.
    """
    if df_closed is None or df_closed.empty:
        return []

    df = df_closed[df_closed["resolved_yes"].notna()]
    if max_markets is not None:
        df = df.head(max_markets)
    if df.empty:
        return []

    rows = [row for _, row in df.iterrows()]
    total = len(rows)
    pairs: list[tuple[MarketSnapshot, Resolution]] = []

    if max_workers <= 1:
        # Sequential path — preserves the legacy order and is what tests
        # exercise. Progress callback fires per-row before processing.
        for i, row in enumerate(rows):
            if progress_callback is not None:
                try:
                    progress_callback(i, total)
                except Exception:
                    pass
            pair = _fetch_one_history(
                row, provider, lead_hours, history_days, fidelity,
            )
            if pair is not None:
                pairs.append(pair)
    else:
        # Parallel path via threads. ThreadPoolExecutor is the right
        # choice because the work is I/O-bound; the GIL is released
        # during requests.get inside _get. Order of results is not
        # preserved — that's fine since run_backtest re-sorts by
        # as_of when portfolio_constrained=True.
        import concurrent.futures as cf
        done_count = 0
        with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(
                    _fetch_one_history, row, provider,
                    lead_hours, history_days, fidelity,
                ): row
                for row in rows
            }
            for fut in cf.as_completed(futures):
                done_count += 1
                if progress_callback is not None:
                    try:
                        progress_callback(done_count, total)
                    except Exception:
                        pass
                try:
                    pair = fut.result()
                except Exception as e:
                    log.warning("worker raised: %s", e)
                    continue
                if pair is not None:
                    pairs.append(pair)

    if progress_callback is not None:
        try:
            progress_callback(total, total)
        except Exception:
            pass

    return pairs


# ---------------------------------------------------------------------------
# Strategy library
# ---------------------------------------------------------------------------

Strategy = Callable[[MarketSnapshot], "Signal | None"]


def strategy_market_consensus(snap: MarketSnapshot) -> Signal | None:
    """Trust the market — pick whichever side has the higher implied
    probability, with a small assumed edge so Kelly produces a non-zero
    stake. Essentially a "follow the crowd" baseline.

    On a well-calibrated market this should produce a hit rate slightly
    above 50% (because we're always taking the favorite) but near-zero
    edge — by construction. If a strategy can't beat this, it has no
    edge worth shipping.
    """
    p = snap.latest_price
    if p is None:
        return None
    edge = 0.02
    if p >= 0.5:
        return Signal(direction="YES", probability=min(1.0, p + edge))
    return Signal(direction="NO", probability=max(0.0, p - edge))


def strategy_always_yes_flat(snap: MarketSnapshot, *, fraction: float = 0.01) -> Signal | None:
    """Dumb baseline: always bet YES, fixed fraction of bankroll, no edge
    claim. Useful for stress-testing the engine and for sanity-checking
    that real strategies actually beat "do something random."
    """
    if snap.latest_price is None:
        return None
    return Signal(
        direction="YES",
        probability=snap.latest_price,
        fraction_override=fraction,
    )


def strategy_momentum(
    snap: MarketSnapshot,
    *,
    lookback_hours: float = 72,
    threshold: float = 0.05,
    edge: float = 0.05,
) -> Signal | None:
    """If the market has moved at least ``threshold`` in the last
    ``lookback_hours``, bet in the direction of the move with an assumed
    edge of ``edge`` over the new price.

    Premise: large recent moves reflect new information that the market
    will continue to incorporate — the trend persists.
    """
    p_now = snap.latest_price
    p_then = snap.price_at_lookback(lookback_hours)
    if p_now is None or p_then is None:
        return None
    move = p_now - p_then
    if move >= threshold:
        return Signal(direction="YES", probability=min(1.0, p_now + edge))
    if move <= -threshold:
        return Signal(direction="NO", probability=max(0.0, p_now - edge))
    return None


def strategy_mean_reversion(
    snap: MarketSnapshot,
    *,
    high: float = 0.85,
    low: float = 0.15,
    edge: float = 0.05,
) -> Signal | None:
    """Bet against the extremes. If the market price is above ``high``,
    bet NO with assumed edge; below ``low``, bet YES.

    Premise: extreme prices over-extrapolate from recent information.
    """
    p = snap.latest_price
    if p is None:
        return None
    if p >= high:
        return Signal(direction="NO", probability=max(0.0, p - edge))
    if p <= low:
        return Signal(direction="YES", probability=min(1.0, p + edge))
    return None


# Registry — the UI iterates this. Each value is (display_name, callable).
STRATEGIES: dict[str, tuple[str, Strategy]] = {
    "market_consensus": ("Follow the favorite (baseline)", strategy_market_consensus),
    "always_yes_flat": ("Always YES, flat 1% (dumb baseline)", strategy_always_yes_flat),
    "momentum": ("Momentum (+5% in 72h)", strategy_momentum),
    "mean_reversion": ("Mean reversion (>85% or <15%)", strategy_mean_reversion),
}


# ---------------------------------------------------------------------------
# PnL accounting
# ---------------------------------------------------------------------------

def _effective_entry_price(
    direction: str, market_yes_price: float, half_spread: float,
) -> float:
    """Per-share cost of the chosen side **including the half-spread**.

    Real fills aren't at the mid-quote — you buy at the ask, which sits
    half-a-spread above the mid. ``half_spread`` is in absolute price
    units (so 0.005 = half a cent per side; 0.01 = one cent per side).

    Clamped to (0, 1) so a wide assumed spread on a deep-OTM market
    can't produce a price ≥ 1 (which would break the payoff math).
    """
    raw = market_yes_price if direction == "YES" else 1.0 - market_yes_price
    eff = raw + max(0.0, half_spread)
    # Leave a tiny buffer below 1.0 so 1/eff stays finite and below 1
    # gives non-zero payoff space.
    return min(0.999, max(0.001, eff))


def _trade_pnl(stake: float, entry_price: float, won: bool) -> float:
    """Binary prediction-market payoff against an *already-adjusted*
    ``entry_price`` (callers should pass :func:`_effective_entry_price`).

    A share of the chosen side costs ``entry_price`` and pays $1 if it
    wins, $0 if it loses. So ``stake`` dollars buys ``stake/entry_price``
    shares; PnL is::

        win:  stake * (1 - entry_price) / entry_price
        loss: -stake
    """
    if entry_price <= 0 or entry_price >= 1:
        # Pathological — can't trade at a 0 or 1 price.
        return 0.0
    if won:
        return stake * (1.0 - entry_price) / entry_price
    return -stake


def _kelly_fraction_with_spread(
    your_probability: float, market_yes_price: float,
    direction: str, half_spread: float,
) -> float:
    """Kelly fraction computed against the *effective* (spread-adjusted)
    entry price for the strategy's chosen direction.

    Standard Kelly: ``f = edge / (1 - effective_price)`` for an outcome
    that pays $1 when you bought at ``effective_price``. The effective
    price is worse for you (mid + half-spread on the chosen side), so
    edge is smaller and may even go negative — in which case the
    function returns 0 and the engine skips the trade. This is the
    desired "spread eats the edge" behavior.
    """
    eff_price = _effective_entry_price(direction, market_yes_price, half_spread)
    if direction == "YES":
        your_p = your_probability
    else:
        your_p = 1.0 - your_probability
    edge = your_p - eff_price
    if edge <= 0:
        return 0.0
    denom = 1.0 - eff_price
    if denom <= 0:
        return 0.0
    return edge / denom


def _size_trade(
    signal: Signal,
    market_yes_price: float,
    bankroll: float,
    kelly_multiplier: float,
    half_spread: float,
) -> float:
    """Dollar stake for one trade. Returns 0 when there's no edge in the
    strategy's chosen direction after accounting for the half-spread."""
    if signal.fraction_override is not None:
        return max(0.0, bankroll * float(signal.fraction_override))
    kelly_frac = _kelly_fraction_with_spread(
        signal.probability, market_yes_price, signal.direction, half_spread,
    )
    return bankroll * kelly_frac * kelly_multiplier


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion. Better than
    a normal-approximation CI on small samples or extreme proportions.
    """
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _max_drawdown(equity_curve: pd.Series) -> float:
    """Worst peak-to-trough drop (dollar amount, negative or zero).

    Anchored to the $0 starting bankroll — if the first trades are losses
    the drawdown counts from $0 down, not from the first (negative) sample
    which would otherwise become the running peak by accident.
    """
    if equity_curve.empty:
        return 0.0
    anchored = pd.concat([pd.Series([0.0]), equity_curve], ignore_index=True)
    running_peak = anchored.cummax()
    return float((anchored - running_peak).min())


def _sample_size_warning(n_trades: int) -> str | None:
    """Translate sample size into a plain-English caution. Returns None
    when the sample is large enough to be quietly trusted."""
    if n_trades < 10:
        return (f"Only {n_trades} trades — anything could be noise. "
                "Get to at least 30 before treating any number as signal.")
    if n_trades < 30:
        return (f"{n_trades} trades is tentative. The hit-rate CI is wide; "
                "rerun with more closed markets if available.")
    if n_trades < 100:
        return (f"{n_trades} trades — directional but not robust. "
                "Aim for 100+ before sizing up.")
    return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def run_backtest(
    strategy: Strategy,
    dataset: list[tuple[MarketSnapshot, Resolution]],
    *,
    bankroll: float = 1000.0,
    kelly_multiplier: float = 0.5,
    half_spread: float = 0.005,
    fee_pct: float = 0.0,
    fee_fn: Callable[[float, float, float, str], float] | None = None,
    portfolio_constrained: bool = True,
    max_position_pct: float | None = None,
    strategy_name: str | None = None,
) -> BacktestResult:
    """Apply ``strategy`` to each snapshot in ``dataset`` and aggregate.

    ``kelly_multiplier`` defaults to half-Kelly (0.5) — the same default
    the Kelly Sizer page uses, and what real-world Kelly practitioners
    almost always pick to dampen variance.

    ``half_spread`` and ``fee_pct`` are the transaction-cost knobs:

    - ``half_spread`` (default 0.005 = half a cent per side) shifts the
      effective entry price worse than the mid quote. This both reduces
      payoff (you bought fewer shares per dollar) and shrinks Kelly
      fraction (your edge over the *effective* price is smaller). When
      the spread eats the whole edge, Kelly fraction → 0 → trade skipped.
    - ``fee_pct`` (default 0 = no fees) is a fraction of stake deducted
      from every trade's PnL. For Polymarket-on-Polygon use ≈ 0 (no
      order fees on most markets); for Kalshi the actual fee schedule
      is tier-based — pick a representative value.
    - ``fee_fn`` (default None) lets you plug in a per-trade fee
      calculator that overrides ``fee_pct``. Signature:
      ``(entry_price, exit_price, contracts, ticker) -> dollars``.
      ``kalshi_fees.round_trip_fees`` matches this signature when its
      kwarg-only contracts arg is the third positional — wrap with a
      small lambda or use ``import kalshi_fees;
      fee_fn=lambda e, x, c, t: kalshi_fees.round_trip_fees(
          entry_price=e, exit_price=x, contracts=c, ticker=t)``.
      For held-to-resolution backtests, ``exit_price`` is 1.0 (won)
      or 0.0 (lost) — Kalshi fees are zero at those extremes, so
      only the entry fee matters here.

    Set both to 0 to recover the original "ideal mid-quote fills"
    behaviour. Set them to realistic values to find out whether your
    strategies survive contact with real markets — most don't.

    ``portfolio_constrained`` (default True) tracks available cash
    across the run. Markets are processed in chronological ``as_of``
    order; before each new open, any positions whose resolution time
    has passed get their cash returned. New stakes are capped at
    ``min(desired, available_cash)`` — a strategy fully invested in
    five overlapping markets can't size into the sixth. The Kelly
    fraction is still computed against the configured ``bankroll`` so
    fractions stay comparable to live Kelly usage; the constraint
    bites at the dollar level. Set to False to recover the older
    "every trade sized against full bankroll independently" behavior.
    """
    if strategy_name is None:
        strategy_name = getattr(strategy, "__name__", "anonymous")
    half_spread = max(0.0, float(half_spread))
    fee_pct = max(0.0, float(fee_pct))

    trade_rows: list[dict[str, Any]] = []
    n_evaluated = len(dataset)

    # Sort by as_of so the cash trajectory reflects real time-ordering.
    # Without this, the constraint is applied in dataset order which
    # may not be chronological.
    ordered = sorted(dataset, key=lambda pair: pair[0].as_of)

    cash = float(bankroll)
    min_cash = cash
    n_clipped = 0
    n_skipped_for_cash = 0
    # Open positions awaiting resolution: list of dicts with `stake`,
    # `closed_time`, `gross_pnl` (settled value on resolution).
    pending: list[dict[str, Any]] = []

    def _settle_due(now: datetime) -> None:
        """Return cash from any pending position resolved by `now`."""
        nonlocal cash, pending
        still_pending: list[dict[str, Any]] = []
        for p in pending:
            if p["closed_time"] <= now:
                cash += p["stake"] + p["gross_pnl"]
            else:
                still_pending.append(p)
        pending = still_pending

    for snap, res in ordered:
        if portfolio_constrained:
            _settle_due(snap.as_of)

        signal = strategy(snap)
        if signal is None:
            continue
        market_yes = snap.latest_price
        if market_yes is None:
            continue

        desired_stake = _size_trade(
            signal, market_yes, bankroll, kelly_multiplier, half_spread,
        )
        if desired_stake <= 0:
            continue

        # Hard cap on per-trade size as fraction of bankroll. Mirrors
        # the strategy_runner's --max-position-pct flag so backtests
        # measure what a realistic live config would actually do.
        # Applied BEFORE the portfolio constraint so a single trade
        # can't drain the bankroll regardless of cash availability.
        if max_position_pct is not None:
            desired_stake = min(
                desired_stake, bankroll * max_position_pct,
            )
            if desired_stake <= 0:
                continue

        # Apply portfolio constraint
        if portfolio_constrained:
            available = max(0.0, cash)
            stake = min(desired_stake, available)
            if stake <= 0:
                n_skipped_for_cash += 1
                continue
            if stake < desired_stake - 1e-6:
                n_clipped += 1
            cash -= stake
            min_cash = min(min_cash, cash)
        else:
            stake = desired_stake

        eff_entry = _effective_entry_price(signal.direction, market_yes, half_spread)
        mid_entry = _effective_entry_price(signal.direction, market_yes, 0.0)
        won = (signal.direction == "YES") == res.resolved_yes
        gross_pnl = _trade_pnl(stake, eff_entry, won)
        # Spread cost is the difference between the ideal-fill PnL and
        # the effective-fill PnL — surfaced for visibility, not double-
        # counted (it's already inside gross_pnl).
        ideal_pnl = _trade_pnl(stake, mid_entry, won)
        spread_cost = ideal_pnl - gross_pnl
        # Fee accounting: prefer the per-trade fee_fn callable when
        # provided; fall back to flat fee_pct otherwise.
        if fee_fn is not None:
            # Held-to-resolution: exit is $1 if won, $0 if lost. Note
            # that won is from the strategy-side perspective: for a
            # YES position, exit_price=1 means YES resolved true; for
            # a NO position, exit_price=1 means NO resolved true
            # (i.e., the underlying resolved no). Either way the
            # winning side settles at $1 — that's what the trader
            # actually receives, so it's the right exit_price to feed
            # to the fee calc.
            exit_price = 1.0 if won else 0.0
            contracts = stake / max(eff_entry, 1e-9)
            try:
                fee = float(fee_fn(eff_entry, exit_price, contracts,
                                    res.condition_id))
            except Exception as e:
                log.warning("fee_fn raised on %s (%s); falling back to fee_pct",
                            res.condition_id, e)
                fee = stake * fee_pct
        else:
            fee = stake * fee_pct
        net_pnl = gross_pnl - fee

        if portfolio_constrained:
            pending.append({
                "stake": stake,
                "closed_time": res.closed_time,
                "gross_pnl": gross_pnl,
            })

        trade_rows.append({
            "condition_id": res.condition_id,
            "question": snap.question,
            "category": snap.category,
            "as_of": snap.as_of,
            "closed_time": res.closed_time,
            "direction": signal.direction,
            "probability": float(signal.probability),
            "market_yes_price": float(market_yes),
            "entry_price": float(eff_entry),
            "stake": float(stake),
            "won": bool(won),
            "pnl": float(net_pnl),
            "gross_pnl": float(gross_pnl),
            "spread_cost": float(spread_cost),
            "fee": float(fee),
            "actual": 1.0 if res.resolved_yes else 0.0,
            "brier_term": float((signal.probability - (1.0 if res.resolved_yes else 0.0)) ** 2),
        })

    trades = pd.DataFrame(trade_rows)
    n_trades = len(trades)

    if n_trades == 0:
        return BacktestResult(
            strategy_name=strategy_name,
            n_markets_evaluated=n_evaluated,
            n_trades=0,
            n_wins=0,
            hit_rate=None,
            hit_rate_ci=None,
            total_stake=0.0,
            total_pnl=0.0,
            roi=None,
            mean_pnl_per_trade=None,
            pnl_std=None,
            sharpe_per_trade=None,
            max_drawdown=0.0,
            brier_score=None,
            half_spread=half_spread,
            fee_pct=fee_pct,
            total_fees=0.0,
            implied_spread_cost=0.0,
            portfolio_constrained=portfolio_constrained,
            min_cash_seen=float(bankroll),
            n_clipped_for_cash=0,
            n_skipped_for_cash=n_skipped_for_cash,
            trades=trades,
            equity_curve=pd.DataFrame(columns=["closed_time", "cumulative_pnl"]),
            by_category={},
            warnings=[f"Strategy emitted 0 trades over {n_evaluated} markets."],
        )

    n_wins = int(trades["won"].sum())
    hit_rate = n_wins / n_trades
    hit_rate_ci = _wilson_interval(n_wins, n_trades)

    total_stake = float(trades["stake"].sum())
    total_pnl = float(trades["pnl"].sum())
    roi = total_pnl / total_stake if total_stake > 0 else None
    mean_pnl = float(trades["pnl"].mean())
    pnl_std = float(trades["pnl"].std()) if n_trades >= 2 else None
    sharpe = (mean_pnl / pnl_std) if (pnl_std is not None and pnl_std > 0) else None

    eq = (
        trades.sort_values("closed_time")
              .assign(cumulative_pnl=lambda d: d["pnl"].cumsum())
              [["closed_time", "cumulative_pnl"]]
              .reset_index(drop=True)
    )
    max_dd = _max_drawdown(eq["cumulative_pnl"])
    brier = float(trades["brier_term"].mean())

    by_cat: dict[str, dict[str, Any]] = {}
    for cat, sub in trades.groupby("category"):
        n_c = len(sub)
        w_c = int(sub["won"].sum())
        stake_c = float(sub["stake"].sum())
        pnl_c = float(sub["pnl"].sum())
        by_cat[str(cat) or "Unknown"] = {
            "n_trades": n_c,
            "hit_rate": w_c / n_c if n_c else None,
            "total_pnl": pnl_c,
            "roi": pnl_c / stake_c if stake_c > 0 else None,
            "brier": float(sub["brier_term"].mean()),
        }

    warnings_list: list[str] = []
    w_sample = _sample_size_warning(n_trades)
    if w_sample is not None:
        warnings_list.append(w_sample)
    if roi is not None and roi < 0:
        warnings_list.append(
            f"ROI is {roi*100:+.1f}% — this strategy is losing money on this sample."
        )
    if portfolio_constrained and n_skipped_for_cash > 0:
        warnings_list.append(
            f"{n_skipped_for_cash} trade(s) skipped because cash was "
            "exhausted by earlier open positions — bankroll too small "
            "for the strategy's concurrent open positions on this sample."
        )
    if portfolio_constrained and n_clipped > 0:
        warnings_list.append(
            f"{n_clipped} trade(s) clipped below Kelly-desired stake by "
            "the portfolio constraint."
        )

    total_fees = float(trades["fee"].sum())
    implied_spread_cost = float(trades["spread_cost"].sum())

    return BacktestResult(
        strategy_name=strategy_name,
        n_markets_evaluated=n_evaluated,
        n_trades=n_trades,
        n_wins=n_wins,
        hit_rate=hit_rate,
        hit_rate_ci=hit_rate_ci,
        total_stake=total_stake,
        total_pnl=total_pnl,
        roi=roi,
        mean_pnl_per_trade=mean_pnl,
        pnl_std=pnl_std,
        sharpe_per_trade=sharpe,
        max_drawdown=max_dd,
        brier_score=brier,
        half_spread=half_spread,
        fee_pct=fee_pct,
        total_fees=total_fees,
        implied_spread_cost=implied_spread_cost,
        portfolio_constrained=portfolio_constrained,
        min_cash_seen=float(min_cash if portfolio_constrained else bankroll),
        n_clipped_for_cash=int(n_clipped),
        n_skipped_for_cash=int(n_skipped_for_cash),
        trades=trades,
        equity_curve=eq,
        by_category=by_cat,
        warnings=warnings_list,
    )


def market_baseline_brier(
    dataset: list[tuple[MarketSnapshot, Resolution]],
) -> tuple[float | None, int]:
    """Brier score of "just trust the market" over the same dataset.

    For each snapshot, predict ``latest_price`` and compare to the
    resolution. Returns ``(brier, n)`` — the score and the sample size
    actually used (snapshots without a latest price are skipped).

    This is **the gate** for shipping any alpha strategy: the strategy's
    Brier on its category must beat this baseline's Brier on the same
    category, or the strategy is just paying transaction costs to lose
    to the crowd.
    """
    terms: list[float] = []
    for snap, res in dataset:
        p = snap.latest_price
        if p is None:
            continue
        actual = 1.0 if res.resolved_yes else 0.0
        terms.append((p - actual) ** 2)
    if not terms:
        return (None, 0)
    return (sum(terms) / len(terms), len(terms))


# ---------------------------------------------------------------------------
# Walk-forward backtest (Tier 3b v2)
# ---------------------------------------------------------------------------
#
# Single-shot model: one trade per market at `closed_time - lead_hours`,
# hold to resolution. Sufficient for evaluating "given one decision
# point, what's your edge?" but can't capture strategies that respond
# to intra-market price moves (momentum, mean-reversion).
#
# Walk-forward model:
# - For each market, walk the price history at fixed steps (default
#   24h).
# - At each step, call the strategy with prices truncated strictly
#   before that step (same lookahead-safety guarantee as single-shot).
# - Track ONE in-or-out position per market (v2 — no scaling, no
#   partial exits, exit-and-flip on direction change).
# - Mark-to-market exits on signal flip: sell at (mid − half_spread) on
#   whichever side is being closed out.
# - Any open position at the snapshot's `as_of` (the last walk point)
#   is closed at resolution — $1 if direction matches resolved_yes,
#   $0 otherwise.
#
# Same `BacktestResult` shape as single-shot, with extra optional
# columns on the trades DataFrame: `entry_time`, `exit_time`,
# `exit_reason` ("signal_flip" | "resolution"), `holding_period_days`.
# This lets the Backtest page render walk-forward results through the
# same code path.


def _eval_timestamps(
    prices: pd.DataFrame, *, step_hours: float, end: datetime,
) -> list[datetime]:
    """Walk forward through `prices` at fixed `step_hours` intervals.

    Returns a list of strictly-increasing timestamps starting one step
    past the earliest price observation and ending at-or-before
    ``end``. Empty if there's no usable history.
    """
    if prices.empty or len(prices) < 2:
        return []
    start = prices["timestamp"].iloc[0].to_pydatetime() if hasattr(
        prices["timestamp"].iloc[0], "to_pydatetime"
    ) else prices["timestamp"].iloc[0]
    step = timedelta(hours=step_hours)
    out: list[datetime] = []
    target = start + step
    while target <= end:
        out.append(target)
        target += step
    return out


def _exit_price_at_mark(
    direction: str, market_yes_price: float, half_spread: float,
) -> float:
    """Effective price the held position is sold for at a mid quote.

    When closing a YES position we sell into the YES bid, i.e.
    ``market_yes − half_spread``. When closing a NO position we sell
    into the NO bid, i.e. ``(1 − market_yes) − half_spread``. Clamped
    to (0, 1) so a wide assumed spread on an extreme-price market
    can't push the exit price below 0 or above 1.
    """
    if direction == "YES":
        raw = market_yes_price - half_spread
    else:
        raw = (1.0 - market_yes_price) - half_spread
    return min(0.999, max(0.001, raw))


@dataclass
class _OpenPosition:
    """Mutable per-market state during a walk-forward pass."""

    direction: str             # "YES" or "NO"
    entry_time: datetime
    entry_price: float         # effective price at entry (mid + half-spread)
    market_yes_entry: float    # raw mid quote at entry — used for note
    stake: float               # dollars deployed at entry
    probability_at_entry: float  # strategy's prob estimate at entry


def _close_position_at_mark(
    pos: _OpenPosition, *, exit_time: datetime, market_yes_at_exit: float,
    half_spread: float, exit_reason: str,
) -> dict[str, Any]:
    """Build a trade-row dict for closing `pos` at the current mark."""
    exit_price = _exit_price_at_mark(pos.direction, market_yes_at_exit, half_spread)
    # Shares bought: stake / entry_price. Sold at exit_price.
    shares = pos.stake / pos.entry_price
    proceeds = shares * exit_price
    gross_pnl = proceeds - pos.stake
    # Spread cost = what we'd have made selling at mid minus what we
    # actually got selling at the half-spread-adjusted price. Reported
    # for visibility; already absorbed into gross_pnl.
    mid_proceeds = shares * (
        market_yes_at_exit if pos.direction == "YES"
        else (1.0 - market_yes_at_exit)
    )
    spread_cost = mid_proceeds - proceeds
    holding_days = max(0.0,
                        (exit_time - pos.entry_time).total_seconds() / 86_400.0)
    return {
        "direction": pos.direction,
        "probability": float(pos.probability_at_entry),
        "market_yes_price": float(pos.market_yes_entry),
        "entry_price": float(pos.entry_price),
        "stake": float(pos.stake),
        "won": bool(gross_pnl > 0),
        "gross_pnl": float(gross_pnl),
        "spread_cost": float(spread_cost),
        "exit_price": float(exit_price),
        "entry_time": pos.entry_time,
        "exit_time": exit_time,
        "exit_reason": exit_reason,
        "holding_period_days": float(holding_days),
    }


def _close_position_at_resolution(
    pos: _OpenPosition, *, exit_time: datetime, resolved_yes: bool,
    market_yes_at_exit: float,
) -> dict[str, Any]:
    """Build a trade-row dict for closing `pos` at $1/$0 on resolution.
    No spread is paid here — winning shares redeem automatically on
    Polymarket; losing shares go to $0. This is the same accounting as
    single-shot's hold-to-resolution path."""
    won = (pos.direction == "YES") == resolved_yes
    if won:
        # stake / entry_price shares × $1 each, minus the stake
        gross_pnl = pos.stake * (1.0 - pos.entry_price) / pos.entry_price
    else:
        gross_pnl = -pos.stake
    holding_days = max(0.0,
                        (exit_time - pos.entry_time).total_seconds() / 86_400.0)
    return {
        "direction": pos.direction,
        "probability": float(pos.probability_at_entry),
        "market_yes_price": float(pos.market_yes_entry),
        "entry_price": float(pos.entry_price),
        "stake": float(pos.stake),
        "won": bool(won),
        "gross_pnl": float(gross_pnl),
        "spread_cost": 0.0,
        "exit_price": 1.0 if won else 0.0,
        "entry_time": pos.entry_time,
        "exit_time": exit_time,
        "exit_reason": "resolution",
        "holding_period_days": float(holding_days),
        "market_yes_at_exit": float(market_yes_at_exit),
    }


def _walk_forward_one_market(
    snap: MarketSnapshot, res: Resolution, strategy: Strategy,
    *, bankroll: float, kelly_multiplier: float, half_spread: float,
    fee_pct: float, eval_step_hours: float,
) -> list[dict[str, Any]]:
    """Walk one market's price history step by step, returning the
    trade-row dicts produced. Position state is local to this market
    — bankroll constraint is per-trade, not portfolio-wide (v2)."""
    eval_times = _eval_timestamps(
        snap.prices, step_hours=eval_step_hours, end=snap.as_of,
    )
    if not eval_times:
        return []

    open_pos: _OpenPosition | None = None
    trade_rows: list[dict[str, Any]] = []

    actual = 1.0 if res.resolved_yes else 0.0

    for t in eval_times:
        # Sub-snapshot truncated to rows strictly-before t (same lookahead-
        # safety guarantee as build_dataset's initial cut).
        sub_prices = snap.prices[snap.prices["timestamp"] < t].reset_index(drop=True)
        if sub_prices.empty:
            continue
        current_mid = float(sub_prices["price"].iloc[-1])

        sub_snap = MarketSnapshot(
            condition_id=snap.condition_id,
            question=snap.question,
            category=snap.category,
            as_of=t,
            prices=sub_prices,
            end_date=snap.end_date,
        )

        signal = strategy(sub_snap)

        # Position-management logic:
        if open_pos is None and signal is not None:
            # Open new position.
            stake = _size_trade(
                signal, current_mid, bankroll, kelly_multiplier, half_spread,
            )
            if stake > 0:
                eff_entry = _effective_entry_price(
                    signal.direction, current_mid, half_spread,
                )
                open_pos = _OpenPosition(
                    direction=signal.direction,
                    entry_time=t,
                    entry_price=eff_entry,
                    market_yes_entry=current_mid,
                    stake=stake,
                    probability_at_entry=float(signal.probability),
                )
        elif open_pos is not None and signal is not None and signal.direction != open_pos.direction:
            # Direction flip: close the existing position at mark, open
            # a fresh one if the new signal has Kelly-eligible edge.
            row = _close_position_at_mark(
                open_pos, exit_time=t, market_yes_at_exit=current_mid,
                half_spread=half_spread, exit_reason="signal_flip",
            )
            trade_rows.append(_finalize_trade_row(
                row, snap=snap, res=res, fee_pct=fee_pct, actual=actual,
            ))
            open_pos = None

            stake = _size_trade(
                signal, current_mid, bankroll, kelly_multiplier, half_spread,
            )
            if stake > 0:
                eff_entry = _effective_entry_price(
                    signal.direction, current_mid, half_spread,
                )
                open_pos = _OpenPosition(
                    direction=signal.direction,
                    entry_time=t,
                    entry_price=eff_entry,
                    market_yes_entry=current_mid,
                    stake=stake,
                    probability_at_entry=float(signal.probability),
                )
        # All other combinations are "hold":
        # - open_pos is None and signal is None → flat, stay flat
        # - open_pos is not None and signal is None → hold existing
        # - open_pos is not None and signal.direction == open_pos.direction → hold

    # Always close any open position at resolution. This is the
    # honest hold-to-resolution accounting — same payoff math as
    # single-shot for held-through trades.
    if open_pos is not None:
        # Resolution-exit uses the snapshot's last known mark for the
        # market_yes_at_exit field, but the actual payoff is $1/$0.
        last_mid = (
            float(snap.prices["price"].iloc[-1])
            if not snap.prices.empty else open_pos.market_yes_entry
        )
        row = _close_position_at_resolution(
            open_pos, exit_time=res.closed_time,
            resolved_yes=res.resolved_yes,
            market_yes_at_exit=last_mid,
        )
        trade_rows.append(_finalize_trade_row(
            row, snap=snap, res=res, fee_pct=fee_pct, actual=actual,
        ))

    return trade_rows


def _finalize_trade_row(
    row: dict[str, Any], *, snap: MarketSnapshot, res: Resolution,
    fee_pct: float, actual: float,
) -> dict[str, Any]:
    """Stamp the per-market context + fee onto a trade row. Shared by
    the signal-flip and resolution-exit paths so the schema stays
    uniform."""
    fee = float(row["stake"]) * float(fee_pct)
    row["fee"] = fee
    row["pnl"] = float(row["gross_pnl"]) - fee
    row["condition_id"] = res.condition_id
    row["question"] = snap.question
    row["category"] = snap.category
    # `as_of` is kept on the trade row for compatibility with single-
    # shot's schema (the Backtest page sorts by this) — for walk-
    # forward it equals the entry_time.
    row["as_of"] = row["entry_time"]
    row["closed_time"] = res.closed_time
    row["actual"] = actual
    row["brier_term"] = float((row["probability"] - actual) ** 2)
    return row


def _walk_forward_constrained(
    dataset: list[tuple[MarketSnapshot, Resolution]],
    strategy: Strategy,
    *, bankroll: float, kelly_multiplier: float, half_spread: float,
    fee_pct: float, eval_step_hours: float,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Portfolio-constrained walk-forward.

    Builds a single time-ordered event queue across every market in
    the dataset — eval points + resolution events — and walks it with
    one shared ``cash`` variable. A position open in market A blocks
    or shrinks a contemporaneous open in market B; A's resolution
    returns its proceeds to the pool that B's next decision sees.

    The unconstrained variant runs each market's loop in isolation
    (``_walk_forward_one_market``), which can produce concurrent open
    stakes that exceed the configured bankroll — useful as a "pure"
    strategy-comparison baseline but not realistic.

    Returns (trade_rows, portfolio_stats) so the caller can populate
    the BacktestResult's portfolio fields.
    """
    events: list[tuple[datetime, int, int]] = []  # (time, kind_rank, market_idx)
    # kind_rank: 0=eval (decide first), 1=resolution (settle after)
    for i, (snap, _res) in enumerate(dataset):
        eval_times = _eval_timestamps(
            snap.prices, step_hours=eval_step_hours, end=snap.as_of,
        )
        for t in eval_times:
            events.append((t, 0, i))
        events.append((_res.closed_time, 1, i))
    events.sort(key=lambda e: (e[0], e[1]))

    cash = float(bankroll)
    min_cash = cash
    n_clipped = 0
    n_skipped = 0
    positions: dict[int, _OpenPosition] = {}
    trade_rows: list[dict[str, Any]] = []

    def _open_at(idx: int, t: datetime, signal: Signal, current_mid: float) -> None:
        """Open a position with the cash constraint applied. Updates
        the closed-over cash / min_cash / counters."""
        nonlocal cash, min_cash, n_clipped, n_skipped
        desired = _size_trade(
            signal, current_mid, bankroll, kelly_multiplier, half_spread,
        )
        if desired <= 0:
            return
        actual_stake = min(desired, max(0.0, cash))
        if actual_stake <= 0:
            n_skipped += 1
            return
        if actual_stake < desired - 1e-6:
            n_clipped += 1
        eff_entry = _effective_entry_price(signal.direction, current_mid, half_spread)
        positions[idx] = _OpenPosition(
            direction=signal.direction, entry_time=t,
            entry_price=eff_entry, market_yes_entry=current_mid,
            stake=actual_stake,
            probability_at_entry=float(signal.probability),
        )
        cash -= actual_stake
        min_cash = min(min_cash, cash)

    for t, kind_rank, idx in events:
        snap, res = dataset[idx]
        actual = 1.0 if res.resolved_yes else 0.0

        if kind_rank == 1:  # resolution
            pos = positions.pop(idx, None)
            if pos is None:
                continue
            last_mid = (
                float(snap.prices["price"].iloc[-1])
                if not snap.prices.empty else pos.market_yes_entry
            )
            row = _close_position_at_resolution(
                pos, exit_time=res.closed_time,
                resolved_yes=res.resolved_yes,
                market_yes_at_exit=last_mid,
            )
            trade_rows.append(_finalize_trade_row(
                row, snap=snap, res=res, fee_pct=fee_pct, actual=actual,
            ))
            cash += pos.stake + row["gross_pnl"]
            continue

        # kind_rank == 0: eval
        sub_prices = snap.prices[snap.prices["timestamp"] < t].reset_index(drop=True)
        if sub_prices.empty:
            continue
        current_mid = float(sub_prices["price"].iloc[-1])
        sub_snap = MarketSnapshot(
            condition_id=snap.condition_id, question=snap.question,
            category=snap.category, as_of=t, prices=sub_prices,
            end_date=snap.end_date,
        )
        signal = strategy(sub_snap)
        open_pos = positions.get(idx)

        if open_pos is None and signal is not None:
            _open_at(idx, t, signal, current_mid)
        elif (open_pos is not None and signal is not None
              and signal.direction != open_pos.direction):
            # Close existing at mark; return cash; try to open opposite
            row = _close_position_at_mark(
                open_pos, exit_time=t, market_yes_at_exit=current_mid,
                half_spread=half_spread, exit_reason="signal_flip",
            )
            trade_rows.append(_finalize_trade_row(
                row, snap=snap, res=res, fee_pct=fee_pct, actual=actual,
            ))
            cash += open_pos.stake + row["gross_pnl"]
            del positions[idx]
            _open_at(idx, t, signal, current_mid)
        # else: hold (flat/no-signal or held/same-direction or held/no-signal)

    return trade_rows, {
        "min_cash_seen": float(min_cash),
        "n_clipped": int(n_clipped),
        "n_skipped": int(n_skipped),
    }


def run_backtest_walk_forward(
    strategy: Strategy,
    dataset: list[tuple[MarketSnapshot, Resolution]],
    *,
    bankroll: float = 1000.0,
    kelly_multiplier: float = 0.5,
    half_spread: float = 0.005,
    fee_pct: float = 0.0,
    eval_step_hours: float = 24.0,
    portfolio_constrained: bool = True,
    strategy_name: str | None = None,
) -> BacktestResult:
    """Walk-forward variant of :func:`run_backtest`.

    For each market in ``dataset``, walks the price history at
    ``eval_step_hours`` intervals, calling the strategy at each step
    with prices truncated strictly before the eval time (same look-
    ahead-safety guarantee as single-shot). One in-or-out position
    per market; mark-to-market exits on signal flip; resolution exit
    at $1/$0.

    ``portfolio_constrained`` (default True) interleaves every market's
    eval + resolution events into one time-ordered queue and walks
    them with a shared cash variable, so concurrent open positions
    actually compete for bankroll. Setting it False reverts to per-
    market isolation — each market's loop runs sequentially with
    full bankroll, useful as an unconstrained strategy-comparison
    baseline but not realistic for a single-account live execution.

    The aggregate metric computation (hit_rate, ROI, Sharpe, drawdown,
    Brier, by_category, warnings) reuses single-shot's plumbing —
    walk-forward just produces a richer trade list.
    """
    if strategy_name is None:
        strategy_name = getattr(strategy, "__name__", "anonymous")
    half_spread = max(0.0, float(half_spread))
    fee_pct = max(0.0, float(fee_pct))

    n_evaluated = len(dataset)

    if portfolio_constrained:
        trade_rows, port_stats = _walk_forward_constrained(
            dataset, strategy,
            bankroll=bankroll, kelly_multiplier=kelly_multiplier,
            half_spread=half_spread, fee_pct=fee_pct,
            eval_step_hours=eval_step_hours,
        )
        n_skipped_for_cash = port_stats["n_skipped"]
        n_clipped_for_cash = port_stats["n_clipped"]
        min_cash_seen = port_stats["min_cash_seen"]
    else:
        trade_rows = []
        for snap, res in dataset:
            trade_rows.extend(_walk_forward_one_market(
                snap, res, strategy,
                bankroll=bankroll, kelly_multiplier=kelly_multiplier,
                half_spread=half_spread, fee_pct=fee_pct,
                eval_step_hours=eval_step_hours,
            ))
        n_skipped_for_cash = 0
        n_clipped_for_cash = 0
        min_cash_seen = float(bankroll)

    trades = pd.DataFrame(trade_rows)
    n_trades = len(trades)

    if n_trades == 0:
        return BacktestResult(
            strategy_name=strategy_name,
            n_markets_evaluated=n_evaluated,
            n_trades=0,
            n_wins=0,
            hit_rate=None,
            hit_rate_ci=None,
            total_stake=0.0,
            total_pnl=0.0,
            roi=None,
            mean_pnl_per_trade=None,
            pnl_std=None,
            sharpe_per_trade=None,
            max_drawdown=0.0,
            brier_score=None,
            half_spread=half_spread,
            fee_pct=fee_pct,
            total_fees=0.0,
            implied_spread_cost=0.0,
            portfolio_constrained=portfolio_constrained,
            min_cash_seen=float(min_cash_seen),
            n_clipped_for_cash=int(n_clipped_for_cash),
            n_skipped_for_cash=int(n_skipped_for_cash),
            trades=trades,
            equity_curve=pd.DataFrame(columns=["closed_time", "cumulative_pnl"]),
            by_category={},
            warnings=[f"Strategy emitted 0 trades over {n_evaluated} markets "
                      f"(walk-forward, step={eval_step_hours}h)."],
        )

    n_wins = int(trades["won"].sum())
    hit_rate = n_wins / n_trades
    hit_rate_ci = _wilson_interval(n_wins, n_trades)

    total_stake = float(trades["stake"].sum())
    total_pnl = float(trades["pnl"].sum())
    roi = total_pnl / total_stake if total_stake > 0 else None
    mean_pnl = float(trades["pnl"].mean())
    pnl_std = float(trades["pnl"].std()) if n_trades >= 2 else None
    sharpe = (mean_pnl / pnl_std) if (pnl_std is not None and pnl_std > 0) else None

    # Equity curve uses exit_time (when the cash actually arrives) for
    # walk-forward — closer-time alone hides the intra-market signal-flip
    # trades that resolved before market close.
    eq = (
        trades.sort_values("exit_time")
              .assign(cumulative_pnl=lambda d: d["pnl"].cumsum())
              [["exit_time", "cumulative_pnl"]]
              .rename(columns={"exit_time": "closed_time"})
              .reset_index(drop=True)
    )
    max_dd = _max_drawdown(eq["cumulative_pnl"])
    brier = float(trades["brier_term"].mean())

    by_cat: dict[str, dict[str, Any]] = {}
    for cat, sub in trades.groupby("category"):
        n_c = len(sub)
        w_c = int(sub["won"].sum())
        stake_c = float(sub["stake"].sum())
        pnl_c = float(sub["pnl"].sum())
        by_cat[str(cat) or "Unknown"] = {
            "n_trades": n_c,
            "hit_rate": w_c / n_c if n_c else None,
            "total_pnl": pnl_c,
            "roi": pnl_c / stake_c if stake_c > 0 else None,
            "brier": float(sub["brier_term"].mean()),
        }

    warnings_list: list[str] = []
    w_sample = _sample_size_warning(n_trades)
    if w_sample is not None:
        warnings_list.append(w_sample)
    if roi is not None and roi < 0:
        warnings_list.append(
            f"ROI is {roi*100:+.1f}% — this strategy is losing money on this sample."
        )
    n_signal_flips = int((trades["exit_reason"] == "signal_flip").sum())
    if n_signal_flips:
        warnings_list.append(
            f"{n_signal_flips} of {n_trades} trades exited on signal flip; "
            "mark-to-market PnL on those depends on intermediate prices "
            "the live engine may not be able to fill at."
        )
    if portfolio_constrained and n_skipped_for_cash > 0:
        warnings_list.append(
            f"{n_skipped_for_cash} trade(s) skipped because cash was "
            "exhausted by earlier open positions — bankroll too small "
            "for the strategy's concurrent open positions on this sample."
        )
    if portfolio_constrained and n_clipped_for_cash > 0:
        warnings_list.append(
            f"{n_clipped_for_cash} trade(s) clipped below Kelly-desired "
            "stake by the portfolio constraint."
        )

    total_fees = float(trades["fee"].sum())
    implied_spread_cost = float(trades["spread_cost"].sum())

    return BacktestResult(
        strategy_name=strategy_name,
        n_markets_evaluated=n_evaluated,
        n_trades=n_trades,
        n_wins=n_wins,
        hit_rate=hit_rate,
        hit_rate_ci=hit_rate_ci,
        total_stake=total_stake,
        total_pnl=total_pnl,
        roi=roi,
        mean_pnl_per_trade=mean_pnl,
        pnl_std=pnl_std,
        sharpe_per_trade=sharpe,
        max_drawdown=max_dd,
        brier_score=brier,
        half_spread=half_spread,
        fee_pct=fee_pct,
        total_fees=total_fees,
        implied_spread_cost=implied_spread_cost,
        portfolio_constrained=portfolio_constrained,
        min_cash_seen=float(min_cash_seen),
        n_clipped_for_cash=int(n_clipped_for_cash),
        n_skipped_for_cash=int(n_skipped_for_cash),
        trades=trades,
        equity_curve=eq,
        by_category=by_cat,
        warnings=warnings_list,
    )


def register_strategy(key: str, display_name: str | None = None):
    """Decorator for adding a strategy to the global ``STRATEGIES``
    registry. Designed for user-defined plugins in ``user_strategies/``.

    Built-in strategies populate the registry directly via the dict
    literal at the bottom of this module. User strategies use this
    decorator so they don't have to import + mutate ``STRATEGIES``
    by hand. Registering a key that already exists *overrides* the
    previous entry (last writer wins) — intentional, so users can
    shadow a built-in with their own variant if they want, but logged
    as a warning so it's not silent.

    Usage in a user file::

        from pm.research import backtest as bt

        @bt.register_strategy("buy_lows", "Buy when YES < 25%")
        def my_strategy(snap, *, threshold: float = 0.25):
            if snap.latest_price is None or snap.latest_price >= threshold:
                return None
            return bt.Signal(direction="YES",
                              probability=min(0.99, snap.latest_price + 0.20))

    The decorator returns the original function unchanged so the
    strategy is also directly callable for unit tests.
    """
    if not key:
        raise ValueError("register_strategy: key must be a non-empty string")

    def decorator(fn: Strategy) -> Strategy:
        if key in STRATEGIES:
            existing_display = STRATEGIES[key][0]
            logging.getLogger(__name__).warning(
                "register_strategy: overriding existing key %r (was %r)",
                key, existing_display,
            )
        STRATEGIES[key] = (display_name or fn.__name__, fn)
        return fn

    return decorator


def discover_user_strategies(
    *, package: str = "user_strategies", search_dir: Path | None = None,
) -> int:
    """Import every Python module under ``user_strategies/`` so its
    ``@register_strategy`` decorators fire. Returns the number of
    modules successfully imported.

    Safe to call multiple times: Python caches modules, so a re-import
    is a no-op. Decorator-driven registration in ``STRATEGIES`` is
    idempotent on identical keys (last writer wins; logged on override).

    A user plugin that raises at import time is **logged and skipped**
    rather than crashing the app — a broken strategy file shouldn't
    block the rest of the framework. The traceback goes to the log so
    the user can find and fix it.
    """
    if search_dir is None:
        # user_strategies/ is a drop-in dir at the REPO root; __file__ stopped
        # pointing there when R-24 moved this module (gotcha 1b)
        search_dir = REPO_ROOT / package
    if not search_dir.exists() or not search_dir.is_dir():
        return 0

    n_loaded = 0
    log = logging.getLogger(__name__)
    # Use pkgutil to walk only the top-level modules; subdirectories
    # need their own __init__.py to be importable, which the user can
    # add if they want package-style organization.
    import pkgutil
    for module_info in pkgutil.iter_modules([str(search_dir)]):
        if module_info.name.startswith("_"):
            continue
        full_name = f"{package}.{module_info.name}"
        try:
            importlib.import_module(full_name)
            n_loaded += 1
        except Exception as e:
            log.warning("discover_user_strategies: failed to load %s: %s",
                        full_name, e, exc_info=True)
    return n_loaded


def list_strategies() -> list[tuple[str, str]]:
    """Strategy registry as [(key, display_name)]. Used by the UI to
    populate the strategy selector."""
    return [(k, v[0]) for k, v in STRATEGIES.items()]


def get_strategy(key: str) -> Strategy:
    """Look up a strategy by registry key. Raises KeyError for unknown."""
    return STRATEGIES[key][1]


__all__ = [
    "MarketSnapshot",
    "Resolution",
    "Signal",
    "BacktestResult",
    "Strategy",
    "STRATEGIES",
    "build_dataset",
    "run_backtest",
    "run_backtest_walk_forward",
    "market_baseline_brier",
    "list_strategies",
    "get_strategy",
    "register_strategy",
    "discover_user_strategies",
    "strategy_market_consensus",
    "strategy_always_yes_flat",
    "strategy_momentum",
    "strategy_mean_reversion",
]
