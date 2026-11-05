# Kalshi Trader — File Map & API Gotchas

This file is the authoritative quick-reference for anyone working in this
repo — an **index**, kept short enough to read end to end. Two companions carry
the bulk: [`docs/module-notes.md`](docs/module-notes.md) (why each module
exists, what it measured, what falsified it) and
[`docs/api-surface.md`](docs/api-surface.md) (key function signatures).
**Keep all three in sync** when you add or change a public API.

## What this project is

A Streamlit investment analysis tool for Polymarket prediction markets. No authentication. All data is read-only from public APIs except `portfolio.json` and `forecasts.json` (local file writes).

## File map

One line per file: what it is, and whether you need it. **The rationale — why a
thing was built, what it measured, what falsified it, and the traps inside it —
is in [`docs/module-notes.md`](docs/module-notes.md)**, linked as ↳ per row.
Signatures are in [`docs/api-surface.md`](docs/api-surface.md). Keep all three
in sync when you change a public API.

### Layout — `pm/` packages vs the root

The root namespace was split into layered packages (P3/R-24). **Phases A, B
and C all landed 2026-08-24**: 79 modules now live under `pm/`, strictly
layered — a package may only import from layers below it, and
`tests/test_pm_layout.py` enforces this (0 upward imports) plus the
path-anchoring rules in gotcha 1b.

| Package | Contains | Notes |
|---|---|---|
| `pm/core/` | profiles, sqlite_schema, provenance, circuit_breaker, atomic_io, shutdown, emergency_stop, notify, web_auth, sidecars | bottom layer |
| `pm/venue/` | Kalshi + Polymarket clients, `provider`, fees, orders, auth, slippage | |
| `pm/ledger/` | portfolio, calibration, alerts, risk, position_meta, exits_analysis, oos_ledger | |
| `pm/feeds/` | crypto / sports / macro / manifold / underlying data, the `llm_*` stack, web_context | |
| `pm/archive/` | book_archive, trade_archive | |
| `pm/live/` | tennis_live, wnba_live, event_followup | |
| `pm/research/` | backtest, backtest_history, oscillation_*, pre_event_scanner, inefficiency_sweep, candle_panel | `user_strategies/` plugins now import `from pm.research import backtest` |
| `pm/trading/` | execution, live_gates, live_preflight, live_status | |
| `pm/arms/` | strategy_runner, tennis_runner, wnba_runner (cron); reactive_scalper, pre_position, arb_scan (⏸). The sqlite fade arms are all retired → `attic/` (near-close pair 2026-08-24 after the candle_panel retrospective) | cron runs `-m pm.arms.<x>` |
| `pm/capture/` | book/trade/mlb_book pollers, kalshi_ws, near_close/mention/news/tennis/wnba captures | systemd runs `-m pm.capture.<x>` |
| `pm/mention/` | mention_listen/rehearsal/cascade_backfill, monologue_recorder, audio_transcribe, hdhomerun, feed_latency | |
| `pm/ops/` | health_digest, snapshot_runner, auto_resolve_runner, backup_sqlite, bulk_deep_analysis | timers/cron run `-m pm.ops.<x>` |
| `pm/ui/` | shared.py + pages/ (the 15 Streamlit routes) | `app.py` at the root is the main script |

**Still at the root**: `app.py` (deliberately — see its file-map row: the
main script's directory is the process's `sys.path` anchor), the `edges/`
package (public API kept stable where it is), `user_strategies/` (drop-in
dir), `conftest.py`, and five deliberate shims. All systemd units and crontab entries now invoke
`-m pm.<layer>.<module>`; `deploy/` is the source of truth as before.

**Five root shims are deliberate**, not leftovers: `live_gates.py`,
`oos_ledger.py`, `notify.py`, `emergency_stop.py`, `health_digest.py` forward
to their `pm/` homes so the documented operator commands — including all of
RESUME §1's muscle-memory resume checks and the emergency kill switch — keep
working unchanged. The fleet no longer needs them (units run the `pm/` paths);
they serve the human at the keyboard.

### App shell

| File | Responsibility |
|---|---|
| `app.py` | Streamlit router — `st.navigation` dispatches to `pm/ui/pages/*.py`, no page logic. **Stays at the repo root on purpose**: the main script's directory is what Streamlit puts on `sys.path` for the whole process, which is what makes `import pm...` work in every page. |
| `pm/ui/shared.py` | Cached `load_markets`/`load_events`, alert hook, UI constants. Imported by every page as `from pm.ui.shared import …`. |
| `pm/ui/pages/*.py` | One file per route: markets, price_history, scanner, kelly, portfolio, live_portfolio, orders, calibration, backtest, backtest_history, edges, recommendations, deep_analysis, alerts, settings |
| `pm/venue/provider.py` | MarketProvider Protocol + registry. Pages call `get_provider()` for any network-touching op — lets Polymarket / Kalshi / Polymarket-US swap without page changes. |
| `pm/core/profiles.py` | Multi-user profile system (local, file-based, no auth). Each profile owns a data directory. |
| `run.sh` | Setup + launch (creates `.venv`, installs deps, runs streamlit) |

### Venue clients

| File | Responsibility |
|---|---|
| `pm/venue/polymarket_client.py` | Polymarket HTTP client + pure helpers (scanners, Kelly, `parse_order_book`). Wrapped by PolymarketProvider. |
| `pm/venue/kalshi_client.py` | Kalshi HTTP client + KalshiProvider (auto-registers on import). Public-read endpoints + authed `get_balance` / `get_authed_positions` / `get_fills` / `get_settlements`. |
| `pm/venue/kalshi_auth.py` | Authenticated Kalshi layer — RSA-PSS-SHA256 signs `{ts}{METHOD}{path}`. `is_configured()` gates on env + PEM **and** the active profile being authorized. ↳ [notes](docs/module-notes.md#pmvenuekalshi_authpy) |
| `pm/venue/kalshi_orders.py` | Order placement: `place_limit_order` / `cancel_order` / `get_resting_orders` + `pre_flight_check` caps. Gated by `KALSHI_LIVE_ENABLED` **and** `kalshi_auth.is_configured()`. Every attempt audit-logged to `data/shared/audit/orders.log` (in `audit/` to stay out of logrotate's reach). ↳ [notes](docs/module-notes.md#pmvenuekalshi_orderspy) |
| `pm/venue/kalshi_fees.py` | Kalshi fee formula `ceil(0.07 × P × (1-P) × C × 100)/100` (0.035 sports). Used by exit logic and backtest `fee_fn`. ↳ [notes](docs/module-notes.md#pmvenuekalshi_feespy) |
| `pm/capture/kalshi_ws.py` | WebSocket streamer — `orderbook_delta` → `book_archive`, `trade` → `trade_archive`, flushed every 15s. ⏸ service stopped. ↳ [notes](docs/module-notes.md#pmcapturekalshi_wspy) |

### Live execution + its guardrails

| File | Responsibility |
|---|---|
| `pm/trading/execution.py` | **The seam between strategy decisions and order fills.** `Broker` Protocol + `PaperBroker` (default) + `LiveBroker`. Triple-gated to live; account-level drawdown breaker (fails CLOSED); position-scoping so arms share one account; settlement reconciler; `_place` is a `shutdown.deferred` critical section. ↳ [notes](docs/module-notes.md#pmtradingexecutionpy), `docs/live-execution-adapter.md` |
| `pm/trading/live_gates.py` | **"What is armed right now" — the authoritative answer.** Resolves the gate chain per-profile the way a daemon does. `python -m live_gates [--json]`. Never trust an ad-hoc shell check. ↳ [notes](docs/module-notes.md#pmtradinglive_gatespy) |
| `pm/trading/live_preflight.py` | Read-only authed-API preflight — step 0 of the go-live runbook, places nothing. ↳ [notes](docs/module-notes.md#pmtradinglive_preflightpy) |
| `go_live.sh` | Guarded go-live enabler. Dry-runs by default; `--enable` writes the live env vars only if preflight passes. ↳ [notes](docs/module-notes.md#go_livesh) |
| `pm/trading/live_status.py` | Read-only live account readout (cash, open real positions, settlements). Scopes to tickers we placed. ↳ [notes](docs/module-notes.md#pmtradinglive_statuspy) |

### Operational infrastructure

| File | Responsibility |
|---|---|
| `pm/core/atomic_io.py` | Crash-safe + concurrency-safe JSON persistence for the per-profile ledgers. **`mutate(path, default)` is the API mutation sites must use** — it holds an advisory `flock` across the whole read-modify-write, which is what closes the lost-update race. ↳ [notes](docs/module-notes.md#pmcoreatomic_iopy) |
| `pm/core/sqlite_schema.py` | Schema versioning for the arm/capture/archive stores. Declare `SCHEMA`, call `ensure_schema` in `_con()`, add to `tests/test_store_schemas.py`. Fails closed on an unknown-future version. ↳ [notes](docs/module-notes.md#pmcoresqlite_schemapy) |
| `pm/core/shutdown.py` | Cooperative shutdown for the daemons. `install()` in `main()`; `requested()`/`sleep()` in every loop; `deferred(label)` masks SIGTERM across a place-then-record window. ↳ [notes](docs/module-notes.md#pmcoreshutdownpy) |
| `pm/core/logctx.py` | Structured logging + correlation IDs (R-23). `bind()`/`scope()` put `[arm=… cycle=… order=…]` on every log line in a block; `setup()` replaces per-entry-point `basicConfig`; `orders.log` audit rows embed the bound ctx. `POLYMARKET_LOG_FORMAT=json` → JSONL. ↳ [notes](docs/module-notes.md#pmcorelogctxpy) |
| `pm/core/provenance.py` | Which commit produced this result. `stamp()` / `stamp_line()` / `label()` — embedded in position rows, order audit lines, exits, backtest runs, reports. ↳ [notes](docs/module-notes.md#pmcoreprovenancepy) |
| `pm/core/circuit_breaker.py` | One `Breaker` per outbound dependency, wrapping that connector's single HTTP entry point. `CircuitOpenError` subclasses `RequestException` on purpose. Never catch it to retry. ↳ [notes](docs/module-notes.md#pmcorecircuit_breakerpy) |
| `pm/core/notify.py` | Operator alerting — health-log JSONL + desktop + ntfy phone push. Every channel optional; **nothing can raise**. `python -m notify --test`. ↳ [notes](docs/module-notes.md#pmcorenotifypy) |
| `pm/ops/health_digest.py` | Daily fleet digest — catches "nothing crashed but an arm stopped working". Pushes only when something needs attention. ↳ [notes](docs/module-notes.md#pmopshealth_digestpy) |
| `pm/core/web_auth.py` | Shared-secret gate for the four LAN-facing UIs. A shared secret, **not** authentication. No-op unless `WEB_UI_TOKEN` is set. ↳ [notes](docs/module-notes.md#pmcoreweb_authpy) |
| `pm/ops/settlement_check.py` | Did the live orders we placed actually settle, and does the venue agree? Read-only; scopes to tickers from `orders.log` so a shared account's manual trades are never reported. `--notify` pushes to phone. ↳ [notes](docs/module-notes.md#pmopssettlement_checkpy) |
| `pm/ops/backup_sqlite.py` | Consistent SQLite snapshots for the nightly backup (online backup API → cold staging → `integrity_check` → rsync). ↳ [notes](docs/module-notes.md#pmopsbackup_sqlitepy) |
| `pm/ledger/oos_ledger.py` | Out-of-sample bookkeeping — fingerprints each arm's SELECTION parameters and splits P&L at the boundary, so a parameter edit resets the evidence clock. `python -m oos_ledger --status`. ↳ [notes](docs/module-notes.md#pmledgeroos_ledgerpy) |
| `deploy/` | Fleet topology in version control — 26 systemd units (22 services + 4 timers) + crontab. `install.sh` dry-runs by default. Daemons stagger themselves at boot. **Deliberately never enables or starts anything.** ↳ [notes](docs/module-notes.md#deploy) |
| `control_plane/` | Read-only ops dashboard over the whole fleet (FastAPI :8773). `collect.snapshot()` degrades gracefully per source; `/` + `/health.json`. ↳ [notes](docs/module-notes.md#control_plane-collectpy-webpy-templates-static-configsystemdusercontrol-planeservice) |

### Per-profile ledgers + data files

| File | Responsibility |
|---|---|
| `pm/ledger/portfolio.py` | Transaction log + derived positions / realized P&L / value snapshots + `reconcile_positions` |
| `pm/ledger/calibration.py` | Forecast log + Brier score / calibration bins |
| `pm/ledger/alerts.py` | Price alert log + threshold checking |
| `pm/ledger/risk.py` | Tier 1 risk management (calibration-adjusted Kelly, drawdown, exposure caps). See `docs/recommendations-design.md`. |
| `pm/ledger/position_meta.py` | Per-position lifecycle metadata (entry_price, opened_at, running peak) at `data/<profile>/position_meta.json`. Backs the TP/SL/trail logic. Env `POLYMARKET_POSITION_META_FILE`. |
| `pm/ledger/exits_analysis.py` | Reader + analytics for `data/<profile>/exits.jsonl` — every closure, resolution and managed. Counterfactual P&L = what holding to resolution would have paid. ↳ [notes](docs/module-notes.md#pmledgerexits_analysispy) |
| `data/<profile>/{portfolio,forecasts,alerts,settings}.json` | Gitignored per-profile ledgers. `portfolio.json` v1→v2 auto-migrated. |
| `data/.active_profile` | One-line file naming the currently active profile |
| `data/shared/` | Cross-profile artifacts (stores, logs, reports) |

### Backtest + strategy framework

| File | Responsibility |
|---|---|
| `pm/research/backtest.py` | Tier 3b framework — MarketSnapshot/Resolution/Signal/BacktestResult, lookahead-safe `build_dataset`, `run_backtest`, strategy registry, `market_baseline_brier` gate. |
| `pm/research/backtest_history.py` | Persistent backtest-run history, one JSON per run in `data/shared/backtest_runs/`. Env `BACKTEST_HISTORY_DIR`. |
| `pm/arms/strategy_runner.py` | Agentic runner — records a registered strategy's signals as forecasts + paper buys into a profile. Auto-closes on resolution and on TP/SL/trail/time stops. ↳ [notes](docs/module-notes.md#pmarmsstrategy_runnerpy) |
| `user_strategies/` | Drop-in `@backtest.register_strategy` functions, discovered at app startup. In-process, no sandbox. |
| `pm/research/pre_event_scanner.py` | Pre-event lottery scanner (sub-$X markets with a catalyst in <Y days) + `pre_event_lottery` strategy, UNTESTED. ↳ [notes](docs/module-notes.md#pmresearchpre_event_scannerpy) |
| `pm/research/oscillation_scanner.py` | Oscillation ranking + two strategies: `oscillation_revert` **⚠️ KNOWN BAD**, `oscillation_momentum` 🧪 paper. ↳ [notes](docs/module-notes.md#pmresearchoscillation_scannerpy) |
| `pm/research/oscillation_backtest.py` | Retrospective evaluation that falsified oscillation_revert. ↳ [notes](docs/module-notes.md#pmresearchoscillation_backtestpy) |
| `pm/venue/slippage.py` | Pure order-book slippage helpers — `walk_book_for_stake`, `effective_edge`. ↳ [notes](docs/module-notes.md#pmvenueslippagepy) |

### External-data sources (Tier 4)

| File | Responsibility |
|---|---|
| `edges/` | Tier 4 framework — `__init__` holds the `Edge` registry + `find_all_edges`; `_base.py` the shared primitives; one module per source (`crypto`, `llm`, `sports`, `baseball`, `wnba`, `tennis`, `macro`, `manifold`). Split from a 2,175-line `edges.py` (R-24) with the public API unchanged — `import edges` and every `edges.<name>` still resolve. **Patch internals at their real home** (`edges.baseball._x`), not `edges._x`. ↳ [notes](docs/module-notes.md#edges) |
| `pm/feeds/crypto_data.py` | Coinbase spot + OHLC, realized vol, lognormal terminal + one-touch barrier models. US-accessible (Binance geo-blocked). |
| `pm/feeds/underlying_data.py` | Commodity/index/FX/yield quotes via Yahoo v8 (UNOFFICIAL — handled defensively). **Staleness policy lives at the CALLER.** ↳ [notes](docs/module-notes.md#pmfeedsunderlying_datapy) |
| `pm/feeds/sports_data.py` | ESPN public site API — MLB/NBA/NFL/NHL/WNBA scoreboards + consensus moneyline, de-vigged. |
| `pm/feeds/manifold_data.py` | Manifold free public API — NL search + community probabilities. Matching is direction-blind; eyeball every flag. |
| `pm/feeds/nhc_data.py` | NHC active-storm feed (`CurrentStorms.json`, `nhc` breaker) — the primary-source half of the storm trigger; seed for the eventual advisory instrument. |
| `pm/feeds/macro_data.py` | FRED unauthenticated CSV — UNRATE, DGS2, DGS10, FEDFUNDS + normal-on-level model. |
| `pm/feeds/llm_data.py` | Ollama client at `OLLAMA_API_BASE`. Model discovery + JSON-constrained forecast prompting. No paid API. |
| `pm/feeds/llm_deep_analysis.py` | Slow per-market structured-reasoning pipeline → JSON `DeepAnalysis`. Tagged `llm_deep:<model>` so it tracks separately from fast `llm:<model>`. ↳ [notes](docs/module-notes.md#pmfeedsllm_deep_analysispy) |
| `pm/feeds/llm_matcher.py` | LLM semantic question matcher — fixes Jaccard direction-blindness. ↳ [notes](docs/module-notes.md#pmfeedsllm_matcherpy) |
| `pm/feeds/llm_cache.py` | Persistent SQLite K/V cache for LLM forecasts + match verdicts. Schema-version prefix in keys. ↳ [notes](docs/module-notes.md#pmfeedsllm_cachepy) |
| `pm/feeds/web_context.py` | Google News RSS + Wikipedia REST for Deep Analysis, cached in `llm_cache`. ↳ [notes](docs/module-notes.md#pmfeedsweb_contextpy) |
| `pm/ops/bulk_deep_analysis.py` | Cron bulk runner — deep analysis over closing-soon markets, one model per invocation. Forecast-only. **Never covers MLB.** ↳ [notes](docs/module-notes.md#pmopsbulk_deep_analysispy) |

### Capture + archive daemons

| File | Responsibility |
|---|---|
| `pm/archive/book_archive.py` | SQLite order-book snapshots, keyed `(venue, market_id, token_id, snapshot_ts)`. ↳ [notes](docs/module-notes.md#pmarchivebook_archivepy) |
| `pm/archive/trade_archive.py` | SQLite executed-trade prints (the Kalshi tape), keyed on `trade_id` so re-fetching is idempotent. ↳ [notes](docs/module-notes.md#pmarchivetrade_archivepy) |
| `pm/capture/book_poller.py` | Live `/book` capture across providers → `book_archive`. `--loop`. ↳ [notes](docs/module-notes.md#pmcapturebook_pollerpy) |
| `pm/capture/trade_poller.py` | `markets/trades` → `trade_archive`. `--backfill` reconstructs a finished game's tape. ↳ [notes](docs/module-notes.md#pmcapturetrade_pollerpy) |
| `pm/capture/mlb_book_poller.py` | REST fallback book poller (not needed when `kalshi_ws` runs). 15s live / 120s idle. ⏸ stopped. |
| `pm/capture/advisory_capture.py` | ⏸ stopped 2026-09-09, **disabled 2026-10-13** (left `enabled`, so the 10-11 reboot silently revived it — retiring a service means `disable --now`, not `stop`) — candidate A died on its kill rule (median |Δmid@90min| = 0). **Was the candidate-A instrument** (spec = edge log §2026-08-24 late-3): NHC advisory events (detection-time T0 + raw product text) + storm-market ORDERBOOK paths w/ depth, 15s hot / 90s idle. Places no orders; never imports `kalshi_orders`/`execution`. ↳ [notes](docs/module-notes.md#pmcaptureadvisory_capturepy--deploysystemdadvisory-captureservice) |
| `pm/capture/near_close_capture.py` | ⏸ disabled 2026-11-04 — every consumer closed; `candle_panel` reconstructs near-close historically, killing the forward-only premise. Was the idle-accrual engine (CAPTURE at ~2h lead + RESOLVE); 100 MB panel retained. ↳ [notes](docs/module-notes.md#pmcapturenear_close_capturepy-configsystemdusernear-close-captureservice) |
| `pm/research/candle_panel.py` | Historical near-close reconstruction for SETTLED markets via `get_candlesticks` — kills the "unreachable" premise. `load_panel(lead_hours)` is the study surface. ↳ [notes](docs/module-notes.md#pmresearchcandle_panelpy-configsystemdusercandle-panelservice) |
| `pm/capture/mention_capture.py` | ⏸ disabled 2026-11-04 — all three mention angles closed (sniping 07-30, pricing 08-24, candidate D 09-09); was accruing on spec. ~10s repricing capture w/ hourly series discovery; 755 MB retained. ↳ [notes](docs/module-notes.md#pmcapturemention_capturepy-configsystemdusermention-captureservice) |
| `pm/capture/news_capture.py` | ⏸ stopped. News-reaction speed instrument; detection lag median ~150 min → free RSS likely too slow. ↳ [notes](docs/module-notes.md#pmcapturenews_capturepy-configsystemdusernews-captureservice) |
| `pm/capture/tennis_capture.py` | ⏸ stopped. Joins `tennis_live` events to the Kalshi market to measure book lag. ↳ [notes](docs/module-notes.md#pmcapturetennis_capturepy-configsystemdusertennis-captureservice) |
| `pm/capture/wnba_capture.py` | ⏸ stopped. Same for WNBA WP-swings; exact abbr→abbr matching. ↳ [notes](docs/module-notes.md#pmcapturewnba_capturepy-configsystemduserwnba-captureservice) |

### Live + paper arms

| File | Responsibility |
|---|---|
| `pm/arms/reactive_scalper.py` | In-game reaction arm (WPA-gated entry on the book's lag). Ran live 2026-06-06→09, **pulled to paper** — scalp-exit whipsaw. `--hold` variant is the validated +EV one. ↳ [notes](docs/module-notes.md#pmarmsreactive_scalperpy-configsystemduserreactive-scalperservice) |
| `pm/arms/pre_position.py` | ⏸ stopped. In-game pre-positioning evaluator, logs to `pre_position_log.jsonl`. ↳ [notes](docs/module-notes.md#pmarmspre_positionpy) |
| `pm/arms/arb_scan.py` | ⏸ stopped. Cross-market arb scanner on **executable** bid/ask + per-leg fees. Finding: no real arb across 3000 markets. ↳ [notes](docs/module-notes.md#pmarmsarb_scanpy-configsystemduserarb-scanservice) |
| `pm/research/storm_watch.py` | Storm-detection trigger (edge candidate A): pages ONCE per new named storm / new storm-flavored Kalshi series; state `data/shared/storm_watch.json` (env `STORM_WATCH_STATE_FILE`). Cron 4×/day after NHC advisory slots. `--status`. |
| `pm/research/inefficiency_sweep.py` | Cross-sector mispricing map — calibration deviation by bucket, one-winner-field overround, venue-wide favourite-longshot. Read-only. ↳ [notes](docs/module-notes.md#pmresearchinefficiency_sweeppy) |
| `pm/ops/snapshot_runner.py` | Cron portfolio snapshotter across every profile (4×/day). ↳ [notes](docs/module-notes.md#pmopssnapshot_runnerpy) |
| `pm/ops/auto_resolve_runner.py` | Cron auto-resolver — marks pending forecasts whose tickers resolved. Without it Track Record stays "untested". ↳ [notes](docs/module-notes.md#pmopsauto_resolve_runnerpy) |

### Mention / audio stack

| File | Responsibility |
|---|---|
| `pm/mention/mention_listen.py` | Streaming audio → per-market word/phrase detection. **Places no orders — must never import `kalshi_orders`/`execution`.** ↳ [notes](docs/module-notes.md#pmmentionmention_listenpy) |
| `pm/mention/mention_rehearsal.py` | Paper-fire + post-event scoring harness — "had we fired at detection, where would we have landed". Never a trader. ↳ [notes](docs/module-notes.md#pmmentionmention_rehearsalpy) |
| `pm/mention/mention_cascade_backfill.py` | Offline trade-tape shape reconstruction for a past mention event. ↳ [notes](docs/module-notes.md#pmmentionmention_cascade_backfillpy) |
| `pm/mention/monologue_recorder.py` | Nightly late-night monologue corpus recorder (OTA → WAV → whisper). Records + transcribes only. ↳ [notes](docs/module-notes.md#pmmentionmonologue_recorderpy-configsystemdusermonologue-recordservice-per-show-timers) |
| `pm/mention/audio_transcribe.py` | Reusable real-time audio→text via faster-whisper on GPU. Any ffmpeg-reachable source (URL, file, PulseAudio). ↳ [notes](docs/module-notes.md#pmmentionaudio_transcribepy) |
| `pm/mention/hdhomerun.py` | Pinned-tuner OTA stream URLs, so pipeline capture never collides with Plex on the same 4-tuner device. ↳ [notes](docs/module-notes.md#pmmentionhdhomerunpy) |
| `pm/mention/feed_latency.py` | Picks the fastest of 2+ live audio feeds carrying the same content (envelope cross-correlation). ↳ [notes](docs/module-notes.md#pmmentionfeed_latencypy) |

### Sidecars

| File | Responsibility |
|---|---|
| `pm/core/sidecars.py` | Sidecar Protocol + registry (`health`/`stats`/`recent_activity`). ↳ [notes](docs/module-notes.md#pmcoresidecarspy), `docs/sidecar-protocol.md` |
| `baseball/` | Baseball sidecar (first instance) — MLB Stats API client, v0.6.0 matchup model, SQLite state, live play daemon, WPA, park factors, FastAPI UI :8770. ↳ [notes](docs/module-notes.md#baseball) |
| `wnba/` | WNBA sidecar — ESPN data, Pythagorean + prior model, FastAPI UI :8771. Ticker suffix IS the yes team. ↳ [notes](docs/module-notes.md#wnba) |
| `tennis/` | Tennis sidecar, first individual sport — rank-seeded Elo, FastAPI UI :8772. Ticker bridge matches the YES player's **full name from the question**, never the 3-letter code. ↳ [notes](docs/module-notes.md#tennis) |
| `pm/live/tennis_live.py` | Tennis live feed + serve-free break detection (a ≥2-game lead within a set guarantees a break). ↳ [notes](docs/module-notes.md#pmlivetennis_livepy) |
| `pm/live/wnba_live.py` | WNBA live feed — ESPN's `summary` exposes a `winprobability` array, so `wp_swing` events come free. ↳ [notes](docs/module-notes.md#pmlivewnba_livepy) |

### Scripts, ops, docs

| File | Responsibility |
|---|---|
| `scripts/backup.sh` | Daily `data/` backup to `/mnt/backups/kalshi-trader/` with `--link-dest` hardlinks, 90-day retention; records its outcome to `backup_status.json` for the health digest. Cron 00:30 UTC. |
| `scripts/logrotate.conf` | User-level logrotate for the cron log files in `data/shared/`. Daily, 14-day history, copytruncate. **Does not — and must not — cover `data/shared/audit/orders.log`**; it silently truncated that audit trail and aged out everything before ~2026-07-11 until 2026-09-20. |
| `scripts/{baseball_web,baseball_live,mlb_book_poller}.sh` | Launchers — foreground by default, `--background` / `--status` / `--stop`. PIDs + logs under `data/shared/`. |
| `~/.config/systemd/user/{baseball,wnba,tennis}-web.service` | Sidecar web UIs on :8770 / :8771 / :8772. Auto-restart, start at boot via linger. Manual launchers: `scripts/{baseball,wnba,tennis}_web.sh`. |
| `deploy/systemd/`, `~/.config/systemd/user/*.service` | Unit files. `baseball-live`, `mlb-book-poller`, `kalshi-ws`, `pre-position` are **⏸ stopped 2026-07-11** (theses measured dead; state DBs retained, re-enable with `systemctl --user enable --now`). |
| `attic/` | Retired code — not imported, linted, typed or tested. `attic/README.md` is the index. Rule: ❌ retired/falsified → attic; ⏸ re-enable-able infrastructure stays at the root. ↳ [notes](docs/module-notes.md#attic) |
| `pyproject.toml` / `Makefile` / `TODO.md` | pytest + coverage config / `make test`, `coverage`, `run` / roadmap by section |
| `docs/edge-research-log.md` | **Canonical edge-research log — findings are documented HERE BEFORE any instrument is coded.** Read before starting any new edge work. ↳ [notes](docs/module-notes.md#docsedge-research-logmd) |
| `docs/failed-experiments.md` | Post-mortems of falsified strategies. Read before re-trying anything that looks like one. |
| `docs/strategies.md` | Trading strategies + ideas (market consensus, take-profit ladder, baseball model, …) |
| `docs/compounding-capital-cycle.md` | ⭐ Next build target, **gated**: build the fractional-Kelly sizing layer only after an arm confirms OOS. ↳ [notes](docs/module-notes.md#docscompounding-capital-cyclemd) |
| `docs/module-notes.md` | The long-form rationale behind every ↳ above |
| `docs/api-surface.md` | Key function signatures for every public entry point |
| `docs/sidecar-protocol.md` | Canonical Sidecar spec — read before adding a sidecar |
| `docs/second-machine.md` | R-26 runbook: stand the system up elsewhere — the secrets that exist nowhere else, data restore, units/cron, verify. `deploy/bootstrap.sh` + `make docker-check` are the executable halves. |
| `docs/sports-model-playbook.md` | Repeatable recipe for standing up a new sport. ↳ [notes](docs/module-notes.md#docssports-model-playbookmd) |
| `docs/user-guide.md` / `docs/developer-guide.md` | End-user feature walkthrough / architecture + extension patterns |
| `docs/api-reference.md` / `docs/glossary.md` | Gamma + CLOB endpoints and quirks / plain-English terms |

## Critical API gotchas — read before touching API code

1. **JSON-string fields**: Gamma returns `outcomes`, `outcomePrices`, and `clobTokenIds` as JSON-encoded strings, not arrays. Always use `parse_json_field()`. `get_markets()` already does this — its DataFrame contains native lists for `outcomes` and `outcome_prices`. Never store raw JSON strings back into the DataFrame.
2. **`get_market` uses `condition_ids` (plural, snake_case)** — not `conditionId`. The wrong param is silently ignored and the API returns its default first market. The fixed `get_market()` also verifies the returned `conditionId` matches the request.
3. **`conditionId` ≠ CLOB token ID.** Price history requires a numeric token ID from `clobTokenIds` (77-digit integer string). Passing a `conditionId` (hex `0x...`) to `/prices-history` returns HTTP 400.
4. **`/prices-history` requires `startTs`.** Without it the API returns `{"history": []}`. No error, just silence.
5. **`market.tokens` is always `null`.** Token IDs come from `clobTokenIds`.
6. **`outcomePrices` always sums to 1.0 for binary markets.** It's a derived midpoint. Real spread info is in `bestBid`/`bestAsk`.
7. **Gamma API caps at 100 markets per request.** Paginate with `offset`. Same applies to `/events`.
8. **`category` field is always `""` on the markets endpoint** AND on the events endpoint directly. The real category data lives in the event's `tags` field (a JSON-encoded list of tag dicts like `[{"label": "Soccer"}, {"label": "Sports"}]`). This project maps tag labels to our buckets via `_TAG_TO_CATEGORY` inside `_category_from_event_tags()`.
9. **Polymarket prices can be as low as $0.0005** for near-resolution losing outcomes. Any `st.number_input` / `st.slider` with `min_value=0.001` will crash on prefill. Use `0.0001` as the floor.
10. **Order book sort order is inverted**: `/book` returns bids in **ascending** price order and asks in **descending** price order — the best top-of-book price is at the END of each list. `parse_order_book()` reverses both. Don't assume `bids[0]` is the best bid.
11. **`_get` retries 429 specially**: it sleeps per `Retry-After` (clamped [0.1s, 30s]) up to `retries` times. Other 4xx/5xx still raise immediately. If you add new endpoints, just call `_get` — no per-call backoff code needed.
11a. **Every connector's `_get` is under a circuit breaker** (`pm/core/circuit_breaker.py`, R-21). Add a new HTTP-touching function the same way — `circuit_breaker.get("<dependency>").call(_unguarded_fn, ...)` — or its failures are retried forever and invisible. Never catch `CircuitOpenError` to retry; it is the breaker saying stop.
12. **Kalshi `?status=` has separate query and response vocabularies.** The query param accepts `open` / `closed` / `settled` (and a few others); passing `finalized` returns HTTP 400. But the response's market-level `status` field uses `finalized` (= settled) and `determined` (= closed, awaiting settlement). When filtering settled markets via the query, use `?status=settled` and expect response rows with `status="finalized"`.
13a. **Kalshi v2 orders 400 on a `client_order_id` containing `.`** — `invalid_parameters` with no field hint. Fractional-strike tickers (`KXSOLD-…-T72.9999`, `KXXRP-…-B1.0499500`) contain dots, so any coid that embeds the raw ticker makes every fractional-strike market silently unorderable (this hid for a month: 23/23 canary errors vs 2/2 accepts partitioned exactly on the dot; isolated 2026-08-01 by A/B probe — identical body, dotted coid 400s, sanitized accepted). `kalshi_orders._make_client_order_id` now sanitizes to `[A-Za-z0-9_-]`.
13b. **Kalshi fill prices are YES-leg** — `average_fill_price` (and fills' `yes_price_cents`) quote the YES contract regardless of which side you traded. A NO buy at NO-cost 0.83 reports `average_fill_price 0.17`. `execution.LiveBroker.buy` converts to our-side terms (`1 − yes`) per the `OrderResult` contract; anything reading fills/order responses directly must convert itself.
13c. **Kalshi sharded the exchange — `exchange_index` decides whether your money can reach a market.** Since ~2026-09 every market carries an `exchange_index` (0 Default / 1 Combos / 2 Crypto / 3 Tennis & Baseball; `GET exchange/status` names them) and the account holds a SEPARATE balance per shard (`portfolio/balance` → `balance_breakdown`). Ordering a market on a shard where the balance is $0 fails with a bare **HTTP 404 `user_not_found`** — which reads like a broken endpoint, not "your money is on another shard". Found 2026-09-09 by the live canary: a 1-contract order on `KXBTCD` (index 2, Crypto) 404'd while every read path was healthy. **All current research targets are index 0** (storms, mention/earnings, weather, macro, WNBA), which is where the balance sits; **MLB is index 3 and crypto is index 2, both unfunded**. `live_preflight` now prints funded vs unfunded shards, and `kalshi_orders` translates the 404 into an actionable log line. Moving money between shards needs an intra-exchange transfer (`intra_exchange_transfers_active: true`).

13. **Kalshi order endpoint is v2 — the old `/portfolio/orders` create/cancel is deprecated** (returns `410 deprecated_v1_order_endpoint` since ~2026-06-26). Use `POST`/`DELETE /portfolio/events/orders[/{order_id}]` (`kalshi_orders._ORDERS_ENDPOINT`). The v2 body quotes from the **YES leg only** as a book side: `side: "bid"` = buy YES, `side: "ask"` = sell YES, with `price` a **fixed-point dollar STRING** (`"0.45"`, not `45`) and `count` a string. NO is expressed as the opposite side at the complement price (buy NO @P → `ask` @`1-P`); `_v2_book_side_and_price()` does the translation. `time_in_force` (`good_till_canceled`) and `self_trade_prevention_type` (`taker_at_cross`) are now **required**. The GET list-orders path (`get_resting_orders`) was NOT deprecated — only create/cancel moved.

## Gotchas that have actually cost data or days (non-API)

These are not venue quirks — they are this codebase's own traps, each learned
expensively. The API gotchas above are about Kalshi; these are about us.

1. **Never freeze a path at import.** `DB = os.getenv("X", "data/...")` at
   module level captures the environment as it was at import, so nothing —
   tests especially — can redirect it. This has put synthetic rows into
   production databases **three separate times**. Every store now has a lazy
   `_db_path()` (or `profiles.profiles_root()`, `reactive_log_path()`, …) plus
   an env override, and the root `conftest.py` redirects all nineteen (moved
   there from `tests/` on 2026-08-17 so root doctests get the same isolation).
   If you add a store, do both.
1b. **Never anchor a path default on `__file__` either.** The sibling trap of
   (1): an env-var default captures the environment at import; a
   `Path(__file__).parent / "data"` default captures the module's LOCATION.
   Both look correct until something moves. When R-24 Phase B moved
   `emergency_stop` and `profiles` into `pm/core/`, the kill-switch flag
   default silently became `pm/core/data/.emergency_stop` and the per-profile
   tree default became `pm/core/data/` — nothing crashed, `status` just
   reported NOT ACTIVE while looking where nobody writes. Anchor on
   `pm.REPO_ROOT` (the one place the root is derived); `provenance` is the
   allowed exception because it ASCENDS to `.git`, which is correct from any
   depth. `tests/test_pm_layout.py` enforces all of this, plus the strict
   pm/ layering.
2. **The test tripwire watches specific file types.** A session-scoped fixture
   in the root `conftest.py` fingerprints the live `data/` tree and fails the run
   if anything changed. It originally watched only `.json` and therefore missed
   a SQLite write; it now covers `.sqlite` and `.txt`. Adding a new artifact
   type means adding it there. If it fires, fix the escaping path — never
   silence it.
3. **pandas empty-frame traps, both of which produced `KeyError` in production
   filter chains:**
   - `df[[]]` — an *empty list* — is **column** selection, not row selection,
     so a frame with no qualifying rows comes back with zero columns.
   - `.map()` on an empty frame returns a `str`-dtype Series; inverting it
     yields a mask that also drops every column.
   Both surfaced as an exception on a routine "nothing qualified this cycle",
   which `run_cycle` swallowed and mislabelled "discovery failed". **Guard with
   `if cand.empty: return 0` between filter stages**, and prefer a boolean
   `pd.Series` mask over a bare list.
4. **Nothing loads `.env` unless it says so.** No systemd unit supplies an
   `EnvironmentFile`. The four web apps did not call `load_dotenv()`, so
   `WEB_UI_TOKEN` was absent in the served process and the security gate would
   have reported itself open while believed closed. Any new entry point that
   reads configuration must load dotenv itself, **before** anything reads a
   setting.
5. **Live-gate state depends on the asking process.** `kalshi_auth.
   authorized_profiles()` compares against the ACTIVE profile, and each daemon
   pins `POLYMARKET_ACTIVE_PROFILE` for its own cycle — so a shell snippet
   reports "all paper" while daemons are live. Use `python -m live_gates`,
   which pins per profile the way a daemon does. Never trust an ad-hoc check.
6. **Money paths fail CLOSED.** `emergency_stop.is_active()` returns True on
   `OSError`; `LiveBroker._in_drawdown_pause()` returns True when equity is
   unknown or its state file is unusable. Preserve that direction: on a money
   path, not knowing is not the same as being fine.
7. **A developer machine hides isolation and dependency bugs.** Files that
   already exist never look "created", and locally-installed packages mask a
   missing manifest entry. Both reached CI during the hardening pass despite
   passing locally. Verify against a fresh clone with an empty `data/`, in a
   clean 3.11 venv built only from `requirements.txt`.
8. **Tests could reach the real venue.** A `LiveBroker` test mocked the
   placement, the resting-order check and the fills — but not `cancel_order`,
   so its adopted phantom order was really `DELETE`d on Kalshi with live
   credentials on every run (404; 70 `cancel_*` rows in the LIVE `orders.log`
   between 2026-08-15 and 08-17, invisible to the tripwire because it did not
   watch `orders.log`). Isolation of `data/` had a tripwire; isolation of the
   venue had only convention. Now the root `conftest.py` blocks
   `socket.connect` outright for every test (`_no_network`), redirects
   `ORDERS_LOG_PATH`, and the tripwire watches `orders.log` by name. **A test
   that needs a connector mocks it above the socket.** The 58 synthetic
   `OID-RECOVERED` rows are left in the append-only log; they count toward
   nothing (`_today_submit_count` counts only `submit`).

9. **Cron runs from `$HOME` — every entry and every script it calls must
   `cd` to the repo first.** `python -m pm.<x>` resolves the package from the
   CWD, and relative log-redirect paths point into `~`. This bit twice in one
   week: the storm_watch crontab line (the only one without the `cd` prefix)
   silently never ran for 5 days, and `scripts/backup.sh` computed `REPO_DIR`
   but never `cd`'d into it, so after R-24 the nightly sqlite stage failed
   four nights running — while the snapshot directory kept appearing, hiding
   it from every freshness view. New cron entries copy an existing line's
   `cd <repo> && ` prefix; scripts invoked by cron `cd "$REPO_DIR"` at the
   top; and anything whose failure matters records an outcome the health
   digest checks (`backup_status.json` is the pattern).

## Key function signatures

Moved to **[`docs/api-surface.md`](docs/api-surface.md)** — every public entry
point in the root modules and the sidecar packages, with the contracts and
gotchas that aren't visible from the type alone. Update it in the same commit
as any signature change.

## Sidecar architecture

A **sidecar** is a domain-isolated sub-package that owns its state, runs on its own schedule, and exposes:
1. An **operational** surface (`health`/`stats`/`recent_activity`) per `sidecars.Sidecar` protocol — uniform across sidecars, used by the control plane.
2. A **domain** surface unique to that sidecar (e.g. `baseball.predict_game`, future `news.recent_since`).

Canonical spec: `docs/sidecar-protocol.md`. **Read before adding the second sidecar.**

**Load-bearing rule**: a sidecar does NOT import `kalshi_client`, `provider`, `polymarket_client`, `backtest`, `edges`, `calibration`, `portfolio`, `risk`, `alerts`, `shared` (now `pm/ui/shared`), or `pm/ui/pages/*`. The market app reaches into the sidecar via its public `__init__.py` only. The single legitimate cross-boundary file is the *strategy adapter* — since R-24 that is literally one file per sport (`edges/baseball.py`, `edges/wnba.py`, `edges/tennis.py`), which turns a sidecar's domain output into an `Edge` / `Signal`.

**Sidecar UIs are NOT Streamlit.** Streamlit's session model doesn't scale to concurrent users; the eventual app-wide replatform is FastAPI. Baseball ships with its own FastAPI+Jinja2+HTMX UI on port 8770. New sidecars should follow the same pattern. The main Streamlit app links out via the sidebar — does not embed.

**Storage**: one SQLite per sidecar at `data/shared/<sidecar>_state.sqlite`. WAL + `synchronous=NORMAL`. Env override `<SIDECAR>_STATE_PATH`. Schema versioning via `PRAGMA user_version` + idempotent `migrate()` in `state.py`.

## Provider abstraction

Network-touching operations route through `provider.get_provider()` so the app can swap between Polymarket and other venues (Kalshi planned next, then Polymarket-US). Schema is normalized at the provider boundary — pure helpers (scanners, Kelly, parse fns) take a DataFrame and don't care which venue produced it.

**Rule**: any new HTTP-touching code goes through a provider method. Pure data transforms can live anywhere; they take and return DataFrames.

**Don't** add provider-specific behavior to scanner functions or `kelly_sizing` — those are venue-agnostic by design. If a new venue's schema differs, normalize it inside that provider's methods so the helpers see what they expect.

### Kalshi-specific notes

- Base URL: `https://api.elections.kalshi.com/trade-api/v2` (env `KALSHI_API_BASE`)
- Pagination is **cursor-based**, not offset
- Prices are **already in dollars** as strings (`"0.7800"`)
- Ticker IS the identifier — no separate `condition_id`/`token_id`
- Series ticker = prefix before first `-` (used for candlestick URL)
- `period_interval` only accepts 1, 60, 1440 minutes (`_closest_period_interval` rounds)
- Settled markets: `status == "finalized"`, `result` ∈ {`"yes"`, `"no"`, `""`}
- Categories on events only — `get_markets` enumerates via `/events?with_nested_markets=true`
- Orderbook YES levels → bids; NO levels → asks at price `1 − no_price` (selling NO at X = buying YES at 1-X)

## Lazy file-path pattern (profile-aware data modules)

Each of `pm/ledger/portfolio.py`, `pm/ledger/calibration.py`, `pm/ledger/alerts.py`, `pm/ledger/risk.py` has a private `_*_path()` function that's called on every read/write. Resolution order:

1. **Env-var override** (e.g. `POLYMARKET_PORTFOLIO_FILE`) — used by tests via `monkeypatch.setenv` and by `.env` config
2. **Active profile's directory** via `profiles.file_path(kind)`

This is what makes profile switching work without re-importing modules. `st.rerun()` after `set_active_profile()` is enough.

**Don't** reintroduce module-level `XXX_FILE = Path(os.getenv(...))` constants — they freeze at import time and break profile switching.

## DataFrame column references

### `get_markets()` output

```
id, condition_id, question, category, event_id, volume, volume_24hr,
liquidity, end_date, outcomes (list[str]), outcome_prices (list[float|None]),
active, spread, best_bid, best_ask, last_trade_price, days_to_resolution
```

`outcomes` and `outcome_prices` are **native Python lists** here (parsed once in `get_markets`). Safe to `zip()` and iterate directly. `event_id` is the first embedded event's id (or `""`) — used as the join key with `get_events()`. After `enrich_categories_from_events()`, an additional `event_title` column is present.

### `get_events()` output

```
id (str), title, slug, category (mapped from tags), volume_24hr, active
```

### `parse_order_book()` output

```
side ("bid" | "ask"), price (float), size (float), cumulative_size (float)
```

Within each side: sorted best-price-first (descending bids, ascending asks). `cumulative_size` accumulates from the best price outward.

### `get_price_history()` output

```
timestamp (datetime64, UTC), price (float64, 0–1)
```

### `get_transactions()` output

```
id, timestamp, type ("buy"|"sell"), question, condition_id, outcome,
shares, price, timestamp_dt
```

### `get_positions()` output

```
question, condition_id, outcome, shares, avg_price, cost_basis, added_at
```

After `enrich_with_current_prices()` also has: `current_price`, `current_value`, `unrealized_pnl`, `pnl_pct`.

### `get_realized_pnl()` output

```
question, condition_id, outcome, realized_pnl
```

### `get_alerts()` output

```
id, created_at, question, condition_id, outcome, direction, threshold,
triggered, triggered_at, triggered_price, created_at_dt
```

`direction` is `"above"` or `"below"`. Triggered alerts have non-null `triggered_at` and `triggered_price`; non-triggered alerts have `None`/`NaN` there. The DataFrame is sorted newest-first by `created_at_dt`.

### `get_snapshots()` output

```
timestamp, cost_basis, current_value, unrealized_pnl, realized_pnl,
open_positions, timestamp_dt, net_value, total_pnl
```

`net_value = current_value + realized_pnl`. `total_pnl = unrealized_pnl + realized_pnl`. Both derived on read, not stored.

### `get_forecasts()` output

```
id, timestamp, question, condition_id, outcome, market_price,
your_probability, resolution (None|bool), resolved_at, timestamp_dt
```

### `calibration_bins()` output

```
bin_center, n, mean_predicted, observed_rate
```

## Category system

Three-stage pipeline (highest priority first):
1. **`/events` tags** — `get_events()` fetches events with their `tags` array; `_category_from_event_tags()` maps tag labels (e.g. `"Soccer"`) to our buckets via `_TAG_TO_CATEGORY`. `enrich_categories_from_events()` overrides the keyword category with this authoritative one when present.
2. **`event.seriesSlug`** on embedded events (in `_infer_category`) — authoritative for sports leagues without an events fetch.
3. **Keyword matching** against `_CATEGORY_RULES` (ordered list, first match wins). `_kw_match()` uses regex word boundaries for short tokens so `"eth"` doesn't false-positive inside `"something"`.
4. Fallback to `"Other"`.

Valid categories: `Crypto`, `Sports`, `Politics`, `Finance`, `Tech`, `Geopolitics`, `Entertainment`, `Other`.

`app.py` calls `load_events()` (cached 300s) and runs the enrichment inside `load_markets()` so every page automatically benefits.

## App architecture notes

- **Multi-page via `st.navigation`**: `app.py` is the router (configures page, builds sidebar chrome, calls `st.navigation`). Each `pm/ui/pages/*.py` runs top-to-bottom when its route is selected — no `if/elif page == ...` switch anywhere.
- `shared.load_markets()` is the single `@st.cache_data(ttl=60)` entry every page uses. **Don't pass per-page `limit=` overrides** — that creates multiple cache entries and doubles API traffic.
- `shared.load_markets()` catches `RequestException` and renders a friendly error card.
- After mutations (buy/sell/delete/forecast): call `st.cache_data.clear()` then `st.rerun()`.
- All analysis logic belongs in the appropriate data module, **not** inline in pages. Pages stay presentational.
- Sidebar chrome (alert badge + "Page rendered at HH:MM:SS UTC") is added by `app.py` *after* `pg.run()` so it lands below page-specific sidebar widgets.
- Sidebar header (above the auto-generated nav) contains the **market-venue selector** then the **profile selector** — in that order, because venue is more fundamental than per-user state. Both follow the same set-clear-rerun pattern.

### Adding a new page

1. Create `pm/ui/pages/your_page.py`. It runs top-to-bottom when selected.
2. `from pm.ui.shared import load_markets, ...` for cached data + helpers.
3. Add `st.Page("pm/ui/pages/your_page.py", title="🎯 Your Page")` to the list in `app.py` (the path is relative to the main script's directory — the repo root).

## Running

```bash
./run.sh                          # default — creates .venv, installs deps, launches
./run.sh --server.port 8502       # custom port
PORT=8080 ./run.sh                # same via env var
```

Python 3.11+ required.

## Environment variables

All optional — defaults are public Polymarket endpoints and local file paths.

| Variable | Default |
|---|---|
| `POLYMARKET_GAMMA_BASE` | `https://gamma-api.polymarket.com` |
| `POLYMARKET_CLOB_BASE` | `https://clob.polymarket.com` |
| `POLYMARKET_HTTP_TIMEOUT` | `15` (seconds) |
| `POLYMARKET_LOG_LEVEL` | `INFO` |
| `POLYMARKET_LOG_FORMAT` | unset → text (`HH:MM:SS [LEVEL] name: msg [k=v …]`); `json` → one JSON object per line (`ts`/`level`/`logger`/`msg`/`ctx`) — `pm/core/logctx.py` |
| `POLYMARKET_PORTFOLIO_FILE` | `./portfolio.json` |
| `POLYMARKET_FORECASTS_FILE` | `./forecasts.json` |
| `POLYMARKET_ALERTS_FILE` | `./alerts.json` |
| `POLYMARKET_POSITION_META_FILE` | `data/<profile>/position_meta.json` |
| `KALSHI_LIVE_ENABLED` | `false` (must be `true` to place real orders) |
| `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` / `KALSHI_OWNER_PROFILE` | unset / unset / `jeff` (live-trading credentials + owner-profile lock) |
| `LIVE_TRADING_PROFILES` | empty (comma-sep profiles allowed to use `execution.LiveBroker`; empty = all paper) |
| `LIVE_MAX_STAKE_DOLLARS` | `25` (LiveBroker per-order hard cap; set tiny for min-size canary) |
| `WEB_UI_TOKEN` | unset (gate disabled, apps bind loopback-only, logs a loud warning). Shared secret for the four LAN-facing web UIs; setting it binds them `0.0.0.0` and requires the token — see `pm/core/web_auth.py` |
| `WEB_BIND_HOST` | unset. Explicit bind-address override for the four FastAPI UIs (overrides the token-derived default) |
| `HDHR_IP` | unset → `192.168.0.50` (`hdhomerun.device_ip()`, resolved per call). The tuner is on DHCP and keeps moving (each move silently killing captures) — **pin the current IP in `.env`**, re-find via `curl http://<tuner-ip>/discover.json`. A DHCP reservation on the router is the real fix |
| `NHC_BASE` / `NHC_HTTP_TIMEOUT` | `https://www.nhc.noaa.gov` / `20` — NHC active-storm feed (`pm/feeds/nhc_data.py`) |
| `ADVISORY_CAPTURE_DB` | `data/shared/advisory_capture.sqlite` (candidate-A capture store; conftest-redirected) |
| `STORM_WATCH_STATE_FILE` | `data/shared/storm_watch.json` (storm-trigger dedupe state; conftest-redirected) |
| `NTFY_TOPIC` / `NTFY_SERVER` / `NTFY_PRIORITY` / `NTFY_TOKEN` | unset / `https://ntfy.sh` / `high` / unset — phone push. **The topic is a secret**: a public ntfy topic is readable by anyone who knows it. See `docs/push-notifications.md` |
| `HEALTH_LOG_FILE` | `data/shared/health_events.jsonl` (operator-visible event log) |
| `OOS_LEDGER_FILE` | `data/shared/oos_ledger.json` (rule-freeze dates per arm) |
| `POLYMARKET_DATA_DIR` | unset → `./data` (per-profile tree root; resolved per call, which is what makes test isolation possible) |
| `REACTIVE_LOG_FILE` | `data/shared/reactive_log.jsonl` |
| `LIVE_EQUITY_STATE_FILE` | `data/shared/live_equity.json` (drawdown breaker's persisted account high-water mark) |
| `ORDERS_LOG_PATH` | `data/shared/audit/orders.log` (real-order audit log; in `audit/` so the `data/shared/*.log` logrotate glob cannot reach it — see `scripts/logrotate.conf`. The root `conftest.py` redirects it for every test) |
| `CIRCUIT_FAILURE_THRESHOLD` / `CIRCUIT_COOLDOWN_S` | `5` / `300` — consecutive dependency failures that open a connector's circuit breaker, and how long it fails fast before one probe (`pm/core/circuit_breaker.py`) |
| `PROVENANCE_COMMIT` | unset → read from `.git`; set it in a build without one so results still carry the producing commit (`pm/core/provenance.py`) |
| `BACKUP_MAX_SQLITE_MB` / `BACKUP_FORCE_INCLUDE` / `BACKUP_SKIP_EXPECTED` | `8192` / empty / `book_archive.sqlite` — nightly-backup size policy; larger databases are skipped, named, and recorded in `backup_status.json` — the health digest **pages** on any skip not in `BACKUP_SKIP_EXPECTED` (the 2048 default silently dropped the live candle panel for 12 nights in Oct 2026) |
| `BACKUP_DEST_ROOT` | `/mnt/backups/kalshi-trader` (overridable so the backup can be exercised against a scratch destination) |
| `BACKUP_STATUS_FILE` | `data/shared/backup_status.json` (written by `scripts/backup.sh` after every run; `health_digest.check_backup` pages on FAILED/stale/missing) |
| `LIVE_MAX_ORDERS_PER_DAY` | unset → pre-flight default 10/UTC-day; raise for a high-frequency arm (reactive scalp ≈50–80/day) — still an anti-runaway cap |
| `LIVE_MARKETABLE_CROSS_CENTS` | `1` (`execution.LiveExecConfig.marketable_cross_cents`) — cents an ENTRY buy crosses the spread to take liquidity. `0` rests at the (often stale) ask and frequently never fills once the book ticks during the reaction window (measured ~33% of live reaction buys rested unfilled at 0). Set `2` for the reaction arm (covers a 1¢ adverse tick). Non-marketable sells (resting take-profits) always rest AT price; marketable exits use `exit_cross_cents`. |
| `POLYMARKET_ACTIVE_PROFILE` | unset (process-local active-profile override; honored by `profiles.active_profile()` before the `.active_profile` file — set by long-running daemons around their cycle so the owner-lock is process-local) |
| `BASEBALL_STATE_PATH` | `data/shared/baseball_state.sqlite` |
| `BASEBALL_CHAT_MODEL` | `llama3.1:latest` (Ollama model used by /chat default) |
| `BASEBALL_CHAT_TIMEOUT` | `60` (seconds; Ollama HTTP timeout for chat) |
| `BASEBALL_DISPLAY_TZ` | `America/Chicago` (IANA name; baseball web UI display timezone, handles DST automatically. Set to `UTC` to show raw UTC.) |
| `BASEBALL_WEB_URL` | `http://localhost:8770` (used by app.py sidebar link) |
| `MLB_STATS_API_BASE` | `https://statsapi.mlb.com/api/v1` |
| `MLB_HTTP_TIMEOUT` | `15` (seconds) |
| `WNBA_STATE_PATH` | `data/shared/wnba_state.sqlite` |
| `WNBA_HTTP_TIMEOUT` | `15` (seconds) |
| `WNBA_DISPLAY_TZ` | `America/New_York` (web UI display tz) |
| `WNBA_WEB_URL` | (optional; app.py sidebar link override, else `http://<lan-ip>:8771`) |
| `TENNIS_STATE_PATH` | `data/shared/tennis_state.sqlite` |
| `TENNIS_HTTP_TIMEOUT` | `20` (seconds) |
| `ESPN_TENNIS_SITE_BASE` | `https://site.api.espn.com/apis/site/v2/sports/tennis` |
| `ESPN_TENNIS_CORE_BASE` | `https://sports.core.api.espn.com/v2/sports/tennis/leagues` |
| `TENNIS_DISPLAY_TZ` | `America/New_York` (web UI display tz) |
| `TENNIS_WEB_URL` | (optional; app.py sidebar link override, else `http://<lan-ip>:8772`) |
| `ESPN_WNBA_SITE_BASE` | `https://site.api.espn.com/apis/site/v2/sports/basketball/wnba` |
| `ESPN_WNBA_STANDINGS_BASE` | `https://site.api.espn.com/apis/v2/sports/basketball/wnba` |

`app.py` runs `dotenv.load_dotenv()` at startup; copy `.env.example` to `.env` to set any of the above.

## Logging

Every data module has `log = logging.getLogger(__name__)`. `log.info` on mutations (buy/sell, save forecast, create alert, fire alert, file migration). `log.warning` on recoverable failures (429s, mismatched market filter, midpoint fetch fail).

**Configuration is `pm.core.logctx.setup()`** (R-23) — every entry point calls it instead of hand-rolling `logging.basicConfig` (37 slightly-different copies were replaced). It honors `POLYMARKET_LOG_LEVEL` and `POLYMARKET_LOG_FORMAT` and installs the correlation filter: inside `logctx.bind(...)` or a `@logctx.scope(arm=…)`-decorated cycle, every log line gains a `[arm=… cycle=…]` suffix; outside, lines look exactly as before R-23. The arms' `run_cycle` and `strategy_runner` cycles are bound; `execution._place` binds `ticker`/`action`/`order` at the one choke point every live order passes; `kalshi_orders._audit` embeds the bound ctx into `orders.log` rows — a real order's audit trail names the cycle that placed it. Grep one cycle id across journald + `orders.log` to see everything that cycle did.

## Testing & lint

```bash
make check       # everything: ruff + mypy + pytest (what CI runs)
make test        # pytest + doctests + coverage (enforces 80% minimum, ~95% actual)
make test-fast   # no coverage, fast
make lint        # ruff check .
make typecheck   # mypy on the four non-Streamlit modules
make audit       # pip-audit against requirements.lock.txt (needs network + `pip install pip-audit`)
```

- Tests live in `tests/` — see `tests/conftest.py` for shared data fixtures; the project-wide isolation fixtures + live-data tripwire are in the root `conftest.py` (they govern root doctests too).
- `tests/test_calibration.py` has its own `forecasts_path` fixture (uses `monkeypatch`).
- Mock `polymarket_client._get` to test API functions without network.
- Doctests in the root modules and the sidecar packages ARE collected (`testpaths = ["."]` + `--doctest-modules`, with `pages`/`attic` in `norecursedirs` (basename patterns — the `pages` entry covers `pm/ui/pages/` too) and `app.py` `--ignore`d; the root `conftest.py` applies the same isolation + tripwire to them). **Never use a `"*.py"` glob there**: testpath entries are initial args, which bypass `--ignore`, so the glob imported `app.py` at collection and ran `initialize_profiles()` + the sidecar bootstraps against the real `data/` tree before any fixture existed. **They were not before 2026-08-17** — `--doctest-modules` sat in `addopts` while `testpaths` pointed only at `tests/`, so every "doctested" pure function was a claim, not a check (three stale expectations surfaced the first day they ran). A doctest that needs a store must be pure or use the env redirects; `attic/` is never collected.
- CI runs on every push via `.github/workflows/ci.yml` — four jobs: lint, typecheck, test, **audit** (`pip-audit` on the lock file, `--strict`; also on a weekly schedule so a CVE against a pinned version surfaces between pushes). Never run before 2026-08-17; first result clean.

## When you ship a change — keep these in sync

1. **TODO.md** — move shipped items into ✅ Done with the commit SHA.
2. **docs/user-guide.md** — update if you added or changed a page, control, or workflow.
3. **docs/developer-guide.md** — update if you added or changed a public function, module, or DataFrame schema.
4. **docs/api-reference.md** — update if you discovered a new API quirk or use a new endpoint.
5. **docs/glossary.md** — add an entry for any new domain term the UI shows.
6. **This file (`docs/file-map.md`)** — add or amend the file-map row, the DataFrame columns, and any new critical gotcha. Keep rows to a line or two; the long form goes in (7).
7. **docs/module-notes.md** — the rationale: why you built it, what it measured, what it falsified, what trap is inside it. Link it from the file-map row as ↳.
8. **docs/api-surface.md** — any changed or added public signature.
9. **`README.md`** — update the feature table and file layout for visible additions.

If the doc and code disagree, the code is right but **fix the doc in the same commit**.

## What's NOT here yet (potential next features — see `TODO.md` for full list)

- True push-based WebSocket prices (currently 3s REST polling via `st.fragment` in
  `shared.live_price_panel`; upgrade path documented in developer-guide)
- Event-grouping UI (multiple sub-markets shown together under their parent event)
- Multi-outcome Kelly (optimal allocation across N outcomes simultaneously)
- Order book depth viewer (`GET /book`)
- `/events` integration for real category data
- Bulk parallel CLOB fetches
- Authentication for placing orders (requires Polymarket API key + Polygon wallet)
- Multi-outcome Kelly
- Backtest mode
