"""Edges page — Tier 4. Surfaces flagged edge candidates from external
data sources for the user to inspect (not auto-execute).

Per the recommendations design doc: an edge here is *advisory*. A new
source is only allowed to influence Kelly sizing after a Tier 3b
backtest shows its predictions beat the market on the relevant
category. The 🧪 Backtest page runs that gate — for the crypto-spot
source, pick **"Crypto spot — lognormal (external)"** in the strategy
dropdown.
"""

from __future__ import annotations

from datetime import UTC, datetime

import streamlit as st

import edges
from pm.feeds import llm_data, sports_data
from pm.ui.shared import load_markets, load_markets_closing_soon, venue_display, venue_name
from pm.venue import polymarket_client as pm
from pm.venue import provider as mp_provider
from pm.venue import slippage

st.title("🔭 Edges")
st.caption(
    "Flagged gaps between the market's implied probability and a "
    "model-derived reference value. Advisory only — every source has "
    "to clear the Backtest gate before it's allowed to size positions."
)

# ──────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────

c1, c2 = st.columns([2, 4])
with c1:
    source_options = edges.list_sources()
    source_key = st.selectbox(
        "Edge source",
        options=[k for k, _ in source_options],
        format_func=lambda k: dict(source_options)[k],
    )
with c2:
    min_gap = st.slider(
        "Min |gap|", min_value=0.05, max_value=0.50, value=0.10, step=0.01,
        format="%.2f",
        help="Minimum |model − market| to flag. Below ~10pp, transaction "
             "costs likely eat any apparent edge.",
    )

# Time-to-resolution band — default upper bound of 30 days keeps the
# "Mars 2099" markets out of an initial-testing scan.
t1, t2 = st.columns(2)
with t1:
    min_days = st.number_input(
        "Min days to resolution",
        min_value=0, max_value=3650, value=1, step=1,
        help="Skip markets resolving sooner than this. 0 = no minimum.",
    )
with t2:
    max_days = st.number_input(
        "Max days to resolution",
        min_value=0, max_value=3650, value=30, step=1,
        help="Skip markets that don't resolve until further out. 0 = no maximum.",
    )

# ── Source-specific knobs ────────────────────────────────────────────────
source_kwargs: dict = {"min_gap": float(min_gap)}
# Every finder in edges.py accepts min_days_to_expiry; the max bound
# is enforced below by filtering the markets DataFrame before dispatch.
source_kwargs["min_days_to_expiry"] = float(min_days)

if source_key == "crypto_spot":
    vol_window = st.slider(
        "Vol window (days)", min_value=14, max_value=180, value=30, step=7,
        help="Days of daily closes used to compute annualized realized "
             "volatility for the lognormal / barrier model.",
    )
    source_kwargs["vol_window_days"] = int(vol_window)

elif source_key == "manifold":
    mc1, mc2, mc3 = st.columns(3)
    with mc1:
        manifold_max_markets = st.number_input(
            "Markets to scan", min_value=5, max_value=100, value=30, step=5,
            help="One Manifold search per market. Slowest source — 1-2s per call.",
        )
    with mc2:
        manifold_min_sim = st.slider(
            "Min similarity", min_value=0.10, max_value=0.80, value=0.30, step=0.05,
            format="%.2f",
            help="Jaccard token-overlap floor. Lower = more matches but more "
                 "noise; higher = only obvious matches but fewer hits.",
        )
    with mc3:
        manifold_min_vol = st.number_input(
            "Min Manifold volume", min_value=0, max_value=10_000, value=100, step=50,
            help="Thin Manifold markets (low mana volume) are dominated by 1-2 "
                 "traders; their community probability is noisy.",
        )
    source_kwargs["max_markets"] = int(manifold_max_markets)
    source_kwargs["min_similarity"] = float(manifold_min_sim)
    source_kwargs["min_volume"] = float(manifold_min_vol)

    # LLM-assisted semantic verification — fixes the direction-blindness
    # failure mode Jaccard can't catch ("Fed cut" vs "Fed hike" share
    # 5/6 tokens but ask opposites). Opt-in because it adds ~1s per
    # candidate that survives the cheap pre-filter.
    if llm_data.is_available():
        with st.expander("🤖 LLM semantic verification (optional)"):
            st.caption(
                "After Jaccard picks the top match per market, optionally "
                "ask a local LLM whether the two questions actually ask "
                "the same thing. Adds ~1s per market that survives the "
                "gap check. Catches \"cut vs. hike\" / \"by July vs. by "
                "August\" / etc. — failure modes Jaccard is structurally "
                "blind to."
            )
            llm_verify_on = st.checkbox(
                "Enable LLM verification", value=False,
                key="manifold_llm_verify",
            )
            if llm_verify_on:
                available_models = llm_data.list_models()
                if not available_models:
                    st.warning("Ollama is reachable but no models pulled. "
                               "Run `ollama pull llama3.1` first.")
                else:
                    default_idx = 0
                    for i, name in enumerate(available_models):
                        if "llama3.1" in name.lower():
                            default_idx = i
                            break
                    chosen = st.selectbox(
                        "Verification model",
                        options=available_models, index=default_idx,
                        key="manifold_llm_verify_model",
                    )
                    min_conf = st.slider(
                        "Min LLM confidence", min_value=0.0, max_value=1.0,
                        value=0.6, step=0.05, format="%.2f",
                        help="Reject matches whose LLM-self-reported "
                             "confidence falls below this. Strict default "
                             "of 0.6 trades recall for precision.",
                        key="manifold_llm_min_conf",
                    )
                    source_kwargs["llm_verify"] = True
                    source_kwargs["llm_verify_model"] = chosen
                    source_kwargs["llm_min_confidence"] = float(min_conf)

elif source_key == "sports":
    chosen_leagues = st.multiselect(
        "Leagues",
        options=list(sports_data.LEAGUES.keys()),
        default=list(sports_data.LEAGUES.keys()),
        help="Which ESPN leagues to pull scheduled events from. Fewer "
             "leagues = faster scan.",
    )
    if not chosen_leagues:
        st.warning("Pick at least one league.")
        st.stop()
    source_kwargs["leagues"] = list(chosen_leagues)

elif source_key == "llm":
    if not llm_data.is_available():
        st.error(
            f"Ollama isn't reachable at the configured base URL "
            f"(`{llm_data.OLLAMA_BASE}`). Start Ollama or set "
            "`OLLAMA_API_BASE` in `.env` and reload."
        )
        st.stop()

    available = llm_data.list_models()
    if not available:
        st.error(
            "Ollama is reachable but has no models pulled. Run "
            "`ollama pull llama3.1` (or your model of choice) and reload."
        )
        st.stop()

    # Prefer something fast as the initial selection if present.
    default_idx = 0
    for i, name in enumerate(available):
        if "llama3.1" in name.lower() or "mistral-small" in name.lower():
            default_idx = i
            break

    lc1, lc2 = st.columns([3, 2])
    with lc1:
        chosen_model = st.selectbox(
            "Local model", options=available, index=default_idx,
            help="All models pulled into Ollama. Smaller (7-13B) is fast; "
                 "larger (30B+) tends to be better-calibrated but slower.",
        )
    with lc2:
        max_markets = st.number_input(
            "Markets to score", min_value=5, max_value=100, value=20, step=5,
            help="One LLM call per market. Small models do this in <1s; "
                 "30B+ models can take 5-10s each.",
        )
    source_kwargs["model"] = chosen_model
    source_kwargs["max_markets"] = int(max_markets)

with st.spinner(f"Loading markets from {venue_display()}…"):
    # When max_days is short, use the time-filtered fetch path so
    # short-dated markets aren't buried by the volume ordering.
    if 0 < max_days <= 90:
        df_markets = load_markets_closing_soon(float(max_days), limit=500)
    else:
        df_markets = load_markets()

if df_markets.empty:
    st.warning("Could not load markets.")
    st.stop()

# Apply the user's time band before dispatch so every source sees the
# same filtered universe. min-days is forwarded as a kwarg too (most
# sources have their own gate); max-days is enforced here since no
# finder accepts a max.
df_scoped = df_markets
if "days_to_resolution" in df_scoped.columns:
    if min_days > 0:
        df_scoped = df_scoped[df_scoped["days_to_resolution"].fillna(-1) >= min_days]
    if max_days > 0:
        df_scoped = df_scoped[df_scoped["days_to_resolution"].fillna(1e9) <= max_days]

st.caption(
    f"Scanning **{len(df_scoped):,}** markets out of {len(df_markets):,} "
    f"after the time-to-resolution filter ({min_days}–{max_days or '∞'} days)."
)

run = st.button("🔍 Find edges", type="primary")
if run:
    finder = edges.get_finder(source_key)
    progress = st.progress(0.0, text="Scanning…")

    def _on_progress(i: int, n: int) -> None:
        progress.progress(min(1.0, i / max(1, n)), text=f"Scanning {i}/{n}…")

    # Only the LLM source uses the progress callback; pass it through
    # only if the finder accepts it. (Inspect is cheaper than try/except.)
    import inspect
    if "progress_callback" in inspect.signature(finder).parameters:
        source_kwargs["progress_callback"] = _on_progress

    with st.spinner("Scanning external data…"):
        flagged = finder(df_scoped, **source_kwargs)
    progress.empty()
    st.session_state["last_edges"] = flagged

flagged: list[edges.Edge] | None = st.session_state.get("last_edges")
if flagged is None:
    st.caption("Pick a source and click 🔍 to scan. Results are cached "
               "in-session until the next scan.")
    st.stop()

# ──────────────────────────────────────────────────────────────────────────
# Results
# ──────────────────────────────────────────────────────────────────────────

if not flagged:
    st.info(
        "No edges met the threshold. Try lowering Min |gap| or check "
        "back when markets reprice. Many crypto markets sit near the "
        "lognormal-model price and won't flag at all."
    )
    st.stop()

st.success(f"{len(flagged)} edge candidate(s) found.")

# ──────────────────────────────────────────────────────────────────────────
# Live order-book slippage (opt-in)
# ──────────────────────────────────────────────────────────────────────────
# An advertised edge of +12pp on a market with $50 of YES depth doesn't
# survive a $500 stake. This panel fetches each flagged edge's current
# book, walks the relevant side at the chosen stake, and shows the
# effective entry price + realized edge after slippage.

with st.expander("🔍 Estimate live slippage for each flagged edge"):
    st.caption(
        "Fetches the current order book for each flagged market and walks "
        "the relevant side at your chosen stake. Shows what the edge "
        "actually looks like after consuming book depth. One extra HTTP "
        "call per flag — fast (~100ms each) but visible on a 10-flag list."
    )
    slip_enabled = st.checkbox(
        "Enable slippage estimation", value=False, key="edges_slip_enabled",
    )
    slip_stake = st.number_input(
        "Stake to size for ($)",
        min_value=1.0, max_value=100_000.0, value=100.0, step=10.0,
        help="Used to walk the book. Bigger stakes walk more levels and "
             "show larger slippage. Pick something representative of how "
             "you'd actually trade.",
        key="edges_slip_stake",
    )

slippage_rows: dict[str, dict] = {}
if slip_enabled:
    provider = mp_provider.get_provider()
    with st.spinner(f"Fetching {len(flagged)} order book(s)…"):
        for e in flagged:
            try:
                tokens = provider.list_outcome_tokens(e.market_id)
            except Exception:
                tokens = []
            if not tokens:
                continue
            # For YES direction → walk asks of the YES token book.
            # For NO direction:
            #   - Polymarket: walk asks of the NO token's book (2nd token).
            #   - Kalshi: walk bids of the YES token's book.
            if e.direction == "YES":
                token_id = tokens[0]["token_id"]
                walk_side = "ask"
            elif len(tokens) >= 2:                  # Polymarket NO path
                token_id = tokens[1]["token_id"]
                walk_side = "ask"
            else:                                   # Kalshi NO path
                token_id = tokens[0]["token_id"]
                walk_side = "bid"
            try:
                raw_book = provider.get_order_book(token_id)
                book_df = pm.parse_order_book(raw_book)
            except Exception:
                continue
            est = slippage.walk_book_for_stake(
                book_df, side=walk_side, stake_dollars=float(slip_stake),
            )
            if est is None:
                continue
            eff_edge = slippage.effective_edge(e.model_implied, est)
            slippage_rows[e.market_id] = {
                "avg_fill_price": est.avg_fill_price,
                "best_price": est.best_price,
                "slippage_per_share": est.slippage_per_share,
                "fillable_stake": est.fillable_stake,
                "fully_filled": est.fully_filled,
                "levels_consumed": est.levels_consumed,
                "effective_edge": eff_edge,
                "walk_side": walk_side,
            }

# Summary metrics
sev_counts = {"high": 0, "medium": 0, "low": 0}
for e in flagged:
    sev_counts[e.severity] += 1
s1, s2, s3 = st.columns(3)
s1.metric("High severity", sev_counts["high"], help="|gap| ≥ 30pp")
s2.metric("Medium severity", sev_counts["medium"], help="15pp ≤ |gap| < 30pp")
s3.metric("Low severity", sev_counts["low"], help="|gap| < 15pp")

if source_key == "crypto_spot":
    st.warning(
        "🛑 **Advisory only.** Flags come from a GBM model (one-touch "
        "barrier or terminal close-on-date based on the question's verb). "
        "It ignores jumps, fat tails, and basis differences between the "
        "spot reference and the market's settlement source. Run a "
        "backtest before sizing anything off them."
    )
elif source_key == "sports":
    st.warning(
        "🛑 **Advisory only.** Flags come from ESPN's consensus sportsbook "
        "odds (moneyline → implied probability → vig removed). Two caveats: "
        "(1) team-name matching is best-effort; ambiguous questions are "
        "skipped silently. (2) The YES side is assumed to be whichever "
        "team appears first in the question — true for most "
        "\"Team A vs. Team B\" markets, not universal."
    )
elif source_key == "macro":
    st.warning(
        "🛑 **Advisory only.** Flags come from a normal-distribution-"
        "on-level model with realized volatility from FRED's historical "
        "data. Two caveats: (1) interest rates aren't actually Brownian "
        "— they have policy steps and mean-reversion the model ignores. "
        "(2) v1 only supports unemployment, 2y/10y Treasury yields, and "
        "the fed funds effective rate. CPI YoY, GDP growth, and Fed-"
        "decision markets are deliberately skipped."
    )
elif source_key == "manifold":
    st.warning(
        "🛑 **Advisory only — cross-platform comparison, not external "
        "ground truth.** Flags compare the active venue's YES price to "
        "Manifold's community probability on a best-match question (top "
        "result of natural-language search, gated by token-Jaccard "
        "similarity ≥ your floor). **Read the matched question on every "
        "flag.** Jaccard similarity is keyword-based and *direction-blind* "
        "— \"Will the Fed CUT rates?\" and \"Will the Fed HIKE rates?\" "
        "share 5 of 6 content tokens. Enable LLM verification above to "
        "have a local model catch these semantic-opposite cases."
    )
else:  # llm
    st.warning(
        "🛑 **Advisory only.** Flags come from a local LLM. Three caveats: "
        "(1) the model has a training cutoff and may lack recent info; "
        "(2) for markets that resolved before the cutoff, the model may "
        "recall the actual outcome from training data — biases backtests; "
        "(3) LLMs are bad calibrators by default. Run a backtest before "
        "sizing anything off them."
    )

df = edges.edges_to_dataframe(flagged)
display = df.copy()
display["market_yes_price"] = display["market_yes_price"].apply(lambda x: f"{x*100:.1f}%")
display["model_implied"] = display["model_implied"].apply(lambda x: f"{x*100:.1f}%")
display["gap"] = display["gap"].apply(lambda x: f"{x*100:+.1f}pp")

# Annotate with slippage columns when the user enabled it.
if slippage_rows:
    display["effective_edge"] = df["market_id"].map(
        lambda mid: slippage_rows.get(mid, {}).get("effective_edge"),
    )
    display["effective_edge"] = display["effective_edge"].apply(
        lambda v: f"{v*100:+.1f}pp" if v is not None else "—",
    )
    display["fillable"] = df["market_id"].map(
        lambda mid: slippage_rows.get(mid, {}).get("fillable_stake"),
    )
    display["fillable"] = display["fillable"].apply(
        lambda v: f"${v:,.0f}" if v is not None else "—",
    )
    display["avg_fill"] = df["market_id"].map(
        lambda mid: slippage_rows.get(mid, {}).get("avg_fill_price"),
    )
    display["avg_fill"] = display["avg_fill"].apply(
        lambda v: f"{v*100:.1f}%" if v is not None else "—",
    )

# Sort high → low → medium → low for display
sev_order = {"high": 0, "medium": 1, "low": 2}
display["_sev_rank"] = display["severity"].map(sev_order)
display = display.sort_values(["_sev_rank"]).drop(columns="_sev_rank")

table_cols = [
    "severity", "source", "category", "question",
    "market_yes_price", "model_implied", "gap", "direction", "note",
]
if slippage_rows:
    # Insert slippage columns right after `gap` so the comparison is
    # easy to eyeball — claimed edge vs. effective edge side-by-side.
    gap_idx = table_cols.index("gap")
    table_cols = (
        table_cols[:gap_idx + 1]
        + ["effective_edge", "avg_fill", "fillable"]
        + table_cols[gap_idx + 1:]
    )

st.dataframe(display[table_cols], width="stretch", hide_index=True)

b1, b2 = st.columns([1, 1])
with b1:
    st.download_button(
        "📥 Download edges CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"{venue_name()}_edges_{source_key}_{datetime.now(UTC).strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )
with b2:
    if st.button(f"📝 Log {len(flagged)} forecast(s) to Calibration"):
        n = edges.log_edges_to_calibration(flagged)
        st.cache_data.clear()
        st.success(
            f"Recorded {n} forecast(s) in the active profile. "
            f"Open the 📊 Calibration page to view per-source Brier."
        )

st.divider()
_gate_strategies = {
    "crypto_spot": "Crypto spot — lognormal (external)",
    "llm":         "LLM forecast — local Ollama (external)",
    "sports":      "Sports win-prob — ESPN consensus (external)",
    "manifold":    "Manifold community — Jaccard match (external)",
    "macro":       "Macro threshold — FRED normal-on-level (external)",
}
gate_strategy = _gate_strategies.get(source_key, "<the relevant strategy>")
st.markdown(
    f"**Next step:** open the 🧪 Backtest page, pick "
    f"**\"{gate_strategy}\"** as the strategy, and run it over the "
    f"closed-markets set. The Brier-vs-market-baseline gate there is what "
    f"decides whether this source is allowed to influence real Kelly sizing yet."
)
