"""Calibration page — Brier score + calibration plot over your saved forecasts."""

from datetime import UTC, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from pm.ledger import calibration as cal
from pm.ui.shared import venue_display, venue_name
from pm.venue import provider as mp


@st.cache_data(ttl=600, show_spinner=False)
def _build_closed_lookup() -> dict[str, bool]:
    """Union of recently-closed markets across all registered providers,
    overlaid with the user's authoritative Kalshi settlement history
    when authed.

    Returns ``{condition_id: resolved_yes}``. Skips markets whose
    resolution is None (Kalshi's ``result == ""`` case). Cached for 10
    minutes — fetching closed markets is the slowest call in the app.

    Authed Kalshi settlements take precedence over the provider-side
    public closed-markets feed for the same ticker — that account-
    side record is the source of truth for the user's resolved
    positions, and it covers tickers that may have aged out of the
    public ``get_closed_markets`` window.

    Providers that error out are logged and skipped; we'd rather
    auto-resolve from the providers that work than fail outright.
    """
    lookup: dict[str, bool] = {}
    for provider in mp.list_providers():
        try:
            df = provider.get_closed_markets(limit=200)
        except Exception:
            continue
        if df.empty:
            continue
        for _, row in df.iterrows():
            cid = row.get("condition_id")
            ry = row.get("resolved_yes")
            if not cid or ry is None or pd.isna(ry):
                continue
            lookup[str(cid)] = bool(ry)

    # Overlay Kalshi authed settlements — authoritative for the user's
    # own positions. Best-effort: if auth fails or the API is down we
    # keep the provider-side lookup we already built.
    try:
        from pm.venue import kalshi_auth as ka
        if ka.is_configured():
            from pm.venue import kalshi_client as kc
            df_settle = kc.get_settlements(limit=500)
            for _, row in df_settle.iterrows():
                ticker = row.get("ticker")
                result = str(row.get("market_result", "") or "").strip().lower()
                if not ticker or result not in ("yes", "no"):
                    continue
                lookup[str(ticker)] = (result == "yes")
    except Exception:
        # Auth errors / network blips here are not worth interrupting
        # the page with — the public-feed lookup is still usable.
        pass

    return lookup

st.title("Calibration Tracking")
st.caption(
    "Are your probability estimates accurate? Save forecasts from the Kelly "
    "Sizer, mark them resolved here, and the Brier score tells you whether "
    "your edge is real."
)

df_fc = cal.get_forecasts()
stats = cal.summary_stats(df_fc)

# ── Scoreboard ────────────────────────────────────────────────────────────
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total forecasts", stats["total"])
c2.metric("Resolved", stats["resolved"])
c3.metric("Pending", stats["pending"])
if stats["brier"] is not None:
    c4.metric(
        "Brier score", f"{stats['brier']:.4f}",
        help=(
            "Mean squared error between your probabilities and actual outcomes. "
            "0 = perfect, 0.25 = uniformly random on 50/50 markets. Lower is better."
        ),
    )
else:
    c4.metric("Brier score", "—", help="Available once at least one forecast is resolved.")

if df_fc.empty:
    st.divider()
    st.info(
        "No forecasts yet. Visit the **📐 Kelly Sizer**, pick a market, set your "
        "probability estimate, and click **💾 Save forecast**."
    )
    st.stop()

# ── Per-source Brier breakdown ────────────────────────────────────────────
# Surfaces external sources (LLM, crypto_spot) alongside the user's own
# Brier so it's obvious whether any of them actually beats hand-calibration.
by_source = cal.brier_by_source(df_fc)
if len(by_source) > 1:  # only show when we have at least one non-user source
    st.divider()
    st.subheader("By source")
    st.caption(
        "Each source tracks its own Brier so external sources (LLM, "
        "crypto_spot) don't get blended into your hand-recorded "
        "calibration. A source ships as a recommendation only once "
        "its Brier beats yours and the market baseline on its category."
    )
    src_rows = []
    for src, info in sorted(by_source.items()):
        src_rows.append({
            "source": src,
            "forecasts": info["n"],
            "brier": f"{info['brier']:.4f}" if info["brier"] is not None else "—",
        })
    st.dataframe(pd.DataFrame(src_rows), width="stretch", hide_index=True)

# Source filter for the plot + downstream log
source_options = sorted(df_fc["source"].dropna().unique().tolist())
if len(source_options) > 1:
    selected_sources = st.multiselect(
        "Filter by source",
        options=source_options,
        default=source_options,
        help="Restrict the calibration plot and forecast log to specific sources.",
    )
    if selected_sources:
        df_fc = df_fc[df_fc["source"].isin(selected_sources)].reset_index(drop=True)

# ── Calibration plot ──────────────────────────────────────────────────────
bins_df = cal.calibration_bins(df_fc, n_bins=10)
if not bins_df.empty:
    st.divider()
    st.subheader("Calibration plot")
    st.caption(
        "Each point: the average probability you gave forecasts that ended up "
        "in that probability bin, vs. the fraction of those forecasts that "
        "actually resolved your way. A perfectly calibrated forecaster sits "
        "on the diagonal."
    )
    fig = go.Figure()
    fig.add_shape(type="line", x0=0, y0=0, x1=1, y1=1,
                  line=dict(dash="dash", color="gray"))
    fig.add_trace(go.Scatter(
        x=bins_df["mean_predicted"],
        y=bins_df["observed_rate"],
        mode="markers+lines",
        marker=dict(size=bins_df["n"] * 4 + 8, color="royalblue"),
        text=[f"n={n}" for n in bins_df["n"]],
        hovertemplate="Predicted: %{x:.2f}<br>Observed: %{y:.2f}<br>%{text}<extra></extra>",
        name="You",
    ))
    fig.update_layout(
        xaxis=dict(title="Your predicted probability", range=[0, 1], tickformat=".0%"),
        yaxis=dict(title="Observed resolution rate", range=[0, 1], tickformat=".0%"),
        height=420,
        showlegend=False,
    )
    st.plotly_chart(fig, width="stretch")

# ── Auto-resolve pending forecasts ───────────────────────────────────────
pending = df_fc[df_fc["resolution"].isna()].copy()
if not pending.empty:
    st.divider()
    st.subheader("🔄 Auto-resolve")
    st.caption(
        "Match pending forecasts against closed markets across every "
        "registered venue. Each forecast's `condition_id` is looked up "
        "in the union of recently-resolved markets — direct matches get "
        "their resolution filled in automatically. Unmatched forecasts "
        "stay pending; you can still mark them manually below."
    )
    ar1, ar2 = st.columns([1, 2])
    with ar1:
        if st.button("Check for closed markets", type="primary", key="auto_resolve_btn"):
            with st.spinner("Fetching closed markets from every venue…"):
                lookup = _build_closed_lookup()
            updated_ids = cal.auto_resolve_pending(lookup)
            if updated_ids:
                st.cache_data.clear()
                st.toast(f"Auto-resolved {len(updated_ids)} forecast(s)", icon="🔄")
                st.rerun()
            else:
                st.info(
                    f"No matches found across {len(lookup)} closed "
                    "markets. Mark them manually below if you know the "
                    "outcome, or wait for the next venue refresh."
                )
    with ar2:
        st.caption(
            f"{len(pending)} pending forecast(s) "
            "in this profile. The closed-markets lookup is cached for "
            "10 minutes per venue — re-running here within that window "
            "doesn't re-hit any API."
        )

# ── Forecast log ──────────────────────────────────────────────────────────
st.divider()
st.subheader("Forecast log")

resolved = df_fc[df_fc["resolution"].notna()].copy()

if not pending.empty:
    st.markdown(f"**Pending resolution ({len(pending)})**")
    for _, fc in pending.iterrows():
        with st.container():
            col_q, col_p, col_y, col_n, col_x = st.columns([5, 1, 1, 1, 1])
            col_q.write(f"**{fc['question'][:60]}** — {fc['outcome']}")
            col_p.write(f"@ {fc['your_probability']:.1%}")
            if col_y.button("✓ Yes", key=f"res_yes_{fc['id']}", help="Mark Yes outcome"):
                cal.record_resolution(fc["id"], outcome_yes=True)
                st.rerun()
            if col_n.button("✗ No", key=f"res_no_{fc['id']}", help="Mark No outcome"):
                cal.record_resolution(fc["id"], outcome_yes=False)
                st.rerun()
            if col_x.button("🗑️", key=f"del_{fc['id']}", help="Delete forecast"):
                cal.delete_forecast(fc["id"])
                st.rerun()

if not resolved.empty:
    st.markdown(f"**Resolved ({len(resolved)})**")
    disp = resolved.copy()
    disp["correct"] = (
        disp["outcome"].astype(str).str.lower().eq("yes") == disp["resolution"].astype(bool)
    )
    disp["correct"] = disp["correct"].apply(lambda b: "✓" if b else "✗")
    disp["resolution"] = disp["resolution"].apply(lambda b: "Yes" if bool(b) else "No")
    disp["your_probability"] = disp["your_probability"].apply(lambda x: f"{x:.1%}")
    disp["market_price"] = disp["market_price"].apply(lambda x: f"{x:.1%}")
    cols = ["timestamp", "question", "outcome", "your_probability",
            "market_price", "resolution", "correct"]
    st.dataframe(disp[cols], width="stretch", hide_index=True)

# CSV export
st.download_button(
    "📥 Download forecast log as CSV",
    data=df_fc.to_csv(index=False).encode("utf-8"),
    file_name=f"{venue_name()}_forecasts_{datetime.now(UTC).strftime('%Y%m%d_%H%M')}.csv",
    mime="text/csv",
)

# ===========================================================================
# Market baseline (Tier 3a) — calibration of the market's own predictions
# ===========================================================================

st.divider()
st.subheader("📊 Market baseline (historical backfill)")
st.caption(
    "Until you accumulate enough resolved forecasts of your own, the panel "
    "above is empty. This section provides the alternative: compute the "
    "**market's own** calibration over recently-closed markets. It answers "
    f"*\"when {venue_display()} priced something at 60%, how often did it actually "
    "happen?\"* — a useful reference point for comparing your own Brier "
    "score later. If your Brier ends up worse than the market's, the rational "
    "move is to match the market price instead of betting your own number."
)

# Cached compute — fetching dozens of price histories is slow, so we cache
# for 6 hours. Closed markets don't update after they resolve.
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _compute_market_baseline(n_markets: int, lead_hours: float):
    provider = mp.get_provider()
    df_closed = provider.get_closed_markets(limit=n_markets)
    if df_closed.empty:
        return None
    df_cal = provider.get_market_calibration_data(
        df_closed,
        lead_hours=lead_hours,
        max_markets=n_markets,
    )
    return df_cal

col1, col2, col3 = st.columns([2, 2, 3])
with col1:
    mb_n = st.number_input(
        "Markets to analyse", min_value=10, max_value=300, value=50, step=10,
        help="Each market requires one extra API call for its price history. "
             "50 takes ~30s on a cold cache; cached results are instant.",
    )
with col2:
    mb_lead = st.slider(
        "Lead time before resolution (hours)",
        min_value=1.0, max_value=168.0, value=24.0, step=1.0,
        help="At what lead time before each market closed do we sample its "
             "price? 24h is a reasonable default — close enough to resolution "
             "that the market had real information, far enough that the "
             "outcome wasn't already obvious.",
    )
with col3:
    st.write("")
    st.write("")
    run_baseline = st.button(
        "Compute / refresh market baseline",
        type="secondary",
        help="Cached for 6 hours.",
    )

if run_baseline:
    # Clear the cache for this exact call so a fresh fetch runs
    _compute_market_baseline.clear()

df_market_cal = _compute_market_baseline(int(mb_n), float(mb_lead))

if df_market_cal is None or df_market_cal.empty:
    st.info(
        "No baseline computed yet — click the button above. "
        f"Note: this requires `n_markets` extra API calls to {venue_display()}; "
        "the result is cached for 6 hours."
    )
else:
    m_brier = cal.market_brier_score(df_market_cal)
    n_eff = len(df_market_cal)

    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("Markets scored", n_eff)
    mc2.metric("Avg lead time (h)", f"{df_market_cal['lead_hours'].mean():.1f}")
    mc3.metric(
        "Market Brier", f"{m_brier:.4f}" if m_brier is not None else "—",
        help="Lower = better. ≤ 0.10 = sharp; ~0.25 = no skill. Compare to "
             "your own Brier (top scoreboard) once you have resolved forecasts.",
    )

    # Per-category breakdown
    by_cat = cal.market_brier_by_category(df_market_cal)
    if by_cat:
        st.markdown("**By category:**")
        cat_df = pd.DataFrame([
            {"category": cat,
             "n_markets": int((df_market_cal["category"] == cat).sum()),
             "market_brier": f"{b:.4f}" if b is not None else "—"}
            for cat, b in sorted(by_cat.items())
        ])
        st.dataframe(cat_df, width="stretch", hide_index=True)

    # Side-by-side calibration plot (market vs you, if you have data)
    market_bins = cal.market_calibration_bins(df_market_cal, n_bins=10)
    if not market_bins.empty:
        st.markdown("**Market calibration plot:**")
        st.caption(
            "Same shape as your calibration plot above. Each dot: average "
            "predicted price in a bin vs. fraction of those markets that "
            "actually resolved YES. The market is usually very well-"
            "calibrated — that's the wisdom-of-crowds working."
        )
        m_fig = go.Figure()
        m_fig.add_shape(type="line", x0=0, y0=0, x1=1, y1=1,
                        line=dict(dash="dash", color="gray"))
        m_fig.add_trace(go.Scatter(
            x=market_bins["mean_predicted"],
            y=market_bins["observed_rate"],
            mode="markers+lines",
            marker=dict(size=market_bins["n"] * 2 + 8, color="seagreen"),
            text=[f"n={n}" for n in market_bins["n"]],
            hovertemplate="Market said: %{x:.2f}<br>Actually happened: %{y:.2f}<br>%{text}<extra></extra>",
            name="Market",
        ))
        m_fig.update_layout(
            xaxis=dict(title="Market's predicted probability (at lead time)",
                       range=[0, 1], tickformat=".0%"),
            yaxis=dict(title="Observed resolution rate", range=[0, 1], tickformat=".0%"),
            height=420, showlegend=False,
        )
        st.plotly_chart(m_fig, width="stretch")

    # Show the raw data for transparency
    with st.expander("Show raw market-calibration data"):
        disp = df_market_cal.copy()
        disp["predicted_prob"] = disp["predicted_prob"].apply(lambda x: f"{x:.3f}")
        disp["actual"] = disp["actual"].apply(lambda x: "Yes" if x == 1 else "No")
        disp["brier_term"] = disp["brier_term"].apply(lambda x: f"{x:.4f}")
        st.dataframe(disp, width="stretch", hide_index=True)
        st.download_button(
            "📥 Download market-baseline CSV",
            data=df_market_cal.to_csv(index=False).encode("utf-8"),
            file_name=f"{venue_name()}_market_baseline_{datetime.now(UTC).strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="dl_mb",
        )

    if stats["brier"] is not None and m_brier is not None:
        st.divider()
        diff = stats["brier"] - m_brier
        if diff < 0:
            st.success(
                f"🎯 Your Brier ({stats['brier']:.4f}) is **better** than the "
                f"market baseline ({m_brier:.4f}) by {abs(diff):.4f}. "
                "Your probability estimates have an edge over the consensus."
            )
        else:
            st.warning(
                f"📉 Your Brier ({stats['brier']:.4f}) is **worse** than the "
                f"market baseline ({m_brier:.4f}) by {diff:.4f}. "
                "The rational move is to match the market price rather than "
                "bet your own number — at least until your calibration improves. "
                "This is what the Tier 1 risk-adjusted Kelly does automatically."
            )
