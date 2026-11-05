# Kalshi Trader — Quickstart

> ⚠️ The analysis console below is read-only and risk-free. But this repo
> also contains a gated **real-money order path**: if you ever configure a
> Kalshi API key and enable the live gates, you are trading actual funds at
> your own risk — read the README's *Disclaimers & risk* section and
> `docs/live-execution-adapter.md` first.

> **Scope note (2026-08-17):** this covers running the Streamlit console
> locally, which is still accurate. It does **not** describe the background
> service fleet, the automated arms, or operations — that system grew up after
> this was written. For those, start at the `deploy/` directory and `docs/module-notes.md`.


## What this is

A local **decision support tool** for analyzing Kalshi and Polymarket prediction markets. You run it on your own machine; it reads the public Polymarket APIs (no auth, no trading). It does **not** place orders, and it does **not** tell you what to buy.

What it does help you do:

- **See** every active market with searchable, categorized lists, live mid-prices, order book depth, and historical price charts.
- **Find** markets worth investigating — narrow spreads, stale quotes, multi-outcome arbitrage gaps.
- **Size** a bet, given your own probability estimate — via the Kelly Criterion.
- **Track** your positions, transactions, realized + unrealized P&L, sector exposure, and portfolio value over time.
- **Measure** whether your past probability estimates were accurate (calibration / Brier score). This is the closed-loop feedback that prevents Kelly sizing from amplifying wishful thinking.
- **Alert** when a market crosses a price threshold you set.

## Does it give buy recommendations?

**No.** This is the most important thing to understand. The app gives you tools to make better decisions; it does not make them for you.

The closest thing to a "recommendation" is the **Kelly Sizer**, and even that is conditional on a number you provide:

1. You decide a market's true probability (e.g. "I think this is 60% likely")
2. The app reads the market's current price (the market's consensus probability, e.g. 50%)
3. The difference is your **edge** — if you're right, the market is mispriced
4. The Kelly formula converts that edge into "bet this fraction of your bankroll"

So the app will say *"if you really believe this is 60% likely, bet 20% of bankroll on YES."* It will not say *"this is 60% likely."* That probability is your judgment, your research, your read of the world.

If you don't have a probability estimate, **the app has nothing to say about whether to bet**.

## What about the Market Scanner — isn't that a recommendation?

Soft signals, not recommendations. Each tab flags markets worth a closer look:

- **Narrow Spreads** — markets that are cheap to enter and exit. Useful for active trading, but says nothing about which side to take.
- **Price Divergence** — markets where the last trade differs notably from the current mid-quote. Could mean a stale quote, thin book, or a market that just moved on news. Worth investigating; not a buy signal.
- **Multi-Outcome Gaps** — multi-outcome markets where prices don't sum to $1. This is the only output close to a real "trade signal" — if the sum is below $1 with enough liquidity on each side, there's a genuine arbitrage. Still requires execution discipline (transaction costs, available size) to act on.

## The intended workflow

```
   1. Browse / Scan ──→ 2. Form a view ──→ 3. Size ──→ 4. Track ──→ 5. Measure
      (Markets,            (your own              (Kelly      (Portfolio)    (Calibration)
       Scanner)             research and          Sizer +
                            judgment)             Save fcst)
```

Steps 2 and 3 are loosely coupled by design: forming a view is research the app can't do for you; the Kelly Sizer accepts whatever number you produce.

Step 5 closes the loop. If you save forecasts each time you size a bet, and you mark them resolved when markets close, the Calibration page tells you whether your probability estimates are systematically too confident, too cautious, or roughly right. This is the feedback that makes Kelly sizing useful instead of dangerous. A bad forecaster who uses Kelly faithfully will go broke faster than a bad forecaster who flat-bets.

## First 10 minutes

```bash
./run.sh
```

This sets up `.venv`, installs dependencies, and launches the app at `http://localhost:8501`.

Then in the browser:

1. **🏠 Markets** — scroll through what's actively trading. Try the search and category filter. Note that everything you'll do later starts with picking one market from this list.
2. **📈 Price History** — pick a market you have an intuition about. Watch the live ticker tick. Open the **📚 Order Book Depth** expander to see how much can actually be traded at quoted prices.
3. **⚡ Market Scanner** — look at the three tabs. The Narrow Spreads tab in particular gives you a feel for which markets are deeply liquid.
4. **📐 Kelly Sizer** — pick the same market. Look at where the market price is. Drag the slider to your own probability estimate. Note the edge, the Kelly fraction, the dollar amount. Click **💾 Save forecast** so the system remembers your estimate.
5. **💼 Portfolio** — if you actually buy on Polymarket itself, come back here and record the buy. The page derives positions, realized + unrealized P&L, sector exposure, and a value-over-time chart.
6. **📊 Calibration** — empty for now. Once you've saved a handful of forecasts and the markets resolve in real life, come back and mark them ✓ Yes / ✗ No. After ~30 resolved forecasts the Brier score becomes a meaningful read on your accuracy.
7. **🔔 Alerts** — set a threshold ("YES on market X goes above $0.80"). Browse to any other page. When it crosses, you get a toast notification and a sidebar warning band.

## What's stored locally

Everything stays on your machine:

- `portfolio.json` — your transaction log (buys, sells) + hourly value snapshots
- `forecasts.json` — your saved probability estimates and their resolutions
- `alerts.json` — your active and triggered price alerts

All three are gitignored. None of this leaves your machine. There's no account, no sync, no cloud.

## Where to go next

- [User Guide](docs/user-guide.md) — every page, every control, in detail
- [Glossary](docs/glossary.md) — Kelly, Brier, CLOB, spread, calibration, and other terms explained in plain English
- [Developer Guide](docs/developer-guide.md) — for if you want to modify or extend
- [Roadmap](TODO.md) — what's done, what's pending

## Honest caveats

- **Polymarket prices ≠ probabilities of truth.** They're probabilities the *market* assigns. Markets can be wrong. Wisdom-of-crowds works in some categories better than others.
- **Edges are usually illusions.** Most of the time when you think the market is wrong, the market is right and you're missing something. The Calibration page exists precisely to make this honest.
- **The Kelly fraction is the maximum sensible bet, not the recommended one.** Most practitioners use half-Kelly or quarter-Kelly. Full Kelly is mathematically optimal only if your probability estimate is exactly right, which it never is.
- **Transaction costs aren't modeled.** Bid-ask spreads, gas fees on Polygon, and slippage from thin order books all eat into theoretical edges. The Order Book Depth view on the Price History page is your tool for sanity-checking expected slippage.
