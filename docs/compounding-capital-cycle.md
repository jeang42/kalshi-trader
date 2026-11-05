# Compounding & the Capital Cycle — the next build target

*Written 2026-07-05. Canonical design doc for the "maximize compounding of slow
edges" direction. Status: **DESIGNED, GATED — do not build the sizing layer until
the fade edge confirms out-of-sample** (see the Gate). The measurement half
(capacity) is done and favorable.*

---

## The reframe (why this is the next target)

Every **fast** edge we probed on Kalshi is efficient/dead (MLB reaction, crypto
latency, sports game-outcomes, big econ — see `strategy-scoreboard.md`). The only
survivor is a **slow** behavioral edge: the favorite-longshot **fade** (retail
markets, and now sports props). Both take weeks to resolve.

The user's insight: **if we can't make trades resolve faster, make the *capital*
cycle faster instead.** Separate two things that feel like one:

- **Trade speed** — how long one bet takes to resolve (weeks; unfixable).
- **Portfolio throughput** — how fast the *whole book* turns over and grows (fixable).

A slow edge does **not** have to grow capital slowly. Growth rate stops depending
on trade speed and starts depending on three levers we *can* pull:

1. **Breadth** — many *independent* slow trades in parallel, staggered so
   something resolves nearly every day. The fades are already breadth machines
   (dozens of uncorrelated longshots).
2. **Reinvestment** — every resolution's proceeds redeploy immediately; capital
   is never idle. Geometric, not linear, growth.
3. **Sizing** — stake each position to maximize the long-run *growth rate*.
   That is literally the **Kelly criterion**; fractional-Kelly across many
   independent edges maximizes compounding without courting ruin.

**Punchline:** a 4-week edge across ~60 staggered, reinvested, Kelly-sized
positions compounds as fast as a 1-day edge traded one at a time. The fade
strategy is unusually well-suited because its whole nature is breadth — lots of
small independent behavioral mispricings, exactly the substrate compounding needs.

---

## The Gate (READ THIS BEFORE BUILDING)

**Compounding amplifies whatever edge you have — including a zero or negative
one.** Kelly is brutally unforgiving of an *overestimated* edge (it over-bets and
compounds toward ruin). Our backtest numbers (retail +12% ROI on 21 events; props
+9.5% on only 5–11 events) are exactly the kind of thin estimate you must NOT
Kelly-size on.

So the sizing/scaling layer is **gated on out-of-sample confirmation.** Concrete
trigger — build only when the *resolved* paper track (per arm) shows:

- **≥ ~20–25 independent resolved events** (grouped by event/tournament, not raw
  markets — one tournament field = ~1 independent observation).
- **Net-of-cost ROI positive with margin** — after fees *and* a realistic spread
  haircut (≥4¢), not mid-to-mid.
- **Win-by-event ≥ ~65–70%** (or an ROI confidence interval that excludes 0).
- **Not fat-tailed** — top-1-event share < ~25% of P&L.
- **Calibration direction holds** in the resolved sample (longshots still
  resolve YES *less* than priced).

These mirror the exact controls that validated the retail fade backtest. Until an
arm clears them, it stays flat-sized paper (or the bounded live canary). **If the
edge doesn't confirm, the compounding layer never gets built — no harm done.**

---

## The measurement half (DONE — capacity is not the wall)

The one compounding fact that *is* measurable today is **capacity**: these are
thin markets, and compounding is worthless if a fade book can only absorb a few
dollars before slippage eats the edge. Measured 2026-07-05
(`scratchpad/capacity_study.py` — walk the YES bids, NO cost = 1 − yes_bid,
sum $ deployable within a slippage tolerance):

| | median $/fade @2¢ | @5¢ | untradeable |
|---|---|---|---|
| Retail fade | $— | $— | 3 / 18 |
| Prop fade | **$—** | **$—** | 0 / 18 |

**Ceiling at current breadth (~76 open fades): ~$—k within 2¢ slippage, ~$—k+
within 5¢** — vs. ~$— currently deployed. That is **500–1000× headroom**, and it
**grows with breadth.**

Findings:
- **Capacity is NOT the binding constraint.** The wall is the *edge being real*,
  not the liquidity to deploy it.
- **Props are ~8× deeper than retail** ($— vs $— median/fade) → props are
  the more *scalable* compounding vehicle, not just the faster-validating one.

Caveats (honest):
- **Right-skewed** — mean ≫ median (a few deep books dominate); honest aggregate
  range ~$—k (median-based) to ~$—k (mean-based) at 2¢.
- **Prop capacity is correlated** — that depth is spread across ~11 tournaments
  with many correlated members, so *safely independent* deployable capital is
  less than the raw sum. **Correlation, not depth, becomes the sizing constraint.**
- **Book depth ≠ guaranteed fills** — resting liquidity can pull; live execution
  will run somewhat worse than a snapshot.

---

## The build (when the Gate opens)

A **capital-allocation / portfolio layer** over the surviving fade arms
(`pm/arms/fade_nearclose.py`, `pm/arms/back_nearclose.py`).

> **Retargeted 2026-08-23.** This layer was originally specified over
> `fade_retail_longshot.py` + `fade_props.py`. Both failed out-of-sample
> (−$— over 200 resolutions and −$— over 224) and were retired to
> `attic/`; see `docs/failed-experiments.md`. The design below is unchanged —
> it was never specific to those two arms — but every reference to "the fade
> arms" now means the near-close pair. **The Gate did its job**: it is the
> reason no real capital was ever sized onto an edge that did not exist.

Sketch:

1. **Per-edge parameters** — from the confirmed OOS track + the calibration band:
   estimate win-prob and payoff for each candidate fade.
2. **Fractional-Kelly stake** per candidate (quarter- or half-Kelly given estimate
   uncertainty — never full Kelly on a live-estimated edge), subject to:
   - **Correlation cap** — treat a tournament field / one-winner group as ~1
     independent bet; cap aggregate stake per *event*, not per market.
   - **Capacity cap** — never exceed the 2¢-slippage depth per fade (reuse the
     capacity-study walk on the live book).
   - **Bankroll / total-exposure cap** — hard ceiling; drawdown circuit-breaker.
3. **Reinvestment loop** — as positions resolve, freed capital + winnings redeploy
   into the best available fades each cycle; keep capital fully deployed across
   independent events (the throughput lever).
4. **Wire into the live path** — replace the arms' flat $—–5 canary sizing with the
   Kelly-derived, capacity-capped, correlation-aware stake. Ramp real money slowly
   (the live gates + `LIVE_MAX_*` caps stay in force).

Design principles: independence enables safe sizing (uncorrelated bets let you
size up without ruin); the correlation cap is the real limiter (not raw depth);
conservative Kelly fraction absorbs edge-estimate error; the whole thing degrades
to today's behavior if the edge estimate is weak.

---

## Status & next steps

- **2026-08-23:** the two arms this was written for **failed the Gate** and
  were retired. The Gate held — nothing was ever sized up. Cost of the whole
  exercise: paper P&L and one read-only capacity study.
- **Now → few months:** the near-close pair accrues resolutions on frozen
  rules (`python -m oos_ledger --status` for the clock; `fade_nearclose` frozen
  2026-07-19, `back_nearclose` 2026-08-06, both 0 edits). Nothing to touch.
  Watch the Gate metrics via each arm's `--stats`.
- **When an arm clears the Gate:** build the portfolio layer above.
- **If neither clears:** the edge wasn't real; the compounding layer is never
  built. That is the same outcome the retail/prop pair reached, and it is a
  success of the process, not a failure of it.

**Judge on mean P&L per independent cluster, never win rate.** The two retired
arms won most of their trades all the way down. This is the specific error the
Gate exists to catch.

**Related:** memory `[[project_compounding_slow_edges]]`,
`[[project_retail_longshot_fade]]`, `[[project_fade_arms_failed_oos]]`;
`strategy-scoreboard.md`, `docs/failed-experiments.md`;
`pm/arms/fade_nearclose.py`, `pm/arms/back_nearclose.py`.
