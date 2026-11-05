"""MLB-specific book poller for Kalshi.

The general ``book_poller`` snapshots top-25 markets per venue at 5-min
granularity — fine for politics + parlay markets that dominate Kalshi
volume, but it misses MLB game markets entirely (they don't crack the
top 25 by volume). This poller fills that gap:

* Pulls every open ``KXMLBGAME-*`` event + its nested markets.
* Snapshots each side's order book at high frequency (default 15s).
* Writes to the same ``book_archive.sqlite`` schema so downstream
  analysis joins are uniform across venues.

Event/market discovery is cached and refreshed every ``--discovery-interval``
seconds (default 300 = 5 min) so the fast book-polling loop doesn't
hammer the events API. The typical inner loop does only N×``get_order_book``
calls per cycle where N = number of open markets (~80 for a full slate).

Designed to run as a long-lived daemon during baseball hours. Cheap
when no games are live (one Kalshi events fetch + 0 book pulls if
no markets are open). Idempotent — re-runs at the same wall-clock
just overwrite the previous snapshot for that (venue, market, ts).

This is the **market-book half of the lead-lag dataset** that pairs
with ``baseball.live``'s play-by-play half. Together they enable the
analysis sketched in TODO.md → "Lead-lag analysis: live MLB events ×
market book reactions."

Usage::

    python mlb_book_poller.py                           # single cycle, exit
    python mlb_book_poller.py --loop --interval 15      # daemon mode (15s)
    scripts/mlb_book_poller.sh --background             # detach to log
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

import requests

from pm.archive import book_archive
from pm.core import logctx
from pm.venue.kalshi_client import _get as _kalshi_get  # uses 429-aware retry
from pm.venue.kalshi_client import get_order_book

# parse_order_book lives in polymarket_client but is venue-agnostic by
# design — kalshi_client.get_order_book pre-shapes its response to the
# common format. See kalshi_client.get_order_book docstring.
from pm.venue.polymarket_client import parse_order_book

log = logging.getLogger("mlb_book_poller")

EVENTS_URL = "/events"  # path on the Kalshi v2 API

# ─── Market discovery (cached) ───────────────────────────────────────────

_cached_markets: list[str] = []
_cache_ts: float = 0.0


def _open_mlb_event_tickers() -> list[str]:
    """Fetch every currently-open MLB event ticker. Cheap — one call,
    typically returns ~50-200 events covering the next week+."""
    try:
        payload = _kalshi_get(EVENTS_URL, params={
            "series_ticker": "KXMLBGAME",
            "status": "open",
            "limit": 200,
        })
    except Exception as e:  # noqa: BLE001
        log.warning("kalshi events fetch failed: %s", e)
        return []
    return [
        e["event_ticker"] for e in payload.get("events", [])
        if e.get("event_ticker")
    ]


def _market_tickers_for_event(event_ticker: str) -> list[str]:
    """Pull the nested markets for one event. Each MLB event has two
    markets (YES on each team). Returns the market tickers."""
    try:
        payload = _kalshi_get(f"/events/{event_ticker}",
                               params={"with_nested_markets": "true"})
    except Exception as e:  # noqa: BLE001
        log.debug("event detail %s failed: %s", event_ticker, e)
        return []
    out: list[str] = []
    for m in payload.get("event", {}).get("markets", []) or []:
        t = m.get("ticker")
        if t:
            out.append(str(t))
    return out


def _discover_markets() -> list[str]:
    """Full event→market discovery pass. Expensive (1 + N API calls
    where N = number of open events). Called infrequently."""
    events = _open_mlb_event_tickers()
    if not events:
        return []
    all_markets: list[str] = []
    for et in events:
        all_markets.extend(_market_tickers_for_event(et))
    return all_markets


def _get_markets(discovery_interval: float = 300.0) -> list[str]:
    """Return the cached market list, refreshing if stale."""
    global _cached_markets, _cache_ts
    now = time.time()
    if not _cached_markets or (now - _cache_ts) >= discovery_interval:
        fresh = _discover_markets()
        if fresh:
            _cached_markets = fresh
            _cache_ts = now
            log.info("discovery refresh: %d events → %d markets",
                     len(fresh) // 2, len(fresh))
        elif not _cached_markets:
            _cache_ts = now
    return _cached_markets


# ─── Polling cycle ───────────────────────────────────────────────────────


def run_cycle(*, discovery_interval: float = 300.0) -> dict[str, Any]:
    """One polling pass: use cached market list, snapshot every market's
    book, persist to book_archive. Returns aggregate stats."""
    t0 = time.time()
    stats = {
        "events": 0, "markets": 0,
        "snapshots_ok": 0, "snapshots_failed": 0,
        "elapsed_s": 0.0,
    }

    all_markets = _get_markets(discovery_interval)
    stats["events"] = len(all_markets) // 2
    stats["markets"] = len(all_markets)

    if not all_markets:
        stats["elapsed_s"] = round(time.time() - t0, 1)
        return stats

    for ticker in all_markets:
        try:
            raw = get_order_book(ticker)
        except requests.RequestException as e:
            log.debug("get_order_book(%s) failed: %s", ticker, e)
            stats["snapshots_failed"] += 1
            continue
        try:
            df_book = parse_order_book(raw)
        except Exception as e:  # noqa: BLE001
            log.debug("parse_order_book(%s) failed: %s", ticker, e)
            stats["snapshots_failed"] += 1
            continue
        ok = book_archive.store_snapshot(
            venue="kalshi",
            market_id=ticker,
            token_id=ticker,
            book=df_book,
        )
        if ok:
            stats["snapshots_ok"] += 1
        else:
            stats["snapshots_failed"] += 1

    stats["elapsed_s"] = round(time.time() - t0, 1)
    return stats


def _any_games_live() -> bool:
    """Check if any MLB games are plausibly in progress right now.

    The games table only stores Scheduled/Final/Postponed — there's no
    "In Progress" status (the live collector goes straight from
    Scheduled to Final). Instead we infer liveness: a game is live if
    it's still Scheduled and its start time is in the past (started but
    not yet marked Final). Also catches games within 15 min of first
    pitch so we're already fast-polling when the action begins.
    """
    try:
        import sqlite3

        from baseball import state
        with sqlite3.connect(state._state_path()) as conn:
            row = conn.execute("""
                SELECT COUNT(*) FROM games
                WHERE status = 'Scheduled'
                AND game_date <= datetime('now', '+15 minutes')
                AND game_date >= datetime('now', '-6 hours')
            """).fetchone()
            return (row[0] or 0) > 0
    except Exception:
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loop", action="store_true",
                     help="Run forever, sleeping --interval between cycles. "
                          "Default: single cycle and exit.")
    ap.add_argument("--interval", type=int, default=15,
                     help="Seconds between book-polling cycles when games "
                          "are live (default 15).")
    ap.add_argument("--idle-interval", type=int, default=120,
                     help="Seconds between cycles when no games are live "
                          "(default 120). Reduces API load during dead hours "
                          "so strategy runners don't hit 429s.")
    ap.add_argument("--discovery-interval", type=int, default=300,
                     help="Seconds between event/market discovery refreshes "
                          "(default 300 = 5 min). The fast --interval loop "
                          "reuses the cached market list between refreshes.")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logctx.setup(args.log_level.upper())

    if not args.loop:
        stats = run_cycle(discovery_interval=args.discovery_interval)
        log.info("cycle: %s", stats)
        return 0

    log.info("mlb_book_poller starting (live=%ds, idle=%ds, discovery=%ds)",
             args.interval, args.idle_interval, args.discovery_interval)
    was_live = None
    while True:
        try:
            t0 = time.time()
            stats = run_cycle(discovery_interval=args.discovery_interval)
            elapsed = time.time() - t0

            live = _any_games_live()
            interval = args.interval if live else args.idle_interval

            if live != was_live:
                mode = "LIVE (fast)" if live else "IDLE (slow)"
                log.info("mode → %s (%ds interval)", mode, interval)
                was_live = live

            log.info("cycle in %.1fs: %s", elapsed, stats)
        except KeyboardInterrupt:
            log.info("interrupted; exiting")
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed: %s", e)
            interval = args.idle_interval  # back off on errors
        time.sleep(max(1, interval))


if __name__ == "__main__":
    sys.exit(main())
