# Glossary

Terms used throughout this project and in prediction market trading generally.

---

## Prediction Market Concepts

**Prediction market**
A market where people buy and sell contracts that pay out $1 if a specific event happens and $0 if it doesn't. The current price of a contract reflects the crowd's collective probability estimate. Polymarket is a prediction market platform running on the Polygon blockchain.

**Outcome / outcome token**
Each possible resolution of a market. Binary markets have two outcomes (Yes / No). Multi-outcome markets have three or more (e.g., candidate A, candidate B, other). Each outcome is a tradeable token on the CLOB.

**Market price / mid-quote**
The midpoint between the best bid and best ask for a given outcome. Displayed as a number between 0 and 1, which you can read directly as a probability. A price of 0.35 means the market thinks there's a 35% chance this outcome resolves YES.

**Implied probability**
The probability the market is pricing in. Numerically identical to the market price — a contract at $0.60 implies a 60% probability.

**Resolution**
When a market closes and a winner is determined. Winning outcome tokens pay $1.00; losing outcome tokens pay $0.00.

**Event**
A higher-level grouping of related markets on Polymarket. For example, "2026 NBA Finals" is one event; the underlying markets are "Will the Lakers win?", "Will the Celtics win?", etc. Polymarket tags events with topic labels (Sports, Crypto, Soccer, etc.) which this app uses as the authoritative source for market categorization.

**Condition ID (`conditionId`)**
A hex identifier (starts with `0x`) that uniquely identifies a market on Polymarket. Used to look up market details. Different from CLOB token IDs.

**CLOB token ID**
A large numeric identifier (77-digit integer) for a specific outcome token on the Central Limit Order Book. This is what the price history and order book APIs use — not the `conditionId`.

---

## Order Book & Pricing

**CLOB (Central Limit Order Book)**
The trading engine that matches buyers and sellers. When you place a limit order, it sits in the book until someone takes the other side. Polymarket's CLOB is what generates the price history and spread data in this app.

**Bid**
The highest price a buyer is currently willing to pay for a contract. If you want to sell immediately, you sell at the bid.

**Ask**
The lowest price a seller is currently willing to accept. If you want to buy immediately, you buy at the ask.

**Best bid / best ask**
The single best (highest) bid and (lowest) ask currently in the order book. The gap between them is the spread.

**Bid-ask spread**
The difference between the best ask and best bid (`ask − bid`). This is the cost of a round-trip trade — if you buy at the ask and immediately sell at the bid, you lose the spread. A spread of 0.001 on a $0.50 contract costs $0.001 per share traded. Narrow spread = liquid, easy to trade. Wide spread = illiquid, expensive to enter or exit.

**Mid-quote**
The average of best bid and best ask: `(bid + ask) / 2`. This is what Polymarket's `outcomePrices` field reports. For binary markets, YES mid + NO mid always equals exactly 1.0.

**Last trade price**
The price of the most recently executed trade. Can differ from the current mid-quote if the market has moved since the last trade or if the book is thin.

**Liquidity**
The total dollar value of orders sitting in the book. Higher liquidity means you can buy or sell larger positions without moving the price much.

**Slippage**
The difference between the price you expected and the price you actually got, caused by moving through multiple levels of the order book. Low liquidity and wide spreads cause high slippage.

**Order book depth**
The total resting size at each price level in the book. "Deep" books have lots of size at each level, so large trades don't move the price much. "Thin" books have little size at each level — a moderately-sized trade walks several levels up (or down) before filling, producing slippage. The Order Book Depth view on the Price History page visualizes this as a staircase chart.

**Cumulative size**
At any given price level, the total size at that level **and all better levels combined**. For bids, "better" means higher price (more attractive to a seller); for asks, "better" means lower price (more attractive to a buyer). Cumulative size at price X is the answer to "how many shares can I fill at price X or better?"

**Level (in the order book)**
A unique price point with resting orders. A book with 25 ask levels has 25 distinct ask prices, each with one or more resting orders aggregated into a single size.

---

## Position & Portfolio

**Position**
An open holding in a market — the shares of an outcome token you own.

**Shares**
Units of an outcome token. Each share pays $1.00 if that outcome resolves YES. You can own fractional shares.

**Cost basis**
The total amount you paid to acquire your current position: `shares × average_purchase_price`.

**Average buy price (avg price)**
The average price per share across all your purchases in a position, weighted by size.

**Unrealized P&L (profit and loss)**
The gain or loss on an open position if you were to close it right now at the current mid-price: `current_value − cost_basis`. "Unrealized" because the position is still open.

**P&L %**
Unrealized P&L expressed as a percentage of cost basis: `(unrealized_pnl / cost_basis) × 100`.

**Net value**
Mark-to-market value of all currently open positions, plus cumulative realized P&L from completed sells. This is the most honest single number for "how much your portfolio is worth right now" — it incorporates both open positions and the cash you've already locked in via sells.

**Drawdown**
The drop from a peak portfolio value to a subsequent trough. "Max drawdown" is the worst such drop over the tracked history. A common risk metric: a 10% max drawdown means at some point your portfolio was down 10% from its high. Drawdown discipline (e.g., "stop trading after a 15% drawdown") is one of the most effective risk controls.

**Snapshot**
A point-in-time record of total portfolio value. This app captures one automatically each hour you visit the Portfolio page, building a history without any user action.

---

## Kelly Criterion & Bet Sizing

**Kelly Criterion**
A mathematical formula for choosing how much of your bankroll to bet in order to maximize the long-run growth rate of your wealth. It was derived by John Kelly at Bell Labs in 1956.

For a prediction market where you buy outcome tokens at price *m* (the market's implied probability) and your true probability estimate is *p*:

- Bet YES when *p > m*: `Kelly fraction = (p − m) / (1 − m)`
- Bet NO when *p < m*: `Kelly fraction = (m − p) / m`

The result is the fraction of your bankroll to wager.

**Edge**
The difference between your probability estimate and the market's implied probability: `edge = your_p − market_price`. Positive edge means you think the market underprices the outcome. Zero or negative edge means no bet.

**Kelly fraction**
The proportion of your total bankroll the Kelly formula recommends betting. A fraction of 0.10 means bet 10% of your available funds.

**Half-Kelly (½ Kelly)**
Betting half the Kelly-recommended amount. Reduces variance (volatility of your bankroll) by roughly half while giving up only a small amount of long-run growth rate. Widely preferred by professional bettors because the full Kelly bet is very aggressive and even small errors in probability estimation can lead to overbetting.

**Bankroll**
The total pool of money available for betting — your trading capital. Kelly sizing is always expressed relative to the current bankroll.

**Expected value (EV)**
The average profit per $1 bet, weighted by the probability of each outcome. A bet with +EV is profitable on average; a -EV bet loses money on average. Kelly naturally maximizes EV relative to your bankroll over many bets.

**Overbetting**
Betting more than the Kelly fraction recommends. Even if your edge is real, overbetting reduces long-run growth and can cause ruin. This is why half-Kelly is safer.

---

## Forecasting & Calibration

**Forecast**
A probability estimate you made for a specific market outcome at a specific time. The forecast log lets you review whether your past estimates were systematically too confident or too cautious.

**Resolution**
The actual outcome of a market once it closes — `Yes` or `No`. Required to score a forecast.

**Brier score**
The mean squared error between your forecast probabilities and the actual outcomes (0 or 1). Lower is better; **0 is perfect**, **0.25 is what a uniformly random forecaster gets** on 50/50 markets. A forecaster confidently wrong scores closer to 1.

**Calibration**
A property of a forecaster: if you say something is 70% likely and you make many such forecasts, *about 70%* should actually happen. A *well-calibrated* forecaster's predicted probabilities line up with observed frequencies. Calibration is a different concept from accuracy — a forecaster can be perfectly calibrated by always saying "50%" but never have any edge.

**Calibration plot**
A scatter plot of predicted probability (your numbers) against observed resolution rate (the actual fractions). Each dot is a probability bin (e.g. "all forecasts between 60% and 70%"). A perfectly calibrated forecaster's dots sit on the diagonal. Dots below the diagonal mean you were overconfident in that range; dots above mean you were underconfident.

**Sharpness**
How often you make confident predictions. A forecaster who says 50% on everything is trivially well-calibrated but useless. Sharp forecasters give predictions far from 50%. The goal is to be sharp *and* calibrated.

---

## Scanning / Analysis

**Arb (arbitrage)**
Profiting from a price discrepancy with little or no risk. In prediction markets, true risk-free arb is rare because binary markets enforce YES + NO = $1. The scanner looks for softer opportunities: thin spreads, stale quotes, and multi-outcome imbalances.

**Price divergence**
A gap between the current mid-quote and the last traded price. Can signal a stale quote (the market maker hasn't updated yet) or rapid price movement after a news event.

**Multi-outcome gap**
In a market with 3+ outcomes, the sum of all outcome prices should equal $1. When it doesn't, at least one outcome may be mispriced relative to the others.

**Volume (24h volume)**
The total dollar value of contracts traded in the last 24 hours. High volume = actively traded market with reliable price discovery.

---

## Backtesting

**Backtest**
Replaying a trading strategy against historical data to see how it would have performed. In this app, each closed market becomes one evaluation point: the strategy sees the price history up to a chosen lead time before resolution, decides what to bet, and gets graded on the known outcome.

**Lookahead bias**
The cardinal sin of backtests — the strategy "accidentally" sees data from the future, then looks great on history but flops live. The `pm/research/backtest.py` engine prevents it structurally: a `MarketSnapshot`'s price series is truncated to rows strictly before the evaluation time, and the outcome is held by the engine in a separate object the strategy can never reach.

**Single-shot vs. walk-forward**
Single-shot: one trade per market, evaluated at one point in time, held to resolution. The v1 backtester does this. Walk-forward: evaluate at every period, can enter/exit, hold for variable durations. More realistic; deferred to v2.

**Hit rate**
% of trades whose direction was correct. Comes with a 95% Wilson confidence interval — better than the normal-approximation CI on small samples or extreme proportions.

**ROI (return on investment)**
`total_pnl / total_stake`. Measures how efficiently each dollar deployed is working. Different from "% return on bankroll" because bankroll is the *anchor* for sizing, not the *denominator* for return.

**Sharpe ratio (per-trade)**
Mean trade PnL divided by std-dev of trade PnL. Not annualized in this app — prediction-market trades have variable horizons, so the usual annualization is misleading. Read as "is the average win bigger than the per-trade noise?"

**Max drawdown**
Worst peak-to-trough drop on the cumulative-PnL equity curve. Anchored at $0 (starting bankroll), so if the first trades are losses, the drawdown counts from zero down, not from the first (negative) sample.

**Market-baseline Brier**
The Brier score of "just predict the latest price" on the same set of historical markets. The bar an alpha strategy has to clear: if your strategy's Brier on a category isn't lower than this baseline's Brier on the same category, your strategy is paying transaction costs to lose to the crowd. The Backtest page surfaces this as an explicit pass/fail.

---

## Trading strategies & live trading

**Scalp**
Entering a position intending to exit on a small, quick favorable move rather than holding to resolution. The longshot scalper buys a cheap side and sells into the first meaningful pop (e.g. +25% net), banking a high hit-rate of small gains instead of waiting for the (unlikely) win.

**Trade tape / prints**
The stream of *executed* trades on a market (the "trades placed" list in the Kalshi UI), as opposed to the order book (resting, unexecuted liquidity). Each print has a price, size, and **taker side** (the aggressor's direction). Captured in `trade_archive` from the public `markets/trades` endpoint + the WebSocket `trade` channel.

**Taker side (aggressor)**
On an executed trade, the side that crossed the spread to make it happen (lifted the offer / hit the bid). A burst of yes-taker volume = aggressive buying pressure on YES — the repricing happening in real time.

**Flip (stop-and-reverse)**
On a losing position, selling it and immediately buying the *opposite* side with the proceeds — i.e. reversing direction to ride the momentum you bet against. In the scalper: a held side dropping past a net-loss threshold sells and buys the opponent ticker. One flip per position to avoid thrashing.

**Free-roll (house money)**
After banking a profit, re-risking *only the realized profit* (principal already secured) into a new position — so the worst case is roughly break-even and the upside is a free option. The scalper's free-roll recycles take-profit gains into the opposite longshot.

**Coin-flip game / close game**
A game a model rates near 50/50 (e.g. forecast in [0.40, 0.60]). The close-game scalp targets these: high in-game price variance + real two-sided equity, so a side bought below its model value has frequent chances to pop.

**Favorite-longshot bias**
The well-documented tendency for betting markets to *overprice underdogs and underprice favorites* — recreational money likes the big payout of a longshot, so favorites are systematically cheap relative to their true win probability. Our counterfactual crunch found it in the gathered MLB data: sides forecast 0.55–0.70 won ~80% despite being priced near their forecast. The **favorite-hold** strategy (`--mode favorite`) exploits it by buying the moderate-favorite side and holding to resolution.

**Run line / spread (`KXMLBSPREAD`)**
A bet on the *margin* of victory rather than the winner — e.g. "team wins by more than 1.5 runs." The baseball equivalent of a point spread.

**Total / over-under (`KXMLBTOTAL`)**
A bet on the *combined* runs scored by both teams crossing a threshold (over/under N runs), independent of who wins.

---

## External Data Edges

**Edge candidate**
A flagged market where an external reference value (e.g. a spot-price-based model) disagrees with the market's implied probability by more than a threshold. *Advisory only* until a backtest shows the source's Brier beats the market on the relevant category — this app surfaces edges, but does not auto-size positions off them.

**Terminal vs. barrier (option-pricing terms)**
*Terminal* models the question "what's the chance spot is above $X on DATE?" — only the closing value matters. *Barrier* models "what's the chance spot ever touches $X before DATE?" — any path through the strike counts. Most prediction-market threshold questions are barrier-style ("reach", "hit", "dip to"); a few are terminal ("be above $X on DATE", "close above $X by DATE"). Mixing them up by a few percentage points doesn't matter — mixing them up by 50pp produces phantom edges on already-decided markets.

**Realized volatility**
Standard deviation of daily log-returns, multiplied by √365 to annualize. A simple, defensible estimator of "how much does this asset move." Implied vol from the options market would be sharper but the data isn't free.

**GBM (Geometric Brownian Motion)**
The standard model for asset prices: returns are normal, prices are lognormal. Both probability models in `pm/feeds/crypto_data.py` assume GBM with no drift (risk-neutral with `r=q=0`). It ignores fat tails and jumps, so quoted probabilities should be read as "what the model says given vanilla assumptions," not the truth.

**Reflection principle**
The math that turns "what's the chance of being at level X at time T" into "what's the chance of ever touching level X by time T." For driftless Brownian motion the latter is exactly twice the former; with drift it gets a correction term involving `exp(2μb)`.

**Half-spread**
The distance from the mid quote to the ask (or bid). Real fills happen at the ask when you're buying, which is half-a-spread above the mid. In `pm/research/backtest.py` this is configurable per-run; setting it to 0 recovers the original "ideal mid-fill" PnL math. The default 0.005 (½¢/side) is typical for liquid prediction markets.

**Implied spread cost**
Sum across all backtest trades of `(ideal-mid PnL − effective-fill PnL)`. It's already absorbed into the per-trade `pnl` column, but surfaced as a separate field so you can see how much of your gross edge was paid to liquidity providers.

**Calibration discipline (LLM forecasting)**
LLMs are not naturally well-calibrated forecasters. Asked to estimate probabilities, they tend to over-commit at the extremes ("definitely YES" / "definitely NO") and under-anchor to base rates. The Ollama prompt in `pm/feeds/llm_data.py` explicitly instructs the model to: treat the market price as a strong prior, avoid probabilities >0.95 or <0.05 without overwhelming evidence, and default to 0.5 or the market price under uncertainty. Whether this helps is empirically measurable via the Tier 3b backtest.

**Moneyline (American odds)**
The North American convention for sports-betting odds. A negative number is the favorite (`-200` means "bet $200 to win $100"); a positive number is the underdog (`+170` means "bet $100 to win $170"). Implied probability: negative `-X → X/(X+100)`; positive `+X → 100/(X+100)`.

**Jaccard similarity**
A simple set-overlap metric: the number of items in both sets divided by the number of items in either set (intersection / union). The `manifold` source uses Jaccard on content-word token sets to match Polymarket questions to Manifold questions. **Important limitation**: Jaccard is direction-blind — "Will the Fed cut rates?" and "Will the Fed hike rates?" share most tokens but ask semantic opposites. Always eyeball the matched question on every flag.

**Cross-platform comparison (vs. external data)**
Different mental model from external-data sources like crypto-spot or ESPN. Crypto/sports give us a *model-derived reference value*; cross-platform sources (Manifold, future Metaculus integration) give us *another forecasting community's probability*. An edge there means the two crowds disagree — useful but not the same as "this market is wrong vs. reality."

**Vig (bookmaker's edge)**
Sportsbooks set both sides of a moneyline such that the implied probabilities sum to >100% (e.g. 67% + 37% = 104%). The excess is the bookmaker's profit margin. To compare ESPN's odds-derived probability to a prediction market's quote, you have to *remove* the vig by renormalizing so the two sides sum to 1.

**Training cutoff (LLM)**
Every LLM has a date after which it has no training data. When backtesting an LLM forecaster, this creates an asymmetric bias: markets that resolved *before* the cutoff risk the model recalling the outcome from training data (artificially low Brier), and markets that resolved *after* the cutoff penalize the model for missing post-cutoff information (artificially high Brier). The cleanest backtest window is markets that closed just-after the model's training cutoff.

---

## Alerts

**Alert**
A rule that says "notify me when the price of (market, outcome) goes above (or below) X." In this app, alerts are checked on every page render against the cached market data — no background process is required.

**Triggered**
An alert's state after its threshold has been crossed. Triggered alerts persist with their fire time and the price at which they fired. They do not re-fire on subsequent renders until you **reset** them, so you never get spammed.

**Reset (an alert)**
Manually return a triggered alert to the active state so it can fire again on the next crossing. Useful if you want a recurring alert at the same threshold (e.g. "every time YES goes below $0.20, ping me again").

---

## Sidecar architecture

**Sidecar**
A domain-isolated sub-package that runs on its own schedule, maintains its own state, and exposes a narrow public API to the main app. First instance: `baseball/`. Future instances anticipated: `news/`, `nba/`, `nfl/`, `politics/`. The pattern exists so domain-specific code can change freely without ripple-effects through the market app. See `docs/sidecar-protocol.md` for the canonical spec.

**Sidecar protocol**
The shared *operational* surface every sidecar implements (`health()` / `stats()` / `recent_activity()`) so a single control plane can introspect any sidecar generically. The *domain* surface (`predict_game` for baseball, `recent_since` for news) is unique per sidecar and intentionally not constrained by the protocol.

**Control plane (sidecars)**
A planned dashboard page that aggregates `health()` / `stats()` / `recent_activity()` across all registered sidecars in one view. Deferred until at least two sidecars exist (one isn't enough to know what the patterns are).

---

## Baseball-specific terms

**Pythagorean expectancy (Bill James)**
A team's expected winning percentage given the runs they've scored and allowed: `RS² / (RS² + RA²)`. The simpler exponent-2 form is close enough for our purposes (vs. the pythagenpat 1.83). Pythagorean is a better short-sample "true talent" estimate than raw W-L because random luck in close games washes out faster in run differential than in wins.

**Regressed Pythagorean (Bayesian)**
Our model's headline team-strength metric. Adds 40 virtual games at .500 to the raw Pythagorean before computing, so a 5-1 hot start doesn't read as .800 true talent. Mathematically equivalent to a Bayesian prior centered on .500 with weight = 40 games.

**Bradley-Terry rating**
A general statistical model for paired comparisons (chess Elo, sports power rankings). Each entity has a strength parameter, and `P(A beats B) = exp(strength_A) / (exp(strength_A) + exp(strength_B))`. Our v1 baseball model is a simplified Bradley-Terry — equivalent to using the regressed Pythagorean ratios as the strength parameters and composing in log-odds.

**Log-odds (logit)**
The natural log of an odds ratio: `log(p / (1 - p))`. Probabilities don't add nicely — 50% + 5% + 5% ≠ 60% — but log-odds do. Our matchup model adds team-strength deltas + home-field + pitcher adjustments in log-odds space, then converts back to a probability.

**Home-field advantage (MLB)**
Long-run home teams win ~54% of MLB games. Translates to `+0.161` log-odds (`ln(0.54 / 0.46)`). Smaller than NFL or NBA home-field, but real and persistent.

**Starting pitcher (SP)**
The first pitcher of a game, who throws ~5-6 innings on average. A big factor in single-game outcomes — an ace (sub-3 ERA) vs a replacement-level arm can swing the win probability by 5-10 percentage points before the lineup is announced. Our model adjusts by `0.13 × (4.30 - era)` log-odds for each side's starter.

**ERA (Earned Run Average)**
A pitcher's average earned runs allowed per 9 innings pitched. League average sits around 4.30 in the modern run environment. Sub-3.00 is elite; >5.00 is replacement-level. We use it as the simplest available "pitcher quality" signal — FIP / xFIP / SIERA are more predictive but require more inputs.

**Bullpen game / opener**
A game where no traditional starting pitcher is announced — the team uses a series of relievers. MLB's API often returns no `probablePitcher` for these. Our model treats missing ERA as league-average (0 log-odds adjustment).

**Bullpen ERA**
The reliever-only earned run average for a team. Treated separately from starting-pitcher ERA in our v0.2+ model because bullpens pitch the back ~40% of the game and vary dramatically across teams. Empirically: top bullpens around 2.85, worst around 5.72 — a 2.87-run gap that swings late-game win probability.

**OPS (On-base Plus Slugging)**
A simple single-number offensive metric: a hitter's on-base percentage plus slugging percentage. League average is ~0.720 in the modern era. Star hitters live in 0.900+; replacement-level falls to 0.600-0.650. Used in our v0.4+ model as the per-lineup average to capture day-of variation when stars rest.

**Park factor**
The ratio of run production at a specific stadium vs the league average. Coors Field plays ~1.30× because altitude + dry air let the ball fly; Petco Park, Marlins Park, and Oracle Park play below 1.0× due to cool air, marine layer, and pitcher-friendly dimensions. Park factors mostly inflate or deflate *both* sides' run totals equally, so they matter more for over/under markets than win probability — but a team that plays disproportionately at an extreme park has skewed RS/RA that distorts their true talent. Our model park-neutralizes team stats before computing strength.

**Pythagorean expectancy (Bayesian regressed)**
The basic Pythagorean is `RS² / (RS² + RA²)` — a team's expected winning percentage from runs scored vs runs allowed. We add a 40-game Bayesian prior at .500 before computing the ratio, so a 5-1 hot start doesn't read as .800 true talent. Park factors are applied before this step in v0.2+.

**Recent-form blend**
The mechanism by which our v0.3+ model lets the last 15 days of play influence team strength. Computed as `α × recent_pythagorean + (1-α) × season_pythagorean` where `α = min(games_in_window / 12, 0.30)`. The cap prevents a 4-game hot streak from dominating; the ramp prevents tiny samples from gaining undeserved weight.

**Lineup OPS**
The mean OPS of a posted batting order (minimum 50 AB per included batter to filter call-up noise). Compared against the 0.720 league baseline and converted to a log-odds adjustment via `LINEUP_LOGODDS_PER_OPS = 3.0`. Captures day-of variation: stars sitting, hot prospects called up, platoon switches.

**Lead-lag analysis**
The proposition that market prices (Kalshi books) react to game events (plays) with measurable delay — and that the delay window might be exploitable. Our system captures both streams forward-only and joins them in `baseball/analysis.py`. Meaningful signals require ~1 week of paired data; smaller samples produce false confidence.

**Paper trading vs live trading**
Paper = simulated orders recorded to a profile's local files (no money, instant fill at the quoted price). Live = real orders on the exchange with real money. In this project the two share identical strategy logic and differ only in the `Broker` used (see below); live is off by default and must be explicitly enabled per profile.

**Broker (execution seam)**
The object a strategy daemon uses to buy/sell and to read positions/cash, so the *same* strategy runs paper or live unchanged. `PaperBroker` writes to the local portfolio; `LiveBroker` places real Kalshi orders and reads truth from the authenticated account. Chosen at runtime by `execution.get_broker(profile)`.

**Limit order**
An order to trade at a stated price *or better*. A limit **buy** at 60¢ fills at ≤60¢; a limit **sell** at 60¢ fills at ≥60¢. Consequence: a limit sell placed *below* the current market fills immediately — so a limit order alone cannot express a downside stop.

**Marketable order**
A limit order priced aggressively enough to cross the spread and fill right away (e.g. a sell placed a few cents *below* the best bid). Used for exits so a stop or scalp actually fills even if the book moved since the price was read — the practical substitute for a market order on the audited limit path.

**Stop-loss**
"Sell if the price falls to X" — caps the downside on a position. Because Kalshi has no native stop order type, our daemon implements it client-side: it watches the price and fires a marketable sell when the loss crosses the threshold. Distinct from a take-profit (sell on the way *up*), which *can* rest on the venue as an ordinary limit.

**Owner profile (live-account lock)**
The profiles authorized to drive the real Kalshi account: the owner (`KALSHI_OWNER_PROFILE`) **plus** any in `LIVE_TRADING_PROFILES` (`kalshi_auth.authorized_profiles()`). `is_configured()` requires the active profile to be one of them, so paper profiles outside the set physically cannot place live orders — but several live arms can each authenticate (combined with position-scoping so they don't touch each other's holds).

**WPA (Win Probability Added)**
The change in a team's win probability caused by a single play: WP-after − WP-before. Win Probability is the estimated chance a team wins given the full game state (inning, score, outs, runners on base). A play's WPA measures its *actual impact on who wins* — a solo HR is huge WPA in a tie-game 9th, ~nothing in a blowout. We compute it from the situation via `baseball.analysis.wpa_for_play` (RE24 run-expectancy matrix + normal approximation); the reactive scalper (#19) gates entries on |WPA| because it's the direct measure of what the book should reprice on. Positive WPA helped the batting team; negative helped the fielding team (a rally-killing double play).

**Leverage (situation)**
How much the current game state lets a single play swing the outcome — high late + close, low early or in a blowout. High WPA only occurs in high-leverage situations, so a |WPA| gate is implicitly a leverage filter.

**Reaction edge / lag (in-game speed)**
The observation that the order book reprices *slowly* (tens of seconds) after a high-impact play, so a fast reactor can buy the benefiting side before the book catches up. Distinct from predicting the final outcome — it exploits the market being *slow*, not *wrong*. The only edge that survived the calibration analysis (the MLB game-winner market is otherwise efficient).


## Operations & evidence terms (added 2026-08-17)

**Out-of-sample (OOS)** — results produced by a rule *after* that rule was
last changed. Results from before are "in-sample": the rule was chosen partly
to fit them, so counting them as evidence overstates it. `pm/ledger/oos_ledger.py`
enforces the split automatically.

**Rules-frozen date** — when an arm's selection parameters last changed. Any
edit resets it, and the evidence clock with it. Visible via
`python -m oos_ledger --status`.

**Independent cluster** — one genuinely independent observation. Members of a
one-winner field resolve together, so N correlated positions across K events is
K observations, not N. **Judge on mean outcome per cluster, never on win rate**
— arms here win 80–95% of trades and can still lose money.

**Fail closed** — when a safety check cannot determine the truth, it blocks
rather than allows. The kill switch reports ACTIVE on I/O error; the drawdown
breaker blocks when account equity is unknown. On a money path, not knowing is
not the same as being fine.

**Drawdown circuit breaker** — blocks new orders once account equity falls a
configured fraction below its persisted high-water mark. Measured account-wide
(several arms plus hand-trading share one pool), so it doubles as the
fleet-wide exposure limit.

**Tripwire (test)** — a session-scoped fixture that fingerprints the live
`data/` tree and fails the run if the test suite modified it. Exists because
tests wrote into production stores three separate times.

**Shared-secret gate** — the token check on the web UIs. One token, no users,
no revocation: a large improvement over nothing, and explicitly *not*
authentication.

**Armed** — a profile that can actually place real orders: live broker **and**
its own arm-level switch on. Membership in the allow-list alone is not armed.
