"""Backtest history page — browse and reload previously-saved runs.

Every run from the 🧪 Backtest page is auto-persisted via
``backtest_history.save_run``. This page lists them, sorted newest-
first, with per-row actions to load (rehydrating the Backtest page's
session-state) or delete.

Storage stats are surfaced in Settings; the destructive "clear all"
button lives there to keep this page focused on per-run inspection.
"""

from __future__ import annotations

import streamlit as st

from pm.research import backtest_history as bh

st.title("Backtest history")
st.caption(
    "Every backtest you run is auto-saved here. Reload one to inspect "
    "it on the 🧪 Backtest page without re-running the API calls."
)

df = bh.list_runs()

if df.empty:
    st.info(
        "No saved runs yet. Run a backtest on the 🧪 Backtest page — it "
        "will be persisted automatically and show up here."
    )
    st.stop()

m1, m2, m3 = st.columns(3)
m1.metric("Saved runs", len(df))
m2.metric(
    "Newest",
    df["saved_at_dt"].max().strftime("%Y-%m-%d %H:%M") if df["saved_at_dt"].notna().any() else "—",
)
m3.metric(
    "Oldest",
    df["saved_at_dt"].min().strftime("%Y-%m-%d %H:%M") if df["saved_at_dt"].notna().any() else "—",
)

st.divider()

# Display table (drop the raw path + dt-helper columns from the visible cut)
display_cols = [
    "saved_at", "strategy_name", "venue", "eval_mode",
    "n_trades", "hit_rate", "roi", "brier_score",
    "total_pnl", "max_drawdown",
]
st.dataframe(
    df[display_cols].style.format({
        "hit_rate": lambda v: f"{v*100:.1f}%" if v is not None and v == v else "—",
        "roi": lambda v: f"{v*100:+.1f}%" if v is not None and v == v else "—",
        "brier_score": lambda v: f"{v:.4f}" if v is not None and v == v else "—",
        "total_pnl": lambda v: f"${v:,.2f}",
        "max_drawdown": lambda v: f"${v:,.2f}",
    }),
    width="stretch",
    hide_index=True,
)

st.divider()
st.subheader("Load or delete a run")

# Pick a run by run_id (with a human-readable label)
def _label(row) -> str:
    saved = row["saved_at"][:19] if isinstance(row["saved_at"], str) else "?"
    return (
        f"{saved} · {row['strategy_name']} · {row['venue']} · "
        f"{row['eval_mode']} · {int(row['n_trades'])} trades"
    )

labels = {row["run_id"]: _label(row) for _, row in df.iterrows()}
chosen = st.selectbox(
    "Run",
    options=list(labels.keys()),
    format_func=lambda r: labels[r],
)

col_a, col_b = st.columns([1, 1])
with col_a:
    if st.button("📂 Load into Backtest page", type="primary", width="stretch"):
        loaded = bh.load_run(chosen)
        if loaded is None:
            st.error("Failed to load that run — file may be corrupted.")
        else:
            result, meta = loaded
            st.session_state["last_backtest"] = result
            # Baseline Brier isn't stored on BacktestResult — it's computed
            # against the dataset, which we don't persist. Clear it so the
            # Backtest page doesn't show a stale baseline alongside a
            # reloaded result.
            st.session_state.pop("last_baseline", None)
            st.success(
                f"Loaded `{result.strategy_name}`. Open 🧪 Backtest to inspect."
            )
with col_b:
    if st.button("🗑️ Delete this run", width="stretch"):
        if bh.delete_run(chosen):
            st.success("Deleted.")
            st.rerun()
        else:
            st.error("Couldn't delete — file may already be gone.")

# Show stored params for the selected run
loaded = bh.load_run(chosen)
if loaded is not None:
    _, meta = loaded
    with st.expander("📋 Run params"):
        st.json(meta.get("params", {}))
