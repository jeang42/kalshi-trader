"""Alerts page — threshold notifications with fire-once semantics."""

from datetime import UTC, datetime

import streamlit as st

from pm.ledger import alerts as al
from pm.ui.shared import check_alerts_and_notify, load_markets, paper_trading_banner, venue_name

st.title("Price Alerts")
paper_trading_banner(show_help=False)
st.caption(
    "Get notified when a market price crosses your threshold. Alerts are "
    "checked automatically every time you visit any page that loads market "
    "data (most pages, every page render). Once fired, an alert stays in "
    "the triggered state until you reset or delete it — no spamming."
)

with st.spinner("Loading markets..."):
    df_alerts_markets = load_markets()

# Check immediately on this page too
check_alerts_and_notify(df_alerts_markets)

df_alerts = al.get_alerts()
stats = al.summary_stats()

c1, c2, c3 = st.columns(3)
c1.metric("Total alerts", stats["total"])
c2.metric("Active (watching)", stats["active"])
c3.metric("Triggered", stats["triggered"])

# ── Add alert form ────────────────────────────────────────────────────────
st.divider()
st.subheader("Create an alert")

if df_alerts_markets.empty:
    st.warning("Could not load markets — alerts can't be created right now.")
else:
    with st.form("add_alert_form"):
        market_options = df_alerts_markets["question"].tolist()
        sel_market = st.selectbox("Market", market_options, key="alert_market")
        market_row = df_alerts_markets[df_alerts_markets["question"] == sel_market].iloc[0]
        outcomes = market_row.get("outcomes") or ["Yes", "No"]
        outcome_prices = market_row.get("outcome_prices") or []

        col1, col2, col3 = st.columns(3)
        with col1:
            sel_outcome = st.selectbox("Outcome", outcomes, key="alert_outcome")
        with col2:
            direction = st.selectbox(
                "Trigger when price goes", ["above", "below"], key="alert_direction"
            )
        with col3:
            try:
                idx = list(outcomes).index(sel_outcome)
                current_price = float(outcome_prices[idx])
            except (ValueError, IndexError, TypeError):
                current_price = 0.5
            threshold = st.number_input(
                "Threshold",
                min_value=0.0001, max_value=0.9999,
                value=round(max(0.0001, min(0.9999, current_price)), 4),
                step=0.01, format="%.4f", key="alert_threshold",
            )

        try:
            idx = list(outcomes).index(sel_outcome)
            cur = float(outcome_prices[idx])
            gap = threshold - cur
            gap_sign = "+" if gap >= 0 else "−"
            st.caption(
                f"Current {sel_outcome} price: **${cur:.3f}** — "
                f"threshold is {gap_sign}${abs(gap):.3f} from current."
            )
        except Exception:
            pass

        if st.form_submit_button("Create alert", type="primary"):
            cid = market_row.get("condition_id", "")
            al.add_alert(sel_market, cid, sel_outcome, direction, threshold)
            st.success("Alert created.")
            st.rerun()

# ── Triggered alerts ──────────────────────────────────────────────────────
triggered_df = df_alerts[df_alerts["triggered"].astype(bool)] if not df_alerts.empty else df_alerts
if not triggered_df.empty:
    st.divider()
    st.subheader(f"🔔 Triggered ({len(triggered_df)})")
    for _, a in triggered_df.iterrows():
        with st.container():
            col_q, col_p, col_r, col_x = st.columns([5, 2, 1, 1])
            arrow = "↑" if a["direction"] == "above" else "↓"
            col_q.write(
                f"**{a['question'][:60]}** — {a['outcome']} {arrow} "
                f"${float(a['threshold']):.3f}"
            )
            tp = a.get("triggered_price")
            ta = a.get("triggered_at", "")[:16].replace("T", " ") if a.get("triggered_at") else ""
            col_p.write(f"@ ${float(tp):.3f}\n\n{ta}" if tp is not None else "")
            if col_r.button("↻ Reset", key=f"reset_{a['id']}",
                            help="Un-trigger so the alert can fire again"):
                al.reset_alert(a["id"])
                st.rerun()
            if col_x.button("🗑️", key=f"del_trig_{a['id']}", help="Delete alert"):
                al.delete_alert(a["id"])
                st.rerun()

# ── Active (watching) alerts ──────────────────────────────────────────────
active_df = df_alerts[~df_alerts["triggered"].astype(bool)] if not df_alerts.empty else df_alerts
if not active_df.empty:
    st.divider()
    st.subheader(f"Watching ({len(active_df)})")
    if not df_alerts_markets.empty:
        price_map = al._build_price_map(df_alerts_markets)
    else:
        price_map = {}
    for _, a in active_df.iterrows():
        with st.container():
            col_q, col_t, col_p, col_x = st.columns([5, 2, 2, 1])
            arrow = "↑" if a["direction"] == "above" else "↓"
            col_q.write(
                f"**{a['question'][:60]}** — {a['outcome']} {arrow} "
                f"${float(a['threshold']):.3f}"
            )
            col_t.caption(f"Direction: **{a['direction']}** ${float(a['threshold']):.4f}")
            current = price_map.get((a["condition_id"], a["outcome"]))
            if current is not None:
                gap = float(a["threshold"]) - current
                col_p.metric("Current", f"${current:.3f}",
                             delta=f"{(-gap):+.3f} to go", delta_color="normal")
            else:
                col_p.caption("price unavailable")
            if col_x.button("🗑️", key=f"del_active_{a['id']}", help="Delete alert"):
                al.delete_alert(a["id"])
                st.rerun()
elif df_alerts.empty:
    st.info("No alerts yet. Create one above.")

# CSV export
if not df_alerts.empty:
    st.divider()
    st.download_button(
        "📥 Download alerts as CSV",
        data=df_alerts.to_csv(index=False).encode("utf-8"),
        file_name=f"{venue_name()}_alerts_{datetime.now(UTC).strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )
