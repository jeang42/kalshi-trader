"""Background order-book poller — captures live `/book` snapshots
across registered providers and writes them to :mod:`book_archive`.

Why a standalone script: Streamlit reruns the page on every interaction
and has no first-class background-process model. A poller has to be
its own thing.

Two modes:

    python book_poller.py
        Run a single polling cycle and exit. Suitable for cron — set
        your interval at the cron level.

    python book_poller.py --loop --interval 300
        Run forever, polling every `--interval` seconds. Suitable for
        systemd / tmux / nohup. Sleeps between cycles; logs each one.

Per-cycle behaviour:
    1. For each registered provider (Polymarket, Kalshi):
       a. Fetch the top-N active markets by volume (configurable via
          --max-markets, default 25).
       b. For each market, fetch its outcome tokens.
       c. For each token, skip if a snapshot was stored within
          --skip-if-recent seconds (default 60); otherwise fetch the
          live book via provider.get_order_book and store.
       4. Log per-provider counts: fetched / skipped / errors.

Defaults are deliberately conservative so a fresh poller doesn't
hammer either venue's API. Tune up only after you've watched a cycle
or two land cleanly.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass

from pm.archive import book_archive
from pm.core import logctx
from pm.venue import polymarket_client as pm
from pm.venue import provider as provider_mod

log = logging.getLogger("book_poller")


@dataclass
class CycleStats:
    """Per-cycle counters surfaced in logs."""

    venue: str
    fetched: int = 0    # snapshots successfully stored this cycle
    skipped: int = 0    # markets/tokens skipped because of has_recent_snapshot
    errors: int = 0     # exceptions from list_outcome_tokens / get_order_book


def _poll_one_provider(
    provider, *, max_markets: int, skip_if_recent_s: int,
) -> CycleStats:
    venue = provider.name
    stats = CycleStats(venue=venue)

    try:
        df = provider.get_markets(limit=max_markets, active=True)
    except Exception as e:
        log.error("%s: get_markets failed: %s", venue, e)
        stats.errors += 1
        return stats

    if df is None or df.empty:
        log.info("%s: 0 active markets returned", venue)
        return stats

    # Collect all snapshots from this provider into one list and flush
    # them in a single transaction at the end. One fsync per cycle is
    # ~75× cheaper on HDD than one fsync per snapshot, and a non-
    # regression on SSD.
    pending: list[dict] = []

    for _, row in df.iterrows():
        market_id = str(row.get("condition_id") or row.get("id") or "")
        if not market_id:
            continue
        try:
            tokens = provider.list_outcome_tokens(market_id)
        except Exception as e:
            log.warning("%s/%s: list_outcome_tokens failed: %s",
                        venue, market_id, e)
            stats.errors += 1
            continue
        for tok in tokens:
            token_id = str(tok.get("token_id") or "")
            if not token_id:
                continue
            if book_archive.has_recent_snapshot(
                venue=venue, market_id=market_id, token_id=token_id,
                within_seconds=skip_if_recent_s,
            ):
                stats.skipped += 1
                continue
            try:
                raw = provider.get_order_book(token_id)
                book_df = pm.parse_order_book(raw)
            except Exception as e:
                log.warning("%s/%s/%s: get_order_book failed: %s",
                            venue, market_id, token_id, e)
                stats.errors += 1
                continue
            pending.append({
                "venue": venue,
                "market_id": market_id,
                "token_id": token_id,
                "book": book_df,
            })

    if pending:
        written = book_archive.store_snapshots(pending)
        stats.fetched = written
        if written != len(pending):
            stats.errors += len(pending) - written
    return stats


def run_cycle(*, max_markets: int, skip_if_recent_s: int) -> list[CycleStats]:
    """Single polling cycle across every registered provider. Returns
    per-venue stats so the caller can log them at INFO."""
    out: list[CycleStats] = []
    for provider in provider_mod.list_providers():
        cs = _poll_one_provider(
            provider, max_markets=max_markets,
            skip_if_recent_s=skip_if_recent_s,
        )
        log.info(
            "%s: fetched=%d skipped=%d errors=%d",
            cs.venue, cs.fetched, cs.skipped, cs.errors,
        )
        out.append(cs)
    return out


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Order-book poller (path 1 — archive the live book).",
    )
    p.add_argument("--loop", action="store_true",
                   help="Loop forever; sleep --interval seconds between cycles.")
    p.add_argument("--interval", type=int, default=300,
                   help="Sleep seconds between cycles in --loop mode (default 300 = 5 min).")
    p.add_argument("--max-markets", type=int, default=25,
                   help="Max active markets per provider per cycle (default 25).")
    p.add_argument("--skip-if-recent", type=int, default=60,
                   help="Skip a (market, token) if it has a snapshot newer "
                        "than this many seconds (default 60).")
    p.add_argument("--log-level", default=os.getenv("POLYMARKET_LOG_LEVEL", "INFO"),
                   help="Logging level: DEBUG, INFO (default), WARNING, ERROR.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup(args.log_level.upper())
    log.info("book_poller starting (loop=%s interval=%ds max_markets=%d "
             "skip_if_recent=%ds)",
             args.loop, args.interval, args.max_markets, args.skip_if_recent)
    log.info("archive path: %s", book_archive._path())

    while True:
        t0 = time.time()
        try:
            run_cycle(max_markets=args.max_markets,
                      skip_if_recent_s=args.skip_if_recent)
        except KeyboardInterrupt:
            log.info("interrupted; exiting")
            return 0
        except Exception as e:
            log.error("cycle failed: %s", e, exc_info=True)
        elapsed = time.time() - t0
        log.info("cycle complete in %.1fs", elapsed)
        if not args.loop:
            return 0
        # Sleep the remainder of the interval (or 1s minimum if the
        # cycle itself exceeded the interval).
        sleep_for = max(1.0, args.interval - elapsed)
        log.debug("sleeping %.0fs", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    raise SystemExit(main())
