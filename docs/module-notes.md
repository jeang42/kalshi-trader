# Module notes — the long form

`docs/file-map.md`'s file map is the index: one line per file, enough to know what a
module is and whether you need it. **This file is the rationale** — why a thing
was built, what it measured, what falsified it, and the traps inside it. Split
out of `docs/file-map.md` on 2026-08-23 (R-25) because the index had grown to 91 KB of
prose and stopped working as an index.

Order matches the file map. A module with no section here is fully described by
its file-map row. When you change a module's behaviour, update whichever of the
two describes it — and if the change invalidates a *finding* recorded below,
say so here rather than deleting the finding.

### `pm/venue/kalshi_orders.py`

Phase-4 order placement. `place_limit_order` / `cancel_order` / `get_resting_orders` + `pre_flight_check` returning `(severity, msg)` tuples (stake cap, daily cap, fat-finger price band, drawdown circuit-breaker). Gated by `KALSHI_LIVE_ENABLED=true` *and* `kalshi_auth.is_configured()`. Every attempt audit-logged JSONL to `data/shared/orders.log`. No market orders, no batches.

**2026-09-09 — exchange sharding.** Kalshi split the venue into
`exchange_index` shards (0 Default / 1 Combos / 2 Crypto / 3 Tennis &
Baseball) each holding its own balance. An order for a market on an
unfunded shard returns a bare `404 user_not_found` naming nothing — the
third time this venue has changed the order contract silently (after the v2
deprecation and the dotted-`client_order_id` 400). The error path now
detects `user_not_found` and logs what it actually means; `live_preflight`
reports funded vs unfunded shards so it is caught before an order rather
than by one. Found by the live canary at a cost of $0 — the order was
rejected, nothing filled. Every research target we care about is index 0.

**2026-09-20 — the audit log moved to `data/shared/audit/orders.log`, and the
reason is worth keeping.** `scripts/logrotate.conf` globs
`data/shared/*.log` daily with `rotate 14` + `copytruncate`. That rule had
been quietly truncating this append-only real-money audit trail every night
and deleting every generation past two weeks — **all order history before
~2026-07-11 is permanently gone**. It surfaced only because a settlement
attribution came back wrong: every trade looked like the user's manual
activity because `placed_tickers()` read an empty file. (It survives partially
only because a past session had already taught `placed_tickers()` to read the
rotated + `.gz` siblings; the rest had to be reconstructed from
`fade_retail.sqlite`'s `live_order_id`.)

The fix is **positional, not an exclusion**, and that distinction was
measured rather than assumed. logrotate 3.21 treats a specific-stanza + glob
overlap as a FATAL duplicate: adding an `orders.log` stanza made it stop
processing the pattern, dropping **28 of 47** other logs from rotation and
exiting 1 — trading an audit-log bug for a disk-fill bug. Since glob(3)'s
`*` does not cross a `/`, one directory down is genuinely unreachable, and
no future log added to `data/shared/` can take this one with it.
`tests/test_pm_layout.py::test_order_audit_log_is_out_of_logrotate_reach`
enforces it with real glob semantics (fnmatch is wrong here — its `*` spans
separators and false-matches the very layout that fixes the bug), and the
guard was mutation-tested in both directions.

The 15 existing generations were moved intact (480 audit rows, verified by
line count) and deliberately NOT consolidated into one file: `placed_tickers()`
already reads them all, so there is no functional gain, and rewriting a
financial audit trail for tidiness is not a trade worth making.


### `pm/venue/kalshi_auth.py`

Authenticated Kalshi HTTP layer. Loads `KALSHI_API_KEY_ID` + RSA PEM at `KALSHI_PRIVATE_KEY_PATH`. RSA-PSS-SHA256 signs `{ts}{METHOD}{path}` per request. `is_configured()` returns True only when env+file are present AND the active profile is **authorized** — `authorized_profiles()` = `KALSHI_OWNER_PROFILE` (default `jeff`) **∪ `LIVE_TRADING_PROFILES`** (so multiple live arms each authenticate against the one account; paper profiles outside the set stay locked out). `authed_get` / `authed_paginate` mirror the public `_get` retry+429 semantics. Refuses to load a PEM with loose perms.

### `pm/trading/live_preflight.py`

**Read-only live-API preflight** — `python -m pm.trading.live_preflight [--profile P]` once credentials are installed (loads `.env`; needs `kalshi_auth.is_configured()`, NOT `KALSHI_LIVE_ENABLED`, so it places nothing). Calls only authed *read* endpoints (`get_balance`/`get_authed_positions`/`get_fills`/`get_resting_orders`) + the `execution.LiveBroker` read paths, verifying the owner-profile lock resolves and our field-name assumptions match live response shapes BEFORE any order. `--profile` sets `POLYMARKET_ACTIVE_PROFILE` to simulate the daemon's owner identity. Step 0 of the go-live runbook.

### `go_live.sh`

**Guarded go-live enabler.** `./go_live.sh --profile agentic_favorite_hold` = dry-run (runs the preflight for that profile, changes nothing). `--enable` (+ `--service`, `--max-stake`) = only if preflight passes, upserts `KALSHI_LIVE_ENABLED=true` / `KALSHI_OWNER_PROFILE` / `LIVE_TRADING_PROFILES` / `LIVE_MAX_STAKE_DOLLARS` into `.env` and restarts the service. Refuses to enable on a failed preflight. Owner-claim: the live account has one owner profile; taking a daemon live points the owner at it.

### `pm/trading/live_status.py`

**Read-only live account readout** — `python -m pm.trading.live_status --profile <owner> [--watch N] [--all]`. Loads `.env`; shows settled cash + pending payout, open real positions (entry from fills vs current mark + unrealized P&L), and recent settlements (won/lost by revenue + realized P&L). Scopes to tickers our system placed by default (`placed_tickers` reads `orders.log` **+ its rotated copies** `orders.log.1`/`.*.gz` — logrotate truncates the live file daily, so without the rotated reads the scope would be empty after a rotation) — `--all` shows the whole Kalshi account. `cost_basis_by_ticker` is a pure, doctested helper. Use to watch a live canary resolve.

### `pm/trading/execution.py`

**Execution adapter — the seam between strategy decisions and order fills.** **Drawdown circuit breaker wired 2026-08-08 (P0):** `_place` passed `in_drawdown_pause=False` with a TODO from the day it was written, so the one guardrail designed to halt a *losing* automated arm protected only the manual UI — while the arms it guards grew for eight more weeks and then went live. Now feeds `_in_drawdown_pause()`, which measures **account-level** mark-to-market equity (`account_equity_dollars` = settled cash + pending payout + Σ position market value; both calls the live path already makes) against a persisted high-water mark in `data/shared/live_equity.json` (env `LIVE_EQUITY_STATE_FILE`). Account-level is deliberate: several arms plus hand-trading share one account, so per-arm equity is undefined — and it doubles as the fleet-wide exposure cap that was missing (each arm previously enforced only its own per-order/per-day caps, so N arms meant N ceilings and no aggregate limit). **Fails CLOSED** — unknown equity or an unusable state file blocks orders, mirroring `emergency_stop.is_active()` returning ACTIVE on `OSError`. The peak persists across restarts so a crash-loop cannot reset the breaker. Pure `evaluate_drawdown()` is doctested; `LiveExecConfig.enforce_drawdown` / `drawdown_pause_pct` tune it. **Caveat: a bad stretch of hand-trading can pause the automated arms — intended, it is one capital pool.** `Broker` Protocol + `PaperBroker` (delegates to `portfolio.add_buy/sell` exactly as today — the default) + `LiveBroker` (real Kalshi limit orders via `kalshi_orders` + pre-flight + fill reconciliation; reads position/cash truth from `kalshi_client.get_authed_positions`/`get_balance`). Daemons call `get_broker(profile)` and route buy/sell/get_positions/available_cash through it. **Triple-gated to live**: `LIVE_TRADING_PROFILES` allow-list AND `kalshi_orders.is_trading_enabled()` AND not emergency-stopped — else PaperBroker. **`_place` is a `shutdown.deferred` critical section** (R-20): a SIGTERM between the venue accepting an order and the caller recording it is held until the record lands. **Dormant by default** (allow-list empty → all paper). Live favorite-hold is buy-only (Kalshi auto-settles resolutions via `closes_settle_on_venue`). **Resting cash-out limit** for live scalp modes (`rests_take_profit`): `place_resting_take_profit`/`cancel`/`fills_for_order` let a scalp rest its +25% take-profit on the venue (zero poll latency); `longshot_scalper` arms it on entry, reconciles fills as `scalp_take_profit` (phase 0), skips the polled TP in live, and cancels it before any stop/flip/resolution close — all gated on `rests_take_profit` so paper + favorite-hold are unaffected. The downside stop stays polled+marketable (Kalshi has no stop order). **Settlement reconciler** (`_reconcile_settlements`, live-only phase 1b): logs positions that SETTLED on the venue before the fast in-cycle resolver caught them (Kalshi can settle seconds after a game ends, faster than the cycle → the position would orphan in `position_meta` and silently drop from the exits.jsonl track record, biasing it optimistically by missing losses). Reads Kalshi `get_settlements` (ground truth), logs `resolution_won`/`lost` + clears meta; no double-log (the fast path clears meta when it logs). **Position-scoping** (`LiveExecConfig.scope_to_own_positions`, default on): a live arm's `get_positions`/`available_cash` see ONLY positions it opened (tracked in its profile-pinned `position_meta` via `_own_tickers`) + its own deployed cost — so multiple live arms can share one account without an arm managing another's (or your hand-traded) holds. Wired into `longshot_scalper.py`; `pm/arms/strategy_runner.py` is the next wiring target. Full design + runbook: `docs/live-execution-adapter.md`.

### `pm/core/atomic_io.py`

**Crash-safe + concurrency-safe JSON persistence for the per-profile ledgers** (2026-08-08, P0). Replaces the `open(path,"w")` + `json.dump` pattern that truncated the file before writing a byte: a crash or full disk left a torn file, and the next read raised `JSONDecodeError` into a `Restart=on-failure` crash-loop. `write_json` = temp file → `fsync` → `os.replace` (atomic on POSIX) → dir fsync, retaining the prior contents as `.bak`; serialization happens BEFORE anything on disk is touched, so an unserializable value cannot destroy good data. `read_json` is tolerant — falls back to `.bak`, quarantines the damaged copy as `.corrupt-<epoch>`, never raises. **`mutate(path, default)` is the API mutation sites must use**: it holds an advisory `flock` across the whole read-modify-write, which is what closes the *lost-update* race (atomicity alone does not — `snapshot_runner` walks every profile 4x/day while each arm writes its own on a 15-min cycle, so two readers could each write and drop the other's transaction). Lock acquisition times out (10s) and proceeds with a loud warning rather than blocking: failing to record a fill already placed is worse than losing an update. Wired into `portfolio`, `calibration`, `alerts`, `position_meta`, `risk`. Regression-tested across real processes (`tests/test_atomic_io.py` — with the lock removed the test loses 30 of 45 appends).

**R-27 (2026-08-24)**: the concurrency claims are now proven at the level production uses, not just at the primitive — `tests/test_concurrency.py` hammers `portfolio.record_transaction` and `position_meta` from real processes, SIGKILLs a holder mid-`mutate()` (store keeps pre-crash contents; the next writer proceeds immediately because flock dies with its holder), and proves plain `json.load` readers never see a torn document. Mutation-tested: neuter the flock and the ledger tests fail deterministically with lost updates.

### `pm/core/sqlite_schema.py`

**Schema versioning for the arm/capture/archive SQLite stores** (P2/R-19, 2026-08-17). The sidecars versioned their state (`PRAGMA user_version` + `migrate()`); the thirteen root stores did not — each `_con()` ran `CREATE IF NOT EXISTS` + try/except `ADD COLUMN`, so a store's migration generation was unknowable and nothing could ever tighten a schema. Now every store declares an ordered `SCHEMA = [Migration(1,…), Migration(2,…)]` and calls `ensure_schema(conn, store=…, migrations=SCHEMA, db_path=…)` on every connection (one PRAGMA read when nothing is pending). Each migration runs in its own `BEGIN IMMEDIATE` transaction and stamps `user_version` in the same transaction; a rebuild step (`backup=True`) first copies the file to `<db>.pre-v<n>.bak` via the online backup API and refuses above 64 MB. **Fails closed**: a file at a HIGHER version than the code knows raises `SchemaError`; a failing step rolls back. Race-safe across processes (re-checks the version under the write lock — an arm and the control plane open the same store). **State today**: the arms are at v3 (`positions` rebuilt with `CHECK`/`NOT NULL`: prices in [0,1], cost in (0,1], count>0, resolved∈{0,1}, and the invariant *resolved ⇔ P&L+closed_at present* — `fade_retail`'s take-profit exit left `resolved_yes` NULL, so its invariant differed). Two of those four arms (`fade_props`, `fade_retail_longshot`) were retired to `attic/` on 2026-08-23; their stores stay at v3 and are retained as evidence, and their schema tests moved to `attic/tests/test_fade_retail_props.py`. The nine capture/archive stores are at v1 (adopted verbatim — constraints deferred because they need a rebuild, which a capture daemon must not do on the way to its next cycle; `book_archive` is 41 GB). The arms' `INSERT OR IGNORE` now checks `rowcount` — SQLite's IGNORE swallows CHECK violations as silently as PK duplicates, and `fade_retail` fired its live canary after the insert regardless. Helpers: `add_column_if_missing`, `rebuild_table`, `table_columns`, `user_version`. Verified on copies of the live arm stores before shipping (rows/P&L/resolved identical, ~10 ms). **Adding a store**: declare `SCHEMA`, call `ensure_schema` in `_con()`, add it to `tests/test_store_schemas.py`. **R-27 (2026-08-24)**: the claimed cross-process init race (arm + control plane migrating one fresh store together) is now actually exercised — six processes race 0→2 in `tests/test_concurrency.py` — and so is the fact that writer contention is absorbed by stdlib `sqlite3.connect`'s DEFAULT `timeout=5.0` busy handler: nothing in the repo sets `busy_timeout`, so that default is load-bearing and pinned by test.

### `pm/core/shutdown.py`

**Cooperative shutdown for the daemons** (P2/R-20, 2026-08-17). Before this no daemon handled signals: `systemctl stop` sent SIGTERM and Python died wherever it was — which could be between `place_limit_order` returning and the arm recording the fill (its `UPDATE positions`, `position_meta.record_entry`, the exits log), leaving a real position nothing on our side knew about (`LiveBroker._reconcile_after_error` covers a *raising* submission, not a vanishing process). `install()` in a daemon's `main()`: first SIGTERM/SIGINT sets a stop flag and logs, second escalates to `SystemExit`. `requested()` / `sleep(s)` are what every loop checks — between cycles AND at the top of each per-market inner loop (a `candle_panel` cycle runs far longer than systemd's 90 s stop timeout). `deferred(label)` is a re-entrant context manager that *masks* the signal across a place-then-record window: `LiveBroker._place` (place → poll fills → cancel remainder) is wrapped, and so are the two arm canaries (`buy` + `UPDATE` + `commit`) and `reactive_scalper`'s entry / manage-exit / resolution-close blocks; a signal arriving inside is honoured on exit. Wired into the seven running daemons (4 arms, `near_close_capture`, `mention_capture`, `candle_panel`) + `reactive_scalper`. NOT wired: `kalshi_ws` (asyncio) and the ⏸ stopped `--loop` runners (`arb_scan`, `news/tennis/wnba_capture`, `book/trade_poller`, `mlb_book_poller`, `pre_position`) — wire them the same way when re-enabling. A mask, not a guarantee: SIGKILL still kills; it closes the window we control.

### `pm/core/logctx.py`

**Structured logging + correlation IDs** (P3/R-23, 2026-08-24). One logical operation — an arm cycle, a live order — logs across several modules plus an `orders.log` audit row, and nothing tied the lines together; "which lines belong to THIS cycle" was timestamp squinting. Stdlib-only by choice (a `ContextVar` + a `logging.Filter` do everything structlog would, without a dependency): `bind(**fields)` adds correlation fields for a block (nests additively, restores on exceptions, isolated across threads/async); `scope(arm=…)` decorates a cycle function so each invocation gets a fresh `cyc-xxxxxxxx` id without re-indenting a 200-line body; `current()` snapshots the fields for embedding in already-structured sinks; `setup()` is the one logging-config call — it replaced **37** slightly-different `logging.basicConfig` copies across the entry points. Text mode appends `[k=v …]` only when context is bound, so quiet paths are byte-identical to pre-R-23; `POLYMARKET_LOG_FORMAT=json` switches the same handler to JSONL (`ts`/`level`/`logger`/`msg`/`ctx`). **The highest-value consumer is `kalshi_orders._audit`**: audit rows now embed `ctx`, so a real order's row names the arm + cycle that placed it (R-18's commit stamp says what code ran; this says which run of it), and `execution._place` binds `ticker`/`action`/a fresh `order` id at the one choke point every live order passes — the submit/accepted/cancel rows of one placement share an id. Deliberately NOT here: cross-process propagation (daemons are single-process; the control plane reads stores, not logs) and any reframing of `health_events.jsonl`/`orders.log` (already structured). **Testing trap, learned at cost**: pytest's logging plugin re-attaches its capture handlers between fixture setup and the test call, so a fixture that strips root handlers is undone by test time and `setup()` correctly takes its idempotent no-op branch — strip handlers inside the test body (see `tests/test_logctx.py`).

### `pm/core/provenance.py`

**Which code produced this result?** (P2/R-18, 2026-08-17). No backtest run, report, position row or audit line recorded the commit that produced it, so several logged findings were not exactly reproducible — the parameters were known (`oos_ledger` fingerprints those) but not the code that applied them. `git_commit()` reads the SHA from `.git` (no subprocess; env `PROVENANCE_COMMIT` overrides for a build without one), `is_dirty()` (one cached `git status`), `stamp()` → the dict JSON writers embed, `stamp_line()` → the header every text report starts with, `label()` → `3fc7cc3` / `3fc7cc3+dirty`. Cached per process, never raises. **Stamped now**: every arm position row (`commit_sha` column — schema v3; `commit` is a SQLite keyword), every `orders.log` audit line, every `exits.jsonl` and `reactive_log.jsonl` row, every saved backtest run (`provenance` key), every `data/shared/*_report*.txt` (first line), and the `oos_ledger --status` footer. Rows that predate it read NULL/absent — the honest answer, not a default.

### `pm/core/circuit_breaker.py`

**Circuit breakers on the outbound connectors** (P2/R-21, 2026-08-17). Before: a persistently failing dependency was retried every cycle forever — each `_get` did its 3 retries, failed, the arm logged "discovery failed", slept, repeated; in a 429 storm that makes the storm worse (restarting seven daemons at once on 2026-08-16 did exactly that). One `Breaker` per dependency wraps that connector's single HTTP entry point: **closed** counts consecutive dependency failures (connection/timeout/5xx/429-after-Retry-After/non-JSON — a 4xx other than 429 is a healthy dependency saying no to *this* request and resets the count); after `CIRCUIT_FAILURE_THRESHOLD` (5) it **opens** and calls fail fast with `CircuitOpenError` for `CIRCUIT_COOLDOWN_S` (300 s); then **half-open** lets ONE probe through. `CircuitOpenError` subclasses `requests.RequestException` on purpose — every existing `except` already handles it as "dependency unavailable". Wired: `kalshi_client._get` + `kalshi_auth.authed_get`/`_authed_request` (one shared `kalshi` breaker — an open circuit means an order is NOT placed; the money path fails closed), `polymarket_client._get`, `sports_data` (`espn`), `crypto_data` (`coinbase`), `manifold_data`, `llm_data` (`ollama`), `underlying_data` (`yahoo`), `web_context` (`google_news`, `wikipedia`), `macro_data` (`fred`). Sidecars' own clients are not wired (cron single-shots; the retried-forever pattern is the daemons'). Transitions are logged ONCE and written to the health log (`circuit_open`/`circuit_closed`, `extra.breaker`) — breaker state is per-process, so that log is how `health_digest` sees it: an open with no later close is reported as a **problem**. `status()` for the control plane; `reset_all()` per test in the root `conftest`.

### `pm/trading/live_gates.py`

**"What is armed right now" — the single authoritative answer** (2026-08-08, P0). The live gate chain resolves *differently depending on which process asks*, because `kalshi_auth.authorized_profiles()` compares against the active profile and each daemon pins `POLYMARKET_ACTIVE_PROFILE` for its cycle — so a shell snippet reports every profile paper while the daemons are live. That is how the near-close canary ran for a week while the status notes called it dormant. Resolves the chain per-profile the way a daemon does and prints broker/auth/trade/arm-switch/ORDERS? per profile plus the global switches, emergency stop and drawdown state. `find_problems()` is pure and splits hard **problems** (owner profile doesn't exist, allow-listed profile missing, breaker tripped → exit 1) from **notes** (an allow-listed arm whose own switch is off — usually deliberate, never invisible). CLI: `python -m live_gates [--json|--quiet]`. Read-only.

### `pm/ops/backup_sqlite.py`

**Consistent SQLite snapshots for the nightly backup** (2026-08-08, P0). The backup used to `rsync` the data tree directly; capture services write continuously, so hot databases had non-empty WAL and rsync copied db + `-wal` at different instants — a restored snapshot could be internally inconsistent, and no restore had ever been tested. Uses the online backup API (`Connection.backup`) into a cold staging dir, `PRAGMA integrity_check`s the *staged* copy, and only then lets rsync run — which also restores `--link-dest` hardlinking (staging is cold), so unchanged databases stop being re-copied nightly. Databases above `BACKUP_MAX_SQLITE_MB` (default 2048) are **skipped and named** rather than silently dropped (`BACKUP_FORCE_INCLUDE` overrides) — this is what stops the 43 GB retired-thesis archive costing a full copy every night. `--verify SNAPSHOT_DIR` integrity-checks a stored snapshot and is run automatically after every backup. Measured: 18 dbs staged in ~7s, snapshot 1.8 GB vs 43 GB.

**2026-10-13 — a skip is a failure unless someone said otherwise.** The 2048 MB cap was set when the only over-cap file was the retired 43 GB archive. The candle panel — a *live* study store — grew past it around 2026-10-01 and was skipped for 12 nights while every run wrote `status: ok` and the digest stayed green. Worse, its last staged copy stayed in `.staging`, so every snapshot still *contained* a `candle_panel.sqlite` — frozen at Oct 1. "Skipped and named in the log" was not enough because nobody reads the log on a green night. Now: the default cap is 8192 MB; `--skipped-file` hands the skipped names to `backup.sh`, which writes them into `backup_status.json` as `skipped` / `skipped_expected`; `health_digest.check_backup` **pages** on any `skipped` entry and only notes `skipped_expected` (`BACKUP_SKIP_EXPECTED`, default `book_archive.sqlite` — frozen since 2026-08-11 and already a symlink into cold storage on the backup drive).

### `pm/core/notify.py`

**Operator alerting — the one place anything tells you something** (2026-08-11, P1/R-10). Nothing did before: services ran under `Restart=on-failure` with no `OnFailure=` hook and the control plane was pull-only, so an arm could die at 03:00 and the first sign was a flat P&L days later. Fans out to a JSONL health log (always), a desktop notification (when a session exists) and an **ntfy push to your phone** (when `NTFY_TOPIC` is set). Every channel optional and independent; **nothing can raise** — an alerting path that crashes the thing it watches is worse than none. `unit_failed()` is what the systemd template calls. CLI: `python -m notify --test`. Security model + why ntfy: `docs/push-notifications.md` — a public topic is readable by anyone who knows the name, so bodies carry no balances, sizes or keys.

### `pm/ops/health_digest.py`

**Daily fleet digest — the quieter failure** (P1/R-10). `notify` covers "something crashed"; this covers "nothing crashed but an arm stopped working", which systemd cannot see: a service green while its capture store goes stale. Checks unit liveness, repeated restarts (crash-looping while reporting active), capture freshness, **the nightly backup's recorded outcome**, recent health events, and the live-gate/breaker state. The backup check exists because of 2026-08-25..28: R-24 moved `backup_sqlite` under `pm/ops` and `scripts/backup.sh` didn't `cd` into the repo before `python -m`, so the sqlite stage failed with `No module named 'pm'` for four consecutive nights — while the snapshot DIRECTORY kept appearing (the rsync stages still ran), so no freshness view saw anything wrong. Now `backup.sh` writes `data/shared/backup_status.json` after every run and `check_backup` treats FAILED, stale (>26h) and *missing* all as problems — missing means the backup never completed since the check shipped, or the status write broke; both need eyes. **Pushes only when something needs attention** — a digest that always fires is muted within a week, and then you have the illusion of coverage. `--force` to send anyway, `--print` to look without sending. Timer: `polymarket-health-digest.timer`.

### `pm/ops/settlement_check.py`

**The one leg of a live order a session cannot watch** (2026-09-09). The
plumbing canary that day exercised submit → fill → position → audit, but the
market closed hours later, long after anyone was looking — so "it settled
fine" would have been an assumption. This project's rule is that unwatched
money paths are exactly where silent breakage lives; the nightly backup
failed for four consecutive nights (2026-08-25..28) precisely because nothing
checked its outcome. Answers one question per ticker — settled or pending,
and what the venue actually paid — and `--notify` pushes it. Read-only.

Two design points worth keeping. **Scope defaults to `orders.log`** via
`live_status.placed_tickers()`, because the live Kalshi account is shared with
the user's own recreational trades and a settlement report that included them
would be both wrong and alarming. And **`main()` loads dotenv and pins
`POLYMARKET_ACTIVE_PROFILE` itself** — under systemd there is no
`EnvironmentFile`, so without the first `NTFY_TOPIC` is absent and the push
goes nowhere (gotcha 4, which already bit `storm_watch`), and without the
second the owner-lock refuses the authed reads (gotcha 5).

Scheduled ad hoc rather than as a fleet unit: `systemd-run --user
--on-calendar=... --working-directory=<repo>` gives a one-shot, self-cleaning
timer. The `--working-directory` is not optional — `python -m pm.<x>` resolves
the package from the CWD (gotcha 9).

**`--wait-hours` exists because the first scheduling attempt failed** (2026-09-10).
The canary settled at 12:09:33 UTC; the one-shot timer fired at 12:00:13 and
pushed "pending" — useless, and off by nine minutes despite the time being
chosen deliberately to be safely late. Guessing a settlement time is the wrong
shape. `--wait-hours N --poll-minutes M` keeps the process alive, re-checking
until nothing is pending, then notifies once. It honours `shutdown` so a
SIGTERM during the wait exits cleanly.

### `pm/core/web_auth.py`

**Shared-secret gate for the four LAN-facing UIs** (P1/R-14). They bound `0.0.0.0` with no authentication of any kind, and the control plane renders the real account balance. Query token on first visit → cookie thereafter → bearer for scripts; constant-time compare; `/health.json` left open for monitoring. **A shared secret, NOT authentication** — one token, no users, no revocation — and the module says so rather than implying more. No-op unless `WEB_UI_TOKEN` is set, so adding it cannot break a deployment. **Note**: the web apps now `load_dotenv()` at import — none of them did, and no unit supplies an `EnvironmentFile`, so the token would have been absent in the served process and the gate would have reported itself open while closed.

### `pm/ledger/oos_ledger.py`

**Out-of-sample bookkeeping — evidence vs. the data that made it** (P1/R-13). Every arm's headline P&L was in-sample with respect to filters fitted to its own forward data, and nothing recorded that. Fingerprints each arm's SELECTION parameters (bands, thresholds, exclusions — not stake, not cadence), records when that fingerprint last changed, and splits P&L at the boundary; **a parameter edit resets the evidence clock automatically**. Verdicts count INDEPENDENT CLUSTERS, not positions. `--freeze-at` backdates to a real commit date (used for both near-close arms). Re-fitting is legitimate; counting the old trades as though the new rules predicted them is not. CLI: `python -m oos_ledger --status`.

### `deploy/`

**Fleet topology in version control** (P1/R-11). 27 systemd units (23 services + 4 timers) + the crontab, previously existing only in `~/.config` and no backup. `deploy/install.sh` dry-runs by default, `--install` copies repo→system with a timestamped backup of anything it replaces, `--capture` snapshots live changes back. **The seven daemon units stagger themselves at boot** (`ExecStartPre=/bin/sleep N`, 8 s apart, added 2026-08-17) — simultaneous first cycles 429'd each other on 2026-08-16, and at boot no one is there to space them; a manual restart of a unit is delayed by its N seconds too. **Deliberately never enables or starts anything** — several units are retired because their theses were measured dead, and a script that started what it found would resurrect them.

### `pm/feeds/web_context.py`

Phase-B web-fetched context for Deep Analysis. `search_news(query, max_items)` hits Google News RSS (no auth, no rate limits at our scale); `wikipedia_summary(title)` hits the Wikipedia REST API (UA must include an email per their policy). Both cached in `llm_cache` SQLite (6h for news, 7d for wiki). `format_news_for_prompt` / `format_wiki_for_prompt` build the strings injected into the deep-analysis prompt.

### `pm/feeds/llm_deep_analysis.py`

Slow per-market structured-reasoning pipeline. `analyze_market(provider, df_markets, ticker, model)` gathers price history + market metadata + order book + sibling markets, packs into one prompt, asks Ollama for a JSON `DeepAnalysis` (probability, confidence, key_drivers, key_risks, evidence_used, recommended_action, reasoning). Cached 24h in `llm_cache`. Used by the 🧠 Deep Analysis page; tagged `llm_deep:<model>` in the calibration log so it tracks separately from the fast `llm:<model>` source.

### `pm/feeds/llm_matcher.py`

LLM-assisted semantic question matcher. `verify_match(a, b, model)` → `MatchVerdict`. Reuses the Ollama plumbing in `llm_data` with a stricter system prompt tuned for discrimination, not forecasting. Fixes Jaccard direction-blindness.

### `pm/feeds/llm_cache.py`

Persistent SQLite K/V cache for LLM forecasts and match verdicts. `data/shared/llm_cache.sqlite` (path env-overridable). Cache-by-default in both wrappers; `bypass_cache=True` forces fresh calls. Schema-version prefix in keys for invalidation.

### `edges/`

**Tier 4 external-data edge framework**, and since 2026-08-24 (P3/R-24) a
package rather than a 2,175-line module. The split was mechanical on purpose:
`edges.py` had one framework section and eight source sections that never
referenced each other, so the dependency graph was already a tree and the
package just made it visible.

Layout: `__init__.py` holds the `SOURCES` registry, `find_all_edges`,
`log_edges_to_calibration`/`_source_tag`, `list_sources`/`get_finder`, and
re-exports every public name. `_base.py` holds what two or more sources share —
the `Edge` dataclass, `_severity_for_gap`, `edges_to_dataframe`, and
`_row_to_snapshot` (that last one lived in the crypto section but is used by all
eight sources, so it moved; it is the only symbol that changed neighbours).
Then one module per source: `crypto`, `llm`, `sports`, `baseball`, `wnba`,
`tennis`, `macro`, `manifold`. Only two inter-source edges exist: everything
needs `_base`, and `baseball` reuses `sports._find_first_alias` +
`sports._DERIVATIVE_KEYWORDS`.

**The public API did not change, and that was the acceptance test.** The full
`dir(edges)` surface plus every signature, the `SOURCES` table and the
registered `backtest.STRATEGIES` list were captured before the split and
diffed after; the only differences are the eight submodule names appearing as
package attributes, and the disappearance of leaked implementation imports
(`edges.sports_data`, `edges.re`, `edges.datetime`, …) which were never API and
which nothing in the tree referenced. Every original non-blank source line was
also accounted for in the new files — that check caught a real one-line
truncation (`edges_to_dataframe`'s final `return`) that the empty-frame test
did not, because the empty case returns early.

**The trap, and the reason this is worth reading before you touch it:** tests
patched module internals by string — `patch("edges._baseball_games_for_date")`.
After the split that name is the package's *re-export*; rebinding it does not
affect the caller inside `edges/baseball.py`, so the patch silently stops
stubbing and the test runs the real code path. Here it failed loudly (the
network is blocked in tests), but it is exactly the shape of change that can
turn a test into a no-op. **Patch internals at their real home**:
`edges.baseball._baseball_games_for_date`, `edges.tennis._tennis_pred`.
Patching a name that genuinely lives in `__init__` (`edges.SOURCES`) is
unaffected. `tests/test_baseball_edges.py` and `tests/test_tennis.py` carry a
note saying so.

**Adding a source** is now: write `edges/<name>.py` with a `find_<name>_edges`
and a `strategy_<name>_*`, import it in `__init__.py`, add one row to `SOURCES`.
That is also the sidecar strategy-adapter boundary — see
`docs/sports-model-playbook.md`.

### `pm/capture/advisory_capture.py` + `deploy/systemd/advisory-capture.service`

**Status: ⏸ stopped 2026-09-09, disabled 2026-10-13.** It had only been `stop`ped when candidate A died, so the unit stayed `enabled` and the 2026-10-11 reboot brought it back for two days — read-only, but adding to the Kalshi 429 load. Retiring a service is `systemctl --user disable --now`.

**The candidate-A measurement instrument**, built 2026-08-24 to the pre-registered spec (edge log §2026-08-24 late-3 — the spec is the contract; amend it there before drifting here). Measures whether storm-linked Kalshi books reprice slowly after NHC advisories; house null: they don't. Three tables: `advisories` (one row per (storm, product, advNum) with nominal issuance, NHC `fileUpdateTime`, OUR `detected_at`, and raw product text so content parsing is redoable offline — rows loaded at daemon start are `baseline=1` and must be excluded from T0 analysis), `books` (top-3 orderbook levels WITH SIZES, change-only + 15-min heartbeats so staleness ≠ no-change; `ts` is REAL because same-second change-after-heartbeat collided on an INTEGER PK and `OR IGNORE` swallowed the change — caught by test pre-deploy, the R-19 lesson again), `markets` (watchlist + resolutions). Reads ORDERBOOKS, never market rows (that endpoint zeroes quotes — measured). Watchlist = season series + `KXHURCAT-<ACTIVE storm>` members only. Cadence: books 15 s in hot windows ([slot−45m, slot+90m] around {03,09,15,21}Z, or 90 min after any detection) / 90 s idle; NHC 1/min hot / 5 min idle. Places no orders; **must never import `kalshi_orders` or `pm.trading.execution`**. Kill/graduation rules live in the spec, not here. First contact: 12 baseline products (Iselle/Lala), 115-market watchlist, live spreads at 1–2¢ with 5–100-contract depth.

### `pm/feeds/nhc_data.py` + `pm/research/storm_watch.py`

**The storm-detection trigger** (2026-08-24). After the day's edge search closed every candidate but scheduled-advisory weather, the pre-registered rule was "specify the instrument when a named storm exists; build nothing before" — and a trigger nobody watches never fires. `nhc_data` fetches NHC's `CurrentStorms.json` (machine-readable active-cyclone list, updated on the 5/11 AM/PM ET advisory cycle) under the `nhc` breaker; classifications TS/HU/MH/STS/SS count as NAMED (TD/PTC deliberately not — numbers, not names). `storm_watch` is a cron single-shot (4×/day, ~35 min after each advisory slot) that alerts ONCE per new named storm and once per new storm-flavored Kalshi series (full open-events scan keyword-matched on titles — the close-time feed misses props, the documented discovery gotcha; season-aggregate series `KXHURRICANE`/`KXNAMEDSTORM` excluded). Dedupe state via `atomic_io`. **First live run fired immediately** — TS Iselle + TS Lala active, ten storm series already listed incl. path markets (`KXHURPATHHAWAII`, `KXHURPATHNC`) — i.e., the trigger condition was already met the day it shipped. It also flushed out two latent bugs, both fixed the same hour: the entry point didn't load `.env` (gotcha #4 — the phone page silently didn't send), and `notify._ntfy` crashed on non-latin-1 TITLES (HTTP headers are latin-1; ntfy supports RFC 2047, now applied only when needed). Trades nothing; imports no order-capable module.

### `pm/feeds/underlying_data.py`

**Commodity/index/FX/yield quotes — Yahoo v8 chart endpoint** (no key, plain requests; UNOFFICIAL — every response handled defensively, failures degrade to None). Extends the near-close **z-instrumentation** beyond crypto to `back_nearclose`'s largest flow class. `SYMBOLS` keys are `fade_nearclose.asset_of` outputs → (yahoo symbol, sanity band): GOLD/SILVER/NATGAS/COPPER/WTI/BRENT futures, ^GSPC/^DJI/^NDX indices, JPY=X/EURUSD=X, ^TYX/^TNX/^FVX treasury yields (verified UNSCALED at build; 0–25 sanity band turns a silent ×10 scale flip into None, not wrong z). `get_spot(root)` → `(price, quote_age_seconds)` — **staleness policy deliberately lives at the CALLER** (`fade_nearclose.z_for_entry` refuses age > `MAX_SPOT_AGE_S` 30min; these markets close nights/weekends, and a Friday-close quote would poison z with confident-looking garbage — verified: Saturday build-time probe showed all symbols ~20h stale). `hourly_sigma(root)` from 60m candles, ~6h cache. Env `UNDERLYING_HTTP_TIMEOUT`.

### `pm/venue/slippage.py`

Pure helpers for live order-book slippage. `walk_book_for_stake(book, *, side, stake_dollars)` walks one side of a parsed book accumulating cost/proceeds; `effective_edge(model_prob, est)` returns the buyer's-perspective realized edge. Used by the 🔭 Edges page opt-in panel.

### `pm/archive/book_archive.py`

SQLite store of order-book snapshots at `data/shared/book_archive.sqlite`. Keyed on `(venue, market_id, token_id, snapshot_ts)`. `get_snapshot_at(ts)` returns the most-recent at-or-before. `store_snapshots(rows)` for one-transaction bulk inserts. WAL + synchronous=NORMAL for HDD-friendly fsync behaviour. Path env-overridable.

### `pm/archive/trade_archive.py`

SQLite store of **executed-trade prints** (the Kalshi "trade tape") at `data/shared/trade_archive.sqlite`. Sibling of `book_archive` but a different grain: discrete fills keyed on `trade_id` (so re-fetching overlapping windows is idempotent via `INSERT OR IGNORE`), each tagged with the aggressor side. `store_trades(rows)` / `store_trades_df(df)` / `get_trades_in_range(ticker, since_ts, until_ts)` / `latest_trade_ts(ticker)` / `stats()`. Captures *executed flow* (vs book_archive's *resting liquidity*). Env override `TRADE_ARCHIVE_PATH`.

### `pm/capture/trade_poller.py`

Runnable that captures Kalshi `markets/trades` into `trade_archive`. `--backfill --days N` = full historical sweep per ticker (cursor-paginated min_ts/max_ts → reconstructs a *finished* game's tape, filling the "book_archive is forward-only" gap); default/`--loop` = incremental poll (only trades newer than the archive). Ticker universe = open MLB markets (shared discovery with `mlb_book_poller`) + (with `--include-archived`/`--backfill`) every `KXMLBGAME-*` in book_archive. REST complement to the WS `trade` channel.

### `pm/capture/book_poller.py`

Runnable background poller. `python -m pm.capture.book_poller` = single cycle; `python -m pm.capture.book_poller --loop --interval 300` = forever. Captures live `/book` across registered providers into `book_archive`. Not Streamlit-side — run via cron/systemd/tmux.

### `pm/ops/bulk_deep_analysis.py`

Cron-able bulk runner. Walks closing-soon markets, runs `llm_deep_analysis.analyze_market` on each, logs forecasts to the `agentic_deep` profile tagged `llm_deep:<model>`. One model per invocation (`--model` arg); six cron entries staggered 2-7am run all recommended models nightly on the same market universe. Forecast-only — no paper trades, by design (the agentic strategy runners cover paper-PnL tracks). Generates the head-to-head Brier-per-model dataset that informs which model to promote to live triggers. **Note**: this NEVER covers MLB games — `KXMLBGAME` markets don't appear in the first ~1000 closing-soon markets, so MLB deep coverage comes from `mlb_deep_primer.py` instead.

### `pm/ops/snapshot_runner.py`

Cron-able portfolio snapshotter. Walks every profile, enriches positions with live prices, and calls `maybe_snapshot` so the portfolio value chart builds without UI visits. `python -m pm.ops.snapshot_runner` = one cycle; `--profile X` = single profile; `--min-interval-hours N` = debounce (default 4h). Runs 4x daily via cron (00:00, 06:00, 12:00, 18:00 UTC).

### `pm/ops/auto_resolve_runner.py`

Cron-able auto-resolver. Walks every profile, builds a merged closed-markets lookup (public providers + Kalshi settlements when authed) once on the owner profile so settlement data is visible to all profiles, and marks any pending forecasts whose tickers have resolved. Without this the Track Record badges stay "untested" even after markets resolve. `python -m pm.ops.auto_resolve_runner` = one cycle; `--loop --interval 900` = forever.

### `pm/arms/strategy_runner.py`

Agentic runner. Records a chosen registered strategy's signals as forecasts + paper buys into a designated profile (default `agentic`). Auto-closes positions on resolution and (when an `ExitPolicy` is configured) on TP/SL/trail/time stops. Cron-able; same single-cycle vs `--loop` modes as `pm/capture/book_poller.py`. Managed-sibling profiles (`agentic_*_managed`) provide A/B comparison against the hold-to-resolution baseline.

### `pm/venue/kalshi_fees.py`

Kalshi trading fee calculation per their published formula: `ceil(0.07 × P × (1-P) × C × 100) / 100` (sports series uses 0.035). `fee_for_trade` / `round_trip_fees` / `fee_pct_of_stake` / `fee_rate_for_series`. Used by strategy_runner's exit logic (so TP/SL gate on NET P&L) and backtest's `fee_fn` parameter.

### `pm/research/pre_event_scanner.py`

Pre-event lottery scanner — finds sub-$X markets with a catalyst landing in <Y days. Includes `strategy_pre_event_lottery` (registered as `pre_event_lottery`, marked UNTESTED in display name). Concentration warning when >50% of the basket is one event series — important because the lottery thesis assumes independent catalysts. CLI: `python -m pre_event_scanner --max-price 0.10 --max-days 1.5`.

### `pm/research/oscillation_scanner.py`

Ranks active markets by oscillation behavior + registers two strategies. `strategy_oscillation_revert` — **⚠️ KNOWN BAD** (Brier 0.169 vs market baseline 0.079, -21.9% ROI), fires runtime warning if invoked. `strategy_oscillation_momentum` — **🧪 PAPER**: inverted variant (bet WITH the direction), backtest +8.9% ROI at 60.9% hit rate with max_position_pct=0.05, but P&L is fat-tail-dependent (top 5 wins = 229% of total P&L). Wired into `agentic_oscillation_momentum_managed` cron for live paper-trade validation. `strategy_runner` imports this module for auto-registration. Scanner CLI: `python -m oscillation_scanner --days 30 --top 20`. See `docs/failed-experiments.md` for the revert post-mortem.

### `pm/research/oscillation_backtest.py`

Retrospective evaluation: runs `strategy_oscillation_revert` over historical closed markets (via `backtest.build_dataset`, lookahead-safe), compares against `market_consensus` baseline, and buckets results by `range_score` quintile. Was used to falsify oscillation_revert. CLI: `python -m oscillation_backtest --since-days 90 --max-workers 8 --lead-hours 48`.

### `attic/`

**Retired code, kept for reference — not imported, linted, typed or tested** (P2/R-17, 2026-08-17). The retired baseball daemon (`longshot_scalper`, all three modes) and its five units, the crypto latency probe, the orphaned MLB deep primer, the reaction-thesis research tools, the microstructure/maker/settlement/econ null-result instruments, the two concluded A/B evaluators, the falsified early-exit backtest, `broadcast_events`, and the `scratch_*` crunches — with their tests under `attic/tests/` and unit files under `attic/deploy/systemd/` (uninstalled from systemd). **`attic/README.md` is the index**: why each moved, where its finding is documented, how to run one (`PYTHONPATH=.:attic`), and the ORIGINAL file-map rows verbatim. Rule: ❌ RETIRED / falsified / scratch → attic; ⏸ STOPPED re-enable-able infrastructure (baseball live stack, tennis/WNBA/news captures, arb-scan) stays at the root.

### `docs/edge-research-log.md`

**Canonical edge-research log — findings are documented HERE BEFORE any instrument/arm is coded** (workflow: research and documentation first, code follows). Session-dated entries: thesis → mechanism → evidence → verdict + a priority queue. 2026-07-09 headline: **near-close 0.10–0.20 longshot fade survives the executable + de-correlated test (+1.6¢/contract net, 90% by-event win, 532 independent events, ~2h turnaround)**; tennis-react gate CLOSED-dead (7,348 forward samples, zero move); WNBA lean-dead (fat-tail, medians 0); weather (KXHIGH* exists, 12/city), ladder-monotonicity, earnings-mention base-rate, chart/album leading-data candidates queued. Read before starting any new edge work.

### `docs/compounding-capital-cycle.md`

**⭐ NEXT BUILD TARGET (gated).** Design doc for the compounding / capital-cycle layer: since fast edges are dead and the surviving fade edge is slow, maximize *portfolio throughput* (breadth × reinvestment × fractional-Kelly) over the fade arms, not trade speed. Includes the 2026-07-05 capacity study (book absorbs ~$30k@2¢/~$60k@5¢ at current breadth vs ~$— deployed — capacity is NOT the constraint; props ~8× deeper than retail) and the **Gate**: build the fractional-Kelly/capacity-capped/correlation-aware sizing layer ONLY after an arm confirms OOS (compounding amplifies a negative edge). Read before building any auto-sizing.

### `pm/ledger/exits_analysis.py`

Reader + analytics for `data/<profile>/exits.jsonl` (every closure, both resolution and managed). `load_exits` / `exit_reason_summary` / `counterfactual_pnl` / `compare_profiles`. Counterfactual P&L = what would have been if held to resolution.

### `pm/core/sidecars.py`

**Sidecar protocol** (`Sidecar` Protocol class + registry). Every sub-package that runs on its own schedule and exposes a uniform operational surface (`health`/`stats`/`recent_activity`) registers here. See `docs/sidecar-protocol.md` for the canonical spec — read before adding a 2nd sidecar.

### `baseball/`

**Baseball prediction sidecar** (first instance of the sidecar pattern). Sub-package modules: `data.py` (MLB Stats API client — teams, schedule, pitcher stats, batter stats, bullpen stats, lineups), `model.py` (v0.6.0 matchup model: regressed Pythagorean + halved home-field + lineup OPS; SP/bullpen ERA disabled — see `docs/failed-experiments.md`), `state.py` (SQLite at `data/shared/baseball_state.sqlite` — teams, team_season, pitchers, batters, bullpens, lineups, games, predictions, live_plays, activity), `park_factors.py` (per-park run/HR multipliers, Bayesian-regressed to 1.0), `recent_form.py` (last-15-day blend, ≤30% weight), `runner.py` (cron-able nightly refresher + `--lineups-only` mode for in-day refresh), `live.py` (daemon polling MLB `feed/live` every 10s for plays; also syncs game Finals back to games table for fast resolution), `wpa.py` (Win Probability + WPA model — RE24 matrix + normal approximation), `backfill.py` (historical play-by-play loader with `--games-only` flag), `analysis.py` (lead-lag join queries between live_plays and book_archive), `insights.py` (aggregates for Stats page + LLM context), `llm_chat.py` (Ollama-backed chat with full state context), `web.py` (FastAPI+Jinja2+HTMX UI on port 8770), `__init__.py` (public surface: `predict_game` / `predict_for_ticker` / `team_ratings_table` / `today_schedule` / `resolve_pending_tickers` + Sidecar protocol impl). Multi-user safe web UI — NOT Streamlit.

### `wnba/`

**WNBA prediction sidecar** (2nd sidecar, built from baseball via `docs/sports-model-playbook.md`). `data.py` (ESPN client — `site.api.espn.com` basketball/wnba: teams, scoreboard/schedule, standings→pointsFor/Against/W-L, game summary; pure HTTP), `model.py` (v0.1.0: **basketball** Pythagorean exponent 13.91 + Bayesian prior PRIOR_WEIGHT=10 + seeded home-court 0.55 *to be re-measured*; deliberately minimal — nothing beyond Pythagorean+prior+HFA until Brier earns it), `state.py` (SQLite `data/shared/wnba_state.sqlite`, env `WNBA_STATE_PATH` — teams/team_season/games/predictions/activity/meta; `event_id` is TEXT, ESPN ids are strings; `get_games_for_eastern_date` handles the Kalshi-ticker-is-Eastern vs stored-UTC gotcha), `runner.py` (cron cycle: teams, stats, schedule today+4-day-lookahead, predictions, settle finals), `web.py`+`llm_chat.py`+`templates/`+`static/` (FastAPI UI on **:8771**, reuses baseball's style.css verbatim — pages: Today slate (clickable cards → game detail, win-prob bars, favorite-hold ★), Game detail `/game/{event_id}` (prediction + model inputs + team strength), Recent Games (finals vs model ✓/✗), Stats (today's model edges + scoring leaders), Chat (Ollama via `wnba.llm_chat` with WNBA state context), Standings; systemd `wnba-web`, launcher `scripts/wnba_web.sh`), `__init__.py` (same public surface as baseball + Sidecar protocol + ticker bridge). **Ticker `KXWNBAGAME-{YY}{MON}{DD}{AWAYHOME}-{YESTEAM}`**: suffix IS the yes team (match on date+yes_team, never parse the matchup blob); Kalshi→ESPN abbr map (`CONN`→`CON`, `PDX`→`POR`). No live daemon — pre-game-forecast only (the favorite-hold thesis doesn't need in-game). Edges adapter: `edges.find_wnba_edges` / `strategy_wnba_winprob` (source `wnba`, key `wnba_winprob`) + `strategy_wnba_favorite_hold` (key `wnba_favorite_hold`, buy moderate favorites 0.55–0.70 & hold). Agentic arm: `pm/arms/wnba_runner.py` discovers WNBA markets via the events path (they don't surface in the volume feed) → `agentic_wnba_favorite` profile.

### `tennis/`

**Tennis prediction sidecar** (3rd sidecar, **first individual sport** — Elo, not Pythagorean; see `docs/sports-model-playbook.md`). `data.py` (ESPN ATP+WTA: rankings→id↔name, tournaments via core-API date-range, matches parsed INLINE from each tournament event — winner+athlete-ids+date+round, no per-match fetch; surface inferred from tournament name), `elo.py` (**pure Elo engine** — P=logistic(Δ/400), zero-sum K-updates, **rank-based seeding** via `seed_for_points` [the accuracy fix — walk-forward Brier 0.2405→0.2198 vs flat 1500 seed; K-decay available but OFF, didn't help], optional surface blend; covers whole field), `model.py` (**v0.2.0** rank-seeded Elo→P(win) + surface blend), `state.py` (SQLite `data/shared/tennis_state.sqlite`, env `TENNIS_STATE_PATH` — players [Elo cache global+per-surface], matches [Elo replay source of truth], predictions, Brier), `runner.py` (`--backfill` season-to-date + nightly top-up; recomputes Elo from full matches table each cycle), `web.py`+`llm_chat.py`+`templates/`+`static/` (FastAPI UI **:8772**, reuses baseball css — pages: Today (date-navigable ◀/▶: past=results w/ model ✓/✗, future=forecast, + live model-vs-market edge panel), Player detail `/player/{tour}/{id}` (Elo global+per-surface + match history, clickable), Stats (live edges + clay/hard surface leaders), Chat (Ollama via `tennis.llm_chat`), Elo Ratings, Recent Matches; systemd `tennis-web`, `scripts/tennis_web.sh`), `__init__.py` (public surface + Sidecar + ticker bridge). **Ticker bridge — the hard part**: Kalshi 3-letter codes are unreliable (`BER`→Bergs not Berrettini), so read the YES player's FULL NAME from the market **question** ("Will <NAME> win the …") and full-name match to the Elo table; skip on ambiguity, never guess. Edges adapter: `edges.find_tennis_edges` / `strategy_tennis_winprob` (source `tennis`, key `tennis_winprob`), parallel to consensus. `resolve_pending_tickers` resolves the paper arm from the matches table (ticker→date+code→finished match→winner). **Model accuracy**: `elo.walk_forward_brier` scores every match out-of-sample (predict from pre-match Elo, then apply); the runner persists it to `state.meta` and `tennis.stats()` surfaces it (the old ticker-keyed predictions table was never populated). First read **≈0.235 over ~4,800 matches — only modestly better than 0.25 coin-flip → the Elo is weak over the full field**.

### `pm/capture/kalshi_ws.py`

WebSocket streaming client for real-time Kalshi order book updates. Authenticated via RSA-PSS handshake headers (reuses `kalshi_auth`). Subscribes to **both** `orderbook_delta` (resting liquidity → in-memory `LiveBook` per ticker, sub-100ms deltas, flushed to `book_archive.sqlite` every 15s) **and** `trade` (executed prints → buffered and flushed to `trade_archive.sqlite` on the same cadence; `_normalize_ws_trade` tolerates cents-or-dollars price schemas and synthesizes a `trade_id` when absent). Auto-discovers MLB tickers on startup. Systemd service `kalshi-ws`.

### `pm/arms/reactive_scalper.py` + `~/.config/systemd/user/reactive-scalper.service`

**In-game reaction (speed) arm** — the one positive signal from the data mine (`reaction_decay.py`). Event-driven: watches EVERY live game (no pre-existing position) and on a NEW play whose model **WPA** clears the gate (`|WPA| ≥ wpa_threshold` [0.10, ~top 22% of plays] & inning ≥ `min_inning`) BUYS the side whose win-prob went UP, at ask, while the book lags — then SELLS via take-profit / time-stop (harvest the ~60s repricing) / resolution close. **WPA-conditioned** via `baseball.analysis.wpa_for_play` (RE24/normal model — the MLB feed's win-prob is NOT in raw_json): catches high-impact NON-scoring plays (rally-killing DP → fielding side gains) and skips low-impact runs. Pure fns `qualifies`/`batting_side`/`beneficiary_from_wpa`/`evaluate_exit` unit-tested. Routes all buy/sell/position/cash through the `execution` broker (profile `agentic_reactive`). **Ran LIVE 2026-06-06→09 at `$2`/order, then PULLED to paper** (`LIVE_TRADING_PROFILES` emptied) — live test ≈ −$—/4 nights isolated the **scalp-exit whipsaw** (choppy/extra-inning games: marketable time-stop sells into the reversal, both sides of a game can lose); in redesign. Going live again is a `go_live.sh`/`LIVE_TRADING_PROFILES` change. `main()` calls `load_dotenv()` **and** `run_cycle` pins `POLYMARKET_ACTIVE_PROFILE` per cycle — **both REQUIRED or the live gate silently falls back to paper** (see `docs/live-execution-adapter.md`). `broker.buy`/`broker.sell` return an `OrderResult` and do NOT raise on a non-fill, so only a genuine fill (`res.ok and filled_count>0`) logs `react_entry`/the exit + counts toward opened/held/cash/`position_meta`; non-fills log `react_reject`/`exit_unfilled` (status+detail) and KEEP meta (retry next cycle; `_close_resolved` catches at game end) — keeps the track record honest. Entry gate also has a price floor `min_entry_price` (0.25) — skip a benefiting side still priced as a deep underdog — **and a book-thinness gate `max_entry_depth` (60000 contracts within 3¢ of ask, the q33 thin/mid boundary; 0 disables)**: the reaction edge is **thin-book concentrated** (`reaction_capacity.py`: thin-tercile +7.7¢/contract @ 69% win vs deep-book −4.9¢ — the lag lives where few market-makers reprice), so it skips already-liquid books (`react_reject status=book_too_deep`). `_ask_depth_contracts`/`_entry_depth` read the same fresh `book_archive` snapshot as `_quote`. Entries/rejections → `data/shared/reactive_log.jsonl`, exits → profile `exits.jsonl`, real orders audit-logged to `data/shared/orders.log`; watermark in `reactive_state.json` (age-guard prevents firing on stale plays). **`--hold` mode (`ReactiveConfig.hold_to_resolution`, profile `agentic_reactive_hold`, `reactive-hold.service`): REACT-AND-HOLD — same WPA entry, NO scalp `_manage` exit, ride to `_close_resolved` at $1/$0. The validated +EV variant** — counterfactual on 126 real reaction entries held to resolution = +1.5¢/contract net (bought 2.5¢ below fair on the reaction lag; holding pays the cost once + skips the whipsaw that made the scalp net-negative). Defensive resolved-reentry guard (`_is_resolved`/`_mark_resolved`, mirrors the longshot fix). See `[[project_strategy_research_findings]]`.

### `pm/research/inefficiency_sweep.py`

**Cross-sector mispricing map** — reframes the strategy search from "is this one strategy profitable?" to "where across the whole venue is the market exploitably mispriced?" For a broad sample of recently-closed markets, takes the YES price at a fixed lead before close as the forecast and measures **calibration deviation** (observed resolution rate vs priced rate by probability bucket) — NOT raw Brier, which conflates uncertainty with mispricing. **`classify()`** tags each market `binary` / `multi_outcome` / scalar-range using Kalshi's real `event_ticker` (mutually-exclusive group). Report has three sections: (1) binary-only calibration map, (2) **one-winner-field overround** (`sum(member YES prices) − 1` = fade-able vig; scored only on groups that resolved to exactly one winner — scalar ranges excluded, incomplete + non-exclusive groups reported as drops), (3) venue-wide favorite-longshot calibration (the cleanest cross-sector signal). **Scope limit**: the settled-events endpoint is newest-first, so this is a RECENT-window sweep (deep history needs the trade archive — future work); the fetch is paced (batch + cooldown) to stay under Kalshi's 429. Per-market price-sampling cached to `data/shared/inefficiency_cache/`; report to `data/shared/inefficiency_sweep_report.txt`. Read-only. Pure fns `classify`/`category_edges`/`multi_outcome_overround`/`format_report` unit+doctested. First finding (2026-06-10): ~89% of recent Kalshi is multi-outcome fields; sports efficient (Brier 0.005); favorite-longshot bias is the live lead (longshots overpriced, moderate favorites underpriced). See `[[project_north_star_kalshi_leaderboard]]`. CLI: `python -m pm.research.inefficiency_sweep --since-days 14 --min-volume 100`.



### `pm/research/candle_panel.py` + `~/.config/systemd/user/candle-panel.service`

**2026-10-13 — `volume`/`open_interest` were 0 in all 19.4M rows.** Kalshi's fixed-point migration renamed the candle fields to `volume_fp`/`open_interest_fp` (strings); `kalshi_client.get_candlesticks` kept reading the bare keys and `_to_float(None)` returned 0. The unit test encoded the OLD payload, so it stayed green. Prices were unaffected. Found by an external research pass whose volume gate came back empty. Fixed with an `_fp`-first read (old key as fallback); rows stored before the fix keep 0 because inserts are `INSERT OR IGNORE`. **Treat candle volume/OI as unknown, not zero, for any ticker fetched before 2026-10-13.**

**Candle panel — historical near-close price reconstruction for SETTLED markets** (2026-08-08, Gap A of the research log). Kills the "historical near-close prices are unreachable" premise: `kc.get_candlesticks` returns hourly `yes_bid_close`/`yes_ask_close` for any settled ticker, so this builds a panel — `markets` (one row/settled market: outcome, close_time, category) + `candles` (hourly bid/ask/price/vol/OI for the **final `FINAL_HOURS`=120h** before close) — multiplying the sample the near-close studies run on (~2,000 binary settled markets in a 3.6h window vs the forward panel's weeks of accrual). Each cycle: `get_closed_markets_in_range` (vol>50, binary-only, `max_items=40000` — the 10k default silently orphaned older in-window markets on a 14d sweep) → one paced `get_candlesticks` call per not-yet-stored ticker (resumable per-ticker state, `--max-markets` bounds a cycle, remainder next cycle). **`load_panel(lead_hours)`** = the study surface: one row/market with the executable candle nearest `close − lead` + outcome. Honest limits in the docstring: settled feed reaches back only weeks (panel GROWS forward, deep past stays unreachable); candles are hourly closes not tick top-of-book. Read-only; lazy `_db_path` (env `CANDLE_PANEL_DB`); loud empty-feed warning. systemd `--loop --interval 21600 --since-days 14 --max-markets 4000`. CLI: `python -m pm.research.candle_panel [--backfill --since-days 21|--stats]`.

### `pm/capture/near_close_capture.py` + `~/.config/systemd/user/near-close-capture.service`

**⏸ disabled 2026-11-04** (`disable --now`, not just `stop` — the
advisory_capture lesson). Every consumer of the panel is closed: near-close
itself CLOSED+RETIRED 2026-08-24, and `candle_panel` reconstructs near-close
prices for settled markets historically, killing the "unreachable forward-only"
premise that justified paying for this accrual. 100 MB of panel retained at
`data/shared/near_close_capture.sqlite`. Re-enable only with a preregistered
study that needs *forward* capture specifically (e.g. depth, which candles
lack).

**Idle-accrual data engine** — builds the clean near-close panel the mining studies need, forward (historical near-close prices are unreachable: settled-events is newest-first). Each cycle: CAPTURE (once per market, when it first falls within `--target-lead-hours` [2h] of close, snapshot best_bid/ask + last + volume + event_id from one closing-soon fetch — zero extra calls) + RESOLVE (fill outcomes once captured markets settle, scanning only when something's pending). SQLite at `data/shared/near_close_capture.sqlite` (env `NEAR_CLOSE_DB`, WAL); `INSERT OR IGNORE` keeps the first (closest-to-target-lead) snapshot. `load_panel()` feeds the studies; `stats()` for visibility. Pure `_lead_hours`/`_yes_mid`/`_resolution_updates` + store ops unit-tested. Read-only vs venue. systemd `--loop --interval 1800`. CLI: `python -m pm.capture.near_close_capture [--stats]`.

### `pm/capture/mention_capture.py` + `~/.config/systemd/user/mention-capture.service`

**⏸ disabled 2026-11-04** (`disable --now`). All three mention angles are
closed — sniping 2026-07-30, pricing 2026-08-24, candidate D
(earnings release→call) 2026-09-09 — and no open thesis consumes the stream;
it had been accruing (755 MB) purely on spec. State retained at
`data/shared/mention_capture.sqlite`. Re-enable with any fresh preregistered
mention-family study; the capture itself worked fine and the discovery loop
(2026-09-01 note below) is the version to restart.

**Mention-market repricing capture — measure-first instrument for the live-mention-sniping thesis (2026-07-09).** The mention family (`MENTION_SERIES`: Trump/LoveIsland/LateNight/WC/debate/…) is the fastest-cycling retail corner (~300+ resolutions/3wk, tied to broadcasts); a mention market settles YES *the moment the word is said on stream* — deterministic resolution our GPU whisper stack (`pm/mention/audio_transcribe.py`) could detect live. **The open question this answers before any trader is built: when a word hits, does the thin book reprice instantly (sniper exists → dead, like MLB) or grind up over 30s–min (human-watching-Peacock lag → capturable)?** Every ~10s polls each series' full market list (ONE call/series returns all markets' yes_bid/yes_ask/last/volume — no per-book calls, no 429 risk) and delta-stores changed quotes → full price paths around every mention resolution, passively, 24/7. Post-hoc: align jump shape vs Kalshi `settled_time` (later: transcript word-timestamps) → repricing-lag distribution. Pure `changed_rows` doctested. Read-only; no trades. SQLite `data/shared/mention_capture.sqlite` (env `MENTION_CAPTURE_DB`). systemd `--loop --interval 10`. CLI: `python -m pm.capture.mention_capture [--stats]`.

**2026-09-01 — the watchlist is now discovered, not maintained.** The
hardcoded series list missed an entire earnings wave for the second time
(Jul-14 banks, then the Sep wave — 22 open `KXEARNINGSMENTION*` series while
the list still named the settled Q2 banks). `discover_mention_series()`
sweeps open events hourly for any series ticker containing MENTION and
unions the result with the static seeds; a failed sweep returns () and the
loop keeps its previous watchlist, so venue trouble never shrinks coverage
mid-broadcast. This capture is also the study surface for **candidate D**
(earnings release-to-call repricing, edge log §2026-09-01) — question 1
there is answered entirely by data this daemon was already storing.


### `pm/mention/mention_rehearsal.py`

**PAPER-FIRE + post-event scoring harness for the mention cascade thesis** (docs/edge-research-log.md 2026-07-19: the "instant" reprice found 07-18 turned out to be a 7–60s HUMAN CASCADE on the trade tape, prize ~5 figures/event). Answers *had we fired at detection time, where would we have landed and what would we have made* — a rehearsal, never a trader: does NOT import `kalshi_orders`/`execution`, places no orders, read-only REST + logs only. **`--follow`**: tails `data/shared/mention_detections.jsonl` (produced by `pm/mention/mention_listen.py`, rows `{ts_detected, ts_word_audio, event_ticker, ticker, matched_text, pattern, model, source}`) via `JsonlTailer` (inode+size rotation-aware, ~0.2s poll), and for each NEW row fetches the market's current quote (`kc.get_market`) and appends an intended-order record to `data/shared/mention_rehearsal.jsonl` — `intended.price = min(yes_ask+0.02, 0.99)`, `count = floor(stake/price)` (`--stake`, default $50); logs the quote round-trip latency as its own measurement. **`--report EVENT_TICKER`**: for each rehearsal/detection row of that event, pulls the executed-trade tape (`kc.get_all_trades`, detection ±10min) and computes, per ticker: `cascade_start` (first trade below 0.95 that's ≥5¢ above the median of its prior trades AND is followed by a ≥0.95 print within 60s — pure, unit-tested against clean-sweep/pre-utterance-drift/already-high/empty tapes), `our_lag_s = ts_detected − cascade_start` (negative = we beat the cascade), `simulated_fill` (VWAP of tape prints in `[ts_detected+placement_latency, +2s]`, default latency 0.7s via `--placement-latency`; falls back to the first print within 30s; pure, unit-tested), and `would_be_pnl` net of `kalshi_fees.fee_for_trade` at the standard 0.07 rate (mention series are non-sport) against the market's real `result`. Writes `data/shared/mention_rehearsal_report_<EVENT>.txt` + prints a per-ticker table + summary (n detections, n beat-cascade, median lag, total would-be P&L, false-positive count/cost — loud on any detection that resolved NO). **`--simulate-detection TICKER`**: appends a synthetic now-timestamped detection row so `--follow` can be smoke-tested end-to-end against a live quote without audio. CLI: `python -m pm.mention.mention_rehearsal --follow [--stake 50]` \| `--report KXFTNMENTION-26JUL19 [--placement-latency 0.7]` \| `--simulate-detection TICKER`.

### `pm/mention/mention_cascade_backfill.py`

**Trade-tape SHAPE reconstruction for a past mention event — the offline complement to `pm/mention/mention_rehearsal.py`'s live `--report`** (built 2026-07-23: the system is off over weekends, so live capture of Fri WHCD + the Jul-25/26 MLB announcer-mention games is impossible, but Kalshi's `get_all_trades` tape carries exchange timestamps and is backfillable anytime after the fact). Read-only; imports NO `kalshi_orders`/`execution`, places nothing. Per `--event EVENT_TICKER` (repeatable): fetches every member market (`kc._paginate("markets", {"event_ticker":...}, "markets", 1000)`), reads each market's real `result` + `yes_sub_title`/`custom_strike.Word` (word label). **Window logic**: does NOT bound the trade-tape pull by `close_time` (that's Kalshi's weeks-out settlement backstop, not when the word was said) — pulls each ticker's FULL lifetime tape (capped by `--max-trades`, default 50k) and runs cascade detection over the whole series; mention markets are near-silent until the broadcast moment so this stays cheap (hundreds–low-thousands of rows/market). **`characterize_cascade(trades)`** (pure, doctested + 6 unit tests covering every shape) reuses `mention_rehearsal.cascade_start` for sweep-start detection, then computes `pre_jump_price` (median of the ≤10 trades strictly before the start), `seconds_to_95` (start → first ≥0.95 print), `cascade_volume`/`n_trades` (sum/count of trades in that window), and `shape` (`instant` ≤2s, `fast` ≤15s, `slow_cascade` >15s, `no_sweep` if no qualifying sweep). **`detect_no_spike(trades)`** (pure, doctested) flags a NO-resolved word whose tape peaked ≥0.5 then fell back ≥0.15 from its peak — a false-positive-risk audit for the live detector's pattern list. Report (stdout + `data/shared/mention_cascade_backfill_<EVENT>.txt`): YES words sorted fastest-cascade-first + summary (n YES/NO, median seconds_to_95, shape counts, total cascade volume) + the NO-spike list. **Live validation (2026-07-23) against the two events `docs/edge-research-log.md`'s 2026-07-19 entry describes**: Trump 07-15 HOTT reproduces exactly — cascade_start 20:07:24 UTC, 0.97 print at 20:07:31 = **7.0s**, matching the doc's "0.97 by +7s" verbatim; 16/18 YES words are `instant`/`fast` (≤10s), consistent with "clean utterances reprice in one capture tick." WC-final (`KXWCMENTION-26JUL18FRAENG`) reproduces the *heterogeneity* the doc describes ("cascades even slower and much deeper") but not the doc's two specific number pairs: TRUM/GIAN swept in ~2s here (not 23-60s), while MARA (58s), HATT (38s), MESS (28s), WHAT (17s) land squarely in the doc's claimed range — so the "slower/deeper" characterization holds in aggregate (4/14 words `slow_cascade`, median 2.5s pulled down by simultaneous-utterance words) but not per-word as originally read. The doc's volume figures (TRUM 64,892; GIAN 30k) turn out to match each ticker's **full lifetime trade volume** (TRUM lifetime 237k total, but MARA's lifetime volume is 64,604 — suspiciously close to the doc's "TRUM" figure), not the tight cascade-window volume this tool reports (TRUM cascade_volume here: 4,931) — the 07-19 field notes were very likely eyeballing total-volume-around-the-event from a coarser read, not a strict start-to-0.95 window. Conclusion: the tool's *timing* measurement is validated exactly against the one number the doc pinned precisely (HOTT +7s); the WC-final numbers were loose estimates this tool has now made precise. CLI: `python -m pm.mention.mention_cascade_backfill --event KXTRUMPMENTION-26JUL15 [--event ... --max-trades 50000]`.

### `pm/mention/monologue_recorder.py` + `~/.config/systemd/user/monologue-record@.service` (+ per-show timers)

**2026-10-13 — dark for two months, unnoticed.** The tuner moved .43 → .4 on DHCP across an outage; every run from 2026-08-16 left an empty run dir (79 of them; only Aug 11–15 `tonight` ever transcribed). Three faults stacked: `hdhomerun.DEFAULT_IP` was frozen at import (gotcha 1) from an env var nothing loaded (gotcha 4); `main()` exited 0 on a failed capture, so every unit was `Finished`; and the unit had no `OnFailure=` hook. Now: `hdhomerun.device_ip()` resolves per call (`HDHR_IP`, fallback `.4`); `main` loads dotenv and returns 1 on a failed capture; the unit pages via `polymarket-alert@`. Durable fix still owed: a DHCP reservation for the tuner.

**Nightly late-night monologue corpus recorder** (2026-08-08, collection-plan item 3): OTA tuner → 16kHz mono WAV → whisper transcript, building the ground-truth (audio, known-settlement) corpus the mention detector needs to fix its 71%/53% precision/recall BEFORE any auto-fire arm. Records + transcribes only — never trades, imports no trading modules. **Capture-first, transcribe-after** (corpus races nothing; two ffmpeg processes on different pinned tuners, GPU work post-capture). Shows are a data table (`SHOWS`): `tonight` (NBC 5.1, tuner 3), `kimmel` (ABC 8.1, tuner 2) — Mon–Fri 22:30 CT timers ENABLED; `snl` (NBC, Sat) table entry present but **timer ships disabled until the fall season** (summer reruns = waste). No CBS entry (Late Show ended May 2026 — a 22:30 CBS slot would record news; unit-tested). Outputs per run under `data/shared/monologue_corpus/<date>_<show>/`: `audio.wav` (KEPT — improved detectors re-run against raw audio), `transcript.jsonl` (segments+words with **absolute `wall_ts`** anchors for scoring vs Kalshi settlement times), `transcript.txt`, `meta.json`. Loud failure on tiny/absent WAV; capture survives tuner-lock failure (broadcast is unrepeatable; defense-in-depth try/except beyond hdhomerun's own contract). Known quirk: segment offsets can read ~−10s from chunk-overlap math — fine at minute-scale alignment. Env `MONOLOGUE_CORPUS_DIR`, `MONOLOGUE_WHISPER_MODEL` (small). CLI: `--show tonight [--minutes 40]` \| `--transcribe-only <run_dir>` \| `--list`.

### `pm/mention/hdhomerun.py`

**HDHomeRun FLEX QUATRO helper — pinned-tuner OTA stream URLs**, built for `pm/mention/mention_listen.py`'s `--hdhr-channel` source. Plex Media Server shares the same 4-tuner device on the LAN via `/auto/v<CH>` (device picks the lowest free tuner, so Plex gravitates to tuner0/1); the pipeline instead pins the top tuner (`PIPELINE_TUNER = 3`) via the explicit `/tuner<N>/v<CHANNEL>` URL form so the two never collide. `stream_url(channel, *, tuner=PIPELINE_TUNER, ip=DEFAULT_IP)` (pure, doctested) accepts either a DFW network name (`DFW_CHANNELS`: CBS 11.1, FOX 4.1, NBC 5.1, ABC 8.1 — Dallas-Fort Worth ATSC virtual channels, market-specific) or an explicit virtual channel string. `discover(ip)`/`channel_present(channel, ip)` are network GETs against `discover.json`/`lineup.json` (mocked in tests), never raise, used as a preflight (warn-only, doesn't block the stream attempt). `acquire_lock`/`release_lock` are a **best-effort HARD reservation** via the `hdhomerun_config` CLI tool (shells out to `hdhomerun_config <device_id> set /tuner<N>/lockkey <key>`) when installed; when it isn't, logs a clear note and returns `None` — the fallback protection is simply holding the HTTP stream connection open (which the pipeline does anyway for the run's duration). No raw UDP control protocol, no trading imports. Never lets a lock failure crash a capture session.

### `pm/mention/mention_listen.py`

**Mention-market DETECTION layer — streaming audio → per-market word/phrase detection**, the rehearsal instrument that follows the 2026-07-19 trade-tape read (`docs/edge-research-log.md`: mention resolutions are a 7–60s human cascade, not an instant tick — there's a real window, incl. a "context anticipation" tier needing no race at all). **Places no orders — must never import `kalshi_orders`/`execution`.** Reuses `audio_transcribe.AudioTranscriber` for ffmpeg capture (added `word_timestamps`/`TranscriptSegment.words`/`stream_start_wall`, and now optional `vad_filter`/`vad_min_silence_ms` constructor params, to that module for this consumer; CLI defaults unchanged) at a 5s/1.0s chunk/overlap default with VAD on but less-aggressive (`--vad`/`--no-vad`, `--vad-min-silence-ms 700`). `TranscriptWindow` keeps a rolling ~15s buffer of normalized words with absolute wall-clock timestamps so a target phrase spanning a chunk boundary still matches; `compile_pattern` normalizes+word-boundary-wraps each configured regex the same way. `MentionDetector` fires **once per ticker per run**, appending to `data/shared/mention_detections.jsonl` (`ts_detected` ms epoch, `ts_word_audio` absolute audio-timeline ts, matched text/pattern/model/source/`occurrence`) + an optional in-process `on_detection` callback for a future harness. Event config is JSON (`event_ticker`, `source`, `single_speaker`, `markets: [{ticker, patterns, count}]`); **`--build-config EVENT_TICKER`** fetches the event's markets from Kalshi and extracts the target phrase from quoted text in the question (`extract_patterns`, naive plural/possessive variants via `_variants`), flagging uncertain extractions with a `comment` for manual fix rather than guessing (and always emits `single_speaker: false` + a comment for a human to confirm). **v0.1 (2026-07-19 rehearsal fixes)**: chunking widened to 5s/1.0s overlap (fixed a whisper chunk-boundary recall miss — a word straddling a short low-overlap chunk got hallucinated into unrelated text); COUNT-market support (`MarketWatch.count`, default 1 — fires only on the Nth *distinct* occurrence, deduped by matched-word audio timestamp so the same instance re-seen as the window slides never double-counts); and a `single_speaker` config flag that logs a loud startup WARNING on multi-speaker content (speaker-blind detections measured net-negative paper P&L in the FTN rehearsal — guardrail only, not diarization). **`--hdhr-channel CH` source (2026-07-23)**: mutually exclusive with `--source`; uses `pm/mention/hdhomerun.py` to build a pinned-tuner OTA stream URL (`--hdhr-tuner`, default `hdhomerun.PIPELINE_TUNER`=3; `--hdhr-ip`) so live-broadcast capture (debates/awards/local news simulcast OTA) never collides with Plex sharing the same 4-tuner HDHomeRun device (Plex's `/auto` gravitates to tuner0/1) — `discover()` preflight is warn-only, `acquire_lock`/`release_lock` wrap the whole run best-effort (never crash capture on lock failure). CLI: `python -m pm.mention.mention_listen --event-config event.json --source <url> [--model small]`, `--replay <file>` for offline testing, or `--hdhr-channel CBS` for the pinned OTA tuner. **v0 is whisper-small chunked decode (~1–2s/chunk latency) — future upgrade path: KWS/wake-word models (openWakeWord/Porcupine) for sub-second detection, or streaming/incremental whisper**, gated on whether detection speed turns out to be the binding constraint (may not be — the anticipation tier doesn't need to race). Pure fns (`normalize_text`, `compile_pattern`, `TranscriptWindow`, `extract_patterns`, `MentionDetector.check` dedup) unit+doctested, mocked network in tests — no GPU/ffmpeg needed to test.

### `pm/capture/news_capture.py` + `~/.config/systemd/user/news-capture.service`

**⏸ STOPPED 2026-07-11** (fleet cleanup). **News-reaction (SPEED) thesis instrument.** Context: the analysis edge is dead (LLM loses to the market head-to-head, 776 forecasts — see `[[project_strategy_research_findings]]`), so the only remaining info edge is speed (a fresh catalyst the book hasn't repriced). Measure-first: this is the instrument, NOT a trader. Each cycle searches recent news (`web_context.search_news`, Google News RSS, cached 6h) for news-sensitive open markets (raw Kalshi categories: Politics/Elections/Economics/Financials/World/…; scalar/cumulative excluded) and records each FRESH item (published within `--freshness-hours`, deduped per ticker+url) with the market price at detection + the **published→captured lag** (our latency disadvantage). SQLite `data/shared/news_capture.sqlite` (env `NEWS_CAPTURE_DB`). `load_events()`/`stats()`. Pure `news_query_for`/`parse_published`/`is_fresh` unit-tested. **Early reading (2026-06-18): detection lag median ~150 min / mean ~230 min + 6h RSS cache → free-RSS speed is likely too slow for liquid markets; the forward-price join (next build) is the verdict.** Read-only; no trades. CLI: `python -m pm.capture.news_capture [--stats]`.

### `control_plane/` (`collect.py`, `web.py`, `templates/`, `static/`) + `~/.config/systemd/user/control-plane.service`

**Control-plane dashboard — one read-only ops page over the whole fleet** (the "professional surface", not idle-game). FastAPI+Jinja2 on **:8773** (project sidecar-UI standard; reuses baseball's `style.css`), `python -m control_plane.web` (env `CONTROL_PLANE_PORT`). `collect.snapshot()` aggregates, defensively (one failing source degrades to a placeholder, never crashes the page): **service health** (systemctl `--user show` of the curated `SERVICES` grid — feeds/idlers/arms/UIs), **data-panel growth** (each capture's `stats()`), **paper-arm P&L** (`exits_analysis`), **sidecar health** (`sidecars` registry), **live account** (best-effort read-only `kalshi_client.get_balance`). Routes: `/` (HTML, 30s auto-refresh) + `/health.json` (machine-readable). Pure `_parse_systemctl_show` + graceful-degradation of every collector unit-tested. Observe-only (no start/stop yet). Replaces ad-hoc bash monitoring.

### `pm/arms/arb_scan.py` + `~/.config/systemd/user/arb-scan.service`

**⏸ STOPPED 2026-07-11** (fleet cleanup). **Live cross-market arbitrage scanner / alerting idler** — guaranteed-profit one-winner fields net of cost. Unlike the legacy `scan_cross_market_arb` (mid prices, no fees → false arbs), uses **executable bid/ask + per-leg Kalshi fees**. Two sides: **OVERROUND** (Σ yes_bid > 1 → buy NO on all; *robust to incomplete fields* — a missing member only helps) and **UNDERROUND** (Σ yes_ask < 1 → buy YES on all; *fragile* — gated by a **completeness guard**: only trusted when Σ yes_bid ≥ `min_complete_bid_sum` [0.85], else the field is missing members incl. the dominant outcome and the basket would lose). Mutual exclusivity uses Kalshi's **authoritative `mutually_exclusive` event flag** (now carried through `get_markets`); falls back to a cumulative-threshold filter (`_is_cumulative_field`) + mid-sum band only when the flag is absent. Kills both false-arb classes (cumulative "Before <date>" thresholds AND independent multi-select like "Will Justice X resign"). Flags candidates → `data/shared/arb_candidates.jsonl` + loud log; does NOT trade. `--loop` runs as the idler (systemd, every 5 min, scans `--limit` 3000 open markets incl. thinner fields). Pure `find_arbs` unit-tested (14). Findings (2026-06-10): **no real arb across 3000 markets / 458 one-winner fields** — efficient venue; the candidates the widened scan surfaced were all incomplete-field underround artifacts, correctly rejected. CLI: `python -m pm.arms.arb_scan --limit 3000` or `--loop`.

### `pm/live/tennis_live.py`

**Tennis live layer — step 1 of react-and-hold for tennis** (scaling the validated MLB latency edge to a 2nd sport; "go wide"). The tennis sidecar is pre-match only; this adds the LIVE feed the reaction thesis needs. `live_matches()` polls ESPN tennis (scoreboard → tournament `groupings` → `competitions`) and parses each singles match's player names + per-set linescores + state/winner into a `LiveMatch`; `detect_events(prev, curr)` diffs two snapshots for high-leverage swings — **SET won, MATCH won, and BREAK**. Break detection is **serve-free** (ESPN exposes no server/point data — `serving=None`, `situation=null`, no summary endpoint): a player crossing to a **≥2-game lead within a set GUARANTEES a break occurred** (holds alternate the lead by ≤1) and is a real set advantage; only tested within a set (set count unchanged) so a set rollover isn't a phantom break, fires once per crossing (no re-fire as the lead extends). Pure `parse_competition`/`detect_events`/`_sets_and_games` unit-tested against real ESPN shapes (incl. break/hold/rollover cases). Read-only, no trades, no persistence yet. **Next**: (3) wire to a react-and-hold arm via the tennis ticker bridge — *gated on the step-2 measurement*. See `[[project_strategy_research_findings]]`, `docs/sports-model-playbook.md`.

### `pm/capture/tennis_capture.py` + `~/.config/systemd/user/tennis-capture.service`

**⏸ STOPPED 2026-07-11** (fleet cleanup). **Tennis capture — step 2: MEASURE the lag.** Joins `tennis_live` events to the Kalshi market: on a set/match won, maps the beneficiary → the market where they're the YES side (reuses `tennis._name_from_question` full-name bridge — Kalshi's 3-letter codes are unreliable; ambiguous/no match → SKIP, recorded as unmatched, never guess) and records the price AT DETECTION. Open ATP/WTA match markets via `tennis_runner._discover_tennis_markets` (events path — they don't surface in the volume feed). SQLite `data/shared/tennis_capture.sqlite` (env `TENNIS_CAPTURE_DB`); the loop carries per-match state in memory to diff snapshots. `load_events()`/`stats()`. Pure `_norm`/`build_player_index`/`match_market` unit-tested (accent-insensitive, surname fallback, ambiguity-drop). systemd `--loop --interval 45`. **The gate**: once a live tournament accrues events, measure whether the book moves toward the beneficiary after detection (does it lag like MLB?) — only then build step 3. Read-only; no trades. See `[[project_strategy_research_findings]]`.

### `pm/live/wnba_live.py`

**WNBA live layer — step 1 of react-and-hold for WNBA** (3rd sport). The WNBA sidecar is pre-game only; this adds the LIVE feed. **The lucky break: ESPN's basketball/wnba `summary` endpoint exposes a `winprobability` array** (`homeWinPercentage` per play) — so unlike MLB (we computed WPA) and tennis (no serve/point data), ESPN does the in-game win-prob modeling for us. `live_games()` polls the scoreboard, fetches `summary` only for in-progress games (carries winprob), parses into a `LiveGame`; `detect_events(prev,curr)` emits a **`wp_swing`** when homeWinPercentage moves ≥ `wp_threshold` (0.10) between polls (beneficiary = side it moved toward — the direct analog of a high-WPA MLB play) + a `game` event at final. Pure `parse_game`/`latest_home_winprob`/`detect_events` unit-tested. Read-only, no persistence. Mirrors `pm/live/tennis_live.py`.

### `pm/capture/wnba_capture.py` + `~/.config/systemd/user/wnba-capture.service`

**⏸ STOPPED 2026-07-11** (fleet cleanup). **WNBA capture — step 2: MEASURE the lag.** Joins `wnba_live` WP-swings to the Kalshi market: maps the beneficiary abbr → the KXWNBAGAME market where it's the YES side and records price AT DETECTION. Matching is **exact abbr→abbr** (beneficiary is an ESPN abbr; `wnba._parse_ticker` gives the yes-team abbr — cleaner than tennis's fuzzy names; same-team-twice → skip). Open markets via `wnba_runner._discover_wnba_markets` (events path). SQLite `data/shared/wnba_capture.sqlite` (env `WNBA_CAPTURE_DB`); loop carries per-game state to diff snapshots. `load_events()`/`stats()`. Pure `build_team_index`/`match_market` unit-tested. systemd `--loop --interval 30`. **The gate** (same as tennis): measure whether the book lags the WP swing before building the arm. Read-only; no trades.

### `pm/arms/pre_position.py`

In-game pre-positioning strategy. Uses `baseball.wpa` to compute target prices before scoring plays, evaluates both pre-position and reactive approaches, logs to `data/shared/pre_position_log.jsonl`. Targets close games (≤2 run diff, inning 4+). Systemd service `pre-position`.

### `pm/mention/audio_transcribe.py`

Reusable real-time audio→text via faster-whisper on GPU. Captures any audio source (URL, file, PulseAudio device) via ffmpeg, chunks to 16kHz mono, transcribes with VAD filtering. CLI: `python -m pm.mention.audio_transcribe "https://stream-url" --model small`.

### `pm/mention/feed_latency.py`

**Feed-latency measurement tool** — picks the fastest of 2+ live audio feeds carrying the same content, for the mention-cascade thesis (whisper detection speed is gated by input-feed latency, not decode speed). Read-only: captures short clips via ffmpeg (16kHz mono s16le), imports no trading modules, places no orders. `--source LABEL=SPEC` (repeatable, raw URL / `hdhr:<channel>[:<tuner>]` / `yt:<url>` resolved via `yt-dlp -g`) captures each concurrently for `--seconds` (default 90). Core pure fn `estimate_lag(sig_a, sig_b, sample_rate) -> (lag_seconds, confidence)`: envelope cross-correlation (rectify + downsample ~100Hz + `scipy.signal.correlate` full + argmax), positive lag = A leads B, confidence = normalized peak height in [0,1] (< 0.4 flagged LOW CONFIDENCE — feeds may not match or one failed). Total real lag = signal-domain lag + wallclock capture-start offset (each source's `time.time()` sampled just before its `Popen`; ffmpeg connection/startup latency is NOT subtracted, documented caveat). Report is pairwise ("`A leads B by X.Xs (confidence 0.NN)`") + `FASTEST FEED: LABEL`, printed and written to `data/shared/feed_latency_report.txt`. CLI: `python -m pm.mention.feed_latency --source YOUTUBE=yt:<url> --source CSPANORG=<hls-url> --seconds 90` (this week's C-SPAN YouTube-vs-.org comparison); later `--source OTA=hdhr:11.1:3 --source WEB=yt:<url>` for the OTA-vs-web reference delta. Env `HDHR_IP` (unset → an unroutable RFC 5737 placeholder; set your tuner's IP).

### `docs/sports-model-playbook.md`

**Repeatable recipe for standing up a new sport** (data→model→live→forecasts→resolution→strategy), baseball as worked example. Read before targeting a 2nd sport. Says what's reusable vs per-sport + the lessons (favorite-longshot bias works; in-game scalping + pitching factors didn't).
