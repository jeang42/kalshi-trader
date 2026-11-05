"""FastAPI web UI for the tennis sidecar (port 8772).

Per the "every major sidecar ships a UI" standard. Reuses baseball's
stylesheet. Pages: Today (date-navigable day view — past=results with
model ✓/✗, future=forecast — plus a live model-vs-market edge panel),
Player detail (Elo + match history), Stats (edges + surface leaders),
Chat (Ollama with Elo context), Elo Ratings, Recent Matches.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import tennis
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

from tennis import model, state

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

app = FastAPI(title="🎾 Tennis Sidecar", version="0.1.0")

# LAN-facing with no authentication until 2026-08-11. Shared-secret gate;
# a no-op unless WEB_UI_TOKEN is set. See web_auth.py for what this is and,
# more importantly, what it is not.

web_auth.protect(app, app_name="Tennis UI")
app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")


def _display_tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("TENNIS_DISPLAY_TZ", "America/New_York"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


_DISPLAY_TZ = _display_tz()


def _fmt_dt(s):
    if not s:
        return "—"
    dt = s if isinstance(s, datetime) else None
    if dt is None:
        try:
            dt = datetime.fromisoformat(str(s))
        except (ValueError, TypeError):
            return str(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")


templates.env.filters["dt"] = _fmt_dt


def _discover_today_matches():
    """Live Kalshi ATP+WTA markets → rows with model P(YES). Both sides."""
    from pm.venue import kalshi_client as kc
    from pm.venue.kalshi_client import _get
    rows = []
    for ser, tour in [("KXATPMATCH", "atp"), ("KXWTAMATCH", "wta")]:
        try:
            payload = _get("/events", params={"series_ticker": ser,
                                              "status": "open", "limit": 50})
        except Exception:  # noqa: BLE001
            continue
        for ev in payload.get("events", []):
            try:
                df = kc.lookup_markets_by_id(ev["event_ticker"])
            except Exception:  # noqa: BLE001
                continue
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                tk, q = str(r["condition_id"]), str(r["question"])
                pred = tennis.predict_for_ticker(tk, q)
                # market mid from outcome_prices if present
                mkt = None
                op = r.get("outcome_prices")
                if isinstance(op, list) and op and op[0] is not None:
                    mkt = float(op[0])
                rows.append({
                    "ticker": tk, "tour": tour,
                    "yes_name": pred["yes_name"] if pred else None,
                    "opponent": pred["opponent"] if pred else None,
                    "yes_prob": pred["yes_prob"] if pred else None,
                    "yes_elo": pred["yes_elo"] if pred else None,
                    "opp_elo": pred["opp_elo"] if pred else None,
                    "market": mkt,
                    "question": q,
                })
    return rows


_PLACEHOLDER_ID = 2147483647  # ESPN draw-slot stub for unscheduled matches


@app.get("/", response_class=HTMLResponse)
def index(request: Request, date: str | None = None):
    """Date-navigable day view. Past dates show results (with the model's
    pre-match P + whether it was right); today/future show scheduled
    matches with the model's P. Plus a live 'model vs market' edge panel
    from open Kalshi markets."""
    from datetime import date as _date
    from datetime import timedelta
    today = datetime.now(_DISPLAY_TZ).date()
    try:
        target = _date.fromisoformat(date) if date else today
    except ValueError:
        target = today

    rows = state.matches_for_date(target.isoformat())
    # drop unscheduled draw slots (placeholder athlete ids)
    rows = [m for m in rows
            if m["p1_id"] != _PLACEHOLDER_ID and m["p2_id"] != _PLACEHOLDER_ID]
    for m in rows:
        p1 = state.get_player(m["p1_id"], m["tour"])
        p2 = state.get_player(m["p2_id"], m["tour"])
        pred = model.predict_match(p1, p2, surface=m.get("surface") or "unknown")
        m["p1_win_prob"] = pred.p1_win_prob if pred else None
        if m.get("winner_id") and pred:
            model_p1 = pred.p1_win_prob >= 0.5
            actual_p1 = m["winner_id"] == m["p1_id"]
            m["model_correct"] = (model_p1 == actual_p1)
        else:
            m["model_correct"] = None

    # live edge panel (only meaningful for today/future)
    edge_rows = _discover_today_matches() if target >= today else []
    for m in edge_rows:
        m["edge"] = (m["yes_prob"] - m["market"]
                     if m["yes_prob"] is not None and m["market"] is not None else None)

    return templates.TemplateResponse(request, "index.html", {
        "target": target,
        "yesterday": (target - timedelta(days=1)).isoformat(),
        "tomorrow": (target + timedelta(days=1)).isoformat(),
        "is_past": target < today,
        "matches": rows,
        "edge_rows": edge_rows,
        "atp_top": tennis.ratings_table("atp", limit=10),
        "wta_top": tennis.ratings_table("wta", limit=10),
        "health": tennis.health(),
        "stats": tennis.stats(),
        "activity": tennis.recent_activity(limit=10),
    })


@app.get("/player/{tour}/{pid}", response_class=HTMLResponse)
def player_detail(request: Request, tour: str, pid: int):
    """One player: Elo, rank, recent match history with W/L."""
    p = state.get_player(pid, tour)
    if p is None:
        return HTMLResponse(f"Player {pid} ({tour}) not found", status_code=404)
    import json as _json
    surface_elo = {}
    if p.get("surface_elo"):
        try:
            surface_elo = _json.loads(p["surface_elo"])
        except (ValueError, TypeError):
            surface_elo = {}
    matches = state.matches_for_player(pid, tour, limit=40)
    for m in matches:
        m["won"] = (m.get("winner_id") == pid) if m.get("winner_id") else None
        m["opp_name"] = (m.get("p2_name") if m["p1_id"] == pid else m.get("p1_name"))
        m["opp_id"] = m["p2_id"] if m["p1_id"] == pid else m["p1_id"]
    return templates.TemplateResponse(request, "player.html", {
        "player": p, "surface_elo": surface_elo, "matches": matches,
        "health": tennis.health(),
    })


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    """Biggest live model-vs-market edges + per-surface leaders + Brier."""
    edge_rows = [m for m in _discover_today_matches()
                 if m["yes_prob"] is not None and m["market"] is not None]
    for m in edge_rows:
        m["edge"] = m["yes_prob"] - m["market"]
    edge_rows.sort(key=lambda m: abs(m["edge"]), reverse=True)
    # surface leaders: re-read player rows, parse surface_elo
    import json as _json
    def _surface_leaders(tour, surface, n=8):
        out = []
        for p in state.ratings_table(tour, limit=2000):
            se = {}
            if p.get("surface_elo"):
                try:
                    se = _json.loads(p["surface_elo"])
                except (ValueError, TypeError):
                    se = {}
            if surface in se:
                out.append({"name": p["name"], "elo": se[surface],
                            "matches": p["matches_played"]})
        out.sort(key=lambda x: x["elo"], reverse=True)
        return out[:n]
    return templates.TemplateResponse(request, "stats.html", {
        "edges": edge_rows[:12],
        "atp_clay": _surface_leaders("atp", "clay"),
        "wta_clay": _surface_leaders("wta", "clay"),
        "atp_hard": _surface_leaders("atp", "hard"),
        "stats": tennis.stats(),
        "health": tennis.health(),
    })


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    from tennis import llm_chat
    return templates.TemplateResponse(request, "chat.html", {
        "models": llm_chat.list_available_models(),
        "default_model": llm_chat.DEFAULT_MODEL,
        "ollama_available": llm_chat.is_available(),
        "health": tennis.health(),
    })


@app.post("/chat/send", response_class=HTMLResponse)
async def chat_send(request: Request):
    from tennis import llm_chat
    form = await request.form()
    message = str(form.get("message", "")).strip()
    model_name = str(form.get("model", "") or llm_chat.DEFAULT_MODEL).strip()
    try:
        import json as _json
        history = _json.loads(str(form.get("history", "[]")))
        if not isinstance(history, list):
            history = []
    except Exception:  # noqa: BLE001
        history = []
    if not message:
        return HTMLResponse("<em>Empty message — say something.</em>")
    messages = list(history) + [{"role": "user", "content": message}]
    result = llm_chat.chat(messages, model=model_name)
    return templates.TemplateResponse(request, "chat_turn.html", {
        "user_message": message, "result": result,
        "updated_history": messages + (
            [{"role": "assistant", "content": result["reply"]}]
            if result["ok"] else []),
    })


@app.get("/ratings", response_class=HTMLResponse)
def ratings(request: Request):
    return templates.TemplateResponse(request, "ratings.html", {
        "atp": tennis.ratings_table("atp", limit=100),
        "wta": tennis.ratings_table("wta", limit=100),
        "health": tennis.health(),
        "stats": tennis.stats(),
    })


@app.get("/matches", response_class=HTMLResponse)
def recent_matches(request: Request):
    return templates.TemplateResponse(request, "matches.html", {
        "matches": state.recent_matches(limit=80),
        "health": tennis.health(),
    })


@app.post("/refresh", response_class=HTMLResponse)
def refresh():
    try:
        subprocess.Popen(
            [sys.executable, "-m", "tennis.runner"],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        state.log_activity("trigger", "manual refresh from web UI")
        status = "refresh started — reload in ~30s"
    except Exception as e:  # noqa: BLE001
        status = f"failed: {e}"
    return HTMLResponse(f"<span class='status-msg'>{status}</span>")


@app.get("/health.json")
def health_json():
    h = tennis.health()
    return {"status": h.status,
            "last_update_ts": h.last_update_ts.isoformat() if h.last_update_ts else None,
            "errors_24h": h.errors_24h, "detail": h.detail}


@app.get("/api/predict")
def api_predict(p1: str, p2: str, tour: str | None = None,
                surface: str = "unknown"):
    pred = tennis.predict_match_by_names(p1, p2, tour=tour, surface=surface)
    return pred or {"error": f"could not predict {p1} vs {p2}"}


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse(url="/static/favicon.svg")


def main() -> int:
    import uvicorn
    logging.basicConfig(level="INFO",
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    state.migrate()
    uvicorn.run("tennis.web:app", host=web_auth.default_bind_host(), port=8772)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
