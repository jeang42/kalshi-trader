"""Lead-lag analysis: paired plays × Kalshi book snapshots.

Bridges the two streams the system collects:

* ``baseball_state.live_plays`` — MLB play-by-play, keyed on
  ``(game_pk, play_id)`` with ``ts`` (UTC ISO).
* ``book_archive.book_snapshots`` — Kalshi order books, keyed on
  ``(venue, market_id, token_id, snapshot_ts)`` where ``market_id``
  is a Kalshi ticker like ``KXMLBGAME-26MAY231610NYMMIA-NYM``.

The two tables don't share a key. The link is **(game_date, matchup
abbreviation pair)** — parseable from the Kalshi ticker and queryable
from ``games``. This module owns that mapping and exposes the
right SQL joins for analysis.

Three primary functions:

* :func:`tickers_for_game` — which Kalshi market tickers correspond
  to one MLB ``game_pk``.
* :func:`book_around_play` — for a single play, return the book
  trajectory in a window before+after.
* :func:`reaction_summary` — aggregate "did the book move within N
  seconds after event X" stats across many games.

Caveats baked in:

* Book data is **forward-only** — ``mlb_book_poller`` started recently.
  The 30-day play-by-play backfill has no matching book data; analysis
  effectively starts the day the poller did.
* Lead-lag signals need lots of paired data. Until ~1-2 weeks of
  forward collection have accumulated, every result is noisy. The
  functions return what they have; the caller must apply judgement
  to small samples.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from baseball import state

log = logging.getLogger(__name__)

BOOK_ARCHIVE_PATH = os.environ.get(
    "BOOK_ARCHIVE_PATH", "data/shared/book_archive.sqlite",
)


# ─── Ticker bridge ────────────────────────────────────────────────────────

_TICKER_RGX = re.compile(
    r"^KXMLBGAME-(?P<yy>\d{2})(?P<mmm>[A-Z]{3})(?P<dd>\d{2})"
    r"\d{4}(?P<matchup>[A-Z]+)-(?P<yes>[A-Z]{2,4})$"
)
_MONTH_ABBR = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def tickers_for_game(game_pk: int) -> list[str]:
    """Both Kalshi market tickers (YES on each team) corresponding to
    one MLB game_pk. Looks up the game in baseball state, then walks
    book_archive for tickers that match the game's (date, teams).

    Returns ``[]`` if the game isn't in state or we have no book
    snapshots for that matchup yet. Order is undefined — usually
    YES-home before YES-away but caller shouldn't rely on it."""
    g = _get_game_row(game_pk)
    if g is None:
        return []

    # Game date (UTC) and team abbreviations
    try:
        game_dt = datetime.fromisoformat(g["game_date"])
    except ValueError:
        return []
    # Kalshi tickers use EASTERN time in their date+time portion (see
    # insights.py's same finding). Convert UTC → Eastern → take the
    # date.
    from zoneinfo import ZoneInfo
    eastern_date = game_dt.astimezone(ZoneInfo("America/New_York")).date()
    home = str(g.get("home_abbr", "")).upper()
    away = str(g.get("away_abbr", "")).upper()
    if not (home and away):
        return []

    # Tickers in book_archive whose YY/MMM/DD match this Eastern date
    # AND whose matchup contains both team abbrs.
    yy = eastern_date.year % 100
    mmm = ["", "JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG",
           "SEP","OCT","NOV","DEC"][eastern_date.month]
    prefix = f"KXMLBGAME-{yy:02d}{mmm}{eastern_date.day:02d}"

    out: set[str] = set()
    with sqlite3.connect(BOOK_ARCHIVE_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT market_id FROM book_snapshots "
            "WHERE market_id LIKE ? AND venue = 'kalshi'",
            (prefix + "%",),
        ).fetchall()
    for (mid,) in rows:
        m = _TICKER_RGX.match(mid)
        if not m:
            continue
        matchup = m.group("matchup")
        if home in matchup and away in matchup:
            out.add(mid)
    return sorted(out)


def _get_game_row(game_pk: int) -> dict[str, Any] | None:
    """Fetch a game row joined with team abbreviations. Returns None
    if not in state. Reads directly to avoid querying through the
    games-for-date filter which would limit results to one calendar day."""
    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("""
            SELECT g.*, ta.abbreviation AS away_abbr,
                          th.abbreviation AS home_abbr
            FROM games g
            JOIN teams ta ON ta.id = g.away_team_id
            JOIN teams th ON th.id = g.home_team_id
            WHERE g.game_pk = ?
        """, (game_pk,)).fetchone()
        return dict(r) if r else None


# ─── Book-around-play ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class BookPoint:
    """One book snapshot in a play's surrounding window. ``mid`` is
    the YES-side midpoint price; useful for lead-lag plots."""
    ticker: str
    snapshot_ts: int           # unix seconds UTC
    seconds_from_play: float   # signed: negative = before, positive = after
    best_bid: float | None
    best_ask: float | None
    mid: float | None


def book_around_play(
    game_pk: int, play_id: int, *,
    window_minutes: float = 5.0,
) -> list[BookPoint]:
    """All book snapshots within ``window_minutes`` of the play's
    timestamp, across both YES-side tickers for the game.

    Returns ``[]`` if the play isn't found, no tickers map to the
    game, or no snapshots fall in the window. Caller sorts as needed;
    the natural sort is by ``snapshot_ts``."""
    play_ts = _play_ts(game_pk, play_id)
    if play_ts is None:
        return []

    tickers = tickers_for_game(game_pk)
    if not tickers:
        return []

    window_s = window_minutes * 60
    lo = int(play_ts.timestamp() - window_s)
    hi = int(play_ts.timestamp() + window_s)

    out: list[BookPoint] = []
    with sqlite3.connect(BOOK_ARCHIVE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(tickers))
        rows = conn.execute(
            f"SELECT market_id, snapshot_ts, book_json FROM book_snapshots "
            f"WHERE venue='kalshi' AND market_id IN ({placeholders}) "
            f"AND snapshot_ts BETWEEN ? AND ? "
            f"ORDER BY snapshot_ts",
            (*tickers, lo, hi),
        ).fetchall()
    for r in rows:
        bb, ba, mid = _book_summary(r["book_json"])
        out.append(BookPoint(
            ticker=r["market_id"],
            snapshot_ts=int(r["snapshot_ts"]),
            seconds_from_play=int(r["snapshot_ts"]) - play_ts.timestamp(),
            best_bid=bb, best_ask=ba, mid=mid,
        ))
    return out


def _play_ts(game_pk: int, play_id: int) -> datetime | None:
    with sqlite3.connect(state._state_path()) as conn:
        r = conn.execute(
            "SELECT ts FROM live_plays WHERE game_pk = ? AND play_id = ?",
            (game_pk, play_id),
        ).fetchone()
        if r is None:
            return None
        try:
            dt = datetime.fromisoformat(r[0].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt


def _book_summary(book_json: str) -> tuple[float | None, float | None, float | None]:
    """Pull best bid + best ask + midpoint from a stored book JSON."""
    try:
        rows = json.loads(book_json)
    except (json.JSONDecodeError, TypeError):
        return None, None, None
    bids = [r for r in rows if r.get("side") == "bid"]
    asks = [r for r in rows if r.get("side") == "ask"]
    bb = max((float(r["price"]) for r in bids), default=None)
    ba = min((float(r["price"]) for r in asks), default=None)
    mid = None
    if bb is not None and ba is not None and bb > 0 and ba > 0:
        mid = (bb + ba) / 2.0
    elif ba is not None:
        mid = ba / 2.0  # bid is 0 — implied mid is half the ask
    return bb, ba, mid


# ─── Reaction-time aggregation ────────────────────────────────────────────


def reaction_summary(
    event_type: str, *,
    window_minutes: float = 10.0,
    min_pct_change: float = 0.02,
    require_score_change: bool = True,
) -> dict[str, Any]:
    """Across every play of ``event_type``, count how often the book
    moved by ≥``min_pct_change`` (in YES midpoint price) within the
    window after the play.

    ``require_score_change=True`` (default) restricts the population
    to plays that ACTUALLY changed the score (RBIs, runs scored, the
    final out of an inning that strands a runner, etc). This kills the
    biggest noise source in the naive version: non-scoring plays
    (Mound Visit, Batter Timeout, Pitching Substitution) that just
    happen to sit in the same time window as a real scoring event,
    which was incorrectly getting credit for the price move.

    Set ``require_score_change=False`` to revert to the v1 behavior
    (any play of ``event_type`` counted) for completeness or for event
    types where the metric makes sense without scoring (e.g. studying
    pitching changes as a leading indicator).

    Returns::

        {
          "n_plays": int,                    # plays of this type with book data
          "n_played_with_book": int,         # subset that had ANY book around them
          "n_reacted": int,                  # subset where book moved by min_pct
          "reaction_pct": float | None,      # n_reacted / n_played_with_book
          "median_reaction_seconds": float | None,
          "window_minutes": float,
          "min_pct_change": float,
          "require_score_change": bool,
        }

    For tiny samples (n_played_with_book < 5) the percent fields are
    None — calculating them would produce false confidence. Tells the
    caller "we don't have enough data yet" cleanly.
    """
    plays = (_score_changing_plays_of_type(event_type)
              if require_score_change else _plays_of_type(event_type))
    n_plays = len(plays)
    n_with_book = 0
    n_reacted = 0
    reaction_seconds: list[float] = []

    for game_pk, play_id in plays:
        points = book_around_play(game_pk, play_id,
                                    window_minutes=window_minutes)
        if not points:
            continue
        n_with_book += 1
        # Use the closest pre-play point as the baseline; check if any
        # post-play point moves ≥ min_pct from that baseline.
        pre = [p for p in points if p.seconds_from_play <= 0 and p.mid is not None]
        post = [p for p in points if p.seconds_from_play > 0 and p.mid is not None]
        if not pre or not post:
            continue
        baseline = pre[-1].mid  # closest pre-play sample
        # First post-play sample whose |mid - baseline| / baseline ≥ threshold
        reacted_secs: float | None = None
        for p in post:
            change = abs(p.mid - baseline) / max(baseline, 1e-9)
            if change >= min_pct_change:
                reacted_secs = p.seconds_from_play
                break
        if reacted_secs is not None:
            n_reacted += 1
            reaction_seconds.append(reacted_secs)

    reaction_pct: float | None = None
    median_sec: float | None = None
    if n_with_book >= 5:
        reaction_pct = n_reacted / n_with_book if n_with_book else 0.0
        if reaction_seconds:
            reaction_seconds.sort()
            mid = len(reaction_seconds) // 2
            median_sec = (reaction_seconds[mid]
                          if len(reaction_seconds) % 2 == 1
                          else (reaction_seconds[mid-1]+reaction_seconds[mid])/2)
    return {
        "event_type": event_type,
        "n_plays": n_plays,
        "n_played_with_book": n_with_book,
        "n_reacted": n_reacted,
        "reaction_pct": reaction_pct,
        "median_reaction_seconds": median_sec,
        "window_minutes": window_minutes,
        "min_pct_change": min_pct_change,
        "require_score_change": require_score_change,
    }


def _plays_of_type(event_type: str) -> list[tuple[int, int]]:
    """All (game_pk, play_id) tuples in live_plays of given event_type.
    Filters to plays we have a chance of pairing — i.e., plays from
    the period when both pollers were running. The actual filtering
    happens organically in book_around_play returning [] for
    unpaired plays."""
    with sqlite3.connect(state._state_path()) as conn:
        rows = conn.execute(
            "SELECT game_pk, play_id FROM live_plays WHERE event_type = ?",
            (event_type,),
        ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def _score_changing_plays_of_type(event_type: str) -> list[tuple[int, int]]:
    """Subset of ``_plays_of_type`` to plays that ACTUALLY changed the
    score — the previous play in the same game had a different
    home_score_after / away_score_after.

    This kills the biggest noise source in naive reaction analysis:
    non-scoring plays (mound visits, pitching changes, timeouts)
    sitting in the time window of a real scoring event were getting
    credited for the book's reprice. Now only the play that actually
    changed the score counts.

    For non-scoring event types (Strikeout, Walk, etc.) this returns
    the subset where the inning's score state changed — typically the
    third out of a half-inning where runners were left on. Less
    obvious causality but still ahead of "any play within the window."
    """
    with sqlite3.connect(state._state_path()) as conn:
        rows = conn.execute("""
            WITH plays AS (
              SELECT
                game_pk, play_id, event_type,
                home_score_after, away_score_after,
                LAG(home_score_after) OVER (PARTITION BY game_pk ORDER BY play_id)
                  AS prev_home,
                LAG(away_score_after) OVER (PARTITION BY game_pk ORDER BY play_id)
                  AS prev_away
              FROM live_plays
            )
            SELECT game_pk, play_id FROM plays
            WHERE event_type = ?
              AND (
                (home_score_after IS NOT NULL AND prev_home IS NOT NULL
                  AND home_score_after != prev_home)
                OR
                (away_score_after IS NOT NULL AND prev_away IS NOT NULL
                  AND away_score_after != prev_away)
              )
        """, (event_type,)).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


# ─── Top-line summary for the analysis page ───────────────────────────────


# ─── Game-situation features ──────────────────────────────────────────────
#
# Sabermetric convention: a game state is the tuple
#   (inning, half_inning, outs, base_state, score_diff)
# where base_state is a 3-bit encoding of runners on (1st/2nd/3rd) — 8
# distinct states. With 3 possible outs (0/1/2), that's 24 base-out
# states; multiplied by inning (1-9+) and signed score_diff, this is
# the natural feature space for win-probability work.
#
# We don't bundle Tom Tango's full WPA/LI lookup table here — it's
# multi-decade calibrated, available in baseballr / pybaseball, and
# would commit us to a specific reference dataset. Instead we expose
# the primitives + a small set of well-known leverage HEURISTICS:
#
#   - "late and close":   inning ≥ 7 AND |score_diff| ≤ 1 (or batter
#                         on deck represents tying/winning run)
#   - "high leverage":    bases loaded OR ≥2 runners w/ tying run on
#                         base in close game
#   - "garbage time":     |score_diff| ≥ 5 in late innings (book
#                         barely moves no matter what)
#
# Real LI/WPA integration later — TODO entry references the canonical
# tables. For now the heuristics are pragmatic enough for "is this
# play a clutch spot or not" segmentation.


@dataclass(frozen=True)
class GameSituation:
    """Decoded game state at the moment of one play. ``score_diff`` is
    home_score − away_score (signed). ``bases`` is a 3-element bool
    tuple (first, second, third) where True means runner is on after
    the play."""
    inning: int | None
    half_inning: str  # "top" | "bottom" | ""
    outs_after: int | None
    bases: tuple[bool, bool, bool]  # (1B, 2B, 3B)
    home_score: int | None
    away_score: int | None
    score_diff: int | None  # home − away
    captivating_index: int | None

    @property
    def runners_on(self) -> int:
        return sum(self.bases)

    @property
    def risp(self) -> bool:
        """Runners In Scoring Position (2nd or 3rd)."""
        return self.bases[1] or self.bases[2]

    @property
    def bases_loaded(self) -> bool:
        return all(self.bases)

    @property
    def is_late(self) -> bool:
        return self.inning is not None and self.inning >= 7

    @property
    def is_close(self) -> bool:
        """Within 1 run."""
        return self.score_diff is not None and abs(self.score_diff) <= 1

    @property
    def is_late_and_close(self) -> bool:
        """Standard MLB definition: late inning (7+) AND the tying run
        is at-bat, on-deck, or on-base.

        Math: ``abs(score_diff) <= 2 + runners_on``
          - diff=1, no runners → batter is tying run (1 ≤ 2) ✓
          - diff=2, no runners → on-deck batter is tying run (2 ≤ 2) ✓
          - diff=3, no runners → need a runner; not late-and-close ✗
          - diff=3, 1 runner   → tying run on 1st (3 ≤ 3) ✓
          - diff=4, 2 runners  → tying run on 2nd (4 ≤ 4) ✓

        This is the standard "save situation"-style definition used in
        baseball-reference's clutch splits and Tango's leverage tables.
        """
        if not self.is_late or self.score_diff is None:
            return False
        return abs(self.score_diff) <= 2 + self.runners_on

    @property
    def is_garbage_time(self) -> bool:
        """Late blowout. The book barely moves on any single play."""
        return (self.is_late
                and self.score_diff is not None
                and abs(self.score_diff) >= 5)

    @property
    def base_out_state(self) -> str:
        """Compact key for the 24-state matrix. e.g. "1_2outs" =
        runner on 1st, 2 outs. "123_0outs" = bases loaded, nobody out.
        """
        if self.outs_after is None:
            return "?"
        bases = "".join(b for b, on in zip("123", self.bases, strict=False) if on) or "_"
        return f"{bases}_{self.outs_after}outs"


def parse_situation(raw_json_str: str | dict | None) -> GameSituation:
    """Extract a GameSituation from a play's raw_json blob. Returns a
    GameSituation with all-None fields if the blob is missing or
    malformed — callers can still group by that null state."""
    if raw_json_str is None:
        return GameSituation(None, "", None, (False, False, False),
                              None, None, None, None)
    try:
        raw = json.loads(raw_json_str) if isinstance(raw_json_str, str) else raw_json_str
    except (json.JSONDecodeError, TypeError):
        return GameSituation(None, "", None, (False, False, False),
                              None, None, None, None)
    about = raw.get("about", {}) or {}
    result = raw.get("result", {}) or {}
    count = raw.get("count", {}) or {}
    matchup = raw.get("matchup", {}) or {}

    # Runners-on-base after this play. MLB puts the post-play state in
    # `matchup.postOnFirst/Second/Third` (dict with player info if
    # occupied, missing or None if empty).
    on_first = bool(matchup.get("postOnFirst"))
    on_second = bool(matchup.get("postOnSecond"))
    on_third = bool(matchup.get("postOnThird"))

    home_score = result.get("homeScore")
    away_score = result.get("awayScore")
    score_diff = None
    if home_score is not None and away_score is not None:
        try:
            score_diff = int(home_score) - int(away_score)
        except (TypeError, ValueError):
            pass

    return GameSituation(
        inning=about.get("inning"),
        half_inning=str(about.get("halfInning") or ""),
        outs_after=count.get("outs"),
        bases=(on_first, on_second, on_third),
        home_score=home_score,
        away_score=away_score,
        score_diff=score_diff,
        captivating_index=about.get("captivatingIndex"),
    )


def situation_for_play(game_pk: int, play_id: int) -> GameSituation:
    """Convenience accessor that pulls a single play and parses its
    situation. Returns the all-None GameSituation if the play isn't
    found."""
    with sqlite3.connect(state._state_path()) as conn:
        r = conn.execute(
            "SELECT raw_json FROM live_plays WHERE game_pk = ? AND play_id = ?",
            (game_pk, play_id),
        ).fetchone()
    if r is None:
        return GameSituation(None, "", None, (False, False, False),
                              None, None, None, None)
    return parse_situation(r[0])


def reaction_summary_by_situation(
    event_type: str | None = None,
    *,
    window_minutes: float = 10.0,
    min_pct_change: float = 0.02,
    require_score_change: bool = True,
    only: str = "all",  # "all" | "late_and_close" | "high_leverage" | "risp" | "bases_loaded"
) -> dict[str, Any]:
    """Same as :func:`reaction_summary` but with an additional
    situational filter applied to the play population.

    ``only`` values map to GameSituation predicates:

    * ``"all"``                — no situational filter (default)
    * ``"late_and_close"``     — 7th+ inning, tying run on base/deck
    * ``"high_leverage"``      — bases loaded OR RISP w/ close score
    * ``"risp"``               — runners in scoring position
    * ``"bases_loaded"``       — full bases
    * ``"garbage_time"``       — late blowout (5+ run diff)

    Useful for asking "does the book react more to HRs in late-and-
    close situations than in blowouts?" once enough data accumulates.
    """
    # Build the play population (same as reaction_summary)
    if event_type is None:
        # All events — useful for "any play with ≥X leverage"
        if require_score_change:
            plays = _all_score_changing_plays()
        else:
            plays = _all_plays()
    elif require_score_change:
        plays = _score_changing_plays_of_type(event_type)
    else:
        plays = _plays_of_type(event_type)

    # Filter by situation
    if only != "all":
        plays = _filter_by_situation(plays, only)

    # Reuse the same reaction-detection loop logic as reaction_summary
    n_with_book = 0
    n_reacted = 0
    reaction_seconds: list[float] = []
    for game_pk, play_id in plays:
        points = book_around_play(game_pk, play_id,
                                    window_minutes=window_minutes)
        if not points:
            continue
        n_with_book += 1
        pre = [p for p in points if p.seconds_from_play <= 0 and p.mid is not None]
        post = [p for p in points if p.seconds_from_play > 0 and p.mid is not None]
        if not pre or not post:
            continue
        baseline = pre[-1].mid
        for p in post:
            change = abs(p.mid - baseline) / max(baseline, 1e-9)
            if change >= min_pct_change:
                reaction_seconds.append(p.seconds_from_play)
                n_reacted += 1
                break

    reaction_pct: float | None = None
    median_sec: float | None = None
    if n_with_book >= 5:
        reaction_pct = n_reacted / n_with_book
        if reaction_seconds:
            reaction_seconds.sort()
            mid = len(reaction_seconds) // 2
            median_sec = (reaction_seconds[mid]
                          if len(reaction_seconds) % 2 == 1
                          else (reaction_seconds[mid-1]+reaction_seconds[mid])/2)
    return {
        "event_type": event_type or "(any)",
        "situation": only,
        "n_plays": len(plays),
        "n_played_with_book": n_with_book,
        "n_reacted": n_reacted,
        "reaction_pct": reaction_pct,
        "median_reaction_seconds": median_sec,
        "window_minutes": window_minutes,
        "min_pct_change": min_pct_change,
    }


def _all_plays() -> list[tuple[int, int]]:
    with sqlite3.connect(state._state_path()) as conn:
        rows = conn.execute(
            "SELECT game_pk, play_id FROM live_plays WHERE event_type IS NOT NULL"
        ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def _all_score_changing_plays() -> list[tuple[int, int]]:
    """Every play in any game where the score changed vs the prior play."""
    with sqlite3.connect(state._state_path()) as conn:
        rows = conn.execute("""
            WITH plays AS (
              SELECT game_pk, play_id, event_type,
                     home_score_after, away_score_after,
                LAG(home_score_after) OVER (PARTITION BY game_pk ORDER BY play_id) AS prev_home,
                LAG(away_score_after) OVER (PARTITION BY game_pk ORDER BY play_id) AS prev_away
              FROM live_plays
            )
            SELECT game_pk, play_id FROM plays
            WHERE event_type IS NOT NULL
              AND (
                (home_score_after IS NOT NULL AND prev_home IS NOT NULL
                  AND home_score_after != prev_home)
                OR
                (away_score_after IS NOT NULL AND prev_away IS NOT NULL
                  AND away_score_after != prev_away)
              )
        """).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def _filter_by_situation(
    plays: list[tuple[int, int]], situation: str,
) -> list[tuple[int, int]]:
    """Bulk-load raw_json for each play, parse situation, keep matches."""
    if not plays:
        return []
    out: list[tuple[int, int]] = []
    # Batch by game_pk to reduce round-trips
    with sqlite3.connect(state._state_path()) as conn:
        for gpk, pid in plays:
            r = conn.execute(
                "SELECT raw_json FROM live_plays WHERE game_pk = ? AND play_id = ?",
                (gpk, pid),
            ).fetchone()
            if r is None:
                continue
            sit = parse_situation(r[0])
            keep = False
            if situation == "late_and_close":
                keep = sit.is_late_and_close
            elif situation == "high_leverage":
                keep = sit.bases_loaded or (sit.risp and sit.is_close)
            elif situation == "risp":
                keep = sit.risp
            elif situation == "bases_loaded":
                keep = sit.bases_loaded
            elif situation == "garbage_time":
                keep = sit.is_garbage_time
            if keep:
                out.append((gpk, pid))
    return out


def base_out_state_distribution() -> dict[str, int]:
    """Across all score-changing plays in the DB, count how many fall
    into each of the 24 base-out states. Useful for a UI panel showing
    where scoring most commonly happens."""
    out: dict[str, int] = {}
    plays = _all_score_changing_plays()
    with sqlite3.connect(state._state_path()) as conn:
        for gpk, pid in plays:
            r = conn.execute(
                "SELECT raw_json FROM live_plays WHERE game_pk = ? AND play_id = ?",
                (gpk, pid),
            ).fetchone()
            if r is None:
                continue
            sit = parse_situation(r[0])
            key = sit.base_out_state
            out[key] = out.get(key, 0) + 1
    return out


def wpa_for_play(game_pk: int, play_id: int) -> float | None:
    """Win Probability Added for one play, batting team's perspective.

    Walks the prior play in the same game for the "before" state and
    parses this play for the "after" state. Returns None if either
    side can't be parsed.

    Positive WPA = play helped the batting team. Use the sign + the
    half-inning to determine whether the home or away team benefited.
    """
    from baseball import wpa as _wpa
    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT play_id, raw_json, home_score_after, away_score_after
               FROM live_plays
               WHERE game_pk = ? AND play_id <= ?
               ORDER BY play_id DESC LIMIT 2""",
            (game_pk, play_id),
        ).fetchall()
    if len(rows) < 2:
        return None  # no prior play to compare against
    this_play, prev_play = rows[0], rows[1]
    if this_play["play_id"] != play_id:
        return None

    sit_after = parse_situation(this_play["raw_json"])
    sit_before = parse_situation(prev_play["raw_json"])
    if sit_before.inning is None or sit_after.outs_after is None:
        return None
    try:
        return _wpa.estimate_wpa_for_play(
            inning_before=sit_before.inning or 1,
            half_before=sit_before.half_inning or "top",
            outs_before=sit_before.outs_after or 0,
            bases_before=sit_before.bases,
            home_score_before=prev_play["home_score_after"] or 0,
            away_score_before=prev_play["away_score_after"] or 0,
            outs_after=sit_after.outs_after,
            bases_after=sit_after.bases,
            home_score_after=this_play["home_score_after"] or 0,
            away_score_after=this_play["away_score_after"] or 0,
        )
    except Exception:  # noqa: BLE001
        return None


def top_wpa_plays(*, limit: int = 20, min_abs_wpa: float = 0.05
                    ) -> list[dict[str, Any]]:
    """Highest-|WPA| plays in the database. Surfaces the "this play
    mattered" rows that are the natural targets for lead-lag
    analysis.

    Walks each score-changing play (cheap query) + computes WPA on
    each. With ~870 HR-class score-changing plays in our 31k-play
    sample, this finishes in a few seconds and gives the user a
    real "what shook things up" view.
    """
    # Only consider score-changing plays — the rest have WPA dominated
    # by noise / mound visits in score-change windows.
    plays = _all_score_changing_plays()
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        for gpk, pid in plays:
            w = wpa_for_play(gpk, pid)
            if w is None or abs(w) < min_abs_wpa:
                continue
            r = conn.execute(
                """SELECT lp.event_type, lp.description, lp.ts,
                          lp.home_score_after, lp.away_score_after,
                          ta.abbreviation AS away, th.abbreviation AS home
                   FROM live_plays lp
                   JOIN games g ON g.game_pk = lp.game_pk
                   JOIN teams ta ON ta.id = g.away_team_id
                   JOIN teams th ON th.id = g.home_team_id
                   WHERE lp.game_pk = ? AND lp.play_id = ?""",
                (gpk, pid),
            ).fetchone()
            if r is None:
                continue
            rows.append({
                "game_pk": gpk, "play_id": pid,
                "event_type": r["event_type"], "description": r["description"],
                "ts": r["ts"], "wpa": w,
                "score": f"{r['away_score_after']}-{r['home_score_after']}",
                "matchup": f"{r['away']} @ {r['home']}",
            })
    rows.sort(key=lambda r: abs(r["wpa"]), reverse=True)
    return rows[:limit]


def coverage_summary() -> dict[str, Any]:
    """How much paired data do we have? Shown at the top of the UI so
    the user knows whether the rest of the page is meaningful or noisy."""
    with sqlite3.connect(state._state_path()) as conn:
        n_plays = conn.execute("SELECT COUNT(*) FROM live_plays").fetchone()[0]
    with sqlite3.connect(BOOK_ARCHIVE_PATH) as conn:
        n_mlb_books = conn.execute(
            "SELECT COUNT(*) FROM book_snapshots WHERE market_id LIKE 'KXMLBGAME-%'"
        ).fetchone()[0]
        first_ts = conn.execute(
            "SELECT MIN(snapshot_ts) FROM book_snapshots WHERE market_id LIKE 'KXMLBGAME-%'"
        ).fetchone()[0]
    return {
        "total_plays": int(n_plays),
        "total_mlb_book_snapshots": int(n_mlb_books),
        "book_collection_started_ts": int(first_ts) if first_ts else None,
        "book_collection_started_iso": (
            datetime.fromtimestamp(int(first_ts), tz=UTC).isoformat()
            if first_ts else None
        ),
    }


def event_type_counts(*, since_ts: int | None = None) -> list[dict[str, Any]]:
    """Distribution of event types in plays. If ``since_ts`` provided,
    filters to plays whose fetched_at >= since (use book collection
    start to focus on pairable data)."""
    with sqlite3.connect(state._state_path()) as conn:
        if since_ts:
            since_iso = datetime.fromtimestamp(
                since_ts, tz=UTC).isoformat()
            rows = conn.execute("""
                SELECT event_type, COUNT(*) AS n
                FROM live_plays
                WHERE fetched_at >= ?
                GROUP BY event_type
                ORDER BY n DESC
                LIMIT 20
            """, (since_iso,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT event_type, COUNT(*) AS n
                FROM live_plays
                GROUP BY event_type
                ORDER BY n DESC
                LIMIT 20
            """).fetchall()
    return [{"event_type": r[0] or "(none)", "n": int(r[1])} for r in rows]
