# External Review Brief — Kalshi Trading System

*A briefing for an outside model (ChatGPT) to critique our direction and suggest leads we may have missed. Written 2026-06-28. Be skeptical — tell us where we're wrong, what we're missing, and what you'd do differently.*

---

## 1. What we're doing & the goal

We run an automated trading system on **Kalshi** (a regulated US prediction-exchange: binary event contracts that pay $1 if YES resolves, $0 if NO; prices are probabilities in cents). The markets we focus on are **in-game sports outcomes** (mostly MLB: "will team X win this game").

**The goal:** get on Kalshi's profit leaderboard. We treat it like an idle game — autonomous daemons run strategies and collect data 24/7; a human (solo operator) steers when present. The **binding constraint is finding a real, repeatable edge** — not engineering. Almost everything we've tried is efficient-market-dead; we have exactly one signal that survives scrutiny.

**Operating constraints:**
- Solo operator, consumer hardware (one box, RTX 5000 Ada 32GB GPU).
- Strong preference for **free/public data + local LLMs (Ollama)** — no paid data feeds or paid LLM APIs by default.
- Real money is live but **tiny** ($10/order cap) — a canary to prove execution, not size yet.
- Python; Streamlit analysis UI; per-strategy daemons as systemd services; SQLite for all stores.

---

## 2. The one edge we've validated (and its limits)

**In-game reaction speed / "the book lags true win-probability."** When a high-impact play happens (big swing in win probability), the Kalshi order book is **slow to reprice** — for ~60 seconds the benefiting side is buyable below fair value. We buy the beneficiary fast and **hold to resolution**.

Evidence & nuances we've measured:
- **Leverage-conditioned**: full-sample reaction ≈ 0 (most plays don't move price). The edge shows up in **high-leverage** plays (late innings, close score): ~**+2.5¢/contract** mean, persists ~60s.
- **Thin-book concentrated**: the edge lives where few market-makers are repricing. Thin-tercile books show ~+7.7¢/contract; deep/liquid books show *negative*. So we gate out already-liquid books.
- **Swings STICK, they don't revert**: we tested a "fade the overreaction" idea — measured 354 swings; the price settles at ~the peak and stays (98% of an underdog swing's move persists). Fading loses, worst when the swing contradicts the favorite (because the swing reflects *real* info). So speed-toward-the-beneficiary is right; mean-reversion is wrong.
- **P&L is fat-tailed / outlier-dependent**: paper track is +$— over 40 closes, but **one trade (+$—) carries it**; the other 39 net +$—. Cheap-underdog beneficiaries (entry <$0.45) drive all the profit (lottery payoff); the ≥$0.45 body is net-negative despite a 67% win rate. We've explicitly decided **not to tune entry gates on this small sample** (curve-fitting risk) and to revisit at ~100 resolved trades on *trimmed* P&L.

---

## 3. What we've tried and KILLED (please don't re-suggest these without a new angle)

All falsified with data on this venue:
- **Static/pre-game outcome prediction** — our model (Pythagorean+) and ESPN de-vigged consensus both ≈ the market. No gap to trade. Sports markets are efficient (calibration Brier ≈ 0.005).
- **"Buy moderate favorites and hold" (favorite-hold)** — 164 trades, −$—. Moderate favorites win *at or below* their price. Retired.
- **Close-game scalping / longshot entry** — both net-negative; retired.
- **Fading in-game overreactions** — swings stick; fade is negative, worst when contradicting the favorite (see above).
- **Crypto latency arb** (does the Kalshi crypto book lag Coinbase spot?) — no capturable lag; crypto is HFT-saturated, book moves lockstep with spot (<20s). Retired.
- **News-reaction speed** (free RSS) — detection lag median ~150 min; far too slow to beat liquid markets.
- **Cross-market arbitrage** — scanned 3000+ markets with executable bid/ask + fees; no real arb. Efficient venue.
- **Mean-reversion on range-bound markets** — backtested negative.

The throughline: **static/analysis edges are dead on this venue; only an information/speed edge survives.**

---

## 4. Current live state

- One live arm: in-game **reaction-and-hold** on MLB, $10/order cap, RSA-signed orders via Kalshi's v2 order API.
- Recently fixed: (a) migrated to Kalshi's v2 order endpoint after the v1 was deprecated; (b) **entries now cross the spread** — we discovered ~33% of live reaction buys were resting *at the stale ask* and never filling.
- First full live cycle: 6 bets, 4W–2L, **+$—** (net positive, but n=6 — proves the *plumbing*, not the edge).
- Paper shadow arms run in parallel for track record.

---

## 5. Our abilities / what we have built

- **Data feeds (live):** Kalshi WebSocket (order-book deltas + executed-trade tape → SQLite); MLB Stats API live play-by-play poller (10s); ESPN public APIs (scoreboards, win-probability for WNBA/NBA, betting consensus).
- **Historical stores:** 21M+ Kalshi order-book snapshots; executed-trade archive; near-close price+outcome panel; per-sport capture DBs measuring in-game-swing → book-move lag (MLB, WNBA, tennis).
- **Models:** baseball win-prob (WPA / RE24); Elo (tennis); Pythagorean (WNBA); local-LLM forecasting + deep analysis via Ollama (free, on-GPU).
- **Sidecars** (domain-isolated sub-apps with their own FastAPI UIs): baseball, WNBA, tennis.
- **Execution:** a broker abstraction (paper ↔ live) with pre-flight risk checks (stake cap, daily cap, fat-finger band, drawdown breaker), emergency stop, full audit log.
- **Idle/measurement tooling:** arb scanner, inefficiency/calibration sweeps, control-plane ops dashboard, backtest framework (lookahead-safe).
- **Unused assets we own:** real-time **audio transcription** (faster-whisper on GPU) + a baseball **broadcast-event NLP extractor** — built, idle. Radio play-by-play calls events ~5–15s before the stats API logs them.

---

## 6. The current focus & roadmap (what we just decided — critique this)

**Key finding:** our median **detection latency is ~28s** (play happens → we act), but the edge only persists ~60s. **We forfeit ~46% of the edge window before acting**, missing the freshest/largest part of the move. Latency is the master profit lever.

Prioritized plan:
1. **Measure the latency budget** — split `play_ts → detected → ordered → filled` to see how much of the 28s is the stats API's own lag vs our 10s play-poll vs our 12s strategy cycle.
2. **Tighten polling cadence** (cheap; the two intervals stack into the 28s).
3. **Broadcast-audio trigger** — use our idle audio→event pipeline to react *before* the stats feed updates (get under the data-source floor). Biggest swing.
4. **Go where books are thin** — reframe "go wide" as neglected thin in-game markets MMs ignore, not marquee liquid sports.
5. **WP-gap continuous trigger** — fire on market-price-vs-live-win-prob divergence, not just discrete plays (more signals + a fair-value anchor).

We assessed **Kalshi "combos" (parlays)** and set them aside: RFQ/market-maker-priced with no public order book → no lag to exploit, and we'd be the retail side against pros (a "multivariate" API endpoint group exists, so they *may* be automatable, but the no-edge objection stands).

---

## 7. Questions for you (ChatGPT)

1. **Is latency really the right priority**, or are we over-indexing on one number? What would you attack first?
2. **Faster info, cheaply:** beyond broadcast audio, what *free/public* sources could get us under the stats-API floor for live sports (or other domains)? Any we're not thinking of?
3. **Where else might a speed/lag edge exist** on a venue like Kalshi that we haven't probed? (We've ruled out crypto-vs-spot, news-RSS, and marquee liquid sports.)
4. **Thin-book edge, but thin = low capacity.** How would you think about scaling a real but capacity-limited, fat-tailed edge toward leaderboard-level P&L without destroying it?
5. **Fat-tail / small-sample discipline:** given the edge's profit is outlier-dependent, how would you decide it's real vs. luck, and when to size up? What sample size / statistic would convince you?
6. **Anything we've prematurely killed** in §3 that deserves a second look with a different framing?
7. **Blind spots:** what are we systematically not seeing? Different market types, different edges (microstructure, settlement timing, correlation, behavioral), or a fundamentally different approach to "get on the leaderboard"?

Be direct and concrete. We value a falsifiable suggestion we can measure over a plausible-sounding one we can't.
