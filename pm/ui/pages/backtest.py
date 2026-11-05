"""Backtest page — Tier 3b. Replay strategies through historical resolved
markets and report PnL / hit rate / Brier / drawdown / per-category."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from pm.research import backtest as bt
from pm.research import backtest_history as bh
from pm.ui.shared import venue_display, venue_name
from pm.venue import provider as mp

st.title("Strategy Backtester")
st.caption(
    "Replay a strategy through recently-resolved markets and report "
    "hit rate, ROI, Brier, and drawdown. **This is the gate**: no alpha "
    "strategy should ship without a backtest that beats the market on "
    "its category."
)
eval_mode = st.radio(
    "Evaluation mode",
    options=["single_shot", "walk_forward"],
    format_func=lambda k: ("Single-shot (one trade per market, hold to resolution)"
                            if k == "single_shot"
                            else "Walk-forward (multi-trade per market, mark-to-market on signal flip)"),
    horizontal=True,
    help="**Single-shot**: one decision at `closed_time − lead_hours`, hold "
         "to settlement. Fast and simple; sufficient for strategies that "
         "act on one decision point. **Walk-forward**: walks the price "
         "history at fixed steps, calls the strategy at each, tracks "
         "one in-or-out position per market with mark-to-market exits on "
         "signal flip and resolution exit at $1/$0. Required for "
         "momentum / mean-reversion strategies that need multiple "
         "decision points to evaluate honestly.",
)

# ──────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────

c1, c2, c3 = st.columns([2, 2, 2])
with c1:
    strategy_options = bt.list_strategies()
    strategy_key = st.selectbox(
        "Strategy",
        options=[k for k, _ in strategy_options],
        format_func=lambda k: dict(strategy_options)[k],
        help="The decision function applied to each market snapshot.",
    )
with c2:
    n_markets = st.number_input(
        "Closed markets to evaluate",
        min_value=10, max_value=300, value=50, step=10,
        help="Each market costs one extra API call for its price history. "
             "50 takes ~30s on a cold cache; cached results are instant.",
    )
with c3:
    lead_hours = st.slider(
        "Lead time before resolution (hours)",
        min_value=1.0, max_value=168.0, value=24.0, step=1.0,
        help="At what lead time before each market closed do we evaluate? "
             "Shorter = more information available; longer = harder problem.",
    )

c4, c5, c6 = st.columns([2, 2, 2])
with c4:
    bankroll = st.number_input(
        "Backtest bankroll ($)", min_value=100.0, max_value=1_000_000.0,
        value=1000.0, step=100.0,
        help="Notional bankroll for PnL accounting. Each trade sizes off this "
             "anchor — Kelly fractions are not compounded across trades in v1, "
             "so the bankroll stays constant.",
    )
with c5:
    kelly_mult = st.slider(
        "Kelly multiplier", min_value=0.1, max_value=1.0, value=0.5, step=0.05,
        help="0.5 = half-Kelly (the standard variance-reduction default). "
             "1.0 = full Kelly. Below 0.25 is too defensive to learn from.",
    )
with c6:
    history_days = st.slider(
        "Price-history lookback (days)", min_value=7, max_value=180, value=90, step=7,
        help="How much history to fetch per market. The strategy sees this "
             "much (truncated to strictly-before the lead-time cutoff).",
    )

if eval_mode == "walk_forward":
    eval_step_hours = st.slider(
        "Eval step (hours)",
        min_value=1, max_value=168, value=24, step=1,
        help="How often the strategy is evaluated within each market's "
             "price history. 24h is the typical default; 1h captures "
             "intraday signals at the cost of 24× the strategy calls.",
    )
else:
    eval_step_hours = 24  # unused in single-shot path

c7, c8 = st.columns([2, 2])
with c7:
    half_spread = st.slider(
        "Half-spread (¢ per side)",
        min_value=0.0, max_value=0.05, value=0.005, step=0.001, format="%.3f",
        help="The effective entry price shifts worse than mid by this much. "
             "0.005 = half a cent per side (typical for liquid prediction "
             "markets). Wider spreads both reduce payoff AND shrink Kelly "
             "fraction — strategies whose edge gets eaten skip those trades. "
             "Set 0 for the legacy ideal-mid-fill behaviour.",
    )
with c8:
    fee_pct = st.slider(
        "Fee (% of stake)",
        min_value=0.0, max_value=0.05, value=0.0, step=0.0025, format="%.4f",
        help="Per-trade fee as fraction of stake. Polymarket has ~0% order "
             "fees on most markets; Kalshi has tier-based fees. Use a "
             "representative value for your venue if applicable.",
    )

portfolio_constrained = st.checkbox(
    "💰 Portfolio-level bankroll constraint",
    value=True,
    help="When on (the default), the engine tracks available cash through "
         "the run and caps each new stake at min(Kelly-desired, cash). "
         "A strategy fully invested in five overlapping markets can't size "
         "into the sixth — same as live execution against a single account. "
         "Turn off to recover the older \"each trade sized against full "
         "bankroll independently\" baseline; useful for pure strategy "
         "comparison but not realistic for live trading.",
)

# ──────────────────────────────────────────────────────────────────────────
# Strategy-specific parameter exposure (kwargs of the strategy callable)
# ──────────────────────────────────────────────────────────────────────────

strategy_fn = bt.get_strategy(strategy_key)
strategy_kwargs: dict[str, Any] = {}
sig = inspect.signature(strategy_fn)
tunable = [
    (name, p) for name, p in sig.parameters.items()
    if p.kind == inspect.Parameter.KEYWORD_ONLY and isinstance(p.default, int | float)
]
if tunable:
    with st.expander("⚙️ Strategy parameters"):
        st.caption(
            "These are the strategy's own knobs. Defaults come from the "
            "module; tune to see how sensitive the strategy is."
        )
        kw_cols = st.columns(min(3, len(tunable)))
        for i, (name, param) in enumerate(tunable):
            col = kw_cols[i % len(kw_cols)]
            default = float(param.default)
            # Heuristic ranges: probability-like in (0, 1), hours up to a week
            if name.endswith("_hours") or name == "lookback_hours":
                strategy_kwargs[name] = col.slider(
                    name, min_value=1.0, max_value=168.0,
                    value=default, step=1.0,
                )
            elif 0.0 <= default <= 1.0:
                strategy_kwargs[name] = col.slider(
                    name, min_value=0.0, max_value=1.0,
                    value=default, step=0.01, format="%.2f",
                )
            else:
                strategy_kwargs[name] = col.number_input(
                    name, value=default, step=1.0,
                )


def _bound_strategy(snap):
    return strategy_fn(snap, **strategy_kwargs) if strategy_kwargs else strategy_fn(snap)


# ──────────────────────────────────────────────────────────────────────────
# Dataset cache — fetching price histories is the slow part
# ──────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _load_dataset_cached(
    venue: str, n_markets: int, lead_hours: float, history_days: int,
) -> list[tuple[bt.MarketSnapshot, bt.Resolution]]:
    """Cached dataset build. ``venue`` is part of the cache key so a
    provider switch invalidates correctly."""
    provider = mp.get_provider(venue)
    df_closed = provider.get_closed_markets(limit=int(n_markets))
    if df_closed.empty:
        return []
    return bt.build_dataset(
        provider, df_closed,
        lead_hours=float(lead_hours),
        history_days=int(history_days),
        fidelity=60,
        max_markets=int(n_markets),
    )


run = st.button("▶️ Run backtest", type="primary")

if run:
    with st.spinner(f"Loading {n_markets} closed markets from {venue_display()}…"):
        dataset = _load_dataset_cached(
            mp.active_provider_name(),
            int(n_markets), float(lead_hours), int(history_days),
        )

    if not dataset:
        st.error(
            "No usable closed markets found. Try lowering the lead time, "
            "increasing the lookback, or switching venues."
        )
        st.stop()

    with st.spinner(f"Running {strategy_key}…"):
        if eval_mode == "walk_forward":
            result = bt.run_backtest_walk_forward(
                _bound_strategy, dataset,
                bankroll=float(bankroll),
                kelly_multiplier=float(kelly_mult),
                half_spread=float(half_spread),
                fee_pct=float(fee_pct),
                eval_step_hours=float(eval_step_hours),
                portfolio_constrained=bool(portfolio_constrained),
                strategy_name=strategy_key,
            )
        else:
            result = bt.run_backtest(
                _bound_strategy, dataset,
                bankroll=float(bankroll),
                kelly_multiplier=float(kelly_mult),
                half_spread=float(half_spread),
                fee_pct=float(fee_pct),
                portfolio_constrained=bool(portfolio_constrained),
                strategy_name=strategy_key,
            )
    baseline_brier, baseline_n = bt.market_baseline_brier(dataset)
    st.session_state["last_backtest"] = result
    # Persist the run so it survives session/script restarts and shows up
    # in 📜 Backtest history. Wrapped: a save failure must never block
    # the user from seeing the result they just paid 30s of API for.
    try:
        bh.save_run(
            result,
            params={
                "n_markets": int(n_markets),
                "lead_hours": float(lead_hours),
                "history_days": int(history_days),
                "bankroll": float(bankroll),
                "kelly_multiplier": float(kelly_mult),
                "half_spread": float(half_spread),
                "fee_pct": float(fee_pct),
                "eval_step_hours": float(eval_step_hours),
                "portfolio_constrained": bool(portfolio_constrained),
                "strategy_kwargs": strategy_kwargs,
            },
            venue=mp.active_provider_name(),
            eval_mode=eval_mode,
        )
    except Exception as e:
        st.warning(f"Couldn't save run to history: {e}")
    st.session_state["last_baseline"] = (baseline_brier, baseline_n)

# Render whichever result is current (so widget tweaks below don't rerun
# the backtest if the user just wants to inspect the table)
result: bt.BacktestResult | None = st.session_state.get("last_backtest")

if result is None:
    st.caption(
        "Pick a strategy and click ▶️ to run. Datasets are cached for "
        "6 hours per (venue, n_markets, lead_hours, history_days) tuple, "
        "so tuning a strategy's own parameters is fast after the first run."
    )
    st.stop()


# ──────────────────────────────────────────────────────────────────────────
# Results
# ──────────────────────────────────────────────────────────────────────────

st.divider()
st.subheader(f"Result: {result.strategy_name}")

if result.warnings:
    for w in result.warnings:
        st.warning(w)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Trades", result.n_trades,
          help=f"Out of {result.n_markets_evaluated} markets the strategy saw.")
m2.metric(
    "Hit rate",
    f"{result.hit_rate*100:.1f}%" if result.hit_rate is not None else "—",
    help=(f"95% Wilson CI: {result.hit_rate_ci[0]*100:.1f}%–"
          f"{result.hit_rate_ci[1]*100:.1f}%") if result.hit_rate_ci else None,
)
m3.metric(
    "Total PnL",
    f"${result.total_pnl:,.2f}",
    delta=f"{result.roi*100:+.1f}% ROI" if result.roi is not None else None,
    help="ROI = total PnL / total $ staked (not bankroll).",
)
m4.metric(
    "Max drawdown", f"${result.max_drawdown:,.2f}",
    help="Worst peak-to-trough on the cumulative-PnL equity curve, anchored at $0.",
)

m5, m6, m7, m8 = st.columns(4)
m5.metric(
    "Brier",
    f"{result.brier_score:.4f}" if result.brier_score is not None else "—",
    help="Mean (probability − actual)². Lower is better.",
)
m6.metric(
    "Mean $/trade",
    f"${result.mean_pnl_per_trade:,.2f}" if result.mean_pnl_per_trade is not None else "—",
)
m7.metric(
    "Total stake", f"${result.total_stake:,.2f}",
    help="Sum of dollars deployed across all trades.",
)
m8.metric(
    "Sharpe (per-trade)",
    f"{result.sharpe_per_trade:.2f}" if result.sharpe_per_trade is not None else "—",
    help="Per-trade mean / std. Not annualized — prediction-market trades have "
         "variable horizons so the usual annualization is misleading.",
)

# Transaction-cost panel — shows how much spread + fees ate. The
# strategy must beat the market baseline AND keep ROI positive after
# these costs to graduate from advisory to influence-Kelly-sizing.
cc1, cc2, cc3 = st.columns(3)
cc1.metric(
    "Half-spread (¢/side)", f"{result.half_spread*100:.3f}",
    help="Effective entry price was shifted worse than mid by this amount.",
)
cc2.metric(
    "Spread cost (implied)", f"${result.implied_spread_cost:,.2f}",
    help="Sum of what each trade gave up by filling at ask instead of mid. "
         "Already absorbed into PnL above; shown here so you can see how "
         "much of your gross edge was paid to liquidity providers.",
)
cc3.metric(
    "Fees paid", f"${result.total_fees:,.2f}",
    help=f"Total per-trade fees at {result.fee_pct*100:.2f}% of stake.",
)

# Portfolio-constraint panel — only meaningful when the constraint is on.
if result.portfolio_constrained:
    pc1, pc2, pc3 = st.columns(3)
    pc1.metric(
        "Min cash seen", f"${result.min_cash_seen:,.2f}",
        help="The trough of available cash during the run. $0 means the "
             "strategy ran out completely at some point.",
    )
    pc2.metric(
        "Trades clipped",
        f"{result.n_clipped_for_cash}",
        help="Number of trades whose stake was smaller than Kelly-desired "
             "because cash was constrained.",
    )
    pc3.metric(
        "Trades skipped",
        f"{result.n_skipped_for_cash}",
        help="Number of would-be trades that got $0 stake because cash was "
             "fully committed elsewhere. A non-zero count means the bankroll "
             "is too small for this strategy's concurrent exposure.",
    )

# ── Gate: does the strategy beat the market baseline on Brier? ──────────
baseline = st.session_state.get("last_baseline")
if baseline is not None and baseline[0] is not None and result.brier_score is not None:
    b_brier, b_n = baseline
    delta = result.brier_score - b_brier
    beats = delta < 0
    st.markdown("**Brier vs. market baseline:**")
    g1, g2, g3 = st.columns(3)
    g1.metric("Strategy Brier", f"{result.brier_score:.4f}")
    g2.metric("Market-baseline Brier",
              f"{b_brier:.4f}",
              help=f"\"Just trust the latest price\" applied to the same "
                   f"{b_n} snapshots. The gate every alpha strategy has to "
                   f"clear before it's worth shipping.")
    g3.metric("Δ (strategy − baseline)",
              f"{delta:+.4f}",
              delta="better" if beats else "worse",
              delta_color="normal" if beats else "inverse")
    if beats:
        st.success(
            "✅ Strategy beats the market baseline on Brier. Re-run with "
            "more markets (300+) before concluding the edge is real."
        )
    else:
        st.warning(
            "⚠️ Strategy loses to "
            "*just trusting the latest price* on this dataset. Tune "
            "parameters or scope to the category where it does best — "
            "look at the per-category Brier breakdown below."
        )

# ── Equity curve ─────────────────────────────────────────────────────────
if not result.equity_curve.empty:
    fig_eq = go.Figure()
    fig_eq.add_trace(go.Scatter(
        x=result.equity_curve["closed_time"],
        y=result.equity_curve["cumulative_pnl"],
        mode="lines+markers",
        line=dict(color="royalblue", width=2),
        hovertemplate="%{x}<br>Cumulative PnL: $%{y:,.2f}<extra></extra>",
    ))
    fig_eq.add_hline(y=0, line=dict(color="gray", dash="dot"))
    fig_eq.update_layout(
        title="Cumulative PnL (sorted by market resolution time)",
        xaxis_title="", yaxis_title="$",
        height=320, hovermode="x unified",
    )
    st.plotly_chart(fig_eq, width="stretch")

# ── Per-category breakdown ───────────────────────────────────────────────
if result.by_category:
    st.markdown("**By category:**")
    rows = []
    for cat, stats in sorted(result.by_category.items()):
        rows.append({
            "category": cat,
            "trades": stats["n_trades"],
            "hit_rate": f"{stats['hit_rate']*100:.1f}%" if stats["hit_rate"] is not None else "—",
            "pnl": f"${stats['total_pnl']:,.2f}",
            "roi": f"{stats['roi']*100:+.1f}%" if stats["roi"] is not None else "—",
            "brier": f"{stats['brier']:.4f}",
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption(
        "Strategies often have lopsided category performance. A strategy "
        "that loses overall but wins on one category is still useful — "
        "ship it scoped to that category."
    )

# ── Raw trade log ────────────────────────────────────────────────────────
with st.expander("📄 Trade log"):
    if not result.trades.empty:
        disp = result.trades.copy()
        for col in ["probability", "market_yes_price", "entry_price"]:
            disp[col] = disp[col].apply(lambda x: f"{x:.3f}")
        for col in ["stake", "pnl"]:
            disp[col] = disp[col].apply(lambda x: f"${x:,.2f}")
        disp["brier_term"] = disp["brier_term"].apply(lambda x: f"{x:.4f}")
        st.dataframe(
            disp[[
                "as_of", "closed_time", "category", "question",
                "direction", "market_yes_price", "probability", "entry_price",
                "stake", "won", "pnl", "brier_term",
            ]],
            width="stretch", hide_index=True,
        )
        st.download_button(
            "📥 Download trade log CSV",
            data=result.trades.to_csv(index=False).encode("utf-8"),
            file_name=(
                f"{venue_name()}_backtest_{result.strategy_name}_"
                f"{datetime.now(UTC).strftime('%Y%m%d_%H%M')}.csv"
            ),
            mime="text/csv",
        )
    else:
        st.caption("No trades.")
