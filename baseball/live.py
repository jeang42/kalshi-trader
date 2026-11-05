"""Live in-game event collection.

Polls MLB's `feed/live` endpoint for every in-progress game and persists
each new play to the sidecar's `live_plays` table. Pure data collection
— no analysis, no trading. The lead-lag research and any eventual
in-game reaction strategy build on the dataset this collector produces.

Architecture
------------
Two-stage pipeline that runs every ``--interval`` seconds (default 10):

1. **Discover** — `_in_progress_games()` scans the sidecar's `games`
   table for today's/yesterday's games, then re-checks each via the
   MLB live API (the stored status can be stale for hours).

2. **Pull + persist** — for each in-progress game, fetch
   `feed/live`, pull `liveData.plays.allPlays`, and persist any play
   whose id is newer than the last one we saw for that game.

The 10-second interval matches MLB's CDN cache TTL (`max-age=10`).
Each call is ~0.15s and ~750KB; 15 concurrent games = 1.5 req/s,
well within CDN capacity. Average play detection latency: ~5 seconds.

State
-----
Table ``live_plays`` in ``baseball_state.sqlite``:

    game_pk, play_id, ts, half_inning, inning, event_type, description,
    home_score_after, away_score_after, home_win_prob, raw_json

``home_win_prob`` is MLB's own win-expectancy when present, NOT our
model's. The join with our model lives in analysis.

Schedule
--------
Run as a long-lived daemon (`python -m baseball.live --loop --interval 10`)
during game hours. Or one-shot (`python -m baseball.live`) from cron at
high frequency. Both modes are idempotent — re-running with the same
state is a no-op except for any new plays.

When to run
-----------
MLB games happen roughly 12:00-04:00 UTC during the regular season,
with afternoon games starting ~17:00 UTC and evening games ~22:00 UTC.
The collector is cheap when no games are in progress (one DB query, no
HTTP) so leaving the daemon running 24/7 is fine.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from baseball import state

log = logging.getLogger("baseball.live")

# Live game-feed endpoint is on the v1.1 API (not v1).
LIVE_FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"


# ─── State extensions ────────────────────────────────────────────────────


def _ensure_live_schema() -> None:
    """Idempotent schema bootstrap. Called at runner startup. Avoids a
    full migration because this is purely additive — no rewrites of
    existing tables."""
    import sqlite3

    from baseball.state import _state_path
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=30.0) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS live_plays (
                game_pk          INTEGER NOT NULL,
                play_id          INTEGER NOT NULL,
                ts               TEXT NOT NULL,
                half_inning      TEXT,
                inning           INTEGER,
                event_type       TEXT,
                description      TEXT,
                home_score_after INTEGER,
                away_score_after INTEGER,
                home_win_prob    REAL,
                raw_json         TEXT,
                fetched_at       TEXT NOT NULL,
                PRIMARY KEY (game_pk, play_id)
            );
            CREATE INDEX IF NOT EXISTS live_plays_by_game_ts
                ON live_plays(game_pk, ts);
            CREATE INDEX IF NOT EXISTS live_plays_by_ts
                ON live_plays(ts DESC);
        """)
        conn.commit()


def _last_play_id(game_pk: int) -> int:
    """Highest play_id we've persisted for this game, or -1 if none.
    Used to skip already-seen plays on each poll."""
    import sqlite3

    from baseball.state import _state_path
    with sqlite3.connect(_state_path(), timeout=30.0) as conn:
        row = conn.execute(
            "SELECT MAX(play_id) FROM live_plays WHERE game_pk = ?",
            (game_pk,),
        ).fetchone()
        if row is None or row[0] is None:
            return -1
        return int(row[0])


def _insert_play(row: dict[str, Any]) -> None:
    import sqlite3

    from baseball.state import _state_path
    with sqlite3.connect(_state_path(), timeout=30.0) as conn:
        conn.execute("""
            INSERT INTO live_plays (game_pk, play_id, ts, half_inning,
                                     inning, event_type, description,
                                     home_score_after, away_score_after,
                                     home_win_prob, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_pk, play_id) DO NOTHING
        """, (
            row["game_pk"], row["play_id"], row["ts"],
            row.get("half_inning"), row.get("inning"),
            row.get("event_type"), row.get("description"),
            row.get("home_score_after"), row.get("away_score_after"),
            row.get("home_win_prob"),
            json.dumps(row.get("raw") or {}),
            datetime.now(UTC).isoformat(timespec="seconds"),
        ))
        conn.commit()


def get_recent_plays(game_pk: int, *, limit: int = 50) -> list[dict[str, Any]]:
    """Public read API. Returns the most-recent ``limit`` plays for one
    game in chronological order (oldest first)."""
    import sqlite3

    from baseball.state import _state_path
    with sqlite3.connect(_state_path(), timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM live_plays
            WHERE game_pk = ?
            ORDER BY play_id DESC
            LIMIT ?
        """, (game_pk, limit)).fetchall()
        return list(reversed([dict(r) for r in rows]))


def live_plays_summary(*, since_hours: float = 24.0) -> dict[str, Any]:
    """Aggregate stats for the control plane / health checks."""
    import sqlite3

    from baseball.state import _state_path
    since = (datetime.now(UTC)
             - timedelta(hours=since_hours)).isoformat()
    with sqlite3.connect(_state_path(), timeout=30.0) as conn:
        n_plays = conn.execute(
            "SELECT COUNT(*) FROM live_plays WHERE fetched_at >= ?",
            (since,),
        ).fetchone()[0]
        n_games = conn.execute(
            "SELECT COUNT(DISTINCT game_pk) FROM live_plays "
            "WHERE fetched_at >= ?",
            (since,),
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM live_plays").fetchone()[0]
    return {
        "plays_last_24h": int(n_plays),
        "games_last_24h": int(n_games),
        "total_plays": int(total),
    }


# ─── Live feed pull ──────────────────────────────────────────────────────


def fetch_live_feed(game_pk: int, *, timeout: float = 10.0
                     ) -> dict[str, Any] | None:
    """One call to MLB's live feed. Returns the raw JSON or None on
    failure. We don't want a failed game to take down the whole loop."""
    url = LIVE_FEED_URL.format(game_pk=game_pk)
    try:
        r = requests.get(url, timeout=timeout, headers={
            "User-Agent": "kalshi-trader-baseball/0.1 (+https://github.com/jeang42/kalshi-trader)",
        })
        if r.status_code == 404:
            return None  # game doesn't exist (rare; defensive)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning("live feed fetch failed for %s: %s", game_pk, e)
        return None


def _extract_plays(feed: dict[str, Any], game_pk: int
                    ) -> list[dict[str, Any]]:
    """Flatten ``liveData.plays.allPlays`` into our row shape. The MLB
    schema is generous — we keep the raw blob in ``raw_json`` so future
    analysis can drill in without re-pulling.

    Win-expectancy: MLB sometimes hydrates ``homeTeamWinProbability``
    on a play's ``about`` block. When absent we leave it None and
    backfill later via a separate hydrate call (TBD)."""
    out: list[dict[str, Any]] = []
    plays = (feed.get("liveData", {}).get("plays", {})
                  .get("allPlays", []) or [])
    for p in plays:
        about = p.get("about", {}) or {}
        result = p.get("result", {}) or {}
        play_id = about.get("atBatIndex")
        if play_id is None:
            continue
        end_time = about.get("endTime") or about.get("startTime")
        out.append({
            "game_pk": game_pk,
            "play_id": int(play_id),
            "ts": end_time or datetime.now(UTC).isoformat(),
            "half_inning": about.get("halfInning"),
            "inning": about.get("inning"),
            "event_type": result.get("event") or result.get("eventType"),
            "description": result.get("description"),
            "home_score_after": result.get("homeScore"),
            "away_score_after": result.get("awayScore"),
            "home_win_prob": about.get("homeTeamWinProbability"),
            "raw": p,  # full blob for downstream analysis
        })
    return out


# ─── In-progress-game discovery ──────────────────────────────────────────

# Game statuses considered "live" — anything that isn't a final state
# and isn't a future "Scheduled" state. We're permissive here because
# MLB's status taxonomy has many edge cases (manager challenge, rain
# delay, suspended, etc.) and we'd rather over-poll a delayed game than
# miss the resumption.
_LIVE_STATUSES = {
    "In Progress", "Manager challenge", "Manager Challenge",
    "Delayed", "Delayed: Rain", "Delayed Start: Rain",
    "Pre-Game", "Warmup",
}


def _in_progress_game_pks() -> list[int]:
    """Game IDs we should poll. Pulls every loaded game and re-checks
    status via a lightweight schedule call rather than trusting our
    nightly-refresh snapshot (which can be stale by hours for a midnight
    Eastern game)."""
    import sqlite3

    from baseball.state import _state_path
    today = datetime.now(UTC).date()
    out: list[int] = []
    with sqlite3.connect(_state_path(), timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        # Only consider games scheduled for today or yesterday (a late
        # extra-innings game can spill over midnight UTC).
        for offset in (-1, 0):
            day = (today + timedelta(days=offset)).isoformat()
            for r in conn.execute(
                "SELECT game_pk FROM games WHERE game_date LIKE ?",
                (day + "%",),
            ).fetchall():
                out.append(int(r["game_pk"]))
    return out


def _sync_final_status_to_games(feed: dict[str, Any], game_pk: int) -> bool:
    """When the feed says a game is Final, push that + the final scores
    back into the games table. Returns True if the games row actually
    changed (was previously "Scheduled" or had no score).

    This closes a 6-hour resolution lag — without this, a 22:30 UTC
    game finish has to wait for the 04:15 UTC runner to refresh status
    before the auto-resolver can pick up the result. With this, the
    next 60s daemon cycle marks it Final and the next 15-min auto-resolve
    cycle resolves the forecast."""
    import sqlite3

    from baseball.state import _state_path
    status = (feed.get("gameData", {}).get("status", {})
                   .get("detailedState", "") or "").strip()
    if status != "Final":
        # We only sync clean "Final" — postponed / cancelled / suspended
        # all have separate semantics we don't auto-resolve from.
        return False
    linescore = feed.get("liveData", {}).get("linescore", {}) or {}
    teams = linescore.get("teams", {}) or {}
    home_score = (teams.get("home", {}) or {}).get("runs")
    away_score = (teams.get("away", {}) or {}).get("runs")
    if home_score is None or away_score is None:
        return False

    with sqlite3.connect(_state_path(), timeout=30.0) as conn:
        cur = conn.execute(
            "SELECT status, home_score, away_score FROM games WHERE game_pk = ?",
            (game_pk,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        cur_status, cur_home, cur_away = row
        if cur_status == "Final" and cur_home == home_score and cur_away == away_score:
            return False  # already in sync
        from datetime import datetime
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE games SET status = ?, home_score = ?, away_score = ?, "
            "updated_at = ? WHERE game_pk = ?",
            ("Final", int(home_score), int(away_score), now_iso, game_pk),
        )
        conn.commit()
    return True


def _is_game_live(feed: dict[str, Any]) -> bool:
    status = (feed.get("gameData", {}).get("status", {})
                   .get("detailedState", "") or "").strip()
    if not status:
        return False
    if status in {"Final", "Game Over", "Completed Early",
                  "Postponed", "Cancelled"}:
        return False
    if status == "Scheduled":
        # Sometimes a "Scheduled" game actually has plays (pre-game
        # transition window). Treat as live if any plays exist.
        plays = (feed.get("liveData", {}).get("plays", {})
                      .get("allPlays") or [])
        return len(plays) > 0
    # Everything else (In Progress, Delayed, Pre-Game, etc.) → poll
    return True


# ─── Cycle ───────────────────────────────────────────────────────────────


def run_cycle() -> dict[str, Any]:
    """One polling pass. Returns stats so the daemon log can show
    activity at a glance."""
    _ensure_live_schema()
    state.migrate()  # ensure the baseline tables exist too

    candidates = _in_progress_game_pks()
    polled = 0
    new_plays = 0
    skipped_final = 0
    errors = 0
    games_with_new = 0

    games_finalized = 0
    for game_pk in candidates:
        feed = fetch_live_feed(game_pk)
        if feed is None:
            errors += 1
            continue
        if not _is_game_live(feed):
            # Game is final/postponed/cancelled — propagate status + scores
            # from the feed into our games table so the resolver can pick
            # up the result immediately. Without this, the games row stays
            # "Scheduled" until baseball.runner re-fetches at 04:15 UTC,
            # which can be 6+ hours of resolution lag for a late game.
            try:
                if _sync_final_status_to_games(feed, game_pk):
                    games_finalized += 1
            except Exception as e:  # noqa: BLE001 — best-effort sync
                log.debug("status sync failed for %s: %s", game_pk, e)
            skipped_final += 1
            continue
        polled += 1
        plays = _extract_plays(feed, game_pk)
        last_seen = _last_play_id(game_pk)
        new_for_game = 0
        for play in plays:
            if play["play_id"] <= last_seen:
                continue
            _insert_play(play)
            new_for_game += 1
        new_plays += new_for_game
        if new_for_game > 0:
            games_with_new += 1

    stats = {
        "candidates": len(candidates),
        "polled": polled,
        "skipped_final": skipped_final,
        "games_finalized": games_finalized,
        "errors": errors,
        "new_plays": new_plays,
        "games_with_new": games_with_new,
    }
    if new_plays > 0 or polled > 0:
        state.log_activity("live", f"polled {polled} game(s), "
                                     f"+{new_plays} new plays")
    return stats


# ─── CLI ─────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loop", action="store_true",
                     help="Run forever, sleeping --interval between cycles. "
                          "Default: single cycle and exit.")
    ap.add_argument("--interval", type=int, default=10,
                     help="Seconds between cycles in --loop mode (default 10, "
                          "matching MLB's CDN cache TTL).")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.loop:
        stats = run_cycle()
        log.info("cycle: %s", stats)
        return 0

    log.info("baseball live collector starting (interval=%ds)", args.interval)
    while True:
        try:
            t0 = time.time()
            stats = run_cycle()
            elapsed = time.time() - t0
            log.info("cycle in %.1fs: %s", elapsed, stats)
        except KeyboardInterrupt:
            log.info("interrupted; exiting")
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed: %s", e)
            try:
                state.log_activity("error", f"live cycle failed: {e}")
            except Exception:
                pass
        # Sleep regardless of success/failure to avoid hot-spinning on
        # a persistent error.
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
