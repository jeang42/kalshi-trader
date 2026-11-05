"""Retrospective evaluation of the oscillation scanner + strategy.

The scanner ranks live markets by how much they oscillate. The strategy
``strategy_oscillation_revert`` bets against z-score deviations from
the running mean on those ranked markets. The question this module
answers: **does it actually work on history?**

Approach (lookahead-safe end to end):

1. Pull closed markets (via the active provider) — these have known
   resolutions.
2. ``backtest.build_dataset`` builds (snapshot, resolution) pairs with
   the snapshot's price history truncated strictly before ``as_of``
   (the strategy can't peek at the outcome).
3. For each snapshot, compute oscillation metrics on its truncated
   price history. The same metrics the live scanner would have
   produced at that moment.
4. Run the registered ``strategy_oscillation_revert`` over the dataset
   using ``backtest.run_backtest`` — gives a standard
   ``BacktestResult`` (hit rate, P&L, Brier, Sharpe).
5. Compare against ``strategy_market_consensus`` on the same dataset —
   the "just follow the favorite" baseline.
6. Bucket by ``range_score`` quintile and report per-bucket
   strategy performance — does the top bucket of bouncy markets
   actually mean-revert better than the bottom bucket?

The bucketing is the killer diagnostic. If the strategy's hit rate
is uniform across quintiles, the scanner isn't adding information.
If hit rate climbs with range_score, the scanner is genuinely
identifying tradeable mean-reversion.

Public surface:

    @dataclass OscillationBacktestResult
    run_oscillation_backtest(provider, *, max_markets=200,
                              lead_hours=72, history_days=30,
                              bankroll=1000.0, kelly_multiplier=0.5,
                              half_spread=0.005,
                              strategy_kwargs=None, n_buckets=5,
                              progress_callback=None)

CLI: ``python -m oscillation_backtest --max-markets 200 --lead-hours 72``.

Cost: one HTTP call per market for price history (via ``build_dataset``);
all other work is in-memory. Cache the dataset if you're iterating on
strategy parameters.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pm import REPO_ROOT
from pm.core import logctx
from pm.research import backtest as bt
from pm.research import oscillation_scanner as osc
from pm.venue import kalshi_fees

log = logging.getLogger(__name__)


@dataclass
class BucketStats:
    """Per-bucket roll-up of strategy performance + scanner metrics.

    ``bucket_index`` is 0-indexed where 0 is the LOWEST range_score
    bucket and ``n_buckets-1`` is the HIGHEST (the bouncy bucket the
    scanner would prioritize). Mean-reversion should be most profitable
    in the highest bucket if the scanner's hypothesis is correct.
    """

    bucket_index: int
    range_score_min: float
    range_score_max: float
    n_markets: int
    n_strategy_fired: int
    hit_rate: float | None
    total_pnl: float
    brier_score: float | None
    avg_mean_cross_freq: float
    avg_range_width: float


@dataclass
class OscillationBacktestResult:
    """Bundle of everything needed to evaluate the scanner+strategy.

    ``strategy`` is the standard ``BacktestResult`` for
    ``strategy_oscillation_revert`` over the whole dataset.
    ``baseline_consensus`` is the same dataset run through
    ``strategy_market_consensus`` — the no-skill reference for
    "did mean-reversion add value?".

    ``by_range_score_bucket`` answers "does range_score correlate
    with strategy success?". If hit_rate / pnl climbs with
    bucket_index, the scanner is informative. If it's flat or
    inverted, the scanner needs new metrics.
    """

    n_dataset: int
    n_oscillation_qualified: int  # passed the scanner's filter at as_of
    strategy: bt.BacktestResult
    baseline_consensus: bt.BacktestResult
    by_range_score_bucket: list[BucketStats] = field(default_factory=list)
    market_baseline_brier: float | None = None
    market_baseline_n: int = 0


def _compute_metrics_for_dataset(
    dataset: list[tuple[bt.MarketSnapshot, bt.Resolution]],
    *,
    min_history_points: int = 20,
) -> dict[str, osc.OscillationMetrics | None]:
    """Compute ``OscillationMetrics`` for every snapshot in the dataset.

    Returns a dict keyed by condition_id. None values mean the snapshot
    didn't have enough history to score — those markets are excluded
    from bucket analysis.
    """
    out: dict[str, osc.OscillationMetrics | None] = {}
    for snap, _ in dataset:
        out[snap.condition_id] = osc.compute_metrics(
            snap.prices,
            condition_id=snap.condition_id,
            question=snap.question,
            category=snap.category,
            min_history_points=min_history_points,
        )
    return out


def _quintile_buckets(
    scored: list[tuple[str, osc.OscillationMetrics]],
    *, n_buckets: int = 5,
) -> list[list[str]]:
    """Partition scored markets into ``n_buckets`` equal-population
    buckets by range_score (lowest first). Returns a list of lists of
    condition_ids."""
    if not scored:
        return [[] for _ in range(n_buckets)]
    sorted_by_score = sorted(scored, key=lambda x: x[1].range_score)
    # Equal-sized buckets via numpy splits; the last bucket absorbs
    # any remainder so we don't lose markets.
    chunks = np.array_split(np.arange(len(sorted_by_score)), n_buckets)
    return [
        [sorted_by_score[i][0] for i in chunk]
        for chunk in chunks
    ]


def _bucket_stats(
    bucket_index: int,
    cids_in_bucket: set[str],
    metrics: dict[str, osc.OscillationMetrics | None],
    strategy_trades: pd.DataFrame,
    dataset: list[tuple[bt.MarketSnapshot, bt.Resolution]],
) -> BucketStats:
    """Roll up a single bucket — N markets, scanner means, and the
    intersection of the strategy's trade log with this bucket."""
    in_bucket_metrics = [
        metrics[cid] for cid in cids_in_bucket
        if metrics.get(cid) is not None
    ]
    if not in_bucket_metrics:
        return BucketStats(
            bucket_index=bucket_index, range_score_min=0.0,
            range_score_max=0.0, n_markets=0,
            n_strategy_fired=0, hit_rate=None, total_pnl=0.0,
            brier_score=None, avg_mean_cross_freq=0.0,
            avg_range_width=0.0,
        )

    if strategy_trades is None or strategy_trades.empty:
        trades_in_bucket = pd.DataFrame()
    else:
        trades_in_bucket = strategy_trades[
            strategy_trades["condition_id"].isin(cids_in_bucket)
        ]
    n_fired = len(trades_in_bucket)
    if n_fired > 0:
        wins = int((trades_in_bucket["pnl"] > 0).sum())
        hit_rate = wins / n_fired
        total_pnl = float(trades_in_bucket["pnl"].sum())
    else:
        hit_rate = None
        total_pnl = 0.0

    # Brier on the trades in this bucket — apples-to-apples comparison
    # with the strategy's own resolved-vs-predicted prob.
    if not trades_in_bucket.empty and "probability" in trades_in_bucket.columns:
        # Need resolution per trade: join via condition_id
        resolutions = {
            snap.condition_id: res.resolved_yes for snap, res in dataset
        }
        terms = []
        for _, row in trades_in_bucket.iterrows():
            cid = row["condition_id"]
            if cid not in resolutions:
                continue
            actual = 1.0 if resolutions[cid] else 0.0
            prob = float(row["probability"])
            # The trade's `probability` is the side-aligned probability
            # — already what the strategy predicted for the direction
            # taken. Resolution maps to actual based on direction:
            # YES direction → actual=resolved_yes; NO → 1-resolved_yes.
            if str(row["direction"]).upper() == "NO":
                actual = 1.0 - actual
            terms.append((prob - actual) ** 2)
        brier = float(np.mean(terms)) if terms else None
    else:
        brier = None

    return BucketStats(
        bucket_index=bucket_index,
        range_score_min=float(min(m.range_score for m in in_bucket_metrics)),
        range_score_max=float(max(m.range_score for m in in_bucket_metrics)),
        n_markets=len(in_bucket_metrics),
        n_strategy_fired=n_fired,
        hit_rate=hit_rate,
        total_pnl=total_pnl,
        brier_score=brier,
        avg_mean_cross_freq=float(
            np.mean([m.mean_cross_freq for m in in_bucket_metrics]),
        ),
        avg_range_width=float(
            np.mean([m.range_width for m in in_bucket_metrics]),
        ),
    )


def _cache_path_for(
    *, venue: str, since_days: float, until_days: float,
    lead_hours: float, history_days: int, fidelity: int,
) -> Path:
    """Per-parameter cache file. The dataset is the slow part of the
    backtest (one HTTP call per market); separating the fetch from
    the evaluation lets you iterate on strategy parameters without
    re-fetching."""
    fname = (
        f"oscillation_{venue}_since{since_days:g}_until{until_days:g}"
        f"_lead{lead_hours:g}_hist{history_days}_fid{fidelity}.pkl"
    )
    # REPO_ROOT, not __file__ — gotcha 1b (the module moved under pm/ in R-24)
    return REPO_ROOT / "data" / "shared" / "backtest_cache" / fname


def _load_cached_dataset(path: Path) -> list | None:
    if not path.exists():
        return None
    try:
        import pickle
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        log.warning("dataset cache read failed (%s); refetching", e)
        return None


def _save_cached_dataset(path, dataset) -> None:
    try:
        import pickle
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(dataset, f)
    except Exception as e:
        log.warning("dataset cache write failed (%s); continuing", e)


def run_oscillation_backtest(
    provider: Any,
    *,
    max_markets: int = 200,
    since_days: float | None = None,
    until_days: float = 0.0,
    lead_hours: float = 72,
    history_days: int = 30,
    fidelity: int = 60,
    bankroll: float = 1000.0,
    kelly_multiplier: float = 0.5,
    half_spread: float = 0.005,
    strategy_kwargs: dict[str, Any] | None = None,
    n_buckets: int = 5,
    max_workers: int = 1,
    min_volume: float = 0.0,
    use_cache: bool = True,
    use_kalshi_fees: bool = True,
    progress_callback: Callable[[int, int], None] | None = None,
) -> OscillationBacktestResult:
    """Run the full scanner-backtest evaluation against historical
    closed markets.

    Two fetch modes:

    1. **Latest N** (default — when ``since_days`` is None):
       ``provider.get_closed_markets(limit=max_markets)`` — small/fast
       sample for iterating on strategy params.

    2. **Date-range** (when ``since_days`` is set):
       ``provider.get_closed_markets_in_range(since_days, until_days,
       max_items=max_markets)`` — bigger, slower, statistically
       meaningful sample. Use this for the "load down historical to
       predict future" path.

    ``lead_hours`` controls how far before resolution the snapshot is
    taken. 72h gives enough runway to actually trade.

    ``max_workers > 1`` parallelizes the price-history fetch (8 is a
    reasonable upper bound for Kalshi — beyond that you mostly buy
    yourself 429s).

    ``use_cache`` writes the built dataset to
    ``data/shared/backtest_cache/`` so re-running with different
    strategy params doesn't refetch. Cache key includes all fetch
    params (since/until/lead/history/fidelity/venue); strategy params
    don't invalidate.
    """
    if since_days is not None:
        df_closed = provider.get_closed_markets_in_range(
            since_days=since_days, until_days=until_days,
            max_items=max_markets, min_volume=min_volume,
        )
        log.info(
            "fetched %d finalized markets in window [-%.1fd, -%.1fd]",
            len(df_closed) if df_closed is not None else 0,
            since_days, until_days,
        )
    else:
        df_closed = provider.get_closed_markets(limit=max_markets)
        log.info("fetched %d recent closed markets",
                 len(df_closed) if df_closed is not None else 0)
    if df_closed is None or df_closed.empty:
        log.warning("no closed markets returned by provider")
        return OscillationBacktestResult(
            n_dataset=0, n_oscillation_qualified=0,
            strategy=_empty_result("oscillation_revert"),
            baseline_consensus=_empty_result("market_consensus"),
        )

    # Dataset cache — only when a date range was specified (otherwise
    # "latest N" is moving target anyway).
    cache_path = None
    dataset: list[tuple[bt.MarketSnapshot, bt.Resolution]] | None = None
    if use_cache and since_days is not None:
        venue_name = getattr(provider, "name", "unknown")
        cache_path = _cache_path_for(
            venue=venue_name, since_days=since_days,
            until_days=until_days, lead_hours=lead_hours,
            history_days=history_days, fidelity=fidelity,
        )
        dataset = _load_cached_dataset(cache_path)
        if dataset is not None:
            log.info("loaded dataset from cache: %s (%d pairs)",
                     cache_path.name, len(dataset))

    if dataset is None:
        log.info("building dataset: %d closed markets, lead_hours=%.1f, "
                 "max_workers=%d",
                 len(df_closed), lead_hours, max_workers)
        dataset = bt.build_dataset(
            provider, df_closed,
            lead_hours=lead_hours, history_days=history_days,
            fidelity=fidelity, max_markets=max_markets,
            max_workers=max_workers,
            progress_callback=progress_callback,
        )
        if cache_path is not None and dataset:
            _save_cached_dataset(cache_path, dataset)
            log.info("cached dataset to %s", cache_path.name)
    if not dataset:
        log.warning("dataset is empty — no eligible markets")
        return OscillationBacktestResult(
            n_dataset=0, n_oscillation_qualified=0,
            strategy=_empty_result("oscillation_revert"),
            baseline_consensus=_empty_result("market_consensus"),
        )

    log.info("dataset built: %d (snapshot, resolution) pairs", len(dataset))

    # Precompute metrics on each snapshot's truncated history
    metrics_by_cid = _compute_metrics_for_dataset(dataset)
    n_qualified = sum(1 for m in metrics_by_cid.values() if m is not None)
    log.info("oscillation metrics computed: %d/%d markets had enough history",
             n_qualified, len(dataset))

    # Run the strategy + consensus baseline on the same dataset
    strategy_kwargs = strategy_kwargs or {}
    if strategy_kwargs:
        # Wrap the registered strategy with the kwargs bound
        from functools import partial
        wrapped = partial(osc.strategy_oscillation_revert, **strategy_kwargs)
    else:
        wrapped = osc.strategy_oscillation_revert

    # Fee model: when use_kalshi_fees is on, wire kalshi_fees.round_trip_fees
    # so every backtest trade pays venue-accurate fees on entry and exit
    # (held-to-resolution exits land at $1 or $0 where Kalshi's fee is
    # zero — but the entry fee still bites). Net P&L will be lower than
    # the no-fee run; that's the point.
    fee_fn: Callable[[float, float, float, str], float] | None = None
    if use_kalshi_fees:
        def fee_fn(entry_p: float, exit_p: float, contracts: float,
                   ticker: str) -> float:
            return kalshi_fees.round_trip_fees(
                entry_price=entry_p, exit_price=exit_p,
                contracts=contracts, ticker=ticker,
            )

    strategy_result = bt.run_backtest(
        wrapped, dataset, bankroll=bankroll,
        kelly_multiplier=kelly_multiplier, half_spread=half_spread,
        fee_fn=fee_fn,
        strategy_name="oscillation_revert",
    )
    consensus_result = bt.run_backtest(
        bt.strategy_market_consensus, dataset,
        bankroll=bankroll, kelly_multiplier=kelly_multiplier,
        half_spread=half_spread,
        fee_fn=fee_fn,
        strategy_name="market_consensus",
    )
    market_brier, market_n = bt.market_baseline_brier(dataset)

    # Bucket the qualifying markets by range_score
    qualified_pairs = [
        (cid, m) for cid, m in metrics_by_cid.items() if m is not None
    ]
    buckets = _quintile_buckets(qualified_pairs, n_buckets=n_buckets)
    # strategy_result.trades is already a DataFrame (possibly empty)
    strategy_trades_df = strategy_result.trades
    bucket_stats: list[BucketStats] = []
    for i, cids in enumerate(buckets):
        stats = _bucket_stats(
            bucket_index=i,
            cids_in_bucket=set(cids),
            metrics=metrics_by_cid,
            strategy_trades=strategy_trades_df,
            dataset=dataset,
        )
        bucket_stats.append(stats)

    return OscillationBacktestResult(
        n_dataset=len(dataset),
        n_oscillation_qualified=n_qualified,
        strategy=strategy_result,
        baseline_consensus=consensus_result,
        by_range_score_bucket=bucket_stats,
        market_baseline_brier=market_brier,
        market_baseline_n=market_n,
    )


def _empty_result(name: str) -> bt.BacktestResult:
    """A zero-trade BacktestResult — used when the dataset is empty so
    callers don't have to special-case None."""
    return bt.BacktestResult(
        strategy_name=name, n_markets_evaluated=0, n_trades=0,
        n_wins=0, hit_rate=None, hit_rate_ci=None,
        total_stake=0.0, total_pnl=0.0, roi=None,
        mean_pnl_per_trade=None, pnl_std=None,
        sharpe_per_trade=None, max_drawdown=0.0,
        brier_score=None, half_spread=0.0, fee_pct=0.0,
        total_fees=0.0, implied_spread_cost=0.0,
        portfolio_constrained=False, min_cash_seen=0.0,
        n_clipped_for_cash=0, n_skipped_for_cash=0,
        trades=pd.DataFrame(),
        equity_curve=pd.DataFrame(),
        by_category={},
        warnings=["empty dataset"],
    )


def format_report(result: OscillationBacktestResult) -> str:
    """Render a human-readable report of an OscillationBacktestResult.

    Used by the CLI; useful in notebooks / page-side display too.
    """
    lines: list[str] = []
    lines.append("═" * 72)
    lines.append("Oscillation scanner backtest")
    lines.append("═" * 72)
    lines.append(f"Dataset: {result.n_dataset} markets "
                 f"({result.n_oscillation_qualified} had enough history "
                 f"to score)")
    if result.market_baseline_brier is not None:
        lines.append(
            f"Market-baseline Brier (just-trust-current-price): "
            f"{result.market_baseline_brier:.4f} "
            f"on n={result.market_baseline_n}",
        )
    lines.append("")
    lines.append("─" * 72)
    lines.append("Strategy: oscillation_revert")
    lines.append("─" * 72)
    s = result.strategy
    lines.append(f"  Trades fired       : {s.n_trades}")
    lines.append(f"  Hit rate           : "
                 f"{'%.1f%%' % (s.hit_rate * 100) if s.hit_rate is not None else 'n/a'}")
    lines.append(f"  Total P&L          : ${s.total_pnl:+,.2f}")
    roi_str = (f"{s.roi * 100:+.1f}%" if s.roi is not None else "n/a")
    lines.append(f"  ROI                : {roi_str}")
    if s.brier_score is not None:
        lines.append(f"  Strategy Brier     : {s.brier_score:.4f}")
    lines.append("")
    lines.append("─" * 72)
    lines.append("Baseline: market_consensus (follow the favorite)")
    lines.append("─" * 72)
    b = result.baseline_consensus
    lines.append(f"  Trades fired       : {b.n_trades}")
    lines.append(f"  Hit rate           : "
                 f"{'%.1f%%' % (b.hit_rate * 100) if b.hit_rate is not None else 'n/a'}")
    lines.append(f"  Total P&L          : ${b.total_pnl:+,.2f}")
    b_roi = (f"{b.roi * 100:+.1f}%" if b.roi is not None else "n/a")
    lines.append(f"  ROI                : {b_roi}")
    lines.append("")
    lines.append("─" * 72)
    lines.append("Per-bucket by range_score (lowest → highest oscillation)")
    lines.append("─" * 72)
    lines.append(f"  {'Bkt':>3} {'Score range':>14}  {'N mkts':>6} "
                 f"{'Fired':>6} {'Hit%':>6} {'P&L':>10} {'cross/d':>8} "
                 f"{'range_w':>8}")
    for st in result.by_range_score_bucket:
        rs_range = f"{st.range_score_min:.2f}-{st.range_score_max:.2f}"
        hit = (f"{st.hit_rate * 100:.0f}%"
               if st.hit_rate is not None else "n/a")
        lines.append(
            f"  {st.bucket_index:>3} {rs_range:>14}  {st.n_markets:>6} "
            f"{st.n_strategy_fired:>6} {hit:>6} "
            f"${st.total_pnl:>+8.2f} {st.avg_mean_cross_freq:>7.2f} "
            f"{st.avg_range_width:>7.3f}",
        )
    lines.append("")
    lines.append("Reading: if hit_rate / P&L climb monotonically with "
                 "bucket index,\nthe scanner's range_score is "
                 "informative. Flat/inverted → not informative.")
    lines.append("═" * 72)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: run the scanner backtest against the active provider."""
    p = argparse.ArgumentParser(
        description="Retrospective evaluation of oscillation_scanner + "
                    "strategy_oscillation_revert on historical closed "
                    "markets.",
    )
    p.add_argument("--max-markets", type=int, default=200,
                   help="Cap on markets to fetch. With --since-days, "
                        "this is a hard ceiling on the date-range scan; "
                        "without it, just `limit=N` on the provider.")
    p.add_argument("--since-days", type=float, default=None,
                   help="Pull finalized markets from this many days ago "
                        "until --until-days. Required for big historical "
                        "samples (default 'latest N' mode is fast but "
                        "tops out around the venue's recent backlog).")
    p.add_argument("--until-days", type=float, default=0.0,
                   help="Window upper bound, in days back from now "
                        "(default 0 = present).")
    p.add_argument("--max-workers", type=int, default=1,
                   help="Parallel workers for the price-history phase. "
                        "8 is a good ceiling for Kalshi (more invites 429s).")
    p.add_argument("--no-cache", action="store_true",
                   help="Bypass the dataset cache (forces refetch).")
    p.add_argument("--no-fees", action="store_true",
                   help="Disable Kalshi fee accounting (run ideal-fill, "
                        "rosy-numbers baseline). Default applies "
                        "kalshi_fees.round_trip_fees to every trade.")
    p.add_argument("--min-volume", type=float, default=100.0,
                   help="Skip markets with lifetime volume below this. "
                        "Default 100 — Kalshi's settled-market list is "
                        "dominated by zero-volume auto-generated parlays "
                        "(KXMVESPORTSMULTIGAMEEXTENDED-*). Set 0 to "
                        "include everything.")
    p.add_argument("--lead-hours", type=float, default=72.0)
    p.add_argument("--history-days", type=int, default=30)
    p.add_argument("--fidelity", type=int, default=60)
    p.add_argument("--bankroll", type=float, default=1000.0)
    p.add_argument("--kelly-multiplier", type=float, default=0.5)
    p.add_argument("--half-spread", type=float, default=0.005)
    p.add_argument("--n-buckets", type=int, default=5)
    p.add_argument("--min-cross-freq", type=float, default=None,
                   help="Override the strategy's min_cross_freq filter. "
                        "Default is the strategy's own (0.5).")
    p.add_argument("--min-range-score", type=float, default=None,
                   help="Override the strategy's min_range_score filter.")
    p.add_argument("--z-threshold", type=float, default=None,
                   help="Override the strategy's z_threshold.")
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup(args.log_level.upper())

    from pm.venue import provider as provider_mod
    provider = provider_mod.get_provider()

    strategy_kwargs: dict[str, Any] = {}
    if args.min_cross_freq is not None:
        strategy_kwargs["min_cross_freq"] = args.min_cross_freq
    if args.min_range_score is not None:
        strategy_kwargs["min_range_score"] = args.min_range_score
    if args.z_threshold is not None:
        strategy_kwargs["z_threshold"] = args.z_threshold

    def _progress(i: int, total: int) -> None:
        if i % 25 == 0 or i == total - 1:
            print(f"\rbuilding dataset {i + 1}/{total}…",
                  end="", file=sys.stderr, flush=True)

    result = run_oscillation_backtest(
        provider,
        max_markets=args.max_markets,
        since_days=args.since_days,
        until_days=args.until_days,
        lead_hours=args.lead_hours,
        history_days=args.history_days,
        fidelity=args.fidelity,
        bankroll=args.bankroll,
        kelly_multiplier=args.kelly_multiplier,
        half_spread=args.half_spread,
        strategy_kwargs=strategy_kwargs,
        n_buckets=args.n_buckets,
        max_workers=args.max_workers,
        min_volume=args.min_volume,
        use_cache=not args.no_cache,
        use_kalshi_fees=not args.no_fees,
        progress_callback=_progress,
    )
    print("", file=sys.stderr)
    print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
