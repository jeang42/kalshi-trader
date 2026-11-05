# User Guide

> **New to prediction market terminology?** See the [Glossary](glossary.md) for plain-English definitions of terms like spread, implied probability, Kelly fraction, Brier score, CLOB, and more.

## Profiles (multi-user)

The app supports multiple users on a single machine via **profiles**. Each profile has its own isolated portfolio, forecasts, alerts, and settings; switching profiles is a one-click sidebar action and takes effect instantly.

Three profiles ship by default:

| Profile | Purpose |
|---|---|
| **jeff** | Primary user. Selected by default. Legacy data files at the project root are migrated here on first launch. |
| **dex** | A second real user. Same shape as `jeff`, completely independent state. |
| **sandbox** | Playground / paper trading. Default $1,000 bankroll, clear "PAPER TRADING" banner across action pages, "SIMULATED" watermark on the Portfolio chart so screenshots can't be mistaken for real performance. Visit Settings → 🧹 Reset sandbox to wipe everything clean and start over. |

Create a fourth (or fifth, etc.) profile via Settings → Create a new profile.

All profile data lives under `data/<profile>/` and is gitignored. There is no authentication: anyone with shell access can see every profile's files. Adequate for a family laptop or shared workstation; not for an internet-exposed deployment.

## Paper trading

The **sandbox** profile is the app's paper-trading mode. The app never places real orders on any profile — buys and sells are always recorded manually — but the sandbox makes the simulated nature unmistakable:

- **Default $1,000 bankroll** (change anytime in Settings)
- **Visible paper-trading banner** at the top of every action page (Kelly Sizer, Portfolio, Alerts)
- **"SIMULATED" watermark** diagonally across the Portfolio value-over-time chart so a screenshot can't be mistaken for real performance
- **🧹 Sandbox mode** indicator pinned in the sidebar whenever sandbox is active
- **One-click reset** via Settings → 🧹 Reset sandbox

### The paper-trading workflow

1. Sidebar → switch to **sandbox**
2. Visit ⚙️ Settings → confirm the bankroll, adjust if you want a different starting capital
3. Find a market you'd like to bet on (Markets page or Scanner)
4. Open Kelly Sizer → set your probability estimate → note the recommendation
5. Go to Portfolio → **🟢 Buy** → enter the market, outcome, shares, and price *as if you'd placed the trade*
6. Watch unrealized P&L move with live prices. Drawdown, sector exposure, and Tier 1 risk caps all apply in sandbox just like a real profile.
7. When the market resolves, **🔴 Sell** at the final price (Polymarket marks resolved sides at $1.00 or $0.00) or simply delete the row
8. Once you've made some forecasts via the Kelly Sizer's **💾 Save forecast** button, the Calibration page starts scoring your probability estimates — compare your Brier against the market baseline to see if your "edges" are real
9. Reset sandbox anytime; your `jeff` (and `dex`) profiles are completely untouched

### When to use it

- **Learning** — practice Kelly sizing and forecasting discipline without risking money
- **Validation** — check whether your "edge" actually produces realized P&L over many trades
- **Onboarding** — let a new user (your son, for example) experiment safely on `sandbox` before they touch their own real profile
- **A/B testing** — try a category or strategy in sandbox first; promote to a real profile only after the calibration data on the Calibration page shows you're not losing money

Paper profiles (`sandbox`, `agentic_*`, `dex`) never place real orders — they record trades to local files only. Real orders are possible **only** on the owner profile and **only** when live trading is explicitly enabled (see *Enabling live trading* below and `docs/live-execution-adapter.md` for autonomous execution). Sandbox is paper trading by *replacing the real-trade step* with manual recording; the rest of the workflow (sizing, tracking, scoring) is identical to what you'd do on a real profile. See `QUICKSTART.md` for the broader "what this is vs. isn't" framing.

## Running the system

The project is several cooperating processes, not one binary. Here's everything you can start, what it does, and why:

### Main Streamlit app (foreground, port 8501)

The dashboard you interact with — markets, scanners, Kelly sizer, portfolio, calibration, settings, all the rest.

```bash
./run.sh                          # default port 8501
./run.sh --server.port 8502       # custom port
PORT=8080 ./run.sh                # same via env var
```

Creates `.venv/` and installs dependencies on first run. Open `http://localhost:8501` once it starts. The sidebar tells you when the page last rendered (data is cached up to 60s).

### ⚾ Baseball web UI (multi-user, port 8770)

A separate FastAPI app served by the baseball sidecar — diamond-themed today's slate, win probabilities, standings, line-movement history. **Not Streamlit**, so multiple devices on your LAN can use it simultaneously without stepping on each other.

```bash
scripts/baseball_web.sh                 # foreground
scripts/baseball_web.sh --background    # detach, log to data/shared/baseball_web.log
```

Then open `http://<your-LAN-IP>:8770` from any device. The main Streamlit app's sidebar has a "⚾ Baseball ↗" link that auto-resolves your LAN IP.

### ⚾ Baseball live event collector (long-lived daemon)

Polls MLB's `feed/live` endpoint every 10s (matching MLB's CDN cache TTL) for every in-progress game and persists every play to the `live_plays` table. Average play detection latency: ~5 seconds. Feeds lead-lag analysis and the pre-positioning strategy. Cheap when no games are running.

```bash
scripts/baseball_live.sh                # foreground (Ctrl-C to stop)
scripts/baseball_live.sh --background   # detach
scripts/baseball_live.sh --status       # is it running?
scripts/baseball_live.sh --stop         # stop the background daemon
```

Safe to leave running 24/7. Log: `data/shared/baseball_live.log`.

### ⚾ MLB book poller (long-lived daemon)

Captures Kalshi order books for every open `KXMLBGAME-*` market via REST polling (15s during live games, 120s idle), writing to `book_archive.sqlite`. **Largely superseded by the Kalshi WebSocket streamer** (`kalshi-ws` service) which provides sub-100ms order book updates. The REST poller runs as a fallback and for non-MLB markets.

```bash
scripts/mlb_book_poller.sh                # foreground
scripts/mlb_book_poller.sh --background   # detach to data/shared/mlb_book_poller.log
scripts/mlb_book_poller.sh --status
scripts/mlb_book_poller.sh --stop
```

Cheap when no games are open (one events fetch returns zero markets). Safe 24/7.

### Systemd user services (recommended — auto-restart on boot)

The long-lived daemons are configured as **systemd user services** so they survive reboots and power outages automatically. Unit files at `~/.config/systemd/user/`.

```bash
# Check status
systemctl --user status baseball-web baseball-live mlb-book-poller kalshi-ws pre-position

# Restart one after a code change
systemctl --user restart baseball-live

# Tail logs
journalctl --user -u mlb-book-poller -f

# Reload after editing a .service file
systemctl --user daemon-reload
```

`loginctl enable-linger` is set so services start at boot without a login session. The shell launcher scripts (`scripts/*.sh --background`) still work for ad-hoc use but are no longer needed for persistent operation.

**After a power outage**: daemons restart automatically. Order book snapshots during downtime are lost (no historical API). Play-by-play can be backfilled: `.venv/bin/python -m baseball.backfill --start YYYY-MM-DD --end YYYY-MM-DD`. Then run `.venv/bin/python auto_resolve_runner.py` to catch up on missed resolutions.

### Cron-driven background jobs (install once, runs forever)

All wrapped with `flock` so overlapping invocations skip instead of piling up. View the active list with `crontab -l`.

| When | What | Purpose |
|---|---|---|
| every 5 min | `pm/capture/book_poller.py` | Snapshots order books across registered providers to `book_archive.sqlite`. |
| `:00,:30` (every 30 min) | `strategy_runner.py --strategy market_consensus` | Paper-trades the simplest "follow the line" strategy under profile `agentic`. |
| `:00,:30` | `strategy_runner.py --strategy favorite_grind` | Paper-trades the "pennies in front of a steamroller" strategy under `agentic_favorites`. |
| `:15,:45` | `strategy_runner.py --strategy llm_forecast` | Local Ollama LLM forecast strategy under `agentic_llm`. |
| `:10,:40` | `strategy_runner.py --strategy baseball_winprob` | Our baseball sidecar's predictions vs Kalshi MLB markets, under `agentic_baseball`. |
| every 15 min | `pm/ops/auto_resolve_runner.py` | Walks every profile and settles forecasts whose underlying markets have closed. |
| `0,6,12,18` UTC | `pm/ops/snapshot_runner.py` | Records portfolio value snapshots for every profile with open positions — builds the value chart without visiting the UI. |
| `0 3-7am` | `pm/ops/bulk_deep_analysis.py` (5 entries, one per model) | Nightly head-to-head LLM forecast batch — qwen3, qwen2.5, deepseek-r1, gemma3, llama3.1. (qwen3.5 dropped — too slow.) |
| `0 2am` | `logrotate` | Rotates the cron log files daily, keeps 14 days. |
| `15 4am` | `baseball.runner` | Nightly full refresh: team season stats (incl. bullpen ERA), pitchers, 7-day schedule, batters' OPS for tomorrow's lineups, park factors, predictions. |
| every 30 min | `baseball.runner --lineups-only` | In-day lineup catcher. Lineups post 60-90 min before first pitch, well after the nightly runner; this picks them up + recomputes affected predictions. |
| `30 0am` | `scripts/backup.sh` | Daily `data/` snapshot to `/mnt/backups/kalshi-trader/` with 90-day hardlinked retention. |

To install the cron lines fresh on a new machine, copy them from a working `crontab -l` output and `crontab` the file back. (No automated installer yet — TODO.)

### One-off / manual runs

| Need | Command |
|---|---|
| Manually refresh baseball state right now | `.venv/bin/python -m baseball.runner` |
| One bulk deep-analysis pass across all models | `scripts/run_all_models.sh 15` (N markets per model) |
| Force a fresh backup snapshot | `scripts/backup.sh` |
| One auto-resolve cycle | `.venv/bin/python auto_resolve_runner.py` |
| Snapshot all profiles now | `.venv/bin/python snapshot_runner.py --min-interval-hours 0` |
| Transcribe a live audio stream | `.venv/bin/python audio_transcribe.py "https://stream-url" --model small` |
| Test baseball event extraction | `.venv/bin/python broadcast_events.py` |

### Order of operations on a fresh checkout

1. `./run.sh` once to create `.venv/` and install deps.
2. `.venv/bin/python -m baseball.runner` to seed baseball state (~5 sec).
3. Enable systemd user services for the long-lived daemons:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now baseball-web baseball-live mlb-book-poller kalshi-ws pre-position
   loginctl enable-linger $USER
   ```
4. Install cron entries (see table above) for ongoing collection + paper trading.

Use the sidebar to navigate between pages. The sidebar also shows when the page was last rendered (data is cached up to 60s).

---

## Markets Page

The default landing page. Shows all active Polymarket markets sorted by 24-hour volume.

**Filters (applied client-side — no extra API calls):**

- **Search** — free-text filter on the market question. Case-insensitive, partial match. Example: `bitcoin`, `NBA`, `trump`.
- **Category** — predefined buckets: Crypto, Entertainment, Finance, Geopolitics, Politics, Sports, Tech, Other. Categories come from authoritative Polymarket event tags when available, with keyword inference as a fallback. Most markets are now categorized via the API.
- **Show top** — limit the table to the top 50 / 100 / 200 results after filtering.

**What you see:**

- Three summary metrics: market count, combined 24h volume, combined liquidity.
- Bar chart of the top 20 markets by 24h volume.
- Full scrollable table with question, category, 24h volume, liquidity, current outcome prices, **days to resolution**, and end date.
- **📥 Download as CSV** button for the visible (filtered) markets.

---

## Price History Page

Charts the YES and NO token prices over time for any market.

**Controls:**

- **Market selector** — dropdown of all loaded markets sorted by volume.
- **Candle size** — 1m, 5m, 15m, 1h, or 1d resolution.
- **Days of history** — slider from 1 to 90 days.

**Current prices** are shown as metric tiles above the chart (mid-quote from the 60-second-cached snapshot).

**🟢 Live ticker** under the cached tiles refreshes the midpoint for each outcome every 3 seconds, with a small delta showing the change since the previous tick. Useful for watching a market move in real time without reloading the page. The ticker only re-fetches the prices — the rest of the page (history chart, order book) stays put.

**Chart** shows one line per outcome (YES / NO, or named outcomes for multi-outcome markets). Hover for exact prices; use the Plotly toolbar to zoom or pan.

**📚 Order Book Depth** expander (below the price chart) — shows the live bid/ask depth for each outcome side-by-side. For each outcome you get:

- **Best bid / best ask / spread** — the top of book.
- **Total liquidity per side**: how many shares are resting in the book at each level.
- **Depth chart**: a staircase plot of cumulative size vs. price. Green = bids (lower prices, walking down from best), red = asks (higher prices, walking up from best). The taller the wall, the more you can trade without moving the price.
- **Top levels** table — every single bid and ask line item in the book.

Use this **before** sizing a meaningful position from the Kelly Sizer. If Kelly tells you to bet $1,200 but the ask depth at the quote is only $50, you'll move the price through several levels (slippage) trying to fill that order.

**Show raw price history data** expander — the underlying timestamp/price rows.

**Note:** some markets show no history if they have low trading activity or are not CLOB-enabled. Same applies to order books — newly-listed or near-resolution markets sometimes have a one-sided or empty book.

---

## Market Scanner Page

Three tabs for finding trading opportunities. All scans run on the cached market dataset — no extra API calls.

**Sidebar control:** Min liquidity ($) applies across all three tabs.

Each tab has its own **📥 Download as CSV** button for the current scan results.

### Tab 1 — Narrow Spreads

Lists markets with the tightest bid-ask spread (cheapest to enter and exit). Useful for:
- Finding actively-traded markets with real two-sided liquidity.
- Identifying good entry points — a 0.001 spread on a $0.50 market costs $0.001 per share in slippage.

Columns: question, category, YES price, spread, best bid, best ask, 24h volume, liquidity.

### Tab 2 — Price Divergence

Markets where the **last traded price** differs significantly from the current **mid-quote**. This can indicate:
- A stale quote (market maker hasn't updated).
- Thin book — one trade moved the price far from the quote.
- Rapid information arrival — the market moved fast and quotes haven't caught up.

The diagonal reference line on the scatter plot marks perfect agreement. Points far from the line are worth investigating.

### Tab 3 — Multi-Outcome Gaps

Markets with three or more outcomes where the prices don't sum to $1.00. In a fair binary market YES + NO always equals exactly $1 (they're complementary). In multi-outcome event markets this isn't enforced, so gaps can appear.

- **Sum < 1**: at least one outcome is underpriced relative to the others.
- **Sum > 1**: collective overconfidence, or stale quotes in a thinly traded book.

### Tab 4 — Cross-Market Arbitrage

Different from Tab 3 — here we look at **groups of binary markets** that share an `event_id`. Polymarket bundles related markets under one event (e.g., "2026 FIFA World Cup Winner" has 48 sub-markets, one per team). If those sub-markets are mutually exclusive *and* exhaustive, their YES prices should sum to exactly $1.00. When the sum is off, there's a candidate arbitrage:

- **Sum < 1**: buy one YES of every sub-market → guaranteed $1 payout when one resolves YES. Implied edge = (1 − sum) / sum.
- **Sum > 1**: the inverse — the basket is overpriced. Captured by buying every NO (Polymarket doesn't natively short).

**Critical caveat**: mutual exclusivity isn't verified automatically. The tool filters out clearly non-exclusive events via a plausibility band (default ±30% around 1.0), but you still need to read the sub-market questions to confirm. For example, an event titled "2026 macro predictions" with markets like *"Will inflation > 5%?"* and *"Will unemployment > 4%?"* is **not** mutually exclusive — the sum is unconstrained.

Controls:
- **Min markets per event** — skip events with few sub-markets (noise filter).
- **Min |sum − 1|** — minimum gap to report. Below this, transaction costs eat the edge.
- **Plausibility band** — only events with sum within ±this of 1.0 are reported. Tighter = fewer false positives but might miss arbs.

Output columns: event title, category, number of sub-markets, sum of YES prices, deviation, implied edge %, total liquidity, sample of the first few questions.

---

## Kelly Sizer Page

Calculates how much of your bankroll to bet on any market given your own probability estimate. See the [Glossary — Kelly Criterion](glossary.md#kelly-criterion--bet-sizing) for a full explanation of the math.

**Controls:**

- **🔎 Look up a specific market by ticker or URL** — expander at the top. Paste a Kalshi URL (e.g. `kalshi.com/markets/.../kxky4r-26`), an event ticker (`KXKY4R-26`), or a specific market ticker (`KXKY4R-26-TMAS`). The looked-up market is prepended to the picker — useful for markets that aren't in the volume-ordered default fetch (low-volume, long-dated, niche events).
- **Market selector** — pick any active market.
- **Bankroll** — your total trading capital in dollars. All dollar recommendations are relative to this number. The page-level bankroll input overrides the saved Settings value for this session only.
- **Your probability sliders** — one per outcome. Drag to your estimate of the true probability. The slider starts at the current market price (no edge). For binary YES/NO markets the two sliders carry the same information mirrored (Yes=0.30 is equivalent to No=0.70); pick whichever framing is more natural.

**What you see:**

- **Current market prices** — the market's implied probabilities shown as metric tiles.
- **Edge** — your estimate minus the market price. Positive means you think the market underprices this outcome.
- **Expected value per $1** — average profit on a $1 bet if your estimate is correct.
- **Full Kelly** — the mathematically optimal bet fraction and dollar amount.
- **½ Kelly (recommended)** — half the full Kelly, which most practitioners prefer. Cuts variance significantly while giving up little long-run growth.
- **Kelly curve chart** — shows how the recommended bet fraction changes across all possible probability estimates. The curve crosses zero at the market price (where your view matches the market and there's nothing to bet on).
- **💾 Save forecast for {outcome}** button per outcome — records your probability estimate for later scoring on the Calibration page. Use this every time you take a position with a Kelly-sized bet.

**Risk-adjusted Kelly (Tier 1 recommendations)**

The "Recommended" column shows **raw Kelly × calibration multiplier**, capped at the global limit you set in **⚙️ Settings**. The multiplier comes from your historical Brier score in this market's category:

| Your Brier (category) | Multiplier | Meaning |
|---|---|---|
| ≤ 0.10 | 1.0 | Sharp — full Kelly allowed |
| 0.10 – 0.20 | 0.5 | Solid — half Kelly |
| 0.20 – 0.25 | 0.25 | Marginal — quarter Kelly |
| > 0.25 | **0** | Worse than random — don't bet your own number; match the market |
| No data yet | 0.5 | Half-Kelly default until you accumulate ≥ 5 resolved forecasts in the category |

A blue info banner above each market tells you the category, your current Brier, and which band you're in.

**Drawdown circuit breaker**

If your portfolio's current value is more than the configured threshold (default 15%) below its all-time peak, a red banner appears at the top of the Kelly Sizer page recommending you pause and review. Same banner appears on the Portfolio page.

**Practical notes:**

- The risk-adjusted Kelly is now the recommended number, not raw Kelly. Raw Kelly is still shown so you understand what's being trimmed and why.
- Per-market position-size cap (default 5% of bankroll) warns when the recommendation would exceed it.
- Kelly assumes you can make many similar bets over time. For a one-off event, treat the output as a ceiling, not a target.

---

## Sandbox Portfolio Page

This is the **paper-trade log** for the active profile — *not* your real Kalshi state. The Live Portfolio page (further down in this guide) is where your actual Kalshi balance, positions, fills, and settlements live. The two are intentionally decoupled: paper profiles (`sandbox`, `agentic_*`, `dex`) can't reach the Kalshi auth API at all, so Sandbox Portfolio is the only portfolio view available to them.

For the owner profile (`jeff`), this page is largely redundant with Live Portfolio. Manual buy/sell entries still work if you want to log hypothetical trades or annotate decisions with notes, but the real-money trade flow goes through the 🚀 button on Kelly Sizer and lands on Kalshi directly.


Tracks your Polymarket positions locally as an append-only log of buy and sell transactions. Open positions and P&L are derived from this log using the **average-cost method**. No connection to your actual Polymarket account — you record transactions manually.

### Summary

Four metric tiles at the top:

| Metric | Meaning |
|---|---|
| Cost Basis (open) | What you paid for currently-held shares |
| Current Value | Open shares × current market mid-price |
| Unrealized P&L | Current value − cost basis. Positive = open positions are up. |
| Realized P&L | Cumulative profit/loss from completed sells |

### Portfolio Value Over Time

After your second visit (snapshots are taken automatically once per hour), a line chart appears showing **net value** (mark-to-market open positions + cumulative realized P&L) with the **cost basis** as a reference. A max-drawdown stat appears underneath once you've experienced any drawdown from peak. The history builds itself silently — there's no "save" button to remember.

Below the metrics, a **sector exposure donut chart** breaks down your open cost basis by category (Crypto, Sports, etc.).

### Open Positions Table

One row per (market, outcome) pair with shares > 0. Columns include `question`, `outcome`, `category`, `shares`, `avg_price`, `cost_basis`, `current_price`, `current_value`, `unrealized_pnl`, `pnl_pct`, and `days_to_resolution`.

A ⚠️ warning appears if any positions resolve within 7 days.

**📥 Download positions as CSV** for full numeric data.

### Realized P&L

Expand "Realized P&L by market" to see per-market realized profit/loss from your sells.

### Recording a Transaction

Three tabs at the bottom of the page:

#### 🟢 Buy
1. Select a market from the dropdown.
2. Choose the outcome.
3. Enter shares and your buy price. The price field pre-fills with the current market price.
4. Click **Record buy**.

If you buy the same (market, outcome) twice, the position is automatically aggregated using a weighted average price.

#### 🔴 Sell
1. Pick from your open positions in the dropdown.
2. Enter shares to sell (defaults to all). You cannot sell more than you hold.
3. Enter your sell price (defaults to the current market mid). Sub-$0.001 prices are allowed for near-resolution markets.
4. The **estimated realized P&L** is shown as a preview.
5. Click **Record sell**.

Realized P&L is computed using the average-cost method: each sell takes its proportional share of the running average cost basis.

#### 📜 History
- Full chronological transaction log.
- **📥 Download transactions as CSV**.
- **⚠️ Delete a transaction (advanced)** expander — for typo correction only. Deleting an old buy can retroactively invalidate later sells.

### Storage

Transactions **and snapshots** are saved in `portfolio.json` in the project directory. This file is gitignored and never leaves your machine. Old (v1) `portfolio.json` files from before the transactions refactor are migrated automatically on first read. Pre-snapshots v2 files also work — the snapshots array starts empty.

---

## Calibration Page

Tracks whether your probability estimates from the Kelly Sizer are actually accurate. The Kelly formula amplifies your edge — but only if the edge is real. This page measures it.

### Workflow

1. On the **Kelly Sizer**, save your probability estimates with **💾 Save forecast**.
2. When markets resolve in real life, come back here and click ✓ Yes or ✗ No for each pending forecast.
3. Once enough forecasts are resolved, the **Brier score** and **calibration plot** become meaningful.

### What You See

**Scoreboard** (four tiles):

| Metric | Meaning |
|---|---|
| Total forecasts | Everything you've ever saved |
| Resolved | Forecasts with a recorded outcome |
| Pending | Awaiting resolution |
| Brier score | Your accuracy. 0 = perfect, 0.25 = random, lower is better |

**Calibration plot**: a scatter of your *predicted* probability against the *observed* resolution rate, binned in 10% intervals.

- **Dots on the diagonal** = well-calibrated (predictions match reality).
- **Dots below the diagonal** = overconfident at that probability range.
- **Dots above the diagonal** = underconfident.
- **Dot size** scales with the number of forecasts in that bin — small dots are noisy estimates.

**Pending forecasts**: one row per forecast awaiting resolution, with ✓ Yes / ✗ No / 🗑️ delete buttons.

**Resolved log**: chronological list with a ✓/✗ correctness column.

**📥 Download forecast log as CSV** for full numeric history.

### Market baseline (historical backfill)

Below the forecast log is a separate section: **📊 Market baseline**. This computes the *market's* own calibration over recently-closed markets — answering *"when Polymarket priced something at 60%, how often did it actually happen?"*. Use this:

1. **As a sanity check** — well-functioning prediction markets are usually well-calibrated. If the market Brier comes out near 0.10–0.15, that's normal.
2. **As a reference for your own performance** — once you have enough resolved forecasts, you can see whether *you* are better-calibrated than the consensus. If you're not, the rational move is to match the market price rather than bet your own number. The Tier 1 risk-adjusted Kelly already enforces this when your Brier exceeds 0.25.

**Important**: this is **the market's calibration, not yours**. The app can't backfill *your* historical forecasts because they were never recorded. Going forward you have to save forecasts as you go to build up your own calibration data.

Controls: how many closed markets to fetch (default 50), and the lead time before resolution to sample the price (default 24 hours — close enough to resolution that the market had real information, far enough that the outcome wasn't already obvious).

The fetch is slow (~30 seconds for 50 markets — one extra API call each) and cached for 6 hours. Many recently-closed markets have limited history; not every market in the list will produce a scored row.

### How to Use the Result

- **Brier below 0.10**: very sharp, well-calibrated. Trust your Kelly sizing.
- **Brier 0.10–0.20**: solid, beats most amateurs. Use ½ Kelly to be safe.
- **Brier 0.20–0.25**: barely better than random. Use ¼ Kelly or match the market.
- **Brier above 0.25**: you're losing money systematically. Stop sizing on your estimates; treat the market as authoritative until your scores improve.

A well-calibrated forecaster who only forecasts narrow, easy markets can still have a high (bad) Brier score because the markets they pick are noisy. **Sharpness** — how far your forecasts are from 50% — matters too. A consistent "50% on everything" gets Brier 0.25 trivially but provides no value.

---

## Backtest & Backtest History Pages

The Backtest page replays a strategy through recently-resolved markets and reports hit rate, ROI, Brier, and drawdown. Two evaluation modes: **single-shot** (one decision per market, hold to resolution) and **walk-forward** (multi-trade per market, mark-to-market on signal flips). The half-spread + fee knobs model real fills. The "portfolio-level bankroll constraint" checkbox shares cash across markets in time order, matching live execution semantics. **The market-baseline Brier gate** at the bottom is the explicit shippability test: a strategy whose Brier doesn't beat the "just predict latest price" baseline isn't edge — it's noise.

Every run is auto-saved as a JSON file under `data/shared/backtest_runs/`. The **Backtest History** page renders them sorted newest-first; pick one and click **📂 Load** to inspect the full result on the Backtest page without paying the API cost to rebuild the dataset.

---

## Edges & Recommendations Pages

**Edges** runs **one** Tier-4 source at a time against the active venue's markets — `crypto_spot` (lognormal model on Coinbase realized vol), `llm` (local Ollama forecast), `sports` (ESPN moneylines), `manifold` (Manifold community probability), `macro` (FRED normal-on-level). Each surfaces a model-vs-market gap with a direction and severity. Days-to-resolution filter on top defaults to ≤30 days so the long-dated "supervolcano by 2050" markets stay out of initial scans.

**Recommendations** runs **all** sources at once and stacks the results into one |gap|-ranked table. Each row carries a "Track record" badge sourced from your calibration log:

- `Brier 0.187 · n=42` — that source has logged forecasts you've resolved, with an aggregate Brier
- `untested` — no resolved forecasts under that source yet (or you've never logged any of its edges)

The badge prefix-matches calibration entries like `llm:llama3.1` against the source key `llm`, so per-model LLM tracks roll up under one badge. Lower Brier = better-calibrated. Use it to weight which sources to trust.

**Recommended pre-trade workflow:**
1. Open Recommendations with max-days ~7 for the shortest turnaround
2. Cross-reference the Track record column
3. Click into Price History / Scanner for a candidate to sanity-check liquidity + spread
4. Open Kelly Sizer, enter your probability, review the recommendation
5. Only then consider placing a real order (next section)

---

## Live Portfolio & Orders Pages (Kalshi authenticated)

These two pages appear once you've configured a Kalshi API key. They show **real account state** — settled balance, real positions, fills, settlements, resting orders — for the profile that matches `KALSHI_OWNER_PROFILE` (default `jeff`). Any other profile sees a "switch to **jeff**" prompt rather than the data.

**Live Portfolio** has four sections:
1. **Balance**: settled cash + pending payout
2. **Positions**: real Kalshi positions, signed (positive = YES long, negative = NO long)
3. **Reconciliation**: paper-vs-real per-ticker drift report with four statuses — `matched`, `paper_only`, `real_only`, `mismatch`. The status filter defaults to hiding `matched` so drift stands out.
4. **Fills + Settlements**: historical trade matches and resolved-market revenue

**Orders** shows resting (unfilled) orders + one-click cancel + today's audit-log tail. Audit log lives at `data/shared/orders.log`.

### Enabling live trading

Three flags must all be set before the **🚀 Place real order** button appears on the Kelly Sizer page:

1. `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` in `.env` (PEM file mode `600`)
2. Active profile == owner profile (default `jeff`)
3. `KALSHI_LIVE_ENABLED=true` in `.env`

Three guardrails enforced on every order:
- **Pre-flight checks** — stake cap (default $50/order), daily cap (default 10 orders), price within ±10¢ of mid (fat-finger guard), drawdown circuit-breaker, valid count + price range
- **Two-click confirmation** — an acknowledgement checkbox + a primary button; block-severity issues disable the button
- **Audit-first logging** — every attempt JSONL-logged *before* the API call so a crash mid-request still leaves a trail

The above is the **manual** path (Phase 4): the 🚀 button on Kelly Sizer, limit-only, single-order, GTC, no batches.

**Autonomous execution (Phase 5) has shipped** — a strategy daemon can place real orders via the `pm/trading/execution.py` broker seam, behind the same auth gates plus a per-profile `LIVE_TRADING_PROFILES` allow-list (so it's off by default even with the key configured). To take a strategy live at minimum size: run `python live_preflight.py --profile <profile>` (read-only — places nothing, confirms the account reads work), then `./go_live.sh --profile <profile> --service <svc> --max-stake 2 --enable` (refuses to enable unless the preflight passes). Full runbook + design: `docs/live-execution-adapter.md`. **Scaling beyond minimum size** is still gated on per-strategy evidence (Brier < market baseline, enough resolved trades) — going live at $2/order validates the *plumbing*, not the *edge*. Stop anytime with `python -m emergency_stop activate`.

---

## Alerts Page

Get notified when a market price crosses a threshold you set. Checks happen automatically on every page load that fetches market data — no background daemon, no email, just visible notifications in-app.

### Creating an alert

1. Pick a market and outcome.
2. Choose **above** or **below** as the direction.
3. Set a threshold price (e.g. `0.80`). The form pre-fills with the current market price.
4. The form shows a live preview of how far the threshold is from the current price.
5. Click **Create alert**.

### How alerts fire

- Every time you visit the Markets page (or the Alerts page itself), every active alert is checked against the latest cached prices.
- When an alert's threshold is crossed, it fires **once** and shows up in the Triggered section with the exact price and time it fired. A toast notification also pops up.
- Triggered alerts stay triggered until you click **↻ Reset** (lets it fire again on the next crossing) or **🗑️** (delete entirely). This way you won't get spammed every page render.
- The sidebar shows a `🔔 N alert(s) triggered` warning whenever there are unread triggers.

### Sections on the page

- **Scoreboard** — total / active / triggered counts.
- **Create an alert** form with a live current-vs-threshold preview.
- **🔔 Triggered** — alerts that have fired, with reset and delete buttons.
- **Watching** — active alerts with a metric column showing how far from threshold each currently is.
- **📥 Download alerts as CSV**.

### Storage

Alerts are saved in `alerts.json` in the project directory (gitignored).

---

## Where the baseball model shows up in the main app

Beyond the standalone Baseball UI (below), the baseball sidecar's predictions are wired into several places in the main Streamlit app for any Kalshi MLB market:

- **Sidebar** — a live 🟢/🟡/🔴 sidecar health badge with last-refresh hover tooltip + a link to the standalone UI.
- **🏠 Markets page** — new "model P(YES)" column populated for every MLB market row.
- **📐 Kelly Sizer** — when you pick an MLB market, the "your probability" slider auto-prefills with the model's number instead of the market midpoint. A blue note shows the model's source and the matched game so you know what you're sizing against.
- **📈 Price History** — the model's prediction history is overlaid on the price chart as a dashed diamond-marker line. Lets you see at a glance when the market diverged from the model.
- **🧠 Deep Analysis** — for MLB markets, the model's probability is included in the LLM prompt as a "domain model" anchor. The LLM is instructed to explain if its conclusion differs by more than ~10pp.
- **💼 Sandbox Portfolio / 🏦 Live Portfolio** — held MLB positions get a "model P(YES)" column showing what the model says about the side you hold *right now*. Useful as an additional signal for take-profit / cash-out decisions.

The baseball model is **unvalidated until enough paper trades resolve** (Brier vs market-baseline gate). Treat all of these as one signal, not authority.

## ⚾ Baseball Sidecar UI (standalone)

Lives outside the main Streamlit app on its own web server — binds **0.0.0.0:8770** so it's reachable from any device on the LAN (phone, tablet, other PC). Find it at `http://<this-machine's-LAN-IP>:8770` — the sidebar link in the main Streamlit app auto-detects the LAN IP for you. Multi-user safe (FastAPI handles concurrent requests properly; Streamlit doesn't), so the whole family can browse predictions simultaneously without stepping on each other.

Start it with:

```bash
scripts/baseball_web.sh                 # foreground (Ctrl-C to stop)
scripts/baseball_web.sh --background    # detach to data/shared/baseball_web.log
```

A sidebar link "**⚾ Baseball ↗**" appears in the main Streamlit app for one-click access.

### What you see

- **Today's slate**: every MLB game card with team logos (from MLB's public CDN), probable starters with their season ERA, and a green/orange win-probability bar showing the model's read at a glance. Click any card to drill in.
- **Game detail**: enlarged win-probability strip, pitcher cards, every stored prediction so you can see how the line moved as starters got announced, raw model inputs (regressed Pythagorean per team + pitcher log-odds adjustments) for auditability.
- **Standings**: full AL/NL by division with regressed-Pythagorean strength as the model's actual headline metric.
- **Top 10 teams panel** + **recent activity feed** on the side: see what the nightly cron last fetched / computed / encountered.
- **Refresh now** button (top-right, red): kicks off a fresh refresh in the background. Reload after ~30s to see updated lines.

### Stats & Insights page

The **Stats** tab (top nav) gives you a permanent home for the aggregate analysis the system can do on what it knows:

- **Top 10 + Bottom 5** by model strength rating
- **Current model edges** for today and tomorrow (cached 5min — refresh to pull fresh)
- **League leaders**: hottest offenses (R/G), stingiest pitching (RA/G), HR leaders, K-pitching leaders, most one-run finals
- **Coverage banner**: how much data is loaded

### Lead-lag analysis

The **Analysis** tab joins the two streams the system collects: MLB play-by-play (from `baseball.live`) and Kalshi order books (from `mlb_book_poller`). The promise: after every game event, did the market move? How quickly? Is the reaction window long enough to act on?

What you'll see:

- **Coverage banner** — how many plays total, how many MLB book snapshots, when book collection started. The two streams have different start dates because `mlb_book_poller` is forward-only; historical plays from the backfill don't have matching books.
- **Reaction-rate table** — for each event type (HR, K, walk, single, etc.), how often the book midpoint moved ≥2% within 10 min after the play. Shows "—" when sample size is below 5 — deliberate, so small samples can't fake a percent.
- **Event distribution** since book collection began.
- **Code snippet** for REPL-level access to the join queries.

The page is **deliberately sparse** until enough paired data accumulates — meaningful signals need ~1 week of forward collection. Until then it functions as a "what data have we accumulated?" dashboard rather than a "what should I bet?" dashboard.

### Chat with the analyst

The **Chat** tab opens a local-LLM conversation surface. Pick any model your local Ollama has installed; the system prompt comes pre-loaded with current team rankings, today/tomorrow's games + predictions, current model edges, and league-leader aggregates.

Try:
- "Why is the model down on the Mets tomorrow?"
- "Compare the Brewers and Dodgers pitching staffs"
- "Which underdog has the biggest model edge?"
- "Walk me through tomorrow's CWS @ SF prediction"

Conversation history lives in the page DOM — reload to start fresh. Multi-user safe; each tab/device gets its own conversation. The analyst is **instructed to caveat** that the model is unvalidated v0.1 — don't expect actionable advice, expect explanation of what the model believes and why.

Set `BASEBALL_CHAT_MODEL` to your preferred default (defaults to `llama3.1:latest`).

### Browsing historical games

The **Recent Games** tab (top nav) lets you jump to any date the sidecar has loaded. Default view is yesterday. Each card links to a per-game detail page with the full play-by-play timeline if plays are recorded for that game (e.g., from the historical backfill or yesterday's live capture).

To pull in more history, run:

```bash
.venv/bin/python -m baseball.backfill --days 30           # last 30 days, plays + games
.venv/bin/python -m baseball.backfill --days 7 --games-only  # games-table only (fast)
.venv/bin/python -m baseball.backfill --start 2026-04-01 --end 2026-04-30
```

Re-runs are idempotent — both the games table (upsert on game_pk) and the live_plays table (PK on game_pk + play_id) deduplicate cleanly.

### How the model works

For each game, P(home wins) is computed in **log-odds space** (because raw probabilities don't compose additively):

1. Each team's **regressed Pythagorean expectancy** (`runs² / (runs² + runs_allowed²)`, with 40 virtual games at .500 mixed in to dampen small-sample extremes — a 5-1 hot start doesn't read as .800 true talent).
2. **Home-field advantage** of ~4 percentage points (`+0.161` log-odds), the long-run MLB home-team rate.
3. **Starting-pitcher delta**: better ERA shifts the line via `0.13 × (4.30 - era)` log-odds per pitcher. A 2-run ERA gap is worth ~5 percentage points on its own.

The model is deliberately simple and transparent for v1. Once enough games resolve, the persistent `predictions` table feeds rolling Brier-score validation — that data will tell us where to iterate next.

### Schedule

Nightly cron at **04:15 UTC**, sandwiched between the bulk-deep-analysis runs. `flock` ensures it never double-runs.

### Storage

`data/shared/baseball_state.sqlite` (env override: `BASEBALL_STATE_PATH`). Override `BASEBALL_WEB_URL` if you change the port or run on another host so the sidebar link still works.

---

## Settings Page

Risk-management thresholds. All persist to `settings.json` across sessions.

| Setting | Default | What it controls |
|---|---|---|
| Bankroll | $1,000 | Denominator for position-size limits + the prefill on the Kelly Sizer |
| Max Kelly fraction | 25% | Absolute cap on any Kelly recommendation regardless of edge |
| Max position per market | 5% | Warn when a single market would exceed this fraction of bankroll |
| Max category exposure | 30% | Warn on the Portfolio page when any one category exceeds this share of open cost basis |
| Drawdown circuit breaker | 15% | When net portfolio value falls more than this from peak, surface a hard warning on Kelly and Portfolio pages |

These are *anti*-recommendations — caps and warnings, not buy signals. See `docs/recommendations-design.md` for the full roadmap of recommendation features.

---

## Agentic paper-trading profiles

The `agentic_*` profiles are auto-driven by `pm/arms/strategy_runner.py` (cron-scheduled, no user action needed). Each runs one registered strategy and records its signals as forecasts + paper buys into its own profile so calibration data accumulates honestly. Two flavors:

**Unmanaged** (hold to resolution):
- `agentic` (market_consensus baseline)
- `agentic_favorites` (favorite_grind)
- `agentic_llm` (llm_forecast)
- `agentic_baseball` (baseball model)
- `agentic_deep` (LLM deep analysis — forecast-only, no trades)

**Managed siblings** — same strategy + the full exit-management stack:
- `agentic_managed`, `agentic_favorites_managed`, `agentic_llm_managed`, `agentic_baseball_managed`, `agentic_oscillation_momentum_managed`

The exit stack on managed profiles:
- **Take-profit** at +25% net (after Kalshi fees — gross gain has to clear the fee drag)
- **Two-stage TP**: hitting TP doesn't sell; instead arms a tight 5%-off-peak trail so winners can ride further
- **Volatility-aware trailing stop**: drawdown threshold = 2σ × recent realized vol (quiet markets get tight trail; jumpy markets get room)
- **Stop-loss** at -25% net
- **Time-stop**: per strategy (1.5d baseball, 7d LLM, 14d favorites/momentum, 30d consensus)

Switch to any of these profiles via the sidebar dropdown to see their forecasts and paper positions. The Portfolio page shows mark-to-market value; closures land in `exits.jsonl` (analytics-only — not visible in the standard Portfolio view yet).

**A/B comparison**: `exits_analysis.compare_profiles("agentic_baseball_managed", "agentic_baseball")` returns a dict with totals + counterfactual P&L (what the managed profile would have made had it held to resolution instead). Useful once each side has 30+ resolved trades.

## Emergency stop (kill switch)

Single-file flag halts every new order + every new paper open system-wide:

```bash
python -m emergency_stop activate --reason "the reason text shows in the UI banner"
python -m emergency_stop status        # human-readable state
python -m emergency_stop deactivate    # resume
```

**What halts**: `kalshi_orders.place_limit_order` refuses, `strategy_runner` cycles short-circuit, every Streamlit page renders a red banner at the top. **What keeps running**: read-only data collection daemons (book_poller, mlb_book_poller, baseball.live) and `auto_resolve_runner` (records resolutions for already-settled markets, doesn't open new positions). The distinction: data ingestion and bookkeeping continue so we don't lose history; anything that takes a new action refuses.

The flag lives at `data/.emergency_stop`. Activating writes a two-line text file (ISO timestamp + reason); deactivating deletes it. Idempotent — re-activating updates the timestamp.

---

## Storage

| Path | Contents |
|---|---|
| `data/<profile>/portfolio.json` | Your transaction log (buys/sells) + value snapshots |
| `data/<profile>/forecasts.json` | Your probability forecasts |
| `data/<profile>/alerts.json` | Your price alerts |
| `data/<profile>/settings.json` | Risk-management thresholds + bankroll |
| `data/<profile>/position_meta.json` | Per-position entry_price, peak_price, opened_at, tp_armed — backs the agentic runner's exit-management logic (TP/SL/trail/two-stage). Survives runner restarts. |
| `data/<profile>/exits.jsonl` | Append-only log of every position closure (resolution + managed exits). Each line: entry_price, exit_price, peak, gross/net P&L, fees_paid, exit_reason, held_days, strategy. Used by `exits_analysis` to compute A/B comparisons between managed vs hold-to-resolution profiles. |
| `data/.active_profile` | One-line file remembering the last selected profile |
| `data/.emergency_stop` | Kill-switch flag. When present, every order placement + every new paper open across the system refuses (read-only data daemons keep running). |
| `data/shared/backtest_cache/` | Pickled `(snapshot, resolution)` datasets keyed on fetch params, so re-runs of `oscillation_backtest` with different strategy params don't re-fetch from Kalshi. |
| `data/shared/` | Cross-profile artifacts (backtest cache, baseball state, book archive, LLM cache, strategy-runner logs). |

All of `data/` is gitignored.

Both files live in the project directory and never leave your machine.

---

## Tips

- All pages share a 60-second market data cache. If prices look stale, refresh after a minute or check the "Page rendered at HH:MM:SS UTC" timestamp in the sidebar.
- If the Polymarket API is unreachable, you'll see a friendly error card instead of a stack trace. Check your internet and refresh.
- For Sports markets, try searching a team name (`Celtics`, `Yankees`) rather than the league — league names are handled by the category filter.
- The Scanner's price divergence tab is most useful for recently-resolved or near-resolution markets where quotes lag the actual outcome.
- The Calibration page becomes useful after ~30 resolved forecasts. Below that, the Brier score is noisy.
