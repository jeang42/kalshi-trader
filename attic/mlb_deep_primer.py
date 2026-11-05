"""Pre-game MLB deep-analysis primer for the close-game scalper.

The close-game scalp (``longshot_scalper.py --mode close_game
--forecast-source deep``) gates on a gemma3:27b deep-analysis forecast per
game. But ``bulk_deep_analysis`` never covers MLB games: ``KXMLBGAME``
markets don't appear in the first ~1000 closing-soon markets (the
``/markets`` ordering buries them), so the deep log has zero MLB coverage.
This runner fills that gap.

Two-stage design so we don't waste the slow LLM on lopsided games the
scalper can't use:

1. **Discover** today's MLB games via the ``series_ticker=KXMLBGAME`` events
   path (the only way MLB games surface), and build context rows with
   ``kalshi_client.lookup_markets_by_id``.
2. **Pythagorean pre-filter** (instant): keep only games whose model win
   prob is in ``[prefilter_lo, prefilter_hi]`` — coin-flip-ish, a touch
   wider than the scalper's 0.40–0.60 gate so the LLM can still pull a
   borderline game in.
3. **Deep-analyze** both sides of each surviving game with gemma3:27b and
   log a YES forecast to the ``agentic_deep`` profile tagged
   ``llm_deep:gemma3:27b`` — exactly what ``longshot_scalper.
   _latest_deep_forecast`` reads.

Forecast-only; no paper trades. Run pre-game (cron, a few hours before
first pitch); the 24h deep-analysis cache + a recent-forecast skip make
re-runs cheap. Once this is running, switch the close-game service to
``--forecast-source deep``.

Usage::

    python mlb_deep_primer.py                       # gemma3:27b, today's slate
    python mlb_deep_primer.py --model gemma3:27b --max-days 1.5
    python mlb_deep_primer.py --prefilter-lo 0.35 --prefilter-hi 0.65
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

import pandas as pd

import bulk_deep_analysis as bda  # reuse _recent_forecast_exists
import calibration as cal
import llm_deep_analysis as lda
import profiles
import provider as mp

log = logging.getLogger("mlb_deep_primer")


def _event_of(ticker: str) -> str:
    """Event ticker = market ticker minus the team suffix.
    ``KXMLBGAME-26MAY282005HOUTEX-HOU`` → ``KXMLBGAME-26MAY282005HOUTEX``."""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def select_priming_tickers(
    df: pd.DataFrame,
    forecast_fn: Callable[[str], float | None],
    *,
    prefilter_lo: float,
    prefilter_hi: float,
) -> list[str]:
    """Return the tickers (both sides) of games that look like coin flips.

    Groups ``df``'s ``condition_id``s by event, asks ``forecast_fn`` for one
    side's win prob per game (one call per game — the band is symmetric
    about 0.5, so either side decides closeness), and keeps both sides when
    that prob is in ``[prefilter_lo, prefilter_hi]``. Games with no forecast
    are skipped. Pure — the LLM/IO lives in the caller."""
    if df is None or df.empty or "condition_id" not in df.columns:
        return []
    by_event: dict[str, list[str]] = {}
    for t in df["condition_id"].astype(str):
        by_event.setdefault(_event_of(t), []).append(t)
    out: list[str] = []
    for tickers in by_event.values():
        prob: float | None = None
        for t in tickers:
            prob = forecast_fn(t)
            if prob is not None:
                break
        if prob is None:
            continue
        if prefilter_lo <= prob <= prefilter_hi:
            out.extend(tickers)
    return out


def _todays_mlb_markets(*, max_days: float) -> pd.DataFrame:
    """Discover today's MLB game markets (both sides) closing within
    ``max_days``, as a normalized DataFrame for ``analyze_market`` context.

    MLB games only surface via the events path, so we enumerate open
    ``KXMLBGAME`` events and expand each via ``lookup_markets_by_id``."""
    import kalshi_client as kc
    from mlb_book_poller import _open_mlb_event_tickers
    events = _open_mlb_event_tickers()
    frames: list[pd.DataFrame] = []
    for et in events:
        try:
            sub = kc.lookup_markets_by_id(et)
        except Exception as e:  # noqa: BLE001
            log.debug("lookup_markets_by_id(%s) failed: %s", et, e)
            continue
        if sub is not None and not sub.empty:
            frames.append(sub)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df[df["condition_id"].astype(str).str.startswith("KXMLBGAME")]
    df = df.drop_duplicates("condition_id")
    if "days_to_resolution" in df.columns:
        df = df[df["days_to_resolution"].fillna(99.0) <= max_days]
    return df.reset_index(drop=True)


def _pythag_forecast(ticker: str) -> float | None:
    """P(YES) for a ticker from the baseball Pythagorean model, or None."""
    try:
        import baseball
        r = baseball.predict_for_ticker(ticker)
        return float(r["yes_prob"]) if r else None
    except Exception:  # noqa: BLE001
        return None


def run_cycle(
    *,
    model: str = "gemma3:27b",
    profile: str = "agentic_deep",
    max_days: float = 1.5,
    prefilter_lo: float = 0.35,
    prefilter_hi: float = 0.65,
    include_web_context: bool = False,
    skip_age_hours: float = 12.0,
    forecast_fn: Callable[[str], float | None] | None = None,
) -> dict[str, Any]:
    """One priming pass: discover → Pythagorean pre-filter → gemma3 deep →
    log to ``profile``. Save/restores the active profile."""
    forecast_fn = forecast_fn or _pythag_forecast
    profiles.initialize_profiles()
    saved = profiles.active_profile()
    if profile not in profiles.list_profiles():
        profiles.create_profile(profile)
    profiles.set_active_profile(profile)

    stats = {"model": model, "games_found": 0, "games_close": 0,
             "considered": 0, "analyzed": 0, "logged": 0,
             "skipped_recent": 0, "skipped_failed": 0}
    source = f"llm_deep:{model}"
    try:
        df = _todays_mlb_markets(max_days=max_days)
        if df.empty:
            log.info("no MLB game markets in the next %.1f days", max_days)
            return stats
        stats["games_found"] = df["condition_id"].astype(str).map(_event_of).nunique()

        tickers = select_priming_tickers(
            df, forecast_fn, prefilter_lo=prefilter_lo, prefilter_hi=prefilter_hi,
        )
        stats["games_close"] = len({_event_of(t) for t in tickers})
        stats["considered"] = len(tickers)
        log.info("found %d games, %d coin-flip-ish (%d sides to prime)",
                 stats["games_found"], stats["games_close"], len(tickers))
        if not tickers:
            return stats

        provider = mp.get_provider()
        ticker_set = set(tickers)
        df_ctx = df[df["condition_id"].astype(str).isin(ticker_set)].reset_index(drop=True)

        for ticker in tickers:
            if bda._recent_forecast_exists(ticker, source, skip_age_hours):
                stats["skipped_recent"] += 1
                continue
            row = df_ctx[df_ctx["condition_id"].astype(str) == ticker]
            question = str(row.iloc[0]["question"]) if not row.empty else ticker
            prices = (row.iloc[0].get("outcome_prices") or []) if not row.empty else []
            yes_mid = float(prices[0]) if prices and prices[0] is not None else 0.5

            t0 = time.time()
            try:
                result = lda.analyze_market(
                    provider=provider, df_markets=df_ctx, ticker=ticker,
                    model=model, include_web_context=include_web_context,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("analyze_market(%s) raised: %s", ticker, e)
                stats["skipped_failed"] += 1
                continue
            if result is None:
                stats["skipped_failed"] += 1
                continue
            stats["analyzed"] += 1
            try:
                cal.add_forecast(
                    question=question, condition_id=ticker, outcome="Yes",
                    market_price=yes_mid,
                    your_probability=float(result.probability),
                    source=source, notes=(result.reasoning or "")[:500],
                )
                stats["logged"] += 1
                log.info("primed %s yes=%.3f (mkt=%.3f) %.1fs",
                         ticker, result.probability, yes_mid, time.time() - t0)
            except Exception as e:  # noqa: BLE001
                log.warning("add_forecast(%s) failed: %s", ticker, e)
                stats["skipped_failed"] += 1
    finally:
        if saved and saved != profiles.active_profile():
            try:
                profiles.set_active_profile(saved)
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't restore active profile: %s", e)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="gemma3:27b")
    ap.add_argument("--profile", default="agentic_deep",
                    help="Profile to log forecasts under (default agentic_deep "
                         "— what the close-game scalper reads).")
    ap.add_argument("--max-days", type=float, default=1.5,
                    help="Only prime games closing within this many days "
                         "(default 1.5 = today's slate).")
    ap.add_argument("--prefilter-lo", type=float, default=0.35)
    ap.add_argument("--prefilter-hi", type=float, default=0.65,
                    help="Pythagorean win-prob band for the pre-filter; only "
                         "games in [lo,hi] get the (slow) deep analysis. A bit "
                         "wider than the scalper's 0.40-0.60 gate.")
    ap.add_argument("--web-context", action="store_true",
                    help="Add Google News + Wikipedia context (off by default "
                         "— marginal for game outcomes, adds latency).")
    ap.add_argument("--skip-age-hours", type=float, default=12.0)
    ap.add_argument("--http-timeout", type=float, default=180.0)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    import llm_data
    llm_data.HTTP_TIMEOUT = float(args.http_timeout)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("mlb_deep_primer starting (model=%s profile=%s max_days=%.1f "
             "prefilter=[%.2f,%.2f])", args.model, args.profile, args.max_days,
             args.prefilter_lo, args.prefilter_hi)

    t0 = time.time()
    try:
        stats = run_cycle(
            model=args.model, profile=args.profile, max_days=args.max_days,
            prefilter_lo=args.prefilter_lo, prefilter_hi=args.prefilter_hi,
            include_web_context=args.web_context, skip_age_hours=args.skip_age_hours,
        )
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        return 0
    except Exception as e:  # noqa: BLE001
        log.error("cycle failed: %s", e, exc_info=True)
        return 1
    log.info("done in %.1fs: %s", time.time() - t0, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
