# Prior-Art Survey — strategies from outside this repo

**First pass: 2026-05-29.** A scan of documented findings on prediction-market
efficiency, Kalshi-specific trading, and baseball betting models — translated
into a ranked shortlist of **backtest candidates** for this repo. Goal: learn
from others before spending build time, and avoid re-running things
`docs/failed-experiments.md` already falsified.

Credibility is flagged per source: **[academic]** (peer-reviewed / working
paper), **[practitioner]** (blog / vendor / community — directional, often
self-promotional, treat claims as hypotheses not evidence).

---

## Annotated reading list

### Lane 1 — Prediction-market efficiency

- **Favorite-longshot bias (FLB)** — strong and consistent in *fixed-odds
  sportsbooks* (longshots overbet, favorites underbet), but **prediction
  markets do NOT reliably show it** — sometimes present, absent, or *reversed*.
  [academic: ScienceDirect 2017; Green/Lee/Rothschild "Favorite-Longshot
  Midas"]. **Implication for us**: don't assume an FLB edge exists on Kalshi —
  measure it per-category. Corroborates why our `pre_event_lottery` (buying
  sub-$0.10 longshots) backtested negative: in sportsbook-like pricing the
  longshot leg is *over*priced, not under.
- **Modern prediction-market price discovery** — **Polymarket leads Kalshi**
  in price discovery, especially at high liquidity; cross-platform prices for
  the same event diverge **>5pp ~15–20% of the time**; ~$40M of arbitrage was
  extracted from Polymarket alone (Apr'24–Apr'25). [academic: Ng/Peng/Tao/Zhou
  SSRN; IMDEA Networks]. **Implication**: a *Polymarket-leads-Kalshi*
  lead-follow signal is plausible and we already have both providers wired.
- **Base rate is brutal** — a CEPR working paper analyzing 300k+ Kalshi
  contracts found the **average pre-fee return ≈ −20%**; most traders lose
  before fees even enter. [academic, via practitioner summary]. **Implication**:
  edges must be real and fee-aware; "it traded" is not "it worked."

### Lane 2 — Kalshi-specific tactics

- **Maker > taker** — maker (resting limit) fees are ~75% cheaper than taker
  fees, and resting unmatched orders are **fee-free**; makers outperform takers.
  [practitioner: oddsassist, marketmath]. **Implication**: our scalpers pay the
  **ask (taker)** today — the whole longshot-scalp thesis hinges on clearing
  Kalshi's fee drag, so a maker/limit-entry variant could be the difference
  between net-positive and net-negative.
- **Fee curve favors extremes** — fee `= ceil(0.07·P·(1−P)·C)`, maximal at
  $0.50, near-zero at the tails. [Kalshi fee schedule; matches our
  `kalshi_fees`]. **Implication**: cheap-longshot and heavy-favorite entries are
  structurally fee-advantaged; mid-price (coin-flip) entries pay the most — a
  real headwind for the **close-game scalp**, worth quantifying.
- **+EV vs de-vigged sportsbook odds** — strip the vig from a sportsbook line to
  get a fair probability, buy Kalshi contracts priced below it. [practitioner,
  widely cited]. **Implication**: we already pull ESPN consensus
  (`sports_data.devig`) — this is a stronger fair-value benchmark than our
  Pythagorean model for the close-game gate.
- **Cross-platform / cross-venue arbitrage** (Kalshi ↔ Polymarket ↔
  sportsbooks) when total cost of opposite sides < $1. [practitioner; open-source
  bots exist]. **Implication**: real but execution- and settlement-rule-
  sensitive; a *monitoring* feature is lower-risk than an auto-arb bot.
- **Overreaction-and-pullback** — fade exaggerated moves back toward fair value.
  [practitioner]. ⚠️ **We have already falsified the slow-market version**
  (`oscillation_revert`, Brier 0.169 vs 0.079 baseline). Only worth re-testing
  *intra-game*, where prices are genuinely volatile and mean-reverting.

### Lane 3 — Baseball / sabermetrics betting

- **De-vigged consensus / closing line is the benchmark** — MLB markets show
  FLB *at the opening line* but **correct it by close**; the closing line is
  ~sharp, and closing-line value (CLV) is the standard yardstick for any model.
  [academic: Harvard "Swing and a Miss"]. **Implication**: (a) opening/early
  lines are the soft target — entering well before close (our pre-game
  close-game scalp) is the right window; (b) benchmark every baseball model
  against the de-vigged consensus, not just coin-flip.
- **Simulation models** (≈10k game sims from player projections) drive
  moneyline / run-line / total edges. [practitioner: FTN, Rithmm]. **Implication**:
  our run-distribution model for the queued **spreads/totals** work is the
  standard approach; RE24/WPA (`baseball.wpa`) is a reasonable base.
- **In-game: public overreacts to scoring (recency bias); models struggle to
  quantify momentum.** [practitioner, consistent across live-betting guides].
  **Implication**: this is direct external support for our core baseball edge
  thesis — react to live plays faster than the book, and fade the public's
  overreaction to a run. Our `trade_archive` (aggressor flow) + `wpa` +
  `analysis.reaction_summary` are exactly the instruments to test it.

---

## Backtest candidates (ranked by expected leverage × ease)

1. **De-vigged consensus as the close-game gate** *(high, easy)* — ✅ **BUILT
   2026-05-29**: `edges.sports_consensus_for_ticker(ticker)` maps a Kalshi MLB
   ticker → ESPN de-vigged consensus P(YES); wired into the close-game scalper
   as `--forecast-source consensus`. Live-verified. **Still TODO**: measure its
   Brier vs Pythagorean (0.228) and gemma3 deep (0.10) on the same games
   (needs consensus forecasts logged + resolved over time), and decide
   whether to A/B it against the deep gate or promote it. Strongly supported
   by the CLV literature.
2. **Fade-the-scoring-overreaction (in-game)** *(high, medium)* — using
   `trade_archive` aggressor flow + `wpa`, test whether the book moves *more*
   than the WPA-justified amount right after a run, then reverts. This is our
   baseball edge thesis with external backing. Reuses `analysis.reaction_summary`.
3. **Maker-entry scalping** *(high, medium)* — re-derive the longshot/close-game
   scalp economics assuming **resting limit (maker) fills** instead of paying
   the ask. If maker entries flip the net edge positive, it changes how the live
   daemons should place orders. Ties to `kalshi_fees` + `pm/venue/slippage.py`.
4. **Polymarket-leads-Kalshi lead-follow** *(medium, medium)* — for events
   listed on both, detect when Polymarket moves and Kalshi lags >X pp, trade
   Kalshi toward Polymarket. We have both providers; needs a same-event matcher
   (cf. `llm_matcher`) and is mostly a non-MLB (politics/macro) play.
5. **Run-distribution pricing for spreads/totals** *(high, hard)* — the already-
   queued `KXMLBSPREAD`/`KXMLBTOTAL` work; the survey confirms the simulation/
   run-expectancy approach is standard. Benchmark against de-vigged book totals.
6. **Fee-aware entry selection** *(low, easy)* — formalize preferring tail-priced
   entries and treating coin-flip entries as fee-disadvantaged; quantify the
   $0.50-max fee headwind on the close-game scalp.

---

## Do NOT re-try (already falsified here — see `docs/failed-experiments.md`)

- **Slow-market mean-reversion / oscillation** — `oscillation_revert` lost
  (Brier 0.169 vs 0.079 baseline). The practitioner "overreaction pullback" is
  the same idea; only the *intra-game* variant (#2) is worth pursuing.
- **Starter/bullpen ERA weighting** — zero/negative signal at our sample sizes;
  the prior art's "use the de-vigged market line" (#1) is the better path than
  building our own pitcher-strength model.

---

## Sources

- [Favorite-longshot bias in fixed-odds markets (ScienceDirect, 2017)](https://www.sciencedirect.com/science/article/abs/pii/S1062976916000041)
- [Green, Lee & Rothschild — "The Favorite-Longshot Midas"](https://www.stat.berkeley.edu/~aldous/157/Papers/Green.pdf)
- [Harvard — "Swing and a Miss: Behavioral Biases in MLB Betting Markets"](https://dash.harvard.edu/bitstreams/81e73570-084b-4e39-84d3-ccb4649c61ab/download)
- [Ng, Peng, Tao & Zhou — "Price Discovery and Trading in Modern Prediction Markets" (SSRN)](https://papers.ssrn.com/sol3/Delivery.cfm/5331995.pdf?abstractid=5331995&mirid=1)
- [Prediction-market arbitrage: cross-platform Kalshi/Polymarket (AhaSignals)](https://ahasignals.com/research/prediction-market-arbitrage-strategies/)
- [How to make money on Kalshi — strategies & tools (OddsAssist)](https://oddsassist.com/prediction-markets/how-to-make-money-on-kalshi/)
- [Kalshi fees complete guide 2026 (MarketMath)](https://marketmath.io/blog/kalshi-fees-guide-2026)
- [Beating the MLB Moneyline (Stanford CS229, Chen & He)](https://cs229.stanford.edu/proj2010/ChenHe-BeatingTheMLBMoneyline.pdf)
- [Assessing win strength in MLB win prediction models (arXiv 2511.02815)](https://arxiv.org/pdf/2511.02815)
