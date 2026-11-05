"""Markets page — landing page. Browse active markets with search & category filter."""

from datetime import UTC, datetime

import pandas as pd
import plotly.express as px
import streamlit as st

from pm.ui.shared import (
    check_alerts_and_notify,
    load_markets,
    load_markets_closing_soon,
    venue_display,
    venue_name,
)

st.title(f"Active {venue_display()} Markets")

col1, col2, col3 = st.columns([3, 2, 1])
with col1:
    search = st.text_input("Search markets", placeholder="e.g. election, bitcoin, NBA")
with col2:
    all_categories = ["All", "Crypto", "Entertainment", "Finance", "Geopolitics",
                      "Politics", "Sports", "Tech", "Other"]
    category = st.selectbox("Category", all_categories)
with col3:
    limit = st.selectbox("Show top", [50, 100, 200], index=1)

# Time-to-resolution filter — keeps the long-dated "Mars 2099" /
# supervolcano markets out of view when you're testing quick trades.
# When max_days is set to a short value the default volume-ordered
# fetch can hide ALL near-term markets, so we switch to the time-
# filtered fetch path (provider.get_markets_closing_within).
col_min, col_max = st.columns([1, 1])
with col_min:
    min_days = st.number_input(
        "Min days to resolution",
        min_value=0, max_value=3650, value=0, step=1,
        help="Exclude markets resolving sooner than this. 0 = no minimum.",
    )
with col_max:
    max_days = st.number_input(
        "Max days to resolution",
        min_value=0, max_value=3650, value=90, step=1,
        help="Exclude markets that don't resolve until further out. 0 = no "
             "maximum. Default 90d to focus on actionable markets. When set "
             "≤ 90 days, switches to a closing-soonest fetch path so short-"
             "dated markets aren't buried by long-dated ones.",
    )

with st.spinner("Loading markets..."):
    if 0 < max_days <= 90:
        df_all = load_markets_closing_soon(float(max_days), limit=500)
        st.caption(f"Fetched closing-soonest markets within {max_days} days.")
    else:
        df_all = load_markets()

# Auto-check alerts against the freshest cached data
check_alerts_and_notify(df_all)

if df_all.empty:
    st.warning("No markets found.")
    st.stop()

# Client-side filters
df = df_all.copy()
if search:
    df = df[df["question"].str.contains(search, case=False, na=False)]
if category != "All":
    df = df[df["category"] == category]
if "days_to_resolution" in df.columns:
    if min_days > 0:
        df = df[df["days_to_resolution"].fillna(-1) >= min_days]
    if max_days > 0:
        df = df[df["days_to_resolution"].fillna(1e9) <= max_days]
df = df.head(limit)

if df.empty:
    st.info("No markets match the current filters.")
    st.stop()

# Summary metrics
m1, m2, m3 = st.columns(3)
m1.metric("Markets loaded", len(df))
m2.metric("Total 24h Volume", f"${df['volume_24hr'].sum():,.0f}")
m3.metric("Total Liquidity", f"${df['liquidity'].sum():,.0f}")

st.divider()

# Top markets by 24h volume bar chart
top_n = df.head(20).copy()
top_n["short_q"] = top_n["question"].str[:60]
fig = px.bar(
    top_n,
    x="volume_24hr",
    y="short_q",
    orientation="h",
    title="Top 20 Markets by 24h Volume",
    labels={"volume_24hr": "24h Volume ($)", "short_q": ""},
    color="volume_24hr",
    color_continuous_scale="Blues",
)
fig.update_layout(height=600, yaxis={"autorange": "reversed"}, coloraxis_showscale=False)
st.plotly_chart(fig, width="stretch")

st.divider()

# Full table
st.subheader("All markets")
# Baseball sidecar model column — populated only for Kalshi MLB rows.
# Pulls every prediction from the sidecar state once (in-memory join),
# rather than calling predict_for_ticker per row. Drops to N/A for any
# market the sidecar doesn't know about (non-MLB, future games beyond
# the runner's window, etc).
df_with_model = df.copy()
try:
    import baseball as _baseball
    is_mlb = df_with_model["condition_id"].astype(str).str.startswith("KXMLBGAME-")
    if is_mlb.any():
        df_with_model["model_yes"] = df_with_model.loc[is_mlb, "condition_id"].apply(
            lambda t: (lambda r: r["yes_prob"] if r else None)(_baseball.predict_for_ticker(str(t)))
        )
        df_with_model.loc[~is_mlb, "model_yes"] = None
    else:
        df_with_model["model_yes"] = None
except Exception:  # noqa: BLE001 — model column is optional
    df_with_model["model_yes"] = None

display_cols = ["question", "category", "volume_24hr", "liquidity",
                "outcome_prices", "model_yes", "days_to_resolution", "end_date"]
display_cols = [c for c in display_cols if c in df_with_model.columns]
df_display = df_with_model[display_cols].copy()
df_display["volume_24hr"] = df_display["volume_24hr"].apply(lambda x: f"${x:,.0f}")
df_display["liquidity"] = df_display["liquidity"].apply(lambda x: f"${x:,.0f}")
if "days_to_resolution" in df_display.columns:
    df_display["days_to_resolution"] = df_display["days_to_resolution"].apply(
        lambda x: f"{x:.1f}d" if pd.notna(x) else "—"
    )
if "model_yes" in df_display.columns:
    df_display["model_yes"] = df_display["model_yes"].apply(
        lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—"
    )
    df_display = df_display.rename(columns={"model_yes": "model P(YES)"})
st.dataframe(df_display, width="stretch", hide_index=True)
n_with_model = df_with_model["model_yes"].notna().sum() if "model_yes" in df_with_model.columns else 0
if n_with_model > 0:
    st.caption(f"⚾ Baseball-sidecar model probability shown on {int(n_with_model)} MLB row(s).")

# CSV export
st.download_button(
    "📥 Download as CSV",
    data=df_with_model[display_cols].to_csv(index=False).encode("utf-8"),
    file_name=f"{venue_name()}_markets_{datetime.now(UTC).strftime('%Y%m%d_%H%M')}.csv",
    mime="text/csv",
)
