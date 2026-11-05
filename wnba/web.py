"""FastAPI web UI for the WNBA sidecar.

Token-gated (WEB_UI_TOKEN, see pm/core/web_auth.py; loopback-only when no
token is configured), multi-user safe (FastAPI handles concurrent requests
fine; SQLite is WAL-mode). Mirrors ``baseball/web.py`` and reuses its
stylesheet so the two sidecars look identical — per the
"every major sidecar ships a UI" rule in docs/sports-model-playbook.md.

Run::

    uvicorn wnba.web:app --port 8771

Pages: Today (slate + model win-prob + favorite-hold highlight),
Standings (team ratings). No Chat/Analysis pages — those need the
live-play + LLM machinery WNBA doesn't have (forecast-only sidecar).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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

import wnba
from wnba import state

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

app = FastAPI(title="🏀 WNBA Sidecar", version="0.1.0")

# LAN-facing with no authentication until 2026-08-11. Shared-secret gate;
# a no-op unless WEB_UI_TOKEN is set. See web_auth.py for what this is and,
# more importantly, what it is not.

web_auth.protect(app, app_name="WNBA UI")
app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")


# ─── Jinja helpers ────────────────────────────────────────────────────────


def _fmt_pct(p: float | None) -> str:
    return f"{p * 100:.0f}%" if p is not None else "—"


def _display_tz() -> ZoneInfo:
    name = os.environ.get("WNBA_DISPLAY_TZ", "America/New_York")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


_DISPLAY_TZ = _display_tz()


def _fmt_dt(s: str | datetime | None) -> str:
    if not s:
        return "—"
    if isinstance(s, datetime):
        dt = s
    else:
        try:
            dt = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _logo_url(abbr: str | None) -> str:
    """ESPN WNBA team-logo CDN, keyed on lowercase abbreviation."""
    a = (abbr or "").lower()
    return f"https://a.espncdn.com/i/teamlogos/wnba/500/{a}.png"


templates.env.filters["pct"] = _fmt_pct
templates.env.filters["dt"] = _fmt_dt
templates.env.globals["logo_url"] = _logo_url


# ─── Routes ──────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index(request: Request, date: str | None = Query(None)):
    """Today's slate: games + model win-prob bars + favorite-hold flags."""
    today = (datetime.now(_DISPLAY_TZ).date()
             if not date else datetime.fromisoformat(date).date())
    today_iso = today.isoformat()
    games = state.get_games_for_eastern_date(today_iso)
    for g in games:
        preds = state.get_predictions_for_game(g["event_id"])
        hp = preds[0]["home_win_prob"] if preds else None
        g["home_win_prob"] = hp
        # favorite-hold flag: is either side a moderate favorite (0.55-0.70)?
        fav = None
        if hp is not None:
            if 0.55 <= hp <= 0.70:
                fav = g.get("home_abbr")
            elif 0.55 <= (1 - hp) <= 0.70:
                fav = g.get("away_abbr")
        g["fav_hold"] = fav

    ratings = wnba.team_ratings_table()
    health = wnba.health()
    stats = wnba.stats()
    activity = wnba.recent_activity(limit=10)

    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()

    return templates.TemplateResponse(request, "index.html", {
        "today": today,
        "yesterday": yesterday,
        "tomorrow": tomorrow,
        "games": games,
        "ratings_top": ratings[:10],
        "health": health,
        "stats": stats,
        "activity": activity,
    })


@app.get("/standings", response_class=HTMLResponse)
def standings(request: Request):
    """Full team ratings by model strength."""
    ratings = wnba.team_ratings_table()
    health = wnba.health()
    return templates.TemplateResponse(request, "standings.html", {
        "ratings": ratings,
        "health": health,
        "stats": wnba.stats(),
    })


@app.get("/game/{event_id}", response_class=HTMLResponse)
def game_detail(request: Request, event_id: str):
    """Drill into one game — prediction, model inputs, team strengths,
    score. 404 only when the game isn't in state at all."""
    game = state.get_game_with_teams(event_id)
    preds = state.get_predictions_for_game(event_id)
    if game is None and not preds:
        return HTMLResponse(f"Game {event_id} not found", status_code=404)

    # Team strength breakdown for the matchup (what fed the prediction).
    ratings = {r["abbreviation"]: r for r in wnba.team_ratings_table()}
    home_rating = ratings.get(game.get("home_abbr")) if game else None
    away_rating = ratings.get(game.get("away_abbr")) if game else None

    latest = preds[0] if preds else None
    inputs = {}
    if latest and latest.get("inputs"):
        import json as _json
        try:
            inputs = _json.loads(latest["inputs"])
        except (ValueError, TypeError):
            inputs = {}

    return templates.TemplateResponse(request, "game.html", {
        "game": game,
        "latest": latest,
        "inputs": inputs,
        "home_rating": home_rating,
        "away_rating": away_rating,
        "health": wnba.health(),
    })


@app.get("/games", response_class=HTMLResponse)
def recent_games(request: Request):
    """Recent finished games with model prediction vs actual result."""
    games = state.get_recent_final_games(limit=50)
    for g in games:
        hp = g.get("home_win_prob")
        won_home = (g.get("home_score") is not None
                    and g.get("away_score") is not None
                    and g["home_score"] > g["away_score"])
        g["winner_abbr"] = (g.get("home_abbr") if won_home
                            else g.get("away_abbr")) if g.get("home_score") is not None else None
        # was the model right (did its favored side win)?
        if hp is not None and g["winner_abbr"]:
            model_home = hp >= 0.5
            g["model_correct"] = (model_home == won_home)
        else:
            g["model_correct"] = None
    return templates.TemplateResponse(request, "games.html", {
        "games": games,
        "health": wnba.health(),
    })


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    """Aggregates: ratings top/bottom, scoring leaders, model edges today."""
    ratings = wnba.team_ratings_table()
    # scoring leaders from team_season
    season = datetime.now(UTC).year
    seasons = state.get_all_team_seasons(season)
    for s in seasons:
        gp = max(s.get("games_played", 1), 1)
        s["off_ppg"] = s["points_for"] / gp
        s["def_ppg"] = s["points_against"] / gp
    offense = sorted(seasons, key=lambda x: x["off_ppg"], reverse=True)[:8]
    defense = sorted(seasons, key=lambda x: x["def_ppg"])[:8]

    # today's model edges (model vs market) via the edges adapter
    edges_today = []
    try:
        import pandas as _pd

        import edges as _edges
        from pm.venue import kalshi_client as _kc
        from pm.venue.kalshi_client import _get
        payload = _get("/events", params={"series_ticker": "KXWNBAGAME",
                                          "status": "open", "limit": 50})
        frames = []
        for ev in payload.get("events", []):
            et = ev.get("event_ticker")
            if et:
                sub = _kc.lookup_markets_by_id(et)
                if sub is not None and not sub.empty:
                    frames.append(sub)
        if frames:
            df = _pd.concat(frames, ignore_index=True).drop_duplicates("condition_id")
            edges_today = _edges.find_wnba_edges(df, min_gap=0.03)
    except Exception as e:  # noqa: BLE001
        log.debug("edges fetch failed: %s", e)

    return templates.TemplateResponse(request, "stats.html", {
        "ratings_top": ratings[:8],
        "ratings_bot": ratings[-5:],
        "offense": offense,
        "defense": defense,
        "edges_today": edges_today[:10],
        "stats": wnba.stats(),
        "health": wnba.health(),
    })


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    from wnba import llm_chat
    return templates.TemplateResponse(request, "chat.html", {
        "models": llm_chat.list_available_models(),
        "default_model": llm_chat.DEFAULT_MODEL,
        "ollama_available": llm_chat.is_available(),
        "health": wnba.health(),
    })


@app.post("/chat/send", response_class=HTMLResponse)
async def chat_send(request: Request):
    from wnba import llm_chat
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
    messages = list(history) + [{"role": "user", "content": message}]
    result = llm_chat.chat(messages, model=model)
    return templates.TemplateResponse(request, "chat_turn.html", {
        "user_message": message, "result": result,
        "updated_history": messages + (
            [{"role": "assistant", "content": result["reply"]}]
            if result["ok"] else []),
    })


@app.post("/refresh", response_class=HTMLResponse)
def refresh():
    """Kick off a background runner refresh (subprocess, non-blocking)."""
    try:
        subprocess.Popen(
            [sys.executable, "-m", "wnba.runner"],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        state.log_activity("trigger", "manual refresh triggered from web UI")
        status = "refresh started — reload in ~10s to see updates"
    except Exception as e:  # noqa: BLE001
        status = f"failed to start refresh: {e}"
    return HTMLResponse(f"<span class='status-msg'>{status}</span>")


@app.get("/health.json")
def health_json():
    h = wnba.health()
    return {
        "status": h.status,
        "last_update_ts": h.last_update_ts.isoformat() if h.last_update_ts else None,
        "errors_24h": h.errors_24h,
        "detail": h.detail,
    }


@app.get("/api/predict")
def api_predict(home: str, away: str):
    """Quick JSON predict endpoint — useful for curl / cross-sidecar calls."""
    pred = wnba.predict_game(home, away)
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
    uvicorn.run("wnba.web:app", host=web_auth.default_bind_host(), port=8771)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
