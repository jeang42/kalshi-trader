# Release audit — 03: Order-safety model

State of the live-order path after the hardening commits on this branch.

## Layered enforcement

```
caller (UI page / LiveBroker / script)
  │   advisory layer: pre_flight_check(...)  — richer context
  │     drawdown pause, distance-from-mid, bankroll % (advisory),
  │     caller-configured stake/daily caps (may be stricter than the floor)
  ▼
pm/venue/kalshi_orders.place_limit_order / cash_out_position / place_take_profit
  │   MANDATORY layer — cannot be skipped by any caller:
  │     1. emergency stop (file flag; fail-closed on OSError)
  │     2. live-trading gates: KALSHI_LIVE_ENABLED + kalshi_auth.is_configured()
  │        (key + PEM perms + active profile authorized; lookup failure now
  │        DENIES — fail-closed)
  │     3. argument validation (side/action/count/price band/ticker)
  │     4. per-order stake cap for BUYS (mandatory_caps(): LIVE_MAX_STAKE_DOLLARS,
  │        default $50) — sells exempt by design (exits must not be size-blocked)
  │     5. daily order cap for every place_limit_order order
  │        (LIVE_MAX_ORDERS_PER_DAY, default 10/UTC-day), check-and-reserve
  │        ATOMIC across processes (flock on <orders_log>.lock; fail-closed)
  │     6. fail-closed pre-submission audit write (unwritable log ⇒ no order)
  ▼
pm/venue/kalshi_auth._authed_request   — retries=1 (writes NEVER retry),
  │                                      shared circuit breaker (open ⇒ not sent)
  ▼
Kalshi v2 /portfolio/events/orders     — de-dupes on client_order_id
```

## Entry-point × control matrix

ES = emergency stop, GATES = live-enabled + authorized profile, ARGS =
validation, SC = stake cap, DC = daily cap, PFC = advisory pre-flight,
DD = drawdown, PA = `LIVE_TRADING_PROFILES` allow-list.

| Entry point | ES | GATES | ARGS | SC | DC | PFC | DD | PA |
|---|---|---|---|---|---|---|---|---|
| `LiveBroker` buy/sell (autonomous) | ✅ (get_broker + venue) | ✅ | ✅ | ✅ venue + stricter broker clamp | ✅ venue + broker PFC | ✅ | ✅ account-level, fail-closed | ✅ |
| `LiveBroker.place_resting_take_profit` | ✅ venue | ✅ | ✅ | exempt (sell) | ✅ venue | ❌ (sell; documented) | ❌ (sell) | ✅ |
| Kelly page buy (manual) | ✅ venue + UI banner | ✅ | ✅ | ✅ venue (+UI PFC $50) | ✅ venue + UI | ✅ | ⚠️ paper-snapshot based (see limitations) | ❌ **by design** (manual ≠ autonomous) |
| Live Portfolio take-profit / cash-out (manual sells) | ✅ venue | ✅ | ✅ | exempt (sell) | exempt (tp/cashout events uncounted — risk-reducing) | ❌ | ❌ | ❌ by design |
| Orders page cancel / `LiveBroker` cancel | ❌ **intentional** | ✅ | ✅ | n/a | n/a | n/a | n/a | ❌ |
| `scripts/smoke_test.py` | ✅ venue | ✅ | ✅ | ✅ venue | ✅ venue | ❌ (1 contract @ 1¢) | ❌ | ❌ |

**Why cancel skips the emergency stop:** during a panic the operator needs to
*pull* resting orders; blocking cancellation would be risk-increasing. The
emergency stop blocks everything that creates or re-prices exposure,
including sells (consistent with the drawdown breaker: when in doubt, halt,
and exit manually on Kalshi's own site).

## Manual vs. autonomous authorization (P1-C)

Intended matrix, now normative:

| Action | Requires |
|---|---|
| Authenticated **reads** (Live Portfolio, Orders page) | key + PEM + active profile ∈ {owner} ∪ `LIVE_TRADING_PROFILES` |
| **Manual** order (a human clicking, two-step ack, on the owner/authorized profile) | reads requirements + `KALSHI_LIVE_ENABLED=true` |
| **Autonomous** orders (a daemon via `get_broker`) | all of the above + the daemon's profile listed in `LIVE_TRADING_PROFILES` + emergency stop inactive |

Listing a profile in `LIVE_TRADING_PROFILES` authorizes that *daemon profile*
to trade autonomously; enabling `KALSHI_LIVE_ENABLED` alone authorizes only
attended, manual clicks. Neither implies the other direction. The venue-layer
mandatory caps apply to both identically.

## Order lifecycle and state reconciliation (P0-C)

1. **Id first.** `LiveBroker` generates the `client_order_id` *before*
   submitting; the id survives any failure. Kalshi de-dupes on it.
2. **Reserve + audit.** Daily-cap reservation and the `submit` audit row are
   written atomically before the HTTP call (fail-closed).
3. **Submit once.** Writes never retry (`retries=1`).
4. **Classify failures.**
   - `OrderRejected` — never sent (local guard). Confirmed.
   - `requests.HTTPError` — venue answered. Confirmed rejection.
   - circuit-breaker open — never sent. Confirmed.
   - `OrderOutcomeUnknown` — transport failure, no response. **Unconfirmed**;
     carries the client_order_id.
5. **Reconcile.** On any submission exception `LiveBroker` checks resting
   orders then fills for the client_order_id and *adopts* an order the venue
   actually has. Manual callers get the UI warning with the id and the
   Orders page. Nothing anywhere resubmits automatically.
6. **Fill polling + remainder cancel.** Unfilled remainders are cancelled;
   a cancel failure is surfaced on `OrderResult.detail` ("may still be
   resting") and in the audit log with an unknown/rejected classification —
   never assumed cancelled.
7. **Restart recovery.** Live positions/cash are re-read from the venue each
   cycle (`get_positions` / `available_cash`); the resting-order idempotency
   check stops a restarted daemon from stacking a second order on a ticker;
   the drawdown high-water mark persists across restarts; `SIGTERM` is
   masked across the place→record critical section (`shutdown.deferred`).

**Accepted limitation (documented, not fixed):** there is no dedicated
persistent "uncertain order" store. After a hard crash mid-submission the
recovery record is the audit log itself (a `submit` row with no
`accepted`/`error` row = investigate that client_order_id). For a
single-operator system whose daemons re-read venue truth every cycle, a
separate store would duplicate the venue's own order book as a second source
of truth. Future users scaling this up should revisit.

## Concurrency protection (P0-D)

- **Atomic:** daily order cap (flock reservation, fail-closed; regression
  test drives 10 threads at a cap of 5).
- **Venue-side:** `client_order_id` de-dupe; `self_trade_prevention_type`.
- **Advisory / non-atomic (bounded by the two above):** per-ticker
  resting-order dedupe (read-then-act), per-arm bankroll budgets
  (venue-truth reads, scoped by `position_meta`), account balance as the
  hard ceiling on total exposure.

## Safety invariants (enforced by tests)

1. No order without a persisted pre-submission audit row.
2. No buy above the per-order stake cap, from any caller.
3. No order beyond the daily cap, from any number of concurrent processes.
4. Emergency stop blocks every submission path; never blocks cancels.
5. A profile-authorization lookup failure denies, never allows.
6. A venue-silent submission is reported as UNKNOWN, never as rejected, and
   is never automatically resubmitted.
7. Tests cannot reach the network or the real data tree (root conftest).
