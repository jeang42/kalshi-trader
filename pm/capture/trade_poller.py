"""Kalshi executed-trades poller + historical backfill.

Captures the public ``markets/trades`` tape into ``trade_archive.sqlite``.
The trade-tape counterpart to ``mlb_book_poller.py`` (which captures the
order book). Two jobs in one runnable:

* **Backfill** (``--backfill --days N``): for every ticker in the universe,
  pull the *entire* trade history over the last ``N`` days into the
  archive. Because ``markets/trades`` is cursor-paginated with min_ts/max_ts
  and unauthenticated, we can reconstruct a *finished* game's full tape
  after the fact — this is what fills the "book_archive is forward-only"
  gap (``baseball/analysis.py``). Idempotent: re-running an overlapping
  window inserts only trades not already seen (``trade_id`` primary key).

* **Incremental poll** (default / ``--loop``): pull only trades newer than
  the latest archived one per ticker. Cheap; a REST complement/fallback to
  the live WebSocket ``trade`` channel in ``kalshi_ws.py``.

Ticker universe:
* Open MLB markets via Kalshi event discovery (shared with mlb_book_poller).
* Optionally (``--include-archived``, on by default for ``--backfill``) every
  ``KXMLBGAME-*`` ticker already in ``book_archive`` — i.e. past games we
  have book data for and want matching trade flow for.

Usage::

    python trade_poller.py --backfill --days 14        # historical sweep
    python trade_poller.py                              # one incremental cycle
    python trade_poller.py --loop --interval 30         # live REST daemon
    python trade_poller.py --ticker KXMLBGAME-...-NYM   # one market
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from typing import Any

from pm.archive import trade_archive
from pm.core import logctx
from pm.venue import kalshi_client

log = logging.getLogger("trade_poller")


# ─── Ticker universe ─────────────────────────────────────────────────────

def _open_mlb_markets() -> list[str]:
    """Currently-open MLB market tickers (both sides per game). Reuses
    mlb_book_poller's event→market discovery so the two pollers stay in
    lock-step on what 'open' means."""
    try:
        from pm.capture.mlb_book_poller import _discover_markets
    except Exception as e:  # noqa: BLE001
        log.warning("could not import market discovery: %s", e)
        return []
    return _discover_markets()


def _archived_mlb_markets() -> list[str]:
    """Every distinct ``KXMLBGAME-*`` ticker already in book_archive —
    games we've snapshotted books for and want matching trade flow for,
    including ones that have since finished."""
    from pm.archive import book_archive
    try:
        with sqlite3.connect(str(book_archive._path())) as conn:
            rows = conn.execute(
                "SELECT DISTINCT market_id FROM book_snapshots "
                "WHERE venue = 'kalshi' AND market_id LIKE 'KXMLBGAME-%'",
            ).fetchall()
        return [str(r[0]) for r in rows]
    except sqlite3.Error as e:
        log.warning("could not read book_archive tickers: %s", e)
        return []


def _ticker_universe(*, include_open: bool, include_archived: bool) -> list[str]:
    seen: dict[str, None] = {}
    if include_open:
        for t in _open_mlb_markets():
            seen[t] = None
    if include_archived:
        for t in _archived_mlb_markets():
            seen[t] = None
    return list(seen)


# ─── Per-ticker fetch ────────────────────────────────────────────────────

def backfill_ticker(ticker: str, *, days: float) -> dict[str, int]:
    """Pull the full trade tape for ``ticker`` over the last ``days`` days
    into the archive. Returns ``{fetched, stored}`` (stored = newly
    inserted, deduped on trade_id)."""
    until = int(time.time())
    since = until - int(days * 86_400)
    df = kalshi_client.get_all_trades(ticker, since_ts=since, until_ts=until)
    fetched = int(len(df))
    stored = trade_archive.store_trades_df(df)
    return {"fetched": fetched, "stored": stored}


def poll_ticker(ticker: str, *, lookback_s: int = 3600) -> dict[str, int]:
    """Incrementally pull trades newer than the latest archived one for
    ``ticker`` (or the last ``lookback_s`` seconds if the archive is empty
    for it). Returns ``{fetched, stored}``."""
    last = trade_archive.latest_trade_ts(ticker)
    since = last if last is not None else int(time.time()) - lookback_s
    df = kalshi_client.get_all_trades(ticker, since_ts=since)
    fetched = int(len(df))
    stored = trade_archive.store_trades_df(df)
    return {"fetched": fetched, "stored": stored}


# ─── Cycle ───────────────────────────────────────────────────────────────

def run_cycle(
    *,
    tickers: list[str],
    backfill_days: float | None = None,
    sleep_ms: int = 200,
    progress_callback=None,
) -> dict[str, Any]:
    """One pass over ``tickers``. ``backfill_days`` set → historical sweep;
    else incremental poll. ``sleep_ms`` throttles between markets to stay
    polite to the API. Returns aggregate stats."""
    stats = {"markets": len(tickers), "fetched": 0, "stored": 0, "errors": 0}
    for i, ticker in enumerate(tickers):
        try:
            if backfill_days is not None:
                r = backfill_ticker(ticker, days=backfill_days)
            else:
                r = poll_ticker(ticker)
            stats["fetched"] += r["fetched"]
            stats["stored"] += r["stored"]
        except Exception as e:  # noqa: BLE001
            log.debug("trade fetch failed for %s: %s", ticker, e)
            stats["errors"] += 1
        if progress_callback is not None:
            try:
                progress_callback(i + 1, len(tickers))
            except Exception:  # noqa: BLE001
                pass
        if sleep_ms > 0 and i < len(tickers) - 1:
            time.sleep(sleep_ms / 1000.0)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true",
                    help="Historical sweep: pull the full trade tape over "
                         "--days for each ticker. Default: incremental poll "
                         "(only trades newer than the archive).")
    ap.add_argument("--days", type=float, default=7.0,
                    help="Backfill lookback in days (default 7). Only used "
                         "with --backfill.")
    ap.add_argument("--ticker", default=None,
                    help="Restrict to a single market ticker.")
    ap.add_argument("--include-archived", action="store_true",
                    help="Include every KXMLBGAME ticker already in "
                         "book_archive (past games), not just currently-open "
                         "markets. Implied by --backfill.")
    ap.add_argument("--no-open", action="store_true",
                    help="Skip the currently-open-markets discovery (use with "
                         "--include-archived to backfill only past games).")
    ap.add_argument("--sleep-ms", type=int, default=200,
                    help="Throttle between markets in ms (default 200).")
    ap.add_argument("--loop", action="store_true",
                    help="Run forever, --interval seconds between cycles.")
    ap.add_argument("--interval", type=int, default=30,
                    help="Seconds between cycles in --loop mode (default 30).")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    logctx.setup(args.log_level.upper())

    backfill_days = args.days if args.backfill else None

    def _tickers() -> list[str]:
        if args.ticker:
            return [args.ticker]
        include_archived = args.include_archived or args.backfill
        return _ticker_universe(
            include_open=not args.no_open, include_archived=include_archived,
        )

    if not args.loop:
        tickers = _tickers()
        log.info("%s %d ticker(s)%s",
                 "backfilling" if backfill_days else "polling",
                 len(tickers),
                 f" over {args.days}d" if backfill_days else "")
        stats = run_cycle(tickers=tickers, backfill_days=backfill_days,
                          sleep_ms=args.sleep_ms)
        log.info("done: %s", stats)
        return 0

    log.info("trade_poller starting (interval=%ds)", args.interval)
    while True:
        try:
            t0 = time.time()
            stats = run_cycle(tickers=_tickers(), backfill_days=backfill_days,
                              sleep_ms=args.sleep_ms)
            elapsed = time.time() - t0
            if stats["stored"] or stats["errors"]:
                log.info("cycle in %.1fs: %s", elapsed, stats)
        except KeyboardInterrupt:
            log.info("interrupted; exiting")
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed: %s", e)
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    sys.exit(main())
