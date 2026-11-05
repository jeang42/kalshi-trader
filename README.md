# Kalshi Trader

by [Jeff Angelcyk](https://nexurite.ai)

A research harness and Streamlit console for [Kalshi](https://kalshi.com) and
[Polymarket](https://polymarket.com) prediction markets. Browse markets, chart
price history, scan for structural opportunities, size positions with the Kelly
Criterion, run honest backtests (fees, half-spread, out-of-sample ledger),
paper-trade strategies on a cron fleet, and — optionally, behind three
independent gates — place real Kalshi orders.

**What makes this repo worth reading is not a trading edge. It is the absence
of one, measured carefully.** Over six months this system tested strategy
families spanning longshot fades, favorite backing, in-game sports reaction,
mention/broadcast sniping, settlement-source lag, listing-time pricing,
liquidity-incentive making, and open-interest confirmation. Every one died at a
preregistered kill rule, documented in place. The repeatable finding, measured
independently five ways: **the venue's spread is an accurate price for its own
inefficiency** — every bias found was the same size as the cost of correcting
it. The durable asset is the evaluation harness that reached that conclusion
cheaply and honestly: an out-of-sample ledger that resets when parameters
change, fee-aware net accounting, provenance stamping on every result, circuit
breakers, triple-gated live execution, and an emergency stop.

Read the story in [docs/edge-research-log.md](docs/edge-research-log.md)
(thesis → mechanism → evidence → verdict, written before any code),
[docs/failed-experiments.md](docs/failed-experiments.md) (post-mortems) and
[docs/strategy-scoreboard.md](docs/strategy-scoreboard.md) (final verdicts).

> **A note on redaction:** exact dollar amounts (balances and P&L) are redacted
> as `$—` throughout the research logs. Signs, verdicts, win rates, sample
> sizes, and per-contract economics are preserved — the methodology and the
> conclusions are the point, not the account statement.

## Features

| Page | Description |
|---|---|
| 🏠 **Markets** | Browse active markets with text search, category filter, days-to-resolution Min/Max filters, CSV export |
| 📈 **Price History** | Interactive price charts for any market (up to 90 days) + order book depth viewer showing bid/ask staircase per outcome |
| ⚡ **Market Scanner** | Four scanners: narrow spreads, price divergence, multi-outcome gaps, cross-market arbitrage. CSV export per tab. |
| 📐 **Kelly Sizer** | Optimal bet sizing from your probability estimates vs. market prices. Save forecasts for later scoring. With `KALSHI_LIVE_ENABLED=true`, gains a "🚀 Place real order" expander for live trading. |
| 💼 **Sandbox Portfolio** | Paper-trade log (NOT your real Kalshi state — see Live Portfolio for that). Transaction log, average-cost positions, realized + unrealized P&L, sector exposure, value-over-time chart with drawdown, CSV export. |
| 🏦 **Live Portfolio** | (Kalshi-authed) Real balance, positions with estimated cash-out values, fills, settlements + paper-vs-real reconciliation. Locked to the owner profile. |
| 📑 **Orders** | (Kalshi-authed) Resting orders + cancel buttons + today's order audit-log tail. |
| 📊 **Calibration** | Brier score + per-source breakdown + calibration plot over your saved forecasts. Auto-resolution from public closed markets + your Kalshi settlements when authed. |
| 🧪 **Backtest** | Replay strategies through historical resolved markets. Single-shot or walk-forward eval, portfolio-level bankroll constraint, half-spread + fees, market-baseline Brier gate. |
| 📜 **Backtest History** | Every backtest run auto-persisted as JSON; reload + inspect past results without rerunning the API calls. |
| 🔭 **Edges** | One model source at a time (crypto_spot, llm, sports, manifold, macro). Model-vs-market gap flagging. |
| 🎯 **Recommendations** | All model sources run at once, results stacked into one \|gap\|-ranked table with per-source calibration Brier badges. |
| 🧠 **Deep Analysis** | Slow per-market structured-reasoning LLM pipeline: price history + metadata + resolution criteria + order book + siblings packed into a long prompt for a local model, with optional web context for grounding. |
| 🔔 **Alerts** | Threshold-crossing notifications. "Tell me when YES on market X crosses $0.80." Checked on every page render. |
| ⚙️ **Settings** | Risk thresholds + profile management + storage panels (LLM cache, book archive, backtest history). |
| ⚾ **Baseball UI** (standalone) | FastAPI+Jinja2+HTMX web UI on port 8770 served by the baseball sidecar. Today's slate · Recent Games · Stats · Analysis (lead-lag join of plays × Kalshi books) · Chat (Ollama-backed). |
| 🏀 **WNBA UI** (standalone) | Same design system on port 8771: slate with model win-prob bars, game detail, model-vs-result history, standings, chat. |
| 🎾 **Tennis UI** (standalone) | Elo-based ATP+WTA sidecar UI on port 8772: date-navigable results/forecasts, player detail, surface leaders, live model-vs-market edges. |

**Multi-venue**: Kalshi is the default provider (US-legal). Polymarket is
supported as a read-only fallback. Switch via the sidebar.

**Multi-user profiles** isolate per-user state; several `agentic_*` automated
paper-trading profiles ship by default. The Kalshi authenticated API is locked
to a single owner profile so paper experiments can't touch the real account.

**Agentic paper-trading**: `pm/arms/strategy_runner.py` (cron-driven) records a
chosen strategy's signals as forecasts + paper buys into a designated profile,
with a full exit-management stack (take-profit, stop-loss, two-stage TP,
volatility-aware trailing stop, time-stop). Decisions gate on **net P&L after
Kalshi fees**. See the [Strategies doc](docs/strategies.md) for the catalog,
including the ones marked ❌ KNOWN-BAD — kept because the negative result is
the documentation.

**Autonomous live execution** (`pm/trading/execution.py`): a strategy daemon
can place real Kalshi orders instead of paper writes, behind a `Broker` seam —
`PaperBroker` (default) vs `LiveBroker` (real orders + pre-flight + fill
reconciliation). **Triple-gated and dormant by default**: live only when the
profile is in `LIVE_TRADING_PROFILES` *and* `KALSHI_LIVE_ENABLED=true` *and*
not emergency-stopped. `python -m live_gates` is the authoritative answer to
*what can place real orders right now*. Full runbook in
[docs/live-execution-adapter.md](docs/live-execution-adapter.md).

**Operations**: `pm/core/notify.py` + `pm/ops/health_digest.py` push service
failures and stale capture streams to your phone
([docs/push-notifications.md](docs/push-notifications.md)).
`pm/ledger/oos_ledger.py` separates results that genuinely tested a strategy
from results it was tuned on, and resets an arm's evidence clock whenever its
selection parameters change. `deploy/` holds the systemd units and crontab
under version control, with an installer that dry-runs by default.
`pm/ops/backup_sqlite.py` takes consistent snapshots and verifies they restore.

**Durability**: every store is schema-versioned, every daemon stops gracefully
on SIGTERM with the place-then-record window protected, every result row is
stamped with the commit that produced it, and every outbound connector sits
behind a circuit breaker. Retired code lives in `attic/` (index:
[attic/README.md](attic/README.md)) — visible, not deleted, because the
falsified experiments are part of the record.

**Emergency stop**: `python -m emergency_stop activate --reason "..."` halts
every new order and every new paper open system-wide via a single flag file.
Data-collection daemons keep running. `deactivate` resumes.

## Quick Start

```bash
./run.sh                       # main Streamlit app on http://localhost:8501
```

Creates `.venv/` and installs dependencies on first run. No API key required —
all analysis pages work read-only against public APIs.

### Manual setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

### Optional: sidecars and collectors

```bash
scripts/baseball_web.sh --background       # multi-user baseball UI on :8770
scripts/wnba_web.sh                        # WNBA UI on :8771
scripts/tennis_web.sh                      # tennis UI on :8772
python trade_poller.py --loop --interval 30   # Kalshi executed-trade tape → archive
```

In long-running use these are **systemd user services** — the unit files and
crontab live under `deploy/` with an installer that dry-runs by default. See
[QUICKSTART.md](QUICKSTART.md) for the intended workflow and
[docs/user-guide.md](docs/user-guide.md) for every page and control.

### Kalshi API (optional, for live account pages)

Copy `.env.example` to `.env` and set the `KALSHI_*` variables. Reading your
account (balance, positions, fills) needs only the key; placing orders
additionally requires the live-trading gates described above. Everything else
works without any of it.

## Project Layout

```
kalshi-trader/
├── app.py                  # Streamlit main script + router (st.navigation)
├── run.sh                  # Setup + launch (venv, deps, streamlit)
├── conftest.py             # Project-wide test isolation + live-data tripwire
├── live_gates.py, oos_ledger.py, health_digest.py,
├── notify.py, emergency_stop.py
│                           # Deliberate shims: documented operator commands
│                           #   (kill switch, alert hook) forward to pm/
├── edges/                  # Edge framework (public API `import edges`);
│                           #   one module per model source
├── user_strategies/        # Drop-in backtest strategies
├── pm/                     # The layered packages. Strict layering,
│   │                       #   enforced by tests/test_pm_layout.py
│   ├── core/               #   profiles, sqlite_schema, provenance,
│   │                       #     circuit_breaker, shutdown, notify, web_auth
│   ├── venue/              #   kalshi_client/auth/fees/orders,
│   │                       #     polymarket_client, provider, slippage
│   ├── ledger/             #   portfolio, calibration, alerts, risk,
│   │                       #     exits_analysis, oos_ledger
│   ├── feeds/              #   crypto/sports/macro/manifold data,
│   │                       #     llm_* stack, web_context
│   ├── archive/            #   book_archive, trade_archive
│   ├── live/               #   tennis_live, wnba_live, event_followup
│   ├── research/           #   backtest (+ registry), backtest_history,
│   │                       #     candle_panel, scanners
│   ├── trading/            #   execution (Broker seam), live_gates,
│   │                       #     live_preflight, live_status
│   ├── arms/               #   paper/live strategy arms + strategy_runner
│   ├── capture/            #   book/trade pollers, kalshi_ws, captures
│   ├── mention/            #   broadcast mention detection stack
│   ├── ops/                #   health_digest, auto_resolve, backup, snapshots
│   └── ui/                 #   shared loaders + pages/ (one file per route)
├── baseball/               # ⚾ MLB sidecar: model, SQLite state, UI :8770
├── wnba/                   # 🏀 WNBA sidecar: model + UI :8771
├── tennis/                 # 🎾 Tennis sidecar: Elo + UI :8772
├── control_plane/          # Read-only ops dashboard (FastAPI :8773)
├── deploy/                 # Fleet topology: systemd units + crontab + installer
├── scripts/                # backup.sh, logrotate, launchers
├── attic/                  # Retired code — not imported, linted or tested
├── tests/                  # Test suite (isolation fixtures in ../conftest.py)
├── docs/                   # file map, module notes, guides, research logs
└── data/                   # Gitignored: per-profile ledgers + shared stores
```

## Documentation

- [**Quickstart**](QUICKSTART.md) — what this is, what it isn't, the intended workflow
- [Edge Research Log](docs/edge-research-log.md) — the canonical research record: every thesis, its mechanism, the measurement, the verdict
- [Failed Experiments](docs/failed-experiments.md) — post-mortems, kept deliberately
- [Strategy Scoreboard](docs/strategy-scoreboard.md) — final verdicts on completed attempts
- [Strategies](docs/strategies.md) — running catalog of trading approaches, with honest reads on each
- [User Guide](docs/user-guide.md) — walkthrough of every page, every control
- [Developer Guide](docs/developer-guide.md) — module-by-module architecture, testing, extension patterns
- [File Map](docs/file-map.md) — the index: one line per file, API gotchas, schemas
- [Module Notes](docs/module-notes.md) — the long form behind each file-map row: why it was built, what it measured, what falsified it
- [API Surface](docs/api-surface.md) — key function signatures for every public entry point
- [API Reference](docs/api-reference.md) — venue endpoints, schemas, known quirks
- [Live-execution adapter](docs/live-execution-adapter.md) — the Broker seam + go-live runbook
- [Sports Model Playbook](docs/sports-model-playbook.md) — repeatable recipe for standing up a new sport sidecar
- [Glossary](docs/glossary.md) — plain-English definitions: spread, Kelly, Brier, CLOB, and more

## Testing

```bash
make check         # everything CI runs: ruff + mypy + pytest + pip-audit
make test          # pytest + doctests + coverage report
make test-fast     # no coverage, faster feedback loop
```

CI runs on every push (`.github/workflows/ci.yml`) with four jobs: lint,
typecheck, test, audit. Coverage floor: 80% — the gate covers the modules that
transact, not just the analytical ones. Tests cannot reach the network
(`socket.connect` is blocked for every test) and cannot write into `data/`
(a session tripwire fails the run if they do).

## Requirements

- Python 3.11+
- Core dependencies: `streamlit`, `pandas`, `numpy`, `plotly`, `requests`

## Disclaimers

- This project is **not affiliated with, endorsed by, or connected to Kalshi
  or Polymarket** in any way. "Kalshi" and "Polymarket" are trademarks of
  their respective owners, used here only to identify the public APIs the
  software talks to.
- **Nothing in this repository is financial advice.** The research record
  exists precisely because the measured answer to "is there an edge here?"
  was no. Prediction-market trading can lose money; the live-order path is
  disabled by default and gated three independent ways for a reason.
- Use at your own risk, under the terms of the license below.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
Copyright 2026 [Jeff Angelcyk](https://nexurite.ai).
