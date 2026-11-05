"""Kelly Sizer page — optimal bet sizing + save-forecast for calibration."""

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from pm.ledger import calibration as cal
from pm.ledger import portfolio as pf
from pm.ledger import risk
from pm.ui.shared import KELLY_P_MAX, KELLY_P_MIN, load_markets, paper_trading_banner
from pm.venue import kalshi_auth as ka
from pm.venue import kalshi_orders as ko
from pm.venue import polymarket_client as pm
from pm.venue import provider as mp

st.title("Kelly Criterion Position Sizer")
# show_help=False to avoid duplicating the paper-trading explainer that
# already lives on the Portfolio page (the primary action surface)
paper_trading_banner(show_help=False)

st.info(
    "**What is the Kelly Criterion?**\n\n"
    "The Kelly Criterion is a formula that tells you what fraction of your bankroll "
    "to bet in order to maximize your long-run wealth growth — without risking ruin.\n\n"
    "It works by comparing **your probability estimate** for an outcome against "
    "**the market's implied probability** (the current price). When you think something "
    "is more likely than the market does, there is an *edge*. Kelly tells you exactly "
    "how much to bet on that edge.\n\n"
    "**The formula for a prediction market:**\n\n"
    "To bet YES at market price *m* when your true probability is *p*:\n\n"
    "```\nKelly fraction = (p − m) / (1 − m)\n```\n\n"
    "To bet NO (i.e. you think *p < m*):\n\n"
    "```\nKelly fraction = (m − p) / m\n```\n\n"
    "The result is the **fraction of your total bankroll** to put into this position. "
    "A Kelly fraction of 0.10 means bet 10% of your bankroll.\n\n"
    "**Half-Kelly** (betting half the recommended amount) is the most common real-world "
    "approach — it cuts variance roughly in half while giving up only a small amount of "
    "long-run growth. Most professional bettors use ¼ Kelly to ½ Kelly."
)

st.divider()

with st.spinner("Loading markets..."):
    df_kelly = load_markets()

# Look-up-by-ticker — pull a specific market that isn't in the
# volume-ordered default fetch (low-volume / long-dated / niche events).
# Accepts a Kalshi URL or a ticker (event or market).
with st.expander("🔎 Look up a specific market by ticker or URL"):
    st.caption(
        "Paste a Kalshi URL (e.g. `kalshi.com/markets/.../kxky4r-26`) "
        "or a ticker (`KXKY4R-26` for the event, `KXKY4R-26-TMAS` for one "
        "candidate). Brings it into the picker below even if it's "
        "outside the default fetch."
    )
    lookup_input = st.text_input(
        "Ticker or URL", value="",
        placeholder="https://kalshi.com/markets/... or KXKY4R-26",
        key="kelly_lookup_input",
    )
    if lookup_input.strip():
        try:
            from pm.venue import provider as _mp
            _p = _mp.get_provider()
            if hasattr(_p, "lookup_markets_by_id"):
                df_lookup = _p.lookup_markets_by_id(lookup_input.strip())
            else:
                # Polymarket fallback path (no auth, no lookup yet)
                from pm.venue import kalshi_client as _kc
                df_lookup = _kc.lookup_markets_by_id(lookup_input.strip())
        except Exception as e:
            st.error(f"Lookup failed: {e}")
            df_lookup = pd.DataFrame()
        if df_lookup is None or df_lookup.empty:
            st.warning(f"No markets found for `{lookup_input}`.")
        else:
            st.success(
                f"Found {len(df_lookup)} market(s). They'll appear at the "
                "top of the picker below."
            )
            # Drop dupes if the looked-up market is already in the
            # default fetch (e.g. a high-volume market also queried)
            existing = set(df_kelly["condition_id"].astype(str))
            df_lookup = df_lookup[
                ~df_lookup["condition_id"].astype(str).isin(existing)
            ]
            df_kelly = pd.concat([df_lookup, df_kelly], ignore_index=True)

if df_kelly.empty:
    st.warning("Could not load markets.")
    st.stop()

settings = risk.load_settings()

# ── Drawdown circuit breaker ─────────────────────────────────────────────
# Pulled in once at the top of the page so it's hard to miss.
_snaps = pf.get_snapshots()
_dd = risk.current_drawdown(_snaps)
if risk.should_pause_for_drawdown(_dd, settings.drawdown_pause_pct):
    st.error(
        f"🛑 **Drawdown circuit breaker** — you're currently down "
        f"**{_dd['pct']*100:+.1f}%** from your peak (${_dd['peak']:,.2f} → ${_dd['current']:,.2f}). "
        f"Threshold is {settings.drawdown_pause_pct*100:.0f}%. "
        "Consider pausing new positions and reviewing what happened before sizing more bets."
    )

# Per-category Brier scores — used to adjust Kelly recommendations below.
brier_by_cat = cal.brier_score_by_category(cal.get_forecasts(), df_kelly)

col1, col2 = st.columns([3, 1])
with col1:
    selected_q = st.selectbox("Select a market", df_kelly["question"].tolist())
with col2:
    bankroll = st.number_input(
        "Your bankroll ($)", min_value=1.0,
        value=float(settings.bankroll), step=100.0,
        help="Defaults from Settings. Change for this session only, "
             "or open the Settings page to make it stick.",
    )

row = df_kelly[df_kelly["question"] == selected_q].iloc[0]
outcomes = row.get("outcomes") or []
prices = [float(p) for p in (row.get("outcome_prices") or []) if p is not None]

if not prices or not outcomes or len(prices) != len(outcomes):
    st.warning("Could not read prices for this market.")
    st.stop()

st.subheader("Current market prices")
pcols = st.columns(len(prices))
for i, (o, p) in enumerate(zip(outcomes, prices, strict=False)):
    pcols[i].metric(f"{o}", f"{p:.1%}", help="Market's implied probability")

st.divider()
st.subheader("Your probability estimate")

# Binary YES/NO markets: only render ONE slider (the YES one). The NO
# slider was historically rendered too, but on a binary it carries
# identical information mirrored (Yes=0.30 ≡ No=0.70) and confused
# users into thinking they had to set both. For true multi-outcome
# markets (Polymarket's N-candidate markets) we keep the per-outcome
# loop intact.
_outcome_strs = [str(o).strip().lower() for o in outcomes]
_is_binary = len(outcomes) == 2 and set(_outcome_strs) == {"yes", "no"}

if _is_binary:
    st.caption(
        "Enter what YOU think the chance is that this market resolves YES. "
        "Below the market price → Kelly will recommend NO (bet against). "
        "Above the market price → Kelly will recommend YES (bet for)."
    )
else:
    st.caption(
        "Enter what YOU think the true probability is for each outcome. "
        "If it differs from the market price above, Kelly will size a "
        "bet on that disagreement."
    )

results = []
iter_outcomes = list(zip(outcomes, prices, strict=False))
if _is_binary:
    # Find the Yes index — Kalshi typically yields ["Yes", "No"] in that
    # order but don't hard-code it.
    yes_i = next(i for i, o in enumerate(_outcome_strs) if o == "yes")
    iter_outcomes = [iter_outcomes[yes_i]]

# Baseball-sidecar prefill: for MLB markets, the sidecar's model has a
# real opinion. Use it as the default slider value instead of the market
# midpoint (which would always say "no edge"). Doesn't override an
# already-set slider value — Streamlit will keep the user's input as
# long as the key is stable.
#
# Reads the ticker off `row` (set at the top of the page) rather than
# `selected_market_cid`, which is not assigned until ~40 lines BELOW this
# block. Because a Streamlit page script executes top-to-bottom, the old
# reference raised NameError on every single render — and the broad
# `except Exception: pass` below swallowed it, so this feature was silently
# dead for as long as it had existed while the docs described it as working.
_baseball_yes_prob: float | None = None
_baseball_note: str | None = None
try:
    import baseball as _baseball
    _bb = _baseball.predict_for_ticker(str(row["condition_id"]))
    if _bb is not None:
        _baseball_yes_prob = float(_bb["yes_prob"])
        _baseball_note = (
            f"⚾ Baseball model: **{_bb['yes_team']}** wins "
            f"{_baseball_yes_prob*100:.1f}% "
            f"(home={_bb['home']} vs away={_bb['away']}, "
            f"{_bb['game_date']}, {_bb['source']})"
        )
except Exception:  # noqa: BLE001 — sidecar is optional
    pass

for outcome, mkt_price in iter_outcomes:
    if _is_binary and _baseball_yes_prob is not None:
        prefill = max(KELLY_P_MIN, min(KELLY_P_MAX, round(_baseball_yes_prob, 4)))
    else:
        prefill = max(KELLY_P_MIN, min(KELLY_P_MAX, round(float(mkt_price), 4)))
    label = (
        f"Your probability for **{outcome}**"
        if not _is_binary
        else "Your probability that this resolves **YES**"
    )
    if _baseball_note and _is_binary:
        st.info(_baseball_note, icon="📊")
        _baseball_note = None  # only show once per page render
    your_p = st.slider(
        label,
        min_value=KELLY_P_MIN, max_value=KELLY_P_MAX,
        value=prefill,
        step=0.001, format="%.4f",
        key=f"kelly_slider_{outcome}",
    )
    sizing = pm.kelly_sizing(market_price=mkt_price, your_probability=your_p)
    results.append((outcome, mkt_price, your_p, sizing))

st.divider()
st.subheader("Recommended position sizes")

selected_market_question = selected_q
selected_market_cid = row["condition_id"]
selected_market_category = str(row.get("category", "") or "")

# Category Brier (or None if no/insufficient data). Drives the
# calibration-adjusted Kelly multiplier shown alongside raw Kelly below.
category_brier = brier_by_cat.get(selected_market_category)
adj_multiplier, adj_explanation = risk.calibration_adjusted_kelly_multiplier(category_brier)

# Show the calibration context once for the whole market
if selected_market_category:
    brier_str = f"{category_brier:.3f}" if category_brier is not None else "no data yet"
    st.info(
        f"**Risk-adjusted Kelly for {selected_market_category}:** {adj_multiplier:.0%} multiplier. "
        f"Your Brier in this category: {brier_str}.\n\n{adj_explanation}"
    )

for outcome, mkt_price, your_p, s in results:
    edge = s["edge"]
    st.markdown(f"### {outcome}")

    if abs(edge) < 0.001:
        st.write("No edge (your estimate matches the market).")
    else:
        direction = s["direction"]
        kelly_frac = s["kelly_fraction"]
        # Apply both the global cap and the category-based calibration multiplier
        adj_frac = min(kelly_frac * adj_multiplier, settings.max_kelly_fraction)
        kelly_dollars = bankroll * kelly_frac
        adj_dollars = bankroll * adj_frac
        ev = s["expected_value"]

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Edge", f"{edge:+.1%}", help="Your probability minus market price")
        m2.metric("Expected value per $1", f"${ev:+.4f}",
                  help="Average profit per $1 bet at full Kelly")
        m3.metric(
            "Raw Kelly", f"{kelly_frac:.1%}  (${kelly_dollars:,.0f})",
            help="Mathematical optimum assuming your probability estimate is exactly right.",
        )
        m4.metric(
            "Risk-adjusted (recommended)", f"{adj_frac:.1%}  (${adj_dollars:,.0f})",
            help=(
                f"Raw Kelly × calibration multiplier ({adj_multiplier:.0%}), "
                f"capped at {settings.max_kelly_fraction:.0%} of bankroll per the global limit."
            ),
        )

        # Per-market position-size limit check
        max_market_dollars = settings.max_market_position_pct * bankroll
        if adj_dollars > max_market_dollars:
            st.warning(
                f"⚠️ Position-size limit: recommendation of "
                f"${adj_dollars:,.0f} exceeds the per-market cap "
                f"(${max_market_dollars:,.0f} = {settings.max_market_position_pct:.0%} "
                f"of bankroll). Consider sizing down."
            )

        if adj_multiplier == 0:
            st.error(f"Don't bet on **{direction}** — calibration insufficient. See note above.")
        elif edge > 0:
            st.success(f"Bet **{direction}** — market underestimates this outcome")
        else:
            st.warning(f"Bet **{direction}** — market overestimates this outcome")

    if st.button(
        f"💾 Save forecast for **{outcome}**",
        key=f"save_forecast_{outcome}",
        help="Record your probability estimate. Mark it resolved later on the Calibration page.",
    ):
        cal.add_forecast(
            selected_market_question, selected_market_cid,
            outcome, mkt_price, your_p,
        )
        st.toast(f"Saved forecast: {outcome} @ {your_p:.1%}", icon="✅")

    # ─── Phase-4 live-order placement ──────────────────────────────
    # Only shows on Kalshi, only on the owner profile, only when
    # KALSHI_LIVE_ENABLED=true. Two-click confirmation; hard gates
    # block submission rather than just warning. Outside those guards
    # the expander never renders so the Kelly Sizer stays exactly as
    # it was for paper / non-owner profiles.
    if (mp.active_provider_name() == "kalshi"
            and ka.is_configured()
            and ko.is_live_enabled()
            and abs(edge) >= 0.001 and adj_dollars > 0):
        with st.expander(
            f"🚀 Place real order on Kalshi for **{outcome}**", expanded=False
        ):
            st.caption(
                "**Real money.** This sends a limit order to your live Kalshi "
                "account. Review every field before clicking the final button."
            )
            # Kalshi ticker == condition_id for Kalshi-sourced markets.
            ticker = str(selected_market_cid)

            # Default count = Kelly-sized dollars / limit price
            default_limit_cents = max(1, min(99, int(round(mkt_price * 100))))
            default_count = max(1, int(adj_dollars // (default_limit_cents / 100.0)))

            oc1, oc2, oc3 = st.columns(3)
            with oc1:
                limit_cents = st.number_input(
                    "Limit price (cents)",
                    min_value=1, max_value=99,
                    value=default_limit_cents, step=1,
                    key=f"limit_cents_{outcome}",
                )
            with oc2:
                count = st.number_input(
                    "Count (contracts)",
                    min_value=1, max_value=10_000,
                    value=default_count, step=1,
                    key=f"order_count_{outcome}",
                    help="Each contract pays $1 on a YES resolution if you "
                         "bought YES (and vice versa for NO).",
                )
            with oc3:
                # On a Kalshi binary market, BOTH outcome rows refer to
                # the SAME ticker. The Kalshi API side ("yes"/"no") is
                # the contract type being bought, not "bet for/against
                # this outcome row." So we need to invert when looking
                # at the No row:
                #   Yes row + bet FOR Yes  → buy YES
                #   Yes row + bet AGAINST  → buy NO
                #   No row  + bet FOR No   → buy NO   (was buggy: "yes")
                #   No row  + bet AGAINST  → buy YES  (was buggy: "no")
                outcome_is_yes = str(outcome).strip().lower().startswith("y")
                if direction == "YES":
                    side = "yes" if outcome_is_yes else "no"
                else:
                    side = "no" if outcome_is_yes else "yes"
                st.metric("Side", side.upper(),
                          help="Kalshi contract type. Buying YES pays $1 if "
                               "the market resolves YES; buying NO pays $1 if "
                               "it resolves NO.")

            stake_dollars = (count * limit_cents) / 100.0
            max_payout = count * 1.0
            max_loss = stake_dollars
            st.caption(
                f"**Stake**: ${stake_dollars:.2f}  ·  "
                f"**Max payout**: ${max_payout:.2f}  ·  "
                f"**Max loss**: ${max_loss:.2f}  ·  "
                f"**Today's orders**: {ko._today_submit_count()}"
                f"/{ko.DEFAULT_MAX_ORDERS_PER_DAY}"
            )

            # Drawdown check
            df_snaps = pf.get_snapshots()
            dd = risk.current_drawdown(df_snaps) if not df_snaps.empty else None
            in_pause = (dd is not None
                        and risk.should_pause_for_drawdown(dd, settings.drawdown_pause_pct))

            # Current market mid (Kalshi reports it via outcome_prices in
            # our normalized DataFrame — for binary markets prices[0]
            # already is the YES mid). 5¢-buffer band check.
            current_mid_cents = int(round(mkt_price * 100)) if side == "yes" \
                else int(round((1.0 - mkt_price) * 100))

            issues = ko.pre_flight_check(
                ticker=ticker, side=side, count=int(count),
                limit_price_cents=int(limit_cents),
                current_mid_cents=current_mid_cents,
                bankroll=settings.bankroll,
                in_drawdown_pause=in_pause,
                todays_order_count=ko._today_submit_count(),
            )

            blocks = [m for sev, m in issues if sev == "block"]
            warns = [m for sev, m in issues if sev == "warn"]
            for m in blocks:
                st.error("🛑 " + m)
            for m in warns:
                st.warning("⚠️ " + m)

            # Two-click confirmation: a checkbox + button.
            ack = st.checkbox(
                "I have reviewed the order parameters above and want to "
                "submit this with real money.",
                key=f"ack_{outcome}",
                value=False,
            )
            if st.button(
                f"🚀 SUBMIT — {side.upper()} {int(count)} @ {int(limit_cents)}¢  "
                f"(${stake_dollars:.2f})",
                key=f"submit_order_{outcome}",
                type="primary",
                disabled=bool(blocks) or not ack,
                width="stretch",
            ):
                try:
                    resp = ko.place_limit_order(
                        ticker=ticker, side=side, action="buy",
                        count=int(count), limit_price_cents=int(limit_cents),
                    )
                    st.success(f"✅ Order accepted: {resp}")
                    st.balloons()
                except ko.OrderRejected as e:
                    st.error(f"Local guardrail rejected order: {e}")
                except Exception as e:
                    # HTTPError objects carry the response body — that's
                    # where Kalshi puts the human-readable rejection
                    # reason ("insufficient_balance", "market_not_open",
                    # "price_too_far_from_market", etc.).
                    body = ""
                    if hasattr(e, "response") and e.response is not None:
                        try:
                            body = e.response.text[:500]
                        except Exception:
                            pass
                    if body:
                        st.error(f"Kalshi rejected order: {e}\n\n"
                                 f"**Response body:** `{body}`")
                    else:
                        st.error(f"Kalshi rejected order: {e}")

# Kelly fraction vs. your-probability chart
st.divider()
st.subheader("Kelly fraction across all probability estimates")
st.caption("How the recommended bet size changes as your probability estimate changes.")

fig = go.Figure()
p_range = np.linspace(0.001, 0.999, 300)
colors = px.colors.qualitative.Set2

for i, (outcome, mkt_price, _, _) in enumerate(results):
    fracs = [pm.kelly_sizing(mkt_price, float(p))["kelly_fraction"] for p in p_range]
    fig.add_trace(go.Scatter(
        x=p_range, y=fracs,
        mode="lines", name=outcome,
        line=dict(color=colors[i % len(colors)], width=2),
    ))
    fig.add_vline(x=mkt_price, line_dash="dash", line_color=colors[i % len(colors)],
                  annotation_text=f"{outcome} mkt", annotation_position="top")

fig.add_hline(y=0, line_color="gray", line_width=1)
fig.update_layout(
    xaxis_title="Your probability estimate",
    yaxis_title="Kelly fraction (fraction of bankroll)",
    yaxis=dict(tickformat=".0%"),
    xaxis=dict(tickformat=".0%"),
    hovermode="x unified",
    height=380,
)
st.plotly_chart(fig, width="stretch")

st.caption(
    "Positive values = bet that outcome. Negative values = bet against it (bet the other side). "
    "The curve crosses zero at the market price — that's where your estimate agrees with the market."
)
