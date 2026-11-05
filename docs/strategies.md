# Strategy ideas

A running catalog of trading approaches we've considered for prediction markets. Distinct from `TODO.md` (build queue) — this is where the *ideas* live, with honest reads on whether they'd actually work, what data we'd need to validate them, and how they map to existing or future infrastructure.

Each entry follows the same shape:

- **Premise** — the underlying thesis, in one sentence
- **Mechanics** — what trades the strategy actually places
- **When it works** — market conditions / data shape that make it pay
- **When it doesn't** — the failure mode that breaks the math
- **Status** — `idea` / `designed` / `prototype` / `live`
- **Edge honesty** — best estimate of whether real edge exists, and how we'd measure it

Strategies marked `live` are running today (in some form — usually paper, never live-money without passing the Backtest gate per `docs/recommendations-design.md`).

---

## 1. Market consensus (baseline)

- **Premise**: the market price is approximately the true probability; bet on whatever the market thinks is more likely.
- **Mechanics**: at each evaluation, take YES if market > 50%, NO otherwise; size at half-Kelly assuming market price is the true probability.
- **When it works**: as a baseline — measures whether a more sophisticated strategy actually adds value beyond "just believe the market."
- **When it doesn't**: never produces edge on its own (by construction matches the market).
- **Status**: `live` — `strategy_market_consensus` registered in `backtest.STRATEGIES`, runs in cron under `agentic_*/30 * * * *`. Uses `--min-gap 0.01` (the strategy adds a fixed 2pp synthetic edge; higher thresholds like the original 0.05 silently prevented all trades). `--closing-within-days 30` focuses on markets approaching resolution — the top-by-volume markets on Kalshi are ultra-long-dated (2045–2099) and produce no actionable signals.
- **Edge honesty**: zero by design. Useful as a *floor* — every other strategy's Brier must beat this to be worth running.

## 2. Favorite-grind (heavy on heavy favorites)

- **Premise**: heavy favorites (≥0.85 YES) are systematically *under-confident* — they should win 95% of the time but get priced at 88%. Pick pennies in front of a steamroller.
- **Mechanics**: when market YES ≥ 0.85, bet YES at small size assuming true probability is 0.95+. Repeat across many high-favorite markets to diversify the tail risk.
- **When it works**: if the market's calibration is biased toward hedging — pricing heavy favorites lower than true odds because traders are reluctant to commit at 90+¢ prices.
- **When it doesn't**: one upset on a 95% favorite costs ~19 wins on similar bets to recover. Asymmetric tail risk.
- **Status**: `live` — `user_strategies/favorite_grind.py`, runs in `agentic_favorites`.
- **Edge honesty**: empirical question — needs sample size that's still accumulating. Watch the Brier-vs-baseline gate before promoting from paper.

## 3. LLM forecast (fast, batch)

- **Premise**: a local LLM with general world knowledge can sometimes spot mispricing on markets where the venue's price reflects only the trader subset that found and bet on it.
- **Mechanics**: prompt the model with question + market price + days-to-deadline, parse a single YES probability, bet when the gap exceeds `min_gap` (default 5%).
- **When it works**: low-traffic markets where the venue price reflects ~20 traders and the LLM has access to broader context. Niche political / scientific markets.
- **When it doesn't**: high-traffic markets (the market price IS the LLM consensus by then); LLM tends to over-anchor on the market price; bad at "tail" predictions.
- **Status**: `live` — `find_llm_edges`, registered as `llm_forecast` strategy. Runs in `agentic_llm` every 30 min. Uses `--closing-within-days 60 --max-markets 50` with `model=qwen3:32b`. The forecast is **blind** — the LLM does not see the market price and must form an independent probability estimate. The gap between the LLM's blind forecast and the market price is the signal. Previous setup (llama3.1:8B with market price in prompt) produced zero signal — the model echoed the market price verbatim.
- **Edge honesty**: the LLM now produces real disagreement (20-70pp gaps on test markets), but whether it's RIGHT is what the Brier score will measure. Needs head-to-head vs market-consensus baseline + deep-analysis.

## 4. LLM deep analysis (slow, per-market, multi-context)

- **Premise**: the fast batch version is constrained by single-prompt JSON output; giving the model price history, resolution criteria, order book, sibling sub-markets, news headlines, and a Wikipedia summary lets it ground its reasoning rather than guess.
- **Mechanics**: `llm_deep_analysis.analyze_market(...)` packs every context source into one long prompt, asks for structured JSON (probability + confidence + key_drivers + key_risks + evidence_used + recommended_action + reasoning). Tagged `llm_deep:<model>`.
- **When it works**: news has happened that the price hasn't fully digested (the WaPo Massie headline → 0.000 probability vs 0.45 from the Phase-A local-context-only version). Markets where context is richer than what's in the price.
- **When it doesn't**: when the news IS already in the price (most cases); when the model hallucinates evidence not actually in the context (mitigated by the `evidence_used` honesty rule).
- **Status**: `live` — Phase A + Phase B both shipped. Nightly bulk runner across 6 models populates `agentic_deep` for head-to-head comparison.
- **Edge honesty**: 2-3 weeks of resolved-forecast data needed before any model is trustworthy for automated trading. Current best Brier: qwen3:32b at 0.174 (n=2) — meaningless sample.

## 5. Take-profit ladder (oscillation play)

- **Premise**: markets oscillate around their fair value during the time they're open. Buying near a local low and selling at a target above gives small repeatable wins even when the market doesn't ultimately resolve in your favor.
- **Mechanics**:
  1. Buy underdog at e.g. 25¢
  2. Set take-profit at 45¢ via `place_take_profit`
  3. If filled → +80% on the round-trip
  4. If not filled by resolution → either underdog wins (+300%) or doesn't (-100%)
  5. Optionally repeat: if market drops back to 25¢, buy again
- **When it works**: liquid markets that genuinely swing — sports games with score changes, political markets reacting to polls. The "ladder" version (re-enter on drop-back) works when there's mean reversion within the trading window.
- **When it doesn't**: thin markets (the spread eats the round-trip profit); one-way markets (price goes up and never comes back, you held but didn't sell); resolution before the TP fires (you're stuck holding to settlement).
- **Status**: `idea` — primitive exists (`place_take_profit`), no automation yet. Manually executable via the 🎯 Take-profit setters on Live Portfolio.
- **Edge honesty**: untested. The right way to evaluate is backtest on historical price-history data to see whether 25¢ → 45¢ swings happen often enough on the markets we'd target. Likely positive on sports, negative on macro.

## 6. Double-back / snake hedge

- **Premise**: in a binary market with two complementary sub-outcomes (e.g. TEAM A YES + TEAM B YES ≈ 100¢), buying both extremes during opposite price moments gives partial coverage. Example: buy TEAM A YES at 10¢, wait for the market to swing, buy TEAM B YES at 10¢ once it crosses through.
- **Mechanics**: as described — but importantly, NOT a guarantee. The full math:
  - Buy 100 TEAM A YES at 10¢ = $— stake
  - IF market swings and TEAM B YES drops to 10¢: buy 100 TEAM B YES at 10¢ = +$—
  - Now hold 100 of each. Outcome: one wins → 100 × $1 = $—. Net: $— profit on $— stake.
  - BUT: in most games the market doesn't swing 80pp from 10/90 to 90/10. If it doesn't, you just hold TEAM A YES at 10¢ and likely lose your $—.
- **When it works**: high-volatility markets that swing through extremes during their trading window — typically baseball games with lead changes, basketball games with 4th-quarter comebacks.
- **When it doesn't**: most games don't swing 80pp. Spread + fees at extremes (10¢ side often has 2¢ spread → ~20% friction). Adverse selection: if YES went from 10 to 90, *the actual game state changed* and the market may be ahead of you.
- **Status**: `idea` — user-proposed.
- **Edge honesty**: real-money equivalent of the "Martingale" trap. Looks like it works in theory; in practice the conditions for the second leg to fill are *exactly* the conditions where the team won't recover. The asymmetry is bad. Probably negative-expectancy on average. **Would need a backtest using historical sports market price-history data to validate before committing capital.**

## 7. Static dutching (cross-market arbitrage)

- **Premise**: a mutually-exclusive event with N candidates should have YES prices summing to ~$1.00. When the sum is < $1.00, you can buy YES on every candidate, lock in a guaranteed payout.
- **Mechanics**: detect events where Σ(yes_price across candidates) < 1.00 by some margin. Buy YES on each at current ask. Whichever resolves YES pays $1.00 → net profit = $1.00 − Σ(stake).
- **When it works**: rarely. Markets converge to arb-free pricing fast.
- **When it doesn't**: most of the time. When it *appears* to exist, the sub-markets may not be mutually exclusive (e.g. an event with a "none of the above" outcome not represented), or the prices are stale.
- **Status**: `live` — `scan_cross_market_arb` runs on the Scanner page's "Cross-market arb" tab. Currently surfacing ~4 candidates from the Kalshi universe at any given time, but most have plausibility-band issues (e.g. the sub-markets aren't truly mutually exclusive).
- **Edge honesty**: real arbitrage rarely exists. The scanner mostly finds *apparent* arbs from mispriced thin markets where the spread destroys the edge. Worth running but treat hits skeptically.

## 8. Late-game momentum (sports-specific)

- **Premise**: in sports markets, the trailing team has a historical comeback rate that the live market sometimes prices below true odds, especially during a brief window before a comeback solidifies.
- **Mechanics**: track score state (innings, runs, lead size) via ESPN's live `pickcenter` API or similar. Compare market price to empirical comeback rates by sport and game state. Bet underdog when market < historical rate.
- **When it works**: sports markets near critical inflection points (7th inning, 4th quarter) where the trailing team's true odds change faster than the market re-prices.
- **When it doesn't**: markets in well-followed leagues are typically priced by automated systems with similar data. Edge is in niche or off-hours markets.
- **Status**: `idea` — needs an archival data source for backtest (`sports_winprob` strategy exists as a backtest target but lacks historical data). Live execution needs a polling watcher on game state.
- **Edge honesty**: plausible in obscure leagues / odd hours; doubtful in MLB primetime. Backtest gate would tell us.

## 9. News-driven re-pricing (the workflow we have designed)

- **Premise**: news events move markets, but the price re-prices over minutes-to-hours, not instantly. A poller that catches news quickly + an LLM that interprets correctness + an order trigger that fires when conditions align = systematic capture of the re-pricing window.
- **Mechanics**: see `TODO.md`'s `News-driven alerts + targeted auto-trigger` entry — RSS poller (Federal Register, White House, Google News) + watchlist + pre-approved trigger rules + Deep Analysis re-run on relevant headlines + automatic order placement when delta + bid/ask conditions met.
- **When it works**: Slow-moving structured news (Federal Register EO publications, polling releases, regulatory filings). Markets that re-price over minutes, not seconds.
- **When it doesn't**: Fast-moving news (Twitter scoops). RSS polling has 5-30 min latency; major news has already moved markets by the time we see it.
- **Status**: `designed` — captured in `TODO.md`. Build is ~2-3 days; auto-trigger piece is gated on having weeks of Brier calibration data.
- **Edge honesty**: real edge exists for slow-news / off-hours news. Real edge does NOT exist on free-tier latency for major breaking news.

## 10. Baseball win-prediction model

- **Premise**: MLB win probabilities can be modeled honestly from a Bradley-Terry team-strength rating + matchup adjustments (home, starting pitcher, lineup, recent form) and the model can beat the public-consensus odds enough to find edges on Kalshi's MLB game markets.
- **Mechanics**: sidecar architecture — a self-contained `baseball/` sub-package maintains team ratings, pitcher quality, injury list etc. on its own cron. Nightly cron refreshes per-team rolling stats from MLB Stats API → recomputes ratings → state lands in `data/shared/baseball.sqlite`. The market app's strategy adapter calls `baseball.predict_game(home, away, date)` and registers the result as the `baseball_winprob` strategy. Same Brier-vs-baseline gate as every other source. News-driven adjustments between nightly tick and game time (pitcher scratches, late lineups). Domain isolation: nothing in `baseball/` knows about Kalshi, so the engine could be lifted into its own repo / serve a stock-research app later without rewrite.
- **When it works**: niche or off-hours games where the Kalshi market is thinly traded and the model has access to more current data than the price reflects. MLB has 2,430 regular-season games — high signal volume.
- **When it doesn't**: high-profile primetime games are priced by sportsbooks with sophisticated models; replicating that head-to-head with a few hundred lines of Python and free data is unrealistic. Edge probably lives at the margins (afternoon getaway games, NL West-Eastern Time miscoordinations, late-summer call-up uncertainty).
- **Status**: `prototype-shipped, v0.4.0` — sidecar in `baseball/` with the matchup model evolved across four iterations:
  - **v0.1**: regressed Pythagorean (40-game prior) + 0.16 log-odds home-field + log-odds SP-ERA adjustment.
  - **v0.2**: + park factors (Bayesian-regressed per-park run/HR multipliers) + bullpen ERA delta (halved SP weight).
  - **v0.3**: + recent-form blend (last 15 days, ≤30% weight, ramps with games-in-window).
  - **v0.4**: + posted-lineup OPS adjustment (min 50 AB per included batter to filter call-up noise).

  Full Sidecar protocol implementation: FastAPI multi-user UI on :8770 (Stats / Analysis / Chat / Recent Games / Standings pages), nightly + in-day cron refreshes, live event collector daemon, MLB book poller daemon, sidecar resolver short-circuiting Kalshi's 4-day settlement window. `find_baseball_edges` + `strategy_baseball_winprob` in `edges.py` runs in parallel to `find_sports_edges` (ESPN consensus) on the same MLB markets — the calibration head-to-head finally becomes measurable as forecasts resolve. **Open**: weather sidecar, AI-augmented projection, news-driven adjustments. All gated on Brier-vs-baseline showing v0.4 has measurable edge before further complexity.
- **Exit management (managed profile)**: `agentic_baseball_managed` uses SL + time-stop ONLY — no trailing stops. Baseball markets resolve to $0/$1 within hours; intra-game price swings are efficient reactions to game state (runs scored, outs, runners on base) that the pre-game model doesn't know about. The original ATR trail stop (K=2.0, vol window 12h) produced 7 exits with 0% win rate and -9.2% mean P&L — it interpreted normal in-game volatility (a single home run can move price 10-20%) as stop signals and sold before the game finished. Current config: `--stop-loss-pct 0.40` (fires only on true blowouts ~8+ run deficits) + `--time-stop-days 1.5` (cleanup for delayed resolutions).
- **Edge honesty**: early live results (unmanaged profile): 10/18 correct (55.6% hit rate), Brier 0.252. Better than coin-flip but sample is small. The right validation is walk-forward backtest: model trained on data up to date T predicts game on T+1, scored against actual result. Compare Brier to the `pickcenter`-derived consensus baseline. **Building without backtesting is the failure mode here** — it's easy to write a confident-looking model that's actually worse than just trusting Kalshi's price. The persisted `predictions` table makes the rolling Brier easy to compute as games resolve.

## 12. Oscillation mean-reversion (❌ KNOWN BAD — kept as reference)

- **Premise (wrong, as it turned out)**: markets demonstrating range-bound oscillation over their 30-day history will continue to oscillate around the same mean. Buy when current price is 2σ below mean; sell when 2σ above. Anchor probability on the mean.
- **Status**: ❌ falsified by 90-day Kalshi backtest (n=2155, lead=48h, fees on). Brier 0.169 vs market baseline 0.079 (2.15× worse); 25% hit rate; -21.9% ROI. Per-bucket P&L flat across `range_score` quintiles — the scanner's ranking metric isn't informative. See [`failed-experiments.md`](failed-experiments.md) for the full post-mortem.
- **Why it failed**: at 48h-pre-close, market prices are correctly forecasting resolution direction. A market that wobbled for 30 days then sits 2σ below its mean is *breaking down*; betting against that move means buying the cheap leg of a market that's correctly pricing the impending NO outcome.
- **Kept in the registry** as `oscillation_revert` for comparative backtests and as a worked example of a falsified strategy. Display name leads with ⚠️ KNOWN BAD; fires a runtime warning if invoked.

## 13. Oscillation momentum (🧪 PAPER — inverted variant of #12)

- **Premise**: take the EXACT same scanner-selected trades as #12, then bet **with** the direction the market moved instead of against it. A market 2σ above its mean keeps going up (toward $1); 2σ below keeps going down (toward $0).
- **Mechanics**: register `strategy_oscillation_momentum`. Same filters as `oscillation_revert` (range_score, mean_cross_freq, range_width). Z-score the current price vs window mean. If z ≥ +threshold → bet YES at `current_price + edge`. If z ≤ -threshold → bet NO at `(1 - current_price) + edge`. Probability anchored on current price (we expect the move to continue), not the mean.
- **Status**: 🧪 **paper trading** in `agentic_oscillation_momentum_managed` profile.
- **Backtest**: same 90-day Kalshi dataset, fees on, max_position_pct=0.05: **+8.9% ROI at 60.9% hit rate** (n=64). At zero position cap, +17.1% ROI; at 3% cap, +4.8%. Hit rate is stable ~61% across cap settings — real directional signal.
- **The honesty caveat**: P&L is **fat-tail dependent**. Top 5 winning trades = 229% of total P&L at the 5% cap. Without them, the other 59 trades lose -$—. The "edge" is convexity ("win small, lose small, occasionally win big") more than forecast accuracy (Brier 0.175 still worse than the market baseline 0.079). If the next 90 days don't deliver those 5 fat tails, the strategy underperforms.
- **Cron line installed**: every 30 min via `agentic_oscillation_momentum_managed`, full exit-mgmt stack (vol-aware trail, two-stage TP at +25% net). Needs `≥30 resolved live trades` to confirm or refute the backtest edge.
- **Edge honesty**: single backtest window, fat-tail dependent. Treat as "paper-trading experiment" not "tested edge."

## 14. Pre-event lottery (🧪 UNTESTED — register-only)

- **Premise**: sub-$0.10 markets with a known catalyst landing in <24h carry asymmetric payoff. A few are stuck near a stale quote; one of those mispriced ones occasionally resolves YES and pays $1, returning ~20× on the entry.
- **Status**: ❌ backtest on cached 90-day Kalshi dataset: -91% ROI at `max_price=0.10`. Markets at 48h-pre-close are correctly priced or slightly *over*-priced, not under-priced.
- **Live finding** when running `pre_event_scanner` against the live Kalshi universe: the sub-$0.10/under-1.5d basket is dominated by single-event threshold ladders (today: 30 Drake album tracker markets at $0.01-$0.02). The "lottery basket" thesis assumes independent catalysts; concentration warning fires correctly when >50% of the basket is one series.
- **Kept** as `strategy_pre_event_lottery` (UNTESTED in display name) + `pm/research/pre_event_scanner.py` module. The scanner is reusable; the strategy is for backtest comparison only.
- **What would make it live-worthy**: pair the price filter with an existing model edge (`baseball_winprob` / `llm_forecast` / `crypto_spot_lognormal`) — only buy sub-$0.10 markets where MY model says the implied probability is meaningfully higher than the market price. That's already implementable via the edges framework + a price filter.

## 15. Longshot scalper + flip (❌ RETIRED — net-negative in paper)

- **Premise**: a baseball underdog priced $0.10–$0.35 mid-game is volatile. We don't need it to *win* (that's the negative-EV lottery of #14). We buy the cheap side and **sell into the first meaningful pop** (+25% net) — banking the bounce, accepting a high hit-rate of small gains instead of a few resolution wins. This is a *trading/volatility* thesis, not a *forecast-edge* thesis — entry is model-free (underdog in the price band, inning 4–7, not a blowout).
- **Flip (stop-and-reverse)**: when a held longshot drops −15% net, sell it and buy the **opponent** (the favorite now pulling away) with the proceeds — ride the momentum we bet against. One flip per position (`from_flip` guard) to avoid thrash; a flipped position then only takes profit or rides to resolution.
- **Why a daemon, not the 30-min cron**: pops last seconds. `longshot_scalper.py` loops every ~15s and prices exits off the live `book_archive` bid/ask the Kalshi WebSocket keeps fresh (entry pays the ask, exit hits the bid — honest fills). Both TP and flip gate on **net** P&L after Kalshi fees (`kalshi_fees`), so a gross pop that doesn't clear the fee drag won't fire.
- **Scoping (load-bearing)**: this only makes sense on fast, mean-reverting prices. Running scalp/flip on slow-resolution markets repeats the disabled-stop-loss mistake (see `failed-experiments.md` — stop-losses had a 0% win rate on the slow profiles). **Baseball-only by design.**
- **Status**: ❌ **RETIRED** (daemon stopped + disabled 2026-05-31). Result: **~−$— paper over 67 exits** (`agentic_longshot_scalp`). The verdict from the counterfactual crunch (`scratch_*.py`): the cheap-underdog *entry* is the drag, not the exit — underdogs fade and stop out far more than they pop, and no exit rule rescued it (hold-to-resolution was the *worst*, −$—; skipping the flip saved ~$— but the arm stayed −$—). The flip leg alone lost ~−$— over 30 flips. Answer to the open question: intra-game longshot pops do **not** clear the fee drag often enough. Code kept for reference; superseded by #17.
- **Free-roll (`--freeroll-profit`, off by default)**: on a take-profit exit where the opposite side is still in the longshot band, recycle **only the realized net profit** into that opposite longshot. Principal already banked → a free option: if the team we just sold reverses, the opposite pays; worst case we give back only the recycled profit (~break-even round trip). Distinct from the flip (which is loss-triggered and redeploys the whole stake). The free-roll position is tagged `from_freeroll` so it can only take profit or ride to resolution — it never flips (that would circle back to the side we just sold) or free-rolls again. Skipped when the opposite is out of band (would buy an expensive favorite, not cheap convexity) or the profit is below `min_stake`.

## 16. Close-game scalp (🧪 paper, weak — `longshot_scalper.py --mode close_game`)

- **Premise**: in a genuine coin-flip game, the price swings a lot. If a model says the game is ~50/50 but the market prices one side **below** that, the cheap side is underpriced *and* sits in a high-variance game — buy it early and ride the expected in-game pop. A forecast-edge entry (unlike #15's model-free band) sharing #15's exit loop.
- **Entry**: model forecast for a side ∈ `[0.40, 0.60]` (the "20-point spread" toss-up gate) **AND** its ask ≥ `cg_min_edge` (default 5pp) below the forecast **AND** ask ≥ `cg_min_ask` (default 0.25, a **price floor** added after the floor-less version bought deep longshots like KC@$0.07 on a 0.45 forecast and rode them to $0 — a real coin-flip side trades near $0.50, so far below means the market is pricing a longshot regardless of the model) → buy it, in innings 1–`cg_max_entry_inning` (default 3, "buy early"). Among qualifying sides, largest edge wins.
- **Exit**: same +25% net take-profit as #15; flip **off** by default (flipping a coin-flip into its opponent is circular). Closes at $1/$0 on game Final. **Optional downside stop** (`--cg-stop-loss-pct`, default off): cut a fading position to cash at −X% net instead of riding it to $0 — see the stop-loss A/B arm below.
- **Forecast source is pluggable** (`--forecast-source`): `baseball` (Pythagorean model, instant, every game, Brier 0.228) ships first; `deep` (gemma3:27b deep-analysis log, Brier 0.10) is a **drop-in upgrade once an MLB deep-analysis primer exists** — today `bulk_deep_analysis` has *zero* coverage of `KXMLBGAME` tickers (it walks volume-ordered closing-soon markets, which MLB games miss), so `_latest_deep_forecast` returns None until a primer populates it.
- **Status**: 🧪 paper, **weak** — three arms run live: `agentic_closegame_scalp` (deep gate, hold), `agentic_closegame_consensus` (consensus gate, hold), and `agentic_closegame_scalp_stop` (deep gate **+ −25% stop**). The two hold arms went net-negative as games resolved (consensus ~−$— deep −$— all-time); the scalp only slows the bleed and the forecasts are barely better than a coin (Brier ~0.22–0.23). Price floor (above) fixed the deep-longshot gate failure but losses moved to legit coin-flips that fade with no downside exit — which is what the stop arm targets. Not the promising thread for *entry* — see #17 — but the stop arm may rescue the *exit*.
- **Stop-loss A/B arm (📊 2026-06-02, `agentic_closegame_scalp_stop`)**: a `book_archive` replay of the deep arm's 20 exits found close-game losers *bleed* through sellable bids (all 11 losers hit a min bid of $0.01–$0.14 before resolving at $0), so a stop always finds a fill on the way down. A **−25% stop turns the deep arm's −$— into +$— with zero false-stops on winners** (in-sample). Wired as `evaluate_exit` precedence TP → stop → flip; exit reason `stop_loss`. Deployed as a third arm (deep gate + `--cg-stop-loss-pct 0.25`) to confirm out-of-sample. Full methodology + chart: **`docs/close-game-stop-loss.md`**.
  - **First out-of-sample read (2026-06-02, 9 exits): trailed the hold arm — but the cause was a bug, not the stop.** All 7 stop-outs were on just 2 games: CWS@MIN got **whipsawed 4×** (re-bought the dip, stopped, re-bought…) on a game that ultimately **won** (4 false stops, ~−$—); TEX@STL was cut 3× on a game that correctly **lost**. The in-sample replay assumed one entry per game and so never modeled re-entry. **Fix: a re-entry-after-stop guard** (`_mark_stopped`/`_is_stopped`, persisted per-profile in `stopped_games.json`, keyed by `game_pk`, 24h TTL) — once stopped out of a game, the daemon won't re-enter it. With the guard, MIN would have been one stop, not four. The arm's pre-guard numbers are contaminated by the whipsaw; the guard makes the forward A/B meaningful.
- **MLB deep primer** (`mlb_deep_primer.py`, built): discovers today's games via the events path, **Pythagorean-prefilters to coin-flip-ish games** (so the slow LLM only runs where the scalper can use it — a full slate ~15 games drops to ~9 close ones), gemma3:27b deep-analyzes both sides, logs to `agentic_deep`. Deploy via pre-game cron, then flip `closegame-scalper.service` to `--forecast-source deep`.
- **Next**: **spreads/totals** — Kalshi exposes `KXMLBSPREAD-*` (run line) and `KXMLBTOTAL-*` (over/under) per game that we don't yet capture or price; a run-distribution model (built on `baseball.wpa` RE24) could find automatable edges there.

## 17. Favorite-hold (🧪 paper — current best hypothesis — `longshot_scalper.py --mode favorite`)

- **Premise**: the market **underprices moderate favorites**. From the counterfactual crunch over ~45 games / 90 sides (full per-side price paths in `scalper_observer.jsonl` + known outcomes), this was the *only* positive-EV thread among everything tried. Calibration was the tell: sides forecast 0.55–0.65 actually won **79%** (11/14), 0.60–0.70 won **100%** (6/6) — well above their ~60% implied price. (Below 0.55 it's noise — 0.50–0.60 won 55%, = the forecast. The edge is specifically *past* a true coin flip.) This is a coarse **directional value bet**, the opposite of the scalp theses: not "trade in-game volatility" but "back the underpriced favorite and wait."
- **Entry**: buy the side whose forecast ∈ `[fav_min_prob, fav_max_prob]` (default 0.55–0.70), early (innings 1–`fav_max_entry_inning`, default 3), at the ask. **No edge/underpricing requirement** — the crunch showed adding one only weakened results (the mispricing is generic, not per-game-findable). Stronger forecast wins ties.
- **Exit**: **HOLD to resolution. No scalp, no flip** — `evaluate_exit` returns None in this mode. Scalping the favorite destroyed the edge in backtest (+$— vs +$— for hold); the value is realized only at $1/$0.
- **Forecast source**: pluggable like #16; runs on `consensus` (ESPN de-vig). The three sources have similar Brier (~0.22–0.24) and the favorite edge held across all of them, so direction is what matters, not the specific source.
- **Status**: 🧪 paper — `favorite-hold.service` → profile `agentic_favorite_hold`. Backtest on the gathered sample: **+$— 73% win, sharpe 0.33** (consensus 0.55–0.70 hold). **Hypothesis-grade**: the favorite bands were only n=14–16, so the arm is deployed to gather a clean *forward* sample, not because the edge is proven. Plausible mechanism: the well-documented favorite-longshot bias (recreational money overpays for underdogs). Re-run `scratch_scenarios.py` / `scratch_favorites.py` as games accumulate to confirm or kill it.

## 18. Tennis Elo value (🧪 paper — `pm/arms/tennis_runner.py`, first individual-sport arm)

- **Premise**: the tennis Elo model (`tennis/elo.py`, computed from the season's match results across the whole field) frequently disagrees with the Kalshi price by a lot — **mean |gap| ~11pp, two-directional** (often the model likes the *underdog*). So unlike favorite-hold (#17), the bet is "back whichever side the model thinks is **underpriced** by ≥ `min_gap` (default 0.08)", hold to resolution. Not a favorites bet — a model-vs-market value bet.
- **The K-decay guardrail (load-bearing)**: a fixed-K Elo over-rates hot small-sample players — e.g. Rafael Jodar (ATP #29, ~40 wins this season) rated 1723, ≈ Zverev (#3, 1742), manufacturing a fake +21pp "edge" to back Jodar. Those are the model's *worst* calls, so `strategy_tennis_value` refuses a bet when, on the side it'd back: (1) that player has < 15 rated matches, (2) we'd fade a top-10 player on Elo alone, or (3) the backed player is ranked ≥ 20 spots worse than the favorite they'd fade. Verified: rejects both sides of Jodar/Zverev while still passing legitimately-priced value bets.
- **Mechanics**: `pm/arms/tennis_runner.py` discovers ATP/WTA match markets via the events path (low-volume, absent from the volume feed), injects them into `strategy_runner.run_cycle(markets_df=…)`, paper-trades into `agentic_tennis_value`, holds to resolution. Cron 20,50.
- **⚠️ One-position-per-match (fixed 2026-06-04)**: a match has two mutually-exclusive tickers (…-P1, …-P2). Run against both, the value strategy backs the same player twice — YES on that player's ticker **and** NO on the opponent's (the identical economic bet) — **doubling exposure** and making the two legs win/lose in lockstep. This silently 2×'d the early paper P&L (e.g. an apparent +$— over "16 bets" was really 8 matches doubled). Fix: `tennis_runner._one_ticker_per_match` collapses each match to a single ticker before the strategy sees it (the decision is symmetric — |gap| and the guardrails are identical on both sides, so either ticker reaches the same backed player). The pre-fix `agentic_tennis_value` paper data was archived and the arm restarted clean for an honest forward sample.
- **Status**: 🧪 paper, **unvalidated** — deployed to gather forward Brier/P&L. The guardrail thresholds (15 / top-10 / 20-rank-gap) are *reasoned, not tuned*. **Model-level update**: the small-sample inflation the guardrail was built around is now fixed at the source — Elo **v0.2 rank-seeds** players (`elo.seed_for_points`), which moved walk-forward Brier 0.2405→0.2198 and re-ordered the ladder correctly (Zverev clearly above Jodar). Guardrail kept as defense-in-depth. **Model accuracy now measured (2026-06-05):** `elo.walk_forward_brier` scores every match out-of-sample (predict from pre-match Elo, then apply) over the full matches table — the runner persists it and `tennis.stats()` surfaces it. (The old ticker-keyed predictions table was never populated — a sidecar can't see Kalshi tickers — so Brier read 0; this replaces that.) **First read: walk-forward Brier ≈ 0.235 over ~4,800 matches** — only modestly better than the 0.25 coin-flip baseline, i.e. the Elo is **weak over the full field** (dragged by the ~2/3 of rated players outside the top-300 rankings seed + early-season cold-start). That tempers expectations for the value arm (§19-style edge is small if the model barely beats a coin flip). See `docs/sports-model-playbook.md`.

## 19. Reactive scalp — in-game speed (🧪 paper, redesign — `pm/arms/reactive_scalper.py`)

- **Premise**: the MLB game-winner market is **efficient on a static basis** — `reaction_decay.py` calibration (n=214) shows prices calibrated across the full range incl. extremes (0.86→won 0.85, 0.14→0.15), so favorite-hold/close-game/longshot were all betting against an efficient market (and lost). The **one** place an edge survives is **speed**: the book *lags* high-leverage scoring plays. Conditioned on leverage, the benefiting side shows **~+2.5¢ mean edge that persists ~60s** after the play (full-sample reaction is ~0 — the edge is entirely in late-and-close spots). 60s ≫ our ~15–30s pipeline latency, so it's *reachable*. This is the user's original baseball thesis (in-game reaction, not outcome prediction).
- **Entry (WPA-conditioned)**: event-driven — watches every live game (no pre-existing position) and on a NEW play whose **WPA** clears the gate (`|WPA| ≥ wpa_threshold` [0.10, ~top 22% of plays] AND inning ≥ `min_inning` [6]) buys the **side whose win-prob went up** at ask, capped at `max_entry_ask` (0.92), on a fresh quote + recent play (`max_play_age_s` 120). WPA = the change in win probability a play caused, from `baseball.analysis.wpa_for_play` (RE24/normal model over base-out + score + inning) — the direct impact measure the book reprices on. **Upgrade over the original "any scoring play" trigger** (shipped 2026-06-05): WPA catches high-impact NON-scoring plays (a rally-killing double play / bases-loaded K — where the *fielding* side gains) and skips low-impact runs (a run in a near-decided game). The MLB feed's win-prob is NOT in `raw_json`; the model computes WPA from the situation it does carry. WPA full coverage verified (400/400 recent plays); median |WPA| 0.02, top plays are 9th/10th-inning HRs (0.5–0.86) + a −0.48 DP.
- **Exit (both sides explicit)**: take-profit (net ≥ `take_profit_pct`, capture the pop) **OR** time-stop (`hold_seconds` 150 — the ~60s window has passed, harvest whatever moved) **OR** resolution close at $1/$0 on Final. Exposure is always bounded.
- **Status**: 🧪 **paper, in redesign** — ran live at $10/order 2026-06-06→09 (`agentic_reactive`), then pulled. The live test did its job by exposing what paper hid: (1) a fill-only logging bug on both buy and sell sides (`broker.buy`/`sell` return an `OrderResult` and don't raise on a non-fill → fictional exits + orphaned positions; fixed — only real fills log `react_entry`/exit, non-fills log `react_reject`/`exit_unfilled`); (2) no entry **price floor** → it sized the $— cap into 9–12¢ "improved but still losing" underdogs that rode to $0 (fixed — `min_entry_price` 0.25). With entry selection corrected it still lost (~−$— over 4 nights), isolating the **core failure: the scalp exit whipsaws**. In choppy/extra-inning games the marketable time-stop sells into the reversal — buy-high/sell-low round-trips, and *both* sides of one game can lose (NYYCLE churned 7 entries for −$—). "Harvest the 60s pop" fails because the pop reverses and/or there's no bid to sell into. The signal itself (`reaction_decay.py`: high-leverage plays, benefiting side +2.5¢ ~60s) may still exist, but **scalping it doesn't survive real spread + reversal**. Redesign options: anti-whipsaw (per-game re-entry cap + no-opposite-side-while-held), or abandon the scalp and hold a contested favorite to resolution. See `reaction_decay.py`, `docs/live-execution-adapter.md`, `[[project_strategy_research_findings]]`.

## 20. Cross-sector inefficiency map (🔬 research — `pm/research/inefficiency_sweep.py`)

- **Premise**: the strategy search kept testing one market (MLB game-winner) and finding it efficient. Flip the question — *where across the whole venue is the market mispriced?* Inefficiency lives where attention/liquidity are thin, and we'd only looked at one of the most-traded corners.
- **Method**: for a broad sample of recently-closed markets, take the YES price at a fixed lead before close as the forecast, then measure **calibration deviation** — observed resolution rate vs priced rate, bucketed by probability. Not raw Brier (a perfectly-priced coin flip scores ~0.25); the *signed gap* between observed and priced in a bucket is the exploitable signal.
- **The trap it had to solve**: ~89% of recent Kalshi is **multi-outcome fields** (award nominees, multi-candidate races, vote-share buckets). Scoring those as if binary produced impossible "$0.50 edges" at 99¢ — pure artifact. The sweep now `classify()`-ies each market `binary` / `multi_outcome` / scalar-range using Kalshi's real `event_ticker`, runs binary calibration only on standalone yes/no, and measures one-winner fields by **overround** (`sum(member YES prices) − 1` = fade-able vig), scored only on groups that resolved to exactly one winner (scalar ranges excluded; incomplete and non-exclusive groups reported, not hidden).
- **First findings (2026-06-10, 14-day sample)**: sports **efficient** (Brier 0.005 — re-confirmed); standalone binaries are sparse; the cleanest cross-sector signal is the **favorite-longshot bias** — longshots overpriced (priced ~0.15 → resolve ~0.05) and moderate favorites underpriced (~0.56 → 0.73, ~0.75 → 0.89). One-winner fields carry large overround, but the 24h lead inflates it (fields are diffusely priced a day out) → measure near-close in the mining phase.
- **Status**: 🔬 research tool, not a strategy. It's the **map**; the next step is mining the favorite-longshot lead (does fading longshots / backing moderate favorites in one-winner fields survive fees + thin-book liquidity?). Scope limit: recent-window only (the settled-events endpoint is newest-first; deep history needs the trade archive). See `[[project_north_star_kalshi_leaderboard]]`.

## 11. Calibration-aware ensemble (future)

- **Premise**: once we have Brier scores for each of the 6 nightly-batch models on the same markets, an ensemble that weights by inverse-Brier should beat any single model.
- **Mechanics**: for each market, query the latest forecast from each model, weight by `1 / brier_by_source[model]['brier']`, take the weighted average probability. Use that as the trading probability.
- **When it works**: in the limit of large samples, ensembles dominate single models (the "wisdom of crowds" for models). Specific models good at specific categories (qwen3 on politics, deepseek-r1 on macro?) — the ensemble auto-discovers this.
- **When it doesn't**: when one model is *very* wrong on a specific question, the ensemble averages toward wrong with confidence. Needs per-category Brier, not just overall.
- **Status**: `idea` — gated on having ~3+ resolved forecasts per (model, category) cell to weight honestly.
- **Edge honesty**: highest-leverage thing to build once we have the data. The bulk_deep_analysis runner exists specifically to feed this.

---

## How to use this doc

- **Adding a new idea**: append a numbered section, fill in the 5 fields. Be honest about edge — `idea` means "untested," not "untestable."
- **Promoting from `idea` → `prototype`**: build a strategy plugin in `user_strategies/`, register it with `bt.register_strategy(key, display_name)`, run it in paper mode via `strategy_runner --strategy <key>`.
- **Promoting from `prototype` → `live`**: pass the Backtest gate (Brier ≤ market-baseline Brier on the strategy's target category) AND accumulate ≥20 resolved forecasts in paper mode AND a human review of the trade pattern. Then enable via `--live` flag (when Phase 5 exists). Until then, all strategies stay paper-only.

Strategies that fail the gate stay in this doc with `status: idea` and the gap noted. Better to have a documented graveyard than re-derive the same bad idea three months later.
