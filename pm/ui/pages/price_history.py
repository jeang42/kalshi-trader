"""Price History page — price charts + order book depth viewer."""

import plotly.graph_objects as go
import streamlit as st

from pm.ui.shared import LIVE_TICK_SECONDS, live_price_panel, load_markets
from pm.venue import (
    polymarket_client as pm,  # for venue-agnostic pure helpers: parse_order_book, order_book_stats
)
from pm.venue import provider as mp

st.title("Price History")

with st.spinner("Loading markets..."):
    df_markets = load_markets()

if df_markets.empty:
    st.warning("Could not load markets.")
    st.stop()

col1, col2 = st.columns([3, 1])
with col1:
    options = df_markets["question"].tolist()
    selected_q = st.selectbox("Select market", options)
with col2:
    fidelity = st.selectbox(
        "Candle size", [1, 5, 15, 60, 1440], index=3,
        format_func=lambda x: {1: "1m", 5: "5m", 15: "15m", 60: "1h", 1440: "1d"}[x],
    )

row = df_markets[df_markets["question"] == selected_q].iloc[0]
cid = row["condition_id"]
outcomes = row.get("outcomes") or []
prices_now = row.get("outcome_prices") or []

# Build the token list early so the live ticker has it before the historical fetch.
provider = mp.get_provider()
tokens = provider.list_outcome_tokens(cid)

# Cached mid-quote summary (from the 60s-cached load_markets data)
if outcomes and prices_now and len(outcomes) == len(prices_now):
    cols = st.columns(len(outcomes))
    for i, (o, p) in enumerate(zip(outcomes, prices_now, strict=False)):
        if p is None:
            cols[i].metric(f"{o} price", "—")
        else:
            cols[i].metric(f"{o} price", f"${float(p):.3f}", help="From the 60s-cached markets snapshot")

# Live ticker — re-fetches midpoints every few seconds without touching the rest of the page
if tokens:
    st.caption(f"🟢 Live (refreshes every {LIVE_TICK_SECONDS}s)")
    live_price_panel(tokens)

days = st.slider("Days of history", 1, 90, 30)

# Historical price chart
fig = go.Figure()
with st.spinner("Fetching price history..."):
    for token in tokens:
        df_hist = provider.get_price_history(token["token_id"], days=days, fidelity=fidelity)
        if not df_hist.empty:
            fig.add_trace(go.Scatter(
                x=df_hist["timestamp"],
                y=df_hist["price"],
                mode="lines",
                name=token["outcome"],
            ))

# Baseball-sidecar overlay: for MLB markets, plot every stored prediction
# for the matched game as a horizontal "model says X" line + scatter of
# computed_at timestamps. Visualizes when the market diverged from the
# model and how the model itself moved as starters/lineups got announced.
_bb_overlay_added = False
try:
    import baseball as _baseball
    from baseball import state as _bstate
    _bb = _baseball.predict_for_ticker(str(cid))
    if _bb is not None:
        # Find the game_pk and pull every prediction
        for _g in _bstate.get_games_for_date(_bb["game_date"]):
            if (str(_g.get("home_abbr","")).upper() == _bb["home"]
                and str(_g.get("away_abbr","")).upper() == _bb["away"]):
                _preds = _bstate.get_predictions_for_game(_g["game_pk"])
                if _preds:
                    # YES-side perspective — flip the home_win_prob if YES is away
                    _xs, _ys = [], []
                    for _p in reversed(_preds):  # chronological
                        _xs.append(_p["computed_at"])
                        _hwp = float(_p["home_win_prob"])
                        _ys.append(_hwp if _bb["yes_side"] == "home" else 1.0 - _hwp)
                    fig.add_trace(go.Scatter(
                        x=_xs, y=_ys, mode="lines+markers",
                        name=f"⚾ baseball model (YES={_bb['yes_team']})",
                        line={"dash": "dash", "width": 2, "color": "#b87333"},
                        marker={"size": 6, "symbol": "diamond"},
                    ))
                    _bb_overlay_added = True
                break
except Exception:  # noqa: BLE001
    pass

if not tokens:
    st.warning("No outcome tokens found for this market — it may not be order-book enabled.")
elif not fig.data:
    st.info("No price history available for this market yet.")
else:
    fig.update_layout(
        title=f"Price History: {selected_q[:80]}",
        xaxis_title="Date",
        yaxis_title="Price ($)",
        yaxis={"range": [0, 1]},
        hovermode="x unified",
        height=450,
    )
    st.plotly_chart(fig, width="stretch")
    if _bb_overlay_added:
        st.caption("📊 Diamond-marker line is the baseball sidecar's model prediction "
                    "(line moves as starters/lineups get announced).")

# ── Order Book Depth ─────────────────────────────────────────────────────
if tokens:
    with st.expander("📚 Order Book Depth", expanded=False):
        st.caption(
            "How much you can actually trade at quoted prices. The Kelly Sizer "
            "tells you how big to bet — but if the book is thin, you'll move the "
            "price through multiple levels (slippage). Check this before sizing "
            "any meaningful position."
        )

        book_cols = st.columns(len(tokens))
        for i, token in enumerate(tokens):
            with book_cols[i]:
                st.markdown(f"**{token['outcome']}**")
                try:
                    raw = provider.get_order_book(token["token_id"])
                except Exception as e:
                    st.caption(f"Order book unavailable ({type(e).__name__})")
                    continue

                df_book = pm.parse_order_book(raw)
                stats = pm.order_book_stats(df_book)

                if df_book.empty:
                    st.caption("Order book is empty.")
                    continue

                m1, m2, m3 = st.columns(3)
                m1.metric("Best bid",
                          f"${stats['best_bid']:.4f}" if stats["best_bid"] is not None else "—")
                m2.metric("Best ask",
                          f"${stats['best_ask']:.4f}" if stats["best_ask"] is not None else "—")
                m3.metric("Spread",
                          f"${stats['spread']:.4f}" if stats["spread"] is not None else "—")

                st.caption(
                    f"Total bid liquidity: **{stats['total_bid_size']:,.0f} shares** "
                    f"across {stats['bid_levels']} levels — "
                    f"ask: **{stats['total_ask_size']:,.0f}** across "
                    f"{stats['ask_levels']} levels"
                )

                # Depth chart: cumulative size vs. price
                bids = df_book[df_book["side"] == "bid"].sort_values("price")
                asks = df_book[df_book["side"] == "ask"].sort_values("price")
                bids = bids.assign(cum_from_best=bids["size"].iloc[::-1].cumsum().iloc[::-1])
                asks = asks.assign(cum_from_best=asks["size"].cumsum())

                depth = go.Figure()
                if not bids.empty:
                    depth.add_trace(go.Scatter(
                        x=bids["price"], y=bids["cum_from_best"],
                        mode="lines", line=dict(shape="hv", color="green", width=2),
                        fill="tozeroy", fillcolor="rgba(0,200,0,0.15)",
                        name="Bids",
                        hovertemplate="Bid $%{x:.4f}<br>Cum size %{y:,.0f}<extra></extra>",
                    ))
                if not asks.empty:
                    depth.add_trace(go.Scatter(
                        x=asks["price"], y=asks["cum_from_best"],
                        mode="lines", line=dict(shape="hv", color="red", width=2),
                        fill="tozeroy", fillcolor="rgba(200,0,0,0.15)",
                        name="Asks",
                        hovertemplate="Ask $%{x:.4f}<br>Cum size %{y:,.0f}<extra></extra>",
                    ))
                depth.update_layout(
                    xaxis=dict(title="Price ($)", range=[0, 1]),
                    yaxis=dict(title="Cumulative size"),
                    height=260, showlegend=False,
                    margin=dict(l=40, r=20, t=10, b=40),
                )
                st.plotly_chart(depth, width="stretch",
                                key=f"depth_{token['token_id']}")

                with st.expander(f"Top levels ({token['outcome']})"):
                    disp = df_book.copy()
                    disp["price"] = disp["price"].apply(lambda x: f"${x:.4f}")
                    disp["size"] = disp["size"].apply(lambda x: f"{x:,.0f}")
                    disp["cumulative_size"] = disp["cumulative_size"].apply(lambda x: f"{x:,.0f}")
                    st.dataframe(disp, width="stretch", hide_index=True)

# Raw history data toggle
with st.expander("Show raw price history data"):
    for token in tokens:
        df_hist = provider.get_price_history(token["token_id"], days=days, fidelity=fidelity)
        if not df_hist.empty:
            st.write(f"**{token['outcome']}**")
            st.dataframe(df_hist, width="stretch", hide_index=True)
