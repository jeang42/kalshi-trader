"""🧠 Deep Analysis — slow per-market LLM reasoning with structured output.

Complement to the fast batch Edges / Recommendations pages. One market
at a time, several context fields packed into the prompt (price
history, market metadata, resolution criteria, order book, sibling
markets), structured JSON output (probability + confidence + key
drivers + risks + evidence + recommended action), and a save-to-
calibration button so the model's track record builds independently
of the fast `llm:` source.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from pm.feeds import llm_data
from pm.feeds import llm_deep_analysis as lda
from pm.ledger import calibration as cal
from pm.ui.shared import load_markets
from pm.venue import provider as mp

st.title("🧠 Deep Analysis")
st.caption(
    "Slow per-market LLM reasoning. Picks one market, packs price "
    "history + metadata + order book + siblings into one prompt, "
    "asks a local model for a structured forecast. Use when the fast "
    "Recommendations page surfaces an interesting candidate but you "
    "want the model's reasoning before committing capital."
)

# ──────────────────────────────────────────────────────────────────────────
# Pre-flight: do we have a model?
# ──────────────────────────────────────────────────────────────────────────

if not llm_data.is_available():
    st.error(
        "Local Ollama not reachable at `OLLAMA_API_BASE`. Start it "
        "(`ollama serve`) or set the env var if it's running elsewhere."
    )
    st.stop()

_all_models = llm_data.list_models()
# Filter out obvious non-chat models (embeddings, vision-only, captioners)
# — they 500 on /api/chat with format=json. Conservative filter; user
# can untick "Hide non-chat models" to see everything if needed.
filter_models = st.sidebar.checkbox(
    "Hide non-chat models", value=True,
    help="Embeddings / vision-only / captioner models often 500 on the "
         "chat endpoint with format=json. Toggle off to show every "
         "installed model.",
)
_chat_models = (
    [m for m in _all_models if lda.is_likely_chat_model(m)]
    if filter_models else _all_models
)
# Pin known-good models to the top of the list and default to the
# first one that's installed (qwen3:32b → qwen2.5:32b → … → llama3.1).
available_models = lda.sort_models_recommended_first(_chat_models)
_default = lda.default_model(available_models)
_default_idx = available_models.index(_default) if _default in available_models else 0

if not available_models:
    st.error(
        "No models pulled into Ollama. Run e.g. `ollama pull qwen3:32b` "
        "or `ollama pull llama3.1:latest` and refresh."
    )
    st.stop()

# ──────────────────────────────────────────────────────────────────────────
# Market selection (mirrors Kelly Sizer lookup pattern)
# ──────────────────────────────────────────────────────────────────────────

with st.spinner("Loading markets…"):
    df_markets = load_markets()

with st.expander("🔎 Look up a specific market by ticker or URL", expanded=False):
    st.caption(
        "Paste a Kalshi URL, an event ticker (returns all sub-markets), "
        "or a single market ticker."
    )
    lookup_input = st.text_input(
        "Ticker or URL", value="",
        placeholder="https://kalshi.com/markets/... or KXKY4R-26",
        key="da_lookup",
    )
    if lookup_input.strip():
        try:
            _p = mp.get_provider()
            df_lookup = _p.lookup_markets_by_id(lookup_input.strip())
        except Exception as e:
            st.error(f"Lookup failed: {e}")
            df_lookup = pd.DataFrame()
        if df_lookup is None or df_lookup.empty:
            st.warning(f"No markets found for `{lookup_input}`.")
        else:
            existing = set(df_markets["condition_id"].astype(str))
            df_lookup = df_lookup[
                ~df_lookup["condition_id"].astype(str).isin(existing)
            ]
            df_markets = pd.concat([df_lookup, df_markets], ignore_index=True)
            st.success(f"Added {len(df_lookup)} market(s) to the picker.")

if df_markets.empty:
    st.warning("No markets loaded.")
    st.stop()

c1, c2 = st.columns([3, 2])
with c1:
    selected_q = st.selectbox(
        "Select market to analyse",
        options=df_markets["question"].tolist(),
        index=0,
    )
with c2:
    chosen_model = st.selectbox(
        "Model",
        options=available_models,
        index=_default_idx,
        help="Recommended models pinned to the top; default is the "
             "first one you have pulled (qwen3:32b preferred). "
             "Larger = better reasoning, slower. Local hardware-bound "
             "— count on 30s–3min depending on model size and prompt "
             "length.",
    )

row = df_markets[df_markets["question"] == selected_q].iloc[0]
ticker = str(row["condition_id"])

st.divider()

# ──────────────────────────────────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────────────────────────────────

run_col1, run_col2, run_col3 = st.columns([1, 1, 3])
with run_col1:
    bypass = st.checkbox(
        "Bypass cache", value=False,
        help="Force a fresh model run instead of returning a cached "
             "analysis (24h TTL by default).",
    )
with run_col2:
    include_web = st.checkbox(
        "🌐 Add web context", value=False,
        help="Fetch recent Google News headlines for this market + a "
             "Wikipedia summary for the entity in the question. Slower "
             "(adds ~1-3s) but grounds the model in real-world signal "
             "instead of just on-venue data. Logs forecasts under the "
             "same llm_deep:<model> source — no separate Brier track "
             "yet, so on/off shows up in the calibration log together.",
    )
with run_col3:
    if st.button("🧠 Run deep analysis", type="primary"):
        st.session_state["_da_run"] = True

if st.session_state.get("_da_run"):
    with st.spinner(
        f"Analysing **{ticker}** with **{chosen_model}**… "
        "This can take several minutes."
    ):
        provider = mp.get_provider()
        analysis = lda.analyze_market(
            provider=provider, df_markets=df_markets,
            ticker=ticker, model=chosen_model,
            bypass_cache=bool(bypass),
            include_web_context=bool(include_web),
        )
    st.session_state["_da_run"] = False
    st.session_state["_da_result"] = analysis
    if analysis is None:
        st.session_state["_da_last_error"] = lda.last_error()
    else:
        st.session_state["_da_last_error"] = ""

analysis: lda.DeepAnalysis | None = st.session_state.get("_da_result")
last_err = st.session_state.get("_da_last_error", "")

if analysis is None:
    if last_err:
        st.error(
            f"**Analysis failed.** Most common cause: the selected model "
            f"doesn't support `format=json` chat mode. Try a general-purpose "
            f"text model (llama3.1, qwen2.5, mistral, gemma3).\n\n"
            f"**Server response:** `{last_err}`"
        )
    else:
        st.caption(
            "Pick a market + model and click 🧠 Run deep analysis. Results "
            "stay in session until the next run; cached on disk for 24h "
            "keyed on (ticker, model, context-hash)."
        )
    st.stop()

# ──────────────────────────────────────────────────────────────────────────
# Render
# ──────────────────────────────────────────────────────────────────────────

if analysis.market_id != ticker:
    st.info(
        f"Showing cached result for `{analysis.market_id}`. Run again "
        f"to refresh for `{ticker}`."
    )

market_yes_price = None
prices = row.get("outcome_prices") or []
if prices:
    market_yes_price = float(prices[0])

m1, m2, m3, m4 = st.columns(4)
m1.metric(
    "Model probability (YES)", f"{analysis.probability:.1%}",
    delta=(f"{(analysis.probability - market_yes_price)*100:+.1f}pp vs market"
           if market_yes_price is not None else None),
)
m2.metric(
    "Market YES", f"{market_yes_price:.1%}" if market_yes_price is not None else "—",
)
m3.metric("Confidence", analysis.confidence.upper())
m4.metric("Recommended action", analysis.recommended_action.replace("_", " "))

st.divider()

st.subheader("Reasoning")
st.write(analysis.reasoning or "*(no reasoning text returned)*")

cc1, cc2 = st.columns(2)
with cc1:
    st.markdown("**Key drivers**")
    if analysis.key_drivers:
        for d in analysis.key_drivers:
            st.markdown(f"- {d}")
    else:
        st.caption("(none)")
with cc2:
    st.markdown("**Key risks**")
    if analysis.key_risks:
        for r in analysis.key_risks:
            st.markdown(f"- {r}")
    else:
        st.caption("(none)")

st.markdown("**Evidence the model claims it used**")
if analysis.evidence_used:
    for e in analysis.evidence_used:
        st.markdown(f"- {e}")
else:
    st.caption("(none — treat this analysis as ungrounded)")

with st.expander("📋 Raw context the model saw", expanded=False):
    st.code(analysis.context_summary or "(not stored in cache — bypass cache to rebuild)",
            language="text")

st.divider()

# ──────────────────────────────────────────────────────────────────────────
# Save to calibration
# ──────────────────────────────────────────────────────────────────────────

st.subheader("Log this forecast")
st.caption(
    "Save to the calibration log so this source's track record can be "
    "scored once the market resolves. Tagged as `llm_deep:<model>` so "
    "it stays separate from fast `llm:<model>` forecasts."
)

if market_yes_price is None:
    st.warning("Can't save — no market price available for this row.")
else:
    save_col1, save_col2 = st.columns([1, 4])
    with save_col1:
        save_outcome = st.selectbox(
            "Outcome", options=["Yes", "No"], index=0,
            help="Save your forecast on YES or NO — Brier scoring will "
                 "use the correct side when the market resolves.",
        )
    with save_col2:
        if st.button("💾 Save forecast"):
            your_p = (analysis.probability if save_outcome == "Yes"
                      else 1.0 - analysis.probability)
            price = (market_yes_price if save_outcome == "Yes"
                     else 1.0 - market_yes_price)
            cal.add_forecast(
                question=str(row["question"]),
                condition_id=ticker,
                outcome=save_outcome,
                market_price=float(price),
                your_probability=float(your_p),
                source=f"llm_deep:{analysis.model}",
                notes=(analysis.reasoning or "")[:500],
            )
            st.toast(
                f"Saved forecast: {save_outcome} @ {your_p:.1%} "
                f"(source `llm_deep:{analysis.model}`)",
                icon="✅",
            )
