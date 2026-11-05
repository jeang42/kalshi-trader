# Developer Guide

> **Currency note (2026-08-17).** This guide was last revised 2026-06-04 and
> describes the architecture as of that date. It is still accurate on the core
> — provider abstraction, backtest engine, sidecar protocol, data schemas — but
> it predates the trading arms added since, and the whole operational layer
> added in the P0+P1 hardening pass.
>
> **`docs/file-map.md` is the authoritative map** and is kept current commit by commit.
> Use this guide for the *why* of the core design; use `docs/file-map.md` for what
> exists today. A full revision is tracked as P3/R-24.
>
> **`docs/file-map.md` was split on 2026-08-23 (R-25).** It is now the index — file
> map, API gotchas, DataFrame schemas. The long form moved to
> [`docs/module-notes.md`](module-notes.md) (why each module exists, what it
> measured, what falsified it) and [`docs/api-surface.md`](api-surface.md)
> (key function signatures). References below to "`docs/file-map.md` function
> signatures" mean `api-surface.md`.
>
> Modules added since this guide was written, each documented in `docs/file-map.md`:
>
> | Module | Purpose |
> |---|---|
> | `pm/core/atomic_io.py` | Crash-safe, lock-protected JSON persistence for the ledgers |
> | `pm/trading/live_gates.py` | Authoritative "what can place real orders right now" |
> | `pm/ledger/oos_ledger.py` | Out-of-sample bookkeeping; resets on any rule change |
> | `pm/core/sqlite_schema.py` | Versioned migrations (`PRAGMA user_version`) for the 13 arm/capture/archive stores; arms carry CHECK/NOT NULL constraints |
> | `pm/core/shutdown.py` | Cooperative SIGTERM handling for the daemons; `deferred()` masks the place-then-record window |
> | `pm/core/provenance.py` | Commit stamp on positions, audit lines, exits, backtest runs and reports |
> | `pm/core/circuit_breaker.py` | Per-dependency circuit breakers on every outbound connector; open/close reported via the health log |
> | `pm/core/notify.py` / `pm/ops/health_digest.py` | Operator alerting (log + desktop + phone) |
> | `pm/core/web_auth.py` | Shared-secret gate for the LAN-facing web UIs |
> | `pm/ops/backup_sqlite.py` | Consistent DB snapshots + restore verification |
> | `deploy/` | systemd units + crontab under version control |


## Architecture Overview

```
app.py                        ← Router only. Configures page, builds sidebar
    │                           chrome, dispatches to pages/ via st.navigation.
    │
shared.py                     ← Cached load_markets / load_events, alert hook,
    │                           UI constants. All pages import from here.
    │
pages/                        ← One file per page. Each runs top-to-bottom
    │   markets.py              when its route is selected. No if/elif switch.
    │   price_history.py
    │   scanner.py
    │   kelly.py
    │   portfolio.py
    │   calibration.py
    │   alerts.py
    │
provider.py                   ← MarketProvider Protocol + provider registry.
                                Pages and shared.py call `get_provider()`
                                instead of importing polymarket_client directly
                                for any provider-bound (network) operation.
polymarket_client.py          ← Polymarket-specific HTTP client + pure helpers
                                (scanners, Kelly, parse_order_book). Wrapped
                                by PolymarketProvider in provider.py.
kalshi_client.py              ← Kalshi-specific HTTP client + KalshiProvider.
                                Public-read endpoints only; no auth.
                                Auto-registered into provider.py at import.
portfolio.py                  ← Transaction log + positions / P&L / snapshots
calibration.py                ← Forecast log + Brier score / calibration bins
alerts.py                     ← Price alerts + threshold checking
risk.py                       ← Tier 1 risk-management knobs (see
                                docs/recommendations-design.md)
profiles.py                   ← Multi-user profiles (Option A — local,
                                file-based, no auth)
```

`app.py` and every page in `pages/` is purely presentational. All data fetching, transformation, and analysis lives in `pm/venue/polymarket_client.py`. `pm/ledger/portfolio.py`, `pm/ledger/calibration.py`, and `pm/ledger/alerts.py` each manage a small JSON store and the derived views on top of it. `pm/ui/shared.py` houses cached loaders and constants that every page would otherwise re-declare.

---

## System topology — how all the processes interact

The project is not one binary — it's a constellation of cooperating processes that share state via SQLite + JSON files. Understanding the dataflow is the only way to debug end-to-end issues.

```
                    ┌─────────────────────────────────────────┐
                    │            data/  (shared state)        │
                    │                                         │
                    │  <profile>/portfolio.json               │
                    │  <profile>/forecasts.json               │
                    │  <profile>/alerts.json                  │
                    │  <profile>/settings.json                │
                    │  shared/book_archive.sqlite             │
                    │  shared/llm_cache.sqlite                │
                    │  shared/backtest_runs/*.json            │
                    │  shared/baseball_state.sqlite           │
                    │      (teams, games, predictions,        │
                    │       live_plays)                       │
                    │  shared/*.log                           │
                    └─────────────────────────────────────────┘
                              ▲                ▲
              ┌───────────────┘                └──────────────┐
              │                                               │
   FOREGROUND PROCESSES                              CRON / DAEMONS
   (user-facing UIs)                                 (background data jobs)

   ┌────────────────────────┐                   ┌─────────────────────────────────┐
   │ Streamlit app          │                   │ book_poller (every 5 min)       │
   │  app.py + pages/       │                   │   → book_archive.sqlite         │
   │  port 8501             │                   │                                 │
   │  single-user           │                   │ strategy_runner (×4, /30 min)   │
   │                        │                   │   market_consensus → agentic    │
   │  Reads: all of data/   │                   │   favorite_grind  → agentic_fav │
   │  Writes: <profile>/    │                   │   llm_forecast    → agentic_llm │
   │          *.json on     │                   │   baseball_winprob→ agentic_bb  │
   │          user actions  │                   │   → <profile>/portfolio.json    │
   └────────────────────────┘                   │   → <profile>/forecasts.json    │
                                                │                                 │
   ┌────────────────────────┐                   │ auto_resolve_runner (every 15m) │
   │ Baseball web UI        │                   │   → marks resolutions across    │
   │  baseball/web.py       │                   │     every profile's forecasts   │
   │  port 8770             │                   │                                 │
   │  MULTI-USER (FastAPI)  │                   │ bulk_deep_analysis (×6 nightly) │
   │                        │                   │   one model per cron line       │
   │  Reads: baseball_state │                   │   → agentic_deep/forecasts.json │
   │  Writes: triggers      │                   │                                 │
   │   refreshes via        │                   │ baseball.runner (nightly 04:15) │
   │   subprocess           │                   │   refresh teams/pitchers/sched  │
   └────────────────────────┘                   │   → baseball_state.sqlite       │
                                                │                                 │
                                                │ baseball.live (daemon, 10s)     │
                                                │   polls feed/live per game      │
                                                │   → live_plays table            │
                                                │                                 │
                                                │ logrotate (daily 02:00)         │
                                                │   rotates shared/*.log          │
                                                │                                 │
                                                │ backup.sh (daily 00:30)         │
                                                │   rsync --link-dest             │
                                                │   → /mnt/mnt/backups/         │
                                                └─────────────────────────────────┘
```

### Key dataflows

**The forecast pipeline (what produces head-to-head Brier scores):**

```
   strategy_runner --strategy X --profile P
        │
        ▼ scans active markets, runs strategy(snap) per market
        │
        ▼ for each signal: adds to <P>/portfolio.json (paper buy)
                          + adds to <P>/forecasts.json (tagged source=X)
        │
        ▼ time passes; market resolves
        │
   auto_resolve_runner   ← walks every profile, joins each forecast's
        │                  condition_id against the union of closed
        │                  markets from every provider (incl. Kalshi
        │                  settlements when authed)
        ▼
   forecasts.json rows gain `resolution: true|false` + `resolved_at`
        │
        ▼ Calibration page reads forecasts.json, groups by `source`,
        │  computes Brier per source.
        │
        ▼ head-to-head comparison: source="baseball" vs source="sports"
          tells us whether our model beats the consensus market.
```

**The book-archive pipeline (groundwork for slippage modeling + live lead-lag):**

```
   book_poller (every 5 min)
        │
        ▼ for each registered provider, get_order_book(token) for top-N
        │  markets
        │
        ▼ inserts into book_archive.sqlite, keyed on
          (venue, market_id, token_id, snapshot_ts)
        │
        ▼ analysis side reads at-or-before via get_snapshot_at(ts)
```

**The live-game data pipeline (new; lead-lag dataset for analysis):**

```
   baseball.live daemon (every 10s)
        │
        ▼ pulls in-progress game_pks from baseball_state.games
        │
        ▼ for each, fetches MLB feed/live
        │
        ▼ extracts plays, dedups by (game_pk, play_id),
          persists to baseball_state.sqlite live_plays table
        │
        ▼ joined later with book_archive snapshots for
          lead-lag analysis (TBD)
```

### Sidecars vs the market app

The market app (`app.py`, `pages/`, `pm/venue/polymarket_client.py`, `pm/venue/kalshi_client.py`, `edges.py`, etc.) owns trading-related concerns: provider abstraction, scanners, Kelly math, portfolio, calibration, paper-trade runners.

Sidecars (`baseball/`, future `news/`, future `nba/`, …) own domain-specific intelligence engines. They run on their own schedule, maintain their own state, and expose narrow public APIs. **A sidecar does not import the market app.** The market app reaches into a sidecar via its sub-package's public surface — for baseball that's `baseball.predict_game(...)`. The single legitimate cross-boundary file is the **strategy adapter** in `edges.py` that converts sidecar predictions into Edge / Signal objects.

Canonical spec: `docs/sidecar-protocol.md`.

### What each persistent file is, and who writes it

| Path | Writer | Reader | Purpose |
|---|---|---|---|
| `data/<profile>/portfolio.json` | Sandbox Portfolio page, strategy_runner | Portfolio page, Live Portfolio reconciliation | Per-profile paper transaction log |
| `data/<profile>/forecasts.json` | Kelly page, Edges page, strategy_runner, bulk_deep_analysis | Calibration page, auto_resolve_runner | Per-profile forecast log w/ source tags |
| `data/<profile>/alerts.json` | Alerts page | Sidebar alert badge | Per-profile threshold alerts |
| `data/<profile>/settings.json` | Settings page | risk.py | Per-profile risk knobs |
| `data/shared/book_archive.sqlite` | book_poller, mlb_book_poller, kalshi_ws | Edges page slippage panel, longshot_scalper | Historical order-book snapshots (resting liquidity) |
| `data/shared/trade_archive.sqlite` | trade_poller, kalshi_ws (trade channel) | lead-lag flow analysis, longshot_scalper | Executed-trade tape (`markets/trades`), deduped on `trade_id`, with aggressor side |
| `data/shared/longshot_scalp_log.jsonl` | longshot_scalper | the user, via `tail -f` | Scalper entries + flips (sells also go to the profile's `exits.jsonl`) |
| `data/shared/llm_cache.sqlite` | llm_data, llm_matcher, llm_deep_analysis | same | Persistent LLM forecast cache |
| `data/shared/backtest_runs/*.json` | Backtest page | Backtest History page | Per-run BacktestResult dumps |
| `data/shared/baseball_state.sqlite` | baseball.runner, baseball.live | baseball.* public API, edges adapter, baseball web UI | Team ratings, pitchers, schedule, predictions, live plays |
| `data/shared/orders.log` | kalshi_orders | Orders page audit tab | JSONL audit log of every live-order attempt |
| `data/.active_profile` | Streamlit sidebar profile selector | every data module's lazy path resolution | Currently-active profile name |
| `data/shared/*.log` | every cron / daemon | the user, via `tail -f` | Process logs (rotated daily) |

---

## Module: `pm/venue/provider.py`

Market-data provider abstraction. Each concrete provider (Polymarket today; Kalshi and Polymarket-US planned) implements the `MarketProvider` Protocol and gets registered in a module-level dict. Pages and `pm/ui/shared.py` call `provider.get_provider()` for any network-touching operation; the result has a normalized DataFrame schema so the rest of the app is venue-agnostic.

```python
class MarketProvider(Protocol):
    name: str               # short identifier, e.g. "polymarket"
    display_name: str       # human-readable, e.g. "Polymarket"

    get_markets(limit, active, order) -> pd.DataFrame
    get_market(market_id) -> dict
    get_events(limit) -> pd.DataFrame
    enrich_categories_from_events(df_markets, df_events) -> pd.DataFrame
    get_price_history(token_id, days, fidelity) -> pd.DataFrame
    get_order_book(token_id) -> dict
    get_midpoint(token_id) -> float | None
    get_closed_markets(limit) -> pd.DataFrame
    get_market_calibration_data(df_closed, lead_hours, max_markets, progress_callback) -> pd.DataFrame

# Provider registry
register_provider(provider: MarketProvider) -> None
list_provider_names() -> list[str]
list_providers() -> list[MarketProvider]
get_provider(name: str | None = None) -> MarketProvider
active_provider_name() -> str
set_active_provider(name: str) -> None
```

### Where the boundary sits

**Provider-bound (network) operations** live behind `provider.get_provider()`. **Pure helpers** (DataFrame in → DataFrame out — scanners, Kelly sizing, `parse_order_book`, `order_book_stats`, etc.) stay in `pm/venue/polymarket_client.py` because their inputs are already normalized. Pages import `polymarket_client as pm` only for those helpers; anything that hits the network goes through the provider.

### Selecting the active provider

Set via the `MARKET_PROVIDER` env var (or `.env`). Defaults to `"kalshi"` (the US-legal venue; Polymarket excludes US persons). At runtime, the sidebar's **🏛️ Market venue** selector in `app.py` calls `set_active_provider()` + `st.cache_data.clear()` + `st.rerun()` so a switch takes effect on the next render. The env var seeds the initial value at boot.

### Adding a new provider (Phase 2 recipe)

1. Create `<venue>_client.py` with HTTP calls to that venue's API
2. Create a `<Venue>Provider` class in `pm/venue/provider.py` (or a separate module that imports `register_provider`) implementing the Protocol — every method must normalize the response to the documented column schema
3. Register it: `register_provider(VenueProvider())`
4. Add tests under `tests/test_<venue>_provider.py` with mocked HTTP responses
5. Update `.env.example` with any new venue-specific config vars

The pure helpers in `pm/venue/polymarket_client.py` (scanners, Kelly, parse functions) should work unchanged on the new provider's output as long as the schema normalizes correctly.

## Module: `pm/venue/kalshi_client.py`

Kalshi adapter (Phase 2 of multi-venue support). Wraps the public read-only endpoints at `api.elections.kalshi.com/trade-api/v2` and exposes a `KalshiProvider` class auto-registered into the provider registry on import.

### Key Kalshi facts (verified against the live API)

- **Pagination is cursor-based** (not offset like Polymarket). `_paginate` handles the loop.
- **Prices are already in dollars** (despite the common assumption they'd be in cents). Stored as strings like `"0.7800"`; `_to_float` handles conversion.
- **Identifier = ticker** (e.g. `KXWARMING-50`). The Protocol's `token_id` argument is the ticker on Kalshi — there's no separate token concept.
- **Series ticker = prefix before first `-`**. Required by the candlesticks URL. `_series_from_ticker` extracts it.
- **Categories live on events**, propagated via `/events?with_nested_markets=true`. `get_markets` enumerates that way so each market row arrives pre-tagged.
- **Settled markets** have `result` ∈ {`"yes"`, `"no"`, `""`}, `status == "finalized"`.
- **Candlestick `period_interval`** only supports `1`, `60`, and `1440` minutes. `_closest_period_interval` maps our generic fidelity to the closest supported.

### Order book adaptation

Kalshi returns `{"orderbook_fp": {"yes_dollars": [[price, size], ...], "no_dollars": [...]}}`. We adapt this to the Polymarket-shaped `{"bids": [...], "asks": [...]}` dict so `polymarket_client.parse_order_book` works unchanged:

- YES levels become bids (sorted ASC to match Polymarket's convention).
- NO levels become asks at the **mirrored price** (`1 - no_price`) because selling NO at `X` is mathematically equivalent to buying YES at `(1 - X)`. The size carries over.

This means scanner functions, order-book depth charts, and the `get_midpoint` fallback all work on Kalshi data with zero changes to downstream code.

### Fetch paths

Two complementary entry points cover the "what markets does the user care about?" question:

- **`get_markets`** (volume-ordered top 500 via `/events?with_nested_markets=true`) — the default. Surfaces the high-engagement markets. On Kalshi, the top of this ordering is dominated by long-dated marquee events; only ~1 in 500 closes within 30 days.
- **`get_markets_closing_within(max_days, limit, min_days)`** — hits `/markets` with `min_close_ts` / `max_close_ts` filters and sorts closing-soonest first. The only reliable way to find short-dated markets. Categories are joined from a broad `/events` scan with a keyword-inference fallback (`polymarket_client._infer_category`) when per-game / per-candidate events sit beyond the scan window.

Pages switch automatically: `shared.load_markets_closing_soon(max_days)` is used when the user sets `max_days ≤ 90` on Markets / Edges / Recommendations; the volume-ordered path runs otherwise.

### Lookup by ticker / URL

`lookup_markets_by_id(market_or_event_id)` resolves three input shapes — a market ticker (`KXKY4R-26-TMAS` → one row), an event ticker (`KXKY4R-26` → every sub-market), or a Kalshi URL (`https://kalshi.com/markets/.../kxky4r-26`). The Kelly Sizer's "🔎 Look up by ticker/URL" expander uses it to bring specific markets into scope when they aren't in any of the default fetch paths (low-volume, long-dated, niche events). Provider Protocol-level method; the Polymarket impl is a `condition_id` lookup fallback.

### `_compose_question` for multi-outcome events

Kalshi events have a shared `title` ("Who will the next Pope be?") with per-sub-market `yes_sub_title` carrying the specific YES proposition ("Pierbattista Pizzaballa"). `_compose_question` joins them with an em-dash so each row reads as a distinct binary question. Falls back to bare `title` when the sub-title is empty, the literal "yes"/"no", or duplicates the title (true binary markets).

### Authentication

The public market data feed is unauthenticated. Authenticated endpoints — balance, positions, fills, settlements, orders — live in a separate module (`kalshi_auth`) so the read-only path remains usable without credentials.

## Module: `pm/venue/kalshi_auth.py`

The authenticated HTTP layer for Kalshi. Loads `KALSHI_API_KEY_ID` + an RSA PEM file at `KALSHI_PRIVATE_KEY_PATH` and signs every request per Kalshi's spec:

```
KALSHI-ACCESS-KEY:       <key UUID>
KALSHI-ACCESS-TIMESTAMP: <epoch ms>
KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256(privkey, f"{ts}{METHOD}{path}"))
```

The signed path is the *full* request path including `/trade-api/v2`. Query strings are excluded from the signed message.

### Guardrails

- **Permission check**: refuses to load a PEM whose mode allows group/world read. Better to fail loudly than leak a key silently.
- **Owner-profile lock**: `is_configured()` returns True only when env vars are set AND the active profile equals `KALSHI_OWNER_PROFILE` (default `jeff`). Paper-trading profiles (sandbox, agentic_*) cannot make authenticated calls. CLI scripts (no profile context) fall through to allow — protection is at the UI layer where accidental cross-profile access is the actual risk.
- **Lazy key load**: the PEM is parsed on the first signed request, not at import, so the module is harmless to import on machines without credentials configured.

### Public surface

- `authed_get(suffix, params=None)` — mirrors `kalshi_client._get` semantics (retry, 429 backoff with `Retry-After`)
- `authed_post(suffix, json_body)` — single-retry write; 4xx body is logged because Kalshi rejection reasons (e.g. `insufficient_balance`) are useful to surface
- `authed_delete(suffix)` — for cancel-order
- `authed_paginate(suffix, params, items_key, max_items)` — cursor-based pagination for authed endpoints

## Module: `pm/venue/kalshi_orders.py`

The first module in the project that can spend real money. Wrapped in three layers of guard:

1. `KALSHI_LIVE_ENABLED=true` env var (separate from the auth key being configured — flipping the key on doesn't flip trading on)
2. `kalshi_auth.is_configured()` — env + PEM perms + owner profile match
3. `pre_flight_check(...)` — a pure function returning `(severity, message)` tuples; severity is `"block"` (must clear) or `"warn"` (advisory)

`pre_flight_check` evaluates: stake cap (default $50/order), daily order cap (default 10/UTC-day, counted from the audit log), price within ±10¢ of mid (fat-finger guard), drawdown circuit-breaker, valid count + price range, valid side. The UI uses this on every widget interaction so the submit button is automatically disabled whenever any block-severity issue is present.

### Order placement

`place_limit_order(*, ticker, side, action, count, limit_price_cents, client_order_id=None)` — GTC limit only, no market orders, no batches. Deterministic-prefixed `client_order_id` (Kalshi de-dupes on this field) so a network-retry of the same order won't double-fill. Every attempt is JSON-line-appended to `data/shared/orders.log` *before* the API call — audit-first means a crash mid-request still leaves a trail. Events: `submit`, `accepted`, `error`, `cancel_submit`, `cancel_accepted`, `cancel_error`.

`cancel_order(order_id)` and `get_resting_orders(ticker=None, limit=200)` complete the surface. Resting-orders fetch is read-only and only needs `kalshi_auth.is_configured()`, not the live flag.

## Module: `pm/trading/execution.py`

The seam between a strategy's *decision* and an order *fill* — so a daemon can run paper or live with no change to strategy logic. A daemon obtains a `Broker` once per cycle via `get_broker(profile)` and routes every buy/sell/position/cash query through it.

- **`PaperBroker`** (default): delegates to `portfolio.add_buy/sell` and reads `portfolio.get_positions()` — byte-for-byte the legacy behavior. `available_cash = bankroll − deployed cost basis`.
- **`LiveBroker`**: places real Kalshi limit orders via `kalshi_orders.place_limit_order` (after `pre_flight_check`), polls `get_fills` to reconcile the actual filled count + avg price, cancels any unfilled remainder, and reads **position/cash truth from the venue** (`get_authed_positions` / `get_balance`) — never the paper log.

`get_broker` returns a `LiveBroker` only when **all three** gates pass, else `PaperBroker`: (1) the profile is in `LIVE_TRADING_PROFILES` (comma-sep env; empty = all paper), (2) `kalshi_orders.is_trading_enabled()` (`KALSHI_LIVE_ENABLED` + `is_configured()`), (3) emergency-stop inactive. `place_limit_order` re-checks (2)+(3) itself — defense in depth.

Key design points (full rationale in `docs/live-execution-adapter.md`):
- **Resolution closes place no order in live** — Kalshi auto-settles resolved markets (`broker.closes_settle_on_venue`). Favorite-hold is therefore buy-only live.
- **Integer contracts**: a live buy floors to whole contracts and **resizes down to fit `LIVE_MAX_STAKE_DOLLARS`** (so a tiny cap yields a min-size order, not a pre-flight rejection); rejects only if the cap can't afford one contract. Sells (exits) are never resized.
- **Marketable exits** (`sell(marketable=True)`): crossed down by `exit_cross_cents` so a stop/scalp sell fills even on a moved book — Kalshi has **no native stop order** (verified), so a reliably-filling marketable sell *is* the stop. A limit crossed through the bid behaves like a market sell on the audited limit path (no `market` type needed).
- **Owner-profile lock is process-local**: daemons pin `POLYMARKET_ACTIVE_PROFILE` (honored by `profiles.active_profile()` before the `.active_profile` file) so `is_configured()`'s `active == owner` check resolves to the daemon's profile, not whatever the UI last selected.

Go-live tooling: `pm/trading/live_preflight.py` (read-only — verifies the read paths + owner lock against the live account, places nothing) and `go_live.sh` (runs the preflight, refuses to enable unless it passes, then upserts the `.env` toggles + restarts the service). Wired into `longshot_scalper.py`; `pm/arms/strategy_runner.py` is the next wiring target.

Capabilities added since the seam shipped:
- **Resting cash-out limit** (`place_resting_take_profit` / `cancel` / `fills_for_order`, gated by `rests_take_profit`): a live scalp rests its take-profit on the venue (zero poll latency); the daemon arms it on entry, reconciles fills as `scalp_take_profit`, and cancels it before any stop/flip/resolution. The downside stop stays polled+marketable (Kalshi has no stop type).
- **Marketable exits** (`sell(marketable=True)`): crossed by `exit_cross_cents` so a stop/scalp sell fills even on a moved book.
- **Position-scoping** (`scope_to_own_positions`, default on): `LiveBroker.get_positions`/`available_cash` return only positions *this arm* opened (its profile-pinned `position_meta`), so multiple live arms can share one account without managing each other's holds.
- **Settlement reconciler** (in `longshot_scalper`): logs positions that settled on the venue before the in-cycle resolver caught them — closes the track-record gap that otherwise silently drops fast-settled (esp. losing) bets.
- **Owner allow-list** (`kalshi_auth.authorized_profiles` = owner ∪ `LIVE_TRADING_PROFILES`): lets several live arms authenticate while paper profiles stay locked out.

Read-only ops/analysis tools alongside it: `pm/trading/live_status.py` (account readout), `pm/trading/live_gates.py` (what is armed), `pm/ops/health_digest.py`. The concluded A/B and calibration reporters (`closegame_stop_check.py`, `consensus_calibration_check.py`) and the reaction-speed research tools (`reaction_decay.py`, `reaction_capacity.py`) were retired to `attic/` on 2026-08-17. See `docs/project-status.md` for the current snapshot.

## Module: `pm/venue/polymarket_client.py`

### HTTP layer

```python
_get(url, params=None, retries=3) -> dict | list
```

Single entry point for all API calls. Uses a shared `requests.Session`. Retry policy:

- **Transient errors** (`ConnectionError`, `Timeout`, etc.) — retried up to `retries` times with exponential backoff (`1.5 ** attempt` seconds).
- **HTTP 429 (rate limited)** — retried specially. Reads the `Retry-After` header; sleeps for that many seconds (clamped to `[0.1, 30]` so a misbehaving server can't hang the UI); retries up to `retries` times. On the final attempt, the `HTTPError` propagates.
- **Other 4xx/5xx** — `HTTPError` raises immediately. These are deterministic; retrying just wastes time.

If you add a new Polymarket endpoint wrapper, call `_get` and you inherit all of the above with no per-call backoff code.

### JSON field parsing

```python
parse_json_field(val) -> list
```

The Gamma API returns `outcomes`, `outcomePrices`, and `clobTokenIds` as **JSON-encoded strings**, not native arrays. This helper handles both string and list inputs and never raises. Use it whenever you read one of those fields off a raw API response.

Inside `get_markets()` we call `parse_json_field` once and store native lists in the DataFrame — downstream code can iterate directly. **Do not store JSON strings back into the DataFrame**; that was the cause of a serious past bug where `zip(outcomes, prices)` iterated characters and silently broke Portfolio P&L.

### Category inference

The Polymarket `/markets` endpoint doesn't expose categories directly. We use a three-stage pipeline:

1. **Keyword inference inside `get_markets()`** — every market gets a baseline category from `_infer_category(question, events)` using:
   - `seriesSlug` on embedded event objects (authoritative for nba, nfl, mlb, nhl, mls, ufc, pga, etc.)
   - Keyword matching against `_CATEGORY_RULES` (ordered list, first match wins, word-boundary aware via `_kw_match`)
2. **Authoritative override via `enrich_categories_from_events()`** — after `get_markets()` returns, we call `get_events()` to fetch the `/events` endpoint, parse each event's `tags` field, and map tag labels like `"Soccer"`, `"Bitcoin"`, `"Trump"` to our standard buckets via `_TAG_TO_CATEGORY`. Markets whose `event_id` matches an event with a recognised tag get the authoritative category. Markets without a matching event keep the keyword-inferred category.
3. **Fallback to `"Other"`** — markets with neither a keyword match nor a recognised tag.

```python
_infer_category(question: str, events: list) -> str
_kw_match(keyword: str, text: str) -> bool          # word-boundary aware
_category_from_event_tags(tags) -> str | None       # tag → category mapping
```

**To add or adjust categories:**
- For keyword fallback: edit `_CATEGORY_RULES` in `pm/venue/polymarket_client.py`. Rules are checked in order; put more specific rules before broader ones.
- For API tags: edit `_TAG_TO_CATEGORY` (lower-cased keys). This dict is consulted by `_category_from_event_tags` and indirectly by `get_events()`.

### Market fetching

```python
get_markets(limit=500, active=True, order="volume24hr") -> pd.DataFrame
```

Paginates the Gamma API (hard cap: 100 per page) until `limit` rows are collected or the API returns fewer than a full page. Returns a DataFrame with these columns:

| Column | Type | Source |
|---|---|---|
| id | str | Gamma `id` |
| condition_id | str | Gamma `conditionId` |
| question | str | Gamma `question` |
| category | str | Inferred locally |
| volume | float | Gamma `volume` |
| volume_24hr | float | Gamma `volume24hr` |
| liquidity | float | Gamma `liquidity` |
| end_date | str | Gamma `endDate` |
| outcomes | **list[str]** | parsed from Gamma `outcomes` JSON string |
| outcome_prices | **list[float\|None]** | parsed from Gamma `outcomePrices` JSON string |
| active | bool | Gamma `active` |
| spread | float | Gamma `spread` |
| best_bid | float | Gamma `bestBid` |
| best_ask | float | Gamma `bestAsk` |
| last_trade_price | float | Gamma `lastTradePrice` |
| days_to_resolution | float | derived from `end_date` − now (clipped at 0) |
| event_id | str | First embedded event's `id` (`""` if no events). Used as the join key with `get_events()`. |

```python
get_market(condition_id: str) -> dict
```

Fetches full detail for a single market by `conditionId`. **Critical gotcha**: the Gamma filter parameter is `condition_ids` (plural, snake_case), not `conditionId` (camelCase). The wrong name is silently ignored and the API returns its default first market — this caused a long-standing bug where every Price History chart showed the same market. The function also verifies the returned `conditionId` matches what was asked for, returning `{}` if not.

The detail object includes:
- `clobTokenIds` (JSON string of numeric token IDs needed for price history)
- `tokens` is always `None` — do not use.
- `outcomes` and `outcomePrices` are also JSON strings here.

### Price history

```python
get_price_history(market_id: str, days=30, fidelity=60) -> pd.DataFrame
```

- `market_id`: a **numeric CLOB token ID** from `clobTokenIds`, not a `conditionId`.
- `fidelity`: candle size in minutes — 1, 5, 15, 60, or 1440.
- Always passes `startTs` (now − days × 86400). The CLOB API returns empty results without it.
- Returns columns: `timestamp` (UTC datetime), `price` (float 0–1).

**Getting token IDs:** call `get_market(condition_id)`, then parse `clobTokenIds`:
```python
detail = get_market(cid)
token_ids = parse_json_field(detail["clobTokenIds"])  # list of numeric strings
```

### Closed markets + market-calibration backfill (Tier 3a)

```python
get_closed_markets(limit=100, order="closedTime") -> pd.DataFrame
# Adds a `resolved_yes` bool column (None for unclean resolutions) and
# `closed_time_dt` (parsed datetime). Same pagination cap as get_markets.

market_price_at_offset_before_close(token_id, closed_at, lead_hours,
    fidelity=60, window_days=90) -> float | None
# Looks up the YES price at approximately `lead_hours` before close.

get_market_calibration_data(df_closed, lead_hours=24, max_markets=100,
    progress_callback=None) -> pd.DataFrame
# Returns one row per (closed) market with columns predicted_prob,
# resolved_yes, actual (1.0/0.0), brier_term, lead_hours. Skips markets
# without sufficient history or clean resolution.
```

Used by the Market Baseline section on the Calibration page to compute the *market's* Brier score over historical resolutions. Useful as a reference for the user's own Brier.

### Events + category enrichment

```python
get_events(limit=500, active=True, order="volume24hr") -> pd.DataFrame
enrich_categories_from_events(df_markets, df_events) -> pd.DataFrame
```

`get_events()` paginates `/events` (same hard cap of 100 per page as `/markets`). For each event, it parses `tags` (a JSON-string list of tag dicts) and computes a `category` via `_category_from_event_tags`. The returned DataFrame columns are `id, title, slug, category, volume_24hr, active`.

`enrich_categories_from_events()` joins markets to events on `event_id`. Where the event has a non-empty `category`, it overrides the keyword-inferred category on the market. Where it doesn't (or the event is missing entirely), the keyword category is preserved. A new `event_title` column is always added.

`app.py` exposes a `@st.cache_data(ttl=300)` `load_events()` (longer cache than markets — tags rarely change) and calls `pm.enrich_categories_from_events()` inside `load_markets()` so every page that reads markets automatically gets the enriched categories.

### Order book

```python
get_order_book(token_id: str) -> dict          # raw API response
parse_order_book(book: dict) -> pd.DataFrame   # tidy normalized form
order_book_stats(df_book: pd.DataFrame) -> dict
```

**Key API quirk**: Polymarket's `/book` returns bids in **ascending** price order and asks in **descending** price order — i.e., the best (top-of-book) price is at the END of each list. `parse_order_book()` reverses both lists so best price comes first per side, then computes `cumulative_size` outward from best.

`parse_order_book()` returns a DataFrame with columns `side` (`"bid"` or `"ask"`), `price` (float), `size` (float), `cumulative_size` (float).

`order_book_stats()` returns `{best_bid, best_ask, spread, total_bid_size, total_ask_size, bid_levels, ask_levels}`.

Both functions are doctested.

### Kelly sizing

```python
kelly_sizing(market_price: float, your_probability: float) -> dict
```

Pure math. Validates inputs are in `[0, 1]` (raises `ValueError`). Returns a fully JSON-serializable dict (no callables):
- `edge`, `direction` (`"YES"` / `"NO"`), `kelly_fraction`, `half_kelly_fraction`
- `expected_value` per $1 wagered

For dollar amounts, callers do `bankroll * result["kelly_fraction"]` directly. The previous lambdas (`kelly_dollars(bankroll)`) were removed because they broke JSON serialization, pickling, and `repr`.

Doctested. See [Glossary — Kelly Criterion](glossary.md#kelly-criterion--bet-sizing) for the formula.

### Scanner functions

All three take a pre-loaded markets DataFrame and return a filtered/ranked DataFrame.

```python
scan_narrow_spreads(df, max_spread=0.02, min_liquidity=500) -> pd.DataFrame
```
Filters on `spread` and `liquidity`. Sorts by spread ascending.

```python
scan_price_divergence(df, min_divergence=0.05, min_liquidity=500) -> pd.DataFrame
```
Computes `|last_trade_price − mid_price|` where `mid_price` is `outcome_prices[0]` (YES token). **Restricted to binary markets only** — the Gamma API's `lastTradePrice` is a single per-market value representing the YES outcome's last trade, so comparing it to `outcome_prices[0]` only makes sense when that's a "YES." Multi-outcome markets are filtered out to avoid bogus matches.

```python
scan_multi_outcome_gaps(df, min_deviation=0.03, min_liquidity=200) -> pd.DataFrame
```
Filters to markets with 3+ outcomes. Computes `sum(prices) − 1.0`.

```python
scan_cross_market_arb(df, min_markets=3, min_total_liquidity=1000,
                       plausible_band=(0.7, 1.3), min_deviation=0.02) -> pd.DataFrame
```
Tier 2 of the recommendations roadmap. Groups binary markets by `event_id` and sums the YES prices across each event. If the sub-markets are mutually exclusive and exhaustive (e.g. "FIFA World Cup Winner" with one market per team) the sum should equal 1.0; deviations indicate arbitrage candidates.

The `plausible_band` argument filters out events whose YES-sum is far from 1.0 — those almost certainly aren't mutually exclusive (e.g. an event with markets like "inflation > 5%?" and "unemployment > 4%?" has no constraint requiring sum = 1). The user still has to read sub-market questions to confirm exclusivity; the band is only a noise filter.

Output columns: `event_id`, `event_title`, `category`, `n_markets`, `sum_yes_prices`, `deviation`, `implied_edge_pct`, `total_liquidity`, `total_volume_24hr`, `markets_sample`. Sorted by `|deviation|` descending.

---

## Module: `pm/ledger/portfolio.py`

Append-only **transaction log** with derived open positions and realized P&L, plus a periodic **snapshots** log of portfolio value over time. The `portfolio.json` file schema (version 2):

```json
{
  "version": 2,
  "transactions": [
    {
      "id": "uuid4",
      "timestamp": "2026-05-17T20:00:00+00:00",
      "type": "buy" | "sell",
      "question": "Will X happen?",
      "condition_id": "0xabc...",
      "outcome": "Yes",
      "shares": 100.0,
      "price": 0.42
    }
  ],
  "snapshots": [
    {
      "timestamp": "2026-05-17T20:00:00+00:00",
      "cost_basis": 1500.0,
      "current_value": 1820.0,
      "unrealized_pnl": 320.0,
      "realized_pnl": 142.5,
      "open_positions": 7
    }
  ]
}
```

Old v1 files (`{"positions": [...]}`) are migrated transparently on first read — each position becomes a single buy transaction. Pre-snapshots v2 files have an empty `snapshots` key added on load (forward-compat).

```python
# Recording
add_buy(question, condition_id, outcome, shares, price) -> dict
add_sell(question, condition_id, outcome, shares, price) -> dict
record_transaction(type, ...) -> dict
delete_transaction(index: int) -> bool

# Backward-compat aliases
add_position(...)         # → add_buy
remove_position(index)    # → delete_transaction

# Derived views
get_transactions() -> pd.DataFrame
get_positions() -> pd.DataFrame
get_realized_pnl() -> pd.DataFrame
enrich_with_current_prices(positions_df, markets_df) -> pd.DataFrame

# Snapshots
add_snapshot(cost_basis, current_value, unrealized_pnl, realized_pnl,
             open_positions=0) -> dict
get_snapshots() -> pd.DataFrame
maybe_snapshot(cost_basis, current_value, unrealized_pnl, realized_pnl,
               open_positions=0, min_interval_hours=1.0) -> bool
```

### Snapshot capture

`maybe_snapshot()` is the auto-recording entry point — call it once per Portfolio page load with the computed totals. It records a snapshot if (a) the portfolio has something to track (any positive cost basis, realized P&L, or open positions), and (b) the most recent snapshot is older than `min_interval_hours`. Returns `True` if it wrote one, `False` otherwise.

`get_snapshots()` returns a DataFrame with derived columns `net_value` (= `current_value + realized_pnl`) and `total_pnl` (= `unrealized_pnl + realized_pnl`) alongside the stored fields.

### Average-cost method

Positions and realized P&L are computed by replaying the transaction log in order:
- Buys add to shares and total cost.
- Sells take their proportional share of the running average cost basis, contributing `qty × (sell_price − avg_price)` to realized P&L.
- Cost basis is reduced proportionally on sell, so `avg_price` stays unchanged through a sale.

The replay logic is in `_replay()`. Both `get_positions()` and `get_realized_pnl()` call it.

### Validation

`add_sell` checks that you hold at least the quantity being sold. Invalid `tx_type`, `shares ≤ 0`, or `price` outside `[0, 1]` raise `ValueError`.

---

## Module: `pm/ledger/calibration.py`

Append-only **forecast log** plus scoring. The `forecasts.json` schema:

```json
{
  "version": 1,
  "forecasts": [
    {
      "id": "uuid4",
      "timestamp": "ISO",
      "question": "...",
      "condition_id": "0x...",
      "outcome": "Yes",
      "market_price": 0.30,
      "your_probability": 0.50,
      "resolution": null | true | false,
      "resolved_at": null | "ISO"
    }
  ]
}
```

```python
add_forecast(question, condition_id, outcome, market_price, your_probability) -> dict
record_resolution(forecast_id: str, outcome_yes: bool) -> bool
delete_forecast(forecast_id: str) -> bool
get_forecasts() -> pd.DataFrame
brier_score(df) -> float | None
calibration_bins(df, n_bins=10) -> pd.DataFrame
summary_stats(df) -> dict
```

### Auto-resolution

`auto_resolve_pending(closed_lookup)` is the loop-closer for Tier-4 sources. It takes `{condition_id: resolved_yes}` and fills in the `resolution` field on every matching *pending* forecast (already-resolved records are never touched). Pure function, no provider import — venue-agnostic by design.

The Calibration page builds the lookup by walking `mp.list_providers()` and unioning each provider's `get_closed_markets(limit=200)`. Provider errors are swallowed individually so one venue being down doesn't block auto-resolution for the others. The lookup is cached for 10 minutes via `@st.cache_data` because that fetch is the slowest call in the page.

The outcome mapping: a forecast resolves correct iff the forecast's `outcome` matches what happened. So `outcome="Yes"` + `resolved_yes=True` → `resolution=True`; `outcome="No"` + `resolved_yes=False` → also `resolution=True`. Both flavours of "I predicted the wrong direction" → `resolution=False`. Tested four ways (Yes/Yes, Yes/No, No/Yes, No/No).

### Scoring math

- **Brier score**: `mean((your_probability − actual)^2)` over resolved forecasts. `actual` is 1 if the resolution matched the forecaster's chosen outcome (YES forecast & resolution=True, or NO forecast & resolution=False), else 0. Range `[0, 1]`. Doctested.
- **Calibration bins**: groups resolved forecasts into 10 probability bins by default; reports `n`, `mean_predicted`, and `observed_rate` per non-empty bin.

---

## Module: `pm/ledger/risk.py`

Tier 1 of the recommendations roadmap (see `docs/recommendations-design.md`). Provides:

```python
@dataclass
class Settings:                              # JSON-persisted user thresholds
    bankroll, max_kelly_fraction,
    max_market_position_pct,
    max_category_exposure_pct,
    drawdown_pause_pct

load_settings() -> Settings                  # tolerant: invalid JSON → defaults
save_settings(s: Settings) -> None

# Pure functions (all doctested)
calibration_adjusted_kelly_multiplier(brier) -> (multiplier, explanation)
# brier → (1.0, 0.5, 0.25, 0.0) per the Brier bands; None → 0.5

current_drawdown(df_snaps) -> {peak, current, amount, pct, in_drawdown}
should_pause_for_drawdown(drawdown, threshold_pct) -> bool

category_exposure_pct(df_pos) -> {category: pct_of_cost_basis}
concentrated_categories(exposure, threshold_pct) -> [(cat, pct), ...]

position_exceeds_limit(cost_dollars, bankroll, max_pct) -> bool
```

**Calibration-adjusted Kelly logic.** Brier bands and their Kelly multipliers are defined at the top of `pm/ledger/risk.py` (`_BRIER_FULL_KELLY`, `_BRIER_HALF_KELLY`, `_BRIER_QUARTER_KELLY`). The Kelly Sizer page calls `risk.calibration_adjusted_kelly_multiplier(category_brier)` and multiplies the raw Kelly fraction, then caps at `settings.max_kelly_fraction`.

**`brier_score_by_category`** in `pm/ledger/calibration.py` joins forecasts to markets by `condition_id`, groups by category, and returns `{category: brier | None}` — bins below `_MIN_RESOLVED_PER_CATEGORY` (5) return None so the multiplier falls back to the no-data default.

**Storage**: `settings.json` is JSON-backed and env-overridable via `POLYMARKET_SETTINGS_FILE`. Gitignored. Schema is forward-compatible — unknown keys are ignored on load.

## Module: `pm/research/backtest.py`

Tier 3b of the recommendations roadmap — the gate every alpha strategy must clear before shipping.

### Why the dataset / engine split

`build_dataset` is the slow part — one HTTP round-trip per market to fetch its price history. The Streamlit page caches it for 6 hours keyed on `(venue, n_markets, lead_hours, history_days)`. Once the dataset is in memory, `run_backtest` is pure Python and runs in milliseconds, so users can tune a strategy's own parameters without paying for another full refetch.

### Lookahead-bias prevention

Structural, not advisory:

- `MarketSnapshot.prices` is **truncated to rows strictly before `as_of`** by `_truncate_prices_before`. A candle at exactly `as_of` is excluded — the strategy never sees a price it could only have known by being lucky with timing.
- The `Resolution` (`resolved_yes`, `closed_time`) is held by the engine in a parallel object the strategy never receives. Strategies can't reach the outcome via the snapshot.
- Tested: `tests/test_backtest.py::TestBuildDataset::test_snapshot_prices_strictly_before_as_of` and `test_resolution_carries_outcome_not_in_snapshot`.

### PnL accounting

Reuses `polymarket_client.kelly_sizing` so the model matches the live Kelly Sizer exactly. Per trade:

- `entry_price = market_yes_price` for YES, `1 − market_yes_price` for NO
- `stake = bankroll × half_kelly_fraction × kelly_multiplier` (default 0.5)
- Win: `pnl = stake × (1 − entry_price) / entry_price`
- Loss: `pnl = −stake`

If the strategy's `direction` disagrees with what Kelly's natural direction would be (i.e., the strategy says YES on a market where its probability < market_price), the trade is **skipped** — there's no edge in that direction, so the engine refuses to bet against the implied edge. Strategies that want flat sizing regardless (the "always-YES dumb baseline") set `Signal.fraction_override`, which bypasses Kelly entirely.

### Strategy registry

```python
STRATEGIES: dict[str, tuple[str, Strategy]] = {
    "market_consensus": ("Follow the favorite (baseline)", strategy_market_consensus),
    "always_yes_flat":  ("Always YES, flat 1% (dumb baseline)", strategy_always_yes_flat),
    "momentum":         ("Momentum (+5% in 72h)", strategy_momentum),
    "mean_reversion":   ("Mean reversion (>85% or <15%)", strategy_mean_reversion),
}
```

The page introspects each strategy's keyword-only parameters via `inspect.signature` and exposes them as sliders/number-inputs automatically — adding a new strategy with sensible default kwargs gets a UI for free.

### The market-baseline gate

`market_baseline_brier(dataset)` computes the Brier of "just predict `latest_price`" on the same snapshots. The page shows it alongside the strategy's Brier with a pass/fail framing. This is the **explicit shippability test** from `docs/recommendations-design.md`: a strategy whose Brier doesn't beat the market baseline on its category is not edge, it's noise + transaction costs.

### Drawdown math

`_max_drawdown` anchors the equity curve at $0 (prepends a zero before `cummax`) so an early losing streak doesn't get treated as the "running peak" by accident. Drawdown is reported in dollars (negative or zero).

### Transaction costs

`run_backtest` accepts `half_spread` (absolute price units per side, default 0.005) and `fee_pct` (fraction of stake, default 0). The half-spread is the meaningful one for the source-graduation gate:

- **Effective entry price** for a YES trade is `mid + half_spread`; for a NO trade it's `(1 − mid) + half_spread`. Real fills are at the ask, not the mid — this captures that exactly.
- **Kelly is computed inline against the effective price**, not the mid (we no longer call `polymarket_client.kelly_sizing` from the engine). So if `your_probability − effective_price ≤ 0`, the Kelly fraction is 0 and the trade is skipped. **The "spread killed the alpha" case is structurally handled** — no extra direction-check needed.
- **Payoff math** uses the effective entry price too. A win pays `stake × (1 − effective_price) / effective_price`; a loss is `−stake`. The `BacktestResult` reports `implied_spread_cost` (the difference between ideal-mid PnL and effective-fill PnL across all trades, surfaced for visibility — it's already absorbed into the per-trade `pnl` column, not double-counted).

The fee is the simpler one: `total_fees = sum(stake × fee_pct)` deducted from `pnl` per trade.

Set both to 0 to recover the original "ideal mid-fill" behaviour. Pre-transaction-cost tests do exactly this so they continue to pin the original PnL math.

Live cost-sensitivity check on 10 closed Polymarket markets (the `market_consensus` baseline):

| half_spread | fee_pct | trades | ROI | PnL |
|---:|---:|---:|---:|---:|
| 0.000 | 0.000 | 10 | +2.6% | +$40.77 |
| 0.005 | 0.000 | 10 | +2.1% | +$29.92 |
| 0.010 | 0.000 | 10 | +1.5% | +$19.67 |
| 0.020 | 0.000 | **2** | +0.1% | +$1.00 |
| 0.020 | 0.010 | 2 | **−0.9%** | −$9.00 |

At 2¢ half-spread, 8 of 10 trades have their edge consumed — Kelly fraction zero, no fill. A 1% fee on top flips the survivors negative.

### Walk-forward (v2)

`run_backtest_walk_forward` is the multi-decision-per-market variant. Same strategy interface, same `BacktestResult` shape; the engine walks each market's price history at `eval_step_hours` intervals, calls the strategy at each step with prices truncated strictly before that step (the same lookahead-safety guarantee `build_dataset` provides), tracks one in-or-out position per market, marks to market on signal-flip exits, and closes any open position at $1/$0 on resolution.

Position state is local to each market — there's no portfolio-level bankroll constraint across simultaneous open positions in v2. Each trade's stake is sized against the configured `bankroll` independently. This matches single-shot's accounting and keeps the comparison apples-to-apples.

Trade rows carry extra columns the single-shot path leaves out: `entry_time`, `exit_time`, `exit_reason` (`"signal_flip"` or `"resolution"`), `holding_period_days`, `gross_pnl`, `spread_cost`, `fee`. The Backtest page's existing rendering treats these as optional — they just appear in the trade-log table when present.

**When walk-forward matters**: strategies that respond to intra-market price moves. Live data: `momentum` produces 1 signal-flip on 18 Kalshi closed markets — the intra-market direction change is invisible to single-shot. `market_consensus` and `mean_reversion` don't flip and the two engines produce identical trade counts for them.

**When it doesn't**: strategies that fire once and never reconsider (e.g. an LLM forecast made at a fixed lead time). For those, walk-forward is single-shot with extra HTTP calls — use single-shot.

### Portfolio-level bankroll constraint

Both engines thread a `portfolio_constrained: bool = True` parameter that tracks available cash through the run and caps each new stake at `min(Kelly-desired, cash)`. The Kelly fraction is still computed against the *configured bankroll* so fractions stay comparable to live Kelly usage; the constraint bites at the dollar level. Resolved positions return proceeds to the cash pool before the next decision sees it.

**Single-shot path**: markets are sorted by `as_of` so the cash trajectory reflects real time-ordering. Before each new open, the engine settles any pending positions whose resolution time has passed.

**Walk-forward path**: a new `_walk_forward_constrained` helper builds a single time-ordered event queue across every market — eval points and resolution events interleaved — and walks it with shared cash state. The unconstrained path still calls the original per-market loop in `_walk_forward_one_market`; that's exposed via `portfolio_constrained=False` as a "pure strategy comparison" baseline that ignores capital limits.

**Live demonstration (18 Kalshi closed markets, 50%-per-market strategy)**:

| Mode | Trades | Skipped for cash | Min cash | PnL |
|---:|---:|---:|---:|---:|
| Constrained (default) | 2 | 16 | $0 | −$1,000 |
| Unconstrained | 18 | 0 | $1,000 | −$8,497 |

Unconstrained implicitly stakes $9,000 on a $1,000 bankroll — 9× leverage that no live single-account execution could achieve. The fix is a real correctness improvement for any strategy whose positions overlap in time.

### User-defined strategy plugins

`backtest.register_strategy(key, display_name)` is a decorator that adds a function to the `STRATEGIES` registry — designed for `user_strategies/*.py` files so you can extend the strategy set without editing `pm/research/backtest.py`. The decorator returns the original function unchanged, so the strategy is still directly callable from unit tests.

`discover_user_strategies()` walks the `user_strategies/` directory at app startup, importing every non-underscore-prefixed `.py` module. The `@register_strategy` side effects register the strategies; a plugin that raises at import time is logged and skipped (a broken file doesn't crash the rest of the framework). Discovery is idempotent — Python's import cache makes re-runs a no-op.

**Trust model**: plugins run in the same Python process as the rest of the app; there's no sandbox. Don't drop in code you haven't read.

**Override semantics**: if a plugin registers a key that already exists (e.g. `momentum`), it overrides the built-in with a warning logged to the user. Intentional — you might want to shadow a built-in with your own variant — but loud enough to catch accidental collisions.

**Wiring at app startup**: `app.py` calls `_bt.discover_user_strategies()` immediately after `profiles.initialize_profiles()`. It also imports `edges` for its strategy-registration side effects, so Tier-4 strategies are available in the Backtest dropdown before any page renders.

### What's not here yet

- **Order-book-depth slippage on the backtest gate**. The half-spread model assumes you can fill any size at the ask. A large order walks the book and pays progressively worse prices. Live slippage (against current depth) is in `pm/venue/slippage.py`; historical-book backtesting waits on `book_archive` to accumulate enough snapshots.

### Logging LLM (or any edge) forecasts to the Calibration page

`edges.log_edges_to_calibration(list[Edge]) -> int` is the bridge between Tier 4's edge surfacing and the existing calibration system. Each `Edge` becomes a forecast record with `your_probability = edge.model_implied` and `market_price = edge.market_yes_price`. The `source` field gets the canonical tag (`"llm:<model>"`, `"crypto_spot"`, …) so the Calibration page can compute per-source Brier without mixing them with hand-entered forecasts.

The schema change is additive — legacy `forecasts.json` records without a `source` field are read as `source="user"` at load time, no on-disk migration required.

`_source_tag(edge)` in `edges.py` derives the source string. For `llm` edges the model name is parsed out of the note (`f"{model}: {reasoning}"`); the parser splits on the literal `": "` separator (colon-space) because model names can themselves contain colons (e.g. `llama3.1:latest`). Keep the format string in `find_llm_edges` in sync with this assumption.

---

## Module: `pm/research/backtest_history.py`

Persistent backtest-run history. One JSON file per run in `data/shared/backtest_runs/` named `{epoch_ms}_{strategy_key}.json`. Auto-saved by the 🧪 Backtest page on every run; browsable on the 📜 Backtest History page. Path env override: `BACKTEST_HISTORY_DIR`.

**Why JSON-per-file, not SQLite**: a `BacktestResult` is a flat record with two DataFrames (`trades`, `equity_curve`) and a couple of dict / list fields. No relational queries are needed — just "list recent" and "load one." One JSON per run lets the user `ls` / `rm` individual files without tooling. Human-readable, no pickle-side-channel risk if a file is ever moved between machines.

**Schema migration**: `_dict_to_result(payload)` reconstructs a `BacktestResult` from a saved dict. Fields missing from the payload (older saves from before a `BacktestResult` field was added) fall back to the dataclass's own default — or `None` if the field has no default — rather than crashing. Adding a new field with a default is therefore backwards-compatible; adding a required field with no default will surface as a load error and the affected runs need re-running.

**DataFrame round-trip**: datetime columns in `trades` (`as_of`, `closed_time`, `entry_time`, `exit_time`) get ISO-stringified on save and `pd.to_datetime`-parsed on load. New datetime columns must be added to `_DATETIME_COLS` for the parse-back step.

---

## Module: `pm/feeds/crypto_data.py`

Coinbase Exchange (formerly Coinbase Pro) public market-data client + GBM probability models. Tier 4's only data source so far.

### Why Coinbase Exchange, not Binance

`api.binance.com` returns HTTP 451 to US IPs across the *entire* market-data subdomain, not just trading. Coinbase Exchange's `/products/<pair>/ticker` and `/products/<pair>/candles` are public, US-accessible, and cover every USD pair we care about. Binance.US exists but supports a much smaller symbol set.

### Pagination

The `/candles` endpoint caps each response at 300 rows. `get_klines` walks backward in chunks of 300 days when asked for longer windows, deduping by timestamp. Coinbase returns newest-first per chunk; the final DataFrame is sorted ascending.

### Models

Two closed-form GBM probabilities, both with `r=q=0` (rates negligible for our horizons):

- **Terminal**: `lognormal_above` / `lognormal_below` — `P(S_T ≷ strike)`. Use for "Will X close above $Y on DATE?" markets.
- **One-touch barrier**: `barrier_hit_above` / `barrier_hit_below` — `P(max/min S_t ≷ strike for t ∈ [0, T])`. Derived from the reflection principle for Brownian motion with drift `−σ²/2` on the log-price. Use for "Will X reach $Y by DATE?" / "Will X dip to $Y?" markets, which is what most crypto-threshold markets actually settle on.

The barrier probability dominates the terminal one in the same direction — a path can wander through the strike and back. `edges.py` dispatches between them based on the parsed verb (see below).

---

## Module: `edges/`

Tier 4 external-data edge framework. First source: crypto spot.

> **Split into a package on 2026-08-24 (P3/R-24).** `edges.py` had grown to
> 2,175 lines and was imported by nearly everything. It is now `edges/`:
> `__init__.py` (source registry, `find_all_edges`, `log_edges_to_calibration`
> and re-exports of every public name), `_base.py` (the `Edge` dataclass,
> `_severity_for_gap`, `edges_to_dataframe`, `_row_to_snapshot`), and one
> module per source. **The public API did not change** — `import edges`,
> `edges.Edge`, `edges.find_all_edges`, `edges.sports_consensus_for_ticker`
> all behave exactly as before, and importing `edges` still triggers the
> Tier-3b strategy registration side effect. References to "`edges.py`"
> elsewhere in this guide mean the package.
>
> **One behaviour change, and it only affects tests:** patch an internal at
> its real home (`edges.baseball._baseball_games_for_date`), not
> `edges._baseball_games_for_date` — the latter is now this package's
> re-export, and rebinding it does not affect the caller inside the submodule.

### Why colocate parser + strategy + finder

`strategy_crypto_spot_lognormal` runs inside the Tier 3b backtest engine; `find_crypto_edges` runs against current markets for the 🔭 Edges page. Both rely on the same parser and the same dispatch into `crypto_data` model functions. Keeping them in one module means there's exactly one place to fix when parser rules or modelling assumptions change.

### Aggregating across sources

`find_all_edges(df_markets, *, sources=None, min_gap=0.10, min_days_to_expiry=1.0, per_source_kwargs=None, on_source_start=None, on_source_done=None) -> dict[str, list[Edge]]` runs every registered source (or the named subset) against the same DataFrame and returns one dict keyed by source name. Three properties worth keeping in mind when extending:

- **Source isolation**: each finder call is in a try/except. If one source raises, its slot becomes `[]` and the aggregator keeps going. The 🎯 Recommendations page depends on this — one broken finder must not break the whole scan.
- **Signature-aware kwargs**: the aggregator inspects each finder's signature and only passes kwargs the finder accepts. Crypto's `vol_window_days`, Manifold's `min_similarity`, and LLM's `model` differ across sources; one global kwarg set without filtering would crash three out of five.
- **Progress callbacks**: `on_source_start(key)` and `on_source_done(key, n_edges)` are optional callables the UI uses for progress feedback without coupling itself to the finder set.

`edges_severity(edge) -> float` returns `|gap|` as the unified ranking score. The Recommendations page sorts the combined edge list with this.

### Question parser

Regex-based; permissive enough to catch real Polymarket / Kalshi phrasing without inventing structure. Key gotchas baked into the implementation:

- **No IGNORECASE.** The caller lowercases the input first; the `[kmb]` magnitude character class is lowercase-only. With IGNORECASE on, `[KMB]` matched the leading `b` of "by" in "$50,000 by July" and parsed the strike as 50 trillion. The trailing `\b` further guards against matching letters that are part of an adjacent word.
- **Verb-aware direction.** "dip to $X" / "fall to $X" / "drop to $X" all mean *barrier-from-above*, not bare-"to"-equals-above. `_DOWN_TO_PATTERN` catches these and flips the direction.
- **Verb-aware model dispatch.** `_TERMINAL_PATTERN` matches "be above" / "close above" / "finish below" / etc. before the strike — these are close-on-date questions and use the terminal lognormal model. Everything else defaults to one-touch barrier.

### Short-dated suppression

`min_days_to_expiry=1.0` (default) drops candidates whose deadline is less than a day out. The model only sees future path; for markets near expiry the "already happened" component of the YES probability dominates, and the model produces phantom edges on markets that are essentially decided.

### Registration with the Tier 3b backtester

At module-import time, `bt.STRATEGIES["crypto_spot_lognormal"]` is set so the Backtest page exposes this source as a selectable strategy. The Brier-vs-market-baseline gate built into `run_backtest` is the rule for whether this source is allowed to influence real Kelly sizing — until it clears, the 🔭 Edges page surfaces it as advisory only.

### Adding a new source

1. New module if it has its own HTTP client (e.g. `polling_data.py`), or extend `edges.py` if it's a small wrapper.
2. Define a `find_<source>_edges(df_markets, **kwargs) -> list[Edge]` function.
3. Add to the `SOURCES` registry in `edges.py`.
4. If you want it gateable, also write a `Strategy` callable and register it in `bt.STRATEGIES`.
5. Run a backtest against the closed-markets dataset. If the source's Brier doesn't beat the market baseline on its category, the source ships as advisory; do not auto-size off it.

---

## Module: `pm/feeds/sports_data.py`

Tier 4's third source: ESPN's public site API. Surfaces consensus betting-market win probabilities for scheduled games so we can compare them to Polymarket / Kalshi YES prices.

### Endpoints used

- `GET /apis/site/v2/sports/{sport}/{league}/scoreboard` — current and upcoming events for a league.
- `GET /apis/site/v2/sports/{sport}/{league}/summary?event={id}` — full event detail. The `pickcenter` array contains consensus betting-market odds (DraftKings is the default provider on most events).

Both are unauthenticated, CORS-enabled (espn.com itself uses them), and US-accessible.

### Why moneyline, not `winPercentage`

ESPN's `pickcenter[*].homeTeamOdds.winPercentage` field is *almost always null* in practice. The reliably-populated field is `moneyLine` (American odds). We convert and de-vig:

```
moneyline_to_prob(-200) = 0.6667     # implied probability with vig
moneyline_to_prob(+170) = 0.3704     # both sides sum to >100% (the vig)
devig(0.6667, 0.3704)   = (0.643, 0.357)   # renormalize to sum=1
```

The de-vigged numbers are what we treat as the consensus fair probability.

### Live-only vs. backtest

ESPN's `/scoreboard` is a *current* endpoint — it returns games happening now, not games that were happening at some past timestamp. No paid-data-free way to query historical odds, so `strategy_sports_winprob` returns None for every closed market in a backtest run. The Tier 3b gate is effectively unusable for this source until we add an archival data source (open follow-up). Live edges from `find_sports_edges` still work and the Edges-page workflow is fully functional.

### Derivative-market filter

Real Polymarket title that produced a false positive during live testing: **"Canadiens vs. Sabres: O/U 6.5"** — that's an over/under total-goals market, not a moneyline matchup. The team-name matcher would happily match both teams, the moneyline-derived YES probability has nothing to do with the actual market, and we get a phantom 12pp edge.

`_DERIVATIVE_KEYWORDS` in `edges.py` is the regex that rejects these. Patterns covered: `O/U`, `over`/`under`, `spread`, `prop`, `total points`, `first to score`, `win by`, `cover`, `handicap`, `alt line`, `moneyline` (when it's an explicit prop rather than the default question shape). If the question contains any of these, `_match_event` returns None before the team-alias scan even runs.

### YES-side convention

For a question like "Lakers vs. Heat" we treat the *first team mentioned* as the YES side. That matches Polymarket's binary-sports-market convention but isn't universal. When you see a sports edge that looks wildly wrong, check the question text — if the platform uses a different YES/NO convention this is the most likely culprit.

---

## Module: `pm/feeds/macro_data.py`

Tier 4's fifth source: macroeconomic data from FRED. Backs the `macro` edge source and the `macro_threshold` backtest strategy.

### Why the CSV endpoint, not the documented JSON API

`api.stlouisfed.org/fred/` requires an API key (free, but gated behind account creation and a `?api_key=` query param on every call). The `fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>` endpoint is the one FRED uses to embed charts on third-party sites — fully public, no auth, full historical archives back to series inception. We get the same observation values via plain CSV without making the user provision a token first.

### Supported series in v1

```python
SERIES = {
    "unemployment":  SeriesMeta("UNRATE",   "monthly", "Unemployment rate (%)"),
    "10y_treasury":  SeriesMeta("DGS10",    "daily",   "10-year Treasury yield (%)"),
    "2y_treasury":   SeriesMeta("DGS2",     "daily",   "2-year Treasury yield (%)"),
    "fed_funds":     SeriesMeta("FEDFUNDS", "monthly", "Federal funds effective rate (%)"),
}
```

The `cadence` field drives volatility annualization (`×√12` for monthly, `×√252` for daily).

### Deliberately skipped in v1

- **CPI YoY** — FRED publishes `CPIAUCSL` as an index level, not a year-over-year percent. The parser would have to compute `(level / level_12_months_ago - 1) × 100` before the threshold makes sense. Worth doing as a follow-up.
- **GDP growth** — quarterly cadence, annual growth conventions, multiple revisions per release. Needs its own modeling treatment.
- **Fed-decision / "Will N rate cuts happen?" markets** — these are about *count-of-events* tied to FOMC meetings, not threshold-on-level. The right reference value is Fed funds futures (CME FedWatch), which is gated behind paid feeds. The parser deliberately doesn't match these so we don't fabricate a wrong probability from a model that can't handle them.

### Normal-on-level, not lognormal

Crypto uses lognormal (multiplicative; prices can't go negative). Macro uses **normal-on-level** (additive; rates and yields can be negative in theory and are naturally measured in differences, not ratios). The model:

```
P(level_at_T ≥ threshold) = 1 − N((threshold − level − drift·T) / (σ·√T))
```

where σ is annualized realized vol of *level changes* (not log returns), T is in years. This is wrong in detail — rates aren't Brownian motion; they have policy steps and mean-reversion the model ignores. It's right in order-of-magnitude for short horizons where the trend term dominates. The docstring on `normal_above` repeats this caveat.

### Failure mode observed live

The active Polymarket macro markets at the time of shipping were all *count-of-cuts* questions ("Will 7 Fed rate cuts happen in 2026?"). The v1 parser correctly returns None for these — the matching regex looks for "above/below/exceed X%" not "N cuts". This is the right behavior: better to flag 0 edges than to force a wrong-model fit. Documented in the module dev-guide section because the next contributor will hit the same shape and might be tempted to add a hacky match.

### As-of lookup for honest backtests

`strategy_macro_threshold` runs through the backtest engine with `snap.as_of` = the historical evaluation timestamp. To keep the gate honest, the model must see only data published before that timestamp — not today's full FRED history.

Two backtest-mode functions handle this:

- `value_at(series_id, as_of)` returns the most recent observation at-or-before `as_of` (or `None` if all observations are after it).
- `realized_volatility_at(series_key, as_of, *, window=24)` computes vol over the same pre-`as_of` window.

`_macro_model_prob(parsed, *, as_of=None)` dispatches: `None` keeps the live path (`latest_value` + `realized_volatility_for`) for the Edges page; an explicit `as_of` routes to the historical versions for the strategy. `find_macro_edges` doesn't pass `as_of` → live path; `strategy_macro_threshold` passes `snap.as_of` → historical path.

**Caveat the docstring on `value_at` calls out**: FRED's CSV exposes `observation_date` (the date the data point applies to) but not `publication_date` (when the value was actually released). For UNRATE/FEDFUNDS the publication lag is ~5–7 days; for DGS2/DGS10 it's same-day. Using `observation_date <= as_of` leaks at most one publication cycle of forward information. Fine for order-of-magnitude backtests; not strictly leak-free. Closing this gap would require a separate ALFRED-style "vintages" data feed which the free fredgraph CSV doesn't expose.

---

## Module: `pm/feeds/llm_cache.py`

Persistent K/V cache backing the LLM wrappers. SQLite at `data/shared/llm_cache.sqlite` (override via `LLM_CACHE_PATH`).

### Why persistence matters

LLM inference is the slowest call in the app (0.5–1.5s per call on 7-13B models; several seconds on 30B+). The Edges-page workflow involves re-tuning thresholds (min_gap, min_similarity, ...) and re-scanning — without a persistent cache, every tweak re-pays full inference cost on questions that haven't changed. Live measurement: a forecast that takes 2.08s cold hits in 0.3ms warm — **~6800× speedup**.

### Cache contract

- **Keys**: SHA-256 of input parts joined by `"|"`. Callers compose the parts so every input that could affect the response is reflected. First part is a schema version like `"forecast/v1"` so a future prompt revision can invalidate just that kind of entry by bumping the string.
- **Values**: any JSON-serializable dict. We use `dataclasses.asdict`-style payloads for `LLMForecast` and `MatchVerdict`.
- **TTL**: per-entry. `None` means "until purged." Forecasts default to 7-day TTL, match verdicts to 30-day.
- **Cache-by-default**, with `bypass_cache=True` on both wrappers as an escape hatch when you want fresh calls regardless.

### Failed calls do not poison the cache

`get_forecast` / `verify_match` only write to the cache on a *successful* parse. Transport failures, unparseable responses, and explicit None returns leave the cache untouched, so the next call still hits the wire. Tested explicitly in both wrapper test modules.

### Schema versioning

Bumping `"forecast/v1"` → `"forecast/v2"` (or `"match/v1"` → `"match/v2"`) invalidates every entry of that kind. Do this when:

- The system prompt changes meaningfully (calibration discipline updates, etc.)
- The set of prompt-input fields changes (e.g. we start passing the venue name as a separate field)
- The response schema changes (e.g. `MatchVerdict` gains a new required field)

Don't bump for trivial wording tweaks; the cache is already temperature-tolerant.

### Match cache is order-invariant

`verify_match(A, B)` and `verify_match(B, A)` ask the LLM the same thing. The cache key sorts the pair canonically before hashing so both directions share an entry. Saves ~half the LLM calls when callers don't bother to canonicalize themselves.

### Why a fresh connection per call

`_connect()` opens a new SQLite connection on every operation rather than holding a module-level long-lived one. Two reasons:
1. The cache path can change at runtime via `LLM_CACHE_PATH` env var (used by the test fixture that redirects to a tempdir).
2. Streamlit reruns the entire script on every interaction; a module-level connection would either leak across reruns or have to be carefully managed via `@st.cache_resource`. A fresh connection per call is dead simple and SQLite's open cost is negligible (microseconds).

### Settings-page integration

`pm/ui/pages/settings.py` adds a "🧠 LLM cache" panel showing entries / expired / oldest age / file size. Two buttons: "Purge expired" (removes only entries past TTL) and "Clear all" (wipes everything). Behaves like the Sandbox-reset button — toast + rerun on success.

---

## Module: `pm/feeds/llm_matcher.py`

Tier 4 cross-cutting tool: LLM-assisted semantic matching for free-text question pairs.

### Why it exists

The `manifold_data` Jaccard matcher is *direction-blind*. Real failure from live data:

> Query: "Will the Fed **cut** rates in May 2026?"
> Top match: "Will the Federal Reserve **hike** interest rates in 2026?"
> Jaccard: 0.25 — solidly above the default similarity floor.

Same content tokens, opposite questions. The Edges-page advisory warning used to tell users to eyeball every flag; this module is the tool that outsources that eyeballing to a local LLM.

### Design choices

- **Verification, not search.** `verify_match` takes two pre-selected questions and judges them; it does *not* search for candidates. The cheap Jaccard pre-filter still narrows 50+ Manifold search results down to 1 survivor, and the LLM only judges that survivor (and only if it already cleared the gap check). One LLM call per *plausible* edge candidate — not per Manifold-search result.
- **Different system prompt from `llm_data`.** Forecasting wants calibration discipline; matching wants strict discrimination. The prompt enumerates the specific failure modes (cut/hike, by-July/by-August, threshold differences) and explicitly says false positives are worse than false negatives.
- **Low temperature (0.1).** Matching is a classification problem, not a creative one.
- **Refuse-on-unreachable.** When `verify_match` returns None (HTTP error, unparseable response), the integration in `edges.py` **refuses the edge** rather than falling back to "trust Jaccard alone." False positives propagate; better to lose a few real matches.
- **Reuses `llm_data._session` and `OLLAMA_BASE`.** No duplicate Ollama config — one place to point at the local server, model discovery via the same `/api/tags` path.

### Live judgement examples

llama3.1 (8B) on the dev box:

| Pair | Verdict | Confidence | Reasoning |
|---|---|---:|---|
| "Fed cut May 2026" vs "Fed hike 2026" | DIFFERENT | 0.80 | "Semantic opposites (cut vs hike) with different timeframes" |
| "Trump impeached 2026" vs "Trump impeached by House 2026" | SAME | 1.00 | "Same underlying event, different mechanism" |
| "BTC $200k by 2027" vs "ETH $10k by 2027" | DIFFERENT | 0.80 | "Different cryptocurrencies and thresholds" |

All three are the cases the strict-judging prompt was tuned for.

### Cost shape

One `verify_match` call ≈ 0.5–1.5s on a 7-13B local model, several seconds on a 30B+ model. The Edges page integration calls it at most once per candidate that survives Jaccard + the gap check — typically 0–5 calls per 30-market scan, not 30+. The opt-in flag (`llm_verify=False` default) keeps the framework's normal latency unchanged for users who don't need the extra guard.

### Reusable beyond Manifold

The signature is `(a: str, b: str) → MatchVerdict | None`. Any future free-text matching pipeline (an LLM-assisted Metaculus integration, a Polymarket-↔-Kalshi same-market detector) can layer the same verifier on top of its cheap pre-filter. Document the cost trade-off in the new caller and gate on `llm_verify`.

---

## Module: `pm/feeds/manifold_data.py`

Tier 4's fourth source: a cross-platform comparison against Manifold Markets' community-forecast probabilities. Closest free-and-maintained option for politics-flavored markets in 2026.

### Why Manifold, not 538/RCP/Metaculus

- **538** was shut down by Disney in March 2025. The data archive on github is useful for *backtests* of historical elections but the site no longer produces fresh forecasts.
- **RCP polling** is paywalled.
- **Metaculus** added API authentication in early 2025; `api.metaculus.com` returns HTTP 403 to unauthenticated requests.
- **Manifold Markets** has a free, no-auth public API with natural-language search and binary-market probabilities. Their community is a small-but-engaged forecasting crowd that often prices in news faster than Polymarket's larger but more passive crowd.

This is honestly framed as **cross-platform comparison**, not a polling aggregator. We're comparing one prediction market's price to another's community-probability — when the two disagree by a wide margin, one of them is mispriced.

### Endpoints

- `GET /v0/search-markets?term=...&limit=...&filter=open` — natural-language search with relevance ranking. The matching workhorse.
- `GET /v0/market/{id}` — single-market detail.

Both are public and unauthenticated; no rate-limit documented but the standard 429+Retry-After backoff is wired up just in case.

### Token-Jaccard matching

The matching pipeline:
1. Search Manifold with the venue question as the natural-language query.
2. Filter to binary outcomes, not-yet-resolved, mana volume ≥ `min_volume` (default 100 — below this, the community probability is dominated by 1-2 traders).
3. Compute Jaccard similarity (token-set intersection / union) between the venue question and each candidate. Stopwords are stripped (`the`/`a`/`is`/`will`/etc.) and tokens are lowercased.
4. Take the highest-scoring candidate. Skip if its similarity is below the floor.

### The direction-blindness failure mode

**Jaccard similarity ignores word polarity.** Real example caught on live data:

> Query: "Will the **Fed cut rates** in May 2026?"
> Top match: "Will the Federal Reserve **hike interest rates** in 2026?"
> Similarity: 0.25
> Probability: 0.376

Those two questions ask *semantic opposites* but share 5 of 6 content tokens — the matcher happily reports a strong-looking similarity and the engine emits a wildly wrong edge. The Edges page's advisory warning calls this out explicitly so users eyeball matched questions before treating any flag as signal.

Two open follow-ups to fix this systematically:
- **Direction-aware matching**: rule-based negation/antonym detection (positive/negative pairs like cut/hike, win/lose, pass/fail).
- **LLM-assisted semantic matching**: use the existing `llm_data` Ollama source to validate that two questions ask the same thing. Slower per-call but much more reliable.

### Backtest caveat

`strategy_manifold_community` is registered in `backtest.STRATEGIES` for consistency with the other sources, but `search_markets` returns *today's* community probability — not the probability as of `snap.as_of`. Manifold does expose historical predictions per-market, but the search-based matching loop doesn't currently consume them, so backtest results reflect "if today's Manifold community had been asked this when the historical market was open" rather than the true contemporaneous comparison. An archival-prediction follow-up would make the gate fully honest.

---

## Module: `pm/feeds/llm_deep_analysis.py`

Slow per-market structured-reasoning pipeline, complementary to the fast batch forecasts in `llm_data`. One market at a time, several context fields packed into one long prompt, JSON-constrained output via Ollama, ~30s-3min per call on a 32B model.

`analyze_market(provider, df_markets, ticker, model, include_web_context=False)` is the entry point. Gathers context (price history summary, market metadata + resolution criteria, order book depth, sibling sub-markets) and packs into one prompt. Returns a `DeepAnalysis` dataclass with `probability`, `confidence`, `key_drivers`, `key_risks`, `evidence_used`, `recommended_action`, `reasoning`, and `context_summary` (the prompt blob for audit).

System prompt enforces an honesty rule: every claim must cite a CONTEXT field, low confidence beats false certainty, output is JSON-only. When `include_web_context=True` is set, the prompt also includes Google News headlines + a Wikipedia summary, and the model must cite those specifically in `evidence_used` — same standard, made explicit.

Cached in `llm_cache` SQLite for 24h on (ticker, model, context-hash, web-on/off). `last_error()` exposes the most recent server response body for UI surfacing when the call fails. `RECOMMENDED_MODELS` + `sort_models_recommended_first` + `default_model` drive the page's model picker (qwen3:32b → qwen2.5:32b → ... → llama3.1:latest).

Forecasts saved from the page are tagged `llm_deep:<model>` in the calibration log so the deep-analysis source has its own Brier track separate from the fast `llm:<model>` line.

## Module: `pm/feeds/web_context.py`

Phase-B context for Deep Analysis. Two free unauthenticated sources: Google News RSS for recent headlines, Wikipedia REST API for entity summaries.

- `search_news(query, max_items)` — Google News RSS at `news.google.com/rss/search`. Returns `list[NewsItem]`. Cached 6h.
- `wikipedia_summary(title)` — Wikipedia REST `/api/rest_v1/page/summary`. Returns `WikiSummary | None` (None on 404 / disambiguation / empty extract). Cached 7d.

Two gotchas worth remembering:
- Wikipedia's UA policy requires an email contact in the User-Agent header (a bare GitHub URL 403s). Our `_USER_AGENT` reflects this.
- The path segment must use `urllib.parse.quote` (%20-encoded spaces), not `quote_plus` (+ -encoded) — Wikipedia REST 404s the latter.

`format_news_for_prompt` / `format_wiki_for_prompt` build the strings injected into the deep-analysis prompt. Trim to fixed char budgets so token usage stays predictable.

## Module: `pm/ops/bulk_deep_analysis.py`

Cron-able CLI runner that walks closing-soon markets and runs `llm_deep_analysis.analyze_market` on each, one model per invocation. Forecast-only — no paper trades by design. Output lands in the `agentic_deep` profile as calibration-log entries tagged `llm_deep:<model>`.

Six cron entries (2am-7am nightly) run the full `RECOMMENDED_MODELS` rotation on the same nightly market universe, producing the head-to-head Brier-per-model dataset that informs which model to promote to live triggers later.

### Category diversity

Sorting purely by liquidity collapses each nightly batch onto Sports (NHL series + MLB games dominate). The runner instead takes up to `max_markets / n_categories` from each category in liquidity-descending order, then fills any remainder from the unallocated tail. Logs `category breakdown: Sports=12, Politics=12, ...` each cycle so the mix is visible at a glance.

### Recency skip

`_recent_forecast_exists(condition_id, source, min_age_hours)` checks the calibration log and skips markets already analyzed by this (ticker, model) pair within `--skip-age-hours` (default 12h). Prevents duplicate rows on back-to-back invocations. The 24h `llm_cache` TTL on `analyze_market` already short-circuits the actual LLM call; this skip prevents writing duplicate calibration entries.

### Why the Ollama timeout bump

The default 120s in `llm_data.HTTP_TIMEOUT` tripped on real-world 35B-model runs with full context (web context + price history + order book + siblings). The runner overrides to 300s via `--http-timeout` and assigns it to `llm_data.HTTP_TIMEOUT` at runtime before the first `analyze_market` call.

## Module: `pm/ops/auto_resolve_runner.py`

Cron-able CLI runner that walks every profile and marks pending forecasts whose underlying markets have resolved. Without this the Track Record badges on the Recommendations page stay "untested" even after the markets they reference have closed — the calibration log only updated when the user manually clicked "Auto-resolve" on the page.

`run_cycle()` is two-phase:

1. Switch to the owner profile, build the merged closed-markets lookup ONCE (public providers via `get_closed_markets()`, plus Kalshi authed settlements when `kalshi_auth.is_configured()`). Settlements are just "did this ticker resolve YES/NO" — no per-profile state, safe to share.
2. Walk every profile in turn, call `cal.auto_resolve_pending(merged_lookup)` on each.

Save/restores the user's UI-selected profile around the cycle so cron firings don't steal the sidebar. Same pattern as `strategy_runner`.

Cron line: `*/15 * * * * cd <repo> && .venv/bin/python auto_resolve_runner.py >> data/shared/auto_resolve.log 2>&1`

The script also `load_dotenv()`s at module level so `KALSHI_API_KEY_ID` reaches the settlement-overlay path when run from cron (one of the day-1 bugs).

## Module: `pm/feeds/llm_data.py`

Tier 4's second source: a local LLM via Ollama. No paid API — the development environment runs Ollama at `localhost:11434` against a dedicated RTX 5000 Ada (32 GB VRAM), so the cost-per-call is effectively zero and latency is the constraint.

### Endpoints used

- `GET /api/tags` — list available models. The Edges page populates its model dropdown from this so the user can swap between (say) `llama3.1:latest`, `qwen2.5:14b`, and `gemma3:27b` without code changes.
- `POST /api/chat` with `format: "json"` — single-turn chat that's guaranteed to return JSON. We layer a defensive regex-fallback parser on top so a prose-wrapped JSON response still gets parsed correctly.

### Why a system prompt for calibration discipline

LLMs are notoriously bad calibrators by default — they over-commit at the extremes and under-anchor to base rates. The system prompt explicitly asks for: market price as a strong prior, no extreme probabilities (>0.95 or <0.05) without overwhelming evidence, fallback to 0.5 / market price under uncertainty. Temperature defaults to 0.2 since forecasting is a calibration problem, not a creativity problem.

### Lookahead with training data

Local models have a knowledge cutoff in the past. Two failure modes when backtesting:

1. **Markets that resolved before the cutoff**: the model may "remember" the actual outcome from training data and return a confidently-correct probability that's not a forecast. Backtest Brier looks suspiciously low on those markets.
2. **Markets that resolved after the cutoff**: the model lacks information the live forecaster would have had. Backtest Brier looks suspiciously high.

The honest approach is to backtest the LLM only on markets that closed *just after* the model's training cutoff — recent enough that the world hasn't changed materially, late enough that the model can't recall the outcome. There's no clean way to know a model's cutoff programmatically, so this is a manual filter the user has to apply.

### Backtest-gate first, recommendation second

`strategy_llm_forecast` is registered in `backtest.STRATEGIES` so it appears in the Backtest page alongside the in-process baselines. The Brier-vs-market-baseline gate there is the rule for whether this source ever graduates from advisory to actually influencing Kelly sizing. The initial 10-market live scan on `llama3.1:latest` produced one flagged edge that was a clean false positive (LLM defaulted to 50% on a sports market priced at 100%) — exactly the kind of failure the gate exists to catch.

### Why per-market caching isn't implemented yet

A bigger optimization would be persistent per-(question, model) caching so re-running an Edges scan doesn't re-score the same markets. For v1 the in-process Streamlit cache is enough; persistent caching is a follow-up if total LLM time becomes a real bottleneck.

---

## Module: `pm/core/profiles.py`

Multi-user profile system, Option A from the design discussion: file-based, single-machine, no auth. Each profile has its own `data/<profile>/` directory holding `portfolio.json`, `forecasts.json`, `alerts.json`, `settings.json`. The active profile is persisted in `data/.active_profile`.

```python
PROFILES_ROOT = Path(__file__).parent / "data"
DEFAULT_PROFILES = ["jeff", "dex", "sandbox"]
DEFAULT_ACTIVE_PROFILE = "jeff"
PROFILE_FILE_KINDS = ("portfolio", "forecasts", "alerts", "settings")

initialize_profiles(project_root=None) -> None
# Idempotent. Creates data/ + default profile dirs + shared/. Migrates
# legacy root-level state files into data/jeff/ on first run.

list_profiles() -> list[str]                     # primary first when empty
active_profile() -> str
set_active_profile(name: str) -> None
create_profile(name: str) -> bool                # raises ValueError on bad names
reset_profile(name: str) -> int                  # wipes files; returns count
file_path(kind: str, profile: str | None = None) -> Path
profile_dir(name: str) -> Path
shared_dir() -> Path
```

### How lazy path lookup works

The data modules (`portfolio`, `calibration`, `alerts`, `risk`) each have a private `_*_path()` function that's called on every read/write. The lookup order is:

1. **Env-var override** (`POLYMARKET_PORTFOLIO_FILE`, etc.) — used by tests via `monkeypatch.setenv`, and by users who want to keep state outside the project (e.g. in a backed-up config dir)
2. **Active profile's directory** via `profiles.file_path(kind)`

This means switching profiles takes effect on the next operation — no module reimport needed. Streamlit's `st.rerun()` is enough.

### Adding a new data module to the profile system

1. Add the kind to `profiles.PROFILE_FILE_KINDS`.
2. In the data module, add `def _xxx_path() -> Path` that reads the env override first, then calls `profiles.file_path("xxx")`.
3. Replace all `XXX_FILE` references with `_xxx_path()`.
4. Update tests to use the env-var-based fixture pattern (see `conftest.py`'s `portfolio_path`).

## Module: `pm/core/emergency_stop.py`

System-wide kill switch. Single flag file (`data/.emergency_stop`)
that, when present, causes every order-placement path and every new
paper open across the system to refuse. Read-only data collection
keeps running.

```python
is_active() -> bool                              # cheap; safe to call frequently
activate(reason: str | None = None) -> Path     # creates the flag
deactivate() -> bool                             # removes the flag
get_status() -> dict | None                      # {"active", "since", "reason"} or None
check_or_raise(*, context: str = "action") -> None  # helper that raises EmergencyStopActive

class EmergencyStopActive(RuntimeError): ...     # raised by check_or_raise
```

**Storage**: two-line text file — ISO timestamp on line 1, optional
reason on line 2. Env override: `POLYMARKET_EMERGENCY_STOP_FILE`.

**Integrations** (where the flag is consulted):
- `kalshi_orders.place_limit_order` — raises `OrderRejected("EMERGENCY STOP active …")` before any other check
- `strategy_runner.run_cycle` — short-circuits and returns empty `RunnerStats` before profile pinning
- `shared.emergency_stop_banner()` — renders a red `st.error()` banner; `app.py` calls it above `pg.run()` so every Streamlit page surfaces the warning

**Fail-safe semantics**: if `is_active()` can't stat the flag file (filesystem error), it returns `True`. A broken disk shouldn't let new orders go out.

**Read-only daemons NOT gated**: `book_poller`, `mlb_book_poller`, `kalshi_ws`, `baseball.live`, `auto_resolve_runner`, `bulk_deep_analysis`, `pre_position` (paper-trade evaluation only). The intent is "halt new actions," not "halt all activity" — losing data ingestion in a panic makes recovery harder.

CLI: `python -m emergency_stop {status|activate|deactivate} [--reason ...]`.

## Module: `pm/ledger/position_meta.py`

Per-position lifecycle metadata that the agentic runner's exit-management phase needs but the portfolio transaction log doesn't carry: entry price (for net-P&L math), opened_at (for time-stop), running peak (for trailing stop), and `tp_armed` (the two-stage TP flag).

Storage: `data/<profile>/position_meta.json`. Keyed on `"<condition_id>|<outcome>"` so YES and NO on the same market coexist. Env override: `POLYMARKET_POSITION_META_FILE`.

```python
record_entry(condition_id, outcome, *, entry_price, opened_at=None) -> dict
# Idempotent on the key — preserves original entry on re-buys so the
# trail/peak math stays interpretable.
update_peak(condition_id, outcome, *, current_price, now=None) -> dict | None
# Peak ratchets UP only.
get_entry(condition_id, outcome) -> dict | None
arm_tp(condition_id, outcome) -> dict | None     # flip tp_armed=True (idempotent)
clear_entry(condition_id, outcome) -> bool       # call on full position close
all_entries() -> dict[str, dict]
```

## Module: `pm/ledger/exits_analysis.py`

Reader + analytics over `data/<profile>/exits.jsonl`. Every closure (managed exit or resolution) is appended as one JSON line with entry/exit/peak prices, gross/net P&L, fees paid, reason.

```python
load_exits(profile=None) -> pd.DataFrame
# Canonical schema columns: ts, profile, condition_id, question, outcome,
# shares, entry_price, exit_price, peak_price, drawdown_from_peak_pct,
# gross_pnl_dollars, fees_paid_dollars, pnl_dollars (NET), pnl_pct (NET),
# exit_reason, held_days, strategy

exit_reason_summary(profile=None) -> pd.DataFrame
# Aggregates: exit_reason, n, total_pnl_dollars, mean_pnl_pct,
#             win_rate, mean_held_days.

counterfactual_pnl(df_exits, closed_lookup) -> pd.DataFrame
# Adds counterfactual_pnl_dollars (what would have been if held to
# resolution, net of round-trip Kalshi fees), counterfactual_pnl_pct,
# and exit_alpha_dollars (actual NET - counterfactual NET).

compare_profiles(managed_profile, unmanaged_profile, *,
                 closed_lookup=None) -> dict
# Top-level A/B card. With closed_lookup, includes managed_counterfactual
# {n_resolved, managed_pnl_on_resolved, held_pnl_on_resolved, exit_alpha}.
```

## Module: `pm/venue/kalshi_fees.py`

Venue-accurate Kalshi fee calculation. Per the published formula:
`ceil(rate × P × (1-P) × C × 100) / 100` dollars per trade. Sports
series gets the 0.035 reduced rate via ticker-prefix lookup; standard
0.07 otherwise.

```python
SPORTS_PREFIXES = ("KXMLB", "KXNBA", "KXNFL", "KXNHL", "KXWNBA",
                   "KXMLS", "KXMVESPORTS")
STANDARD_FEE_RATE = 0.07
SPORTS_FEE_RATE = 0.035

fee_for_trade(price, contracts, *, fee_rate=0.07) -> float
fee_pct_of_stake(price, *, fee_rate=0.07) -> float    # = rate × (1-P)
fee_rate_for_series(ticker_or_series) -> float
round_trip_fees(entry_price, exit_price, contracts, *, ticker="",
                fee_rate=None) -> float
```

**Float-rep gotcha**: `0.07 * 0.5 * 0.5 * 100 = 1.7500000000000002`, which `math.ceil` rounds up to $1.76 (wrong). Implementation snaps to 9 decimal places before ceiling. Doctests pin the numeric expectations.

**Integration points**:
- `strategy_runner._evaluate_exit` — TP/SL thresholds gate on **NET P&L**, computed from a closed-form formula that's stake-independent: `gross_pct - rate × (1 - entry) - rate × exit × (1 - exit) / entry`. A gross gain that doesn't clear the fee drag won't fire a take-profit.
- `strategy_runner._log_exit` — writes `gross_pnl_dollars`, `fees_paid_dollars`, and `pnl_dollars` (NET) into `exits.jsonl`.
- `backtest.run_backtest(fee_fn=...)` — callable parameter overrides flat `fee_pct` for per-trade Kalshi-accurate accounting.
- `exits_analysis.counterfactual_pnl` — counterfactual is also net-of-fees so the A/B comparison is apples-to-apples.

## Module: `pm/research/oscillation_scanner.py`

Ranks active markets by oscillation behavior (detrended_std / drift
ratio + mean-crossing frequency + range_width). Two registered
strategies — see [`failed-experiments.md`](failed-experiments.md) for
the falsification story.

```python
@dataclass OscillationMetrics
    # condition_id, question, category, current_price, n_history,
    # days_covered, avg_price, min_price, max_price, range_width,
    # drift_abs, drift_pct, detrended_std, range_score, mean_cross_freq

compute_metrics(df_prices, *, condition_id="", question="",
                category="", min_history_points=20) -> OscillationMetrics | None
scan_markets(provider, df_markets, *, days=30, fidelity=60,
             max_markets=200, min_history_points=20,
             min_avg_price=0.05, max_avg_price=0.95,
             min_cross_freq=0.0, min_range_width=0.0,
             progress_callback=None) -> pd.DataFrame

# Registered strategies (both consult compute_metrics + the same filters):
strategy_oscillation_revert    # ⚠️ KNOWN BAD (registered as oscillation_revert);
                                # fires runtime warning on invocation
strategy_oscillation_momentum  # 🧪 PAPER (registered as oscillation_momentum);
                                # inverted direction; +8.9% backtest ROI
                                # with max_position_pct=0.05
```

CLI: `python -m oscillation_scanner --days 30 --top 20 --min-cross-freq 0.5`.

## Module: `pm/research/oscillation_backtest.py`

Retrospective evaluation of the scanner + strategies. Uses
`backtest.build_dataset` for lookahead-safe `(snapshot, resolution)`
pairs, runs the strategy + a `market_consensus` baseline on the same
dataset, buckets by `range_score` quintile to test whether the
scanner's ranking metric is informative.

```python
@dataclass BucketStats          # per-quintile rollup
@dataclass OscillationBacktestResult

run_oscillation_backtest(provider, *, max_markets=200,
                          since_days=None, until_days=0.0,
                          lead_hours=72, history_days=30, fidelity=60,
                          bankroll=1000.0, kelly_multiplier=0.5,
                          half_spread=0.005,
                          strategy_kwargs=None, n_buckets=5,
                          max_workers=1, min_volume=0.0,
                          use_cache=True, use_kalshi_fees=True,
                          progress_callback=None) -> OscillationBacktestResult
format_report(result) -> str
```

Dataset cache lives at `data/shared/backtest_cache/`. Cache key
includes all fetch params; strategy params don't invalidate (lets you
iterate on filter thresholds without re-fetching).

CLI: `python -m oscillation_backtest --since-days 90 --max-workers 8 --lead-hours 48`.

## Module: `pm/research/pre_event_scanner.py`

Filter for cheap markets resolving soon. Plus a registered
`strategy_pre_event_lottery` (UNTESTED in display name; backtest on
cached dataset showed -91% ROI — kept for reference).

```python
DEFAULT_CATEGORIES = ("Sports", "Politics", "Economics", "Crypto",
                      "Climate", "Entertainment")

scan_lottery_candidates(provider, *, max_price=0.10, max_days=1.5,
                         categories=None, min_volume=100.0,
                         max_markets=500) -> pd.DataFrame
concentration_summary(df) -> {"n_markets", "top_series",
                              "top_series_share", "n_series"}
strategy_pre_event_lottery  # 🧪 UNTESTED in registry
```

The concentration warning is load-bearing: the lottery thesis assumes
independent catalysts, but Kalshi's sub-$0.10-with-<1.5d universe is
often dominated by one event's threshold-ladder sub-markets.

## Module: `pm/ledger/alerts.py`

Append-and-mutate **alert log** with on-demand threshold checking. The `alerts.json` schema:

```json
{
  "version": 1,
  "alerts": [
    {
      "id": "uuid4",
      "created_at": "ISO",
      "question": "...",
      "condition_id": "0x...",
      "outcome": "Yes",
      "direction": "above" | "below",
      "threshold": 0.80,
      "triggered": false,
      "triggered_at": null,
      "triggered_price": null
    }
  ]
}
```

```python
add_alert(question, condition_id, outcome, direction, threshold) -> dict
delete_alert(alert_id: str) -> bool
reset_alert(alert_id: str) -> bool        # un-trigger so it can fire again
get_alerts() -> pd.DataFrame
check_alerts(df_markets) -> list[dict]    # returns NEWLY-triggered alerts
summary_stats() -> {"total", "active", "triggered"}
```

### Threshold semantics

- **`above`**: triggers when `current_price >= threshold` (≥, equal counts).
- **`below`**: triggers when `current_price <= threshold` (≤, equal counts).
- An already-triggered alert is skipped by `check_alerts` — fires once per crossing. Call `reset_alert` to re-arm it.
- If the market isn't present in `df_markets`, the alert is silently left alone (not an error).

### Hook into the UI

`app.py` exposes a helper `check_alerts_and_notify(df_markets)` that calls `check_alerts` and emits `st.toast(...)` for each newly-fired alert. The Markets page calls it on every render. The Alerts page also calls it before rendering, so opening that page is a manual "check now."

The sidebar runs `al.summary_stats()` on every render to show a `🔔 N triggered` warning band when any alerts are in the triggered state.

---

## Module: `pm/core/sidecars.py`

Defines the **Sidecar protocol** — the shared shape every sidecar engine implements. See `docs/sidecar-protocol.md` for the canonical spec.

Sidecars are domain-isolated sub-packages that:
- Own their state (one SQLite per sidecar at `data/shared/<name>_state.sqlite`)
- Run on their own schedule (cron-able `runner.py`)
- Expose two layers in their `__init__.py`:
  - **Operational** (uniform across all sidecars, used by the control plane): `name`, `display_name`, `health()`, `stats()`, `recent_activity(limit=20)`
  - **Domain** (unique per sidecar): e.g. `predict_game` for baseball, `recent_since` for news (future)

### Key types

```python
@dataclass class SidecarHealth(status, last_update_ts, next_update_ts, errors_24h, detail)
@dataclass class SidecarActivityEvent(ts, kind, message, detail)
@runtime_checkable class Sidecar(Protocol)
```

### Functions

```python
register_sidecar(module_or_name) -> None  # called at sub-package import time
list_sidecars() -> list[Sidecar]
get_sidecar(name) -> Sidecar | None
safe_health(fn) -> SidecarHealth  # wraps a fn so exceptions → status=down
```

### Adding a sidecar

1. Create `mysport/` with `data.py`, `model.py`, `state.py`, `runner.py`, `__init__.py`.
2. In `__init__.py`: implement `health()`/`stats()`/`recent_activity()`, define `NAME`/`DISPLAY_NAME`, call `register_sidecar(__name__)`.
3. Implement the domain layer (whatever the sidecar does).
4. Add cron entry with `flock` lock at a non-conflicting hour.
5. Optionally build a FastAPI web UI (NOT Streamlit — multi-user safe).

**Import discipline**: nothing in `<sidecar>/` may import `kalshi_client`, `provider`, `polymarket_client`, `backtest`, `edges`, `calibration`, `portfolio`, `risk`, `alerts`, `shared`, `app.py`, or `pages/*`. The market app reaches in via the sub-package's public API only.

---

## Module: `baseball/` (sidecar)

First sidecar instance. MLB win prediction from team strength + starting-pitcher quality.

### File layout

```
baseball/
  __init__.py    — public surface (predict_game, team_ratings_table,
                   today_schedule) + Sidecar protocol impl
  data.py        — MLB Stats API client (statsapi.mlb.com)
  model.py       — pure functions: regressed Pythagorean, pitcher adj,
                   matchup compose in log-odds
  state.py       — SQLite at data/shared/baseball_state.sqlite
                   (teams, team_season, pitchers, batters, bullpens,
                    lineups, games, predictions, live_plays, activity)
  park_factors.py— per-park run/HR multipliers (Bayesian regressed)
  recent_form.py — last-15-day Pythagorean blend (≤30% weight)
  analysis.py    — lead-lag join queries (plays × book snapshots)
  insights.py    — Stats-page aggregates + LLM context bundler
  llm_chat.py    — Ollama-backed chat (imports llm_data)
  runner.py      — cron-able nightly refresher (7-day schedule window;
                   --lineups-only mode for in-day cron)
  live.py        — long-lived daemon polling MLB feed/live every 10s;
                   persists every play + syncs game Finals back to
                   games table for fast resolution
  backfill.py    — historical play-by-play loader (--days, --start/end,
                   --games-only)
  web.py         — FastAPI+Jinja2+HTMX UI on :8770
  templates/     — Jinja2 templates (base.html, index.html, game.html,
                   standings.html)
  static/        — style.css (diamond-green theme) + favicon.svg
```

### Public domain API (`baseball/__init__.py`)

```python
predict_game(home, away, game_date=None) -> dict | None
# Returns {home_win_prob, home_team, away_team, as_of, inputs, source}.
# Prefers stored prediction for the matching date; falls back to fresh
# compute from team season stats. None if teams can't be resolved.

team_ratings_table(season=None) -> list[dict]
# Every rated team for the season, sorted by regressed-Pythagorean strength.

today_schedule() -> list[dict]
# Today's games with predictions joined.

refresh_all(force=False) -> dict
# Runs one runner cycle synchronously. Used by the "refresh now" button.
```

### Model (`baseball/model.py`)

Pure-function matchup model. Log-odds composition because rate probabilities don't compose additively but log-odds do.

```python
MODEL_VERSION = "0.1.0"
PRIOR_WEIGHT = 40        # virtual games at .500 (~quarter season)
HOME_FIELD_LOGODDS = ln(0.54/0.46) ≈ 0.161
LEAGUE_AVG_ERA = 4.30
ERA_LOGODDS_PER_RUN = 0.13   # ~5pp swing per 2-run ERA gap

regressed_pythagorean(runs_scored, runs_allowed, games_played) -> float
pitcher_logodds_adj(era: float | None) -> float    # 0 if era is None
predict_matchup(*, home_strength, away_strength,
                home_pitcher_era=None, away_pitcher_era=None,
                home_team_name="Home", away_team_name="Away",
                home_field=True) -> Prediction
```

### Model evolution (v0.4.0 as of this writing)

Every prediction stored carries a `model_version` stamp so future analysis can A/B between versions. Progression so far:

| Version | What it adds | Inputs to `predict_matchup` |
|---|---|---|
| **0.1.0** | Initial. Regressed Pythagorean + home-field + SP ERA delta. | `home_strength`, `away_strength`, `home_pitcher_era`, `away_pitcher_era` |
| **0.2.0** | Park-neutralized team stats + bullpen ERA delta. | + park-neutralized RS/RA inputs, + `home_bullpen_era`, `away_bullpen_era` |
| **0.3.0** | Recent-form blend (last 15 days, ≤30% weight). | Strength is now blended; otherwise same. |
| **0.4.0** | Posted-lineup OPS adjustment. | + `home_lineup_avg_ops`, `away_lineup_avg_ops` |

Composition is always log-odds additive — each layer adds an adjustment that's the difference between home's deviation from league average and away's. See `model.predict_matchup` for the exact assembly.

### Park factors (`baseball/park_factors.py`)

Per-park run/HR multipliers calibrated empirically from finalized games. Plugs in via `park_neutralize_strength(team_id, RS, RA, GP)` which approximates "what would these stats look like at a neutral park" by dividing the home half by the park factor.

Bayesian regression: 30 virtual "neutral" games added before computing the ratio so a single hot week doesn't make a park look like Coors. Real park factors take multi-season data; we ship a noisy first-cut and let it tighten as data accumulates.

### Recent-form blend (`baseball/recent_form.py`)

Computes a windowed Pythagorean (default 15 days) per team and blends it into the season strength. Weight ramps linearly from 0 to `MAX_RECENT_WEIGHT=0.30` as games-in-window approach `TARGET_GAMES_FOR_FULL_WEIGHT=12`. Deliberately capped so a 4-game hot streak doesn't override 50 games of season data.

Same park-neutralization applies to the recent window for consistency.

### Lineups (`baseball/data.py::get_lineups_for_date` + state tables)

MLB Stats API exposes posted lineups via `/api/v1/schedule?hydrate=lineups`. Each game's `lineups.homePlayers`/`awayPlayers` is a 9-element list (DH leagues) of player IDs.

Storage: two tables, `lineups (game_pk, side, player_ids JSON)` and `batters (id, season, name, ops, ...)`. The runner pulls lineups for [yesterday UTC, today UTC, today+1 UTC] (yesterday catches late-Central games that crossed midnight UTC) and persists every batter we see.

Model integration in `runner._avg_lineup_ops`: filters out batters with fewer than 50 AB (so a 0.150-OPS-in-12-AB call-up doesn't crater a star-heavy lineup average). Returns None unless ≥5 batters in the lineup meet the threshold — the model then falls back to "no lineup info, league-average baseline."

In-day refresh cron at `*/30` runs `baseball.runner --lineups-only` because lineups post 60-90 min before first pitch, well after the nightly 04:15 refresh.

### MLB book poller (`pm/capture/mlb_book_poller.py`)

**Market-app-side** companion daemon (NOT in `baseball/` — imports `kalshi_client`, which the sidecar protocol forbids). Discovers every open `KXMLBGAME-*` event, walks its nested markets, snapshots each side's order book, and writes to the existing `book_archive.sqlite` schema with venue='kalshi'. Adaptive cadence: 15s during live games, 120s idle. Event/market discovery cached, refreshed every 5 min. **Largely superseded by `pm/capture/kalshi_ws.py`** (WebSocket streamer) which provides sub-100ms updates; the REST poller runs as a fallback.

Closes the market half of the lead-lag dataset. The general `book_poller` polls top-25 markets per venue at 5-min cadence — MLB games don't crack that filter, so they were invisible before this daemon shipped.

Launcher: `scripts/mlb_book_poller.sh [--background|--stop|--status]`. PID at `data/shared/mlb_book_poller.pid`. Also available as a systemd user service — see [Long-lived daemons (systemd)](#long-lived-daemons-systemd) below.

### Lead-lag analysis (`baseball/analysis.py`)

Joins `baseball_state.live_plays` against `book_archive.book_snapshots` via a ticker→game_pk bridge that uses the **Kalshi-tickers-are-Eastern** finding: convert `game.game_date` from UTC to America/New_York, then match against ticker prefix `KXMLBGAME-YYmmmDD`.

Three primary functions:
- `tickers_for_game(game_pk)` — which Kalshi market tickers correspond to one MLB game
- `book_around_play(game_pk, play_id, window_minutes)` — book trajectory before+after a specific play
- `reaction_summary(event_type, ...)` — aggregate across many games

**Sample-size discipline**: `reaction_summary` returns `None` for `reaction_pct` when `n_played_with_book < 5`. UI shows "—" instead of a misleading percent. Hard-coded protection against tiny-sample false confidence.

### Stats / Insights (`baseball/insights.py`)

Heavy-lift aggregates the Stats page uses. Includes `current_edges_for_date(date_iso)` — a Kalshi-touching pull with 5-min TTL cache so dashboard refreshes don't hammer the API. Also exports `context_for_llm()` which bundles the full state snapshot for the chat prompt.

### LLM chat (`baseball/llm_chat.py`)

Local-LLM conversation surface with the full sidecar state pre-loaded into the system prompt every turn. Stateless — page DOM holds turn history; reload to start fresh. Default model: `llama3.1:latest` (env `BASEBALL_CHAT_MODEL` overrides).

Imports `llm_data` directly. This is a deliberate exception to the strict "sidecars don't import market-app code" rule because `llm_data` is a stateless Ollama HTTP client — utility, not domain.

### Live event collector (`baseball/live.py`)

A long-lived daemon that polls MLB's `feed/live` endpoint every 10s (matching MLB's CDN `max-age=10` TTL) for every in-progress game and persists every new play to a `live_plays` table. Average play detection latency: ~5 seconds. Each call is ~0.15s and ~750KB; 15 concurrent games = 1.5 req/s. Feeds lead-lag analysis and the pre-positioning strategy.

```python
# Schema (additive — appended to baseball_state.sqlite at startup)
CREATE TABLE live_plays (
    game_pk          INTEGER NOT NULL,
    play_id          INTEGER NOT NULL,
    ts               TEXT NOT NULL,
    half_inning      TEXT,
    inning           INTEGER,
    event_type       TEXT,
    description      TEXT,
    home_score_after INTEGER,
    away_score_after INTEGER,
    home_win_prob    REAL,
    raw_json         TEXT,
    fetched_at       TEXT NOT NULL,
    PRIMARY KEY (game_pk, play_id)
);
```

Idempotent `(game_pk, play_id)` primary key — re-polling the same game is a no-op except for new plays. `raw_json` preserves the full MLB API blob for future analysis without re-fetching.

```python
# Public API (baseball.live)
fetch_live_feed(game_pk, *, timeout=10.0) -> dict | None
run_cycle() -> dict             # one pass; safe to call repeatedly
get_recent_plays(game_pk, *, limit=50) -> list[dict]
live_plays_summary(*, since_hours=24.0) -> dict
```

The collector is cheap when no games are running (one SQLite query, no HTTP). Safe to leave running 24/7. Discovers in-progress games via `baseball_state.games` rows on today/yesterday and confirms liveness via the feed's `gameData.status.detailedState`.

**Launcher**: `scripts/baseball_live.sh [--background|--stop|--status]`. PID file at `data/shared/baseball_live.pid`. Log at `data/shared/baseball_live.log`. Also available as a systemd user service — see [Long-lived daemons (systemd)](#long-lived-daemons-systemd) below.

### Web UI

`baseball.web:app` is a FastAPI application served on port 8770 (env override `BASEBALL_WEB_URL` for the link in the Streamlit sidebar). Routes:

- `GET /` — today's slate + top-10 teams + recent activity
- `GET /game/{game_pk}` — drill into one game (line movement, pitcher cards, inputs)
- `GET /standings` — full standings by league/division
- `POST /refresh` — kick off a background refresh (HTMX target)
- `GET /health.json` — JSON health endpoint for external monitors
- `GET /api/predict?home=X&away=Y` — JSON predict endpoint

Multi-user safe by default (FastAPI request scope). No auth — LAN-only. Run with `scripts/baseball_web.sh` (foreground) or `scripts/baseball_web.sh --background`.

### Cron

```
15 4 * * * flock -n /tmp/polymarket_baseball.lock -c 'cd /home/USER/Coding/kalshi-trader && .venv/bin/python -m baseball.runner >> data/shared/baseball.log 2>&1'
```

Slotted between the 4am and 5am bulk_deep_analysis runs. `flock -n` prevents pile-up.

### Long-lived daemons (systemd)

Three long-running daemons need to survive reboots and power outages. They run as **systemd user services** (not system-level — no root required). Unit files live at `~/.config/systemd/user/`:

| Service | Unit file | What it runs |
|---|---|---|
| `baseball-web` | `baseball-web.service` | `python -m baseball.web` (FastAPI on :8770) |
| `baseball-live` | `baseball-live.service` | `python -m baseball.live --loop --interval 10` |
| `mlb-book-poller` | `mlb-book-poller.service` | `python mlb_book_poller.py --loop --interval 15 --idle-interval 120` |
| `kalshi-ws` | `kalshi-ws.service` | `python kalshi_ws.py --archive-interval 15` |
| `pre-position` | `pre-position.service` | `python pre_position.py --loop --interval 10` |

All use `Restart=on-failure` with a 10s delay. `loginctl enable-linger <user>` is set so services start at boot without requiring a login session.

Common operations:

```bash
systemctl --user status baseball-web          # check one service
systemctl --user restart baseball-live        # restart after code changes
systemctl --user stop mlb-book-poller         # stop one
journalctl --user -u baseball-live -f         # tail logs
systemctl --user daemon-reload                # after editing a .service file
```

**After code changes** that affect these daemons, restart the relevant service(s) — they run from the `.venv` in the working directory, so a `git pull` + `pip install` is picked up on restart.

**Recovery after an outage**: the daemons restart automatically. Order book snapshots during the downtime are lost (no historical API). Play-by-play data can be backfilled:

```bash
.venv/bin/python -m baseball.backfill --start YYYY-MM-DD --end YYYY-MM-DD
```

Then run `pm/ops/auto_resolve_runner.py` to catch up on missed resolutions.

### Edges integration

The **strategy adapter** lives in `edges.py` (market-app-side, NOT in `baseball/`). The sidecar exposes `predict_game`; the adapter is the single legitimate import boundary between the two.

#### Public surface in `edges.py`

```python
def find_baseball_edges(
    df_markets: pd.DataFrame,
    *,
    min_gap: float = 0.05,
    min_days_to_expiry: float = 0.0,
    progress_callback: Any = None,
) -> list[Edge]: ...

def strategy_baseball_winprob(
    snap: bt.MarketSnapshot,
    *,
    min_gap: float = 0.05,
) -> bt.Signal | None: ...
```

Both produce `Edge`/`Signal` objects with `source="baseball"` so per-source Brier tracks separately on the Calibration page.

#### Matching logic

Same shape as `find_sports_edges`:

1. Fetch today's slate via `baseball.today_schedule()`.
2. Skip games with status `"Final"` (no model edge after resolution).
3. For each market row: build a team-alias set from each game's `home_abbr` / `home_name` / `short_name` (and away equivalents); locate the earliest alias occurrence in the question text.
4. **YES convention**: the team appearing first in the question is YES (same convention `find_sports_edges` already uses).
5. Skip **derivative-market keywords** (O/U, spread, props, total runs, etc.) — reuse the existing `_DERIVATIVE_KEYWORDS` regex from `find_sports_edges`. The win-probability model means nothing on a "total runs over 8.5" market.
6. **Ambiguous matches skip silently** — if a question mentions both teams from two different games, we can't decide which (same rule as sports).
7. Pull `home_win_prob` from the matched game's stored prediction; if absent, fall back to `baseball.predict_game(home, away)` (which computes on demand).
8. Compute `gap = model_p - market_yes_p`. If `|gap| < min_gap`, skip. Else emit an `Edge` with `direction = "YES" if gap > 0 else "NO"`.

#### Relationship to `find_sports_edges`

The two sources run in parallel on the same MLB markets — `find_sports_edges` uses ESPN's consensus-sportsbook fair probability, `find_baseball_edges` uses our own model. They produce separate `source` tags so Brier-by-source on the Calibration page tells us **head-to-head whether our model beats the consensus market** on the same games. That comparison is the actual validation goal — beating the market is the only way the baseball sidecar earns its keep.

#### Registration

`find_baseball_edges` registers into `edges._SOURCES` (so it appears in the Recommendations page). `strategy_baseball_winprob` registers into `bt.STRATEGIES["baseball_winprob"]` (so it appears in the Backtest page dropdown).

#### Backtest caveat

Identical to `sports_winprob`: our state only contains forward predictions (today + tomorrow). For walk-forward backtest the strategy returns `None` until we add a historical-schedule replay path. Live edge detection works today; backtest validation requires either accumulating live predictions over weeks or building the replay layer.

#### Source-tag conventions

- Calibration log: `source="baseball"` (short, matches the existing `crypto_spot` / `sports` style).
- Backtest registry key: `"baseball_winprob"` (mirrors `sports_winprob`).
- Strategy display name: `"Baseball model (win-probability)"`.

---

## Module: `pm/arms/pre_position.py`

In-game pre-positioning strategy that uses `baseball.wpa` to compute
target prices *before* scoring plays occur. The thesis: the Kalshi
market reprices scoring plays in <10 seconds (95%+ of the move done by
the first observation), so reactive trading captures little edge. By
pre-computing "fair price if home/away scores" via the WPA model and
setting limit orders at a fraction of that delta, we aim to catch the
repricing transition.

**Key functions**:
- `LiveGameState` — current state of one in-progress game (inning,
  half, outs, bases, score). Built from the latest play in `live_plays`.
- `compute_targets(game)` → `PrePositionTarget` — computes `current_wp`,
  `wp_if_home_scores`, `wp_if_away_scores`, and limit-order targets at
  `current_mid + delta × discount` where `discount=0.25`.
- `evaluate_scoring_play(target, game_pk, home_scored, book_pts)` —
  after a scoring play, evaluates both pre-position and reactive
  approaches against the actual book trajectory.

**Targeting criteria**: close games (`|score_diff| ≤ 2`) at inning 4+.

**Log**: every cycle appends to `data/shared/pre_position_log.jsonl`
with two record types — `target` (per-cycle game state + computed
prices) and `evaluation` (post-scoring-play comparison).

**Service**: `pre-position.service`, 10s cycle. Only logs when
`games_targetable > 0` or `scoring_plays > 0` (quiet otherwise).

---

## Module: `pm/capture/kalshi_ws.py`

WebSocket streamer for real-time Kalshi order book updates. Replaces
the 15s REST polling of `pm/capture/mlb_book_poller.py` (which still runs as a
fallback). Sub-100ms delta application; ~85 MLB tickers on one connection.

**Auth**: reuses `kalshi_auth._sign("GET", "/trade-api/ws/v2")` to
build the three handshake headers (RSA-PSS-SHA256 signature).

**Data flow**: connect → subscribe to `orderbook_delta` for all
auto-discovered MLB tickers → receive `orderbook_snapshot` per ticker
→ apply incremental `orderbook_delta` updates to in-memory `LiveBook`
→ flush snapshots to `book_archive.sqlite` every 15s.

**Key types**:
- `LiveBook` — in-memory book per ticker. Methods: `apply_snapshot`,
  `apply_delta`, properties `best_bid`, `best_ask`, `mid`.
- `OrderBookStream` — connection manager with auto-reconnect. Public
  methods: `get_book(ticker)`, `get_mid(ticker)`, `get_all_mids()`.

**Service**: `kalshi-ws.service`. Logs stats every 60s.

---

## Module: `pm/mention/audio_transcribe.py`

Reusable real-time audio→text via faster-whisper on GPU. Captures any
audio source via ffmpeg (URL, file, PulseAudio device), chunks to
16kHz mono PCM, transcribes with VAD filtering.

**Key class**: `AudioTranscriber(model_size, device, chunk_seconds)`.
Methods: `transcribe_stream(source)` yields `TranscriptSegment` objects
(text, start_time, end_time, confidence). `transcribe_file(path)`
convenience wrapper.

**CLI**: `python audio_transcribe.py "https://stream-url" --model small`.

**Performance**: 5s chunks processed in <1s on RTX 5000 Ada with the
`small` model. Total latency per chunk: ~6 seconds (5s capture + 1s
transcribe + VAD overhead).

Not yet wired to any production daemon — exists as the foundation for
broadcast monitoring.

---

## Module: `broadcast_events.py`

> **Retired to `attic/broadcast_events.py` on 2026-08-17** (no importer, no test, the radio-driven pre-positioning path never shipped). Section kept for the design record.

Pattern-based NLP event extractor for baseball radio transcripts.
Sits between `audio_transcribe` and (future) pre-positioning trigger
logic.

**Key types**:
- `EventType` (enum) — HOME_RUN, RUN_SCORED, HIT, WALK, STRIKEOUT,
  DOUBLE_PLAY, ERROR, STOLEN_BASE, PITCHING_CHANGE, HIT_BY_PITCH,
  WILD_PITCH, INNING_CHANGE, GAME_OVER, UNKNOWN.
- `BroadcastEvent` — detected event with confidence, runs_on_play,
  is_scoring flag, and player name extraction.
- `EventDeduplicator` — windowed dedup for overlapping audio chunks.

**Pattern design**: ~12 regex patterns checked in order. First match
per event type wins. Handles broadcaster variation ("GONE", "GOODBYE",
"see you later" all → HOME_RUN). Run-count estimation from text
("grand slam" → 4, "two-run shot" → 2).

**CLI**: `python broadcast_events.py` runs a test suite on 14 sample
phrases for verification.

Framework is domain-agnostic — baseball patterns are one instantiation.

---

## App structure — multi-page with `st.navigation`

`app.py` is the entry point and acts as a thin router:

1. Calls `st.set_page_config(...)` once for the whole app
2. Adds the app title to the sidebar
3. Builds `st.navigation([st.Page(...), ...])` with the pages list, marking `pm/ui/pages/markets.py` as `default=True`
4. Calls `pg.run()` to execute the selected page
5. Adds footer chrome (alert badge, render timestamp) to the sidebar *after* `pg.run()` so it appears below page-specific sidebar widgets

Each file in `pages/` runs top-to-bottom when its route is selected — no `if/elif page == ...` switch. They import shared helpers from `pm/ui/shared.py`:

```python
# Inside any pages/*.py
from pm.ui.shared import load_markets, check_alerts_and_notify, PRICE_MIN, PRICE_MAX
```

### shared.py

Provides what every page needs:
- `load_markets(limit=500)` — `@st.cache_data(ttl=60)`, catches `RequestException` and renders a friendly error
- `load_events(limit=500)` — `@st.cache_data(ttl=300)`, used by `load_markets` for category enrichment
- `check_alerts_and_notify(df_markets)` — fires alert toasts; safe to call from any page
- `outcome_price_prefill(...)` — clamped lookup for form-field defaults
- `live_price_panel(tokens)` — `@st.fragment(run_every=3)` live price ticker; see below
- `is_sandbox_active()` — True when the currently-active profile is `sandbox`; safe to call from any page; never raises
- `paper_trading_banner(show_help=True)` — renders the "🧪 PAPER TRADING" banner (no-op if not in sandbox); optional collapsible explainer
- `simulated_watermark(fig)` — adds a diagonal "SIMULATED" annotation to a Plotly figure (no-op if not in sandbox)
- Constants: `PRICE_MIN`, `PRICE_MAX`, `KELLY_P_MIN`, `KELLY_P_MAX`, `LIVE_TICK_SECONDS`, `SANDBOX_PROFILE`

### Live price ticker (`live_price_panel`)

A live-updating ticker that re-fetches midpoints for the selected market's outcomes every `LIVE_TICK_SECONDS` (default 3s) without re-rendering the rest of the page. Implemented via `@st.fragment(run_every=...)` — Streamlit re-runs just the decorated function on its own schedule.

This is **REST polling, not WebSocket push.** Polymarket exposes a WebSocket endpoint (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) but integrating it cleanly with Streamlit's request-response model requires a `@st.cache_resource` singleton + background thread + thread-safe state — meaningful complexity. For a single-user local app at ~0.3 req/sec/outcome, REST polling delivers indistinguishable UX with no threading or connection management.

**Upgrade path to true WebSocket**: implement a `LivePriceFeed` singleton (background thread maintaining the WS connection, thread-safe dict of latest prices, reconnect logic) and swap the body of `live_price_panel` to read from it instead of calling `pm.get_midpoint()`. The fragment-as-UI-pattern stays the same.

State note: the previous tick's prices are kept in `st.session_state["live_price_last_tick"]` so the ticker can show a delta vs. the previous render.

The single cache entry for `load_markets()` is shared across all pages. Pages invalidate it with `st.cache_data.clear()` after mutations (buy/sell/delete) then call `st.rerun()`.

### Pages

| File | Module dependencies |
|---|---|
| `pm/ui/pages/markets.py`       | `shared`, `polymarket_client` indirectly |
| `pm/ui/pages/price_history.py` | `shared`, `polymarket_client.get_market`/`get_price_history`/`parse_order_book`/`order_book_stats` |
| `pm/ui/pages/scanner.py`       | `shared`, `polymarket_client.scan_*` |
| `pm/ui/pages/kelly.py`         | `shared`, `polymarket_client.kelly_sizing`, `calibration.add_forecast` |
| `pm/ui/pages/portfolio.py`     | `shared`, `portfolio.*` |
| `pm/ui/pages/calibration.py`   | `calibration.*` |
| `pm/ui/pages/alerts.py`        | `shared`, `alerts.*` |
| `pm/ui/pages/settings.py`      | `risk.*` |

---

## Adding a New Page

1. Create `pm/ui/pages/your_page.py`. The file runs top-to-bottom when selected; start with `st.title(...)`.
2. Import what you need from `shared` (`load_markets`, `check_alerts_and_notify`, `PRICE_MIN/MAX`, etc.) and from the relevant data module (`polymarket_client`, `portfolio`, `calibration`, `alerts`).
3. Add a new `st.Page("pm/ui/pages/your_page.py", title="🎯 Your Page")` entry to the list in `app.py`.
4. Add any new analysis functions to the appropriate data module, not inline in the page file. Pages should stay presentational.

You can use `st.sidebar.*` calls inside the page to add page-specific sidebar controls (the Scanner uses this for "Min liquidity").

## Adding a New Scanner

1. Write a function in `pm/venue/polymarket_client.py` with signature `(df: pd.DataFrame, ...) -> pd.DataFrame`.
2. Add a new tab in the `⚡ Arb Scanner` page block in `app.py`.
3. Document inputs/outputs here.

## Extending the Portfolio

The schema is now transaction-based. To add a new field (e.g. a note per transaction):
1. Add the field to the dict in `record_transaction()`.
2. Update the form in `app.py` to capture it.
3. Update `get_transactions()` and the History tab's display columns.
4. Older `portfolio.json` rows missing the field will have `NaN` — handle with `.fillna()` or treat as optional in the UI.

For features that derive from transactions (e.g. realized P&L curve over time), extend `_replay()` to produce additional state.

## Extending Calibration

The forecast schema is simpler. To add a field:
1. Add to the dict in `add_forecast()`.
2. Update the table display in the Calibration page.

For new scoring metrics (e.g. log-loss, calibration error stats), add a function in `pm/ledger/calibration.py` that takes the resolved subset of `get_forecasts()` and returns a float or DataFrame.

---

## Testing

### Running the tests

```bash
make check         # everything CI runs: ruff + mypy + pytest
make test          # pytest + doctests + coverage report
make test-fast     # no coverage, faster feedback loop
make lint          # ruff check .
make typecheck     # mypy on the four non-Streamlit modules
make coverage      # run tests then open HTML coverage report in browser
make smoke         # live-API smoke test (hits real Kalshi endpoints)
```

### Live-API smoke test

`scripts/smoke_test.py` (`make smoke`) is a complement to the unit suite — it exercises the real Kalshi API + filesystem so integration issues that mocked tests miss surface fast. 17 checks cover: profile lifecycle, both market-fetch paths (volume-ordered + closing-soon), categories, auth (configured / get_balance / get_authed_positions / paper-profile lock), Recommendations end-to-end, order pre-flight guards, lookup-by-URL → event sub-markets. When `KALSHI_LIVE_ENABLED=true` it additionally places + cancels one 1-contract order against a real market to verify the order-body contract.

Run before declaring a release "good." This is what caught the `time_in_force` value-validation issue that would have rejected every live order; the unit tests had passed because they mocked `authed_post`.

### Lint

`ruff` is configured in `pyproject.toml` under `[tool.ruff]`. Selected rule sets:

- `E`, `W` — pycodestyle (whitespace, indentation)
- `F` — pyflakes (real bugs: unused imports, undefined names)
- `I` — import sorting
- `UP` — pyupgrade (modernizes old idioms)
- `B` — bugbear (likely-bug patterns)

`E501` (line too long) is disabled — we trust the formatter. Tests get extra leniency on unused imports (fixture imports look unused to ruff but are required by pytest).

### Type checks

`mypy` runs in *permissive* mode (`disallow_untyped_defs = false`) — we don't require annotations everywhere, just catch obvious type bugs. `app.py` and tests are excluded; Streamlit and Plotly aren't typed and would generate noise.

Run on the core modules only:

```bash
mypy polymarket_client.py portfolio.py calibration.py alerts.py
```

Currently the core modules pass with zero errors.

Or call pytest directly:

```bash
pytest                          # everything
pytest tests/test_kelly.py      # one file
pytest -k "test_yes_edge"       # one test by name
pytest --no-cov -q              # quiet, no coverage
```

### Test structure

```
tests/
├── conftest.py            # shared fixtures: portfolio_path, sample_markets_df
├── test_kelly.py          # kelly_sizing math, boundaries, dollar helpers, parametrized
├── test_client.py         # category inference, price parsing, scanners,
│                          # mocked get_markets/get_market/get_price_history,
│                          # _get retry behavior, regression: get_market
│                          # uses `condition_ids` snake_case
├── test_portfolio.py      # transaction CRUD, derived positions, enrichment,
│                          # backward-compat aliases, regression for JSON-string bug
├── test_transactions.py   # transaction model: validation, averaging, partial sells,
│                          # realized P&L math, v1→v2 migration
├── test_calibration.py    # forecast CRUD, Brier score, calibration bins, summary
└── test_alerts.py         # alert CRUD, threshold semantics, double-fire guard,
                           # missing-market handling, summary
```

### Coverage target

`pyproject.toml` enforces **80% minimum coverage** (`fail_under = 80`). Current coverage is ~96%. The uncovered lines are mostly defensive branches that need a flaky network or invalid JSON to exercise.

### Living docs — doctests

Any function with an `Examples` section in its docstring is also a test. `pytest` discovers and runs them automatically via `--doctest-modules`. This means:

- **If you change a function's behavior, the docstring example must be updated too.** The CI will fail if it drifts.
- When adding new public functions, add at least one `>>>` example to the docstring.
- Doctests live in `pm/venue/polymarket_client.py` and `pm/ledger/calibration.py` for pure functions. API functions are not doctested because they need network access.

Example doctest block (from `kelly_sizing`):

```python
>>> r = kelly_sizing(market_price=0.30, your_probability=0.50)
>>> r['direction']
'YES'
>>> round(r['kelly_fraction'], 4)
0.2857
```

### Mocking API calls

API functions are tested with `unittest.mock.patch("polymarket_client._get", ...)`. All HTTP goes through the single `_get()` function, so patching it intercepts everything:

```python
from unittest.mock import patch
from polymarket_client import get_markets

@patch("polymarket_client._get")
def test_something(mock_get):
    mock_get.return_value = [{"conditionId": "0xabc", ...}]
    df = get_markets(limit=1)
    assert len(df) == 1
```

### Adding tests for new features

- **Pure functions** (math, data transforms): add to `test_client.py` or a new file. No mocking needed.
- **API functions**: mock `_get` in `test_client.py`. Provide the minimal dict that the function reads.
- **Portfolio operations**: use the `portfolio_path` fixture from `conftest.py` — monkeypatches `PORTFOLIO_FILE` to a temp file so tests don't touch your real data.
- **Calibration operations**: use the `forecasts_path` fixture (defined in `tests/test_calibration.py`) — same pattern, redirects `FORECASTS_FILE`.
- **New scanner functions**: add a row to `sample_markets_df` in `conftest.py` that exercises the new signal, then write assertions in `test_client.py`.

---

## Running in Development

```bash
# Auto-reload on file save (default Streamlit behavior)
./run.sh

# Or call streamlit directly after activating .venv
streamlit run app.py

# Disable browser auto-open
streamlit run app.py --server.headless true

# Custom port
./run.sh --server.port 8502
```

Streamlit re-runs the entire `app.py` on every user interaction. Keep expensive work inside `@st.cache_data` functions.

## Configuration via environment variables

All sensitive or environment-specific settings come from env vars with sensible defaults. The app works with everything unset — these are all optional overrides.

| Variable | Default | What it does |
|---|---|---|
| `POLYMARKET_GAMMA_BASE` | `https://gamma-api.polymarket.com` | Gamma API base URL |
| `POLYMARKET_CLOB_BASE` | `https://clob.polymarket.com` | CLOB API base URL |
| `POLYMARKET_HTTP_TIMEOUT` | `15` | HTTP request timeout in seconds |
| `POLYMARKET_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `POLYMARKET_PORTFOLIO_FILE` | `./portfolio.json` | Where to store the transaction log |
| `POLYMARKET_FORECASTS_FILE` | `./forecasts.json` | Where to store calibration forecasts |
| `POLYMARKET_ALERTS_FILE` | `./alerts.json` | Where to store price alerts |

`app.py` calls `dotenv.load_dotenv()` at startup, so `.env` in the project directory works automatically. See `.env.example`.

## Structured logging

Every module has a module-level logger via `log = logging.getLogger(__name__)`. Calls:

- **`log.info`** at state-changing moments — transaction recorded, forecast saved, alert created, alerts fired, file migration applied.
- **`log.warning`** when something is recoverable but worth noticing — rate-limited (429), midpoint fetch failed, mismatched market returned from `/markets`, unknown ID supplied to `record_resolution`.

The basicConfig is set up in `app.py` based on `POLYMARKET_LOG_LEVEL`. Default format includes timestamp + level + logger name, e.g.:

```
20:14:33 [INFO] portfolio: recorded buy: 100 shares of Yes on 0xa0f4c4924ea1a8b @ $0.0135
20:14:35 [INFO] alerts: alerts fired: 2
```

## Pre-commit hooks

`.pre-commit-config.yaml` runs ruff + mypy on every `git commit`, plus basic housekeeping (trailing whitespace, YAML/TOML validity, large-file blocking). To activate:

```bash
pip install pre-commit
pre-commit install
```

After that, commits are auto-lint-checked. Skip with `--no-verify` only when deliberate — CI enforces the same gates.

## Documentation Maintenance

This project has **living documentation**. When you ship a change, the docs are part of the change — not a follow-up. The expectation is that anyone (human or agent) reading the docs sees the current truth.

### Rule of thumb: every code change touches at least one doc

| You changed... | Update... |
|---|---|
| Added or removed a UI page or control | `docs/user-guide.md`, `README.md` feature table |
| Added or changed a public function | `docs/developer-guide.md`, `docs/api-surface.md` |
| Added or changed a DataFrame column / schema | `docs/developer-guide.md`, `docs/file-map.md` DataFrame references |
| Discovered a Polymarket API quirk | `docs/api-reference.md`, `docs/file-map.md` Critical API gotchas |
| Learned *why* a module works the way it does (a measurement, a falsification, a trap) | `docs/module-notes.md`, linked as ↳ from the `docs/file-map.md` file-map row |
| Introduced a new domain term in the UI | `docs/glossary.md` |
| Shipped a TODO item | Move it to ✅ Done in `TODO.md` with the commit SHA |
| Added or renamed a file at the root | `README.md` Project Layout, `docs/file-map.md` File map (one or two lines; the long form goes in `docs/module-notes.md`) |
| Changed how to run, test, or deploy | `README.md`, `docs/file-map.md` Running/Testing sections |

### How to keep things honest

1. **Docstrings with `Examples` blocks are doctests.** They run as part of `pytest`. If a docstring example breaks, CI fails — so docs can't silently drift from code for any documented example.
2. **Function signatures in `docs/api-surface.md`** are duplicated from the code. When you change a signature, update both.
3. **Schema tables in `developer-guide.md` and `docs/file-map.md`** are duplicated from the code. When you add or rename a column, update both.
3a. **`docs/file-map.md` file-map rows stay to a line or two.** That file is an index and only works if it can be read end to end; it reached 158 KB once by absorbing rationale a row at a time. Put the rationale in `docs/module-notes.md` and link it.
4. **`TODO.md` ✅ Done section** is the running changelog. Move shipped items there with the commit SHA so future maintainers can trace decisions.

If the doc and code disagree, the code is right but **fix the doc in the same commit**. Documentation debt compounds faster than code debt.

---

## Dependencies

| Package | Role |
|---|---|
| streamlit | UI framework |
| pandas | DataFrame operations |
| numpy | math (used in Kelly curve plot) |
| plotly | Interactive charts |
| matplotlib | Reserved — currently not used at runtime |
| requests | HTTP client |
| python-dateutil | Date parsing utilities |
| pytest, pytest-cov | Testing (dev only) |

Python 3.11+ required (uses `X | Y` union type syntax in annotations).
