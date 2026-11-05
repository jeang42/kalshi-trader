"""🎯 Recommendations — unified view across every Tier-4 edge source.

One scan = every registered source run against the same time-scoped
market universe. Edges are stacked, sorted by absolute gap, and
labelled with the source's calibration Brier so the user knows which
signals have a track record. Designed as the single page to look at
before considering a real-money trade.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import edges
from pm.ledger import calibration as cal
from pm.ui.shared import load_markets, load_markets_closing_soon, venue_display

st.title("🎯 Recommendations")
st.caption(
    "Every registered edge source, run against the same market set, "
    "ranked by gap size. Advisory only — promotion to live execution "
    "still requires a passing Backtest gate for each source."
)

# ──────────────────────────────────────────────────────────────────────────
# Controls
# ──────────────────────────────────────────────────────────────────────────

c1, c2 = st.columns([2, 2])
with c1:
    min_gap = st.slider(
        "Min |gap|", min_value=0.05, max_value=0.50, value=0.10, step=0.01,
        format="%.2f",
        help="Floor on |model − market| across every source. Below ~10pp, "
             "transaction costs likely eat any apparent edge.",
    )
with c2:
    all_sources = [k for k, _ in edges.list_sources()]
    enabled = st.multiselect(
        "Sources to run",
        options=all_sources,
        default=all_sources,
        format_func=lambda k: dict(edges.list_sources())[k],
        help="Defaults to everything. Disable LLM / Manifold for fast scans "
             "(those are the slow sources).",
    )

t1, t2 = st.columns(2)
with t1:
    min_days = st.number_input(
        "Min days to resolution",
        min_value=0, max_value=3650, value=1, step=1,
        help="Skip markets resolving sooner than this.",
    )
with t2:
    max_days = st.number_input(
        "Max days to resolution",
        min_value=0, max_value=3650, value=30, step=1,
        help="Skip long-dated markets. Default 30d for initial testing.",
    )

# ──────────────────────────────────────────────────────────────────────────
# Calibration badges per source
# ──────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _brier_by_source() -> dict[str, dict]:
    """Per-source Brier from the calibration log, aggregated across
    every profile. The agentic_* runners are where most of the
    accumulated evidence lives; a user on `jeff` who has never
    personally logged a forecast should still see those tracks. The
    `Track record` badge is meant to reflect how a *source* has
    performed historically, not how an individual user has.
    """
    df = cal.get_forecasts_all_profiles()
    if df.empty:
        return {}
    return cal.brier_by_source(df)


brier = _brier_by_source()


def _brier_badge(source_key: str) -> str:
    """Human-readable badge for a source's track record.

    Calibration source-tags arrive in several shapes:
      - ``llm:llama3.1:latest`` (logged from the Edges page)
      - ``crypto_spot`` (bare key)
      - ``agentic:llm_forecast`` (from strategy_runner.py — strategy
        name embedded in the suffix)

    We substring-match the registry source key against each tag so all
    three roll up together. Aggregation is sample-size-weighted.
    """
    key_l = source_key.lower()
    total_n = 0
    weighted_sum = 0.0
    for src, info in brier.items():
        if key_l not in src.lower():
            continue
        n = int(info.get("n") or 0)
        b = info.get("brier")
        if n > 0 and b is not None:
            total_n += n
            weighted_sum += b * n
    if total_n == 0:
        return "untested"
    avg = weighted_sum / total_n
    return f"Brier {avg:.3f} · n={total_n}"


# ──────────────────────────────────────────────────────────────────────────
# Market universe
# ──────────────────────────────────────────────────────────────────────────

with st.spinner(f"Loading markets from {venue_display()}…"):
    # When the user picks a short window (≤90d), use the time-filtered
    # fetch path so short-dated markets aren't buried by long-dated
    # ones in the venue's volume ordering. The default volume-ordered
    # fetch only returns ~1 market with ≤30d on Kalshi.
    if 0 < max_days <= 90:
        df_markets = load_markets_closing_soon(float(max_days), limit=500)
    else:
        df_markets = load_markets()

if df_markets.empty:
    st.warning("Could not load markets.")
    st.stop()

df_scoped = df_markets
if "days_to_resolution" in df_scoped.columns:
    if min_days > 0:
        df_scoped = df_scoped[df_scoped["days_to_resolution"].fillna(-1) >= min_days]
    if max_days > 0:
        df_scoped = df_scoped[df_scoped["days_to_resolution"].fillna(1e9) <= max_days]

st.caption(
    f"**{len(df_scoped):,}** markets in the {min_days}–{max_days or '∞'} day "
    f"window (of {len(df_markets):,} active). Sources to run: "
    f"**{len(enabled)}** — " + ", ".join(enabled) if enabled else "none"
)

# ──────────────────────────────────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────────────────────────────────

if not enabled:
    st.warning("Pick at least one source.")
    st.stop()

run = st.button("🔍 Scan all sources", type="primary")

if run:
    progress = st.progress(0.0, text="Starting…")
    n_total = len(enabled)
    done = {"i": 0}

    def _start(key: str) -> None:
        progress.progress(done["i"] / n_total, text=f"Scanning {key}…")

    def _done(key: str, n_edges: int) -> None:
        done["i"] += 1
        progress.progress(done["i"] / n_total,
                          text=f"{key}: {n_edges} edges ({done['i']}/{n_total})")

    with st.spinner("Running every source…"):
        by_source = edges.find_all_edges(
            df_scoped,
            sources=enabled,
            min_gap=float(min_gap),
            min_days_to_expiry=float(min_days),
            on_source_start=_start,
            on_source_done=_done,
        )
    progress.empty()
    st.session_state["last_recs"] = by_source

by_source: dict[str, list[edges.Edge]] | None = st.session_state.get("last_recs")
if by_source is None:
    st.caption(
        "Pick sources + a time window and click 🔍 Scan all sources. "
        "Results stay in session until the next scan."
    )
    st.stop()

# ──────────────────────────────────────────────────────────────────────────
# Per-source summary
# ──────────────────────────────────────────────────────────────────────────
st.divider()
st.subheader("Per-source summary")

summary_rows = []
for key, edge_list in by_source.items():
    summary_rows.append({
        "Source": dict(edges.list_sources()).get(key, key),
        "Edges": len(edge_list),
        "Track record": _brier_badge(key),
    })
summary_df = pd.DataFrame(summary_rows)
st.dataframe(summary_df, width="stretch", hide_index=True)

# ──────────────────────────────────────────────────────────────────────────
# Unified ranked list
# ──────────────────────────────────────────────────────────────────────────
st.divider()
st.subheader("All edges, ranked by |gap|")

all_edges: list[edges.Edge] = []
for edge_list in by_source.values():
    all_edges.extend(edge_list)

if not all_edges:
    st.info(
        "No edges found across the selected sources in this time window. "
        "Try lowering Min |gap| or widening the days window."
    )
    st.stop()

all_edges.sort(key=edges.edges_severity, reverse=True)

# Convert to rendering-friendly frame
rows = []
for e in all_edges:
    rows.append({
        "Source": e.source,
        "Track record": _brier_badge(e.source.split(":")[0]),
        "Question": e.question,
        "Category": e.category,
        "Market YES": e.market_yes_price,
        "Model implied": e.model_implied,
        "Gap (pp)": e.gap * 100,
        "Direction": e.direction,
        "Note": e.note,
        "Market ID": e.market_id,
    })
df_rank = pd.DataFrame(rows)
st.dataframe(
    df_rank.style.format({
        "Market YES": "{:.3f}",
        "Model implied": "{:.3f}",
        "Gap (pp)": "{:+.1f}",
    }),
    width="stretch", hide_index=True,
)

st.caption(
    "**How to read this:** positive gap = model thinks YES is more likely "
    "than the market does → direction `YES`. Negative gap = model thinks "
    "YES is less likely → direction `NO`. The Track record column reflects "
    "how that source has scored historically (lower Brier = better-calibrated)."
)

# Export
st.download_button(
    "📥 Download CSV",
    data=df_rank.to_csv(index=False).encode("utf-8"),
    file_name="recommendations.csv",
    mime="text/csv",
)
