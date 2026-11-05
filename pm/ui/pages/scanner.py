"""Market Scanner page — narrow spreads, price divergence, multi-outcome gaps."""

import pandas as pd
import plotly.express as px
import streamlit as st

from pm.ui.shared import load_markets, load_markets_closing_soon, venue_display, venue_name
from pm.venue import polymarket_client as pm

st.title("Market Scanner")

max_days = st.sidebar.number_input(
    "Max days to resolution", min_value=0, max_value=3650, value=90, step=30,
    help="0 = no limit. Default 90d to focus on actionable markets.",
)

with st.spinner("Loading markets..."):
    if 0 < max_days <= 90:
        df_scan = load_markets_closing_soon(float(max_days), limit=500)
    else:
        df_scan = load_markets()

min_liquidity = st.sidebar.number_input("Min liquidity ($)", value=500, step=250)

tab1, tab2, tab3, tab4 = st.tabs([
    "📉 Narrow Spreads",
    "⚡ Price Divergence",
    "🔢 Multi-Outcome Gaps",
    "🌐 Cross-Market Arb",
])

# ── Tab 1: Narrow Spreads ────────────────────────────────────────────────
with tab1:
    st.subheader("Tightest bid-ask spreads")
    st.caption("Low spread = cheapest markets to enter and exit. Good candidates for active trading.")
    max_spread = st.slider("Max spread", 0.001, 0.05, 0.01, step=0.001, format="%.3f")
    df_spreads = pm.scan_narrow_spreads(df_scan, max_spread=max_spread, min_liquidity=min_liquidity)
    if df_spreads.empty:
        st.info("No markets found with those filters.")
    else:
        st.success(f"{len(df_spreads)} markets")
        disp = df_spreads.copy()
        disp["yes_price"] = disp["yes_price"].apply(lambda x: f"${x:.3f}" if pd.notna(x) else "—")
        disp["spread"] = disp["spread"].apply(lambda x: f"{x:.4f}")
        disp["best_bid"] = disp["best_bid"].apply(lambda x: f"${x:.3f}")
        disp["best_ask"] = disp["best_ask"].apply(lambda x: f"${x:.3f}")
        disp["volume_24hr"] = disp["volume_24hr"].apply(lambda x: f"${x:,.0f}")
        disp["liquidity"] = disp["liquidity"].apply(lambda x: f"${x:,.0f}")
        st.dataframe(disp, width="stretch", hide_index=True)
        st.download_button(
            "📥 Download as CSV",
            data=df_spreads.to_csv(index=False).encode("utf-8"),
            file_name=f"{venue_name()}_narrow_spreads.csv",
            mime="text/csv",
            key="dl_spreads",
        )
        fig = px.histogram(df_spreads, x="spread", nbins=20,
                           title="Spread distribution", labels={"spread": "Bid-Ask Spread"})
        st.plotly_chart(fig, width="stretch")

# ── Tab 2: Price Divergence ──────────────────────────────────────────────
with tab2:
    st.subheader("Last trade vs. current quote divergence")
    st.caption(
        "Large gap between the last traded price and the current mid-quote can signal "
        "a stale quote, thin book, or rapid price movement — worth investigating. "
        "Binary markets only (multi-outcome markets are skipped because lastTradePrice "
        "is a single per-market value and can't be cleanly compared to per-outcome mids)."
    )
    min_div = st.slider("Min divergence", 0.01, 0.30, 0.05, step=0.01, format="%.2f")
    df_div = pm.scan_price_divergence(df_scan, min_divergence=min_div, min_liquidity=min_liquidity)
    if df_div.empty:
        st.info("No markets found with those filters.")
    else:
        st.warning(f"{len(df_div)} markets with notable divergence")
        disp2 = df_div.copy()
        for col in ["mid_price", "last_trade_price", "divergence"]:
            disp2[col] = disp2[col].apply(lambda x: f"${x:.3f}")
        disp2["spread"] = disp2["spread"].apply(lambda x: f"{x:.4f}")
        disp2["volume_24hr"] = disp2["volume_24hr"].apply(lambda x: f"${x:,.0f}")
        disp2["liquidity"] = disp2["liquidity"].apply(lambda x: f"${x:,.0f}")
        st.dataframe(disp2, width="stretch", hide_index=True)
        st.download_button(
            "📥 Download as CSV",
            data=df_div.to_csv(index=False).encode("utf-8"),
            file_name=f"{venue_name()}_price_divergence.csv",
            mime="text/csv",
            key="dl_div",
        )
        fig2 = px.scatter(
            df_div, x="mid_price", y="last_trade_price",
            hover_data=["question", "divergence"],
            title="Mid quote vs. last trade price",
            labels={"mid_price": "Current mid ($)", "last_trade_price": "Last trade ($)"},
            color="divergence", color_continuous_scale="Reds",
        )
        fig2.add_shape(type="line", x0=0, y0=0, x1=1, y1=1,
                       line=dict(dash="dash", color="gray"))
        st.plotly_chart(fig2, width="stretch")

# ── Tab 3: Multi-Outcome Gaps ────────────────────────────────────────────
with tab3:
    st.subheader("Multi-outcome price gaps")
    st.caption(
        "Markets with 3+ outcomes where prices don't sum to $1.00. "
        "A sum < 1 means at least one outcome may be underpriced."
    )
    min_dev = st.slider("Min deviation from $1", 0.01, 0.20, 0.03, step=0.01, format="%.2f")
    df_multi = pm.scan_multi_outcome_gaps(df_scan, min_deviation=min_dev, min_liquidity=min_liquidity)
    if df_multi.empty:
        st.info("No multi-outcome gaps found.")
    else:
        st.warning(f"{len(df_multi)} markets with price gaps")
        disp3 = df_multi.copy()
        disp3["volume_24hr"] = disp3["volume_24hr"].apply(lambda x: f"${x:,.0f}")
        disp3["liquidity"] = disp3["liquidity"].apply(lambda x: f"${x:,.0f}")
        st.dataframe(disp3, width="stretch", hide_index=True)
        st.download_button(
            "📥 Download as CSV",
            data=df_multi.to_csv(index=False).encode("utf-8"),
            file_name=f"{venue_name()}_multi_outcome_gaps.csv",
            mime="text/csv",
            key="dl_multi",
        )

# ── Tab 4: Cross-Market Arbitrage ────────────────────────────────────────
with tab4:
    st.subheader("Cross-market arbitrage candidates")
    st.caption(
        f"{venue_display()} groups related binary markets under one event. If those "
        "sub-markets are mutually exclusive and exhaustive (exactly one wins — "
        "e.g., \"FIFA World Cup Winner\" has one market per team), the YES "
        "prices across all of them should sum to **$1.00**. When the sum "
        "differs, there's a candidate arbitrage."
    )
    st.warning(
        "⚠️ **Mutual exclusivity is not verified automatically.** This tool "
        "filters by a plausibility band but you still need to read the "
        "sub-market questions to confirm the event is genuinely exclusive "
        "before trading. Some events look exclusive but aren't (e.g., a "
        "\"2026 macro predictions\" event mixing independent questions)."
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        cm_min_markets = st.number_input(
            "Min markets per event", min_value=2, max_value=50, value=3, step=1,
            help="Skip events with fewer constituent sub-markets.",
        )
    with col2:
        cm_min_dev = st.slider(
            "Min |sum − 1|", 0.005, 0.20, 0.02, step=0.005, format="%.3f",
            help="Minimum gap from sum=1. Below this, transaction costs eat the edge.",
        )
    with col3:
        cm_band = st.slider(
            "Plausibility band (around 1.0)",
            min_value=0.05, max_value=0.50, value=0.30, step=0.05, format="%.2f",
            help=(
                "Only events whose sum falls within ±this of 1.0 are reported. "
                "Wider = more candidates but more false positives from "
                "non-exclusive events."
            ),
        )

    df_cma = pm.scan_cross_market_arb(
        df_scan,
        min_markets=int(cm_min_markets),
        min_total_liquidity=float(min_liquidity),
        plausible_band=(1.0 - cm_band, 1.0 + cm_band),
        min_deviation=float(cm_min_dev),
    )

    if df_cma.empty:
        st.info(
            "No cross-market arbitrage candidates with those filters. "
            "Try widening the plausibility band or lowering min markets."
        )
    else:
        st.success(f"{len(df_cma)} candidate events (sorted by largest deviation)")
        disp = df_cma.copy()
        disp["sum_yes_prices"] = disp["sum_yes_prices"].apply(lambda x: f"{x:.4f}")
        disp["deviation"] = disp["deviation"].apply(lambda x: f"{x:+.4f}")
        disp["implied_edge_pct"] = disp["implied_edge_pct"].apply(
            lambda x: f"{x*100:+.2f}%"
        )
        disp["total_liquidity"] = disp["total_liquidity"].apply(lambda x: f"${x:,.0f}")
        disp["total_volume_24hr"] = disp["total_volume_24hr"].apply(lambda x: f"${x:,.0f}")
        st.dataframe(
            disp[[
                "event_title", "category", "n_markets",
                "sum_yes_prices", "deviation", "implied_edge_pct",
                "total_liquidity", "total_volume_24hr", "markets_sample",
            ]],
            width="stretch",
            hide_index=True,
        )
        st.download_button(
            "📥 Download as CSV",
            data=df_cma.to_csv(index=False).encode("utf-8"),
            file_name=f"{venue_name()}_cross_market_arb.csv",
            mime="text/csv",
            key="dl_cma",
        )

        st.caption(
            "Reading the table: `sum_yes_prices < 1` means the basket is "
            "underpriced — buying one YES of every sub-market guarantees a $1 "
            "payout if exactly one resolves YES. `implied_edge_pct` is the "
            "profit fraction before transaction costs (spreads + gas). Sum > 1 "
            "implies the inverse, but these venues don't directly support "
            "shorting, so capture requires buying every NO outcome."
        )
