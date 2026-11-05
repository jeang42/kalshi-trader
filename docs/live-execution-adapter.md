# Live-execution adapter — design

> ⚠️ **Real money.** Everything below this line is about letting unattended
> software place actual orders with actual funds. You can lose your entire
> stake; no strategy in this repo has a demonstrated edge (see the research
> logs); bugs and venue changes happen. Read this document in full, keep
> stakes at canary size, and know where the emergency stop is
> (`python -m emergency_stop activate`) before enabling anything.

**Status:** built + wired, **dormant by default**. Flipping it on is a
deliberate, multi-gate human action (see *Turning it on*). Until then every
strategy daemon behaves exactly as it does today (paper writes).

## Goal

Let a strategy daemon place **real Kalshi orders** instead of paper-portfolio
writes, **without changing the strategy logic**, behind enough gates that it
cannot fire by accident, and with live fills reconciled against the venue's
truth (not assumed).

## The seam: one `Broker` interface, two implementations

Every daemon currently calls `portfolio.add_buy/add_sell` and reads
`portfolio.get_positions()` directly. We replace those touch-points with a
**`Broker`** the daemon obtains once per cycle via `execution.get_broker(profile)`:

```
Strategy decision  ──►  broker.buy / broker.sell      ──►  OrderResult
                        broker.get_positions()         (pf-shaped DataFrame)
                        broker.available_cash(bankroll)
```

| Method | `PaperBroker` (default) | `LiveBroker` (gated) |
|---|---|---|
| `buy/sell` | `portfolio.add_buy/sell` at the given price; instant fill | `kalshi_orders.place_limit_order` + pre-flight + fill reconciliation |
| `get_positions` | `portfolio.get_positions()` (paper JSON) | `kalshi_client.get_authed_positions()` normalized to pf shape |
| `available_cash` | `bankroll − deployed cost basis` (today's model) | `min(bankroll, settled balance)` from `get_balance` |

Because `PaperBroker` delegates to the exact functions used today, **paper mode
is byte-for-byte the current behavior** — the wiring is inert until a profile is
explicitly allow-listed for live.

## Gating — three independent switches, all required

`get_broker(profile)` returns a `LiveBroker` **only if all three** hold; any
one missing → `PaperBroker`:

1. `kalshi_orders.is_trading_enabled()` — `KALSHI_LIVE_ENABLED=true` **and**
   `kalshi_auth.is_configured()` (API key + PEM present **and** active profile ==
   `KALSHI_OWNER_PROFILE`; paper profiles physically can't sign).
2. **Profile allow-list** — the profile is in `LIVE_TRADING_PROFILES`
   (comma-separated env, default empty). This is the per-strategy on-switch: you
   turn ONE profile live, not "all daemons."
3. `emergency_stop.is_active()` is **False**.

`place_limit_order` *re-checks* (1) and emergency-stop itself, so even a wiring
bug that constructed a `LiveBroker` wrongly cannot place an order while the
flags are off. Defense in depth.

## Kalshi order-type reality (verified 2026-06-02)

Per the [Create Order API reference](https://docs.kalshi.com/api-reference/orders/create-order),
Kalshi's `type` accepts only **`limit`** and **`market`** — there is **no
stop / stop-loss / trigger order type**. Consequences for our exits:

- **Downside stop cannot be a resting venue order.** A limit sell *below* the
  market fills immediately (a limit sell means "sell at X *or better*"), so it
  can't express "sell only if the price falls to X." With no native stop, the
  −25% stop **must** be daemon-polled (what we built) — there is nothing to
  wire to the venue.
- **The reliability fix that matters:** when the polled stop fires, the daemon
  knows a bid that may be up to `max_quote_age_s` (120s) stale. A limit sell at
  that stale price can rest *above* a book that has since dropped → the stop
  silently fails to exit. So live exit sells are placed **marketable**: crossed
  down by `exit_cross_cents` (default 5¢) so they take whatever liquidity
  exists and actually fill. (A limit sell crossed through the bid behaves like
  a market sell while staying on the audited limit path — we never need the
  `market` type.)
- **Upside take-profit CAN rest** (a limit sell *above* entry is a normal
  resting order). `kalshi_orders.place_take_profit` exists for this. Wiring a
  resting TP for live scalp modes is a documented enhancement (see below); the
  first live strategy — favorite-hold — is buy-only and needs neither a stop
  nor a TP, so it is unaffected.

### Resting take-profit ("cash-out limit") for live scalp modes — BUILT

The close-game scalp's +25% take-profit rests on the venue in live mode (a GTC
limit sell at `entry × (1 + take_profit_pct)`), so the pop is captured with zero
poll latency and survives a daemon crash. The downside stop still can't rest
(no native stop) so it stays the polled marketable sell. Gated on
`broker.rests_take_profit` → **inert in paper** (polled TP unchanged) and in
favorite mode (holds to resolution, no TP), so the running favorite-hold live
arm is unaffected.

Surface (`execution.LiveBroker`): `place_resting_take_profit(ticker, outcome,
count, tp_price)` (rests a sell, not stake-capped — it's an exit), `cancel(order_id)`,
`fills_for_order(ticker, order_id)` (single-shot fill check). `PaperBroker` has
inert no-ops.

Coordination in `longshot_scalper` (all guarded by `broker.rests_take_profit`):
- **Entry** → `_arm_resting_tp` places the limit, stores `tp_order_id` (+ the
  question) in `position_meta` via `set_field`.
- **Upside pop** → the venue fills the resting limit; a new cycle-phase
  `_reconcile_resting_tps` detects the fill (`fills_for_order`), logs it as
  `scalp_take_profit`, and clears meta. The daemon's polled TP is **skipped** in
  live (`_manage_positions` returns early on a `scalp_take_profit` reason) so it
  can't race / double-sell.
- **Downside stop / flip / resolution** → `_cancel_resting_tp` cancels the limit
  *before* closing the position another way, so it can't fill afterward.

`_reconcile_resting_tps` runs as phase 0 (before settlement) so a filled TP is
recorded as a take-profit, not mistaken for a resolution. Wired only on
`_open_positions` entries (flip/freeroll re-buys don't arm a resting TP —
longshot is retired and close-game runs flip off, so those paths don't apply).

## Order mechanics

- **Limit only** (Kalshi policy here — no market orders). A buy places a
  *marketable limit* at `target_price + marketable_cross_cents` (default +0;
  raise to +1¢ to cross the spread and take liquidity) clamped to [1, 99]¢. A
  sell mirrors it downward.
- **Pre-flight** (`kalshi_orders.pre_flight_check`) runs first; any `block`
  severity aborts the order and returns `status="rejected"` with the issues. It
  enforces: live-enabled, drawdown pause, per-order stake cap, fat-finger
  distance-from-mid, daily order cap, ticker/side sanity.
- **Idempotency**: `client_order_id` (Kalshi de-dupes), so a network retry can't
  double-fill. The daemon's existing "already hold either side" guard prevents
  re-entry across cycles.
- **Fill reconciliation**: after placing, poll `get_fills(ticker)` filtered to
  our `order_id` for up to `fill_timeout_s` (default 8s), summing filled count
  and computing the volume-weighted avg fill price for our side. On timeout with
  an unfilled remainder, `cancel_unfilled` (default True) cancels the rest so no
  stale order rests. `OrderResult` reports `filled`/`partial`/`resting`.
- **State truth in live mode comes from the venue**, never the paper log:
  `get_positions` reads `get_authed_positions`, `available_cash` reads
  `get_balance`. The allow-list maps the one live profile 1:1 to the one Kalshi
  account.

## `OrderResult`

```
ok: bool
status: "paper" | "filled" | "partial" | "resting" | "rejected" | "error"
ticker / side / action / requested_count / filled_count
avg_fill_price: float | None      # dollars, our side
order_id: str | None
issues: list[(severity, message)] # pre-flight findings
detail: str
```

The daemon logs it and (in live mode) should record the **actual** filled count
and avg price into `position_meta`, not the requested values — fills can differ.

## What is wired now vs. next

- **Wired:** `longshot_scalper.py` (all buy/sell/position/cash touch-points) →
  this is the MLB **favorite-hold** path, the chosen first live candidate (slow,
  hold-to-resolution, one order per game — the forgiving case).
- **Wired:** `pm/arms/reactive_scalper.py` (in-game reaction speed arm) → first
  *high-frequency* live arm (`agentic_reactive`, $2/order, `LIVE_MAX_ORDERS_PER_DAY`
  raised to 100). Live since 2026-06-06.
- **Next (same pattern, not yet wired):** `pm/arms/strategy_runner.py` (WNBA/tennis
  favorite arms). Deliberately left for a second pass to keep the first change
  small and reviewable.

## Shutdown: the place-then-record window (added 2026-08-17, P2/R-20)

`systemctl stop` sends SIGTERM. Until R-20 no daemon handled it, so the
process died wherever it was — including between `place_limit_order`
returning and the caller recording the fill. `_reconcile_after_error`
covers a submission that *raises*; it cannot cover a process that vanishes
after the submission succeeded. Now:

* `LiveBroker._place` runs inside `shutdown.deferred(...)` — the whole
  place → poll-fills → cancel-remainder sequence is one critical section.
* Every caller wraps its own record step in the same way (the two arm
  canaries: `buy` + `UPDATE` + `commit`; `reactive_scalper`: entry, manage
  exit, resolution close). Nesting is fine — the mask is re-entrant.
* A SIGTERM inside the section is held; on exit the loop's stop flag is set
  and the daemon winds down after the current step. A second signal
  escalates to immediate exit.

**Any NEW live daemon must** call `shutdown.install()` in `main()`, loop on
`shutdown.requested()` / `shutdown.sleep()`, and wrap place-then-record in
`shutdown.deferred()`. This is a mask, not a guarantee: SIGKILL (systemd's
escalation after `TimeoutStopSec`, default 90 s) still kills — keep steps
short and check `requested()` inside long per-market loops.

## The two things EVERY live daemon must do (or it silently goes paper)

A daemon that is allow-listed for live but skips either of these falls back to
`PaperBroker` with no error — `get_broker()` just returns paper. This bit the
reactive arm on its first go-live (fixed 2026-06-06):

1. **`load_dotenv()` in `main()`** — the systemd units have no `EnvironmentFile`,
   so without this the daemon process never sees `KALSHI_LIVE_ENABLED` /
   `LIVE_TRADING_PROFILES` / `LIVE_MAX_STAKE_DOLLARS` → the live gate can never
   pass.
2. **Pin `POLYMARKET_ACTIVE_PROFILE` to the daemon's own profile per cycle**
   (save/restore around the cycle) — else the `kalshi_auth` owner-lock
   (`is_configured()` → `active_profile() ∈ authorized`) resolves against
   whatever profile the *UI* last selected, not the daemon's, and `is_configured()`
   returns False → paper.

**Verify it actually took:** after go-live, the daemon should log
`execution: LIVE broker active for profile <p> (max_stake=$X)` every cycle. Zero
of those lines = it's still on paper regardless of what `go_live.sh` reported.

## Turning it on (the human runbook)

This is the owner's call, not the agent's. **Daemons load `.env`** and pin
`POLYMARKET_ACTIVE_PROFILE` per cycle (see the section above —
`longshot_scalper`, `reactive_scalper`, `live_preflight` all do this), so the
daemon reuses the exact credentials the UI already uses and the owner-lock
resolves to the daemon's profile rather than whatever the UI last selected.

Prerequisite (one time): Kalshi API key + RSA PEM at `KALSHI_PRIVATE_KEY_PATH`
(`chmod 600`), `KALSHI_API_KEY_ID` in `.env`, and a funded account.

**Owner-claim semantics:** the live account has ONE owner profile. Taking a
daemon live points `KALSHI_OWNER_PROFILE` at that daemon's profile — which means
the UI can then trade live only when *that* profile is active. One owner at a
time, by design.

To take **favorite-hold** live at minimum size, use the guarded helper:

```
# dry-run: read-only preflight for the profile, changes nothing
./go_live.sh --profile agentic_favorite_hold

# enable (real money): only proceeds if preflight passes. Upserts
# KALSHI_LIVE_ENABLED=true, KALSHI_OWNER_PROFILE + LIVE_TRADING_PROFILES =
# the profile, LIVE_MAX_STAKE_DOLLARS, then restarts the service.
./go_live.sh --profile agentic_favorite_hold --service favorite-hold \
             --max-stake 2 --enable
```

`go_live.sh` runs `pm/trading/live_preflight.py` first and **refuses to enable unless every
read check is ✓** — that verifies the owner-lock + live response shapes before a
single order. After enabling, watch `data/shared/orders.log` (audit) and compare
live fills to the paper twin for a canary period before raising `--max-stake`.

Manual equivalent if you prefer: `.env` → `KALSHI_LIVE_ENABLED=true`,
`KALSHI_OWNER_PROFILE=<profile>`, `LIVE_TRADING_PROFILES=<profile>`,
`LIVE_MAX_STAKE_DOLLARS=2`; run `python live_preflight.py --profile <profile>`;
restart the service.

To stop instantly at any time: `python -m emergency_stop activate` — every
order path checks it first.

## Running multiple live arms on one account (position-scoping)

The venue-truth reads (`get_positions` / `available_cash`) describe the WHOLE
Kalshi account. With one live arm that's fine; with two it's dangerous — one
arm's exit logic (stop / take-profit) would iterate and close the *other* arm's
positions, and both would deploy against the same balance.

`LiveExecConfig.scope_to_own_positions` (default **True**) fixes this: a
`LiveBroker.get_positions()` returns only positions this arm opened — those
tracked in its (profile-pinned) `position_meta` (`_own_tickers()`). So
`_manage_positions`, `_close_resolved`, and `available_cash` all act only on the
arm's own holds. `available_cash` additionally subtracts the arm's own deployed
cost from its bankroll budget (the shared balance still caps total account
exposure). Net effect: each daemon behaves as if it owns a slice of the account,
and never touches another arm's — or your hand-traded — positions.

**Multi-arm live is now supported on one account.** The owner-profile lock was
relaxed: `kalshi_auth.authorized_profiles()` = the owner (`KALSHI_OWNER_PROFILE`)
**plus** every profile in `LIVE_TRADING_PROFILES`, and `is_configured()` accepts
any authorized active profile. So several live daemons each authenticate against
the one account (each pins its own profile), position-scoping keeps them off
each other's holds, and `available_cash` keeps each within its bankroll while the
shared balance caps the total. Paper profiles (not owner, not allow-listed) stay
locked out exactly as before.

To add a second live arm: add its profile to `LIVE_TRADING_PROFILES` (comma-sep)
and restart its service. No subaccounts needed for the single-account case;
per-strategy Kalshi subaccounts remain an option if you later want hard
balance/risk isolation. **Gate any new live arm on its own validation first** —
e.g. the close-game scalp should clear its 2026-06-16 A/B check before it's
added to the allow-list.

## Self-review: flaws found in the first draft & resolved

Reviewing the draft before building surfaced three correctness bugs and two
enhancements, now baked into the design:

1. **(bug) Resolution closes must NOT place an order in live mode.** The paper
   daemon "sells at $1/$0" when a game goes Final. On Kalshi a resolved market
   **auto-settles** — placing a limit sell at $0/$1 (which would clamp to
   [1,99]¢ and get rejected anyway) is wrong. Fix: the broker exposes
   `closes_settle_on_venue` / a `settle_close()` no-op for live. `_close_resolved`
   in live mode places **no order** — it confirms the position left
   `get_positions` (settled), clears local meta, and logs. **Consequence, and a
   safety win: live favorite-hold is buy-only execution** — its sole exit is
   resolution, so it never places a sell. The first live strategy is the
   simplest possible: place buys, let the venue settle.
2. **(bug) Integer contracts.** Paper uses fractional shares (`stake/price`);
   Kalshi trades whole contracts. Live floors `count = floor(stake / limit$)`
   and may deploy slightly less than the nominal stake. `count == 0` → skip.
   Paper keeps fractional shares for continuity with the running paper arms.
3. **(bug) Marketable price + clamp.** Buy limit = `round(target¢) +
   marketable_cross_cents`, clamped [1,99]; a target ≥ 0.99 or ≤ 0.01 is handled
   rather than crashing.
4. **(enhancement) Resting-order idempotency.** Before placing, the live buy
   checks `get_resting_orders(ticker)` and skips if one already rests for that
   ticker — so a slow-to-fill order isn't re-placed every 15s, which would burn
   the daily order cap. Complements `client_order_id` de-dupe.
5. **(enhancement) `is_live` on the broker** so daemons can branch the few
   places that genuinely differ (resolution settlement) without sniffing env.

## Risks this design accepts (and the mitigations)

- **Partial / no fill on a scalp.** A 15s scalper that can't get filled in 8s
  cancels and misses the trade — acceptable (a missed scalp beats a bad fill).
  This is *why* favorite-hold goes first: it tolerates a resting order.
- **Position drift between paper twin and venue.** Live reads venue truth each
  cycle, so the daemon self-heals to actual holdings; it never trusts the paper
  log in live mode.
- **Drawdown gating not yet auto-fed.** `pre_flight_check` accepts
  `in_drawdown_pause`; the adapter currently passes `False`. Wiring it to
  `risk.current_drawdown` over the live balance history is a tracked follow-up;
  until then the per-order + daily caps are the runaway guards.
- **No live API in CI.** `LiveBroker` is unit-tested entirely with mocked
  `kalshi_orders`/`kalshi_client` calls (never touches the network); the canary
  period is the real integration test.
