"""Kalshi Trader — Streamlit entry point.

This file is the router: it configures the page, builds the sidebar chrome
(app title, alert badge, render timestamp), and dispatches to the selected
page via `st.navigation()`. All page logic lives in `pages/*.py`. All shared
data loaders and helpers live in `shared.py`.
"""

from datetime import UTC, datetime

import streamlit as st

# Optional .env support — silently no-op if python-dotenv isn't installed.
# Loaded *before* any module that reads env vars (alerts, portfolio, etc.).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pm.core import logctx, profiles  # noqa: E402
from pm.ledger import alerts as al  # noqa: E402  — must follow load_dotenv
from pm.venue import provider as mp  # noqa: E402

# ---------------------------------------------------------------------------
# Logging setup (controllable via env: POLYMARKET_LOG_LEVEL=DEBUG/INFO/WARNING)
# ---------------------------------------------------------------------------

logctx.setup()

# ---------------------------------------------------------------------------
# Profiles — create data/ directory structure and migrate legacy root-level
# state files. Idempotent on every page render.
# ---------------------------------------------------------------------------

profiles.initialize_profiles()

# ---------------------------------------------------------------------------
# User-defined strategy plugins. Imports user_strategies/*.py so any
# @register_strategy decorators in those files fire and the strategies
# show up in the Backtest page. Idempotent — Python caches module
# imports, so a Streamlit rerun doesn't re-load anything.
# ---------------------------------------------------------------------------

from pm.research import backtest as _bt  # noqa: E402, I001 — must follow profiles init

_bt.discover_user_strategies()

# Side-effect import: edges.py registers its Tier-4 strategies (crypto_spot,
# llm_forecast, sports_winprob, manifold_community, macro_threshold) into
# bt.STRATEGIES at module load time. Without this, the Backtest page's
# strategy dropdown is missing those entries until the user happens to
# visit the Edges page first.
import edges  # noqa: E402, F401 — imported for its registration side effect

# ---------------------------------------------------------------------------
# Page config (must be the first Streamlit call)
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Kalshi Trader",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Sidebar header (appears above the auto-generated nav)
# ---------------------------------------------------------------------------

st.sidebar.title("Kalshi Trader")

# ---------------------------------------------------------------------------
# Market provider selector — pick which venue's data we fetch. Kalshi is
# the default (US-legal). MARKET_PROVIDER env var seeds the initial value;
# switching here updates os.environ for the rest of the process and reruns
# so cached market data is re-fetched against the new venue.
# ---------------------------------------------------------------------------

_providers = mp.list_providers()
_provider_names = [p.name for p in _providers]
_current_provider = mp.active_provider_name()
_provider_idx = (
    _provider_names.index(_current_provider)
    if _current_provider in _provider_names else 0
)

_picked_provider = st.sidebar.selectbox(
    "🏛️ Market venue",
    options=_provider_names,
    index=_provider_idx,
    format_func=lambda n: mp.get_provider(n).display_name,
    help="Which prediction-market venue to fetch data from. Kalshi is "
         "CFTC-regulated and US-legal; Polymarket excludes US persons. "
         "Switching reloads all market data.",
)

if _picked_provider != _current_provider:
    mp.set_active_provider(_picked_provider)
    st.cache_data.clear()
    st.toast(
        f"Switched to **{mp.get_provider(_picked_provider).display_name}**",
        icon="🏛️",
    )
    st.rerun()

# ---------------------------------------------------------------------------
# Profile selector — multi-user Option A (file-based, single-machine, no auth).
# Each profile's portfolio/forecasts/alerts/settings live in data/<profile>/.
# Switching profile clears the data cache and reruns so storage paths take
# effect immediately (the data modules read their paths lazily on every call).
# ---------------------------------------------------------------------------

_available = profiles.list_profiles()
_current = profiles.active_profile()
_current_idx = _available.index(_current) if _current in _available else 0

_picked = st.sidebar.selectbox(
    "👥 Profile",
    options=_available,
    index=_current_idx,
    help="Each profile has its own portfolio, forecasts, alerts, and settings. "
         "Use **sandbox** to experiment without affecting your real data.",
)

if _picked != _current:
    profiles.set_active_profile(_picked)
    st.cache_data.clear()
    st.toast(f"Switched to **{_picked}** profile", icon="👤")
    st.rerun()

# Visible indicator when sandbox is active so you can't forget
if _picked == "sandbox":
    st.sidebar.info("🧹 Sandbox mode — wipeable via Settings page.")

# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

pages = [
    st.Page("pm/ui/pages/markets.py",       title="🏠 Markets",       default=True),
    st.Page("pm/ui/pages/price_history.py", title="📈 Price History"),
    st.Page("pm/ui/pages/scanner.py",       title="⚡ Market Scanner"),
    st.Page("pm/ui/pages/kelly.py",         title="📐 Kelly Sizer"),
    st.Page("pm/ui/pages/portfolio.py",     title="💼 Sandbox Portfolio"),
    st.Page("pm/ui/pages/live_portfolio.py", title="🏦 Live Portfolio"),
    st.Page("pm/ui/pages/orders.py",         title="📑 Orders"),
    st.Page("pm/ui/pages/calibration.py",   title="📊 Calibration"),
    st.Page("pm/ui/pages/backtest.py",      title="🧪 Backtest"),
    st.Page("pm/ui/pages/backtest_history.py", title="📜 Backtest History"),
    st.Page("pm/ui/pages/edges.py",         title="🔭 Edges"),
    st.Page("pm/ui/pages/recommendations.py", title="🎯 Recommendations"),
    st.Page("pm/ui/pages/deep_analysis.py", title="🧠 Deep Analysis"),
    st.Page("pm/ui/pages/alerts.py",        title="🔔 Alerts"),
    st.Page("pm/ui/pages/settings.py",      title="⚙️ Settings"),
]
pg = st.navigation(pages)

# Emergency-stop banner — rendered ABOVE the selected page so it lands
# at the very top of every view when the kill switch is active. No-op
# when not active.
from pm.ui.shared import emergency_stop_banner  # noqa: E402 — must follow page config

emergency_stop_banner()

# Run the selected page (this is where each page's sidebar widgets get added)
pg.run()

# ---------------------------------------------------------------------------
# Sidecar quick-links — these UIs live OUTSIDE Streamlit (FastAPI, multi-user
# safe) so we link out rather than embed. Add a new sidecar here when it
# ships its own web UI.
# ---------------------------------------------------------------------------

import os as _os  # noqa: E402 — deferred: must follow load_dotenv + profile init
import socket as _socket  # noqa: E402


def _lan_host() -> str:
    """Best-effort LAN host so links work from phones/tablets/other PCs.
    Resolve the host's primary outbound IPv4 (no packets actually sent —
    UDP connect just selects the route). Falls back to hostname, then
    localhost."""
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        pass
    try:
        return _socket.gethostname()
    except OSError:
        return "localhost"


# Per-sidecar web-UI URLs. Each sidecar that ships a FastAPI UI gets a port
# here; the sidebar links to it. Env override per sidecar (e.g.
# BASEBALL_WEB_URL / WNBA_WEB_URL). Add a row when a new sidecar ships a UI.
_SIDECAR_WEB_PORTS = {"baseball": 8770, "wnba": 8771, "tennis": 8772}


def _sidecar_web_url(name: str) -> str | None:
    """LAN URL for a sidecar's web UI, or None if it has none."""
    override = _os.environ.get(f"{name.upper()}_WEB_URL")
    if override:
        return override
    port = _SIDECAR_WEB_PORTS.get(name)
    return f"http://{_lan_host()}:{port}" if port else None
st.sidebar.divider()
st.sidebar.markdown("**🛰️ Sidecars**")

# Iterate every registered sidecar so future sidecars (news, nba, …) get
# the same treatment automatically. Importing baseball below pulls it
# into the registry; we keep that import here so a broken sidecar can't
# crash the whole sidebar render.
for _mod in ("baseball", "wnba", "tennis"):
    try:
        __import__(_mod)  # triggers register_sidecar()
    except Exception as _e:  # noqa: BLE001 — a broken sidecar can't crash the sidebar
        st.sidebar.caption(f"{_mod}: import failed — {_e}")

from pm.core import sidecars as _sidecars  # noqa: E402 — side-effect import; sidecars self-register on import

for _sc in _sidecars.list_sidecars():
    try:
        _h = _sc.health()
    except Exception as _e:  # noqa: BLE001 — should never raise per protocol
        _h = _sidecars.SidecarHealth(status="down", detail=f"probe raised: {_e}")
    _icon = {"ok": "🟢", "degraded": "🟡", "down": "🔴"}.get(_h.status, "❔")
    # External UI URL per sidecar (baseball :8770, wnba :8771). Sidecars
    # without a UI render as plain text.
    _url = _sidecar_web_url(_sc.name)
    if _url:
        st.sidebar.markdown(
            f"{_icon} [{_sc.display_name} ↗]({_url})",
            help=f"{_h.detail or 'no detail'}"
                 + (f"\n\nlast refresh: {_h.last_update_ts.isoformat()}"
                    if _h.last_update_ts else ""),
        )
    else:
        st.sidebar.markdown(
            f"{_icon} {_sc.display_name}",
            help=f"{_h.detail or 'no detail'}",
        )
    if _h.errors_24h > 0:
        st.sidebar.caption(f"⚠️ {_h.errors_24h} error(s) in last 24h")

# ---------------------------------------------------------------------------
# Sidebar footer chrome (appears below the page's own sidebar widgets)
# ---------------------------------------------------------------------------

st.sidebar.divider()

_alert_stats = al.summary_stats()
if _alert_stats["triggered"]:
    st.sidebar.warning(f"🔔 {_alert_stats['triggered']} alert(s) triggered — see Alerts page")
elif _alert_stats["active"]:
    st.sidebar.caption(f"🔔 {_alert_stats['active']} active alert(s)")

st.sidebar.caption(
    f"Data cached up to 60s.\n\n"
    f"Page rendered at {datetime.now(UTC).strftime('%H:%M:%S UTC')}"
)
