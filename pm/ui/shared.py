"""Shared helpers used across all Streamlit pages.

Provides the cached market/event loaders, the alert-check side effect, and
common UI constants. All pages should import from here rather than
redefining their own copies.
"""

from __future__ import annotations

import pandas as pd
import requests
import streamlit as st

from pm.core import profiles
from pm.ledger import alerts as al
from pm.venue import provider as mp  # market provider — the venue-agnostic data layer

# ---------------------------------------------------------------------------
# UI constants
# ---------------------------------------------------------------------------

# Prediction-market price floor — Polymarket markets near resolution trade
# as low as $0.0005, so Streamlit number_input / slider min_values must
# accept that. Kalshi also clamps to fractions of a cent.
PRICE_MIN = 0.0001
PRICE_MAX = 0.9999

# Kelly probability sliders use the same range.
KELLY_P_MIN = PRICE_MIN
KELLY_P_MAX = PRICE_MAX


# ---------------------------------------------------------------------------
# Venue helpers — let pages produce venue-aware filenames and labels
# without each one reaching into `provider` directly.
# ---------------------------------------------------------------------------

def venue_name() -> str:
    """Short, lowercase venue identifier of the active provider — safe to
    interpolate into filenames (e.g. f\"{venue_name()}_markets.csv\")."""
    return mp.get_provider().name


def venue_display() -> str:
    """Human-readable venue name of the active provider for headings/copy."""
    return mp.get_provider().display_name


# ---------------------------------------------------------------------------
# Cached data loaders (shared cache key across all pages)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)   # tags change rarely; cache longer than markets
def load_events(limit: int = 500) -> pd.DataFrame:
    """Events with tag-derived categories. Used to give markets authoritative
    category data, overriding the keyword heuristic in get_markets.

    Goes through the active market provider (`provider.get_provider()`).
    """
    try:
        return mp.get_provider().get_events(limit=limit)
    except requests.RequestException:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_markets(limit: int = 500) -> pd.DataFrame:
    """Single canonical market loader. All pages call without args to share one cache entry.

    Returns an empty DataFrame on network failure so pages can render gracefully.
    Categories are enriched from `/events` tags when available; otherwise fall
    back to keyword inference from question text. Routes through the active
    market provider so this works identically against Polymarket, Kalshi, etc.
    """
    provider = mp.get_provider()
    try:
        df = provider.get_markets(limit=limit)
    except requests.RequestException as e:
        st.error(
            f"⚠️ Couldn't reach the {provider.display_name} API: {e}\n\n"
            "Check your internet connection, then refresh the page."
        )
        return pd.DataFrame()

    events_df = load_events()
    if not events_df.empty:
        df = provider.enrich_categories_from_events(df, events_df)
    return df


@st.cache_data(ttl=60)
def load_markets_closing_soon(max_days: float, limit: int = 500) -> pd.DataFrame:
    """Active markets closing within ``max_days``, fetched via the
    provider's time-filtered endpoint when available.

    The default ``load_markets()`` is ordered by 24h volume, which on
    Kalshi heavily favors long-dated marquee events (CCP succession,
    climate goals, etc.) — short-dated markets can be entirely absent
    from the top-500 cut. Pages that filter for short-duration trades
    should call this instead.

    Categories are enriched from `/events` like the regular loader.
    """
    provider = mp.get_provider()
    try:
        df = provider.get_markets_closing_within(
            max_days=float(max_days), limit=int(limit),
        )
    except requests.RequestException as e:
        st.error(
            f"⚠️ Couldn't reach the {provider.display_name} API: {e}\n\n"
            "Check your internet connection, then refresh the page."
        )
        return pd.DataFrame()
    events_df = load_events()
    if not events_df.empty and not df.empty:
        df = provider.enrich_categories_from_events(df, events_df)
    return df


# ---------------------------------------------------------------------------
# Alert checking (called by each page that has market data)
# ---------------------------------------------------------------------------

def check_alerts_and_notify(df_markets: pd.DataFrame | None) -> None:
    """Check alerts against the current markets and surface any new triggers via toast.

    Safe to call on any page; no-op if df_markets is empty.
    """
    if df_markets is None or df_markets.empty:
        return
    fired = al.check_alerts(df_markets)
    for a in fired:
        arrow = "↑" if a["direction"] == "above" else "↓"
        st.toast(
            f"🔔 {a['outcome']} {arrow} ${a['triggered_price']:.3f} "
            f"({a['threshold']:.3f}): {a['question'][:60]}",
            icon="🔔",
        )


# ---------------------------------------------------------------------------
# Misc helpers used by multiple pages
# ---------------------------------------------------------------------------

def outcome_price_prefill(
    outcomes_list, prices_list, outcome_name: str, fallback: float = 0.5
) -> float:
    """Look up the current market price for an outcome, clamped to the
    Streamlit number_input bounds so it can be used as a prefill value.
    Used by the Buy/Sell forms on Portfolio and the alert-creation form.
    """
    try:
        idx = list(outcomes_list).index(outcome_name)
        val = float(prices_list[idx])
    except (ValueError, IndexError, TypeError):
        val = fallback
    return max(PRICE_MIN, min(PRICE_MAX, val))


# ---------------------------------------------------------------------------
# Live price ticker
# ---------------------------------------------------------------------------

LIVE_TICK_SECONDS = 3
"""How often the live price fragment re-fetches midpoints."""


@st.fragment(run_every=LIVE_TICK_SECONDS)
def live_price_panel(tokens: list[dict]) -> None:
    """A live-updating price ticker.

    Re-runs every `LIVE_TICK_SECONDS` seconds independently of the page,
    fetching the latest mid price for each token via the CLOB `/midpoint`
    endpoint. Shows the current price plus a delta vs. the previous tick.

    `tokens` is a list of `{"token_id": str, "outcome": str}` dicts.

    This is REST polling, not a WebSocket push. The UX is equivalent for a
    single user app at this poll cadence (~0.3 req/s/outcome), well below
    any reasonable rate limit. For higher-frequency or many-user deployment,
    swap the body for a read from a shared LivePriceFeed singleton.
    """
    if not tokens:
        return

    # session_state holds the previous tick's prices so we can render a delta.
    last_key = "live_price_last_tick"
    last_prices: dict[str, float] = st.session_state.get(last_key, {})
    new_prices: dict[str, float] = {}

    cols = st.columns(len(tokens))
    provider = mp.get_provider()
    for i, token in enumerate(tokens):
        tid = token["token_id"]
        outcome = token["outcome"]
        mid = provider.get_midpoint(tid)

        if mid is None:
            cols[i].metric(f"{outcome} (live)", "—")
            continue

        new_prices[tid] = mid
        prev = last_prices.get(tid)
        delta = None if prev is None else (mid - prev)
        delta_str = None
        if delta is not None and abs(delta) >= 1e-5:
            delta_str = f"{delta:+.4f}"

        cols[i].metric(
            f"{outcome} (live)",
            f"${mid:.4f}",
            delta=delta_str,
            help=f"Refreshes every {LIVE_TICK_SECONDS}s via the CLOB /midpoint endpoint.",
        )

    st.session_state[last_key] = new_prices


# ---------------------------------------------------------------------------
# Paper-trading framing (sandbox profile)
# ---------------------------------------------------------------------------

SANDBOX_PROFILE = "sandbox"


def is_sandbox_active() -> bool:
    """True when the currently-active profile is the sandbox.

    Pages use this to add paper-trading framing (banner, watermark, "this is
    simulated" reminders). Safe to call from any page; cheap (a single file
    read).
    """
    try:
        return profiles.active_profile() == SANDBOX_PROFILE
    except Exception:
        # Profile system not initialised yet — assume not sandbox.
        return False


def emergency_stop_banner() -> None:
    """Render a system-wide kill-switch banner at the top of every
    Streamlit page when ``data/.emergency_stop`` is present.

    No-op when the flag is not set. Called by ``app.py`` after the
    page renders so the banner lands above page-specific sidebar
    chrome. Cheap to call (single filesystem stat)."""
    from pm.core import emergency_stop
    status = emergency_stop.get_status()
    if status is None:
        return
    reason = status.get("reason") or "(no reason given)"
    since = status.get("since") or "?"
    st.error(
        f"⚠️ **EMERGENCY STOP ACTIVE** — new orders and new paper opens "
        f"are halted system-wide. Since `{since}` — reason: "
        f"_{reason}_. To resume: run `python -m emergency_stop deactivate` "
        f"in a shell.",
    )


def paper_trading_banner(show_help: bool = True) -> None:
    """Render a paper-trading warning banner at the top of an action page.

    No-op if not in sandbox. Pages that take user actions (Kelly Sizer,
    Portfolio, Alerts) call this at the top so it's impossible to confuse a
    sandbox session with the real `jeff` or `user2` profiles.

    If `show_help=True`, also includes an expander explaining the paper-
    trading workflow. Set to False on pages where the explanation would be
    repetitive.
    """
    if not is_sandbox_active():
        return
    st.info(
        "🧪 **PAPER TRADING — Sandbox profile.** Everything here is simulated. "
        f"Recording a buy or sell does **not** place an order on {venue_display()} — "
        "it just stores a row in the sandbox profile's `portfolio.json`. "
        "Switch profiles in the sidebar; reset sandbox anytime via ⚙️ Settings."
    )
    if show_help:
        with st.expander("📘 How paper trading works"):
            st.markdown("""
**The workflow:**

1. **Set your bankroll** — ⚙️ Settings → Bankroll (defaults to $1,000)
2. **Record hypothetical trades** — when you'd buy on the live venue, click
   **🟢 Buy** here and enter the market, outcome, shares, and price
3. **Watch P&L** — open positions mark-to-market against live prices;
   the Portfolio chart shows your sandbox value over time
4. **Close positions** — when markets resolve in real life, click **🔴 Sell**
   at the final price (or just delete the row)
5. **Reset anytime** — Settings → 🧹 Reset sandbox to start fresh

**Why it's useful:**
- Practice Kelly sizing and forecasting discipline at zero risk
- Validate whether your "edges" actually produce realized P&L
- Compare your Brier score to the market baseline on the Calibration page
- Onboard a new user — let them play in sandbox before they touch their
  own real profile

The sidebar always shows **🧹 Sandbox mode** when this profile is active so
you can never accidentally enter a real trade here when you meant to
record a real one.
""")


def simulated_watermark(fig, text: str = "SIMULATED") -> None:
    """Apply a diagonal "SIMULATED" watermark to a Plotly figure.

    No-op if not in sandbox. Used on chart-heavy views (Portfolio value-over-
    time, drawdown plots) so screenshots of sandbox runs can't be mistaken
    for real trading performance.
    """
    if not is_sandbox_active() or fig is None:
        return
    fig.add_annotation(
        text=text,
        xref="paper", yref="paper",
        x=0.5, y=0.5,
        showarrow=False,
        font=dict(size=64, color="rgba(180, 180, 180, 0.25)"),
        textangle=-25,
    )
