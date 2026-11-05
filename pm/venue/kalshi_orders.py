"""Kalshi authenticated order placement (Phase 4).

This is the first module in the project that can spend real money.
Trust model:

- Disabled unless ``KALSHI_LIVE_ENABLED=true`` in env (separate from
  having a key configured — flipping the key on isn't the same as
  flipping live trading on).
- Authentication gates from ``kalshi_auth`` apply on top: env vars
  set, PEM permissions tight, active profile == owner profile.
- Every attempt — accepted, rejected, errored — is appended to
  ``data/shared/orders.log`` (or whatever ``ORDERS_LOG_PATH`` env
  points at). Audit-first: the line is written *before* the HTTP
  call so a crash mid-request still leaves a trail, and the
  pre-submission write is mandatory — an unwritable audit log refuses
  the order (fail-closed) rather than placing it unrecorded.
- Mandatory caps are enforced at THIS boundary, not only in callers:
  every buy clears the per-order stake cap, and every order placed
  through :func:`place_limit_order` clears the daily order cap, whose
  check-and-reserve is atomic across processes (see
  :func:`_submission_lock`). ``pre_flight_check`` remains the richer,
  advisory layer for callers with more context (drawdown, mid-price).
- Only limit orders are exposed. No market orders (surprise
  slippage during thin liquidity is the most common way to lose
  money fast). No batch endpoint wrapper in Phase 4.

Phase-4 scope: a human clicks a button in the Kelly Sizer or the
Orders page. Automated execution from the agentic runner lands in
Phase 5 and reuses the same primitives.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - project targets Linux
    fcntl = None  # type: ignore[assignment]

import pandas as pd

from pm.core import emergency_stop, logctx, provenance
from pm.venue import kalshi_auth as ka

log = logging.getLogger(__name__)

# Lives in data/shared/audit/, NOT data/shared/, on purpose. This is an
# append-only real-money audit trail, and `scripts/logrotate.conf` rotates
# `data/shared/*.log` daily with a 14-day retention — which silently
# truncated this file and DELETED every generation older than ~2 weeks
# (found 2026-09-20; pre-2026-07-11 order history is gone for good). The
# glob is non-recursive, so a subdirectory is structurally out of its reach:
# no future log added to data/shared can take this one with it. Excluding it
# from the glob instead is NOT an option — logrotate 3.21 treats a
# specific-stanza + glob overlap as a fatal duplicate and stops processing
# the pattern, which silently stripped rotation from 28 of 47 other logs.
DEFAULT_ORDERS_LOG = Path("data/shared/audit/orders.log")

_LIVE_ENABLED_ENV = "KALSHI_LIVE_ENABLED"


class OrderRejected(RuntimeError):
    """Raised when the local guardrails block an order before it
    reaches Kalshi. Distinct from ``requests.HTTPError`` which means
    Kalshi itself rejected it (insufficient balance, market closed,
    etc.)."""


# ---------------------------------------------------------------------------
# Live-enabled flag
# ---------------------------------------------------------------------------

def is_live_enabled() -> bool:
    """Is ``KALSHI_LIVE_ENABLED`` set to a truthy value?

    Separate from ``kalshi_auth.is_configured()`` so flipping the key on
    (which enables read-only Live Portfolio) doesn't accidentally
    enable trading. The two switches must both be on for an order to
    be placeable.
    """
    val = os.getenv(_LIVE_ENABLED_ENV, "").strip().lower()
    return val in ("1", "true", "yes", "on")


def is_trading_enabled() -> bool:
    """Convenience: auth configured AND live flag set."""
    return ka.is_configured() and is_live_enabled()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _orders_log_path() -> Path:
    return Path(os.getenv("ORDERS_LOG_PATH", str(DEFAULT_ORDERS_LOG)))


def _audit(event: str, payload: dict, *, required: bool = False) -> None:
    """Append a JSON-line audit record.

    ``required=True`` — used for every pre-submission record — makes the
    write **fail closed**: if the audit line cannot be persisted, the order
    is refused (``OrderRejected``) rather than placed unrecorded. The
    audit-first invariant this module documents ("the line is written
    *before* the HTTP call") is only an invariant if an unwritable log
    stops the order. The cost is that a full disk also blocks exits placed
    through this module; the operator can always exit manually on Kalshi's
    own website, but an unaudited real-money order can never be un-placed.

    ``required=False`` (post-hoc records: accepted / error, and
    ``cancel_submit`` — refusing to *cancel* because the log is unwritable
    would be risk-increasing) stays best-effort: logged but not raised.
    """
    p = _orders_log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "commit": provenance.label(),   # R-18: which code placed it
            **payload,
        }
        # R-23: which CYCLE placed it. The commit stamp says what code ran;
        # this says which run of it — grep the cycle id across journald and
        # this file to see everything one cycle did around a real order.
        if (ctx := logctx.current()):
            line["ctx"] = ctx
        with open(p, "a") as f:
            f.write(json.dumps(line, default=str) + "\n")
    except OSError as e:
        if required:
            raise OrderRejected(
                f"audit log {p} is unwritable ({e}) — refusing to submit an "
                "order that would leave no audit trail. Fix the filesystem "
                "(or point ORDERS_LOG_PATH at a writable location) first."
            ) from e
        log.warning("orders audit log write failed: %s", e)


# ---------------------------------------------------------------------------
# Pre-flight risk checks
# ---------------------------------------------------------------------------

# Phase-4 defaults are deliberately small. This phase exists to prove
# the order plumbing is correct, not to size up.
DEFAULT_MAX_STAKE_DOLLARS = 50.0
DEFAULT_MAX_ORDERS_PER_DAY = 10
DEFAULT_MAX_PRICE_DISTANCE_CENTS = 10


def _today_submit_count() -> int:
    """Count ``submit`` events in the audit log dated today (UTC).
    Cheap linear scan — the log is small until Phase 5."""
    p = _orders_log_path()
    if not p.exists():
        return 0
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    n = 0
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") != "submit":
                    continue
                ts = str(rec.get("ts", ""))
                if ts.startswith(today):
                    n += 1
    except OSError:
        return 0
    return n


def mandatory_caps() -> tuple[float, int]:
    """The caps enforced unconditionally at this, the venue boundary:
    ``(max_stake_dollars, max_orders_per_day)``.

    Read from ``LIVE_MAX_STAKE_DOLLARS`` / ``LIVE_MAX_ORDERS_PER_DAY`` — the
    same knobs ``execution._live_cfg_from_env`` uses — falling back to this
    module's defaults. These are the *outer* limits: a caller (LiveBroker,
    the Kelly page) may configure itself stricter, never looser. Raising
    them is an explicit env change, i.e. an operator action.
    """
    stake = DEFAULT_MAX_STAKE_DOLLARS
    raw = os.getenv("LIVE_MAX_STAKE_DOLLARS", "").strip()
    if raw:
        try:
            stake = float(raw)
        except ValueError:
            log.warning("ignoring bad LIVE_MAX_STAKE_DOLLARS=%r", raw)
    per_day = DEFAULT_MAX_ORDERS_PER_DAY
    raw_n = os.getenv("LIVE_MAX_ORDERS_PER_DAY", "").strip()
    if raw_n:
        try:
            per_day = int(raw_n)
        except ValueError:
            log.warning("ignoring bad LIVE_MAX_ORDERS_PER_DAY=%r", raw_n)
    return stake, per_day


#: How long a submission waits for the reservation lock before refusing.
SUBMIT_LOCK_TIMEOUT_S = 10.0


@contextlib.contextmanager
def _submission_lock():
    """Exclusive cross-process lock for the daily-cap check + submit audit.

    The daily order cap was a read-check-write race: two daemons could both
    read ``_today_submit_count() == cap-1`` and both submit. Holding this
    lock across *count check → submit-audit append* makes the appended
    ``submit`` row an atomic reservation — the next process's count sees it.

    **Fails closed**: if the lock cannot be taken within
    :data:`SUBMIT_LOCK_TIMEOUT_S` the order is refused. This is the opposite
    of ``atomic_io.locked`` (which proceeds unlocked on timeout so a fill
    that already happened can still be recorded); here nothing has been
    placed yet, so refusing is the safe side.

    The HTTP call happens *outside* the lock — a slow venue response must
    not serialize every other process's submissions behind it.
    """
    if fcntl is None:  # pragma: no cover - project targets Linux
        yield
        return
    lp = Path(str(_orders_log_path()) + ".lock")
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise OrderRejected(
            f"cannot open the order-submission lock {lp} ({e}) — refusing "
            "to submit without the daily-cap reservation.") from e
    deadline = time.monotonic() + SUBMIT_LOCK_TIMEOUT_S
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise OrderRejected(
                        f"timed out after {SUBMIT_LOCK_TIMEOUT_S:.0f}s waiting "
                        "for the order-submission lock — another process may "
                        "be stuck mid-submission; refusing to bypass the "
                        "daily-cap reservation.") from None
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def pre_flight_check(
    *,
    ticker: str,
    side: str,
    count: int,
    limit_price_cents: int,
    current_mid_cents: int | None,
    bankroll: float,
    in_drawdown_pause: bool,
    todays_order_count: int,
    max_stake_dollars: float = DEFAULT_MAX_STAKE_DOLLARS,
    max_orders_per_day: int = DEFAULT_MAX_ORDERS_PER_DAY,
    max_price_distance_cents: int = DEFAULT_MAX_PRICE_DISTANCE_CENTS,
) -> list[tuple[str, str]]:
    """Run every guardrail check against a proposed order.

    Returns a list of ``(severity, message)`` tuples. ``severity`` is
    one of ``"block"`` (must clear before order can be submitted) or
    ``"warn"`` (advisory). Empty list = all clear.

    Pure / side-effect-free so the UI can call it on every widget
    interaction without performance worry.
    """
    issues: list[tuple[str, str]] = []

    if not is_trading_enabled():
        issues.append(("block",
            "Live trading is disabled. Set KALSHI_LIVE_ENABLED=true in .env "
            "and ensure the active profile matches KALSHI_OWNER_PROFILE."))
        # If trading isn't even on, the rest of the checks are moot.
        return issues

    if in_drawdown_pause:
        issues.append(("block",
            "Drawdown circuit breaker tripped. Recover before placing new "
            "orders. Adjust the threshold on the Settings page if intentional."))

    if count <= 0:
        issues.append(("block", f"Count must be positive (got {count})."))
    if not (1 <= limit_price_cents <= 99):
        issues.append(("block",
            f"Limit price must be in [1, 99] cents (got {limit_price_cents})."))

    stake_dollars = (count * limit_price_cents) / 100.0
    if stake_dollars > max_stake_dollars:
        issues.append(("block",
            f"Stake ${stake_dollars:.2f} exceeds the per-order cap "
            f"${max_stake_dollars:.2f}. Reduce count or limit price."))

    # Per-market bankroll cap — re-uses risk.py's notion of position size.
    if bankroll > 0:
        pct_of_bankroll = stake_dollars / bankroll
        if pct_of_bankroll > 0.05:
            issues.append(("warn",
                f"Stake is {pct_of_bankroll*100:.1f}% of bankroll. The "
                "configured per-market cap is 5%."))

    if current_mid_cents is not None:
        distance = abs(limit_price_cents - current_mid_cents)
        if distance > max_price_distance_cents:
            issues.append(("block",
                f"Limit price {limit_price_cents}¢ is {distance}¢ away from "
                f"the current mid ({current_mid_cents}¢). Max allowed: "
                f"{max_price_distance_cents}¢. Cap exists so a fat-finger "
                "doesn't sell at $0.99 on a $0.05 market."))

    if todays_order_count >= max_orders_per_day:
        issues.append(("block",
            f"Daily order cap reached ({todays_order_count}/{max_orders_per_day}). "
            "Anti-runaway guard — wait until UTC midnight or raise the cap."))

    if not ticker or len(ticker) < 3:
        issues.append(("block", f"Ticker looks malformed: {ticker!r}"))

    if side.strip().lower() not in ("yes", "no"):
        issues.append(("block", f"Side must be 'yes' or 'no' (got {side!r})."))

    return issues


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _make_client_order_id(ticker: str) -> str:
    """Client order id: a stable handle for one submission attempt.

    Kalshi de-dupes on this field, so re-sending the SAME id cannot
    double-fill. Note what that does and does not buy us: because a fresh
    UUID4 suffix is generated on every call, two calls produce two different
    ids and would be two different orders. This function therefore does not,
    on its own, make retries safe — the caller must reuse an id for that to
    hold. (It said otherwise until 2026-08-11; the claim was wrong. Writes are
    configured not to retry — ``kalshi_auth._authed_request`` defaults to
    ``retries=1`` — which is what actually prevented double-fills, so raising
    that retry count without threading a stable id through would introduce the
    bug the old comment claimed was already handled.)

    ``execution.LiveBroker._place`` generates the id itself and passes it in,
    both to keep it stable and so a submission that raises can be reconciled
    against the venue afterwards.

    The ticker is SANITIZED to [A-Za-z0-9_-]: the v2 events/orders endpoint
    400s ("invalid_parameters") on a client_order_id containing '.', and
    fractional-strike tickers (KXSOLD-26AUG0110-T72.9999, KXXRP-...-B1.0499500)
    contain dots — which made every fractional-strike market silently
    unorderable through this whole stack until 2026-08-01 (isolated by A/B
    probe: identical body, dotted coid 400s, sanitized coid accepted; 23/23
    canary errors vs 2/2 accepts partitioned exactly on the dot).

    >>> cid = _make_client_order_id("KXSOLD-26AUG0110-T72.9999")
    >>> cid.startswith("polymarket-KXSOLD-26AUG0110-T72_9999-")
    True
    >>> "." in cid
    False
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", ticker)
    return f"polymarket-{safe}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Kalshi v2 order schema
# ---------------------------------------------------------------------------

# The legacy POST/DELETE /portfolio/orders endpoint was deprecated in
# 2026-06 and now returns 410 "deprecated_v1_order_endpoint". The v2
# order resource lives at /portfolio/events/orders and quotes everything
# from the YES leg: ``side`` is a book side (``bid`` = buy YES, ``ask`` =
# sell YES) with a fixed-point dollar STRING price. There is no separate
# yes/no field — buying NO is economically selling YES at ``1 - price``,
# so the NO leg maps to the opposite book side at the complement price.
_ORDERS_ENDPOINT = "portfolio/events/orders"

# time_in_force is REQUIRED on v2 (it could be omitted on v1).
# good_till_canceled with no expiration_time is a true GTC order — the
# same behaviour the v1 path gave us by omitting the field.
DEFAULT_TIME_IN_FORCE = "good_till_canceled"
# self_trade_prevention_type is also required on v2. taker_at_cross
# cancels an incoming taker that would cross one of our own resting
# orders — the conservative choice for a single live account.
DEFAULT_STP = "taker_at_cross"


def _v2_book_side_and_price(side: str, action: str,
                            price_cents: int) -> tuple[str, str]:
    """Translate our (yes/no, buy/sell, integer-cents) order intent into
    the Kalshi v2 (book-side, dollar-string-price) pair.

    v2 quotes from the YES leg only, so the NO side is expressed as the
    opposite book side at the complement price (buy NO == sell YES at
    1 - price; sell NO == buy YES at 1 - price).

    >>> _v2_book_side_and_price("yes", "buy", 45)
    ('bid', '0.45')
    >>> _v2_book_side_and_price("yes", "sell", 99)
    ('ask', '0.99')
    >>> _v2_book_side_and_price("no", "buy", 30)
    ('ask', '0.70')
    >>> _v2_book_side_and_price("no", "sell", 95)
    ('bid', '0.05')
    """
    side_l, action_l = side.strip().lower(), action.strip().lower()
    if side_l == "yes":
        book_side = "bid" if action_l == "buy" else "ask"
        cents = price_cents
    else:  # NO leg → quote from the YES side at the complement price
        book_side = "ask" if action_l == "buy" else "bid"
        cents = 100 - price_cents
    return book_side, f"{cents / 100:.2f}"


def _build_v2_order_body(*, ticker: str, coid: str, side: str, action: str,
                         count: int, price_cents: int,
                         time_in_force: str = DEFAULT_TIME_IN_FORCE,
                         stp: str = DEFAULT_STP) -> dict[str, Any]:
    """Assemble a Kalshi v2 create-order request body from our order
    intent (yes/no side, buy/sell action, integer-cents limit)."""
    book_side, price = _v2_book_side_and_price(side, action, price_cents)
    return {
        "ticker": ticker,
        "client_order_id": coid,
        "side": book_side,
        "count": str(int(count)),
        "price": price,
        "time_in_force": time_in_force,
        "self_trade_prevention_type": stp,
    }


def place_limit_order(
    *,
    ticker: str,
    side: str,           # "yes" or "no"
    action: str,         # "buy" or "sell"
    count: int,
    limit_price_cents: int,
    client_order_id: str | None = None,
) -> dict:
    """Place a single GTC limit order on Kalshi.

    Returns the API response dict on success. Raises
    ``OrderRejected`` for local guard failures (live-trading disabled,
    profile lock, malformed args) and ``requests.HTTPError`` for
    Kalshi-side rejections (insufficient balance, market closed, …).
    """
    # Emergency-stop gate. Checked BEFORE the trading-enabled flag
    # because emergency stop is the more urgent failure mode and the
    # error message is more actionable.
    if emergency_stop.is_active():
        _st = emergency_stop.get_status() or {}
        raise OrderRejected(
            f"EMERGENCY STOP active — refusing order. "
            f"since={_st.get('since', '?')} "
            f"reason={_st.get('reason') or '(none)'}. "
            f"Run `python -m emergency_stop deactivate` to resume."
        )
    if not is_trading_enabled():
        raise OrderRejected(
            "live trading is not enabled — set KALSHI_LIVE_ENABLED=true "
            "and verify the active profile matches KALSHI_OWNER_PROFILE."
        )
    side_l = side.strip().lower()
    action_l = action.strip().lower()
    if side_l not in ("yes", "no"):
        raise OrderRejected(f"side must be 'yes' or 'no', got {side!r}")
    if action_l not in ("buy", "sell"):
        raise OrderRejected(f"action must be 'buy' or 'sell', got {action!r}")
    if count <= 0:
        raise OrderRejected(f"count must be positive, got {count}")
    if not (1 <= limit_price_cents <= 99):
        raise OrderRejected(
            f"limit_price_cents must be in [1, 99], got {limit_price_cents}"
        )
    if not ticker:
        raise OrderRejected("ticker is required")

    coid = client_order_id or _make_client_order_id(ticker)

    # Kalshi v2 order payload (see _build_v2_order_body). The body quotes
    # from the YES leg as bid/ask with a dollar-string price; the audit
    # record keeps the original intent alongside it for readability.
    body = _build_v2_order_body(
        ticker=ticker, coid=coid, side=side_l, action=action_l,
        count=count, price_cents=limit_price_cents,
    )

    # ── Mandatory venue-boundary caps ──────────────────────────────────
    # Enforced HERE, not only in pre_flight_check, so no caller — present
    # or future — can place an order these caps would block by simply not
    # running the advisory pre-flight. pre_flight_check remains the richer
    # check (drawdown, distance-from-mid, bankroll %) for callers that have
    # that context; this is the floor every order must clear.
    max_stake, max_per_day = mandatory_caps()
    if action_l == "buy":
        stake_dollars = (count * limit_price_cents) / 100.0
        if stake_dollars > max_stake:
            raise OrderRejected(
                f"stake ${stake_dollars:.2f} exceeds the per-order cap "
                f"${max_stake:.2f} (LIVE_MAX_STAKE_DOLLARS). Reduce count "
                "or raise the cap explicitly.")
        # Sells are exempt from the stake cap by design: an exit of an
        # existing position reduces risk and must not be blocked by its size.

    # Daily-cap reservation. The count check and the audit append happen
    # under one cross-process lock so concurrent processes cannot both
    # observe cap-1 and both submit; the appended `submit` row *is* the
    # reservation (it is counted whether or not the HTTP call later
    # succeeds — conservative, and unchanged from previous semantics).
    # The audit write is required (fail-closed): see _audit.
    with _submission_lock():
        today_n = _today_submit_count()
        if today_n >= max_per_day:
            raise OrderRejected(
                f"daily order cap reached ({today_n}/{max_per_day}). "
                "Anti-runaway guard — wait until UTC midnight or raise "
                "LIVE_MAX_ORDERS_PER_DAY explicitly.")
        # Audit *before* the call so a crash mid-request leaves a trail.
        _audit("submit", {"body": body, "intent": {
            "side": side_l, "action": action_l,
            "limit_price_cents": int(limit_price_cents)}}, required=True)
    try:
        resp = ka.authed_post(_ORDERS_ENDPOINT, body)
    except Exception as e:
        # Capture Kalshi's response body too — it carries the
        # human-readable rejection reason (insufficient_balance,
        # price_too_far_from_market, etc.) which str(e) doesn't.
        resp_body = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                resp_body = e.response.text[:500]
            except Exception as read_err:  # noqa: BLE001
                # The body carries the venue's actual rejection reason
                # (insufficient_balance, price_too_far_from_market, ...), which
                # str(e) does not. Losing it silently makes an order failure
                # much harder to diagnose after the fact.
                log.warning("could not read the error response body: %s", read_err)
        # Kalshi sharded the exchange into `exchange_index` venues
        # (0 Default / 1 Combos / 2 Crypto / 3 Tennis & Baseball). An order
        # for a market on a shard where the account holds no balance comes
        # back as a bare 404 `user_not_found`, which reads like a broken
        # endpoint rather than "your money is on a different shard".
        # Diagnose it here so the audit trail and the log say what to do.
        if "user_not_found" in resp_body:
            log.error(
                "%s is on a Kalshi exchange shard this account is not funded "
                "on — the 404 'user_not_found' means the SHARD is unfunded, "
                "not that the endpoint is wrong. Check the market's "
                "`exchange_index` against portfolio/balance's "
                "balance_breakdown, and move funds with an intra-exchange "
                "transfer before trading it.", ticker)
        _audit("error", {"client_order_id": coid, "error": str(e),
                          "response_body": resp_body})
        raise
    _audit("accepted", {"client_order_id": coid, "response": resp})
    log.info("Order placed: %s %s %sx%s@%s (coid=%s)",
             ticker, side_l, action_l, count, limit_price_cents, coid)
    return resp


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

def cash_out_position(
    *,
    ticker: str,
    held_side: str,           # "yes" or "no"
    count: int,                # contracts to sell
    current_bid_cents: int,    # required — we sell AT the bid
    client_order_id: str | None = None,
) -> dict:
    """Immediate sell-to-close at the current bid.

    Symmetric to ``place_take_profit`` but with the opposite intent:
    take whatever the market is offering right now. The order is
    placed as a limit at the bid price (most aggressive without
    crossing the spread further), which Kalshi treats as an
    immediately-marketable order and fills against the bid.

    Why a limit at the bid instead of a market order: Kalshi's API
    surface here is limit-only (no market type). A limit-at-the-bid
    behaves like a market order in practice — it crosses the spread
    and fills at the bid (you give up the spread to the maker).

    Pre-flight: every guard from ``place_limit_order`` applies EXCEPT
    the per-order stake cap (sells don't add risk). The drawdown
    breaker also doesn't apply — closing positions during drawdown is
    risk-reducing.

    Use cases: free up bankroll for another bet, lock in gains before
    the market could flip, or exit a position whose thesis is broken
    (the Senate-OR scenario today). For "set-and-forget at a higher
    price" use ``place_take_profit`` instead.
    """
    # Emergency-stop gate. Checked BEFORE the trading-enabled flag
    # because emergency stop is the more urgent failure mode and the
    # error message is more actionable.
    if emergency_stop.is_active():
        _st = emergency_stop.get_status() or {}
        raise OrderRejected(
            f"EMERGENCY STOP active — refusing order. "
            f"since={_st.get('since', '?')} "
            f"reason={_st.get('reason') or '(none)'}. "
            f"Run `python -m emergency_stop deactivate` to resume."
        )
    if not is_trading_enabled():
        raise OrderRejected(
            "live trading is not enabled — set KALSHI_LIVE_ENABLED=true "
            "and verify the active profile matches KALSHI_OWNER_PROFILE."
        )
    side_l = held_side.strip().lower()
    if side_l not in ("yes", "no"):
        raise OrderRejected(f"held_side must be 'yes' or 'no', got {held_side!r}")
    if count <= 0:
        raise OrderRejected(f"count must be positive, got {count}")
    if not (1 <= current_bid_cents <= 99):
        raise OrderRejected(
            f"current_bid_cents must be in [1, 99], got {current_bid_cents}. "
            "If the bid is zero, the market may be closed or illiquid — "
            "consider waiting rather than dumping into an empty book."
        )
    if not ticker:
        raise OrderRejected("ticker is required")

    coid = client_order_id or _make_client_order_id(f"cashout-{ticker}")

    body = _build_v2_order_body(
        ticker=ticker, coid=coid, side=side_l, action="sell",
        count=count, price_cents=current_bid_cents,
    )

    _audit("cashout_submit", {"body": body, "intent": {
        "side": side_l, "action": "sell",
        "limit_price_cents": int(current_bid_cents)}}, required=True)
    try:
        resp = ka.authed_post(_ORDERS_ENDPOINT, body)
    except Exception as e:
        resp_body = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                resp_body = e.response.text[:500]
            except Exception as read_err:  # noqa: BLE001
                # The body carries the venue's actual rejection reason
                # (insufficient_balance, price_too_far_from_market, ...), which
                # str(e) does not. Losing it silently makes an order failure
                # much harder to diagnose after the fact.
                log.warning("could not read the error response body: %s", read_err)
        _audit("cashout_error", {"client_order_id": coid, "error": str(e),
                                   "response_body": resp_body})
        raise
    _audit("cashout_accepted", {"client_order_id": coid, "response": resp})
    log.info("Cash-out placed: %s %s sell %sx@%s (coid=%s)",
             ticker, side_l, count, current_bid_cents, coid)
    return resp


def place_take_profit(
    *,
    ticker: str,
    held_side: str,           # "yes" or "no" — which side of the binary you hold
    count: int,                # contracts to sell (≤ current position)
    sell_price_cents: int,     # limit price on the SAME side as held_side
    current_bid_cents: int | None = None,  # for validation (optional)
    min_buffer_cents: int = 1,
    client_order_id: str | None = None,
) -> dict:
    """Place a sell-limit (take-profit) on a position you already hold.

    Mechanics: if you hold YES contracts on a market, a sell-YES at a
    higher YES price closes the position when the market reaches that
    price. Symmetric for NO. The order rests on Kalshi's book until
    filled or cancelled. Kalshi reserves the contracts; you can't
    re-sell them while the order rests.

    This is a primitive both the UI (manual button on Live Portfolio)
    and the automation layer (post-buy strategy hooks: "buy at X, set
    TP at Y") call directly.

    Pre-flight beyond ``place_limit_order``'s normal guards:
      - ``sell_price_cents`` must be ≥ ``current_bid_cents +
        min_buffer_cents`` when current_bid is supplied — otherwise
        the order fills immediately and there's no "take" in
        take-profit, just a market sell. Pass ``current_bid_cents=None``
        to skip this check (useful for tests or when the bid is
        unknowable).
      - Stake-cap doesn't apply to sells (sells reduce risk, not
        create it). The drawdown breaker also doesn't block —
        liquidating during drawdown is risk-reducing.

    Returns the Kalshi order response. Raises ``OrderRejected`` for
    local guard failures.
    """
    # Emergency-stop gate. Checked BEFORE the trading-enabled flag
    # because emergency stop is the more urgent failure mode and the
    # error message is more actionable.
    if emergency_stop.is_active():
        _st = emergency_stop.get_status() or {}
        raise OrderRejected(
            f"EMERGENCY STOP active — refusing order. "
            f"since={_st.get('since', '?')} "
            f"reason={_st.get('reason') or '(none)'}. "
            f"Run `python -m emergency_stop deactivate` to resume."
        )
    if not is_trading_enabled():
        raise OrderRejected(
            "live trading is not enabled — set KALSHI_LIVE_ENABLED=true "
            "and verify the active profile matches KALSHI_OWNER_PROFILE."
        )
    side_l = held_side.strip().lower()
    if side_l not in ("yes", "no"):
        raise OrderRejected(
            f"held_side must be 'yes' or 'no', got {held_side!r}"
        )
    if count <= 0:
        raise OrderRejected(f"count must be positive, got {count}")
    if not (1 <= sell_price_cents <= 99):
        raise OrderRejected(
            f"sell_price_cents must be in [1, 99], got {sell_price_cents}"
        )
    if not ticker:
        raise OrderRejected("ticker is required")

    if current_bid_cents is not None:
        if sell_price_cents < current_bid_cents + min_buffer_cents:
            raise OrderRejected(
                f"sell price {sell_price_cents}¢ is below the current bid "
                f"{current_bid_cents}¢ + buffer {min_buffer_cents}¢. Either "
                "raise the take-profit price or use a market sell."
            )

    coid = client_order_id or _make_client_order_id(f"tp-{ticker}")

    # Sell on the same side you hold, at the higher take-profit price.
    body = _build_v2_order_body(
        ticker=ticker, coid=coid, side=side_l, action="sell",
        count=count, price_cents=sell_price_cents,
    )

    _audit("tp_submit", {"body": body, "current_bid_cents": current_bid_cents,
                         "intent": {"side": side_l, "action": "sell",
                                    "limit_price_cents": int(sell_price_cents)}},
           required=True)
    try:
        resp = ka.authed_post(_ORDERS_ENDPOINT, body)
    except Exception as e:
        resp_body = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                resp_body = e.response.text[:500]
            except Exception as read_err:  # noqa: BLE001
                # The body carries the venue's actual rejection reason
                # (insufficient_balance, price_too_far_from_market, ...), which
                # str(e) does not. Losing it silently makes an order failure
                # much harder to diagnose after the fact.
                log.warning("could not read the error response body: %s", read_err)
        _audit("tp_error", {"client_order_id": coid, "error": str(e),
                              "response_body": resp_body})
        raise
    _audit("tp_accepted", {"client_order_id": coid, "response": resp})
    log.info("Take-profit placed: %s %s sell %sx@%s (coid=%s)",
             ticker, side_l, count, sell_price_cents, coid)
    return resp


def cancel_order(order_id: str) -> dict:
    """Cancel a single resting order by its Kalshi order_id (NOT the
    client_order_id). Use ``get_resting_orders()`` to find the id."""
    if not is_trading_enabled():
        raise OrderRejected(
            "live trading is not enabled — cannot cancel orders."
        )
    if not order_id:
        raise OrderRejected("order_id is required")
    _audit("cancel_submit", {"order_id": order_id})
    try:
        resp = ka.authed_delete(f"{_ORDERS_ENDPOINT}/{order_id}")
    except Exception as e:
        _audit("cancel_error", {"order_id": order_id, "error": str(e)})
        raise
    _audit("cancel_accepted", {"order_id": order_id, "response": resp})
    return resp


# ---------------------------------------------------------------------------
# Resting orders
# ---------------------------------------------------------------------------

def get_resting_orders(*, ticker: str | None = None,
                        limit: int = 200) -> pd.DataFrame:
    """Currently-resting (open / partially-filled) orders.

    Cols: ``order_id``, ``client_order_id``, ``ticker``, ``side``,
    ``action``, ``status``, ``count_remaining``, ``count_filled``,
    ``yes_price_cents``, ``no_price_cents``, ``created_time``.

    Read-only — doesn't require KALSHI_LIVE_ENABLED, only
    ``kalshi_auth.is_configured()``.
    """
    if not ka.is_configured():
        return pd.DataFrame()
    params: dict[str, Any] = {
        "limit": min(int(limit), 1000),
        "status": "resting",
    }
    if ticker:
        params["ticker"] = ticker
    items = ka.authed_paginate(
        "portfolio/orders", params=params,
        items_key="orders", max_items=int(limit),
    )
    if not items:
        return pd.DataFrame(columns=[
            "order_id", "client_order_id", "ticker", "side", "action",
            "status", "count_remaining", "count_filled",
            "yes_price_cents", "no_price_cents", "created_time",
        ])
    rows = []
    for o in items:
        rows.append({
            "order_id": o.get("order_id", ""),
            "client_order_id": o.get("client_order_id", ""),
            "ticker": o.get("ticker", ""),
            "side": o.get("side", ""),
            "action": o.get("action", ""),
            "status": o.get("status", ""),
            "count_remaining": int(o.get("remaining_count") or 0),
            "count_filled": int(o.get("queue_position") or 0),
            "yes_price_cents": int(o.get("yes_price") or 0),
            "no_price_cents": int(o.get("no_price") or 0),
            "created_time": o.get("created_time", ""),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["created_time_dt"] = pd.to_datetime(
            df["created_time"], errors="coerce", utc=True,
        )
        df = df.sort_values("created_time_dt", ascending=False).reset_index(drop=True)
    return df


__all__ = [
    "OrderRejected",
    "is_live_enabled",
    "is_trading_enabled",
    "mandatory_caps",
    "pre_flight_check",
    "place_limit_order",
    "place_take_profit",
    "cash_out_position",
    "cancel_order",
    "get_resting_orders",
    "DEFAULT_MAX_STAKE_DOLLARS",
    "DEFAULT_MAX_ORDERS_PER_DAY",
    "DEFAULT_MAX_PRICE_DISTANCE_CENTS",
]
