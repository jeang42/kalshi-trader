"""Tennis prediction sidecar — 3rd sidecar, first individual sport.

Elo-based (``tennis.elo``), not team-Pythagorean. Public surface mirrors
the team sidecars where it can:

* :func:`predict_match_by_names` — P(player1 beats player2) from Elo.
* :func:`predict_for_ticker` — model's P(YES) for a Kalshi tennis ticker.
* :func:`ratings_table` — players by Elo.
* :func:`resolve_pending_tickers` — fast resolution from the matches table
  (ticker → date+code → finished match → winner), short-circuiting Kalshi's
  ~4-day settlement.

Plus the Sidecar operational surface (NAME/DISPLAY_NAME/health/stats/
recent_activity).

**Ticker bridge — the hard part (see docs/sports-model-playbook.md).**
Kalshi tennis tickers (`KXATPMATCH-{YY}{MON}{DD}{P1P2}-{CODE}`) use
3-letter player CODES that DON'T reliably map to player names (matching
"BER" by surname prefix returns Bergs, not Berrettini). So we ignore the
code entirely and read the **full player name from the market question**
("Will Matteo Berrettini win the …") — Kalshi provides it. We full-name
match that against the Elo player table; on no-match or ambiguity we SKIP
and log, never forecasting on a wrong player.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from pm.core.sidecars import (
    SidecarActivityEvent,
    SidecarHealth,
    register_sidecar,
    safe_health,
)
from tennis import model, state

log = logging.getLogger(__name__)

NAME = "tennis"
DISPLAY_NAME = "🎾 Tennis"
STALE_AFTER_HOURS = 36

# "Will <NAME> win the <P1> vs <P2>: <round>" → capture <NAME>.
_Q_RGX = re.compile(r"^Will\s+(?P<name>.+?)\s+win\s+the\s+", re.IGNORECASE)
_TICKER_TOUR = {"KXATPMATCH": "atp", "KXWTAMATCH": "wta"}
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _name_from_question(question: str) -> str | None:
    m = _Q_RGX.match(question or "")
    return m.group("name").strip() if m else None


def _tour_from_ticker(ticker: str) -> str | None:
    for prefix, tour in _TICKER_TOUR.items():
        if (ticker or "").startswith(prefix):
            return tour
    return None


def _resolve_player(name: str, tour: str | None) -> dict[str, Any] | None:
    """Full-name match to the Elo player table. None on no-match/ambiguity
    (caller skips — never guess)."""
    rows = state.find_player_by_name(name, tour)
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        log.warning("tennis: ambiguous player name %r (%d) — skipping",
                    name, len(rows))
    return None


def _resolve_opponent(short_name: str, tour: str, *, exclude_id: int) -> dict[str, Any] | None:
    """Resolve the opponent from a short/surname against rated players.
    Unique surname match (excluding the YES player) wins; else None."""
    surname = short_name.split()[-1].lower()
    hits = [p for p in state.ratings_table(tour, limit=2000)
            if p["id"] != exclude_id and surname in p["name"].lower()]
    return hits[0] if len(hits) == 1 else None


# ─── Prediction surface ──────────────────────────────────────────────────


def predict_match_by_names(p1_name: str, p2_name: str, *,
                           tour: str | None = None,
                           surface: str = "unknown") -> dict[str, Any] | None:
    """P(p1 beats p2) by player name. None if either player isn't rated."""
    state.migrate()
    p1 = _resolve_player(p1_name, tour)
    p2 = _resolve_player(p2_name, tour)
    pred = model.predict_match(p1, p2, surface=surface)
    if pred is None:
        return None
    return {
        "p1_win_prob": pred.p1_win_prob,
        "p1_name": pred.p1_name, "p2_name": pred.p2_name,
        "p1_elo": pred.p1_elo, "p2_elo": pred.p2_elo,
        "surface": surface, "as_of": datetime.now(UTC).isoformat(),
        "inputs": pred.to_inputs(), "source": "elo",
    }


def predict_for_ticker(ticker: str, question: str | None = None) -> dict[str, Any] | None:
    """Model's P(YES on this ticker). The YES player's name comes from the
    market ``question``; the opponent from its "<P1> vs <P2>" clause.
    Returns None if not a tennis ticker, no question, or a player isn't
    rated/unambiguous."""
    tour = _tour_from_ticker(ticker)
    if tour is None or not question:
        return None
    yes_name = _name_from_question(question)
    if not yes_name:
        return None
    vs = re.search(r"win the\s+(.+?)\s+vs\s+(.+?):", question or "")
    if not vs:
        return None
    a, b = vs.group(1).strip(), vs.group(2).strip()
    state.migrate()
    yes_row = _resolve_player(yes_name, tour)
    if yes_row is None:
        return None
    yes_surname = yes_name.split()[-1].lower()
    opp_short = b if (a.lower() in yes_name.lower() or yes_surname in a.lower()) else a
    opp_row = _resolve_opponent(opp_short, tour, exclude_id=yes_row["id"])
    if opp_row is None:
        return None
    pred = model.predict_match(yes_row, opp_row)
    if pred is None:
        return None
    return {
        "yes_prob": pred.p1_win_prob,
        "yes_name": yes_row["name"], "opponent": opp_row["name"],
        "yes_elo": pred.p1_elo, "opp_elo": pred.p2_elo,
        # match counts + ranks let strategies guard against unstable Elo
        # (small-sample / K-decay inflation — see the value strategy).
        "yes_matches": yes_row.get("matches_played"),
        "opp_matches": opp_row.get("matches_played"),
        "yes_rank": yes_row.get("rank"), "opp_rank": opp_row.get("rank"),
        "tour": tour, "source": "elo",
    }


def ratings_table(tour: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
    state.migrate()
    return state.ratings_table(tour, limit=limit)


# Raw ticker parse for resolution: capture date + the YES code. Resolution
# can't pass the question, so unlike predict_for_ticker we DO use the code
# here — but only to pick between the TWO players in an already-found match,
# which is safe even though open-field code matching isn't.
_RESOLVE_RGX = re.compile(
    r"^KX(?P<tour>ATP|WTA)MATCH-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})"
    r"(?P<blob>[A-Z]+)-(?P<code>[A-Z]{2,4})$"
)


def resolve_pending_tickers(tickers: list[str]) -> dict[str, bool]:
    """Sidecar hook: resolve Kalshi tennis tickers from the matches table
    (Elo backfill), short-circuiting Kalshi's ~4-day settlement.

    For each ticker: parse → (Eastern date, YES code). Find FINISHED matches
    on that ET date; the one whose two players' surnames the ticker blob
    contains is the match; the YES code picks which of those two is the YES
    side; return whether that player won.

    Safety: a code is only ever matched against the **two players already in
    a found match** (a closed 2-way choice), so the unreliable open-field
    code matching isn't a risk here. Anything not confidently resolvable
    (no finished match, code matches neither/both, unnamed qualifiers) is
    omitted — the auto-resolver falls back to Kalshi settlement. Never raises."""
    out: dict[str, bool] = {}
    if not tickers:
        return out
    try:
        state.migrate()
    except Exception:  # noqa: BLE001
        return out

    from datetime import date as _date
    from datetime import datetime as _dt
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo("America/New_York")

    by_date: dict[str, list[tuple[str, str, str, str]]] = {}
    for t in tickers:
        m = _RESOLVE_RGX.match(t or "")
        if m is None:
            continue
        try:
            d = _date(2000 + int(m.group("yy")), _MONTHS[m.group("mon")],
                      int(m.group("dd"))).isoformat()
        except (KeyError, ValueError):
            continue
        by_date.setdefault(d, []).append(
            (t, m.group("tour").lower(), m.group("blob"), m.group("code")))

    for et_date, entries in by_date.items():
        try:
            d0 = _date.fromisoformat(et_date)
            window = state.finished_matches_in_range(
                (d0 - timedelta(days=1)).isoformat(),
                (d0 + timedelta(days=1)).isoformat())
        except Exception:  # noqa: BLE001
            continue
        day_matches = []
        for mm in window:
            try:
                if _dt.fromisoformat(mm["date"]).astimezone(eastern).date() == d0:
                    day_matches.append(mm)
            except (ValueError, TypeError):
                continue
        for ticker, tour, blob, code in entries:
            try:
                res = _resolve_one(tour, blob, code, day_matches)
                if res is not None:
                    out[ticker] = res
            except Exception:  # noqa: BLE001
                continue
    return out


def _resolve_one(tour: str, blob: str, code: str,
                 day_matches: list[dict[str, Any]]) -> bool | None:
    """Resolve one ticker against the day's finished matches → yes_won, or
    None when not confidently resolvable."""
    code = code.lower()
    bl = blob.lower()
    for mm in day_matches:
        if mm.get("tour") != tour:
            continue
        p1n, p2n = mm.get("p1_name"), mm.get("p2_name")
        if not p1n or not p2n:
            continue  # unnamed qualifier — skip
        s1, s2 = p1n.split()[-1].lower(), p2n.split()[-1].lower()
        # the ticker blob must reference both players (its matchup field)
        if not (s1[:3] in bl and s2[:3] in bl):
            continue
        # the YES code must match exactly one of the two players
        c1, c2 = code in s1, code in s2
        if c1 == c2:
            return None  # ambiguous — don't guess
        yes_is_p1 = c1
        winner_is_p1 = mm.get("winner_id") == mm.get("p1_id")
        return bool(yes_is_p1 == winner_is_p1)
    return None


def refresh_all(force: bool = False) -> dict[str, Any]:
    from tennis import runner
    return runner.run_cycle(force=force)


# ─── Sidecar operational surface ─────────────────────────────────────────


def _compute_health() -> SidecarHealth:
    state.migrate()
    last = state.get_meta("last_refresh_ts")
    if not last:
        return SidecarHealth(status="degraded",
                             detail="never refreshed — run `python -m tennis.runner --backfill`")
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return SidecarHealth(status="degraded", detail=f"bad last_refresh_ts: {last}")
    age_h = (datetime.now(UTC) - last_dt).total_seconds() / 3600
    if age_h > STALE_AFTER_HOURS:
        return SidecarHealth(status="down", last_update_ts=last_dt,
                             detail=f"stale: {age_h:.0f}h since refresh")
    return SidecarHealth(status="ok", last_update_ts=last_dt,
                         detail="all systems nominal")


def health() -> SidecarHealth:
    return safe_health(_compute_health)


def stats() -> dict[str, Any]:
    state.migrate()
    # Model accuracy = the walk-forward (out-of-sample) Brier persisted by the
    # runner over the full matches table. The ticker-keyed predictions table
    # is never populated (the sidecar can't see Kalshi tickers), so that path
    # always read 0 — this scores the Elo against actual match results.
    wf = state.get_meta("walk_forward_brier")
    wf_n = state.get_meta("walk_forward_brier_n")
    return {
        "atp_rated": len(state.ratings_table("atp", limit=2000)),
        "wta_rated": len(state.ratings_table("wta", limit=2000)),
        "matches": len(state.all_matches()),
        "brier_n": int(wf_n) if wf_n else 0,
        "brier": float(wf) if wf else None,
    }


def recent_activity(limit: int = 20) -> list[SidecarActivityEvent]:
    import json
    state.migrate()
    out = []
    for r in state.get_recent_activity(limit):
        detail = {}
        if r.get("detail_json"):
            try:
                detail = json.loads(r["detail_json"])
            except (ValueError, TypeError):
                detail = {}
        try:
            ts = datetime.fromisoformat(r["ts"])
        except (ValueError, TypeError):
            ts = datetime.now(UTC)
        out.append(SidecarActivityEvent(ts=ts, kind=r["kind"],
                                        message=r["message"], detail=detail))
    return out


register_sidecar(__name__)
