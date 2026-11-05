# Failed Experiments — Post-mortems

A living record of strategies and hypotheses that were tried, measured,
and found to not work. Kept because:

1. **Knowing what doesn't work is half the edge.** Future-me will want
   to revisit "what about oscillation mean-reversion?" — this saves a
   re-run.
2. **Failed reference strategies are useful in backtests.** They give
   you a baseline that's worse than `market_consensus`, which sharpens
   the comparison when a new strategy lands.
3. **The post-mortem itself is the artifact.** The hypothesis, the
   measurement, and the why-it-failed analysis is the durable output.

Entries are added oldest-first, never rewritten retrospectively. When a
later experiment validates or contradicts an earlier finding, add a
follow-up entry rather than editing the original.

---

## strategy_oscillation_revert — Mean-reversion on range-bound markets

**Status**: ❌ failed (2026-05-23). Kept in the strategy registry as a
known-bad reference. The runtime invocation logs a `WARNING` once per
process to catch any cron line that accidentally re-uses it.

### Hypothesis

Markets that demonstrate range-bound oscillation over their recent
history (high `detrended_std / drift` ratio + frequent mean crossings +
meaningful absolute range) will continue oscillating around the same
mean. The current price's z-score against that mean is informative:
prices far below mean bounce up; far above bounce down.

### Decision tree (the strategy as implemented)

```
1. Compute over snap.prices:
     - detrended_std  = std of residuals from a linear fit
     - drift_abs      = |slope| of that fit
     - range_score    = detrended_std / max(|drift|, ε)
     - mean_cross_freq = sign changes of (price - mean) per day
     - range_width    = max(price) − min(price)

2. Gate (must pass all):
     range_score    ≥ 0.5
     mean_cross_freq ≥ 0.5 / day
     range_width    ≥ 0.05
     avg_price      ∈ [0.05, 0.95]

3. z = (current_price − avg_price) / detrended_std

4. z ≤ −1.0 → YES at probability ≈ avg_price + edge
   z ≥ +1.0 → NO  at probability ≈ avg_price − edge
   otherwise → no signal
```

### Backtest (the falsification)

| Setup | Value |
|---|---|
| Provider | Kalshi |
| Window | last 90 days, settled markets, volume ≥ $— |
| Universe | 5000 fetched → 2155 with valid resolutions → 1680 scored |
| Lead time | 48 hours pre-resolution |
| Fees | Kalshi formula, per-trade |
| Sizing | half-Kelly, half_spread $0.005 |

| Metric | Strategy | Baseline (market_consensus) | Market-baseline Brier |
|---|---|---|---|
| Trades fired | 60 | 164 | — |
| Hit rate | **25.0%** | 89.0% | — |
| Total P&L | **−$—** | +$— | — |
| ROI | **−21.9%** | +2.4% | — |
| Brier | 0.169 | — | **0.079** |

### Per-bucket result (by range_score quintile)

| Bkt | Score range | N mkts | Fired | Hit% | P&L |
|---|---|---|---|---|---|
| 0 | 0.00–0.20 | 336 | 2 | 50% | −$— |
| 1 | 0.20–0.33 | 336 | 21 | 24% | −$— |
| **2** | **0.33–0.56** | 336 | 12 | **8%** | **−$—** |
| 3 | 0.56–1.10 | 336 | 11 | 27% | −$— |
| 4 | 1.11–85.68 | 336 | 14 | 36% | −$— |

No bucket was profitable. The supposed-best bucket (highest
`range_score`) hit 36% — still well below break-even. The scanner's
ranking metric (`range_score`) is **not informative**.

### Why it failed

At 48 hours pre-close, Kalshi prices are highly informative about
resolution direction. The market-baseline Brier of **0.079** means
"just trust the current price as the probability" is nearly
calibrated — the market has done the work. A market that wobbled for
30 days then sits at $0.30 isn't mispriced; **it's correctly pricing a
probable NO outcome**. The strategy's mean-anchored z-score
interpretation ("price is low → bet YES at the mean") fights the
information already in the price and gets resolved at $0.

Strategy Brier of 0.169 is 2.15× worse than the market baseline —
we're producing **predictions less accurate than the market's own
implied probabilities**, then sizing them with Kelly. That's a losing
construction by design.

### What we'd try instead (NOT shipped — open questions)

- **Push lead time WAY out** (10+ days). At long lead, prices contain
  less resolution information and more noise; mean-reversion might
  have a window where it works. We started a 10-day backtest but
  killed it due to Kalshi rate-limit pressure during cron rollover.
  Re-running this overnight is a TODO.
- **Invert the signal**: if markets at 48h are RIGHT, *follow* the
  mean (momentum) instead of fighting it. Flip the YES/NO signs in
  step 4 of the decision tree. Cheap to test against the cached
  dataset.
- **Use `range_score × range_width` as the composite metric**. The
  initial scanner result hinted that `range_width` (absolute price
  movement) was the more informative half of the pair.
- **Trade only when divergence is paired with an external signal**
  (news event, scheduled catalyst, etc.). The pure-price signal isn't
  enough; needs orthogonal information.

### Provenance

- Strategy: `oscillation_scanner.strategy_oscillation_revert`
- Registered key: `oscillation_revert`
- Backtest module: `pm/research/oscillation_backtest.py`
- Cached dataset:
  `data/shared/backtest_cache/oscillation_kalshi_since90_until0_lead48_hist30_fid60.pkl`
- This post-mortem written: 2026-05-23

---

## Baseball model v0.5 pitching weights (2026-05-25)

### Hypothesis

Starting-pitcher ERA and bullpen ERA should improve single-game win
predictions when added as log-odds adjustments on top of team Pythagorean
strength and home-field advantage.

### What we tried

- **v0.1–v0.4**: Raw SP ERA → log-odds adjustment at
  `ERA_LOGODDS_PER_RUN = 0.13` (a 2-run ERA gap ≈ 5pp at 50/50).
- **v0.5**: Added Bayesian ERA regression (`ERA_PRIOR_IP = 40`) to tame
  small-sample ERAs (e.g. 17.18 on 3.2 IP → regressed to 5.25). Also
  added bullpen ERA at half-weight (`BULLPEN_LOGODDS_PER_RUN = 0.065`).

### What we measured (n=42 settled games)

| Model variant                  | Brier  |
|---                             |---     |
| Coin flip (0.50)               | 0.2500 |
| Flat 54% home wins             | 0.2516 |
| Team strength + HFA only       | 0.2559 |
| Full model minus all pitching  | 0.2544 |
| Full model minus SP            | 0.2606 |
| **Full model (as shipped)**    | **0.2632** |

Every pitching layer made Brier **worse**. Component-level analysis:

- **SP ERA**: r = 0.001 with outcome. Zero signal.
- **Bullpen ERA**: r(bp_delta, strength_delta) = 0.636 — it's a proxy for
  team quality already captured by Pythagorean. The apparent negative
  outcome correlation (97.3% bootstrap) is confounding, not real signal.
- **Home-field advantage**: 54% assumed, 50.0% actual in sample. Halving
  to 52% improved Brier by 0.0006.

The Kalshi market (Brier 0.277 on calibration-log entries) strictly
dominated the model (0.310) at every blending ratio.

### Why it failed

1. Season-level ERA is too coarse for single-game prediction. The factors
   that actually move single games (recent form, platoon splits, weather,
   travel, umpire tendencies) aren't in the model.
2. Bullpen ERA double-counted team quality already in the Pythagorean
   record (correlation 0.636).
3. Small-sample ERA (even after Bayesian regression) pushes predictions
   around randomly — noise that can't be offset by the weak directional
   signal.

### Resolution (v0.6)

- `ERA_LOGODDS_PER_RUN` → 0.0 (disabled)
- `BULLPEN_LOGODDS_PER_RUN` → 0.0 (disabled)
- `HOME_FIELD_LOGODDS` halved: ln(0.52/0.48) instead of ln(0.54/0.46)
- `LINEUP_LOGODDS_PER_OPS` reduced to 0.75× (2.25 from 3.0) — the one
  factor with correct-direction signal (r=0.126).
- Code retained at zero weight for re-evaluation at n=150+.

### References

- Prediction state: `data/shared/baseball_state.sqlite`, `predictions` table
- Calibration log: `agentic_baseball` profile, source `agentic:baseball_winprob`
- Analysis ran: 2026-05-25, this session

---

## favorite-hold — Buy & hold moderate MLB favorites (2026-06-28)

### Hypothesis

The market underprices moderate favorites: buy the side priced in the
consensus 0.55–0.70 band early and hold to resolution. The data-crunch
read claimed 0.55–0.70 sides won ~80%, implying a large +EV edge.

### What we tried

`longshot_scalper.py --mode favorite --profile agentic_favorite_hold
--forecast-source consensus`, $—/trade, hold to $1/$0. Ran ~4 weeks
(2026-06-02 → 06-28), 164 settled paper trades.

### What we measured (n=164 settled)

- **Net −$—, 55% win rate at ~0.60 avg entry.**
- Calibration by entry band — every band won AT OR BELOW its implied price:

  | Entry band | n | Win% | Implied | Net |
  |---|---|---|---|---|
  | 0.55–0.60 | 92 | 54% | ~57% | −$— |
  | 0.60–0.65 | 48 | 58% | ~62% | −$— |
  | 0.65–0.70 | 17 | 41% | ~67% | −$— |
  | 0.70–0.75 | 5 | 80% | ~72% | +$— |

### Why it failed

- **The thesis is backwards.** Moderate favorites do NOT win above their
  price — they win at or slightly below it. Kalshi MLB is efficient
  (matches the `inefficiency_sweep` finding: sports Brier ≈ 0.005).
- **Asymmetric payoff makes the breakeven win rate ≈ the entry price.**
  Buy at $0.60: a win nets +$0.40/share, a loss costs −$0.60/share, so
  breakeven ≈ 60%. Winning 55% → `EV = 0.55×0.40 − 0.45×0.60 ≈ −$0.05/$1`
  → ≈ −$— per $— trade × 164 ≈ −$— matching the realized −$—.
- The arm peaked +$— on 06-17 then gave it all back. Stakes were a
  constant $— throughout (NOT a sizing bug) — it was a marginal/negative
  EV strategy with ±$— per-trade variance, so a week-long drawdown is
  expected, not anomalous.

### Decision

Retired 2026-06-28: `systemctl --user disable --now favorite-hold.service`.
Same category as the retired close-game and longshot arms — no edge on
the efficient MLB market. The only validated edge remains in-game
reaction SPEED (reactive-hold / reactive-scalper), not final-outcome
pricing. Do not re-try favorite-band hold-to-resolution variants.

### References

- Exits: `data/agentic_favorite_hold/exits.jsonl` (164 rows)
- Related: `pm/research/inefficiency_sweep.py` (MLB efficient), close-game/longshot retirements
- Analysis ran: 2026-06-28, this session

---

## In-game reaction-ENTRY edge — falsified by captured-edge test (2026-06-28)

### Hypothesis

The project's central bet for weeks: after a high-impact play (big WP swing),
the Kalshi book is slow to reprice, so the benefiting side is buyable below
fair value for ~60s. Buy it fast, hold to resolution. Prior support:
`reaction_decay.py` (+2.5¢ high-leverage, persists ~60s), `reaction_capacity.py`
(thin-book +7.7¢), and a +$— paper / +$— live track record.

### What we tried (the right metric)

Reframed (outside review, `docs/external-review-brief.md`) from sports
prediction to market microstructure. Built `captured_edge.py` — for every
information shock measure **CapturedEdge = fair − executable_ask**, a
low-variance metric that isolates entry skill from the binary $1/$0 outcome
that made PnL uninformative at our sample size. 2,488 shocks / 490 games.

### What we measured

- **Equilibrium (median settle-window mid) validated as unbiased fair value**:
  mean 0.659 vs actual win rate 0.657; near-perfect calibration curve
  (eq 0.6-0.7 → 0.66 observed, 0.9-1.0 → 0.95). So the metric is trustworthy.
- **No entry edge net of spread.** vs equilibrium: mean −2.2¢, 19% positive.
  vs RESOLUTION (ground truth, n=2307): mean **−0.4¢ gross / −0.9¢ net-of-fee**.
- The resolution view's 66%-win / +7¢-median is the **favorite-payoff illusion**
  (win often, pay ~true probability, losses bigger than wins → mean EV negative)
  — the same trap that killed favorite-hold.
- **Worst exactly where we thought it was best**: high-leverage −3.65¢, underdog
  −2.65¢, wide pre-spread −2.84¢. Bigger event → bigger ask spike → more overpay.
- **Faster is worse**: decay curve more negative at 15s than 120s (the ask spikes
  right after the play, then relaxes) → latency was never the lever.

### Why it failed

The "+2.5¢ lag" was measured **mid-to-mid**. You can't trade the mid — you pay
the **ask**, which already reflects the move. There is no cheap lagging ask to
pick off; you just pay the spread (~3¢). The lag exists in the mid but is not
executable. The +$— paper / +$— live were fat-tail variance around a
~fair-minus-cost entry, not an edge (see the reactive-hold fat-tail analysis).

### Decision

Live arm PAUSED to paper (`LIVE_TRADING_PROFILES` emptied 2026-06-28; open
positions auto-settle). This closes the LAST surviving edge hypothesis — every
probe (prediction, favorite, close-game, longshot, favorite-longshot,
crypto-latency, news-speed, tennis-reaction, MLB reaction-entry) now finds
Kalshi efficient at executable prices. **Do not re-optimize reaction speed.**
Pivot: hunt OTHER delayed-price-discovery mechanisms (settlement-timing,
correlated-market lag, liquidity withdrawal) using the captured-edge engine,
which is now built and validated as a reusable measurement tool.

### References

- `captured_edge.py` + `data/shared/captured_edge.sqlite` (the dataset)
- `docs/external-review-brief.md` (the reframe that prompted this)
- Analysis ran: 2026-06-28, this session

---

## Market-making + the non-MLB hunt — Kalshi is structurally efficient for us (2026-06-29)

### Context

After every TAKER edge died paying the spread (see the captured-edge post-mortem),
we tested the inversion (earn the spread as a maker) and the obvious objection
(maybe non-MLB corners are looser).

### Market-making (maker_sim.py / maker_sim_feedaware.py)

Replayed the executed-trade tape against quotes we'd have rested at ±q around
the book mid, marked to the near-future mid (the "maker who flattens" P&L),
over the 120 busiest MLB markets (1.7M trades).

- **Blind making: net −2.9¢ to −3.5¢/fill**, and it gets WORSE with wider q
  (adverse selection grows faster than the spread earned — the signature of
  toxic/informed flow). 3:1 sell:buy fill imbalance = nasty inventory risk too.
- **Feed-protected making** (suppress fills within ±90s of a scoring play, i.e.
  "we pulled our quote"): adverse selection IS play-concentrated (near-play
  fills −6 to −7¢ vs quiet −2¢), so pulling helps — but **quiet-time fills are
  still −2¢/fill**. The toxicity is everywhere, just thicker around plays.

So both sides of the spread lose: taker loses the spread, maker loses to
adverse selection. The spread is fair compensation for adverse selection we'd
be on the wrong side of.

### Non-MLB hunt (venue scan + econ_capture.py + settlement_lag_probe.py)

- Venue scan found non-MLB *loose* corners (4–12¢ spreads, real volume, no HFT)
  in politics/entertainment/macro — but they are ALL long-dated futures.
- Markets resolving within 14 days = **entirely sports + crypto** (0 non-sport/
  non-crypto near-term catalysts). All listed econ markets close 900+ days out.
- The $—M Fed-chair market was priced ~$0.99 eight days before settling
  (anticipated → smooth, efficient).
- settlement_lag_probe.py: the "buy the certain winner post-game" +11¢/$—k
  "edge" was an artifact (stale halted books + a last-play≠final-out proxy +
  hindsight winner-selection); score reconstruction killed it.

### The structural conclusion

**Catalyst-richness and looseness are anti-correlated on Kalshi.** Sharp
near-term catalysts → liquid/HFT-watched → efficient. Loose markets →
long-dated diffuse futures → no exploitable catalyst, spread too wide. The
intersection we'd need (loose + sharp catalyst) doesn't exist, because a sharp
catalyst attracts the pros who tighten it. There is no mispricing/timing edge
accessible to us on Kalshi, from either side of the spread, in any corner.

### Decision

Stop probing Kalshi for a mispricing edge — it's structurally absent for us.
The tools are kept as reusable null-result instruments (captured_edge.py,
maker_sim*.py, settlement_lag_probe.py, econ_capture.py — all in `attic/`
since 2026-08-17, see `attic/README.md`). See
[[project_kalshi_structural_efficiency]]. The remaining paths are strategic
(genuine private-info/structural edge, different venue, or revisit the goal),
not another probe.

---

## Favourite-longshot fades at a multi-day lead — retail + props (2026-08-23)

The most expensive lesson in the log, because it is the one that had already
passed every test we knew how to apply.

### Hypothesis

Retail overpays for exciting longshots in markets no arbitrageur bothers to
correct (primaries, awards, music, "will X say Y" mentions; and separately,
sports **props** — golf top-N / round-leader, World Cup player-goal — as
opposed to the efficient game outcomes we had already killed). Fade it: buy NO
on the overpriced longshot at a multi-day lead and hold to resolution.

### What we tried

Two forward paper arms, each running its own discovery, both holding to
resolution:

- `fade_retail_longshot.py` — YES ∈ [0.12, 0.45], non-sport/non-crypto, and
  explicitly excluding scheduled macro (CPI/GDP/Fed — efficient, not the
  behavioural corner). Close-time discovery, per-event caps, prop-overround
  targeting on strongly-overround fields.
- `fade_props.py` — YES ∈ [0.10, 0.32], per-SERIES discovery (props never
  surface in the volume-ordered or close-time feeds), capped at 4 per
  tournament because a field is ~150 correlated members.

### What the backtest said

`fade_retail_backtest.py`, grouping by independent EVENT rather than by market:
**+11.8% ROI net of a 4¢ spread haircut, 86% win by event (18/21), top-1-event
share 15%** — explicitly not fat-tailed. Entertainment +34%, Elections +20%.
The prop version was thinner (5–11 independent events) and flagged PROMISING
rather than confirmed, which is why it was paper-only from the start.

### What we measured (forward, out-of-sample)

| Arm | Resolved | Net P&L | Mean per position |
|---|---|---|---|
| `fade_retail_longshot` | 200 | **−$—** | −$— |
| `fade_props` | 224 | **−$—** | −$0.75 |

Both were called failed on 2026-07-30 (at −$— / −$—) and deliberately left
running to grow the sample, on the argument that only 40 of 160 retail
positions had settled and mostly from the fast `say_mention` slice — the August
awards and primaries wave was the real test. That wave arrived. The arms lost
roughly three times as much again. The 2026-07-30 verdict was right.

### Why it failed

1. **The backtest measured a bias that exists; the arms needed an edge that
   pays.** The favourite-longshot gap is real and reproducible at a multi-day
   lead. It is not, at that lead, larger than the cost of crossing a thin
   retail spread repeatedly and carrying the position for days.
2. **21 independent events was not enough**, even de-correlated. Grouping by
   event fixed the *correlation* error and left the *sample-size* error
   untouched — 18/21 and 86% is a comfortable-looking number with a very wide
   interval behind it.
3. **Win rate hid it, again.** Both arms won the majority of their trades. The
   losses were concentrated in the resolutions that went against a fade priced
   near the top of the band, where one loss eats many wins. This is the same
   failure shape as `reactive_hold` and it was not recognised faster the second
   time.
4. **The honesty controls were all real and all insufficient.** Executable
   prices, fees, spread haircut, de-correlation by event, a documented
   behavioural mechanism — every one necessary, none sufficient. Nothing short
   of the forward sample distinguished this from a live edge.

### What survived

The **fast round-leader props** (`KXPGAR1LEAD`, `KXPGAR3LEAD`) never went
negative. They were also never isolated into their own arm, so this is a
promising cut, not a result — and it is a small enough corner that it may not
be worth an arm. Anyone reviving this should start there and nowhere else.

### Decision

Both arms **retired to `attic/` on 2026-08-23** — services stopped, disabled,
unit files removed from `~/.config/systemd/user` and `deploy/systemd/`
(archived under `attic/deploy/systemd/`). Their SQLite stores
(`data/shared/fade_retail.sqlite`, `fade_props.sqlite`) are **retained** as the
evidence; their tests moved to `attic/tests/test_fade_retail_props.py`.

The thesis is not dead in general — the same bias is what `fade_nearclose` and
`back_nearclose` harvest at a **2-hour** lead, and those two are positive on
frozen rules. What is dead is harvesting it *days* out. The compounding /
fractional-Kelly layer (`docs/compounding-capital-cycle.md`) was gated on one
of these arms confirming OOS; that gate stays shut, and its target moves to the
near-close pair.

### References

- `attic/fade_retail_longshot.py`, `attic/fade_props.py`,
  `attic/fade_retail_backtest.py`, `attic/README.md`
- `docs/strategy-scoreboard.md` §5 (the failure) and §5b (what is left)
- `docs/edge-research-log.md`, `docs/compounding-capital-cycle.md`
- Memory: [[project_fade_arms_failed_oos]], [[project_retail_longshot_fade]],
  [[project_compounding_slow_edges]]

---

## Near-close fades at a 2-hour lead — the last arms standing (2026-08-24)

### Hypothesis

The favourite-longshot bias, harvested hours before settlement instead of
days: buy NO on YES 0.10–0.20 at ~2h lead (`fade_nearclose`), buy YES
0.90–0.98 (`back_nearclose`). Founded on a 2026-07-05 panel study: band
mispricing −6.0pp at mid, executable fade +1.6¢/contract at 90% by-event
consistency over 532 de-correlated events.

### What we measured

Forward: `fade_nearclose` +$— lifetime over 1,733 resolutions — but the
uniform-stake era (opened ≥07-30) was **−$— over 618 independent
clusters**, P(mean≤0)=0.61, and the lifetime profit sat in the legacy
$—-stake era. `back_nearclose` +$— over 714 — mean +0.8¢/cluster against
~1¢ of ask-side spread.

Retrospective (2026-08-24, `edge-research-log.md` §2026-08-24, two surfaces):
the founding mispricing is **gone** — +0.4pp on 14,400 markets in the current
regime. Month-by-month on the capture surface, no month was EVER CI-positive;
the founding "+1.6¢" was sampled inside the July bump. The arm-selected
executable fade reads −3.25¢/cluster retrospectively, agreeing with the
forward arm's −4.6¢. Median 2h-lead spreads (6–15¢) exceed every calibration
gap by 5–30×.

### Why it failed

1. **A one-month founding sample caught a transient.** The de-correlation and
   executable-price discipline were all present — the window was simply too
   short to distinguish a regime from a structure. (The same lesson as the
   retail fade's 21 events, wearing a different coat.)
2. **The arm's own exclusions were fitted noise**: applied retrospectively
   they made June and July *worse* and August better.
3. **Win rate masked it for five weeks**, again: 88–95% cluster win rates sat
   almost exactly on the band's breakeven line.

### Decision

Both arms retired to `attic/` on 2026-08-24 (services stopped gracefully,
units archived, stores + OOS-ledger history retained as evidence);
`agentic_fade_nearclose` removed from `LIVE_TRADING_PROFILES`, its canary
flag deleted — the fill-quality question is moot with no paper edge to
measure against. The capture daemons (`near_close_capture`, `candle_panel`)
keep running as the re-entry detector: the pre-registered trigger is the
monthly candle_panel calibration table showing the 0.10–0.20 gap back below
~−4pp.

**With this, every edge thesis the project has tested is closed.** The
measurement machine ended its own best lead cleanly — which is the system
working. The binding constraint is a new mechanism, not more measurement of
the old ones.

### References

- `docs/edge-research-log.md` §2026-08-24 (the closing study)
- `docs/strategy-scoreboard.md` §5b; `attic/README.md` (module notes moved)
- Memory: [[project_nearclose_oos_decision]], [[project_fade_arms_failed_oos]]

---

## NHC scheduled-advisory weather (candidate A) — the market doesn't flinch (2026-09-09)

### Hypothesis

The last surviving edge candidate after the 2026-08-24 search. NHC issues
tropical-cyclone advisories on a **fixed public schedule** (03/09/15/21Z),
each stating intensity and track — a primary, event-specific document that
speaks directly to what the storm markets trade. If fan-priced hurricane
books absorb that slowly, the window between advisory publication and market
repricing is tradeable. Pre-registered 2026-08-24 (edge log §late-3),
instrumented the same night, ran for 16 days.

### What we tried

`pm/capture/advisory_capture.py` — advisory detection at publication time
(T0) plus ORDERBOOK paths with depth for every storm-flavored Kalshi market,
15s hot / 90s idle. Placed nothing, ever. Accrued **720 advisories across 10
storms and 267k book snapshots over 146 markets**.

### What we measured (n = 47 material advisories, gate was 15)

Material = ≥10 kt intensity forecast change, classification change, or ≥40 nm
track shift at 48h. Reprice curve, `mid(t) − mid(T0−)`, per market:

| horizon | median \|Δmid\| | mean | ≥2¢ |
|---|---|---|---|
| 30 s | 0.0000 | 0.0004 | 0% |
| 2 min | 0.0000 | 0.0010 | 1% |
| 10 min | 0.0000 | 0.0041 | 3% |
| 90 min | 0.0000 | 0.0141 | 15% |

Restricted to genuinely uncertain markets (pre-advisory mid 0.05–0.95, 136
observations — only 18% of the sample was pinned, so this is not a
settled-market artifact): median at 90 min rises only to **0.0050**. Even for
intensity advisories on uncertain markets: median 0.0050, 35% move ≥2¢.

Captured-edge test, crossing the stored book at T0+Δ net fees, clustered per
storm: **Δ=2min +1.14¢/contract, CI [−3.46, +7.89]** — spans zero, well short
of the +3¢ graduation bar, over 5 storm clusters (rule wanted ≥10). Median
capacity at the touch was 55 contracts.

### Why it failed

**The kill rule fired on its first clause: median |Δmid @90min| < 2¢.** The
typical storm market does not move at all when a material advisory lands.

The information is not absent — it is *concentrated and unpredictable*. 15%
of market-advisory pairs move ≥2¢, and among those the median move is 5.5¢
(max 41.5¢). But we cannot tell in advance which market that will be, so
there is no systematic trade — only a lottery with a spread on it.

The deeper reason is that a hurricane market is a **cumulative** claim ("will
Karina ever reach Cat 3") while an advisory is an **incremental** update.
Most advisories confirm the existing forecast rather than changing it, and
the book already prices the forecast track. That is the same structure that
killed the earnings-call thesis eight days earlier: the anchor was primary,
scheduled, and semantically on-target, but the market had *already read it*.
Coverage and window are not independent — a document this legible gets priced.

Also of note: at the only horizon we could actually trade (Δ=2min after our
own ~9-minute median detection latency), **1% of markets had moved at all**.
Whatever repricing happens is spread across 90 minutes, not concentrated in a
reaction window.

### Decision

Thesis dead. `advisory-capture.service` stopped per the pre-registration
(state retained; `systemctl --user start advisory-capture.service` to resume).
The season ledger closes here rather than Nov 30. This was the last live edge
candidate in the project.

### References

- Spec + kill rule: `docs/edge-research-log.md` §2026-08-24 (late-3)
- Result: §2026-09-09 (late-4)
- Instrument: `pm/capture/advisory_capture.py`, `docs/module-notes.md`
