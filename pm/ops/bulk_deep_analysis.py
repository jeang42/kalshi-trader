"""Bulk-run Deep Analysis across many markets, one at a time, one model
per invocation. Designed for nightly cron — one cron entry per model
gives a head-to-head dataset of (ticker, model) Brier comparisons on
the same market universe.

Forecast-only by design — no paper-trade writes. The existing
``strategy_runner`` agentic profiles handle simulated-PnL tracking
for the fast strategies. This runner exists specifically to *measure
deep-analysis quality in isolation* without conflating prediction
accuracy with sizing/timing/spread.

Outputs land in the configured ``--profile`` (default ``agentic_deep``)
as calibration-log entries with ``source="llm_deep:<model>"``.
Auto-resolved on the existing ``auto_resolve_runner`` schedule, so
Brier-by-source rolls up across the whole nightly batch as markets
settle.

Cost model — the GPU is free, so the constraint is wall-clock time
to walk a 30-50 market batch on a 32-35B model (~25-40 min per
model per night). Six staggered cron lines (2am-7am) give the same
night's batch across every recommended model. Cache TTL of 24h means
re-runs within a day are no-ops at the LLM-call level.

Usage::

    python bulk_deep_analysis.py --model qwen3.5:35b
    python bulk_deep_analysis.py --model qwen3:32b --max-markets 30
    python bulk_deep_analysis.py --model deepseek-r1:32b --no-web-context
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

from pm import REPO_ROOT

# Load .env early so KALSHI_API_KEY_ID etc. reach the auth/settlements
# overlay when invoked from cron.
try:
    from dotenv import load_dotenv
    # .env lives at the REPO root — Path(__file__).parent stopped meaning that
    # when R-24 moved this module under pm/ (gotcha 1b: a __file__ default
    # captures the module's LOCATION). Loading a nonexistent .env is silent,
    # which is gotcha #4's failure mode: config believed loaded, actually not.
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

import pandas as pd

from pm.core import logctx, profiles
from pm.feeds import llm_deep_analysis as lda
from pm.ledger import calibration as cal
from pm.venue import provider as mp

log = logging.getLogger(__name__)


def _recent_forecast_exists(condition_id: str, source: str,
                              min_age_hours: float) -> bool:
    """Was this (ticker, source) pair already forecasted within the
    last ``min_age_hours``? If so, skip — avoid duplicate rows for the
    same model+market within a single overnight cycle.

    Reads the active profile's forecast log (the runner sets active
    to ``--profile`` before iterating)."""
    df = cal.get_forecasts()
    if df.empty:
        return False
    match = df[(df["condition_id"] == condition_id) & (df["source"] == source)]
    if match.empty:
        return False
    latest = match["timestamp_dt"].max()
    if latest is None:
        return False
    age_hours = (pd.Timestamp.now(tz="UTC") - latest).total_seconds() / 3600
    return age_hours < min_age_hours


def run_cycle(*, model: str, profile: str,
               max_markets: int, max_days: int,
               include_web_context: bool,
               skip_age_hours: float) -> dict:
    """One bulk-analysis pass.

    Walks the closing-soon market universe (capped at ``max_markets``),
    runs ``analyze_market`` on each, logs a YES-side forecast to
    calibration tagged ``llm_deep:<model>``. Skips markets already
    analyzed by this model within ``skip_age_hours``.

    Returns a stats dict with counts. Save/restores the active profile.
    """
    profiles.initialize_profiles()
    saved = profiles.active_profile()
    if profile not in profiles.list_profiles():
        profiles.create_profile(profile)
        log.info("created profile %r", profile)
    profiles.set_active_profile(profile)

    # Counters kept separate from the string labels: a single dict mixing
    # str and int values types every value as `object`, so `+= 1` on a
    # counter could not be checked.
    counts: dict[str, int] = {
        "considered": 0, "analyzed": 0,
        "skipped_recent": 0, "skipped_failed": 0,
        "logged": 0,
    }
    stats: dict[str, Any] = {"model": model, "profile": profile, **counts}
    source = f"llm_deep:{model}"

    try:
        provider = mp.get_provider()
        try:
            df = provider.get_markets_closing_within(
                float(max_days), limit=max_markets * 2,
            )
        except Exception as e:
            log.error("market fetch failed: %s", e)
            return stats

        if df.empty:
            log.warning("no markets in the closing-soon window")
            return stats

        # Diversify across categories. Without this, sorting purely by
        # liquidity collapses the batch onto Sports (NHL series spreads
        # + MLB games dominate the high-liquidity end). Take up to
        # `per_cat` markets from each category in liquidity-descending
        # order, then fill any remainder from the unallocated pool.
        if "liquidity" in df.columns:
            df = df.sort_values("liquidity", ascending=False)
        if "category" in df.columns:
            categories = [c for c in df["category"].unique()
                          if c and str(c).strip()]
            n_cats = max(1, len(categories))
            per_cat = max(1, max_markets // n_cats)
            picked: list = []
            for cat in categories:
                slice_ = df[df["category"] == cat].head(per_cat)
                picked.append(slice_)
            picked_df = (pd.concat(picked, ignore_index=True)
                          if picked else df.head(0))
            # Fill any remainder from the unallocated tail (still
            # liquidity-descending), skipping already-picked tickers.
            if len(picked_df) < max_markets:
                seen = set(picked_df["condition_id"].astype(str))
                tail = df[~df["condition_id"].astype(str).isin(seen)]
                fill_n = max_markets - len(picked_df)
                picked_df = pd.concat(
                    [picked_df, tail.head(fill_n)], ignore_index=True,
                )
            df = picked_df.head(max_markets).reset_index(drop=True)
        else:
            df = df.head(max_markets).reset_index(drop=True)
        stats["considered"] = len(df)
        if "category" in df.columns:
            cat_counts = df["category"].value_counts().to_dict()
            log.info("category breakdown: %s",
                     ", ".join(f"{c}={n}" for c, n in cat_counts.items()))

        log.info("analyzing %d markets with %s (web=%s)",
                 len(df), model, include_web_context)

        for i, row in df.iterrows():
            ticker = str(row["condition_id"])
            question = str(row["question"])
            prices = row.get("outcome_prices") or []
            if not prices:
                continue
            yes_mid = float(prices[0])

            if _recent_forecast_exists(ticker, source, skip_age_hours):
                stats["skipped_recent"] += 1
                continue

            t0 = time.time()
            try:
                result = lda.analyze_market(
                    provider=provider, df_markets=df,
                    ticker=ticker, model=model,
                    include_web_context=include_web_context,
                )
            except Exception as e:
                log.warning("analyze_market(%s) raised: %s", ticker, e)
                stats["skipped_failed"] += 1
                continue
            elapsed = time.time() - t0

            if result is None:
                stats["skipped_failed"] += 1
                err = lda.last_error()
                log.warning("analyze_market(%s) returned None: %s",
                            ticker, err[:200] if err else "(no detail)")
                continue
            stats["analyzed"] += 1

            try:
                cal.add_forecast(
                    question=question,
                    condition_id=ticker,
                    outcome="Yes",
                    market_price=yes_mid,
                    your_probability=float(result.probability),
                    source=source,
                    notes=(result.reasoning or "")[:500],
                )
                stats["logged"] += 1
                log.info("[%d/%d] %s %s yes=%.3f (model=%.3f) conf=%s %.1fs",
                         i + 1, len(df), ticker, question[:50],
                         yes_mid, result.probability, result.confidence,
                         elapsed)
            except Exception as e:
                log.warning("cal.add_forecast(%s) failed: %s", ticker, e)
                stats["skipped_failed"] += 1

    finally:
        if saved and saved != profiles.active_profile():
            try:
                profiles.set_active_profile(saved)
                log.info("restored active profile to %r", saved)
            except Exception as e:
                log.warning("couldn't restore active profile: %s", e)

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True,
                     help="Ollama model name (e.g. 'qwen3.5:35b').")
    ap.add_argument("--profile", default="agentic_deep",
                     help="Profile to log forecasts under (default: agentic_deep).")
    ap.add_argument("--max-markets", type=int, default=50,
                     help="Max markets to analyze per cycle (default: 50).")
    ap.add_argument("--max-days", type=int, default=30,
                     help="Only analyze markets resolving within this many days "
                          "(default: 30).")
    ap.add_argument("--no-web-context", action="store_true",
                     help="Disable Google News + Wikipedia context. Default is "
                          "ON since the GPU is local-free.")
    ap.add_argument("--skip-age-hours", type=float, default=12.0,
                     help="Skip markets this model already analyzed within "
                          "the last N hours (default: 12).")
    ap.add_argument("--http-timeout", type=float, default=180.0,
                     help="Per-call Ollama timeout in seconds (default: 180 = "
                          "3 min). Bumped above llm_data's 120s default for "
                          "bulk runs on 32-35B models with full context, but "
                          "capped at 3 min — calls slower than that almost "
                          "always indicate the model is in a degenerate loop "
                          "rather than legitimately busy.")
    ap.add_argument("--log-level", default="INFO",
                     help="DEBUG / INFO / WARNING")
    args = ap.parse_args()

    # Bump the Ollama HTTP timeout BEFORE first analyze_market call.
    # llm_data caches HTTP_TIMEOUT as a module attribute on import, so
    # we override it at runtime here.
    from pm.feeds import llm_data
    llm_data.HTTP_TIMEOUT = float(args.http_timeout)

    logctx.setup(args.log_level.upper())

    log.info("bulk_deep_analysis starting (model=%s profile=%s "
             "max_markets=%d max_days=%d web=%s)",
             args.model, args.profile, args.max_markets,
             args.max_days, not args.no_web_context)

    t0 = time.time()
    try:
        stats = run_cycle(
            model=args.model, profile=args.profile,
            max_markets=args.max_markets, max_days=args.max_days,
            include_web_context=not args.no_web_context,
            skip_age_hours=args.skip_age_hours,
        )
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        return 0
    except Exception as e:
        log.error("cycle failed: %s", e, exc_info=True)
        return 1

    elapsed = time.time() - t0
    log.info(
        "cycle complete: considered=%d analyzed=%d logged=%d "
        "skipped(recent)=%d skipped(failed)=%d  %.1fs",
        stats["considered"], stats["analyzed"], stats["logged"],
        stats["skipped_recent"], stats["skipped_failed"], elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
