"""Agentic strategy runner — auto-records a chosen strategy's signals
as forecasts + paper buys into a designated profile, and auto-closes
those positions when the underlying markets resolve.

The goal is to accumulate honest calibration history for "what would
full automation have produced?" without trusting any source enough
to ship it as a real-money recommendation yet. Everything this script
does stays inside the active profile's ``forecasts.json`` /
``portfolio.json`` — no order placement, no money moves.

Two modes mirroring ``book_poller.py``:

    python strategy_runner.py --profile agentic --strategy market_consensus
        One cycle, then exit. Suitable for cron.

    python strategy_runner.py --profile agentic --strategy market_consensus \
        --loop --interval 1800
        Forever, with a sleep between cycles. Suitable for systemd / tmux.

Per cycle:
    1. Switch to the target profile (so ``calibration.add_forecast`` and
       ``portfolio.add_buy`` write to that profile's files).
    2. Resolve any closed-and-not-yet-settled positions in the profile
       — for each open position whose market is now resolved, write a
       sell at $1 or $0 depending on whether the direction matched, and
       call ``calibration.auto_resolve_pending`` so the corresponding
       forecasts get their outcomes filled in.
    3. Pull active markets via the configured provider.
    4. For each market, build a single-point ``MarketSnapshot`` and
       run the chosen strategy.
    5. For each non-None signal whose Kelly-sized stake is below the
       portfolio cap AND the market isn't already in an open position
       for this profile: record a forecast (for Brier scoring) plus a
       paper buy.

Idempotency: a market that already has an open position in this profile
is skipped — the runner won't double-buy on subsequent cycles. The
forecast log is append-only by design (each cycle adds a new forecast
row at the current price), so the calibration data shows the strategy
as it evolves.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

import edges as _edges_register  # noqa: F401 — import for Tier-4 strategy registration
from pm.core import emergency_stop, logctx, profiles, provenance
from pm.ledger import calibration as cal
from pm.ledger import portfolio as pf
from pm.ledger import position_meta as pm
from pm.research import backtest as bt
from pm.research import (
    oscillation_scanner as _osc_register,  # noqa: F401 — registers oscillation_revert + oscillation_momentum
)
from pm.venue import kalshi_fees
from pm.venue import provider as provider_mod

log = logging.getLogger("strategy_runner")

UTC = UTC


@dataclass
class ExitPolicy:
    """Tunable exit rules consulted by ``_manage_open_positions``.

    All thresholds are optional — set to None to disable that rule.
    When ``disable_mgmt`` is True the entire phase is a no-op (used to
    keep a profile on the legacy hold-to-resolution baseline so an
    A/B comparison is possible).

    Multiple rules may fire on the same cycle for one position; the
    first match wins in the precedence order documented on
    ``_evaluate_exit``. Resolution-close always pre-empts these (it
    runs in an earlier phase).
    """

    take_profit_pct: float | None = None
    """Exit when unrealized P&L % ≥ this. 0.50 = +50% gain."""

    stop_loss_pct: float | None = None
    """Exit when unrealized P&L % ≤ this. -0.40 = -40% loss. Magnitude
    matters; sign is enforced negative."""

    take_profit_price: float | None = None
    """Exit when the position-side price reaches this absolute level
    (YES price for YES positions; for NO positions, the NO-side price
    = 1 - YES). 0.90 = exit when ≥90¢."""

    trail_stop_pct: float | None = None
    """Exit when the position-side price has dropped this fraction
    below its running peak since entry. 0.15 = 15% off peak."""

    trail_stop_atr_mult: float | None = None
    """**Volatility-aware trailing stop.** Exit when the peak-to-current
    drawdown exceeds ``K × σ_recent``, where ``σ_recent`` is the
    standard deviation of pct returns over the last
    ``trail_vol_window_hours`` of the position-side price history.

    Quiet markets get a tight trail; jumpy markets get room. Fixes the
    "20% trail eats the entire move on a sleepy political market but
    is too loose on a baseball game" mismatch.

    When both ``trail_stop_pct`` and this are set, the position exits
    on whichever threshold fires first. Setting this requires the
    runner to fetch per-position price history each cycle — one extra
    API call per open position. Keep open-position counts reasonable
    (e.g., <100) or this gets slow.

    Typical: K = 1.5–2.0. K=1 fires on roughly half of normal
    fluctuations; K=2 is mostly meaningful reversals."""

    trail_vol_window_hours: float = 48.0
    """Window for the volatility estimate used by ``trail_stop_atr_mult``.
    48h matches typical position lifetimes; shorten for fast-moving
    markets (baseball, crypto-15-min) or lengthen for slow-moving
    political markets."""

    tp_to_trail_stop_pct: float | None = None
    """**Two-stage take-profit.** When set together with
    ``take_profit_pct`` (and/or ``take_profit_price``), hitting the TP
    threshold *does not* fire a sell. Instead it ARMS the position into
    a tight trailing-stop mode using this fraction off peak. The
    position then exits only when the price gives back this much from
    its peak — letting winners run when momentum continues while
    capping the giveback if momentum reverses.

    0.05 is the typical setting: "once I'm up enough to trigger TP,
    don't sell unless we drop 5% off the high." A market that keeps
    climbing keeps ratcheting the peak; one that reverses gets sold
    on first reversal evidence.

    Without ``take_profit_pct`` or ``take_profit_price`` set, this
    flag has no effect (there's no arming trigger)."""

    time_stop_days: float | None = None
    """Force-exit positions older than this many days regardless of P&L."""

    disable_mgmt: bool = False
    """When True, skip the entire management phase. The default profile
    runs hold-to-resolution; managed siblings flip this off."""


@dataclass
class RunnerStats:
    """Per-cycle counters surfaced in logs."""

    cycle_started: datetime
    markets_scanned: int = 0
    signals_emitted: int = 0
    positions_opened: int = 0
    skipped_existing: int = 0
    skipped_no_edge: int = 0
    skipped_for_cap: int = 0
    skipped_event_cap: int = 0
    resolved_closed: int = 0
    managed_exits: int = 0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    errors: int = 0


def _ensure_active_profile(name: str) -> None:
    """Switch the active profile to ``name``, creating it if needed.

    NOTE: This mutates the shared ``data/.active_profile`` file, which
    the Streamlit UI reads on every page render. ``main()`` wraps the
    run in a save/restore so the user's sidebar selection survives
    cron-fired runner invocations. Don't call this from anywhere else
    without the same wrapper.
    """
    profiles.initialize_profiles()
    available = profiles.list_profiles()
    if name not in available:
        try:
            profiles.create_profile(name)
            log.info("created profile %r", name)
        except ValueError as e:
            log.error("can't use profile %r: %s", name, e)
            raise
    profiles.set_active_profile(name)


def _build_snapshot_for_market(
    row: pd.Series,
    *,
    provider: Any = None,
    history_days: int = 0,
    history_fidelity: int = 60,
) -> bt.MarketSnapshot | None:
    """Synthesize a MarketSnapshot from a single live-markets row.

    By default the snapshot carries a one-row "history" DataFrame
    holding only the current YES price — enough for the stateless
    strategies (market_consensus, llm_forecast, baseball_winprob,
    sports_winprob, crypto_spot_lognormal) that look only at the
    *latest* price.

    When ``provider`` is supplied AND ``history_days > 0``, the
    snapshot's ``prices`` column is populated by calling
    ``provider.get_price_history(condition_id, days, fidelity)``.
    History-aware strategies (``momentum``, ``mean_reversion``,
    ``oscillation_revert``) need this to score correctly. The fetch
    failure path falls back to the one-row history so the cycle
    keeps moving — one slow market doesn't poison the rest.
    """
    outcome_prices = row.get("outcome_prices") or []
    if not outcome_prices or outcome_prices[0] is None:
        return None
    yes_price = float(outcome_prices[0])
    now = datetime.now(UTC)
    df: pd.DataFrame | None = None
    if provider is not None and history_days > 0:
        try:
            cid = str(row.get("condition_id", "") or "")
            if cid:
                fetched = provider.get_price_history(
                    cid, days=history_days, fidelity=history_fidelity,
                )
                if fetched is not None and not fetched.empty:
                    df = fetched
        except Exception as e:
            log.debug("history fetch failed for %s: %s",
                      row.get("condition_id"), e)
    if df is None:
        df = pd.DataFrame({
            "timestamp": [now - pd.Timedelta(hours=1)],
            "price": [yes_price],
        })

    # end_date: parse if present
    end_date = row.get("end_date")
    if isinstance(end_date, str):
        try:
            end_dt: datetime | None = datetime.fromisoformat(
                end_date.replace("Z", "+00:00"),
            )
        except ValueError:
            end_dt = None
    elif isinstance(end_date, datetime):
        end_dt = end_date if end_date.tzinfo else end_date.replace(tzinfo=UTC)
    else:
        end_dt = None

    return bt.MarketSnapshot(
        condition_id=str(row.get("condition_id", "")),
        question=str(row.get("question", "")),
        category=str(row.get("category", "") or ""),
        as_of=now,
        prices=df,
        end_date=end_dt,
    )


def _exits_log_path(profile_name: str) -> Path:
    """Per-profile JSONL of position exits. Lives alongside the other
    profile files, NOT in shared/, so each profile owns its history.
    Append-only — analysis joins this with the calibration log to
    compute counterfactual hold-to-resolution P&L."""
    return profiles.profile_dir(profile_name) / "exits.jsonl"


def _log_exit(
    *, profile_name: str, condition_id: str, question: str, outcome: str,
    shares: float, entry_price: float, exit_price: float,
    peak_price: float, opened_at: str | None, exit_reason: str,
    strategy_key: str,
) -> None:
    """Append one exit row to ``data/<profile>/exits.jsonl``.

    Records BOTH gross P&L (just price math) AND net P&L (after
    Kalshi entry+exit fees per ``kalshi_fees.round_trip_fees``). The
    net is what actually matters for evaluating strategy edge —
    Kalshi's fees are non-linear in price (max near $0.50) so a
    strategy that fires only on extremes pays much less in fees
    than one that fires near coin-flip prices.

    Counterfactual P&L (what would have happened if held to resolution)
    is *not* stored here — it's computed at analysis time by joining
    with the closed-markets feed. Storing only what we know now keeps
    the file append-only and avoids drift if a market re-opens (rare
    but possible on data corrections)."""
    cost = entry_price * shares
    gross_pnl = (exit_price - entry_price) * shares
    fees_paid = kalshi_fees.round_trip_fees(
        entry_price=entry_price, exit_price=exit_price,
        contracts=shares, ticker=condition_id,
    )
    net_pnl = gross_pnl - fees_paid
    pnl_pct = net_pnl / cost if cost > 0 else 0.0
    held_days: float | None = None
    if opened_at:
        try:
            ts = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            held_days = (datetime.now(UTC) - ts).total_seconds() / 86400.0
        except (ValueError, TypeError):
            held_days = None
    drawdown_from_peak = (
        (exit_price - peak_price) / peak_price if peak_price > 0 else 0.0
    )
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "commit": provenance.label(),   # R-18: which code closed it
        "profile": profile_name,
        "condition_id": condition_id,
        "question": question,
        "outcome": outcome,
        "shares": float(shares),
        "entry_price": float(entry_price),
        "exit_price": float(exit_price),
        "peak_price": float(peak_price),
        "drawdown_from_peak_pct": float(drawdown_from_peak),
        "gross_pnl_dollars": float(gross_pnl),
        "fees_paid_dollars": float(fees_paid),
        "pnl_dollars": float(net_pnl),  # NET — primary metric
        "pnl_pct": float(pnl_pct),       # NET as % of cost basis
        "exit_reason": exit_reason,
        "held_days": held_days,
        "strategy": strategy_key,
    }
    path = _exits_log_path(profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def _build_price_map(
    df_markets: pd.DataFrame,
) -> dict[str, dict[str, float | None]]:
    """Build ``{condition_id: {yes_bid, yes_ask, yes_mid}}`` from the
    live markets DataFrame. Used by the exit-management phase to mark
    positions to bid (the honest paper-sell price).

    Missing bid/ask falls back to mid (best_bid+best_ask)/2; if neither
    is present, the entry is empty and the management phase skips that
    position this cycle (logged at debug)."""
    out: dict[str, dict[str, float | None]] = {}
    if df_markets is None or df_markets.empty:
        return out
    for _, row in df_markets.iterrows():
        cid = str(row.get("condition_id", "") or "")
        if not cid:
            continue
        bid = row.get("best_bid")
        ask = row.get("best_ask")
        try:
            bid_f = float(bid) if bid is not None and pd.notna(bid) else None
        except (TypeError, ValueError):
            bid_f = None
        try:
            ask_f = float(ask) if ask is not None and pd.notna(ask) else None
        except (TypeError, ValueError):
            ask_f = None
        mid: float | None = None
        if bid_f is not None and ask_f is not None:
            mid = (bid_f + ask_f) / 2
        elif bid_f is not None:
            mid = bid_f
        elif ask_f is not None:
            mid = ask_f
        out[cid] = {"yes_bid": bid_f, "yes_ask": ask_f, "yes_mid": mid}
    return out


def _position_side_price(
    outcome: str, price_info: dict[str, float | None],
) -> float | None:
    """Paper-sell price for the given side.

    For a YES position we'd sell into the YES bid (someone willing to
    buy YES from us). For a NO position, NO bid = 1 − YES ask. Mid
    fallback only when bid is missing — exits should be honest about
    spread cost.
    """
    is_yes = outcome.strip().lower().startswith("y")
    if is_yes:
        yes_bid = price_info.get("yes_bid")
        if yes_bid is not None:
            return float(yes_bid)
        return price_info.get("yes_mid")
    # NO position: side price is (1 - yes_ask), or (1 - yes_mid) fallback
    ask = price_info.get("yes_ask")
    mid = price_info.get("yes_mid")
    if ask is not None:
        return 1.0 - float(ask)
    if mid is not None:
        return 1.0 - float(mid)
    return None


def _compute_recent_vol(
    provider: Any, ticker: str, *,
    window_hours: float = 48.0, fidelity: int = 60,
) -> float | None:
    """Std-dev of pct returns over the last ``window_hours`` of YES-side
    price history for ``ticker``. Returns None if history is too thin
    to estimate (<5 points) or the fetch fails.

    Used by the volatility-aware trailing stop. The unit is "fractional
    return per bar" — e.g., 0.02 means typical bar-to-bar moves are
    ~2%. Multiply by ``K × peak`` for a price-space threshold.
    """
    if provider is None or not ticker:
        return None
    # Fetch enough days to cover the window plus a buffer
    days = max(2, int(window_hours / 24) + 2)
    try:
        df = provider.get_price_history(ticker, days=days, fidelity=fidelity)
    except Exception as e:
        log.debug("vol history fetch failed for %s: %s", ticker, e)
        return None
    if df is None or df.empty or len(df) < 5:
        return None
    df = df.sort_values("timestamp")
    # Tail the last window_hours worth of bars (assume one bar per
    # ``fidelity`` minutes; window in bars = window_hours * 60 / fidelity)
    n_bars = max(5, int(window_hours * 60 / max(fidelity, 1)))
    df = df.tail(n_bars)
    prices = df["price"].astype(float).to_numpy()
    if len(prices) < 5:
        return None
    # Pct returns. Floor the denominator to avoid div-by-zero on
    # markets that touch $0 briefly.
    import numpy as np
    safe_prev = np.maximum(prices[:-1], 1e-6)
    returns = np.diff(prices) / safe_prev
    sigma = float(np.std(returns))
    return sigma if sigma > 0 else None


def _net_pnl_pct(entry_price: float, exit_price: float, *, ticker: str) -> float:
    """Round-trip net P&L as a fraction of cost basis, after Kalshi
    fees on both legs.

    Stake-independent: the fee_per_contract / cost_per_contract ratio
    is the same at any size. Returns the same number whether you're
    trading 1 share or 1 million.
    """
    if entry_price <= 0:
        return 0.0
    rate = kalshi_fees.fee_rate_for_series(ticker)
    gross_pct = (exit_price - entry_price) / entry_price
    # Entry fee as %-of-cost: 0.07 × (1 - entry)
    entry_fee_pct = rate * (1.0 - entry_price)
    # Exit fee as %-of-cost: 0.07 × exit × (1 - exit) / entry
    exit_fee_pct = rate * exit_price * (1.0 - exit_price) / entry_price
    return gross_pct - entry_fee_pct - exit_fee_pct


def _evaluate_exit(
    *, side_price: float, entry: dict, policy: ExitPolicy, now: datetime,
    ticker: str = "", recent_vol_pct: float | None = None,
) -> str | None:
    """Return the exit-reason string if any rule fires, else None.

    Thresholds are checked against **NET P&L %** (after Kalshi entry +
    exit fees, per the ``kalshi_fees`` formula). This matches the
    trader's intent: "exit at +30%" means "exit at +30% in money I
    actually keep," not "exit at +30% gross." A market that posts a
    gross gain just barely clearing the fee drag won't fire here.

    Precedence (first match wins):
        1. stop_loss_pct  — losses cut first (you can't take profit on a loser)
        2. time_stop_days — old positions exit regardless of P&L
        3. tp_to_trail (when armed) — TIGHT trail kicks in after TP hit
        4. take_profit_pct — gains realized OR arms two-stage mode
        5. take_profit_price — absolute price threshold OR arms
        6. trail_stop_pct — peak-to-current drawdown (loose, pre-arm)

    Two-stage take-profit:
        When ``policy.tp_to_trail_stop_pct`` is set and TP threshold
        hits, this function returns the sentinel ``"arm_tp"`` instead
        of an exit reason. The caller (``_manage_open_positions``)
        flips ``position_meta`` to ``tp_armed=True`` and skips the sell
        for this cycle. Subsequent cycles see ``entry["tp_armed"]`` is
        True and use the tight trail; an instant sell only fires when
        the tight trail's drawdown threshold is exceeded.

    Stop-loss precedes time-stop so a position both deeply underwater
    AND stale exits with the more informative reason. Time-stop
    precedes take-profit so we don't sit on a stale winner waiting
    for one more cent.
    """
    entry_price = float(entry["entry_price"])
    if entry_price <= 0:
        return None
    pnl_pct = _net_pnl_pct(entry_price, side_price, ticker=ticker)
    tp_armed = bool(entry.get("tp_armed", False))

    # 1. Stop-loss (only matters when we're underwater). Applies in
    # both pre-arm and post-arm modes — a position that armed at +35%
    # and then crashed back to -25% should still cut.
    sl = policy.stop_loss_pct
    if sl is not None and pnl_pct <= -abs(sl):
        return "stop_loss_pct"

    # 2. Time stop
    if policy.time_stop_days is not None:
        opened = entry.get("opened_at")
        if opened:
            try:
                ts = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                age_days = (now - ts).total_seconds() / 86400.0
                if age_days >= policy.time_stop_days:
                    return "time_stop"
            except (ValueError, TypeError):
                pass

    # 3. TP-armed positions: TIGHT trailing stop, exit on first
    # meaningful giveback from peak. Lets winners run; caps reversal.
    # ALSO short-circuits the further checks below — once armed, the
    # only path out is this trail (or the SL/time-stop above).
    if tp_armed and policy.tp_to_trail_stop_pct is not None:
        peak = float(entry.get("peak_price") or entry_price)
        if peak > 0:
            drawdown = (peak - side_price) / peak
            if drawdown >= policy.tp_to_trail_stop_pct:
                return "tp_to_trail"
        return None  # armed but not yet reversing — hold

    # 4. Take-profit on net %. When tp_to_trail is configured, this
    # returns the "arm_tp" sentinel instead of an immediate sell — the
    # caller flips position_meta.tp_armed and skips the sell.
    if (
        policy.take_profit_pct is not None
        and pnl_pct >= policy.take_profit_pct
    ):
        if policy.tp_to_trail_stop_pct is not None:
            return "arm_tp"
        return "take_profit_pct"

    # 5. Take-profit on absolute side price (same arming semantics)
    if (
        policy.take_profit_price is not None
        and side_price >= policy.take_profit_price
    ):
        if policy.tp_to_trail_stop_pct is not None:
            return "arm_tp"
        return "take_profit_price"

    # 6. Pre-arm trailing stops. Whichever fires first wins; when both
    # are configured we use the tighter threshold for that cycle.
    if policy.trail_stop_pct is not None or policy.trail_stop_atr_mult is not None:
        peak = float(entry.get("peak_price") or entry_price)
        if peak >= entry_price and peak > 0:
            drawdown_pct = (peak - side_price) / peak
            # Fixed-pct path
            if (
                policy.trail_stop_pct is not None
                and drawdown_pct >= policy.trail_stop_pct
            ):
                return "trail_stop"
            # Vol-aware path. Threshold = K × σ. The position-side
            # drawdown is compared directly to the K-sigma envelope.
            if (
                policy.trail_stop_atr_mult is not None
                and recent_vol_pct is not None
                and recent_vol_pct > 0
            ):
                threshold = policy.trail_stop_atr_mult * recent_vol_pct
                if drawdown_pct >= threshold:
                    return "trail_stop_atr"

    return None


def _manage_open_positions(
    *, profile_name: str, df_markets: pd.DataFrame, policy: ExitPolicy,
    strategy_key: str, provider: Any = None,
) -> tuple[int, dict[str, int]]:
    """Mark every open position to current price, update peaks, and
    fire exits per ``policy``. Returns ``(n_exits, reason_counts)``.

    When ``policy.disable_mgmt`` is True returns ``(0, {})`` without
    touching any state — that's the legacy hold-to-resolution behavior
    the unmanaged sibling profile keeps.

    ``provider`` is used by the volatility-aware trail
    (``trail_stop_atr_mult``) to fetch per-position recent price
    history. When None, vol-aware checks are skipped (the policy's
    fixed-pct trail still applies if configured).
    """
    if policy.disable_mgmt:
        return 0, {}
    df_pos = pf.get_positions()
    if df_pos.empty:
        return 0, {}
    price_map = _build_price_map(df_markets)
    now = datetime.now(UTC)
    reasons: dict[str, int] = {}
    n_exits = 0

    # Per-cycle volatility cache. Only populated when the policy
    # actually needs it — avoids paying for an extra history fetch per
    # position when fixed-pct trails are sufficient.
    needs_vol = policy.trail_stop_atr_mult is not None
    vol_cache: dict[str, float | None] = {}

    def _vol_for(t: str) -> float | None:
        if not needs_vol or provider is None:
            return None
        if t not in vol_cache:
            vol_cache[t] = _compute_recent_vol(
                provider, t, window_hours=policy.trail_vol_window_hours,
            )
        return vol_cache[t]

    for _, pos in df_pos.iterrows():
        cid = str(pos["condition_id"])
        outcome = str(pos["outcome"])
        price_info = price_map.get(cid)
        if not price_info:
            # Not in the live feed this cycle — skip. Could fetch
            # /book individually, but we'd rather wait one cycle than
            # hammer the API. Trailing peak still moves forward when
            # the market reappears.
            continue
        side_price = _position_side_price(outcome, price_info)
        if side_price is None:
            continue

        # Get/initialize the meta row. If somehow missing (e.g. a buy
        # logged before this module existed), backfill from the
        # position row's avg_price + added_at.
        entry = pm.get_entry(cid, outcome)
        if entry is None:
            opened_iso = pos.get("added_at")
            try:
                opened_dt = (
                    datetime.fromisoformat(str(opened_iso).replace("Z", "+00:00"))
                    if opened_iso else now
                )
            except (ValueError, TypeError):
                opened_dt = now
            entry = pm.record_entry(
                cid, outcome,
                entry_price=float(pos["avg_price"]),
                opened_at=opened_dt,
            )

        entry = pm.update_peak(
            cid, outcome, current_price=side_price, now=now,
        ) or entry

        reason = _evaluate_exit(
            side_price=side_price, entry=entry, policy=policy, now=now,
            ticker=cid, recent_vol_pct=_vol_for(cid),
        )
        if reason is None:
            continue
        if reason == "arm_tp":
            # Two-stage TP: flip the meta flag and hold. Subsequent
            # cycles will use the tight trail. Logged at info so the
            # decision is auditable, but it's NOT a managed_exit — the
            # position remains open.
            pm.arm_tp(cid, outcome)
            reasons["arm_tp"] = reasons.get("arm_tp", 0) + 1
            log.info(
                "armed-tp %s/%s entry=%.3f current=%.3f peak=%.3f — "
                "switching to %.0f%% trailing stop",
                cid[:10], outcome, float(entry["entry_price"]),
                float(side_price),
                float(entry.get("peak_price") or entry["entry_price"]),
                100 * float(policy.tp_to_trail_stop_pct or 0),
            )
            continue

        # Execute the paper-sell at side_price
        try:
            pf.add_sell(
                question=str(pos["question"]), condition_id=cid,
                outcome=outcome, shares=float(pos["shares"]),
                price=float(side_price),
            )
            _log_exit(
                profile_name=profile_name, condition_id=cid,
                question=str(pos["question"]), outcome=outcome,
                shares=float(pos["shares"]),
                entry_price=float(entry["entry_price"]),
                exit_price=float(side_price),
                peak_price=float(entry.get("peak_price") or entry["entry_price"]),
                opened_at=entry.get("opened_at"),
                exit_reason=reason, strategy_key=strategy_key,
            )
            pm.clear_entry(cid, outcome)
            # Resolve the forecast at the exit price so calibration
            # has *something* to score against without waiting for
            # resolution. We record this as a synthetic resolution
            # ONLY when the market hasn't actually resolved (the
            # forecast log's resolve API takes a bool — we'd lose
            # information). For now, leave the forecast pending;
            # auto_resolve_pending will fill it in when the market
            # actually settles. This means calibration scoring
            # reflects what the market resolved to, not what we
            # exited at, which is the right denominator anyway.
            n_exits += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            net_pnl_pct = _net_pnl_pct(
                float(entry["entry_price"]), float(side_price), ticker=cid,
            )
            log.info(
                "managed-exit %s/%s side=%s entry=%.3f exit=%.3f peak=%.3f "
                "reason=%s net_pnl=%+.1f%% (gross %+.1f%%)",
                cid[:10], outcome, "Y" if outcome.lower().startswith("y") else "N",
                float(entry["entry_price"]), float(side_price),
                float(entry.get("peak_price") or entry["entry_price"]),
                reason,
                100 * net_pnl_pct,
                100 * (float(side_price) - float(entry["entry_price"]))
                / max(float(entry["entry_price"]), 1e-9),
            )
        except Exception as e:
            log.warning("managed-exit failed for %s/%s: %s", cid, outcome, e)

    return n_exits, reasons


def _sidecar_resolutions(tickers: list[str]) -> dict[str, bool]:
    """Ask every registered sidecar's optional ``resolve_pending_tickers``
    hook to resolve any of ``tickers`` from its own domain data (game/match
    finals known long before the venue settles). Returns the merged
    ``{ticker: yes_won}``. Never raises — a broken sidecar is skipped.

    Mirrors ``auto_resolve_runner.overlay_sidecar_resolutions`` but for the
    paper-position close path, so sport paper P&L resolves on the same fast
    cadence as the forecast Brier."""
    if not tickers:
        return {}
    out: dict[str, bool] = {}
    try:
        from pm.core import sidecars as _sidecars
    except ImportError:
        return out
    for sc in _sidecars.list_sidecars():
        fn = getattr(sc, "resolve_pending_tickers", None)
        if not callable(fn):
            continue
        try:
            extras = fn(tickers)
        except Exception as e:  # noqa: BLE001 — hook should swallow already
            log.warning("sidecar %s.resolve_pending_tickers raised: %s",
                        getattr(sc, "name", "?"), e)
            continue
        if extras:
            out.update({str(k): bool(v) for k, v in extras.items()})
    return out


def _close_resolved_positions(
    *, profile_name: str, strategy_key: str = "",
) -> int:
    """For each open position in this profile whose market is now
    resolved (across every registered provider), write a sell at $1
    (won) or $0 (lost), and auto-resolve the matching forecasts.

    Returns the number of positions closed. Errors per-position are
    logged + counted in the caller's stats. Each closure also writes
    a row to ``exits.jsonl`` with reason ``resolution_won`` or
    ``resolution_lost`` so the same analysis pipeline covers both
    managed exits and natural resolutions, and clears the position
    from ``position_meta``.
    """
    df_pos = pf.get_positions()
    if df_pos.empty:
        return 0

    # Build a {condition_id: resolved_yes} lookup from every venue
    lookup: dict[str, bool] = {}
    for provider in provider_mod.list_providers():
        try:
            df = provider.get_closed_markets(limit=200)
        except Exception as e:
            log.warning("%s: get_closed_markets failed: %s", provider.name, e)
            continue
        if df.empty:
            continue
        for _, row in df.iterrows():
            cid = row.get("condition_id")
            ry = row.get("resolved_yes")
            if cid and ry is not None and pd.notna(ry):
                lookup[str(cid)] = bool(ry)

    # Overlay sidecar fast-resolution for this profile's open tickers. The
    # provider feed is the venue's settlement (e.g. Kalshi's ~1-4 day lag);
    # sidecars (baseball/wnba/tennis) know the game/match result within
    # minutes, so for sport tickers this closes paper positions promptly
    # instead of waiting on settlement — same hook auto_resolve_runner uses
    # for forecasts. Sidecar results overlay (and agree with) the venue feed.
    open_tickers = [str(c) for c in df_pos["condition_id"].astype(str).tolist()]
    lookup.update(_sidecar_resolutions(open_tickers))

    n_closed = 0
    for _, pos in df_pos.iterrows():
        cid = str(pos["condition_id"])
        if cid not in lookup:
            continue
        resolved_yes = lookup[cid]
        outcome_was_yes = str(pos["outcome"]).strip().lower() == "yes"
        won = outcome_was_yes == resolved_yes
        close_price = 1.0 if won else 0.0
        try:
            pf.add_sell(
                question=str(pos["question"]), condition_id=cid,
                outcome=str(pos["outcome"]),
                shares=float(pos["shares"]), price=close_price,
            )
            n_closed += 1
            # Also record in exits.jsonl + clear meta so the analysis
            # pipeline sees the same shape for resolution-close as for
            # managed exits. Falls back to avg_price when no meta row
            # exists (positions created before this code shipped).
            entry = pm.get_entry(cid, str(pos["outcome"])) or {
                "entry_price": float(pos.get("avg_price") or close_price),
                "opened_at": pos.get("added_at"),
                "peak_price": float(pos.get("avg_price") or close_price),
            }
            _log_exit(
                profile_name=profile_name, condition_id=cid,
                question=str(pos["question"]), outcome=str(pos["outcome"]),
                shares=float(pos["shares"]),
                entry_price=float(entry["entry_price"]),
                exit_price=float(close_price),
                peak_price=float(entry.get("peak_price") or entry["entry_price"]),
                opened_at=entry.get("opened_at"),
                exit_reason="resolution_won" if won else "resolution_lost",
                strategy_key=strategy_key,
            )
            pm.clear_entry(cid, str(pos["outcome"]))
            log.info("closed %s/%s at $%.2f (%s)",
                     cid[:10], pos["outcome"], close_price,
                     "WON" if won else "lost")
        except Exception as e:
            log.warning("close failed for %s: %s", cid, e)

    # Also auto-resolve forecasts so calibration stays in sync
    if lookup:
        try:
            updated = cal.auto_resolve_pending(lookup)
            if updated:
                log.info("auto-resolved %d forecast(s)", len(updated))
        except Exception as e:
            log.warning("calibration auto-resolve failed: %s", e)

    return n_closed


def _event_ticker(condition_id: str) -> str:
    """Extract the event-level prefix from a Kalshi ticker.

    ``KXMLBDRAFTTOP-26-5-ZROS`` → ``KXMLBDRAFTTOP-26-5``
    ``KXMLBGAME-26MAY241340STLCIN-STL`` → ``KXMLBGAME-26MAY241340STLCIN``

    For non-Kalshi condition_ids (hex hashes), returns the id unchanged
    (no event grouping, which is the safe default).
    """
    return condition_id.rsplit("-", 1)[0] if "-" in condition_id else condition_id


def _event_position_counts(open_cids: set[str]) -> dict[str, int]:
    """Count open positions per event ticker."""
    counts: dict[str, int] = {}
    for cid in open_cids:
        ev = _event_ticker(cid)
        counts[ev] = counts.get(ev, 0) + 1
    return counts


def _deployed_cost_basis(profile_name: str) -> float:
    """Total cost basis of open positions in this profile."""
    try:
        from pm.ledger import portfolio as pf
        pos = pf.get_positions()
        if pos.empty:
            return 0.0
        return float(pos["cost_basis"].sum())
    except Exception:
        return 0.0


def _open_position_set(profile_name: str) -> set[str]:
    """condition_ids that already have an open position in this profile.
    Used by the runner to avoid re-buying on every cycle."""
    df = pf.get_positions()
    if df.empty:
        return set()
    return set(df["condition_id"].astype(str))


def _pin_profile_env(profile_name: str) -> dict[str, str]:
    """Pin POLYMARKET_PORTFOLIO_FILE / _FORECASTS_FILE / _ALERTS_FILE
    to ``profile_name``'s paths via env vars and return the old values.

    Why: the lazy-path pattern in portfolio.py / calibration.py / etc.
    falls back to ``profiles.active_profile()`` (a globally shared file
    at ``data/.active_profile``) when no env override is present. Three
    cron-fired strategy runners overlap in time; if runner A sets the
    active profile, runner B's writes can land in A's profile.

    Setting the env vars explicitly inside ``run_cycle`` makes the
    storage paths *process-local* — each runner's writes go to its
    own profile regardless of what other processes do to the shared
    active-profile file.
    """
    old = {}
    for kind, env_key in [
        ("portfolio", "POLYMARKET_PORTFOLIO_FILE"),
        ("forecasts", "POLYMARKET_FORECASTS_FILE"),
        ("alerts", "POLYMARKET_ALERTS_FILE"),
    ]:
        # Respect any pre-existing override (tests set these via
        # fixtures; we don't want to clobber them). Only pin when
        # the caller hasn't already pointed the storage somewhere.
        existing = os.environ.get(env_key, "")
        if existing:
            old[env_key] = existing
            continue
        old[env_key] = ""
        os.environ[env_key] = str(profiles.file_path(kind, profile_name))
    return old


def _restore_profile_env(old: dict[str, str]) -> None:
    """Restore env vars saved by ``_pin_profile_env``."""
    for env_key, val in old.items():
        if val:
            os.environ[env_key] = val
        else:
            os.environ.pop(env_key, None)


def run_cycle(
    *, strategy_key: str, bankroll: float, kelly_multiplier: float,
    half_spread: float, max_position_pct: float, min_gap: float,
    max_markets: int, profile_name: str,
    strategy_kwargs: dict[str, Any] | None = None,
    closing_within_days: float | None = None,
    exit_policy: ExitPolicy | None = None,
    history_days: int = 0,
    history_fidelity: int = 60,
    max_positions_per_event: int | None = None,
    markets_df: pd.DataFrame | None = None,
) -> RunnerStats:
    """One polling cycle: close resolved positions, manage open positions
    per ``exit_policy``, then run the strategy over active markets and
    open new positions for flagged signals.

    ``markets_df`` (optional): pre-discovered markets to scan instead of
    fetching via the provider. Needed for low-volume sports (e.g. WNBA)
    whose tickers never crack the volume-ordered ``get_markets_closing_
    within`` feed — the caller discovers them via the events path and
    injects the normalized DataFrame here.

    Pins profile-aware storage paths to ``profile_name``'s files via
    env vars for the duration of the cycle. This is process-local —
    immune to concurrent cron runs racing on the shared active-profile
    file.

    ``closing_within_days`` is an optional fetch override. When set, the
    runner uses ``provider.get_markets_closing_within(closing_within_days)``
    instead of the default volume-sorted feed — needed for short-dated
    market scans (sports game-day winners) that don't crack the top-N
    by volume.

    ``exit_policy`` defaults to a disabled policy (``disable_mgmt=True``)
    so legacy callers keep the prior hold-to-resolution behavior. Pass
    an ExitPolicy with thresholds set to enable TP/SL/trailing/time
    exits.

    ``max_positions_per_event`` caps open positions per event ticker
    (e.g., ``KXMLBDRAFTTOP-26-5``). Prevents buying YES on 10 of 30
    candidates in an N-choose-K market. ``None`` = no limit.
    """
    # Emergency-stop gate: if the operator has flipped the kill switch,
    # short-circuit BEFORE any provider calls or profile pinning. The
    # resolve-phase (which only records sells for already-settled
    # markets) is also skipped — it's bookkeeping, but the spirit of
    # "everything stops" is more important than the marginal lag.
    # `auto_resolve_runner` (the dedicated cron job) will catch up
    # when the stop is lifted.
    if emergency_stop.is_active():
        st = emergency_stop.get_status() or {}
        log.warning(
            "EMERGENCY STOP active — cycle short-circuited "
            "(since=%s reason=%s)",
            st.get("since"), st.get("reason") or "(none)",
        )
        stats = RunnerStats(cycle_started=datetime.now(UTC))
        return stats

    saved_env = _pin_profile_env(profile_name)
    try:
        return _run_cycle_inner(
            strategy_key=strategy_key, bankroll=bankroll,
            kelly_multiplier=kelly_multiplier, half_spread=half_spread,
            max_position_pct=max_position_pct, min_gap=min_gap,
            max_markets=max_markets, profile_name=profile_name,
            strategy_kwargs=strategy_kwargs,
            closing_within_days=closing_within_days,
            exit_policy=exit_policy or ExitPolicy(disable_mgmt=True),
            history_days=history_days,
            history_fidelity=history_fidelity,
            max_positions_per_event=max_positions_per_event,
            markets_df=markets_df,
        )
    finally:
        _restore_profile_env(saved_env)


def _run_cycle_inner(
    *, strategy_key: str, bankroll: float, kelly_multiplier: float,
    half_spread: float, max_position_pct: float, min_gap: float,
    max_markets: int, profile_name: str,
    strategy_kwargs: dict[str, Any] | None,
    closing_within_days: float | None = None,
    exit_policy: ExitPolicy,
    history_days: int = 0,
    history_fidelity: int = 60,
    max_positions_per_event: int | None = None,
    markets_df: pd.DataFrame | None = None,
) -> RunnerStats:
    """Inner implementation of run_cycle — assumes _pin_profile_env has
    already set the env vars."""
    stats = RunnerStats(cycle_started=datetime.now(UTC))

    # 1. Settle anything that resolved since last cycle
    try:
        stats.resolved_closed = _close_resolved_positions(
            profile_name=profile_name, strategy_key=strategy_key,
        )
    except Exception as e:
        log.error("close-resolved-positions phase failed: %s", e)
        stats.errors += 1

    # 2. Fetch active markets (or use the caller-injected set)
    provider = provider_mod.get_provider()
    try:
        if markets_df is not None:
            df_markets = markets_df
            log.info("using %d caller-injected markets", len(df_markets))
        elif closing_within_days is not None:
            df_markets = provider.get_markets_closing_within(
                float(closing_within_days), limit=max_markets,
            )
            log.info("fetched %d markets closing within %.1f days",
                     len(df_markets), closing_within_days)
        else:
            df_markets = provider.get_markets(limit=max_markets, active=True)
    except Exception as e:
        log.error("get_markets failed: %s", e)
        stats.errors += 1
        return stats
    if df_markets is None or df_markets.empty:
        log.info("0 active markets returned")
        return stats
    stats.markets_scanned = len(df_markets)

    # 2b. Manage open positions — peaks, TP, SL, trail, time stops.
    # No-op when policy.disable_mgmt is True (default for legacy
    # callers; the hold-to-resolution baseline profiles keep this off).
    try:
        n_exits, reasons = _manage_open_positions(
            profile_name=profile_name, df_markets=df_markets,
            policy=exit_policy, strategy_key=strategy_key,
            provider=provider,
        )
        stats.managed_exits = n_exits
        stats.exit_reasons = reasons
    except Exception as e:
        log.error("manage-open-positions phase failed: %s", e)
        stats.errors += 1

    # 3. Run strategy against each market
    try:
        strategy_fn = bt.get_strategy(strategy_key)
    except KeyError:
        log.error("unknown strategy %r — known: %s",
                  strategy_key, [k for k, _ in bt.list_strategies()])
        stats.errors += 1
        return stats
    # Bind any --strategy-arg kwargs via partial so every per-market
    # call sees them. Missing keys are passed through to the strategy
    # which will raise TypeError on unknown — we surface that as an
    # error rather than silently dropping the args.
    strategy = (
        functools.partial(strategy_fn, **strategy_kwargs)
        if strategy_kwargs else strategy_fn
    )

    already_open = _open_position_set(profile_name)
    event_counts = (
        _event_position_counts(already_open)
        if max_positions_per_event is not None else {}
    )
    max_position_dollars = bankroll * max_position_pct

    # Cash tracking: compute how much bankroll is already deployed in
    # open positions. New positions can only use remaining cash.
    deployed = _deployed_cost_basis(profile_name)
    remaining_cash = max(0.0, bankroll - deployed)
    if deployed > 0:
        log.info("bankroll=$%.0f deployed=$%.0f remaining=$%.0f",
                 bankroll, deployed, remaining_cash)

    for _, row in df_markets.iterrows():
        snap = _build_snapshot_for_market(
            row,
            provider=provider if history_days > 0 else None,
            history_days=history_days,
            history_fidelity=history_fidelity,
        )
        if snap is None:
            continue
        cid = snap.condition_id
        if cid in already_open:
            stats.skipped_existing += 1
            continue
        if max_positions_per_event is not None:
            ev = _event_ticker(cid)
            if event_counts.get(ev, 0) >= max_positions_per_event:
                stats.skipped_event_cap += 1
                continue

        try:
            signal = strategy(snap)
        except Exception as e:
            log.warning("strategy %s threw on %s: %s",
                        strategy_key, cid, e)
            stats.errors += 1
            continue
        if signal is None:
            continue
        stats.signals_emitted += 1

        market_yes = snap.latest_price
        if market_yes is None:
            continue

        # Gap check — same idea as the Edges page's min_gap. Without
        # this, a strategy that fires constantly would fill every
        # market with sub-cent edges.
        if signal.direction == "YES":
            gap = signal.probability - market_yes
        else:
            gap = (1.0 - signal.probability) - (1.0 - market_yes)
        if gap < min_gap:
            stats.skipped_no_edge += 1
            continue

        desired_stake = bt._size_trade(
            signal, market_yes, bankroll, kelly_multiplier, half_spread,
        )
        if desired_stake <= 0:
            stats.skipped_no_edge += 1
            continue

        # Per-market position cap + portfolio cash constraint.
        stake = min(desired_stake, max_position_dollars, remaining_cash)
        if stake <= 0.50:
            stats.skipped_for_cap += 1
            continue
        eff_entry = bt._effective_entry_price(
            signal.direction, market_yes, half_spread,
        )
        shares = stake / eff_entry
        outcome = "Yes" if signal.direction == "YES" else "No"
        market_price_on_side = (
            market_yes if signal.direction == "YES" else 1.0 - market_yes
        )

        try:
            cal.add_forecast(
                question=snap.question, condition_id=cid, outcome=outcome,
                market_price=market_price_on_side,
                your_probability=(
                    signal.probability if signal.direction == "YES"
                    else 1.0 - signal.probability
                ),
                source=f"agentic:{strategy_key}",
                notes=f"runner cycle {stats.cycle_started.isoformat()}",
            )
            pf.add_buy(
                question=snap.question, condition_id=cid, outcome=outcome,
                shares=shares, price=eff_entry,
            )
            # Track entry metadata for the exit-management phase. Done
            # AFTER add_buy so a portfolio-write failure doesn't leave
            # a stale meta entry pointing at nothing.
            try:
                pm.record_entry(cid, outcome, entry_price=float(eff_entry))
            except Exception as e:
                log.warning("position_meta record_entry failed for %s/%s: %s",
                            cid, outcome, e)
            stats.positions_opened += 1
            remaining_cash -= stake
            if max_positions_per_event is not None:
                ev = _event_ticker(cid)
                event_counts[ev] = event_counts.get(ev, 0) + 1
            log.info(
                "opened %s on %s at %.3f (stake $%.2f, gap %+.1fpp, cash=$%.0f)",
                outcome, cid[:10], eff_entry, stake, gap * 100, remaining_cash,
            )
        except Exception as e:
            log.warning("logging trade for %s failed: %s", cid, e)
            stats.errors += 1

    return stats


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Auto-record strategy signals as forecasts + paper "
                    "buys in a designated profile.",
    )
    p.add_argument("--profile", default="agentic",
                   help="Profile to write forecasts + portfolio to "
                        "(default: agentic).")
    p.add_argument("--strategy", required=True,
                   help="Strategy key from backtest.STRATEGIES. "
                        "Try `--strategy market_consensus` to start.")
    p.add_argument("--bankroll", type=float, default=1000.0,
                   help="Notional bankroll used by Kelly sizing (default $1000).")
    p.add_argument("--kelly-multiplier", type=float, default=0.5,
                   help="Multiplier on Kelly fraction (default 0.5 = half-Kelly).")
    p.add_argument("--half-spread", type=float, default=0.005,
                   help="Assumed half-spread per side (default 0.005 = ½¢).")
    p.add_argument("--max-position-pct", type=float, default=0.10,
                   help="Cap each new position at this fraction of bankroll "
                        "(default 0.10 = 10%%).")
    p.add_argument("--min-gap", type=float, default=0.05,
                   help="Skip signals whose edge (probability − market price "
                        "on chosen side) is below this (default 0.05 = 5pp).")
    p.add_argument("--max-markets", type=int, default=50,
                   help="Active markets to scan per cycle (default 50).")
    p.add_argument("--max-positions-per-event", type=int, default=None,
                   help="Cap open positions per event ticker. Prevents "
                        "buying YES on N of M candidates in an N-choose-K "
                        "market (e.g. 'top 5 draft pick' with 30 players). "
                        "None = no limit (default).")
    p.add_argument("--closing-within-days", type=float, default=None,
                   help="If set, fetch markets via get_markets_closing_within() "
                        "instead of the default volume-sorted feed. Use for "
                        "strategies that target short-dated markets (e.g. sports "
                        "game-day winners) which don't crack the top-N by volume. "
                        "Example: 1.5 for 'closing within ~36 hours'.")
    p.add_argument("--loop", action="store_true",
                   help="Loop forever; sleep --interval seconds between cycles.")
    p.add_argument("--interval", type=int, default=1800,
                   help="Sleep seconds between cycles in --loop mode "
                        "(default 1800 = 30 min).")
    p.add_argument("--log-level",
                   default=os.getenv("POLYMARKET_LOG_LEVEL", "INFO"),
                   help="Logging level: DEBUG, INFO (default), WARNING, ERROR.")
    # ── Exit-management flags ───────────────────────────────────────────
    # All optional. Setting any threshold enables that rule; --disable-mgmt
    # forces the legacy hold-to-resolution behavior even if thresholds
    # are passed (useful in cron where you want a profile to stay unmanaged
    # without rewriting the line).
    p.add_argument("--take-profit-pct", type=float, default=None,
                   help="Exit when unrealized P&L%% ≥ this fraction. "
                        "0.50 = +50%% gain. Disabled when omitted.")
    p.add_argument("--stop-loss-pct", type=float, default=None,
                   help="Exit when unrealized P&L%% ≤ -|this|. "
                        "0.40 → exit at -40%% loss. Sign forced negative.")
    p.add_argument("--take-profit-price", type=float, default=None,
                   help="Exit when the position-side price reaches this "
                        "absolute level (YES price for YES positions; "
                        "1-YES for NO).")
    p.add_argument("--trail-stop-pct", type=float, default=None,
                   help="Exit when side price drops this fraction below "
                        "its running peak since entry. 0.15 = 15%% off peak.")
    p.add_argument("--tp-to-trail-stop-pct", type=float, default=None,
                   help="Two-stage take-profit. When set alongside "
                        "--take-profit-pct or --take-profit-price: hitting "
                        "TP arms a TIGHT trailing stop at this fraction "
                        "off peak instead of selling. 0.05 = exit when "
                        "price drops 5%% off the post-TP peak. Lets "
                        "winners ride while capping the giveback.")
    p.add_argument("--trail-stop-atr-mult", type=float, default=None,
                   help="Volatility-aware trailing stop: K standard "
                        "deviations of recent realized vol off peak. "
                        "1.5–2.0 is typical. Quiet markets get a tight "
                        "trail, jumpy markets get room. Composes with "
                        "--trail-stop-pct (whichever fires first wins). "
                        "Costs one extra API call per open position per "
                        "cycle to fetch price history.")
    p.add_argument("--trail-vol-window-hours", type=float, default=48.0,
                   help="Lookback window for the vol estimate used by "
                        "--trail-stop-atr-mult (default 48h).")
    p.add_argument("--time-stop-days", type=float, default=None,
                   help="Force-exit positions older than this many days.")
    p.add_argument("--disable-mgmt", action="store_true",
                   help="Disable the entire management phase (overrides "
                        "any threshold flags). Keep this on for hold-to-"
                        "resolution baseline profiles.")
    # ── History-aware strategy support ─────────────────────────────────
    p.add_argument("--snapshot-history-days", type=int, default=0,
                   help="When > 0, fetch this many days of price history "
                        "per market and pack it into the MarketSnapshot. "
                        "Needed by history-aware strategies (momentum, "
                        "mean_reversion, oscillation_revert). Costs one "
                        "extra API call per scanned market — keep "
                        "--max-markets reasonable.")
    p.add_argument("--snapshot-history-fidelity", type=int, default=60,
                   help="History bar size in minutes (default 60). Only "
                        "consulted when --snapshot-history-days > 0.")
    p.add_argument("--strategy-arg", action="append", default=[],
                   metavar="KEY=VALUE",
                   help="Strategy-specific keyword argument. Repeatable. "
                        "Example: --strategy-arg model=qwen2.5:14b "
                        "--strategy-arg min_gap=0.15. Values are coerced to "
                        "int → float → bool → str (first that parses). Bound "
                        "to the strategy via functools.partial so the value "
                        "shadows the strategy's own default for every call "
                        "in this run.")
    return p.parse_args(argv)


def _parse_strategy_args(items: list[str]) -> dict[str, Any]:
    """Turn ``["model=qwen2.5:14b", "min_gap=0.15"]`` into a dict whose
    values are coerced to int → float → bool → str (first that parses).
    Keys keep their original case. A bare key with no ``=`` is treated
    as ``key=True`` for convenience flags."""
    out: dict[str, Any] = {}
    for raw in items:
        if "=" not in raw:
            out[raw] = True
            continue
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # int → float → bool → str (string is last resort so things
        # like "model=qwen2.5:14b" land as the original string)
        try:
            out[key] = int(value)
            continue
        except ValueError:
            pass
        try:
            out[key] = float(value)
            continue
        except ValueError:
            pass
        if value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
            continue
        out[key] = value
    return out


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logctx.setup(args.log_level.upper())

    log.info("strategy_runner starting (profile=%s strategy=%s "
             "loop=%s interval=%ds)",
             args.profile, args.strategy, args.loop, args.interval)

    # Save the user's current selection so cron invocations don't
    # silently steal it from a running Streamlit session. Restored on
    # exit (normal, error, signal) by the try/finally below.
    profiles.initialize_profiles()
    saved_profile = profiles.active_profile()

    try:
        _ensure_active_profile(args.profile)
    except ValueError:
        return 1

    # Discover plugin strategies so --strategy can reference them
    bt.discover_user_strategies()

    strategy_kwargs = _parse_strategy_args(args.strategy_arg)
    if strategy_kwargs:
        log.info("strategy kwargs: %s", strategy_kwargs)

    exit_policy = ExitPolicy(
        take_profit_pct=args.take_profit_pct,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_price=args.take_profit_price,
        trail_stop_pct=args.trail_stop_pct,
        trail_stop_atr_mult=args.trail_stop_atr_mult,
        trail_vol_window_hours=args.trail_vol_window_hours,
        tp_to_trail_stop_pct=args.tp_to_trail_stop_pct,
        time_stop_days=args.time_stop_days,
        disable_mgmt=args.disable_mgmt,
    )
    if not exit_policy.disable_mgmt and any(
        v is not None for v in (
            exit_policy.take_profit_pct, exit_policy.stop_loss_pct,
            exit_policy.take_profit_price, exit_policy.trail_stop_pct,
            exit_policy.time_stop_days,
        )
    ):
        log.info(
            "exit policy: TP=%s SL=%s TP$=%s trail=%s timeout=%sd",
            exit_policy.take_profit_pct, exit_policy.stop_loss_pct,
            exit_policy.take_profit_price, exit_policy.trail_stop_pct,
            exit_policy.time_stop_days,
        )
    else:
        log.info("exit policy: disabled (hold to resolution)")

    try:
        while True:
            t0 = time.time()
            try:
                # R-23: strategy + profile are runtime args here, so the
                # binding lives at the call site rather than a @scope on
                # run_cycle. One cron invocation = one cycle id.
                with logctx.bind(runner=args.strategy, profile=args.profile,
                                 cycle=logctx.new_id("cyc")):
                    stats = run_cycle(
                        strategy_key=args.strategy,
                        bankroll=args.bankroll,
                        kelly_multiplier=args.kelly_multiplier,
                        half_spread=args.half_spread,
                        max_position_pct=args.max_position_pct,
                        min_gap=args.min_gap,
                        max_markets=args.max_markets,
                        profile_name=args.profile,
                        strategy_kwargs=strategy_kwargs,
                        closing_within_days=args.closing_within_days,
                        exit_policy=exit_policy,
                        history_days=args.snapshot_history_days,
                        history_fidelity=args.snapshot_history_fidelity,
                        max_positions_per_event=args.max_positions_per_event,
                    )
            except KeyboardInterrupt:
                log.info("interrupted; exiting")
                return 0
            except Exception as e:
                log.error("cycle failed: %s", e, exc_info=True)
                if not args.loop:
                    return 1
            else:
                reason_summary = (
                    " " + " ".join(
                        f"{k}={v}" for k, v in stats.exit_reasons.items()
                    ) if stats.exit_reasons else ""
                )
                event_cap_s = (
                    f" skipped(event-cap)={stats.skipped_event_cap}"
                    if stats.skipped_event_cap else ""
                )
                log.info(
                    "cycle complete: scanned=%d signals=%d opened=%d "
                    "skipped(existing)=%d skipped(no-edge)=%d%s "
                    "resolved=%d managed_exits=%d%s errors=%d",
                    stats.markets_scanned, stats.signals_emitted,
                    stats.positions_opened, stats.skipped_existing,
                    stats.skipped_no_edge, event_cap_s,
                    stats.resolved_closed,
                    stats.managed_exits, reason_summary, stats.errors,
                )

            elapsed = time.time() - t0
            log.info("cycle elapsed %.1fs", elapsed)
            if not args.loop:
                return 0
            sleep_for = max(1.0, args.interval - elapsed)
            log.debug("sleeping %.0fs", sleep_for)
            time.sleep(sleep_for)
    finally:
        # Restore the user's prior selection. Critical: cron runs every
        # 30 min; without this the Streamlit UI's active_profile flips
        # to whatever runner ran last.
        if saved_profile and saved_profile != args.profile:
            try:
                profiles.set_active_profile(saved_profile)
                log.info("restored active profile to %r", saved_profile)
            except Exception as e:
                log.warning("couldn't restore active profile: %s", e)


if __name__ == "__main__":
    raise SystemExit(main())
