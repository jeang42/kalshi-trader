"""FastAPI web UI for the baseball sidecar.

Multi-user safe (FastAPI handles concurrent requests out of the box —
unlike Streamlit). HTMX for the few interactive bits (refresh button,
date picker) so we don't need a JS toolchain. Gated by the WEB_UI_TOKEN
shared secret (pm/core/web_auth.py); with no token configured it binds
loopback only — set WEB_UI_TOKEN (or WEB_BIND_HOST) for LAN access.

Run::

    python -m baseball.web          # binds per web_auth.default_bind_host()
    # or
    uvicorn baseball.web:app --port 8770

Theme: classic baseball — diamond green, dirt brown, accent on the
red of stitched ball seams. Team logos from MLB's public CDN
(``mlbstatic.com/team-logos/{id}.svg``).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from datetime import date as date_t
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import baseball
from pm.core import web_auth

# Load .env BEFORE anything reads configuration. These apps are started by
# systemd units that supply no EnvironmentFile, so without this WEB_UI_TOKEN
# (and every other .env setting) is simply absent in the served process — the
# gate below would report itself open while the operator believed it closed.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # optional dependency; the app still runs unconfigured
    pass

from baseball import state

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

app = FastAPI(title="⚾ Baseball Sidecar", version="0.1.0")

# LAN-facing with no authentication until 2026-08-11. Shared-secret gate;
# a no-op unless WEB_UI_TOKEN is set. See web_auth.py for what this is and,
# more importantly, what it is not.

web_auth.protect(app, app_name="Baseball UI")
app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")


# ─── Jinja helpers ────────────────────────────────────────────────────────


def _fmt_pct(p: float | None) -> str:
    return f"{p * 100:.0f}%" if p is not None else "—"


def _display_tz() -> ZoneInfo:
    """Display timezone. Defaults to America/Chicago (Central) — handles
    CST/CDT switching automatically via the IANA tzdata. Override via
    the BASEBALL_DISPLAY_TZ env var (any IANA zone name like
    'America/New_York', 'Europe/London', 'UTC').

    Falls back to UTC if the configured zone isn't installed (rare on
    Linux; happens on bare Windows without tzdata). The fallback is
    silent — we'd rather render in UTC than crash a page render."""
    name = os.environ.get("BASEBALL_DISPLAY_TZ", "America/Chicago")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


_DISPLAY_TZ = _display_tz()


def _fmt_dt(s: str | datetime | None) -> str:
    """Render any ISO-8601 string (or datetime) as ``YYYY-MM-DD HH:MM TZ``
    in the display timezone. Naive timestamps are assumed UTC — that's
    the convention everywhere in the sidecar (state.py stores
    ``datetime.now(tz=timezone.utc).isoformat()`` consistently).

    The trailing TZ abbreviation comes from strftime ``%Z`` which the
    IANA tzdata renders as CST/CDT seasonally — so the user sees
    "2026-05-22 19:30 CDT" in summer and "2026-01-15 19:30 CST" in
    winter without us doing anything."""
    if not s:
        return "—"
    if isinstance(s, datetime):
        dt = s
    else:
        try:
            dt = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return s  # bail — return the raw string
    # Treat naive timestamps as UTC (sidecar convention)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(_DISPLAY_TZ)
    return local.strftime("%Y-%m-%d %H:%M %Z")


def _team_logo_url(team_id: int) -> str:
    """MLB's public team-logo CDN. SVG format. id 119 = Dodgers, etc."""
    return f"https://www.mlbstatic.com/team-logos/{team_id}.svg"


templates.env.filters["pct"] = _fmt_pct
templates.env.filters["dt"] = _fmt_dt
templates.env.globals["logo_url"] = _team_logo_url


# ─── Routes ──────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index(request: Request, date: str | None = Query(None)):
    """Main page: today's slate + standings preview + recent activity.

    "Today" is the LOCAL calendar day per ``BASEBALL_DISPLAY_TZ``
    (default America/Chicago). Games whose start time falls within
    that local day are shown — including late-night games that
    technically have a UTC date one day later."""
    today = (datetime.now(_DISPLAY_TZ).date()
             if not date else datetime.fromisoformat(date).date())
    today_iso = today.isoformat()
    tz_name = str(_DISPLAY_TZ)
    games = state.get_games_for_local_date(today_iso, tz_name)
    # Attach predictions
    for g in games:
        preds = state.get_predictions_for_game(g["game_pk"])
        g["home_win_prob"] = preds[0]["home_win_prob"] if preds else None
        g["prediction_as_of"] = preds[0]["computed_at"] if preds else None

    ratings = baseball.team_ratings_table()
    health = baseball.health()
    stats = baseball.stats()
    activity = baseball.recent_activity(limit=10)

    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()

    return templates.TemplateResponse(request, "index.html", {
        "today": today,
        "today_iso": today_iso,
        "yesterday": yesterday,
        "tomorrow": tomorrow,
        "games": games,
        "ratings_top": ratings[:10],
        "ratings_full": ratings,
        "health": health,
        "stats": stats,
        "activity": activity,
    })


@app.get("/games", response_class=HTMLResponse)
def games_browser(request: Request, date: str | None = Query(None)):
    """Browse historical games by date. Defaults to yesterday (local
    calendar) so the landing view always has finalized games to look at."""
    today = datetime.now(_DISPLAY_TZ).date()
    if date:
        try:
            target = datetime.fromisoformat(date).date()
        except ValueError:
            target = today - timedelta(days=1)
    else:
        target = today - timedelta(days=1)

    games = state.get_games_for_local_date(target.isoformat(), str(_DISPLAY_TZ))
    # Annotate with stored predictions (if any) and play counts.
    import sqlite3

    from baseball.state import _state_path
    with sqlite3.connect(_state_path()) as conn:
        for g in games:
            preds = state.get_predictions_for_game(g["game_pk"])
            g["home_win_prob"] = preds[0]["home_win_prob"] if preds else None
            cnt = conn.execute(
                "SELECT COUNT(*) FROM live_plays WHERE game_pk = ?",
                (g["game_pk"],),
            ).fetchone()[0]
            g["plays_recorded"] = int(cnt)

    # Find adjacent dates that have games — guides the prev/next buttons
    # instead of hopping a day at a time across off days. Falls back to
    # raw +/- 1 day if nothing close.
    def _adjacent_with_games(direction: int, current: date_t) -> date_t:
        candidate = current + timedelta(days=direction)
        for _ in range(14):
            if state.get_games_for_date(candidate.isoformat()):
                return candidate
            candidate += timedelta(days=direction)
        return current + timedelta(days=direction)

    prev_date = _adjacent_with_games(-1, target).isoformat()
    next_date = _adjacent_with_games(1, target).isoformat()

    # Available date range from our games table so the UI knows what's
    # browseable. Cheap: COUNT + MIN/MAX over 95 + 405 rows.
    with sqlite3.connect(_state_path()) as conn:
        r = conn.execute(
            "SELECT MIN(SUBSTR(game_date,1,10)), MAX(SUBSTR(game_date,1,10)), "
            "COUNT(DISTINCT SUBSTR(game_date,1,10)) FROM games"
        ).fetchone()
        date_min, date_max, n_dates = r[0], r[1], r[2]

    return templates.TemplateResponse(request, "games.html", {
        "target": target,
        "target_iso": target.isoformat(),
        "prev_date": prev_date,
        "next_date": next_date,
        "games": games,
        "date_min": date_min,
        "date_max": date_max,
        "n_dates": n_dates,
        "health": baseball.health(),
    })


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    """Aggregate insights — model rankings, current edges, league
    leaders. The "what does the system actually know" surface."""
    from baseball import insights

    today = datetime.now(_DISPLAY_TZ).date().isoformat()
    tomorrow = (datetime.now(_DISPLAY_TZ).date()
                + timedelta(days=1)).isoformat()

    from baseball import park_factors as pf
    from baseball import recent_form as rf
    ratings = baseball.team_ratings_table()
    factors = sorted(pf.get_park_factors().values(),
                      key=lambda x: x.runs_factor, reverse=True)
    recent = rf.get_recent_form_table()
    return templates.TemplateResponse(request, "stats.html", {
        "coverage":          insights.data_coverage(),
        "ratings_top":       ratings[:10],
        "ratings_bot":       ratings[-5:],
        "offense_leaders":   insights.league_leaders_offense(limit=8),
        "pitching_leaders":  insights.league_leaders_pitching(limit=8),
        "hr_leaders":        insights.team_event_leaders("Home Run", "batting", limit=6),
        "k_leaders":         insights.team_event_leaders("Strikeout", "pitching", limit=6),
        "one_run_leaders":   insights.one_run_games(limit=6),
        "park_factors_top":  factors[:6],
        "park_factors_bot":  factors[-6:],
        "recent_hot":        recent[:6],
        "recent_cold":       list(reversed(recent[-6:])),
        "edges_today":       insights.current_edges_for_date(today)[:8],
        "edges_tomorrow":    insights.current_edges_for_date(tomorrow)[:8],
        "today_iso":         today,
        "tomorrow_iso":      tomorrow,
        "health":            baseball.health(),
    })


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    """Local-LLM chat surface. Stateless across requests; HTMX keeps the
    visible conversation on the page until reload."""
    from baseball import llm_chat
    return templates.TemplateResponse(request, "chat.html", {
        "models": llm_chat.list_available_models(),
        "default_model": llm_chat.DEFAULT_MODEL,
        "ollama_available": llm_chat.is_available(),
        "health": baseball.health(),
    })


@app.post("/chat/send", response_class=HTMLResponse)
async def chat_send(request: Request):
    """HTMX endpoint: takes a user message + optional model + prior turns
    (as JSON in a hidden field), sends to Ollama via baseball.llm_chat,
    and returns the assistant's reply as an HTML fragment.

    The conversation history lives in the page's DOM — each turn is
    posted back with the next message so the model sees the full
    context. Keeps things stateless server-side."""
    from baseball import llm_chat
    form = await request.form()
    message = str(form.get("message", "")).strip()
    model = str(form.get("model", "") or llm_chat.DEFAULT_MODEL).strip()
    history_raw = str(form.get("history", "[]"))
    try:
        import json as _json
        history = _json.loads(history_raw)
        if not isinstance(history, list):
            history = []
    except Exception:  # noqa: BLE001
        history = []

    if not message:
        return HTMLResponse("<em>Empty message — say something.</em>")

    # Push the new user turn into the message stack
    messages = list(history) + [{"role": "user", "content": message}]
    result = llm_chat.chat(messages, model=model)

    return templates.TemplateResponse(
        request, "chat_turn.html",
        {"user_message": message, "result": result,
          "updated_history": messages + (
              [{"role": "assistant", "content": result["reply"]}]
              if result["ok"] else []
          )},
    )


@app.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request):
    """Lead-lag analysis — paired plays × Kalshi book reactions.

    Note this page is **deliberately sparse** until ≥1 week of paired
    data accumulates. The mlb_book_poller daemon writes Kalshi book
    snapshots forward-only; the historical 30-day play backfill has
    no matching books. Real reaction-rate signals need lots of
    paired (play, book) pairs — small samples produce false confidence."""
    from baseball import analysis

    cov = analysis.coverage_summary()
    # Focus event-type breakdown on plays AFTER book collection started
    # — those are the ones that could potentially have paired data.
    event_counts = analysis.event_type_counts(
        since_ts=cov.get("book_collection_started_ts"),
    )
    # Reaction summaries for the most interesting event types.
    # Tiny samples return None for percent fields — UI will show "—".
    summaries = [
        analysis.reaction_summary(et, window_minutes=10.0,
                                    min_pct_change=0.02)
        for et in ("Home Run", "Strikeout", "Walk", "Single", "Double",
                    "Field Error", "Wild Pitch", "Pickoff", "Stolen Base")
    ]
    # Reactions segmented by game-situation (any scoring play, but
    # restricted to specific leverage states). Helps answer "is the
    # market more responsive in clutch moments?"
    situations = [
        analysis.reaction_summary_by_situation(only=s, window_minutes=10.0,
                                                  min_pct_change=0.02)
        for s in ("all", "late_and_close", "high_leverage",
                   "risp", "bases_loaded", "garbage_time")
    ]
    # Base-out state distribution — shows where scoring tends to happen
    bo_dist = analysis.base_out_state_distribution()
    bo_sorted = sorted(bo_dist.items(), key=lambda kv: -kv[1])[:12]
    # Highest-WPA plays in the database. Limit=15 for the panel; min
    # WPA threshold filters noise.
    try:
        top_wpa = analysis.top_wpa_plays(limit=15, min_abs_wpa=0.05)
    except Exception:  # noqa: BLE001
        top_wpa = []
    return templates.TemplateResponse(request, "analysis.html", {
        "coverage":    cov,
        "event_counts": event_counts,
        "summaries":   summaries,
        "situations":  situations,
        "base_out_distribution": bo_sorted,
        "top_wpa":     top_wpa,
        "health":      baseball.health(),
    })


@app.get("/standings", response_class=HTMLResponse)
def standings(request: Request, season: int | None = None):
    """Full standings table."""
    ratings = baseball.team_ratings_table(season=season)
    # Group by league + division for the classic look.
    grouped: dict[str, dict[str, list]] = {}
    for r in ratings:
        league = r["league"] or "?"
        div = r["division"] or "?"
        grouped.setdefault(league, {}).setdefault(div, []).append(r)
    for league in grouped:
        for div in grouped[league]:
            grouped[league][div].sort(key=lambda r: r["win_pct"], reverse=True)
    return templates.TemplateResponse(request, "standings.html", {
        "grouped": grouped,
        "all_ratings": ratings,
        "health": baseball.health(),
    })


@app.get("/game/{game_pk}", response_class=HTMLResponse)
def game_detail(request: Request, game_pk: int):
    """Drill into one game — predictions (line movement), inputs, pitcher
    cards, and play-by-play timeline if any plays are recorded.

    Works for both forward-tracked games (predictions present) and
    backfilled historical games (plays present, no predictions). 404
    only when we have neither record of the game."""
    rows = state.get_predictions_for_game(game_pk)

    # Find the matching game row. Look up directly by primary key —
    # the old ±days window missed historical games. Single small query.
    import sqlite3

    from baseball.state import _state_path
    game: dict[str, Any] | None = None
    with sqlite3.connect(_state_path()) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("""
            SELECT g.*,
                   ht.abbreviation AS home_abbr, ht.short_name AS home_name,
                   at.abbreviation AS away_abbr, at.short_name AS away_name,
                   hp.name AS home_pitcher_name, ap.name AS away_pitcher_name,
                   hp.era AS home_pitcher_era, ap.era AS away_pitcher_era
            FROM games g
            JOIN teams ht ON g.home_team_id = ht.id
            JOIN teams at ON g.away_team_id = at.id
            LEFT JOIN pitchers hp ON g.home_pitcher_id = hp.id
            LEFT JOIN pitchers ap ON g.away_pitcher_id = ap.id
            WHERE g.game_pk = ?
        """, (game_pk,)).fetchone()
        if r is not None:
            game = dict(r)

    # Play-by-play timeline — only fetched if any plays exist for this
    # game (lets the template skip the section cleanly).
    from baseball.live import get_recent_plays
    plays = get_recent_plays(game_pk, limit=500)

    if game is None and not rows and not plays:
        return HTMLResponse(f"Game {game_pk} not found", status_code=404)

    return templates.TemplateResponse(request, "game.html", {
        "game": game,
        "predictions": rows,
        "latest": rows[0] if rows else None,
        "plays": plays,
        "health": baseball.health(),
    })


@app.post("/refresh", response_class=HTMLResponse)
def refresh(request: Request):
    """Kick off a background refresh. Returns a small status partial that
    HTMX swaps in. The refresh itself runs as a subprocess so the request
    doesn't block."""
    try:
        subprocess.Popen(
            [sys.executable, "-m", "baseball.runner"],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        state.log_activity("trigger", "manual refresh triggered from web UI")
        status = "refresh started — reload in ~30s to see updates"
    except Exception as e:
        status = f"failed to start refresh: {e}"
    return HTMLResponse(f"<span class='status-msg'>{status}</span>")


@app.get("/health.json")
def health_json():
    """JSON health endpoint for external monitoring."""
    h = baseball.health()
    return {
        "status": h.status,
        "last_update_ts": h.last_update_ts.isoformat() if h.last_update_ts else None,
        "errors_24h": h.errors_24h,
        "detail": h.detail,
    }


@app.get("/api/predict")
def api_predict(home: str, away: str):
    """Quick JSON predict endpoint. Useful for cross-sidecar calls + curl."""
    pred = baseball.predict_game(home, away)
    if pred is None:
        return {"error": f"could not predict {away} @ {home}"}
    return pred


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse(url="/static/favicon.svg")


def main() -> int:
    import uvicorn
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    state.migrate()
    uvicorn.run(
        "baseball.web:app",
        host=web_auth.default_bind_host(), port=8770,
        log_level="info",
        reload=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
