"""Analysis helpers for the exit log produced by ``strategy_runner``.

The runner's exit-management phase appends one JSON row per closure
to ``data/<profile>/exits.jsonl``. Each row captures *what we did* —
entry/exit/peak prices, P&L, exit_reason. This module joins that log
with the resolved-markets feed to compute *what we'd have done if we
held to resolution*, so the A/B story (managed vs unmanaged sibling
profile) has both sides quantified.

Public surface:

    load_exits(profile=None) -> pd.DataFrame
        All exit rows for a profile (defaults to active). Sorted oldest
        first.

    exit_reason_summary(profile=None) -> pd.DataFrame
        One row per exit_reason with n / total_pnl / mean_pnl_pct /
        win_rate.

    counterfactual_pnl(df_exits, closed_lookup) -> pd.DataFrame
        For each exit row, add `counterfactual_pnl_dollars` and
        `counterfactual_pnl_pct` columns based on what the underlying
        market actually resolved to. NaN when the market hasn't resolved
        yet (still pending) — caller can filter.

    compare_profiles(managed, unmanaged) -> dict
        Top-level comparison: number of trades, total P&L, win rate,
        counterfactual-vs-actual P&L for the managed side, on the same
        strategy.

`closed_lookup` is `{condition_id: bool}` (resolved_yes). Built by the
caller via `provider.get_closed_markets` so we stay venue-agnostic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


def _exits_path(profile: str | None) -> Path:
    from pm.core import profiles as _profiles
    name = profile if profile is not None else _profiles.active_profile()
    return _profiles.profile_dir(name) / "exits.jsonl"


def load_exits(profile: str | None = None) -> pd.DataFrame:
    """Read every exit row for the given profile.

    Returns an empty DataFrame (with the canonical schema columns)
    when the file is missing or empty. Bad rows are skipped with a
    warning — the log is append-only and a partial write shouldn't
    poison analysis.
    """
    cols = [
        "ts", "profile", "condition_id", "question", "outcome", "shares",
        "entry_price", "exit_price", "peak_price", "drawdown_from_peak_pct",
        "pnl_dollars", "pnl_pct", "exit_reason", "held_days", "strategy",
    ]
    path = _exits_path(profile)
    if not path.exists():
        return pd.DataFrame(columns=cols)
    rows: list[dict] = []
    with open(path) as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning("%s line %d: bad JSON (%s); skipping", path, i, e)
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    # Ensure canonical columns exist; older rows may be missing fields.
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df["ts_dt"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    return df.sort_values("ts_dt").reset_index(drop=True)


def exit_reason_summary(profile: str | None = None) -> pd.DataFrame:
    """Aggregate by ``exit_reason``.

    Columns: exit_reason, n, total_pnl_dollars, mean_pnl_pct,
    win_rate, mean_held_days. ``win_rate`` is the fraction of exits
    in that bucket with pnl_dollars > 0.
    """
    df = load_exits(profile)
    cols = [
        "exit_reason", "n", "total_pnl_dollars", "mean_pnl_pct",
        "win_rate", "mean_held_days",
    ]
    if df.empty:
        return pd.DataFrame(columns=cols)
    grouped = df.groupby("exit_reason", dropna=False).agg(
        n=("exit_reason", "size"),
        total_pnl_dollars=("pnl_dollars", "sum"),
        mean_pnl_pct=("pnl_pct", "mean"),
        win_rate=("pnl_dollars", lambda s: float((s > 0).mean())),
        mean_held_days=("held_days", "mean"),
    ).reset_index()
    return grouped.sort_values("n", ascending=False).reset_index(drop=True)


def counterfactual_pnl(
    df_exits: pd.DataFrame, closed_lookup: dict[str, bool],
) -> pd.DataFrame:
    """For each managed-exit row, compute the P&L we'd have realized
    by holding to resolution instead.

    Resolution close rows (exit_reason starts with "resolution_") are
    passed through unchanged — they ARE the held-to-resolution outcome.

    Adds two columns:
        counterfactual_pnl_dollars  (NaN if market not yet resolved)
        counterfactual_pnl_pct
        exit_alpha_dollars  (actual - counterfactual; positive = mgmt helped)

    Caller responsibilities:
        - Build `closed_lookup` by unioning provider.get_closed_markets()
          across registered providers. Sidecar resolvers (e.g. baseball)
          can be overlaid for markets the public feed hasn't caught up
          on yet.
    """
    if df_exits.empty:
        return df_exits.copy()
    df = df_exits.copy()

    # Match the runner's actual-exit accounting: counterfactual P&L is
    # net of round-trip Kalshi fees. Without this, the comparison is
    # apples (actual NET) vs oranges (counterfactual GROSS) and
    # systematically overstates the cost of managed exits.
    from pm.venue import kalshi_fees

    def _cf_pnl(row: pd.Series) -> tuple[float | None, float | None]:
        cid = row.get("condition_id")
        if cid not in closed_lookup:
            return (None, None)
        resolved_yes = closed_lookup[cid]
        outcome_was_yes = str(row.get("outcome", "")).strip().lower().startswith("y")
        won = outcome_was_yes == resolved_yes
        payoff = 1.0 if won else 0.0
        shares = float(row.get("shares", 0))
        entry = float(row.get("entry_price", 0))
        gross = (payoff - entry) * shares
        fees = kalshi_fees.round_trip_fees(
            entry_price=entry, exit_price=payoff,
            contracts=shares, ticker=str(cid),
        )
        pnl_d = gross - fees
        cost = entry * shares
        pnl_p = pnl_d / cost if cost > 0 else 0.0
        return (pnl_d, pnl_p)

    out_d = []
    out_p = []
    for _, row in df.iterrows():
        d, p = _cf_pnl(row)
        out_d.append(d)
        out_p.append(p)
    df["counterfactual_pnl_dollars"] = out_d
    df["counterfactual_pnl_pct"] = out_p
    # exit_alpha: positive means our managed exit beat the held-to-
    # resolution P&L. Resolution-close rows always have alpha = 0
    # (we DID hold to resolution).
    df["exit_alpha_dollars"] = df.apply(
        lambda r: (
            (r["pnl_dollars"] - r["counterfactual_pnl_dollars"])
            if r["counterfactual_pnl_dollars"] is not None
            else None
        ),
        axis=1,
    )
    return df


def compare_profiles(
    managed_profile: str, unmanaged_profile: str,
    *, closed_lookup: dict[str, bool] | None = None,
) -> dict:
    """Top-level A/B comparison between sibling profiles.

    Both profiles should be running the same strategy (one with
    --disable-mgmt, one with TP/SL on). Returns a dict with totals and
    averages suitable for a metric card in the UI.

    `closed_lookup` is optional — when provided, the managed side's
    counterfactual P&L (vs holding to resolution) is included.
    """
    managed = load_exits(managed_profile)
    unmanaged = load_exits(unmanaged_profile)

    def _totals(df: pd.DataFrame) -> dict:
        if df.empty:
            return {"n_exits": 0, "total_pnl": 0.0, "win_rate": None,
                    "mean_pnl_pct": None, "mean_held_days": None}
        return {
            "n_exits": int(len(df)),
            "total_pnl": float(df["pnl_dollars"].sum()),
            "win_rate": float((df["pnl_dollars"] > 0).mean()),
            "mean_pnl_pct": float(df["pnl_pct"].mean()),
            "mean_held_days": float(df["held_days"].dropna().mean())
                if not df["held_days"].dropna().empty else None,
        }

    out: dict = {
        "managed_profile": managed_profile,
        "unmanaged_profile": unmanaged_profile,
        "managed": _totals(managed),
        "unmanaged": _totals(unmanaged),
    }
    if closed_lookup is not None and not managed.empty:
        cf = counterfactual_pnl(managed, closed_lookup)
        resolved_only = cf[cf["counterfactual_pnl_dollars"].notna()]
        out["managed_counterfactual"] = {
            "n_resolved": int(len(resolved_only)),
            "managed_pnl_on_resolved": float(resolved_only["pnl_dollars"].sum())
                if not resolved_only.empty else 0.0,
            "held_pnl_on_resolved":
                float(resolved_only["counterfactual_pnl_dollars"].sum())
                if not resolved_only.empty else 0.0,
            "exit_alpha":
                float(resolved_only["exit_alpha_dollars"].sum())
                if not resolved_only.empty else 0.0,
        }
    return out
