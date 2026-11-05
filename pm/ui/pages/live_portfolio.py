"""Live Portfolio (Kalshi) — Phase 1 read-only view of authenticated
account state. No order placement; reconciliation against
``portfolio.json`` lands in Phase 2.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from pm.ledger import portfolio as pf
from pm.venue import kalshi_auth as ka
from pm.venue import kalshi_client as kc

st.title("Live Portfolio (Kalshi)")
st.caption(
    "Authenticated read-only view of your real Kalshi account. No "
    "orders can be placed from this page — this exists to verify the "
    "API key + signing layer works and to surface your actual balance, "
    "positions, fills, and settlements."
)

if not ka.is_configured():
    # Two distinct failure modes — give the right message for each so
    # the user knows which one to fix.
    import os
    key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    pem_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
    if not key_id or not pem_path:
        st.warning(
            "Kalshi API key is not configured. Set `KALSHI_API_KEY_ID` "
            "and `KALSHI_PRIVATE_KEY_PATH` in your `.env` file, then "
            "restart the app."
        )
    elif not ka.is_owner_profile_active():
        st.warning(
            f"The Kalshi API is locked to the **{ka.owner_profile()}** "
            "profile. Switch to it in the sidebar to view your live "
            "account.\n\n"
            "This is intentional — paper-trading profiles (sandbox, "
            "agentic_*, etc.) must not be able to read or trade against "
            "the real account. Override via `KALSHI_OWNER_PROFILE` in "
            "`.env` if you really want a different profile to have "
            "access."
        )
    else:
        st.warning(
            "Kalshi API key is configured but the private key file is "
            "missing or unreadable. Check `KALSHI_PRIVATE_KEY_PATH`."
        )
    st.stop()


@st.cache_data(ttl=30, show_spinner=False)
def _balance() -> dict:
    return kc.get_balance()


@st.cache_data(ttl=30, show_spinner=False)
def _positions() -> pd.DataFrame:
    return kc.get_authed_positions(limit=200)


@st.cache_data(ttl=30, show_spinner=False)
def _fills() -> pd.DataFrame:
    return kc.get_fills(limit=200)


@st.cache_data(ttl=60, show_spinner=False)
def _settlements() -> pd.DataFrame:
    return kc.get_settlements(limit=200)


col_a, col_b = st.columns([5, 1])
with col_b:
    if st.button("🔄 Refresh"):
        st.cache_data.clear()
        st.rerun()

# ──────────────────────────────────────────────────────────────────────────
# Balance
# ──────────────────────────────────────────────────────────────────────────
try:
    bal = _balance()
except ka.KalshiAuthError as e:
    st.error(f"Auth error: {e}")
    st.stop()
except Exception as e:
    st.error(f"Couldn't reach Kalshi: {e}")
    st.stop()

m1, m2, m3 = st.columns(3)
m1.metric("Settled balance", f"${bal['balance_cents'] / 100:,.2f}",
          help="Cash available for new orders / withdrawal.")
m2.metric("Pending payout", f"${bal['payout_cents'] / 100:,.2f}",
          help="Winnings from settled markets not yet credited to balance.")
m3.metric("Total", f"${(bal['balance_cents'] + bal['payout_cents']) / 100:,.2f}",
          help="Settled + pending. Does NOT include unrealized PnL on "
               "open positions — see Positions below for that.")

st.divider()

# ──────────────────────────────────────────────────────────────────────────
# Positions
# ──────────────────────────────────────────────────────────────────────────
st.subheader("Positions")
df_pos = _positions()
if df_pos.empty:
    st.info("No open positions.")
else:
    # Estimate cash-out for each position. One get_market call per row;
    # cached for 30s alongside the position fetch so refresh-spam doesn't
    # hammer Kalshi.
    @st.cache_data(ttl=30, show_spinner=False)
    def _enrich_with_cashout(df_in: pd.DataFrame) -> pd.DataFrame:
        out = df_in.copy()
        cashouts: list[dict] = []
        for _, r in out.iterrows():
            try:
                m = kc.get_market(str(r["ticker"]))
            except Exception:
                m = {}
            yes_bid = float(m.get("yes_bid_dollars") or 0)
            yes_ask = float(m.get("yes_ask_dollars") or 0)
            last = float(m.get("last_price_dollars") or 0)
            cashouts.append(kc.estimate_cashout(
                position=int(r["position"]),
                yes_bid=yes_bid or None,
                yes_ask=yes_ask or None,
                last_trade=last or None,
                cost_basis_cents=int(r["total_traded_cents"]),
            ))
        out["cashout_cents"] = [c["cashout_cents"] for c in cashouts]
        out["cashout_pnl_cents"] = [c["pnl_cents"] for c in cashouts]
        out["cashout_pnl_pct"] = [c["pnl_pct"] for c in cashouts]
        out["cashout_status"] = [c["status"] for c in cashouts]
        return out

    df = _enrich_with_cashout(df_pos)
    df["market_exposure"] = df["market_exposure_cents"] / 100
    df["realized_pnl"] = df["realized_pnl_cents"] / 100
    df["total_traded"] = df["total_traded_cents"] / 100
    df["cashout"] = df["cashout_cents"] / 100
    df["cashout_pnl"] = df["cashout_pnl_cents"] / 100
    # Add a small icon for closed markets so users know the cash-out
    # is an implied settlement value, not a fresh bid.
    df["status_icon"] = df["cashout_status"].map(
        {"ok": "", "closed": "🔒", "unknown": "⚠️"}
    )

    # Baseball-sidecar model column for MLB live positions. Ticker on
    # the Kalshi authed positions endpoint IS the condition_id. The
    # YES side held depends on which sub-market this position is on —
    # predict_for_ticker already handles that via the ticker suffix.
    try:
        import baseball as _baseball
        def _model_for_ticker(t: str) -> float | None:
            r = _baseball.predict_for_ticker(str(t))
            return r["yes_prob"] if r else None
        df["model_yes_pct"] = df["ticker"].apply(_model_for_ticker) * 100
    except Exception:  # noqa: BLE001
        df["model_yes_pct"] = None

    display = df[[
        "ticker", "position", "total_traded", "cashout", "model_yes_pct",
        "cashout_pnl", "cashout_pnl_pct", "status_icon",
        "realized_pnl", "resting_orders_count",
    ]].rename(columns={
        "total_traded": "Cost ($)",
        "cashout": "Cash-out ($)",
        "model_yes_pct": "Model P(YES) %",
        "cashout_pnl": "Unrealized PnL ($)",
        "cashout_pnl_pct": "PnL %",
        "status_icon": "",
        "realized_pnl": "Realized PnL ($)",
        "resting_orders_count": "Resting",
    })
    st.dataframe(
        display.style.format({
            "Cost ($)": "${:,.2f}",
            "Cash-out ($)": "${:,.2f}",
            "Model P(YES) %": "{:.1f}%",
            "Unrealized PnL ($)": "${:+,.2f}",
            "PnL %": "{:+.1f}%",
            "Realized PnL ($)": "${:,.2f}",
        }, na_rep="—"),
        width="stretch", hide_index=True,
    )
    st.caption(
        "**Cash-out** = what you'd receive selling at the current bid. "
        "🔒 = market closed, value shown is implied settlement (last "
        "trade). ⚠️ = no bid + no last trade; cash-out unknown."
    )

    n1, n2, n3, n4 = st.columns(4)
    n1.metric("Open positions", len(df_pos))
    n2.metric("Total cost basis", f"${df['total_traded'].sum():,.2f}")
    n3.metric("Total cash-out value", f"${df['cashout'].sum():,.2f}")
    total_pnl = df["cashout_pnl"].sum()
    n4.metric(
        "Unrealized PnL",
        f"${total_pnl:+,.2f}",
        delta=f"{total_pnl / df['total_traded'].sum() * 100:+.1f}%"
              if df['total_traded'].sum() > 0 else None,
    )

    # ──────────────────────────────────────────────────────────────────
    # Take-profit setters (one expander per position) — only show when
    # live trading is enabled. Lets the user set a sell-limit above
    # the current market that fires automatically if the market spikes.
    # ──────────────────────────────────────────────────────────────────
    from pm.venue import kalshi_orders as ko
    if ko.is_live_enabled():
        st.divider()
        st.subheader("🎯 Take-profit setters")
        st.caption(
            "Set a resting sell-limit on each position. Fires "
            "automatically if the market reaches your target. Useful "
            "for set-and-forget profit-taking on positions you can't "
            "watch 24/7. Cancel from the 📑 Orders page."
        )
        for _, r in df.iterrows():
            ticker = str(r["ticker"])
            pos = int(r["position"])
            held_side = "yes" if pos > 0 else "no"
            abs_pos = abs(pos)
            current_m2m_cents = int(r["cashout_cents"] / abs_pos) if abs_pos > 0 else 0
            # Current bid for the side held (for the buffer check)
            m = kc.get_market(ticker)
            if held_side == "yes":
                current_bid_cents = int(round(
                    float(m.get("yes_bid_dollars") or 0) * 100
                ))
            else:
                # NO bid = 1 - YES ask
                current_bid_cents = int(round(
                    (1.0 - float(m.get("yes_ask_dollars") or 0)) * 100
                ))

            with st.expander(
                f"{ticker} — {abs_pos} {held_side.upper()}  "
                f"(current ≈ {current_m2m_cents}¢)"
            ):
                c1, c2, c3 = st.columns([1, 1, 2])
                with c1:
                    tp_price = st.number_input(
                        "Sell-limit (¢)",
                        min_value=max(1, current_bid_cents + 1),
                        max_value=99,
                        value=min(99, max(
                            current_bid_cents + 5,
                            current_m2m_cents + 3,
                        )),
                        step=1, key=f"tp_price_{ticker}",
                        help=f"Must be above current bid ({current_bid_cents}¢). "
                             f"Default suggests current + 5¢.",
                    )
                with c2:
                    tp_count = st.number_input(
                        "Contracts",
                        min_value=1, max_value=abs_pos,
                        value=abs_pos, step=1,
                        key=f"tp_count_{ticker}",
                        help=f"Up to {abs_pos} (your full position).",
                    )
                proceeds = tp_count * tp_price / 100
                cost_share = (r["total_traded"] * tp_count / abs_pos
                              if abs_pos > 0 else 0)
                tp_pnl = proceeds - cost_share
                with c3:
                    st.markdown(
                        f"**If filled:** receive **${proceeds:,.2f}** → "
                        f"PnL **${tp_pnl:+,.2f}** on the {tp_count} sold"
                    )

                ack = st.checkbox(
                    "Place this resting sell-limit (Kalshi will reserve "
                    "the contracts until filled or cancelled).",
                    key=f"tp_ack_{ticker}",
                )
                if st.button(
                    f"🎯 SET take-profit — {held_side.upper()} {int(tp_count)} "
                    f"@ {int(tp_price)}¢",
                    key=f"tp_submit_{ticker}",
                    disabled=not ack,
                    type="primary",
                ):
                    try:
                        resp = ko.place_take_profit(
                            ticker=ticker, held_side=held_side,
                            count=int(tp_count),
                            sell_price_cents=int(tp_price),
                            current_bid_cents=current_bid_cents,
                        )
                        st.success(f"✅ Take-profit placed: {resp}")
                        st.cache_data.clear()
                    except ko.OrderRejected as e:
                        st.error(f"Rejected: {e}")
                    except ko.OrderOutcomeUnknown as e:
                        st.warning(
                            f"⚠️ **Outcome UNKNOWN** — Kalshi did not answer; "
                            f"the sell may still have been accepted. Check "
                            f"the Orders page for client order id "
                            f"`{e.client_order_id}` before retrying.\n\n{e}")
                    except Exception as e:
                        body = ""
                        if hasattr(e, "response") and e.response is not None:
                            try:
                                body = e.response.text[:300]
                            except Exception:
                                pass
                        st.error(f"Kalshi rejected: {e}\n\n`{body}`" if body
                                 else f"Kalshi rejected: {e}")

        # ──────────────────────────────────────────────────────────
        # Cash-out (immediate sell at bid). Symmetric counterpart to
        # take-profit: "I want OUT now, take whatever the market
        # gives me." Different intent, different button.
        # ──────────────────────────────────────────────────────────
        st.divider()
        st.subheader("💸 Cash-out (immediate sell at bid)")
        st.caption(
            "Sell at the current bid right now. Crosses the spread → "
            "you give up the bid-ask gap. Useful when (a) you want "
            "freed bankroll for another bet, (b) the market could "
            "still flip against you, or (c) the thesis is broken and "
            "you want out. For \"sell later if it spikes\" use the "
            "take-profit setter above."
        )
        for _, r in df.iterrows():
            ticker = str(r["ticker"])
            pos = int(r["position"])
            held_side = "yes" if pos > 0 else "no"
            abs_pos = abs(pos)
            m = kc.get_market(ticker)
            if held_side == "yes":
                current_bid_cents = int(round(
                    float(m.get("yes_bid_dollars") or 0) * 100
                ))
            else:
                current_bid_cents = int(round(
                    (1.0 - float(m.get("yes_ask_dollars") or 0)) * 100
                ))
            cost = r["total_traded"]
            cashout_proceeds = abs_pos * current_bid_cents / 100
            cashout_pnl = cashout_proceeds - cost

            with st.expander(
                f"{ticker} — {abs_pos} {held_side.upper()}  "
                f"(cash out at {current_bid_cents}¢ → ${cashout_proceeds:,.2f})"
            ):
                pnl_sign = "+" if cashout_pnl >= 0 else "-"
                st.markdown(
                    f"**Sell all {abs_pos} contracts at the current bid "
                    f"of {current_bid_cents}¢**\n\n"
                    f"- Receive: **${cashout_proceeds:,.2f}**\n"
                    f"- Cost basis: ${cost:,.2f}\n"
                    f"- Realized PnL: **{pnl_sign}${abs(cashout_pnl):,.2f}**"
                )
                if current_bid_cents <= 0:
                    st.warning(
                        "Current bid is zero — there's no liquidity to "
                        "cash out into right now. Wait, set a "
                        "take-profit limit, or let the market resolve."
                    )
                else:
                    co_count = st.number_input(
                        "Contracts to sell",
                        min_value=1, max_value=abs_pos,
                        value=abs_pos, step=1,
                        key=f"cashout_count_{ticker}",
                    )
                    co_ack = st.checkbox(
                        "I understand this is an immediate sell at the "
                        "current bid (giving up the spread).",
                        key=f"cashout_ack_{ticker}",
                    )
                    if st.button(
                        f"💸 CASH OUT — {held_side.upper()} {int(co_count)} "
                        f"@ {current_bid_cents}¢",
                        key=f"cashout_submit_{ticker}",
                        disabled=not co_ack,
                        type="primary",
                    ):
                        try:
                            resp = ko.cash_out_position(
                                ticker=ticker, held_side=held_side,
                                count=int(co_count),
                                current_bid_cents=current_bid_cents,
                            )
                            st.success(f"✅ Cash-out placed: {resp}")
                            st.cache_data.clear()
                        except ko.OrderRejected as e:
                            st.error(f"Rejected: {e}")
                        except ko.OrderOutcomeUnknown as e:
                            st.warning(
                                f"⚠️ **Outcome UNKNOWN** — Kalshi did not "
                                f"answer; the sell may still have been "
                                f"accepted. Check the Orders page for client "
                                f"order id `{e.client_order_id}` before "
                                f"retrying.\n\n{e}")
                        except Exception as e:
                            body = ""
                            if hasattr(e, "response") and e.response is not None:
                                try:
                                    body = e.response.text[:300]
                                except Exception:
                                    pass
                            st.error(
                                f"Kalshi rejected: {e}\n\n`{body}`" if body
                                else f"Kalshi rejected: {e}"
                            )

st.divider()

# ──────────────────────────────────────────────────────────────────────────
# Reconciliation — paper portfolio vs real Kalshi state
# ──────────────────────────────────────────────────────────────────────────
st.subheader("Reconciliation")
st.caption(
    "Side-by-side of your paper portfolio (the active profile's "
    "`portfolio.json`) versus your real Kalshi positions, netted per "
    "ticker. Surfaces drift between what the log thinks you hold and "
    "what the venue says you actually hold."
)

df_paper = pf.get_positions()
df_recon = pf.reconcile_positions(df_paper, df_pos)

if df_recon.empty:
    st.info(
        "Nothing to reconcile — both your paper portfolio and your "
        "Kalshi account are empty."
    )
else:
    # Headline counts
    n_total = len(df_recon)
    n_matched = (df_recon["status"] == "matched").sum()
    n_paper_only = (df_recon["status"] == "paper_only").sum()
    n_real_only = (df_recon["status"] == "real_only").sum()
    n_mismatch = (df_recon["status"] == "mismatch").sum()

    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Matched", int(n_matched), help="Paper and Kalshi agree.")
    r2.metric("Paper only", int(n_paper_only),
              help="Logged but not held on Kalshi. Likely a paper-trading "
                   "row, a closed position not yet logged as a sell, or "
                   "manual entry error.")
    r3.metric("Real only", int(n_real_only),
              help="Held on Kalshi but missing from the paper log. Real "
                   "trades placed outside this app — log a buy on the "
                   "Portfolio page to bring them in.")
    r4.metric("Mismatch", int(n_mismatch),
              help="Both sides have a position, but the net counts "
                   "differ. The 'drift' column shows the magnitude.")

    # Filterable view
    statuses = st.multiselect(
        "Show statuses",
        options=["matched", "paper_only", "real_only", "mismatch"],
        default=["paper_only", "real_only", "mismatch"]
            if n_mismatch + n_paper_only + n_real_only > 0
            else ["matched"],
        help="Default hides matched rows so drift stands out.",
    )
    filtered = df_recon[df_recon["status"].isin(statuses)]
    if filtered.empty:
        st.caption("No rows for the selected statuses.")
    else:
        st.dataframe(
            filtered[[
                "ticker", "question", "status",
                "paper_net", "real_net", "drift",
                "paper_yes", "paper_no",
            ]].style.format({
                "paper_net": "{:+,.2f}",
                "real_net": "{:+,d}",
                "drift": "{:+,.2f}",
                "paper_yes": "{:,.2f}",
                "paper_no": "{:,.2f}",
            }),
            width="stretch", hide_index=True,
        )

st.divider()

# ──────────────────────────────────────────────────────────────────────────
# Fills
# ──────────────────────────────────────────────────────────────────────────
st.subheader("Recent fills")
df_fills = _fills()
if df_fills.empty:
    st.info("No fills yet.")
else:
    df = df_fills.copy()
    df["yes_price"] = df["yes_price_cents"] / 100
    df["no_price"] = df["no_price_cents"] / 100
    display = df[[
        "created_time", "ticker", "side", "action", "count",
        "yes_price", "no_price", "is_taker",
    ]].rename(columns={
        "yes_price": "YES px",
        "no_price": "NO px",
        "is_taker": "Taker",
    })
    st.dataframe(
        display.style.format({"YES px": "${:.2f}", "NO px": "${:.2f}"}),
        width="stretch", hide_index=True,
    )

st.divider()

# ──────────────────────────────────────────────────────────────────────────
# Settlements
# ──────────────────────────────────────────────────────────────────────────
st.subheader("Settlements")
df_settle = _settlements()
if df_settle.empty:
    st.info("No settlements yet.")
else:
    df = df_settle.copy()
    df["revenue"] = df["revenue_cents"] / 100
    display = df[[
        "settled_time", "ticker", "market_result", "yes_count",
        "no_count", "revenue",
    ]].rename(columns={
        "market_result": "Result",
        "yes_count": "YES held",
        "no_count": "NO held",
        "revenue": "Revenue ($)",
    })
    st.dataframe(
        display.style.format({"Revenue ($)": "${:,.2f}"}),
        width="stretch", hide_index=True,
    )
    st.metric("Lifetime settlement revenue",
              f"${df['revenue'].sum():,.2f}")
