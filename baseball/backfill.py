"""One-shot historical backfill of MLB play-by-play.

Walks past N days, pulls the schedule for each, fetches `feed/live` for
every finalized game, and persists every play to ``live_plays``. Same
table the forward-running ``baseball.live`` daemon writes to — they
co-exist cleanly because inserts are idempotent on ``(game_pk, play_id)``.

What this gives us
------------------
- ~15 games/day × 30 days × ~75 plays/game ≈ 30k+ plays for "free" once
- Bootstraps lead-lag analysis without weeks of forward collection
- Validates the play extraction code against a wide variety of real games
- Stratifies event-type distributions (HR, K, BB, errors, etc.) so we
  know what we're dealing with before designing the analysis layer

What this does NOT give us
--------------------------
- The market-book half of the lead-lag join. ``book_poller`` only
  captures forward from when it was started. Historical Kalshi book
  data isn't recoverable. So historical plays alone don't enable
  reaction-time analysis — but they're still useful for everything
  else.
- Past predictions. Computing "what would the model say on game day"
  requires team stats as-of-date. Doing that honestly is the
  walk-forward backtest piece in TODO.md and is deliberately separate.

Usage
-----
``python -m baseball.backfill --days 30``
``python -m baseball.backfill --start 2026-04-01 --end 2026-05-20``
``python -m baseball.backfill --days 30 --sleep-ms 250``  # be even nicer
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

from baseball import data as _data
from baseball import live, state

log = logging.getLogger("baseball.backfill")


# ─── Date iteration ─────────────────────────────────────────────────────


def _date_range(start: date, end: date) -> Iterator[date]:
    """Inclusive on both ends, oldest-first so progress is visible."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ─── Per-day pull ────────────────────────────────────────────────────────


def _pull_games_for_date(d: date, *, sleep_ms: int) -> list[int]:
    """Returns the list of finalized ``game_pk`` we found for this date.
    Schedule is small (~15 games), one API call. Sleeps after the call
    to be polite even though we're well within MLB's published limits.

    Side effect: upserts every game we see (final or not) into the
    ``games`` table so the web UI can navigate to historical games.
    Idempotent — re-runs just refresh the rows."""
    try:
        sched = _data.get_schedule(d)
    except Exception as e:  # noqa: BLE001
        log.warning("schedule fetch failed for %s: %s", d.isoformat(), e)
        return []
    # Persist all games (including non-finals) — useful for date-range
    # browsing even if we can't backfill plays for non-finalized games.
    for g in sched:
        try:
            state.upsert_game(g)
        except Exception as e:  # noqa: BLE001
            log.warning("upsert_game(%s) failed: %s", g.game_pk, e)
    finals = [g for g in sched if g.status == "Final"]
    log.info("  %s: %d games (%d final)", d.isoformat(), len(sched), len(finals))
    time.sleep(sleep_ms / 1000.0)
    return [g.game_pk for g in finals]


def _backfill_game(game_pk: int, *, sleep_ms: int) -> int:
    """Fetch feed/live for one finalized game and persist every new
    play. Returns the count of plays inserted (0 if all were already in
    the table from a prior run — backfill is fully idempotent)."""
    feed = live.fetch_live_feed(game_pk)
    time.sleep(sleep_ms / 1000.0)
    if feed is None:
        return 0
    plays = live._extract_plays(feed, game_pk)
    last_seen = live._last_play_id(game_pk)
    n = 0
    for p in plays:
        if p["play_id"] <= last_seen:
            continue
        live._insert_play(p)
        n += 1
    return n


# ─── Driver ──────────────────────────────────────────────────────────────


def run_backfill(*, start: date, end: date, sleep_ms: int = 200,
                  games_only: bool = False) -> dict:
    """Walk ``start..end`` inclusive, backfilling plays for every
    finalized game. Returns aggregate stats.

    ``games_only=True`` skips the per-game feed/live fetch — useful when
    you've already pulled the play data and just want to populate the
    games table for the same date range (e.g. so the web UI can navigate
    historical games)."""
    live._ensure_live_schema()
    state.migrate()

    t0 = time.time()
    total_games = 0
    total_plays = 0
    errors = 0
    days_walked = 0

    for d in _date_range(start, end):
        days_walked += 1
        try:
            game_pks = _pull_games_for_date(d, sleep_ms=sleep_ms)
        except Exception as e:  # noqa: BLE001
            log.warning("day %s failed: %s", d, e)
            errors += 1
            continue

        if games_only:
            total_games += len(game_pks)
            continue

        for game_pk in game_pks:
            try:
                n = _backfill_game(game_pk, sleep_ms=sleep_ms)
                total_plays += n
                total_games += 1
                if n > 0:
                    log.info("    game %s: +%d plays", game_pk, n)
            except Exception as e:  # noqa: BLE001
                log.warning("    game %s failed: %s", game_pk, e)
                errors += 1

    elapsed = time.time() - t0
    stats = {
        "start": start.isoformat(), "end": end.isoformat(),
        "days": days_walked, "games_backfilled": total_games,
        "plays_inserted": total_plays, "errors": errors,
        "elapsed_s": round(elapsed, 1),
    }
    state.log_activity("backfill", f"{total_games} games, {total_plays} plays "
                                     f"({start} → {end})", stats)
    return stats


# ─── CLI ─────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=None,
                     help="Backfill the last N days (alternative to "
                          "--start/--end). Default: 30 if neither given.")
    ap.add_argument("--start", type=str, default=None,
                     help="Backfill from this date inclusive (YYYY-MM-DD).")
    ap.add_argument("--end", type=str, default=None,
                     help="Backfill through this date inclusive "
                          "(YYYY-MM-DD). Default: yesterday UTC.")
    ap.add_argument("--sleep-ms", type=int, default=200,
                     help="Sleep this many ms between API calls "
                          "(default 200, ~5 req/sec).")
    ap.add_argument("--games-only", action="store_true",
                     help="Only upsert games rows; skip per-game feed/live "
                          "fetch. Useful when plays were already backfilled.")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    today = datetime.now(UTC).date()
    if args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else today - timedelta(days=1)
    else:
        days = args.days or 30
        end = date.fromisoformat(args.end) if args.end else today - timedelta(days=1)
        start = end - timedelta(days=days - 1)

    log.info("backfilling %s → %s (sleep=%dms between API calls)",
             start.isoformat(), end.isoformat(), args.sleep_ms)
    log.info("(safe to re-run — inserts are idempotent on (game_pk, play_id))")

    try:
        stats = run_backfill(start=start, end=end, sleep_ms=args.sleep_ms,
                              games_only=args.games_only)
    except KeyboardInterrupt:
        log.info("interrupted; partial backfill persisted")
        return 0

    log.info("done in %.1fs: %d games / %d plays inserted%s",
             stats["elapsed_s"], stats["games_backfilled"],
             stats["plays_inserted"],
             f" ({stats['errors']} errors)" if stats["errors"] else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
