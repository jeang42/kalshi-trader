"""Portfolio page — transactions, derived positions, P&L, snapshots, sector exposure."""

from datetime import UTC, datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from pm.ledger import portfolio as pf
from pm.ledger import risk
from pm.ui.shared import (
    PRICE_MAX,
    PRICE_MIN,
    load_markets,
    outcome_price_prefill,
    paper_trading_banner,
    simulated_watermark,
    venue_name,
)

st.title("Sandbox Portfolio")
st.caption(
    "Paper-trade log for the active profile. **Not your real Kalshi "
    "state** — that's the 🏦 Live Portfolio page. Entries here come "
    "from manual buy/sell records and the agentic cron runners "
    "(paper simulations). On paper profiles (`sandbox`, `agentic_*`, "
    "`dex`) this is the only portfolio view since those profiles "
    "can't touch the Kalshi API."
)
paper_trading_banner()
st.caption(
    "Track buys and sells. Open positions are derived from the transaction log "
    "using the average-cost method."
)

settings = risk.load_settings()

with st.spinner("Loading markets for price lookup..."):
    df_markets = load_markets()

df_pos = pf.get_positions()
df_realized = pf.get_realized_pnl()

# Enrich with live prices + category + days-to-resolution
if not df_pos.empty and not df_markets.empty:
    df_pos = pf.enrich_with_current_prices(df_pos, df_markets)
    mkt_meta = df_markets.set_index("condition_id")[["category", "days_to_resolution"]]
    df_pos = df_pos.join(mkt_meta, on="condition_id", how="left")

# Summary metrics
total_realized = df_realized["realized_pnl"].sum() if not df_realized.empty else 0.0
total_cost = 0.0
total_value = 0.0
total_unrealized = 0.0
open_count = 0

if not df_pos.empty and "current_value" in df_pos.columns:
    total_cost = df_pos["cost_basis"].sum()
    total_value = df_pos["current_value"].sum()
    total_unrealized = df_pos["unrealized_pnl"].sum()
    open_count = len(df_pos)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cost Basis (open)", f"${total_cost:,.2f}")
    c2.metric("Current Value", f"${total_value:,.2f}")
    c3.metric(
        "Unrealized P&L", f"${total_unrealized:,.2f}",
        delta=f"{(total_unrealized/total_cost*100):+.1f}%" if total_cost else None,
    )
    c4.metric("Realized P&L", f"${total_realized:,.2f}",
              help="Profit/loss from completed sells")
elif total_realized:
    st.metric("Realized P&L", f"${total_realized:,.2f}")

# Auto-snapshot: record portfolio value if no snapshot in the last hour
pf.maybe_snapshot(
    cost_basis=total_cost,
    current_value=total_value,
    unrealized_pnl=total_unrealized,
    realized_pnl=total_realized,
    open_positions=open_count,
    min_interval_hours=1.0,
)

# ── Portfolio value over time chart ──────────────────────────────────────
df_snaps = pf.get_snapshots()
if len(df_snaps) >= 2:
    st.subheader("Portfolio value over time")
    st.caption(
        "Snapshots taken automatically each hour you visit this page. "
        "Net value = mark-to-market value of open positions + cumulative realized P&L."
    )
    fig_pnl = go.Figure()
    fig_pnl.add_trace(go.Scatter(
        x=df_snaps["timestamp_dt"], y=df_snaps["net_value"],
        mode="lines+markers", name="Net value",
        line=dict(color="royalblue", width=2),
        hovertemplate="$%{y:,.2f}<extra>Net value</extra>",
    ))
    fig_pnl.add_trace(go.Scatter(
        x=df_snaps["timestamp_dt"], y=df_snaps["cost_basis"],
        mode="lines", name="Cost basis (open)",
        line=dict(color="gray", width=1, dash="dot"),
        hovertemplate="$%{y:,.2f}<extra>Cost basis</extra>",
    ))
    fig_pnl.update_layout(
        xaxis_title="", yaxis_title="$",
        height=300, hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    simulated_watermark(fig_pnl)
    st.plotly_chart(fig_pnl, width="stretch")

    # Drawdown stat
    peak = df_snaps["net_value"].cummax()
    drawdown = df_snaps["net_value"] - peak
    max_dd = drawdown.min()
    if max_dd < 0:
        st.caption(
            f"Max drawdown so far: **${max_dd:,.2f}** "
            f"({(max_dd / peak.max() * 100):+.1f}% from peak)"
        )

    # Drawdown circuit breaker — surfaces a hard warning when current
    # (not max historical) drawdown exceeds the configured threshold.
    _dd = risk.current_drawdown(df_snaps)
    if risk.should_pause_for_drawdown(_dd, settings.drawdown_pause_pct):
        st.error(
            f"🛑 **Drawdown circuit breaker** — you're currently down "
            f"**{_dd['pct']*100:+.1f}%** from your peak (${_dd['peak']:,.2f} → "
            f"${_dd['current']:,.2f}). Threshold is "
            f"{settings.drawdown_pause_pct*100:.0f}%. The Kelly Sizer will "
            "show this same warning until your portfolio recovers — pause new "
            "positions and review what's happening before sizing more bets."
        )
elif len(df_snaps) == 1:
    st.caption(
        "📈 First snapshot recorded — return after an hour to start "
        "building your portfolio value chart."
    )

# ── Sector exposure pie chart (always shown when there are open positions
#    — was previously nested inside elif-snapshots-==1 which hid it after
#    the second visit.)
if not df_pos.empty and "category" in df_pos.columns and df_pos["category"].notna().any():
    exposure = (
        df_pos.assign(category=df_pos["category"].fillna("Unknown"))
              .groupby("category", as_index=False)["cost_basis"].sum()
              .sort_values("cost_basis", ascending=False)
    )
    fig_pie = px.pie(
        exposure, values="cost_basis", names="category",
        title="Exposure by category (cost basis)",
        hole=0.4,
    )
    fig_pie.update_traces(textposition="inside", textinfo="percent+label")
    fig_pie.update_layout(height=320, showlegend=True)
    st.plotly_chart(fig_pie, width="stretch")

    # Sector concentration warning — flags any category over the configured cap
    exposure_pct = risk.category_exposure_pct(df_pos)
    concentrated = risk.concentrated_categories(exposure_pct, settings.max_category_exposure_pct)
    if concentrated:
        items = ", ".join(f"**{cat}** ({pct*100:.0f}%)" for cat, pct in concentrated)
        st.warning(
            f"⚠️ Concentration: {items} exceed the "
            f"{settings.max_category_exposure_pct*100:.0f}% per-category cap. "
            "Heavy single-category exposure means a single news event can hurt the "
            "whole portfolio at once."
        )

st.divider()

# ── Positions table ──────────────────────────────────────────────────────
if df_pos.empty:
    st.info("No positions yet. Add one below.")
else:
    st.subheader("Open positions")
    # Baseball-sidecar model column for MLB holdings: shows what the
    # model currently says about THIS YES outcome (the side we hold).
    # Useful for take-profit / cash-out decisions — if the model
    # disagrees, that's a sell signal independent of the market price.
    try:
        import baseball as _baseball
        def _model_for_row(cid: str) -> float | None:
            r = _baseball.predict_for_ticker(str(cid))
            return r["yes_prob"] if r else None
        df_pos["model_yes"] = df_pos["condition_id"].apply(_model_for_row)
    except Exception:  # noqa: BLE001
        df_pos["model_yes"] = None

    display_cols = ["question", "outcome", "category", "shares", "avg_price", "cost_basis",
                    "current_price", "model_yes", "current_value", "unrealized_pnl", "pnl_pct",
                    "days_to_resolution"]
    display_cols = [c for c in display_cols if c in df_pos.columns]
    df_show = df_pos[display_cols].copy()
    if "model_yes" in df_show:
        df_show["model_yes"] = df_show["model_yes"].apply(
            lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—"
        )
        df_show = df_show.rename(columns={"model_yes": "model P(YES)"})
    for col in ["avg_price", "current_price"]:
        if col in df_show:
            df_show[col] = df_show[col].apply(lambda x: f"${x:.3f}" if pd.notna(x) else "—")
    for col in ["cost_basis", "current_value", "unrealized_pnl"]:
        if col in df_show:
            df_show[col] = df_show[col].apply(lambda x: f"${x:,.2f}" if pd.notna(x) else "—")
    if "pnl_pct" in df_show:
        df_show["pnl_pct"] = df_show["pnl_pct"].apply(lambda x: f"{x:+.1f}%" if pd.notna(x) else "—")
    if "days_to_resolution" in df_show:
        df_show["days_to_resolution"] = df_show["days_to_resolution"].apply(
            lambda x: f"{x:.1f}d" if pd.notna(x) else "—"
        )

    st.dataframe(df_show, width="stretch", hide_index=True)

    st.download_button(
        "📥 Download positions as CSV",
        data=df_pos.to_csv(index=False).encode("utf-8"),
        file_name=f"{venue_name()}_portfolio_{datetime.now(UTC).strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )

    # Warn on positions near resolution
    if "days_to_resolution" in df_pos.columns:
        near = df_pos[df_pos["days_to_resolution"].between(0, 7, inclusive="both")]
        if not near.empty:
            st.warning(
                f"⚠️ {len(near)} position(s) resolve within 7 days. "
                "Review your conviction or consider closing."
            )

# Realized P&L breakdown
if not df_realized.empty:
    with st.expander(f"Realized P&L by market ({len(df_realized)})"):
        disp_r = df_realized.copy()
        disp_r["realized_pnl"] = disp_r["realized_pnl"].apply(lambda x: f"${x:,.2f}")
        st.dataframe(disp_r, width="stretch", hide_index=True)

st.divider()

# ── Buy / Sell / History tabs ────────────────────────────────────────────
st.subheader("Record a transaction")
buy_tab, sell_tab, history_tab = st.tabs(["🟢 Buy", "🔴 Sell", "📜 History"])

# ----- BUY -----
with buy_tab:
    with st.form("buy_form"):
        if not df_markets.empty:
            market_options = df_markets["question"].tolist()
            sel_market = st.selectbox("Market", market_options, key="buy_market")
            market_row = df_markets[df_markets["question"] == sel_market].iloc[0]
            buy_outcomes = market_row.get("outcomes") or ["Yes", "No"]
            buy_outcome_prices = market_row.get("outcome_prices") or []
        else:
            sel_market = st.text_input("Market question", key="buy_q")
            buy_outcomes = ["Yes", "No"]
            buy_outcome_prices = []
            market_row = {}

        col1, col2, col3 = st.columns(3)
        with col1:
            sel_outcome = st.selectbox("Outcome", buy_outcomes, key="buy_outcome")
        with col2:
            shares = st.number_input(
                "Shares", min_value=0.01, value=10.0, step=1.0, key="buy_shares"
            )
        with col3:
            avg_price = st.number_input(
                "Buy price", min_value=PRICE_MIN, max_value=PRICE_MAX,
                value=outcome_price_prefill(buy_outcomes, buy_outcome_prices, sel_outcome),
                step=0.001, format="%.4f", key="buy_price",
            )

        if st.form_submit_button("Record buy", type="primary"):
            cid = market_row.get("condition_id", "") if not df_markets.empty else ""
            try:
                pf.add_buy(sel_market, cid, sel_outcome, shares, avg_price)
                st.success(f"Bought {shares:g} shares of {sel_outcome} @ ${avg_price:.3f}")
                st.cache_data.clear()
                st.rerun()
            except ValueError as e:
                st.error(str(e))

# ----- SELL -----
with sell_tab:
    if df_pos.empty:
        st.info("No open positions to sell.")
    else:
        with st.form("sell_form"):
            pos_labels = [
                f"{r['outcome']} — {r['question'][:50]} ({r['shares']:g} shares @ ${r['avg_price']:.3f})"
                for _, r in df_pos.iterrows()
            ]
            sel_idx = st.selectbox(
                "Position to sell",
                range(len(df_pos)),
                format_func=lambda i: pos_labels[i],
                key="sell_pos",
            )
            pos_row = df_pos.iloc[sel_idx]

            col1, col2 = st.columns(2)
            with col1:
                sell_shares = st.number_input(
                    "Shares to sell", min_value=0.01,
                    max_value=float(pos_row["shares"]),
                    value=float(pos_row["shares"]),
                    step=1.0, key="sell_shares",
                )
            with col2:
                current_price = pos_row.get("current_price") if "current_price" in pos_row else None
                raw_default = (
                    float(current_price) if pd.notna(current_price)
                    else float(pos_row["avg_price"])
                )
                default_sell = max(PRICE_MIN, min(PRICE_MAX, raw_default))
                sell_price = st.number_input(
                    "Sell price", min_value=PRICE_MIN, max_value=PRICE_MAX,
                    value=default_sell, step=0.001, format="%.4f",
                    key="sell_price",
                )

            preview_pnl = sell_shares * (sell_price - float(pos_row["avg_price"]))
            st.caption(f"Estimated realized P&L: **${preview_pnl:+,.2f}**")

            if st.form_submit_button("Record sell", type="primary"):
                try:
                    pf.add_sell(
                        pos_row["question"], pos_row["condition_id"],
                        pos_row["outcome"], sell_shares, sell_price,
                    )
                    st.success(
                        f"Sold {sell_shares:g} shares of {pos_row['outcome']} "
                        f"@ ${sell_price:.3f} — realized ${preview_pnl:+,.2f}"
                    )
                    st.cache_data.clear()
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

# ----- HISTORY -----
with history_tab:
    df_tx = pf.get_transactions()
    if df_tx.empty:
        st.info("No transactions yet.")
    else:
        st.caption(f"{len(df_tx)} transactions, oldest first")
        disp_tx = df_tx[["timestamp", "type", "question", "outcome", "shares", "price"]].copy()
        disp_tx["price"] = disp_tx["price"].apply(lambda x: f"${x:.3f}")
        disp_tx["shares"] = disp_tx["shares"].apply(lambda x: f"{x:g}")
        st.dataframe(disp_tx, width="stretch", hide_index=True)

        st.download_button(
            "📥 Download transactions as CSV",
            data=df_tx.to_csv(index=False).encode("utf-8"),
            file_name=f"{venue_name()}_transactions_{datetime.now(UTC).strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            key="dl_tx",
        )

        with st.expander("⚠️ Delete a transaction (advanced)"):
            st.caption(
                "Deletes the transaction at the given chronological index. "
                "Use only to undo typos — deleting old buys can invalidate later sells."
            )
            idx_to_delete = st.number_input(
                "Transaction index", min_value=0,
                max_value=max(0, len(df_tx) - 1), step=1, key="del_tx",
            )
            if st.button("Delete transaction", type="secondary"):
                if pf.delete_transaction(idx_to_delete):
                    st.success("Deleted.")
                    st.cache_data.clear()
                    st.rerun()
