"""Execution adapter — the seam between strategy decisions and order fills.

A strategy daemon obtains a :class:`Broker` once per cycle via
:func:`get_broker` and routes every buy/sell/position/cash query through it.
Two implementations:

* :class:`PaperBroker` — delegates to ``portfolio`` exactly as the daemons do
  today (instant fill at the given price, fractional shares). **Default.**
* :class:`LiveBroker` — places real Kalshi limit orders via ``kalshi_orders``,
  runs the pre-flight guardrails, reconciles fills against the venue, and reads
  position/cash truth from the authenticated account.

See ``docs/live-execution-adapter.md`` for the full design + runbook. The live
path is **dormant** until three independent gates all pass (see
:func:`get_broker`), so wiring this in is a no-op for paper trading.

Nothing here is Streamlit-aware; daemons import it directly.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pandas as pd

from pm.core import atomic_io, logctx, shutdown
from pm.ledger import portfolio as pf

log = logging.getLogger("execution")

# Comma-separated profiles allowed to use the LiveBroker. Empty (default) means
# NO profile is live — even with credentials + KALSHI_LIVE_ENABLED, everything
# stays paper until a profile is named here. The per-strategy on-switch.
LIVE_PROFILES_ENV = "LIVE_TRADING_PROFILES"


@dataclass
class OrderResult:
    """Outcome of a buy/sell. ``status`` ∈ {paper, filled, partial, resting,
    rejected, error, settle}. ``avg_fill_price`` is in dollars for our side."""

    ok: bool
    status: str
    ticker: str
    side: str            # "yes" | "no"
    action: str          # "buy" | "sell"
    requested_count: float
    filled_count: float = 0.0
    avg_fill_price: float | None = None
    order_id: str | None = None
    issues: list[tuple[str, str]] = field(default_factory=list)
    detail: str = ""


@dataclass
class LiveExecConfig:
    """Conservative live-order tunables. All overridable per call/site."""

    max_stake_dollars: float = 25.0     # per-order hard cap (also enforced by
    #                                     kalshi_orders.pre_flight_check)
    marketable_cross_cents: int = 1     # entries: cross the spread by N¢ to
    #                                     TAKE liquidity. 0 rests at the (often
    #                                     stale) ask and frequently never fills
    #                                     once the book ticks during our reaction
    #                                     window — measured ~33% of live reaction
    #                                     buys rested unfilled at 0. Default 1
    #                                     crosses a normal 1¢ spread; bump via
    #                                     LIVE_MARKETABLE_CROSS_CENTS for time-
    #                                     sensitive arms whose quote drifts.
    exit_cross_cents: int = 5           # EXITS (marketable=True): cross this
    #                                     much past the quoted price so a stop /
    #                                     scalp sell fills even if the book moved
    #                                     since we read the (possibly stale)
    #                                     quote. Kalshi has no stop order, so a
    #                                     reliably-filling marketable exit IS the
    #                                     stop. See docs/live-execution-adapter.md.
    fill_timeout_s: float = 8.0         # poll fills up to this long
    poll_interval_s: float = 1.0
    cancel_unfilled: bool = True        # cancel the resting remainder on timeout
    scope_to_own_positions: bool = True  # get_positions returns ONLY this
    #                                     arm's positions (tracked in its
    #                                     position_meta) — required when
    #                                     multiple live arms share one account
    #                                     so an arm never manages another's
    #                                     holds. Off = see the whole account.
    max_orders_per_day: int = 0          # 0 = use pre_flight_check's default
    #                                     (10/UTC-day). Raise for a high-
    #                                     frequency arm (reactive scalp places
    #                                     entry+exit per reaction → ~50-80/day);
    #                                     env LIVE_MAX_ORDERS_PER_DAY. Still an
    #                                     anti-runaway cap, just a higher one.
    enforce_drawdown: bool = True        # feed the drawdown circuit breaker
    #                                     into pre_flight_check. Until
    #                                     2026-08-08 this was hard-coded False
    #                                     with a TODO, so the one guardrail
    #                                     designed to stop a LOSING automated
    #                                     arm protected the manual UI only.
    drawdown_pause_pct: float | None = None  # None = risk.load_settings()'s
    #                                     drawdown_pause_pct (default 0.15).


# ─── Broker protocol ──────────────────────────────────────────────────────


class Broker(Protocol):
    """What a strategy daemon needs from its execution backend."""

    name: str
    is_live: bool
    # True when a resolved market settles itself on the venue (live) — the
    # daemon must NOT place a sell at resolution in that case.
    closes_settle_on_venue: bool
    # True when the broker can rest a take-profit limit on the venue (live).
    # When True the daemon places a resting cash-out limit on entry and lets
    # the venue capture the pop, instead of polling for the take-profit.
    rests_take_profit: bool

    def buy(self, *, question: str, ticker: str, outcome: str,
            count: float, price: float,
            current_mid: float | None = None,
            marketable: bool = False) -> OrderResult: ...

    def sell(self, *, question: str, ticker: str, outcome: str,
             count: float, price: float,
             current_mid: float | None = None,
             marketable: bool = False) -> OrderResult: ...

    def get_positions(self) -> pd.DataFrame: ...

    def available_cash(self, bankroll: float) -> float: ...

    def place_resting_take_profit(self, *, ticker: str, outcome: str,
                                  count: float, tp_price: float) -> OrderResult: ...

    def cancel(self, order_id: str) -> bool: ...

    def fills_for_order(self, *, ticker: str,
                        order_id: str) -> tuple[float, float | None]: ...


# ─── Paper ────────────────────────────────────────────────────────────────


class PaperBroker:
    """Today's behavior, behind the Broker seam. Delegates to ``portfolio``
    against whatever profile is currently pinned (the daemon pins it)."""

    name = "paper"
    is_live = False
    closes_settle_on_venue = False  # paper "sells" at $1/$0 on resolution
    rests_take_profit = False       # paper uses the polled take-profit

    def buy(self, *, question: str, ticker: str, outcome: str,
            count: float, price: float,
            current_mid: float | None = None,
            marketable: bool = False) -> OrderResult:
        # `marketable` is a live-only execution hint (paper fills instantly).
        pf.add_buy(question=question, condition_id=ticker, outcome=outcome,
                   shares=count, price=price)
        return OrderResult(ok=True, status="paper", ticker=ticker,
                           side=_side_of(outcome), action="buy",
                           requested_count=count, filled_count=count,
                           avg_fill_price=price)

    def sell(self, *, question: str, ticker: str, outcome: str,
             count: float, price: float,
             current_mid: float | None = None,
             marketable: bool = False) -> OrderResult:
        pf.add_sell(question=question, condition_id=ticker, outcome=outcome,
                    shares=count, price=price)
        return OrderResult(ok=True, status="paper", ticker=ticker,
                           side=_side_of(outcome), action="sell",
                           requested_count=count, filled_count=count,
                           avg_fill_price=price)

    def get_positions(self) -> pd.DataFrame:
        return pf.get_positions()

    def available_cash(self, bankroll: float) -> float:
        pos = pf.get_positions()
        deployed = 0.0
        if not pos.empty and "cost_basis" in pos.columns:
            try:
                deployed = float(pos["cost_basis"].sum())
            except (ValueError, TypeError):
                deployed = 0.0
        return max(0.0, bankroll - deployed)

    # Resting cash-out limit is a live-only concept; paper relies on the
    # daemon's polled take-profit, so these are inert no-ops.
    def place_resting_take_profit(self, *, ticker: str, outcome: str,
                                  count: float, tp_price: float) -> OrderResult:
        return OrderResult(ok=False, status="paper", ticker=ticker,
                           side=_side_of(outcome), action="sell",
                           requested_count=count)

    def cancel(self, order_id: str) -> bool:
        return True

    def fills_for_order(self, *, ticker: str,
                        order_id: str) -> tuple[float, float | None]:
        return 0.0, None


# ─── Live ─────────────────────────────────────────────────────────────────


class LiveBroker:
    """Real Kalshi orders. Constructed only when all gates in
    :func:`get_broker` pass; still defends itself (pre-flight + the
    re-checks inside ``kalshi_orders.place_limit_order``)."""

    name = "live"
    is_live = True
    closes_settle_on_venue = True  # Kalshi auto-settles resolved markets
    rests_take_profit = True       # cash-out limit rests on the venue

    def __init__(self, cfg: LiveExecConfig | None = None):
        self.cfg = cfg or LiveExecConfig()

    # -- buy / sell -------------------------------------------------------

    def buy(self, *, question: str, ticker: str, outcome: str,
            count: float, price: float,
            current_mid: float | None = None,
            marketable: bool = False) -> OrderResult:
        return self._place(action="buy", ticker=ticker, outcome=outcome,
                           count=count, price=price, current_mid=current_mid,
                           marketable=marketable)

    def sell(self, *, question: str, ticker: str, outcome: str,
             count: float, price: float,
             current_mid: float | None = None,
             marketable: bool = False) -> OrderResult:
        return self._place(action="sell", ticker=ticker, outcome=outcome,
                           count=count, price=price, current_mid=current_mid,
                           marketable=marketable)

    def _place(self, *, action: str, ticker: str, outcome: str,
               count: float, price: float,
               current_mid: float | None, marketable: bool = False) -> OrderResult:
        # The whole place → poll-fills → cancel-remainder sequence is one
        # critical section: a SIGTERM landing between the venue accepting the
        # order and this returning would leave a real position nothing on our
        # side has recorded (R-20). The caller's own record step wraps this
        # in its own deferred(); nesting is fine.
        # R-23: one order id ties the submit/accepted/cancel audit rows and
        # every log line of this placement together — bound here, at the one
        # choke point all live orders pass through, so no caller can forget.
        with logctx.bind(ticker=ticker, action=action,
                         order=logctx.new_id("ord")), \
             shutdown.deferred(f"live {action} {ticker}"):
            return self._place_unguarded(action=action, ticker=ticker,
                                         outcome=outcome, count=count, price=price,
                                         current_mid=current_mid, marketable=marketable)

    def _place_unguarded(self, *, action: str, ticker: str, outcome: str,
                         count: float, price: float,
                         current_mid: float | None, marketable: bool = False) -> OrderResult:
        from pm.venue import kalshi_orders as ko

        side = _side_of(outcome)
        res = OrderResult(ok=False, status="error", ticker=ticker, side=side,
                          action=action, requested_count=count)

        # Limit price in cents, clamped to the tradeable band. A buy crosses
        # UP, a sell crosses DOWN. `marketable` exits use the larger
        # exit_cross so the order takes liquidity even if the book moved since
        # we read the quote (Kalshi has no stop order — a reliably-filling
        # marketable sell is how the stop actually exits).
        if action == "buy":
            limit_cents = round(price * 100) + self.cfg.marketable_cross_cents
        else:
            # Marketable sell (stop/scalp) crosses DOWN to take the bid; a
            # non-marketable sell (resting take-profit) rests AT its target.
            cross = self.cfg.exit_cross_cents if marketable else 0
            limit_cents = round(price * 100) - cross
        limit_cents = max(1, min(99, int(limit_cents)))

        # Integer contracts (Kalshi trades whole contracts).
        int_count = int(math.floor(count))
        if int_count <= 0:
            res.status, res.detail = "rejected", "count<1 contract after floor"
            return res

        # Resize a BUY down to fit the per-order stake cap, so
        # LIVE_MAX_STAKE_DOLLARS produces a minimum-size order instead of a
        # pre-flight rejection of an oversized one (the canary's whole point).
        # Sells are exits of an existing position — never resized (we want to
        # close the full position, not leave a partial).
        if action == "buy":
            max_contracts = int((self.cfg.max_stake_dollars * 100) // limit_cents)
            if max_contracts < 1:
                res.status, res.detail = "rejected", (
                    f"max_stake ${self.cfg.max_stake_dollars:.2f} buys <1 "
                    f"contract at {limit_cents}c")
                return res
            if int_count > max_contracts:
                log.info("clamping %s buy %d->%d contracts to fit $%.2f cap",
                         ticker, int_count, max_contracts,
                         self.cfg.max_stake_dollars)
                int_count = max_contracts

        # Idempotency: don't stack a second order if one already rests here.
        try:
            resting = ko.get_resting_orders(ticker=ticker)
            if resting is not None and not resting.empty:
                res.status = "resting"
                res.detail = "order already resting for ticker — skipped"
                res.order_id = str(resting.iloc[0].get("order_id", "")) or None
                return res
        except Exception as e:  # noqa: BLE001
            log.warning("resting-order check failed for %s: %s", ticker, e)

        # Pre-flight guardrails.
        try:
            today_n = ko._today_submit_count()
        except Exception:  # noqa: BLE001
            today_n = 0
        mid_cents = round(current_mid * 100) if current_mid is not None else None
        pf_kw = {}
        if self.cfg.max_orders_per_day > 0:  # else pre_flight uses its default
            pf_kw["max_orders_per_day"] = self.cfg.max_orders_per_day
        issues = ko.pre_flight_check(
            ticker=ticker, side=side, count=int_count,
            limit_price_cents=limit_cents, current_mid_cents=mid_cents,
            bankroll=self.available_cash(float("inf")),
            in_drawdown_pause=self._in_drawdown_pause(),
            todays_order_count=today_n,
            max_stake_dollars=self.cfg.max_stake_dollars,
            **pf_kw,
        )
        res.issues = issues
        if any(sev == "block" for sev, _ in issues):
            res.status = "rejected"
            res.detail = "; ".join(m for s, m in issues if s == "block")
            log.warning("pre-flight blocked %s %s: %s", action, ticker, res.detail)
            return res

        # Place. The client order id is generated HERE rather than inside
        # place_limit_order so that, if the call raises, we still know what to
        # look for — see the reconciliation below.
        coid = ko._make_client_order_id(ticker)
        try:
            resp = ko.place_limit_order(
                ticker=ticker, side=side, action=action,
                count=int_count, limit_price_cents=limit_cents,
                client_order_id=coid)
        except Exception as e:  # noqa: BLE001
            # An exception does NOT mean the order did not land. A read timeout
            # after the venue accepted it looks identical here to an outright
            # rejection, and treating both as "nothing happened" leaves a real
            # position nobody is managing: invisible to the arm's own
            # bookkeeping, absent from position_meta, and therefore never
            # exited. Ask the venue what it actually has.
            log.error("place_limit_order failed for %s: %s", ticker, e)
            recovered = self._reconcile_after_error(ticker, coid)
            if recovered is None:
                res.status, res.detail = "error", f"place failed: {e}"
                return res
            log.warning(
                "RECOVERED an orphaned order for %s: the submission raised (%s) "
                "but the venue has order %s. Adopting it rather than leaving it "
                "unmanaged.", ticker, e, recovered)
            resp, res.detail = {"order": {"order_id": recovered}}, (
                f"submission raised ({e}) but the order existed; adopted")
        order_id = _extract_order_id(resp)
        res.order_id = order_id

        # Reconcile fills. _summarize_fills returns the YES-leg price (Kalshi
        # reports fills on the YES contract); the OrderResult contract is
        # "dollars for OUR side", so a NO buy must convert: NO cost = 1 - yes.
        # Without this, a NO fade recorded fill=0.17 for a NO bought at 0.83
        # and every downstream slippage/cost number was ~complement-wrong
        # (found 2026-08-01 via the fade_nearclose canary's impossible -70c
        # "slips").
        filled, avg = self._poll_fills(ticker, order_id, int_count)
        if avg is not None and side == "no":
            avg = round(1.0 - avg, 6)
        res.filled_count, res.avg_fill_price = filled, avg
        if filled >= int_count:
            res.ok, res.status = True, "filled"
        elif filled > 0:
            res.ok, res.status = True, "partial"
        else:
            res.status = "resting"

        # Cancel the unfilled remainder so nothing stale lingers.
        if filled < int_count and self.cfg.cancel_unfilled and order_id:
            try:
                ko.cancel_order(order_id)
            except Exception as e:  # noqa: BLE001
                log.warning("cancel remainder failed for %s: %s", order_id, e)
        return res

    @staticmethod
    def _summarize_fills(fills: pd.DataFrame | None,
                         order_id: str | None) -> tuple[float, float | None]:
        """Filled count + volume-weighted avg price (our YES side) for one
        order_id, from a fills frame. (0.0, None) when nothing matches."""
        if fills is None or fills.empty or not order_id:
            return 0.0, None
        mine = fills[fills["order_id"].astype(str) == str(order_id)]
        if mine.empty:
            return 0.0, None
        cnt = float(mine["count"].sum())
        # our side's price in dollars, vol-weighted (we trade the YES contract)
        num = float((mine["yes_price_cents"] * mine["count"]).sum())
        return cnt, (num / cnt / 100.0) if cnt > 0 else None

    def _poll_fills(self, ticker: str, order_id: str | None,
                    want: int) -> tuple[float, float | None]:
        """Poll until our order fills `want` contracts or the timeout."""
        from pm.venue import kalshi_client as kc

        deadline = time.time() + self.cfg.fill_timeout_s
        last_count, last_avg = 0.0, None
        while True:
            try:
                fills = kc.get_fills(ticker=ticker)
            except Exception as e:  # noqa: BLE001
                log.warning("get_fills failed for %s: %s", ticker, e)
                fills = pd.DataFrame()
            cnt, avg = self._summarize_fills(fills, order_id)
            if cnt:
                last_count, last_avg = cnt, avg
            if last_count >= want or time.time() >= deadline:
                return last_count, last_avg
            time.sleep(self.cfg.poll_interval_s)

    def fills_for_order(self, *, ticker: str,
                        order_id: str) -> tuple[float, float | None]:
        """Single-shot: has `order_id` filled? Used to detect whether a
        resting cash-out limit executed (count, avg_price) without waiting."""
        from pm.venue import kalshi_client as kc
        try:
            fills = kc.get_fills(ticker=ticker)
        except Exception as e:  # noqa: BLE001
            log.warning("fills_for_order get_fills failed for %s: %s", ticker, e)
            return 0.0, None
        return self._summarize_fills(fills, order_id)

    def place_resting_take_profit(self, *, ticker: str, outcome: str,
                                  count: float, tp_price: float) -> OrderResult:
        """Rest a GTC limit SELL at `tp_price` (above market) so the venue
        captures the pop with zero poll latency. NOT marketable (it must
        rest, not cross) and NOT polled for an immediate fill. Exits a held
        position, so it's not stake-capped; `place_limit_order`'s own guards
        (live-enabled, emergency-stop, price band) still apply."""
        from pm.venue import kalshi_orders as ko

        side = _side_of(outcome)
        res = OrderResult(ok=False, status="error", ticker=ticker, side=side,
                          action="sell", requested_count=count)
        int_count = int(math.floor(count))
        if int_count <= 0:
            res.status, res.detail = "rejected", "count<1 contract"
            return res
        limit_cents = max(1, min(99, round(tp_price * 100)))
        try:
            resp = ko.place_limit_order(ticker=ticker, side=side, action="sell",
                                        count=int_count, limit_price_cents=limit_cents)
        except Exception as e:  # noqa: BLE001
            res.status, res.detail = "error", f"place TP failed: {e}"
            log.error("resting TP failed for %s: %s", ticker, e)
            return res
        res.ok, res.status = True, "resting"
        res.order_id = _extract_order_id(resp)
        res.avg_fill_price = limit_cents / 100.0
        log.info("RESTING TP %s sell %dx@%dc (order=%s)",
                 ticker, int_count, limit_cents, res.order_id)
        return res

    def cancel(self, order_id: str) -> bool:
        from pm.venue import kalshi_orders as ko
        if not order_id:
            return False
        try:
            ko.cancel_order(order_id)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("cancel order %s failed: %s", order_id, e)
            return False

    # -- state truth from the venue ---------------------------------------

    def get_positions(self) -> pd.DataFrame:
        """Authed positions normalized to the portfolio.get_positions shape
        the daemons expect (condition_id/outcome/shares/avg_price/cost_basis)."""
        from pm.venue import kalshi_client as kc

        cols = ["question", "condition_id", "outcome", "shares",
                "avg_price", "cost_basis", "added_at"]
        try:
            raw = kc.get_authed_positions()
        except Exception as e:  # noqa: BLE001
            log.error("get_authed_positions failed: %s", e)
            return pd.DataFrame(columns=cols)
        if raw is None or raw.empty:
            return pd.DataFrame(columns=cols)
        rows = []
        for _, r in raw.iterrows():
            pos = float(r.get("position", 0) or 0)
            if pos == 0:
                continue
            shares = abs(pos)
            exposure = float(r.get("market_exposure_cents", 0) or 0) / 100.0
            avg = (exposure / shares) if shares else 0.0
            rows.append({
                "question": str(r.get("ticker", "")),
                "condition_id": str(r.get("ticker", "")),
                "outcome": "Yes" if pos > 0 else "No",
                "shares": shares,
                "avg_price": avg,
                "cost_basis": exposure,
                "added_at": str(r.get("last_updated_ts", "")),
            })
        df = pd.DataFrame(rows, columns=cols)
        # Position-scoping: when multiple live arms share one Kalshi account,
        # each must act ONLY on positions it opened — otherwise one arm's
        # exit logic (stop / take-profit) would manage another arm's holds.
        # The arm's own positions are exactly those tracked in its (profile-
        # pinned) position_meta. Filtering here scopes get_positions for every
        # caller (_manage_positions, _close_resolved, available_cash). Also
        # keeps a daemon off the owner's hand-traded positions.
        if self.cfg.scope_to_own_positions and not df.empty:
            mine = _own_tickers()
            df = df[df["condition_id"].isin(mine)].reset_index(drop=True)
        return df

    def _reconcile_after_error(self, ticker: str, coid: str) -> str | None:
        """Did the order land despite the submission raising?

        Returns the venue's ``order_id`` when an order carrying our client
        order id exists, else None.

        This is the reason the client order id is generated by the caller: it
        is the only handle that survives a failed request, and without it a
        timeout that the venue actually honoured is indistinguishable from a
        rejection. Checks resting orders first (the common case — accepted,
        not yet filled), then fills (accepted and filled before we asked).

        Never raises: this runs on the error path, and a failure to reconcile
        must not replace the original error with a less informative one.
        """
        from pm.venue import kalshi_orders as ko

        try:
            resting = ko.get_resting_orders(ticker=ticker)
            if resting is not None and not resting.empty and "client_order_id" in resting.columns:
                mine = resting[resting["client_order_id"].astype(str) == coid]
                if not mine.empty:
                    return str(mine.iloc[0].get("order_id", "")) or None
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile: could not read resting orders for %s: %s",
                        ticker, e)
        try:
            from pm.venue import kalshi_client as kc
            fills = kc.get_fills(ticker=ticker)
            if fills is not None and not fills.empty and "client_order_id" in fills.columns:
                mine = fills[fills["client_order_id"].astype(str) == coid]
                if not mine.empty:
                    return str(mine.iloc[0].get("order_id", "")) or None
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile: could not read fills for %s: %s", ticker, e)
        return None

    def _in_drawdown_pause(self) -> bool:
        """True when the account is far enough below its high-water mark that
        new orders should be blocked.

        **Fails CLOSED.** If equity cannot be established or the state file is
        unusable, this returns True and trading stops. That matches
        ``emergency_stop.is_active()``, which reports ACTIVE on ``OSError`` for
        the same reason: on a money path, not knowing is not the same as being
        fine, and the cost of a false pause is a missed trade while the cost of
        a false all-clear is an unbounded loss.

        The high-water mark ratchets and is persisted, so a restart does not
        reset the breaker — which would otherwise make it trivially defeatable
        by the crash-loop the ledger fix was also written to prevent.
        """
        if not self.cfg.enforce_drawdown:
            return False

        threshold = self.cfg.drawdown_pause_pct
        if threshold is None:
            try:
                from pm.ledger import risk
                threshold = float(risk.load_settings().drawdown_pause_pct)
            except Exception as e:  # noqa: BLE001
                log.error("drawdown: cannot read the threshold (%s); PAUSING", e)
                return True

        equity = account_equity_dollars()
        if equity is None:
            log.error("drawdown: account equity is unknown; PAUSING (fail-closed)")
            return True

        path = _equity_state_path()
        try:
            with atomic_io.locked(path):
                state = atomic_io.read_json(path, None) or {}
                prev_peak = state.get("peak_equity_dollars")
                prev_peak = float(prev_peak) if prev_peak is not None else None
                peak, paused = evaluate_drawdown(equity, prev_peak, threshold)
                if peak != prev_peak or paused != bool(state.get("paused")):
                    atomic_io.write_json(path, {
                        "peak_equity_dollars": peak,
                        "equity_dollars": equity,
                        "threshold_pct": threshold,
                        "paused": paused,
                        "updated_at": datetime.now(UTC).isoformat(),
                    })
        except Exception as e:  # noqa: BLE001
            log.error("drawdown: state file %s unusable (%s); PAUSING", path, e)
            return True

        if paused:
            log.error(
                "DRAWDOWN CIRCUIT BREAKER: account equity $%.2f is %.1f%% below "
                "its peak of $%.2f (threshold %.1f%%). Blocking new orders. "
                "Raise drawdown_pause_pct on the Settings page, or reset the "
                "high-water mark in %s, once you have reviewed why.",
                equity, abs((equity - peak) / peak) * 100.0, peak,
                threshold * 100.0, path)
        return paused

    def available_cash(self, bankroll: float) -> float:
        """Deployable = min(bankroll budget left for THIS arm, settled venue
        balance). The bankroll term subtracts the arm's own deployed cost
        (scoped), so multiple arms on one account each respect their own
        budget; the shared balance caps total account exposure."""
        from pm.venue import kalshi_client as kc

        try:
            bal = kc.get_balance()
            balance_dollars = float(bal.get("balance_cents", 0)) / 100.0
        except Exception as e:  # noqa: BLE001
            log.error("get_balance failed: %s", e)
            return 0.0  # can't confirm cash → deploy nothing
        deployed = 0.0
        try:
            mine = self.get_positions()  # already scoped to this arm
            if not mine.empty and "cost_basis" in mine.columns:
                deployed = float(mine["cost_basis"].sum())
        except Exception:  # noqa: BLE001
            deployed = 0.0
        return max(0.0, min(bankroll - deployed, balance_dollars))


# ─── Drawdown circuit breaker ─────────────────────────────────────────────
#
# Scope note, because it is not obvious and it matters: this measures the
# ACCOUNT, not one arm. Several live arms plus the owner's own hand-trading
# share a single Kalshi account, so per-arm equity is not well defined —
# settled cash is one pool. An account-level measure is also the guard the
# fleet was missing: each arm previously enforced only its own per-order and
# per-day caps, so N arms meant N independent ceilings and no aggregate limit.
#
# The consequence to be aware of: a bad stretch of hand-trading can pause the
# automated arms. That is the intended direction — the breaker exists to stop
# automated trading when the capital pool is shrinking, whatever shrank it.


def _equity_state_path() -> Path:
    return Path(os.getenv("LIVE_EQUITY_STATE_FILE",
                          "data/shared/live_equity.json"))


def evaluate_drawdown(equity: float, peak: float | None,
                      threshold_pct: float) -> tuple[float, bool]:
    """Ratchet the high-water mark and decide whether to pause.

    Returns ``(new_peak, paused)``. Pure — the I/O lives in the caller.

    ``threshold_pct`` is positive (0.15 = "pause at 15% below peak"), matching
    ``risk.should_pause_for_drawdown``.

    >>> evaluate_drawdown(100.0, None, 0.15)      # first observation sets peak
    (100.0, False)
    >>> evaluate_drawdown(120.0, 100.0, 0.15)     # new high
    (120.0, False)
    >>> evaluate_drawdown(90.0, 100.0, 0.15)      # down 10% — inside tolerance
    (100.0, False)
    >>> evaluate_drawdown(85.0, 100.0, 0.15)      # down exactly 15% — pause
    (100.0, True)
    >>> evaluate_drawdown(50.0, 100.0, 0.15)
    (100.0, True)
    >>> evaluate_drawdown(10.0, 0.0, 0.15)        # peak 0 → nothing to fall from
    (10.0, False)
    """
    if peak is None or equity > peak:
        peak = equity
    if peak <= 0:
        return peak, False
    return peak, (equity - peak) / peak <= -abs(threshold_pct)


def account_equity_dollars() -> float | None:
    """Mark-to-market equity of the whole live account, or None if it cannot
    be established.

    ``settled cash + pending payout + market value of every open position``.
    Both calls are ones the live path already makes, so this adds no new
    request pattern. Returns None rather than a guess: a breaker that cannot
    see the account must not silently report "all fine".
    """
    from pm.venue import kalshi_client as kc

    try:
        bal = kc.get_balance()
        cash = (float(bal.get("balance_cents", 0))
                + float(bal.get("payout_cents", 0))) / 100.0
    except Exception as e:  # noqa: BLE001
        log.error("drawdown: get_balance failed (%s); equity unknown", e)
        return None
    try:
        pos = kc.get_authed_positions()
        exposure = 0.0
        if pos is not None and not pos.empty and "market_exposure_cents" in pos.columns:
            exposure = float(pos["market_exposure_cents"].fillna(0).sum()) / 100.0
    except Exception as e:  # noqa: BLE001
        log.error("drawdown: get_authed_positions failed (%s); equity unknown", e)
        return None
    return cash + exposure


# ─── Factory + helpers ────────────────────────────────────────────────────


def _side_of(outcome: str) -> str:
    return "yes" if str(outcome).strip().lower() in ("yes", "y") else "no"


def _own_tickers() -> set[str]:
    """Tickers this arm opened — the condition_ids in its (profile-pinned)
    position_meta. Used to scope a LiveBroker to its own positions."""
    from pm.ledger import position_meta as pm
    return {k.split("|", 1)[0] for k in pm.all_entries()}


def _extract_order_id(resp: dict | None) -> str | None:
    if not isinstance(resp, dict):
        return None
    order = resp.get("order")
    if isinstance(order, dict) and order.get("order_id"):
        return str(order["order_id"])
    return str(resp.get("order_id")) if resp.get("order_id") else None


def live_profiles() -> set[str]:
    raw = os.getenv(LIVE_PROFILES_ENV, "") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_profile_live(profile: str) -> bool:
    """All three gates: live-trading enabled (+ configured + owner-profile
    match), profile allow-listed, emergency-stop not active."""
    from pm.core import emergency_stop
    from pm.venue import kalshi_orders as ko

    if profile not in live_profiles():
        return False
    if not ko.is_trading_enabled():
        return False
    if emergency_stop.is_active():
        return False
    return True


def _live_cfg_from_env() -> LiveExecConfig:
    """Build the live config from env so go-live tooling can cap order size
    without code changes. ``LIVE_MAX_STAKE_DOLLARS`` sets the per-order hard
    cap (default 25); set it to e.g. 2 for minimum-size canary testing."""
    cfg = LiveExecConfig()
    raw = os.getenv("LIVE_MAX_STAKE_DOLLARS", "").strip()
    if raw:
        try:
            cfg.max_stake_dollars = float(raw)
        except ValueError:
            log.warning("ignoring bad LIVE_MAX_STAKE_DOLLARS=%r", raw)
    raw_n = os.getenv("LIVE_MAX_ORDERS_PER_DAY", "").strip()
    if raw_n:
        try:
            cfg.max_orders_per_day = int(raw_n)
        except ValueError:
            log.warning("ignoring bad LIVE_MAX_ORDERS_PER_DAY=%r", raw_n)
    raw_x = os.getenv("LIVE_MARKETABLE_CROSS_CENTS", "").strip()
    if raw_x:
        try:
            cfg.marketable_cross_cents = int(raw_x)
        except ValueError:
            log.warning("ignoring bad LIVE_MARKETABLE_CROSS_CENTS=%r", raw_x)
    return cfg


def get_broker(profile: str, *, live_cfg: LiveExecConfig | None = None) -> Broker:
    """Return the broker for ``profile``: :class:`LiveBroker` only when every
    gate passes, else :class:`PaperBroker`. Safe to call every cycle."""
    if is_profile_live(profile):
        cfg = live_cfg or _live_cfg_from_env()
        log.info("LIVE broker active for profile %s (max_stake=$%.2f)",
                 profile, cfg.max_stake_dollars)
        return LiveBroker(cfg=cfg)
    return PaperBroker()
