"""Control-plane dashboard — one read-only page over the whole fleet.

FastAPI + Jinja2 (the project's sidecar UI standard; Streamlit doesn't scale to
concurrent users). Aggregates `collect.snapshot()` — service health, data-panel
growth, paper-arm P&L, sidecar health, live account — and auto-refreshes. LAN,
no auth, observe-only (no start/stop in this MVP).

Run: scripts/control_plane_web.sh  (uvicorn on :8773), or systemd control-plane.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
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

from control_plane import collect

_PKG_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

app = FastAPI(title="🎛 Control Plane", version="0.1.0")

# LAN-facing with no authentication until 2026-08-11. Shared-secret gate;
# a no-op unless WEB_UI_TOKEN is set. See web_auth.py for what this is and,
# more importantly, what it is not.

web_auth.protect(app, app_name="Control plane")
app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html",
                                      {"d": collect.snapshot()})


@app.get("/health.json")
def health_json():
    """Machine-readable snapshot (for scripts / uptime checks)."""
    return collect.snapshot()


if __name__ == "__main__":
    import os

    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.getenv("CONTROL_PLANE_PORT", "8773")))
