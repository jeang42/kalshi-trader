"""Pre-computed insights / aggregates used by the /stats page and
optionally injected into the LLM chat context.

Pure read-only queries against the sidecar's SQLite — same engine the
rest of the sidecar uses, no new dependencies. Heaviest operations
(current-market edges) are cached with a 5-min TTL so repeated UI
refreshes don't hammer Kalshi.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import UTC
from typing import Any

from baseball import state

log = logging.getLogger(__name__)

# ─── Lightweight TTL cache ────────────────────────────────────────────────

_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn):
    """Toy cache: ``_cached(key, ttl_seconds, lambda: expensive_call())``."""
    now = time.time()
    hit = _CACHE.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _CACHE[key] = (now, value)
    return value


# ─── Aggregates from sidecar state (cheap, no network) ────────────────────


def league_leaders_offense(*, limit: int = 10) -> list[dict[str, Any]]:
    """Total runs scored across all loaded (final) games, grouped by team.
    Includes both home + away halves. ``runs_per_game`` is the rate stat."""
    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT t.id, t.abbreviation, t.name, t.short_name,
                   SUM(CASE WHEN g.home_team_id = t.id
                            THEN COALESCE(g.home_score, 0)
                            ELSE COALESCE(g.away_score, 0) END) AS runs,
                   COUNT(DISTINCT g.game_pk) AS gp
            FROM teams t
            JOIN games g ON g.home_team_id = t.id OR g.away_team_id = t.id
            WHERE g.status = 'Final'
            GROUP BY t.id
            HAVING gp > 0
            ORDER BY runs * 1.0 / gp DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [
        {
            "team_id": r["id"],
            "abbreviation": r["abbreviation"],
            "name": r["name"],
            "short_name": r["short_name"],
            "runs": int(r["runs"]),
            "games": int(r["gp"]),
            "runs_per_game": r["runs"] / max(r["gp"], 1),
        }
        for r in rows
    ]


def league_leaders_pitching(*, limit: int = 10) -> list[dict[str, Any]]:
    """Total runs ALLOWED across all loaded (final) games, grouped by team."""
    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT t.id, t.abbreviation, t.name, t.short_name,
                   SUM(CASE WHEN g.home_team_id = t.id
                            THEN COALESCE(g.away_score, 0)
                            ELSE COALESCE(g.home_score, 0) END) AS runs_allowed,
                   COUNT(DISTINCT g.game_pk) AS gp
            FROM teams t
            JOIN games g ON g.home_team_id = t.id OR g.away_team_id = t.id
            WHERE g.status = 'Final'
            GROUP BY t.id
            HAVING gp > 0
            ORDER BY runs_allowed * 1.0 / gp ASC
            LIMIT ?
        """, (limit,)).fetchall()
    return [
        {
            "team_id": r["id"],
            "abbreviation": r["abbreviation"],
            "name": r["name"],
            "short_name": r["short_name"],
            "runs_allowed": int(r["runs_allowed"]),
            "games": int(r["gp"]),
            "runs_allowed_per_game": r["runs_allowed"] / max(r["gp"], 1),
        }
        for r in rows
    ]


def team_event_leaders(event_type: str, side: str = "batting",
                        *, limit: int = 10) -> list[dict[str, Any]]:
    """Count of plays of ``event_type`` attributable to each team.

    side='batting' attributes to the team at bat (top → away, bottom → home).
    side='pitching' attributes to the defensive team."""
    if side not in ("batting", "pitching"):
        raise ValueError(f"side must be 'batting' or 'pitching', got {side!r}")
    if side == "batting":
        sql = """
            SELECT
              CASE WHEN lp.half_inning = 'top' THEN ta.abbreviation
                   ELSE th.abbreviation END AS team_abbr,
              CASE WHEN lp.half_inning = 'top' THEN ta.short_name
                   ELSE th.short_name END AS team_name,
              CASE WHEN lp.half_inning = 'top' THEN ta.id
                   ELSE th.id END AS team_id,
              COUNT(*) AS n
            FROM live_plays lp
            JOIN games g ON g.game_pk = lp.game_pk
            JOIN teams ta ON ta.id = g.away_team_id
            JOIN teams th ON th.id = g.home_team_id
            WHERE lp.event_type = ?
            GROUP BY team_abbr
            ORDER BY n DESC
            LIMIT ?
        """
    else:
        sql = """
            SELECT
              CASE WHEN lp.half_inning = 'top' THEN th.abbreviation
                   ELSE ta.abbreviation END AS team_abbr,
              CASE WHEN lp.half_inning = 'top' THEN th.short_name
                   ELSE ta.short_name END AS team_name,
              CASE WHEN lp.half_inning = 'top' THEN th.id
                   ELSE ta.id END AS team_id,
              COUNT(*) AS n
            FROM live_plays lp
            JOIN games g ON g.game_pk = lp.game_pk
            JOIN teams ta ON ta.id = g.away_team_id
            JOIN teams th ON th.id = g.home_team_id
            WHERE lp.event_type = ?
            GROUP BY team_abbr
            ORDER BY n DESC
            LIMIT ?
        """
    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (event_type, limit)).fetchall()
    return [
        {
            "team_id": r["team_id"], "abbreviation": r["team_abbr"],
            "name": r["team_name"], "n": int(r["n"]),
        }
        for r in rows if r["team_abbr"]
    ]


def one_run_games(*, limit: int = 10) -> list[dict[str, Any]]:
    """Teams ranked by share of their final games decided by exactly 1 run.
    High share is a 'high-variance' marker: lots of clutch wins or
    heart-breaking losses, model implications either way."""
    with sqlite3.connect(state._state_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT t.id, t.abbreviation, t.name, t.short_name,
                   SUM(CASE WHEN ABS(g.home_score - g.away_score) = 1 THEN 1 ELSE 0 END) AS one_run,
                   COUNT(*) AS gp
            FROM teams t
            JOIN games g ON g.home_team_id = t.id OR g.away_team_id = t.id
            WHERE g.status = 'Final' AND g.home_score IS NOT NULL
            GROUP BY t.id
            HAVING gp > 0
            ORDER BY one_run * 1.0 / gp DESC, one_run DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [
        {
            "team_id": r["id"], "abbreviation": r["abbreviation"],
            "name": r["name"], "short_name": r["short_name"],
            "one_run": int(r["one_run"]), "games": int(r["gp"]),
            "pct": r["one_run"] / max(r["gp"], 1),
        }
        for r in rows
    ]


def data_coverage() -> dict[str, Any]:
    """Counts + date range for the stats banner."""
    with sqlite3.connect(state._state_path()) as conn:
        teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
        n_plays = conn.execute("SELECT COUNT(*) FROM live_plays").fetchone()[0]
        n_games = conn.execute("SELECT COUNT(DISTINCT game_pk) FROM live_plays").fetchone()[0]
        n_final_games = conn.execute(
            "SELECT COUNT(*) FROM games WHERE status = 'Final'"
        ).fetchone()[0]
        r = conn.execute(
            "SELECT MIN(SUBSTR(game_date,1,10)), MAX(SUBSTR(game_date,1,10)) FROM games"
        ).fetchone()
    return {
        "teams_loaded": int(teams),
        "plays_total": int(n_plays),
        "games_with_plays": int(n_games),
        "final_games": int(n_final_games),
        "date_min": r[0] or "—",
        "date_max": r[1] or "—",
    }


# ─── Current-market edges (involves network — cached) ─────────────────────


def current_edges_for_date(date_iso: str, *, ttl: float = 300.0
                            ) -> list[dict[str, Any]]:
    """Live model-vs-market edges for the given UTC date. Hits Kalshi
    to pull current MLB market prices, then asks the sidecar's
    ``predict_for_ticker`` for the model's view of each.

    Cached for ``ttl`` seconds because the page hits this on every render
    and we don't want to spam Kalshi on dashboard refreshes.

    Failures are tolerated — a Kalshi outage shouldn't break the Stats
    page entirely.
    """
    key = f"current_edges:{date_iso}"

    def _fetch():
        try:
            return _current_edges_for_date_impl(date_iso)
        except Exception as e:  # noqa: BLE001
            log.warning("current_edges_for_date(%s) failed: %s", date_iso, e)
            return []

    return _cached(key, ttl, _fetch)


def _current_edges_for_date_impl(date_iso: str,
                                    tz_name: str = "America/Chicago"
                                    ) -> list[dict[str, Any]]:
    """Inner implementation that does the actual HTTP work. Designed to
    fail visibly — caller wraps in try/except for the public surface.

    ``date_iso`` is interpreted as a LOCAL calendar date in ``tz_name``.
    We pull every open MLB event, parse each ticker's encoded
    ``YYMMM DD HHMM`` date+time as UTC (Kalshi convention), convert
    to local time, and keep events whose local date matches.
    Significantly more robust than the prior UTC-prefix match, which
    missed late-evening local games that crossed midnight UTC."""
    import re
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    import requests

    from baseball import predict_for_ticker

    tz = ZoneInfo(tz_name)
    target_date = _dt.fromisoformat(date_iso).date() if "T" in date_iso else \
                  _dt.fromisoformat(date_iso + "T00:00:00").date()

    # Parse Kalshi event-ticker date+time portion. Format is
    # KXMLBGAME-<YY><MMM><DD><HHMM><teams>. EMPIRICAL FINDING: HHMM
    # and the date are US/Eastern (venue local time), NOT UTC. e.g.
    # ticker '...26MAY222215CWSSF' parses to UTC 02:15 May 23 which
    # matches state's CWS @ SF game at that exact UTC time. The
    # "22:15" is 10:15pm Eastern. Treating it as UTC would have
    # placed this game on the wrong local date.
    ticker_rgx = re.compile(
        r"^KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})"
    )
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    eastern = ZoneInfo("America/New_York")

    def _local_date_of(ticker: str):
        m = ticker_rgx.match(ticker or "")
        if not m:
            return None
        try:
            yyyy = 2000 + int(m.group(1))
            mm = months[m.group(2)]
            dd = int(m.group(3))
            hh = int(m.group(4))
            mn = int(m.group(5))
            # Ticker is venue-local (Eastern); convert to display tz
            eastern_dt = _dt(yyyy, mm, dd, hh, mn, tzinfo=eastern)
        except (KeyError, ValueError):
            return None
        return eastern_dt.astimezone(tz).date()

    r = requests.get(
        "https://api.elections.kalshi.com/trade-api/v2/events",
        params={"limit": 200, "series_ticker": "KXMLBGAME", "status": "open"},
        timeout=15,
    )
    r.raise_for_status()
    event_tickers = [
        e["event_ticker"] for e in r.json().get("events", [])
        if _local_date_of(e.get("event_ticker", "")) == target_date
    ]
    if not event_tickers:
        return []

    # Pull both YES sides per event
    rows: list[dict] = []
    for et in event_tickers:
        try:
            er = requests.get(
                f"https://api.elections.kalshi.com/trade-api/v2/events/{et}",
                params={"with_nested_markets": "true"}, timeout=15,
            )
            er.raise_for_status()
            for m in er.json().get("event", {}).get("markets", []) or []:
                ticker = m.get("ticker")
                if not ticker:
                    continue
                # Kalshi exposes prices as ``*_dollars`` strings.
                # The ``yes_bid`` / ``yes_ask`` plain fields are often
                # None even on active markets — the dollars variants
                # are the real source. Fall back to last_price when
                # the book is thin.
                yb = m.get("yes_bid_dollars") or m.get("yes_bid")
                ya = m.get("yes_ask_dollars") or m.get("yes_ask")
                last = m.get("last_price")
                try:
                    yb = float(yb) if yb is not None else None
                except (TypeError, ValueError):
                    yb = None
                try:
                    ya = float(ya) if ya is not None else None
                except (TypeError, ValueError):
                    ya = None
                if yb is not None and ya is not None and (yb > 0 or ya > 0):
                    yes_mid = (yb + ya) / 2.0
                elif last is not None:
                    try:
                        yes_mid = float(last) / 100.0  # last_price is cents
                    except (TypeError, ValueError):
                        continue
                else:
                    continue
                pred = predict_for_ticker(ticker)
                if pred is None:
                    continue
                model_yes = pred["yes_prob"]
                gap = model_yes - yes_mid
                rows.append({
                    "ticker": ticker,
                    "yes_team": pred["yes_team"],
                    "matchup": f"{pred['away']} @ {pred['home']}",
                    "market_yes": yes_mid,
                    "model_yes": model_yes,
                    "gap": gap,
                    "abs_gap": abs(gap),
                })
        except Exception as e:  # noqa: BLE001
            log.debug("event %s pull failed: %s", et, e)
    rows.sort(key=lambda x: x["abs_gap"], reverse=True)
    return rows


# ─── Combined "everything" dict for the LLM chat context ──────────────────


def context_for_llm(*, max_teams: int = 30,
                     max_edges_each: int = 5,
                     tz_name: str = "America/Chicago") -> dict[str, Any]:
    """Compact snapshot of EVERYTHING the LLM should know about current
    baseball state. Used to build the system prompt for chat.

    "Today"/"tomorrow" are LOCAL dates in ``tz_name`` (default Central)
    so the LLM doesn't reason as though it's already tomorrow during
    late-evening US play."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from baseball import team_ratings_table, today_schedule

    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz_name = "UTC"
        tz = UTC

    today = datetime.now(tz).date().isoformat()
    tomorrow = (datetime.now(tz).date() + timedelta(days=1)).isoformat()

    # Model track record
    brier_summary = _model_brier_summary()

    return {
        "as_of_utc": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
        "as_of_local": datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z"),
        "today_local": today,
        "tomorrow_local": tomorrow,
        "tz_name": tz_name,
        "coverage": data_coverage(),
        "model_track_record": brier_summary,
        "team_ratings": team_ratings_table()[:max_teams],
        "offense_leaders": league_leaders_offense(limit=8),
        "pitching_leaders": league_leaders_pitching(limit=8),
        "hr_leaders": team_event_leaders("Home Run", "batting", limit=6),
        "k_leaders": team_event_leaders("Strikeout", "pitching", limit=6),
        "one_run_leaders": one_run_games(limit=6),
        "today_games": today_schedule(),
        "today_edges": current_edges_for_date(today)[:max_edges_each],
        "tomorrow_edges": current_edges_for_date(tomorrow)[:max_edges_each],
    }


def _model_brier_summary() -> dict[str, Any]:
    """Brier score summary by model version for the LLM context."""
    try:
        import sqlite3

        from baseball.state import _state_path
        with sqlite3.connect(_state_path()) as conn:
            rows = conn.execute("""
                SELECT model_version, COUNT(*) as total,
                       SUM(CASE WHEN actual_winner IS NOT NULL THEN 1 ELSE 0 END) as settled,
                       AVG(CASE WHEN actual_winner IS NOT NULL THEN
                           (home_win_prob - CASE WHEN actual_winner = home_team
                            THEN 1.0 ELSE 0.0 END)
                           * (home_win_prob - CASE WHEN actual_winner = home_team
                              THEN 1.0 ELSE 0.0 END)
                       END) as brier
                FROM predictions
                GROUP BY model_version ORDER BY model_version
            """).fetchall()
            return {
                "by_version": [
                    {"version": r[0], "total": r[1], "settled": r[2],
                     "brier": round(r[3], 4) if r[3] else None}
                    for r in rows
                ],
                "coin_flip_baseline": 0.25,
            }
    except Exception:
        return {}
