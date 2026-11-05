"""Orders page — view + cancel currently-resting Kalshi orders.

Phase 4 keeps this simple: a table of resting orders with one-click
cancel, and a tail of today's audit-log entries so the user can
verify every submission landed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from pm.venue import kalshi_auth as ka
from pm.venue import kalshi_orders as ko

st.title("Orders (Kalshi)")
st.caption(
    "Currently-resting orders on your live Kalshi account. Read-only "
    "for non-owner profiles; cancel buttons appear only when live "
    "trading is enabled."
)

if not ka.is_configured():
    if not ka.is_owner_profile_active():
        st.warning(
            f"The Kalshi API is locked to the **{ka.owner_profile()}** profile. "
            "Switch to it in the sidebar."
        )
    else:
        st.warning(
            "Kalshi API key is not configured. Set `KALSHI_API_KEY_ID` and "
            "`KALSHI_PRIVATE_KEY_PATH` in `.env`."
        )
    st.stop()


@st.cache_data(ttl=15, show_spinner=False)
def _resting() -> pd.DataFrame:
    return ko.get_resting_orders(limit=200)


col_a, col_b = st.columns([5, 1])
with col_b:
    if st.button("🔄 Refresh"):
        st.cache_data.clear()
        st.rerun()

try:
    df = _resting()
except Exception as e:
    st.error(f"Couldn't fetch resting orders: {e}")
    st.stop()

if df.empty:
    st.info("No resting orders.")
else:
    show = df.copy()
    show["yes_price"] = show["yes_price_cents"] / 100
    show["no_price"] = show["no_price_cents"] / 100
    st.dataframe(
        show[[
            "created_time", "ticker", "side", "action",
            "count_remaining", "yes_price", "no_price", "status",
        ]].style.format({"yes_price": "${:.2f}", "no_price": "${:.2f}"}),
        width="stretch", hide_index=True,
    )

    if ko.is_live_enabled():
        st.divider()
        st.subheader("Cancel order")
        oid = st.selectbox(
            "Order to cancel",
            options=df["order_id"].tolist(),
            format_func=lambda o: f"{o[:12]}…  ·  "
                                    f"{df[df['order_id']==o].iloc[0]['ticker']}",
        )
        if st.button("🗑️ Cancel this order", type="secondary"):
            try:
                resp = ko.cancel_order(oid)
                st.success(f"Cancel submitted: {resp}")
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Cancel failed: {e}")

# ──────────────────────────────────────────────────────────────────────────
# Audit log tail
# ──────────────────────────────────────────────────────────────────────────
st.divider()
st.subheader("Today's order activity (audit log)")

log_path = Path(ko._orders_log_path())
if not log_path.exists():
    st.caption("No audit log yet — no order activity recorded.")
else:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    rows: list[dict] = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(rec.get("ts", "")).startswith(today):
                    rows.append(rec)
    except OSError as e:
        st.error(f"Couldn't read audit log: {e}")
        rows = []

    if not rows:
        st.caption("No entries today.")
    else:
        df_audit = pd.DataFrame(rows)
        st.dataframe(df_audit.tail(50), width="stretch",
                      hide_index=True)
        st.caption(f"Log file: `{log_path}`")
