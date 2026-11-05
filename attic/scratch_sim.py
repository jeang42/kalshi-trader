"""Counterfactual simulator + forecast scoring over the gathered data.

Replays arbitrary entry/exit rules against real per-side price paths
(from scratch_crunch.load) with known outcomes. Realistic pricing:
  entry  = pay the ASK
  scalp exit = receive the BID
  resolution exit = $1 (win) / $0 (loss)
Fees via kalshi_fees (sports rate auto-detected from ticker).

Stake fixed at $50/position (= the daemons' bankroll*max_position_pct).
"""
from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass

import kalshi_fees
import scratch_crunch as C

STAKE = 50.0


@dataclass
class Trade:
    ticker: str
    entry_price: float
    exit_price: float          # realized sale price (bid) or 1/0 at resolution
    reason: str
    won: bool                  # did the side win its game
    shares: float
    gross: float
    fees: float
    net: float
    entry_idx: int
    exit_idx: int


def _fee_rate(ticker: str) -> float:
    return kalshi_fees.fee_rate_for_series(ticker)


def simulate(
    d: C.Data,
    entry_fn: Callable[[list[dict]], int | None],
    exit_fn: Callable[[list[dict], int, float], tuple[int, str, float]],
    *,
    only_resolved: bool = True,
) -> list[Trade]:
    """entry_fn(rows) -> entry index or None (no trade).
    exit_fn(rows, entry_idx, entry_price) -> (exit_idx, reason, exit_price).
    exit_price convention: caller returns the realized price; for a
    resolution exit pass 1.0/0.0. We recompute fees here.
    """
    trades: list[Trade] = []
    for t, rows in d.traj.items():
        if only_resolved and t not in d.won:
            continue
        ei = entry_fn(rows)
        if ei is None:
            continue
        ask = rows[ei].get("ask")
        if not ask or ask <= 0:
            continue
        xi, reason, xprice = exit_fn(rows, ei, ask)
        won = d.won.get(t, False)
        shares = STAKE / ask
        rate = _fee_rate(t)
        if reason == "resolution":
            xprice = 1.0 if won else 0.0
            # entry-leg fee only (settlement free)
            fees = kalshi_fees.fee_for_trade(ask, shares, fee_rate=rate)
        else:
            fees = kalshi_fees.round_trip_fees(ask, xprice, shares,
                                               ticker=t, fee_rate=rate)
        gross = (xprice - ask) * shares
        net = gross - fees
        trades.append(Trade(t, ask, xprice, reason, won, shares,
                            gross, fees, net, ei, xi))
    return trades


# ─── exit-rule builders ───────────────────────────────────────────────────

def _bid(row: dict) -> float | None:
    b = row.get("bid")
    return b if (b and b > 0) else None


def exit_hold(rows, ei, entry):
    """Hold to resolution always."""
    return len(rows) - 1, "resolution", 0.0


def make_exit(tp: float | None = None, sl: float | None = None,
              time_inning: int | None = None):
    """Take-profit at +tp (net-ish, on bid), stop-loss at -sl (on bid),
    optional time stop at start of inning `time_inning`, else hold to
    resolution. tp/sl are fractional moves on PRICE (bid vs entry ask)."""
    def fn(rows, ei, entry):
        for i in range(ei + 1, len(rows)):
            b = _bid(rows[i])
            if b is not None:
                chg = (b - entry) / entry
                if tp is not None and chg >= tp:
                    return i, "take_profit", b
                if sl is not None and chg <= -sl:
                    return i, "stop_loss", b
            if time_inning is not None and (rows[i].get("inning") or 0) >= time_inning:
                bb = _bid(rows[i]) or entry
                return i, "time_stop", bb
        return len(rows) - 1, "resolution", 0.0
    return fn


def exit_oracle_peak(rows, ei, entry):
    """Upper bound: sell at the highest bid after entry (perfect foresight)."""
    best_i, best_b = ei, entry
    for i in range(ei + 1, len(rows)):
        b = _bid(rows[i])
        if b is not None and b > best_b:
            best_i, best_b = i, b
    if best_i == ei:
        return len(rows) - 1, "resolution", 0.0
    return best_i, "oracle_peak", best_b


# ─── metrics ──────────────────────────────────────────────────────────────

def metrics(trades: list[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    nets = [t.net for t in trades]
    tot = sum(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gross_w = sum(wins)
    gross_l = -sum(losses)
    sd = statistics.pstdev(nets) if n > 1 else 0.0
    sharpe = (statistics.mean(nets) / sd) if sd > 0 else 0.0
    return {
        "n": n,
        "net": round(tot, 2),
        "per_trade": round(tot / n, 2),
        "win_rate": round(len(wins) / n, 3),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else float("inf"),
        "sharpe_per_trade": round(sharpe, 3),
        "best": round(max(nets), 2),
        "worst": round(min(nets), 2),
    }


def fmt(name: str, m: dict) -> str:
    if m.get("n", 0) == 0:
        return f"{name:42s} n=0"
    return (f"{name:42s} n={m['n']:3d}  net=${m['net']:8.2f}  "
            f"/trade=${m['per_trade']:6.2f}  win={m['win_rate']:.0%}  "
            f"pf={m['profit_factor']:.2f}  sharpe={m['sharpe_per_trade']:.3f}")
