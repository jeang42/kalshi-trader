"""Pre-positioning strategy for in-game baseball trading.

Instead of reacting to plays after they happen (market is too fast),
this module computes target prices *before* plays occur and simulates
resting limit orders that would get filled during the repricing window.

Core idea
---------
For each live game in a targetable state (close, meaningful innings):

1. Compute P(home wins) at the CURRENT game state using our WPA model.
2. Compute P(home wins) if the NEXT run is scored by home/away.
3. The difference is the expected price move from one run.
4. Place simulated limit orders at the current book price ± that move.
5. When a run actually scores, check if our pre-positioned price would
   have been filled during the repricing window.

This produces a paper-trading log that tracks both the theoretical P&L
and the comparison against a pure reactive approach (buy at first
observed post-play price).

Usage::

    python pre_position.py                          # one cycle
    python pre_position.py --loop --interval 10     # daemon
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pm import REPO_ROOT
from pm.core import logctx

try:
    from dotenv import load_dotenv
    # .env lives at the REPO root — Path(__file__).parent stopped meaning that
    # when R-24 moved this module under pm/ (gotcha 1b: a __file__ default
    # captures the module's LOCATION). Loading a nonexistent .env is silent,
    # which is gotcha #4's failure mode: config believed loaded, actually not.
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from baseball import analysis, state, wpa
from baseball.live import _ensure_live_schema

log = logging.getLogger("pre_position")

LOG_PATH = Path("data/shared/pre_position_log.jsonl")


# ─── Game state extraction ───────────────────────────────────────────────

@dataclass
class LiveGameState:
    """Current state of one in-progress game, extracted from the most
    recent play in live_plays."""
    game_pk: int
    home_abbr: str
    away_abbr: str
    inning: int
    half: str  # "top" | "bottom"
    outs: int
    bases: tuple[bool, bool, bool]
    home_score: int
    away_score: int
    score_diff: int  # home - away

    @property
    def is_close(self) -> bool:
        return abs(self.score_diff) <= 2

    @property
    def is_targetable(self) -> bool:
        """Games worth pre-positioning on: close enough that a run
        matters, and far enough along that the market is liquid."""
        return self.is_close and self.inning >= 4


@dataclass
class PrePositionTarget:
    """Pre-computed target prices for one game."""
    game_pk: int
    home_abbr: str
    away_abbr: str
    computed_at: str
    inning: int
    half: str
    score: str  # "3-2"

    current_wp: float  # P(home wins) at current state
    wp_if_home_scores: float  # P(home wins) if home scores next run
    wp_if_away_scores: float  # P(home wins) if away scores next run

    home_run_delta: float  # wp_if_home_scores - current_wp
    away_run_delta: float  # wp_if_away_scores - current_wp

    # Book state
    home_ticker: str | None = None
    away_ticker: str | None = None
    current_home_mid: float | None = None

    # Simulated order targets
    buy_home_target: float | None = None  # buy home YES if price below this
    buy_away_target: float | None = None  # buy away YES if price below this


# ─── WPA-based target computation ────────────────────────────────────────

def _current_game_states() -> list[LiveGameState]:
    """Extract the current state of every in-progress game from
    the most recent play in live_plays."""
    import sqlite3
    conn = sqlite3.connect(state._state_path())
    conn.row_factory = sqlite3.Row

    # Games that are genuinely live: a play landed recently AND the game
    # isn't finished. NOTE: games.status is unreliable mid-game — the MLB
    # feed leaves it stuck at 'Pre-Game'/'Warmup' and never sets an
    # 'In Progress' value, so the old `status = 'Scheduled'` gate matched
    # nothing once a game started and this returned 0 live games all night
    # (the live-baseball daemons were blind). 'Final'/'Game Over' DO get
    # synced reliably by baseball.live, so we still use status to exclude
    # finished games — that keeps the scalpers from opening into a market
    # that has already resolved.
    game_pks = conn.execute("""
        SELECT g.game_pk, th.abbreviation AS home, ta.abbreviation AS away
        FROM games g
        JOIN teams th ON th.id = g.home_team_id
        JOIN teams ta ON ta.id = g.away_team_id
        WHERE g.game_pk IN (
            SELECT DISTINCT game_pk FROM live_plays
            WHERE datetime(ts) >= datetime('now', '-20 minutes')
        )
        AND g.status NOT IN ('Final', 'Game Over', 'Completed Early')
    """).fetchall()

    states = []
    for row in game_pks:
        gpk = row["game_pk"]
        # Get the latest play with situation data
        play = conn.execute("""
            SELECT raw_json, home_score_after, away_score_after
            FROM live_plays
            WHERE game_pk = ? AND raw_json IS NOT NULL
            ORDER BY play_id DESC LIMIT 1
        """, (gpk,)).fetchone()

        if not play or not play["raw_json"]:
            continue

        sit = analysis.parse_situation(play["raw_json"])
        if sit.inning is None:
            continue

        states.append(LiveGameState(
            game_pk=gpk,
            home_abbr=row["home"],
            away_abbr=row["away"],
            inning=sit.inning,
            half=sit.half_inning or "top",
            outs=sit.outs_after or 0,
            bases=sit.bases,
            home_score=play["home_score_after"] or 0,
            away_score=play["away_score_after"] or 0,
            score_diff=(play["home_score_after"] or 0) - (play["away_score_after"] or 0),
        ))

    conn.close()
    return states


def compute_targets(game: LiveGameState) -> PrePositionTarget:
    """For one game, compute current WP and WP after next run for
    each side. Returns the pre-positioning targets."""

    current_wp = wpa.win_probability(
        game.inning, game.half, game.outs,
        game.bases, game.score_diff,
    )

    # "Next run scores for home" — simplest model: score_diff + 1,
    # bases cleared, same inning/half/outs (as if a solo HR)
    wp_home_scores = wpa.win_probability(
        game.inning, game.half, game.outs,
        (False, False, False),  # bases cleared after scoring
        game.score_diff + 1,
    )

    wp_away_scores = wpa.win_probability(
        game.inning, game.half, game.outs,
        (False, False, False),
        game.score_diff - 1,
    )

    home_delta = wp_home_scores - current_wp
    away_delta = wp_away_scores - current_wp

    # Get current book prices from tickers
    tickers = analysis.tickers_for_game(game.game_pk)
    home_ticker = None
    away_ticker = None
    current_home_mid = None

    for t in tickers:
        team = t.split("-")[-1]
        if team == game.home_abbr:
            home_ticker = t
        elif team == game.away_abbr:
            away_ticker = t

    if home_ticker:
        from pm.archive.book_archive import get_snapshot_at
        snap = get_snapshot_at(
            venue="kalshi", market_id=home_ticker,
            token_id=home_ticker, ts=datetime.now(UTC),
        )
        if snap is not None:
            df_book = snap.book
            if not df_book.empty:
                bids = df_book[df_book["side"] == "bid"]
                asks = df_book[df_book["side"] == "ask"]
                best_bid = bids["price"].max() if not bids.empty else None
                best_ask = asks["price"].min() if not asks.empty else None
                if best_bid is not None and best_ask is not None:
                    current_home_mid = (best_bid + best_ask) / 2

    # Pre-positioning targets:
    # If we think home scoring moves the price by +home_delta,
    # we want to buy home YES at a price BELOW the post-scoring fair value.
    # Target = current_mid + home_delta * discount_factor
    # The discount captures the fact that we're buying BEFORE the event.
    #
    # Empirical observation (tonight's 9 plays): the market typically only
    # moves 20-40% of the WPA-derived delta, so demanding 50% was too
    # greedy (1/9 fills). 25% targets a more realistic fill rate while
    # still capturing meaningful edge per fill.
    discount = 0.25

    buy_home_target = None
    buy_away_target = None
    if current_home_mid is not None:
        if home_delta > 0.03:  # only if the expected move is meaningful
            buy_home_target = current_home_mid + home_delta * discount
        if away_delta < -0.03:  # away scoring drops home YES price
            buy_away_target = (1.0 - current_home_mid) + abs(away_delta) * discount

    return PrePositionTarget(
        game_pk=game.game_pk,
        home_abbr=game.home_abbr,
        away_abbr=game.away_abbr,
        computed_at=datetime.now(UTC).isoformat(),
        inning=game.inning,
        half=game.half,
        score=f"{game.away_score}-{game.home_score}",
        current_wp=round(current_wp, 4),
        wp_if_home_scores=round(wp_home_scores, 4),
        wp_if_away_scores=round(wp_away_scores, 4),
        home_run_delta=round(home_delta, 4),
        away_run_delta=round(away_delta, 4),
        home_ticker=home_ticker,
        away_ticker=away_ticker,
        current_home_mid=round(current_home_mid, 4) if current_home_mid else None,
        buy_home_target=round(buy_home_target, 4) if buy_home_target else None,
        buy_away_target=round(buy_away_target, 4) if buy_away_target else None,
    )


# ─── Outcome evaluation ─────────────────────────────────────────────────

def evaluate_scoring_play(
    target: PrePositionTarget,
    play_game_pk: int,
    home_scored: bool,
    book_points: list,  # from analysis.book_around_play
) -> dict[str, Any]:
    """After a scoring play, evaluate what both strategies would have done.

    Returns a dict with:
    - pre_position result: would our limit order have been filled?
    - reactive result: what price would we have gotten buying at first post-play snapshot?
    """
    pre = [p for p in book_points if p.seconds_from_play <= 0 and p.mid is not None]
    post = [p for p in book_points if p.seconds_from_play > 0 and p.mid is not None]

    if not pre or not post:
        return {"error": "insufficient book data"}

    baseline_mid = pre[-1].mid
    first_post_mid = post[0].mid
    first_delay = post[0].seconds_from_play
    final_mid = post[-1].mid

    # Pre-position approach: would our target have been filled?
    # During the repricing, the price traverses from baseline to final.
    # A limit order at our target would fill if the price crossed it.
    if home_scored:
        expected_delta = target.home_run_delta
        target_price = target.buy_home_target
    else:
        expected_delta = target.away_run_delta
        target_price = target.buy_away_target

    pre_fill_price = None
    if target_price is not None:
        # Check if any post-play price crossed our target
        for p in post:
            if p.mid is not None and p.mid >= target_price:
                pre_fill_price = target_price
                break

    # Reactive approach: buy at first post-play snapshot
    reactive_price = first_post_mid

    return {
        "game_pk": play_game_pk,
        "home_scored": home_scored,
        "baseline_mid": baseline_mid,
        "final_mid": final_mid,
        "expected_delta": expected_delta,
        "target_price": target_price,
        "pre_position_filled": pre_fill_price is not None,
        "pre_position_price": pre_fill_price,
        "reactive_price": reactive_price,
        "reactive_delay_s": first_delay,
        "pre_position_edge": (final_mid - pre_fill_price) if pre_fill_price else None,
        "reactive_edge": final_mid - reactive_price,
        "move_captured_pre": abs(final_mid - pre_fill_price) if pre_fill_price else 0,
        "move_captured_reactive": abs(final_mid - reactive_price),
        "total_move": abs(final_mid - baseline_mid),
    }


# ─── Logging ─────────────────────────────────────────────────────────────

def _log_targets(targets: list[PrePositionTarget]) -> None:
    """Append current targets to the JSONL log for later analysis."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        for t in targets:
            entry = {
                "type": "target",
                "ts": datetime.now(UTC).isoformat(),
                **{k: v for k, v in asdict(t).items()},
            }
            f.write(json.dumps(entry) + "\n")


def _log_evaluation(evaluation: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        entry = {"type": "evaluation", "ts": datetime.now(UTC).isoformat(), **evaluation}
        f.write(json.dumps(entry) + "\n")


# ─── Run cycle ───────────────────────────────────────────────────────────

def _infer_event_type(description: str) -> str:
    """Best-effort event type from the play description when the
    event_type column is NULL."""
    d = description.lower()
    if "homers" in d or "home run" in d:
        return "Home Run"
    if "doubles" in d:
        return "Double"
    if "triples" in d:
        return "Triple"
    if "singles" in d:
        return "Single"
    if "walks" in d or "walk" in d:
        return "Walk"
    if "wild pitch" in d:
        return "Wild Pitch"
    if "sac fly" in d or "sacrifice fly" in d:
        return "Sac Fly"
    if "error" in d:
        return "Field Error"
    if "hit by pitch" in d:
        return "Hit By Pitch"
    if "grounds into" in d and "double play" in d:
        return "Grounded Into DP"
    if "scores" in d:
        return "Run Scored"
    return "Unknown"


_active_targets: dict[int, PrePositionTarget] = {}
_last_play_ids: dict[int, int] = {}


def run_cycle() -> dict[str, Any]:
    """One cycle: update targets for live games, check for scoring plays
    against existing targets."""
    import sqlite3

    _ensure_live_schema()
    stats = {"games_live": 0, "games_targetable": 0, "targets_set": 0,
             "scoring_plays": 0, "evaluations": 0}

    game_states = _current_game_states()
    stats["games_live"] = len(game_states)

    # Update targets for targetable games
    new_targets: dict[int, PrePositionTarget] = {}
    for game in game_states:
        if not game.is_targetable:
            continue
        stats["games_targetable"] += 1
        try:
            target = compute_targets(game)
            new_targets[game.game_pk] = target
            stats["targets_set"] += 1
        except Exception as e:
            log.debug("target computation failed for %s: %s", game.game_pk, e)

    if new_targets:
        _log_targets(list(new_targets.values()))

    # Check for new scoring plays against active targets
    conn = sqlite3.connect(state._state_path())
    for gpk, target in _active_targets.items():
        last_pid = _last_play_ids.get(gpk, -1)

        # Find scoring plays since our last check
        plays = conn.execute("""
            WITH plays AS (
                SELECT play_id, event_type, description, raw_json,
                       home_score_after, away_score_after,
                       LAG(home_score_after) OVER (ORDER BY play_id) AS prev_home,
                       LAG(away_score_after) OVER (ORDER BY play_id) AS prev_away
                FROM live_plays WHERE game_pk = ?
            )
            SELECT play_id, event_type, description, home_score_after,
                   away_score_after, prev_home, prev_away
            FROM plays
            WHERE play_id > ?
            AND ((home_score_after IS NOT NULL AND prev_home IS NOT NULL
                  AND home_score_after != prev_home)
                 OR (away_score_after IS NOT NULL AND prev_away IS NOT NULL
                     AND away_score_after != prev_away))
        """, (gpk, last_pid)).fetchall()

        for pid, evt, desc, hs, _aws, prev_h, _prev_a in plays:
            if not evt and desc:
                evt = _infer_event_type(desc)
            stats["scoring_plays"] += 1
            home_scored = (hs or 0) > (prev_h or 0)

            book_pts = analysis.book_around_play(gpk, pid, window_minutes=3.0)
            if book_pts:
                evaluation = evaluate_scoring_play(target, gpk, home_scored, book_pts)
                evaluation["event_type"] = evt or "Unknown"
                evaluation["description"] = (desc or "")[:80]
                evaluation["inning"] = target.inning
                evaluation["score_before"] = target.score
                _log_evaluation(evaluation)
                stats["evaluations"] += 1

                if evaluation.get("pre_position_filled"):
                    log.info("PRE-POSITION FILL: %s %s@%s inn=%d | "
                             "target=$%.3f filled, edge=$%.3f",
                             evt, target.away_abbr, target.home_abbr,
                             target.inning, evaluation["pre_position_price"],
                             evaluation.get("pre_position_edge", 0))

    conn.close()

    # Update play tracking and active targets
    conn2 = sqlite3.connect(state._state_path())
    for gpk in set(list(new_targets.keys()) + list(_active_targets.keys())):
        row = conn2.execute(
            "SELECT MAX(play_id) FROM live_plays WHERE game_pk = ?", (gpk,)
        ).fetchone()
        if row and row[0]:
            _last_play_ids[gpk] = row[0]
    conn2.close()

    _active_targets.clear()
    _active_targets.update(new_targets)

    return stats


# ─── CLI ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=10,
                    help="Seconds between cycles (default 10, matches live collector).")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logctx.setup(args.log_level.upper())

    if not args.loop:
        stats = run_cycle()
        log.info("cycle: %s", stats)
        return 0

    log.info("pre_position starting (interval=%ds)", args.interval)
    while True:
        try:
            t0 = time.time()
            stats = run_cycle()
            elapsed = time.time() - t0
            if stats["games_targetable"] > 0 or stats["scoring_plays"] > 0:
                log.info("cycle in %.1fs: %s", elapsed, stats)
        except KeyboardInterrupt:
            log.info("interrupted")
            return 0
        except Exception as e:
            log.exception("cycle failed: %s", e)
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    sys.exit(main())
