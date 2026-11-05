"""Find markets that oscillate rather than trend.

A mean-reversion strategy makes money when a market's price bounces
around a central level instead of drifting toward 0 or 1. The hard
part is *finding* those markets — most prediction markets either sit
flat or trend smoothly toward resolution. This scanner ranks active
markets by how much oscillation they actually exhibit over recent
history.

The headline metric is ``range_score`` =
``detrended_std / max(|drift|, ε)``. Intuitively:

- High detrended_std + low drift → price wobbles inside a band → high score.
- Low detrended_std + high drift → smooth trend → low score.
- High detrended_std + high drift → noisy trend → middling score.

We pair that with ``mean_cross_freq`` (how often the price crosses its
own running mean per day) as a secondary check — a true range-bound
market crosses its mean repeatedly; a trend with one big shock crosses
it once.

Public surface:

    compute_metrics(df_prices) -> OscillationMetrics | None
        Pure function on a price-history DataFrame
        (timestamp, price columns). Returns None when history is too
        short to be meaningful (< ``min_history_points``).

    scan_markets(provider, df_markets, *, days=30, fidelity=60,
                  max_markets=200, min_history_points=20,
                  min_avg_price=0.05, max_avg_price=0.95,
                  progress_callback=None) -> pd.DataFrame
        Per-market metrics, ranked descending by range_score. Skips
        markets too close to 0 or 1 (price compression makes the score
        meaningless near the boundaries).

CLI: ``python -m oscillation_scanner --days 30 --top 20`` (see __main__).

Cost: one ``get_price_history`` call per market. At ~100ms each that's
~20s for 200 markets sequentially. Good enough for cron / on-demand
inspection; not hot-path.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from pm.core import logctx

log = logging.getLogger(__name__)


@dataclass
class OscillationMetrics:
    """One scanned market's behavior over the lookback window.

    All "price" fields are YES-side fractions in [0, 1].
    """

    condition_id: str
    question: str
    category: str
    current_price: float
    n_history: int
    days_covered: float
    avg_price: float
    min_price: float
    max_price: float
    range_width: float
    drift_abs: float
    drift_pct: float
    detrended_std: float
    range_score: float
    mean_cross_freq: float


def compute_metrics(
    df_prices: pd.DataFrame,
    *,
    condition_id: str = "",
    question: str = "",
    category: str = "",
    min_history_points: int = 20,
) -> OscillationMetrics | None:
    """Compute oscillation metrics over a price-history DataFrame.

    Requires columns ``timestamp`` (datetime64 UTC) and ``price``
    (float in [0, 1]) per ``get_price_history`` schema. Returns None
    when fewer than ``min_history_points`` valid points are present.

    >>> import pandas as pd
    >>> t = pd.date_range("2026-01-01", periods=30, freq="1h", tz="UTC")
    >>> # Pure trend from 0.30 → 0.70 — minimal oscillation
    >>> trend = pd.DataFrame({"timestamp": t,
    ...                       "price": np.linspace(0.30, 0.70, 30)})
    >>> m = compute_metrics(trend)
    >>> m.range_score < 0.1
    True
    """
    if df_prices is None or len(df_prices) < min_history_points:
        return None
    df = df_prices.dropna(subset=["price"]).copy()
    if len(df) < min_history_points:
        return None
    df = df.sort_values("timestamp").reset_index(drop=True)
    prices = df["price"].to_numpy(dtype=float)
    times = pd.to_datetime(df["timestamp"], utc=True)
    days_covered = float((times.iloc[-1] - times.iloc[0]).total_seconds() / 86400.0)
    if days_covered <= 0:
        return None

    # Fit a linear trend (price ~ a + b * t_norm) and use its residuals
    # as the "detrended" series. Using normalized time so the slope's
    # magnitude is interpretable as drift over the window.
    t_norm = np.linspace(0.0, 1.0, len(prices))
    slope, intercept = np.polyfit(t_norm, prices, 1)
    trend = intercept + slope * t_norm
    residuals = prices - trend
    detrended_std = float(np.std(residuals))

    # Drift = signed change implied by the fitted line over the window.
    # We take its absolute value for ranking; sign is recoverable via
    # min/max if needed.
    drift_abs = float(abs(slope))  # slope == change over [0, 1] in t_norm
    avg_price = float(np.mean(prices))
    drift_pct = drift_abs / max(avg_price, 1e-6)

    # range_score: oscillation relative to drift. Epsilon prevents
    # division by zero when drift is essentially zero (in which case
    # any oscillation looks "infinitely" oscillation-dominant — we
    # cap at a sane upper bound by adding eps).
    range_score = detrended_std / max(drift_abs, 1e-3)

    # Mean-crossing frequency over the running-mean line. A trend
    # crosses its mean once; a sine wave crosses many times.
    running_mean = float(np.mean(prices))
    centered = prices - running_mean
    sign_changes = int(np.sum(np.diff(np.sign(centered)) != 0))
    mean_cross_freq = sign_changes / max(days_covered, 1e-6)

    return OscillationMetrics(
        condition_id=condition_id,
        question=question,
        category=category,
        current_price=float(prices[-1]),
        n_history=int(len(prices)),
        days_covered=round(days_covered, 3),
        avg_price=round(avg_price, 6),
        min_price=float(np.min(prices)),
        max_price=float(np.max(prices)),
        range_width=float(np.max(prices) - np.min(prices)),
        drift_abs=round(drift_abs, 6),
        drift_pct=round(drift_pct, 6),
        detrended_std=round(detrended_std, 6),
        range_score=round(range_score, 4),
        mean_cross_freq=round(mean_cross_freq, 4),
    )


def scan_markets(
    provider: Any,
    df_markets: pd.DataFrame,
    *,
    days: int = 30,
    fidelity: int = 60,
    max_markets: int = 200,
    min_history_points: int = 20,
    min_avg_price: float = 0.05,
    max_avg_price: float = 0.95,
    min_cross_freq: float = 0.0,
    min_range_width: float = 0.0,
    progress_callback: Callable[[int, int], None] | None = None,
) -> pd.DataFrame:
    """Score every market in ``df_markets`` (up to ``max_markets``)
    and return them sorted descending by ``range_score``.

    Skips markets whose average price is outside
    ``[min_avg_price, max_avg_price]`` — near 0 or 1 the price is
    compressed and standard-deviation-based scores don't translate
    to tradeable oscillation.

    ``provider`` must expose ``get_price_history(market_id, days,
    fidelity)`` returning a DataFrame with ``timestamp`` / ``price``
    columns. The market_id is taken from each row's ``condition_id``;
    if the provider needs a different key (Polymarket needs CLOB token
    IDs, not condition IDs), wrap the call in a thin adapter that
    handles the lookup.

    Failures on individual markets are logged + skipped; one bad
    market doesn't break the scan.
    """
    if df_markets is None or df_markets.empty:
        return pd.DataFrame()
    rows: list[OscillationMetrics] = []
    universe = df_markets.head(max_markets)
    total = len(universe)

    for i, (_, market) in enumerate(universe.iterrows()):
        cid = str(market.get("condition_id", "") or "")
        if not cid:
            continue
        try:
            df_prices = provider.get_price_history(
                cid, days=days, fidelity=fidelity,
            )
        except Exception as e:
            log.debug("price history failed for %s: %s", cid, e)
            if progress_callback:
                progress_callback(i + 1, total)
            continue
        if df_prices is None or df_prices.empty:
            if progress_callback:
                progress_callback(i + 1, total)
            continue

        metrics = compute_metrics(
            df_prices,
            condition_id=cid,
            question=str(market.get("question", "") or ""),
            category=str(market.get("category", "") or ""),
            min_history_points=min_history_points,
        )
        if metrics is None:
            if progress_callback:
                progress_callback(i + 1, total)
            continue
        # Filter by average price — meaningless oscillation when
        # we're glued to a boundary.
        if not (min_avg_price <= metrics.avg_price <= max_avg_price):
            if progress_callback:
                progress_callback(i + 1, total)
            continue
        # Reject markets that jitter-look-bouncy: high score on the
        # ratio metric but the actual price barely moves OR rarely
        # crosses its mean. A true range-bound market crosses its
        # mean repeatedly AND has a meaningful absolute range.
        if metrics.mean_cross_freq < min_cross_freq:
            if progress_callback:
                progress_callback(i + 1, total)
            continue
        if metrics.range_width < min_range_width:
            if progress_callback:
                progress_callback(i + 1, total)
            continue
        rows.append(metrics)
        if progress_callback:
            progress_callback(i + 1, total)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([asdict(r) for r in rows])
    return df.sort_values("range_score", ascending=False).reset_index(drop=True)


# ──────────────────────────────────────────────────────────────────────
# Mean-reversion strategy built on top of the oscillation metrics
# ──────────────────────────────────────────────────────────────────────
#
# Premise: in a market that genuinely oscillates around a stable mean,
# the *current* price's z-score relative to that mean is informative.
# Far above mean → bet NO. Far below → bet YES. The oscillation filters
# (range_score, mean_cross_freq, range_width) guard against trending
# markets where a "low price" just means it's heading to zero.
#
# Implemented as a registered backtest.Strategy so it composes with
# every existing engine: `run_backtest`, `run_backtest_walk_forward`,
# and the live `strategy_runner` (which fetches history when
# `--snapshot-history-days` is set).


def strategy_oscillation_revert(
    snap: Any,  # backtest.MarketSnapshot at runtime — Any to avoid a
                # circular import at module load
    *,
    z_threshold: float = 1.0,
    edge: float = 0.05,
    min_range_score: float = 0.5,
    min_cross_freq: float = 0.5,
    min_range_width: float = 0.05,
    min_history_points: int = 20,
    min_avg_price: float = 0.05,
    max_avg_price: float = 0.95,
) -> Any:  # backtest.Signal | None
    """⚠️ KNOWN-BAD reference strategy. Retained for comparative
    backtests; **NOT for live use**.

    Empirical finding (n=2155 settled Kalshi markets, 90d window,
    lead_hours=48, fees included):
        Strategy Brier  = 0.169  (vs market-baseline 0.079, 2.15× WORSE)
        Hit rate        = 25%
        Total P&L       = -$—  (ROI -21.9%)
        Bucketing by    range_score is NOT informative — no bucket
                        was profitable; the highest-score bucket
                        (1.11–85.68) hit just 36%.

    Why it fails: at 48h-pre-close, market prices ARE the resolution
    direction. A market that wobbled for 30d then sits near $0.30
    isn't mispriced — it's correctly pricing a probable NO outcome.
    The strategy buys the YES and gets paid $0. See
    ``docs/failed-experiments.md`` for the full post-mortem and
    suggestions for what to try instead.

    The behavior (decision tree below) is unchanged from the v1
    implementation; only the docstring is updated to mark the
    experiment as failed.

    ── Original decision tree ──
    Emit a mean-reversion signal when an oscillating market has
    diverged from its mean by ``z_threshold`` standard deviations.

    Decision tree:
        1. Compute oscillation metrics over snap.prices.
        2. Reject if any filter fails (not a clean range-bound market).
        3. Compute the current price's z-score vs the window mean.
        4. If z ≤ -z_threshold → price is "cheap" relative to mean →
           bet YES at (avg_price + edge).
        5. If z ≥ +z_threshold → price is "rich" → bet NO at
           (avg_price - edge), then mapped to NO-side probability.
        6. Otherwise no signal.

    The probability we report is anchored on the *mean*, not the
    current price — that's the price we believe is fair. Edge is a
    small additional buffer so Kelly sizing has something to work
    with even when the divergence is exactly z_threshold.

    ``edge`` is in YES-side units regardless of direction. When we
    short by buying NO, the equivalent YES-probability is
    ``avg_price - edge`` (the YES we think will not happen as often
    as the market is pricing).
    """
    # Lazy import so this module remains importable without backtest
    # (compute_metrics is pure-pandas and useful standalone)
    from pm.research import backtest as bt
    _warn_known_bad_once()

    if snap is None or snap.prices is None or snap.prices.empty:
        return None
    metrics = compute_metrics(
        snap.prices,
        condition_id=getattr(snap, "condition_id", ""),
        question=getattr(snap, "question", ""),
        category=getattr(snap, "category", ""),
        min_history_points=min_history_points,
    )
    if metrics is None:
        return None

    # Filter gates — same shape as scan_markets's filters so this
    # strategy emits signals ONLY on markets the scanner would have
    # surfaced. Keeping the two in lock-step is important: if the
    # scanner's gate changes, the live signal gate changes with it.
    if not (min_avg_price <= metrics.avg_price <= max_avg_price):
        return None
    if metrics.range_score < min_range_score:
        return None
    if metrics.mean_cross_freq < min_cross_freq:
        return None
    if metrics.range_width < min_range_width:
        return None

    # Z-score current price vs the window mean. Use detrended_std as
    # the denominator (not raw std) so a long-running trend on top of
    # oscillation doesn't compress the z-score.
    sigma = metrics.detrended_std
    if sigma <= 1e-9:
        return None
    z = (metrics.current_price - metrics.avg_price) / sigma

    if z <= -z_threshold:
        # Underpriced relative to its own running mean → bet YES at
        # (mean + edge). Capped to [0.01, 0.99] for numerical safety.
        prob = max(0.01, min(0.99, metrics.avg_price + edge))
        return bt.Signal(direction="YES", probability=prob)
    if z >= z_threshold:
        # Overpriced → bet NO. We translate to "YES will not happen
        # as often" → probability = mean − edge.
        prob = max(0.01, min(0.99, metrics.avg_price - edge))
        return bt.Signal(direction="NO", probability=prob)
    return None


def strategy_oscillation_momentum(
    snap: Any,
    *,
    z_threshold: float = 1.0,
    edge: float = 0.05,
    min_range_score: float = 0.5,
    min_cross_freq: float = 0.5,
    min_range_width: float = 0.05,
    min_history_points: int = 20,
    min_avg_price: float = 0.05,
    max_avg_price: float = 0.95,
) -> Any:  # backtest.Signal | None
    """**Inverted variant of ``strategy_oscillation_revert``.** Same
    filters; opposite direction; probability anchored on the *current*
    price (we expect movement to continue), not the historical mean.

    Empirical motivation: when ``strategy_oscillation_revert`` was
    backtested on 90 days of Kalshi settled markets (n=2155, lead=48h,
    fees on), it lost -21.9% ROI at 25% hit rate — the markets it
    bet against were correctly pricing the resolution. Running the
    same scanner-selected trades with the direction *flipped* won
    +17.1% ROI at 62.7% hit rate on the same dataset. The conclusion:
    the oscillation scanner finds real signal, but the signal points
    the OPPOSITE way from mean-reversion. A market that has wobbled
    for 30 days then sits 2σ below its mean is *breaking down*; bet
    WITH the move, not against it.

    **Caveats (do not lose sight of these):**

    - 90-day single backtest window. Could be regime-specific.
    - With max_position_pct=0.05 (realistic live config), ROI drops
      to +8.9% — still positive, still beats market_consensus's
      +2.4%, but the edge is convexity-driven, not forecast-accuracy
      driven (Brier 0.175 vs market baseline 0.079 — worse than
      "just trust the price").
    - **Top-5 winning trades = 229% of total P&L.** Without those 5
      fat-tail wins, the other 59 trades lose $— collectively. This
      is a "win small, lose small, occasionally win big" pattern. If
      the next 90 days don't deliver the fat tails, the strategy
      underperforms.
    - Treat as **paper-trading experiment**, not a tested edge. Real
      conclusion requires another 30+ resolved trades.

    Decision tree:

        1. Compute oscillation metrics on snap.prices (same as revert).
        2. Reject if any filter fails (same gates as revert).
        3. Compute z = (current_price - avg_price) / detrended_std.
        4. If z ≥ +z_threshold (price rich vs mean) →
           bet YES at probability = current_price + edge
           (we expect the up-move to continue to $1).
        5. If z ≤ -z_threshold (price cheap vs mean) →
           bet NO at probability = (1 - current_price) + edge
           (we expect the down-move to continue to $0).
        6. Otherwise no signal.
    """
    from pm.research import backtest as bt

    if snap is None or snap.prices is None or snap.prices.empty:
        return None
    metrics = compute_metrics(
        snap.prices,
        condition_id=getattr(snap, "condition_id", ""),
        question=getattr(snap, "question", ""),
        category=getattr(snap, "category", ""),
        min_history_points=min_history_points,
    )
    if metrics is None:
        return None
    if not (min_avg_price <= metrics.avg_price <= max_avg_price):
        return None
    if metrics.range_score < min_range_score:
        return None
    if metrics.mean_cross_freq < min_cross_freq:
        return None
    if metrics.range_width < min_range_width:
        return None
    sigma = metrics.detrended_std
    if sigma <= 1e-9:
        return None
    z = (metrics.current_price - metrics.avg_price) / sigma

    if z >= z_threshold:
        # Above mean: ride the up-move. Probability claim = current + edge.
        prob = max(0.01, min(0.99, metrics.current_price + edge))
        return bt.Signal(direction="YES", probability=prob)
    if z <= -z_threshold:
        # Below mean: ride the down-move. NO-side probability =
        # (1 - current_yes_price) + edge.
        prob = max(0.01, min(0.99, (1.0 - metrics.current_price) + edge))
        return bt.Signal(direction="NO", probability=prob)
    return None


# Register both strategies. oscillation_revert is KNOWN-BAD (kept as a
# reference). oscillation_momentum is paper-trading (positive backtest,
# fat-tail-dependent). Display names communicate the status so the
# Streamlit Strategy selector + `--strategy` help text make the trust
# level obvious. try/except handles isolated-test imports.
try:
    from pm.research import backtest as _bt
    if "oscillation_revert" not in _bt.STRATEGIES:
        _bt.STRATEGIES["oscillation_revert"] = (
            "⚠️ KNOWN BAD: Oscillation mean-reversion (failed; reference only)",
            strategy_oscillation_revert,
        )
    if "oscillation_momentum" not in _bt.STRATEGIES:
        _bt.STRATEGIES["oscillation_momentum"] = (
            "🧪 PAPER: Oscillation momentum (inverted revert; "
            "+8.9% ROI backtest, fat-tail dependent)",
            strategy_oscillation_momentum,
        )
except ImportError:  # pragma: no cover — only hit in isolated tests
    pass


# One-shot warn flag so the runtime warning fires once per process,
# not once per market scanned.
_OSCILLATION_REVERT_WARNED = False


def _warn_known_bad_once() -> None:
    """Loud one-time log warning the first time this strategy is
    invoked at runtime. Backtest evaluation is the intended use; the
    warning is meant to catch a careless cron line putting it back
    into live paper trading."""
    global _OSCILLATION_REVERT_WARNED
    if _OSCILLATION_REVERT_WARNED:
        return
    _OSCILLATION_REVERT_WARNED = True
    log.warning(
        "strategy_oscillation_revert invoked — this strategy is "
        "KNOWN-BAD per the 90-day Kalshi backtest (Brier 0.169 vs "
        "market baseline 0.079; ROI -21.9%%). It is kept in the "
        "registry only as a reference / known-bad comparison. See "
        "docs/failed-experiments.md.",
    )


def _format_table(df: pd.DataFrame, *, top: int) -> str:
    """Render a CLI-friendly table of the top-N rows."""
    if df.empty:
        return "(no markets scored)"
    cols = [
        "range_score", "mean_cross_freq", "avg_price", "current_price",
        "drift_abs", "detrended_std", "range_width", "n_history",
        "days_covered", "category", "question",
    ]
    view = df.head(top)[cols].copy()
    view["question"] = view["question"].astype(str).str.slice(0, 60)
    return view.to_string(index=True, float_format=lambda v: f"{v:.4f}")


def main(argv: list[str] | None = None) -> int:
    """CLI: scan the active provider's top markets and print a ranked table."""
    p = argparse.ArgumentParser(
        description="Rank active markets by oscillation behavior. Higher "
                    "range_score = price oscillates more than it trends.",
    )
    p.add_argument("--days", type=int, default=30,
                   help="Lookback window in days (default 30).")
    p.add_argument("--fidelity", type=int, default=60,
                   help="Price-history bar size in minutes (default 60).")
    p.add_argument("--max-markets", type=int, default=200,
                   help="Active markets to scan (default 200).")
    p.add_argument("--top", type=int, default=20,
                   help="Top N to print (default 20).")
    p.add_argument("--min-avg-price", type=float, default=0.05)
    p.add_argument("--max-avg-price", type=float, default=0.95)
    p.add_argument("--min-history-points", type=int, default=20)
    p.add_argument("--min-cross-freq", type=float, default=0.3,
                   help="Minimum mean-crossings per day to be considered "
                        "actually range-bound (default 0.3 = roughly once "
                        "every 3 days). Filters out jittery cheap markets "
                        "with high range_score but no real oscillation.")
    p.add_argument("--min-range-width", type=float, default=0.05,
                   help="Minimum max-min spread in the window (default 0.05 "
                        "= 5pp). A market that wobbles between $0.04 and "
                        "$0.05 isn't tradeable, even if it 'oscillates'.")
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    logctx.setup(args.log_level.upper())

    # Local import to avoid pulling in the provider module at file load
    # (the doctest exercises compute_metrics with no provider).
    from pm.venue import provider as provider_mod
    provider = provider_mod.get_provider()
    df_markets = provider.get_markets(limit=args.max_markets, active=True)
    if df_markets is None or df_markets.empty:
        print("No active markets returned.", file=sys.stderr)
        return 1

    def _progress(i: int, total: int) -> None:
        if i % 25 == 0 or i == total:
            print(f"\rscanning {i}/{total}…", end="", file=sys.stderr, flush=True)

    df = scan_markets(
        provider, df_markets,
        days=args.days, fidelity=args.fidelity,
        max_markets=args.max_markets,
        min_history_points=args.min_history_points,
        min_avg_price=args.min_avg_price,
        max_avg_price=args.max_avg_price,
        min_cross_freq=args.min_cross_freq,
        min_range_width=args.min_range_width,
        progress_callback=_progress,
    )
    print("", file=sys.stderr)  # newline after progress line
    print(_format_table(df, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
