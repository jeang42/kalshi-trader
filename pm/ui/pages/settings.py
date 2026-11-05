"""Settings page — risk-management thresholds + persistent bankroll."""

import streamlit as st

from pm.archive import book_archive
from pm.core import profiles
from pm.feeds import llm_cache
from pm.ledger import risk
from pm.research import backtest_history

st.title("Settings")
st.caption(
    "Risk-management thresholds used by the Kelly Sizer and Portfolio pages. "
    "All values persist to `settings.json` (gitignored) so they apply across "
    "sessions. See the **Recommendations Design** doc for the rationale behind "
    "each one."
)

s = risk.load_settings()

with st.form("risk_settings"):
    st.subheader("Bankroll")
    bankroll = st.number_input(
        "Total trading capital ($)",
        min_value=1.0,
        value=float(s.bankroll),
        step=100.0,
        help="The denominator for all position-size limits. Update when you "
             "fund or withdraw from your trading account.",
    )

    st.divider()
    st.subheader("Kelly limits")

    max_kelly_fraction = st.slider(
        "Max Kelly fraction",
        min_value=0.05, max_value=1.0, value=float(s.max_kelly_fraction),
        step=0.05, format="%.2f",
        help="Hard cap on any single Kelly-recommended bet, regardless of edge. "
             "Most practitioners use 0.25 (¼ Kelly). Lower = more conservative.",
    )
    max_market_position_pct = st.slider(
        "Max position per market (% of bankroll)",
        min_value=0.01, max_value=0.50, value=float(s.max_market_position_pct),
        step=0.01, format="%.2f",
        help="Warn when a single market position would exceed this fraction of "
             "your bankroll. Diversification floor.",
    )

    st.divider()
    st.subheader("Portfolio guardrails")

    max_category_exposure_pct = st.slider(
        "Max category exposure (% of cost basis)",
        min_value=0.10, max_value=1.0, value=float(s.max_category_exposure_pct),
        step=0.05, format="%.2f",
        help="Flash a warning on the Portfolio page when any one category "
             "(Crypto, Sports, etc.) holds more than this fraction of your "
             "open cost basis.",
    )
    drawdown_pause_pct = st.slider(
        "Drawdown circuit breaker (% from peak)",
        min_value=0.05, max_value=0.50, value=float(s.drawdown_pause_pct),
        step=0.01, format="%.2f",
        help="When net portfolio value falls more than this fraction below its "
             "peak, the Kelly Sizer shows a hard warning recommending you pause "
             "and review before sizing new positions.",
    )

    if st.form_submit_button("Save settings", type="primary"):
        risk.save_settings(risk.Settings(
            bankroll=bankroll,
            max_kelly_fraction=max_kelly_fraction,
            max_market_position_pct=max_market_position_pct,
            max_category_exposure_pct=max_category_exposure_pct,
            drawdown_pause_pct=drawdown_pause_pct,
        ))
        st.success("Saved.")
        st.cache_data.clear()
        st.rerun()

st.divider()
st.caption(
    "These thresholds are *anti*-recommendations: caps and warnings. They "
    "never tell you what to buy. See `docs/recommendations-design.md` for the "
    "full roadmap of recommendation features (Tiers 1–5)."
)

# ---------------------------------------------------------------------------
# Profile management
# ---------------------------------------------------------------------------

st.divider()
st.subheader("👥 Profile")

_active = profiles.active_profile()
st.caption(
    f"Active profile: **{_active}**. Switch from the sidebar at the top. "
    "Each profile has its own portfolio, forecasts, alerts, and settings."
)

if profiles.is_agentic_profile(_active):
    st.warning(
        "🤖 **Agentic profile.** This profile is intended to be auto-driven "
        "by `strategy_runner.py` — its `forecasts.json` and `portfolio.json` "
        "get appended to by a cron job, and resolved positions are "
        "auto-closed at $1/$0. Manual buys/sells you record here will "
        "coexist with the runner's entries but won't be de-duped against "
        "them. Run "
        "`python strategy_runner.py --profile agentic --strategy market_consensus --loop --interval 1800` "
        "in tmux/systemd/cron."
    )

with st.expander("Create a new profile"):
    new_name = st.text_input(
        "Profile name", placeholder="e.g. dad, alice, paper-trading",
        help="Letters/numbers/hyphens. The name becomes a directory under data/."
    )
    if st.button("Create profile", type="secondary"):
        try:
            created = profiles.create_profile(new_name.strip().lower())
            if created:
                st.success(f"Profile **{new_name}** created. Switch to it from the sidebar.")
                st.rerun()
            else:
                st.warning(f"Profile **{new_name}** already exists.")
        except ValueError as e:
            st.error(str(e))

# Sandbox reset — only visible from the sandbox itself, since wiping someone
# else's profile by accident would be a footgun.
if _active == "sandbox":
    st.divider()
    st.subheader("🧹 Reset sandbox")
    st.warning(
        "This will delete the sandbox profile's `portfolio.json`, "
        "`forecasts.json`, `alerts.json`, and `settings.json` and start "
        "fresh. **Real profiles (jeff, dex) are untouched.**"
    )
    confirm = st.checkbox("I understand this wipes all sandbox data")
    if st.button("Wipe sandbox now", type="primary", disabled=not confirm):
        n = profiles.reset_profile("sandbox")
        st.success(f"Sandbox reset. {n} file(s) removed.")
        st.cache_data.clear()
        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# LLM forecast & match cache
# ──────────────────────────────────────────────────────────────────────────
st.divider()
st.subheader("🧠 LLM cache")
st.caption(
    "Persistent SQLite store for LLM forecast and match-verification "
    "responses. Saves re-paying inference cost when you re-tune Edges-page "
    "thresholds. Shared across profiles — the (question → LLM answer) "
    "mapping doesn't depend on which user is asking."
)

_stats = llm_cache.stats()


def _fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


cc1, cc2, cc3, cc4 = st.columns(4)
cc1.metric("Entries", _stats["entries"])
cc2.metric("Expired (purgeable)", _stats["expired"])
cc3.metric("Oldest entry age", _fmt_age(_stats["oldest_age_s"]))
cc4.metric("File size", f"{_stats['file_bytes'] / 1024:.1f} KB")

bc1, bc2 = st.columns([1, 1])
with bc1:
    if st.button("🧹 Purge expired", help="Removes entries past their TTL only."):
        n = llm_cache.purge_expired()
        st.success(f"Purged {n} expired entr{'y' if n == 1 else 'ies'}.")
        st.rerun()
with bc2:
    if st.button("🗑️ Clear all", type="secondary",
                 help="Wipes every cached forecast and match verdict. "
                      "Next Edges scan will re-pay LLM inference cost."):
        n = llm_cache.clear()
        st.success(f"Cleared {n} entr{'y' if n == 1 else 'ies'}.")
        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# Order-book archive (path 1 — feeds future historical-slippage backtests)
# ──────────────────────────────────────────────────────────────────────────
st.divider()
st.subheader("📚 Order-book archive")
st.caption(
    "Persistent SQLite store of order-book snapshots written by "
    "`book_poller.py`. The data captured today becomes historical "
    "data tomorrow — the archive only becomes useful for backtest "
    "honesty after weeks of polling. Run "
    "`python book_poller.py --loop --interval 300` in tmux/systemd/cron."
)

_arch = book_archive.stats()


def _fmt_age2(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


ac1, ac2, ac3, ac4 = st.columns(4)
ac1.metric("Snapshots", f"{_arch['entries']:,}")
ac2.metric("Distinct markets", f"{_arch['markets']:,}")
ac3.metric("Oldest snapshot", _fmt_age2(_arch["oldest_age_s"]))
ac4.metric("File size", f"{_arch['file_bytes'] / (1024*1024):.2f} MB"
                          if _arch['file_bytes'] > 1024 * 1024
                          else f"{_arch['file_bytes'] / 1024:.1f} KB")

if _arch["venues"]:
    venue_str = "  ·  ".join(f"**{v}**: {c:,}" for v, c in _arch["venues"])
    st.caption(f"By venue: {venue_str}")

ab1, ab2 = st.columns([1, 1])
with ab1:
    days_keep = st.number_input(
        "Days to keep", min_value=7, max_value=365, value=90, step=7,
        help="Snapshots older than this many days will be deleted when "
             "you click Purge. 90 days is a reasonable default — older "
             "than that, the book has likely shifted enough that the "
             "snapshot is unrepresentative anyway.",
        key="archive_days_keep",
    )
    if st.button("🧹 Purge old snapshots",
                 help="Delete snapshots older than the configured days. "
                      "Cheaper than Clear All; preserves recent history."):
        n = book_archive.purge_older_than(days=int(days_keep))
        st.success(f"Purged {n:,} snapshot{'s' if n != 1 else ''}.")
        st.rerun()
with ab2:
    if st.button("🗑️ Clear archive", type="secondary",
                 help="Wipes every captured snapshot. Useful when starting "
                      "over after a schema change; otherwise leave alone — "
                      "the archive's value is its accumulated history."):
        n = book_archive.clear()
        st.success(f"Cleared {n:,} snapshot{'s' if n != 1 else ''}.")
        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# Backtest run history (auto-saved by the 🧪 Backtest page)
# ──────────────────────────────────────────────────────────────────────────
st.divider()
st.subheader("📜 Backtest history")
st.caption(
    "Persisted backtest results, one JSON file per run. Reload them on "
    "the 📜 Backtest History page without re-paying the API cost for the "
    "dataset. Shared across profiles."
)

_bhs = backtest_history.stats()


def _fmt_age3(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


hc1, hc2, hc3, hc4 = st.columns(4)
hc1.metric("Saved runs", f"{_bhs['runs']:,}")
hc2.metric("Newest age", _fmt_age3(_bhs["newest_age_s"]))
hc3.metric("Oldest age", _fmt_age3(_bhs["oldest_age_s"]))
hc4.metric("Total size",
           f"{_bhs['bytes'] / (1024*1024):.2f} MB"
           if _bhs['bytes'] > 1024 * 1024
           else f"{_bhs['bytes'] / 1024:.1f} KB")

if st.button(
    "🗑️ Clear all backtest runs", type="secondary",
    help="Removes every persisted backtest result. The 📜 Backtest "
         "History page will be empty until new runs come in.",
):
    n = backtest_history.clear_history()
    st.success(f"Cleared {n:,} run{'s' if n != 1 else ''}.")
    st.rerun()
