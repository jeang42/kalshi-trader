# TODO / Roadmap

Living document tracking known issues and planned work. Group order reflects priority. When an item is shipped, move it under "Done" with the commit SHA or PR link.

---

## 🔧 Hardening pass (P0 + P1) — from the 2026-08-08 engineering review

An independent review graded the system **B−**: strong analytical engineering
on a weak operational base. It produced a prioritised remediation plan. P0, P1
and P2 are **complete and pushed**; P3 is the next session's work.

### ✅ P0 — release blockers (all closed 2026-08-09)

- [x] **R-01 Live configuration matched to intent** (`d969c91`). Canary
      disarmed; a measured-negative arm removed from the live allow-list; owner
      profile repointed off a paused arm. New `pm/trading/live_gates.py` resolves the gate
      chain the way a daemon does — the chain resolves differently depending on
      which process asks, which is how a live canary ran for a week while the
      notes called it dormant.
- [x] **R-02 Ledger writes atomic + locked** (`c7225c5`). `pm/core/atomic_io.py`:
      temp→fsync→rename, `.bak` recovery, quarantine of damaged files, advisory
      lock across the whole read-modify-write. Regression test loses 30 of 45
      appends with the lock removed.
- [x] **R-03 Backups consistent and proven** (`49ba58d`, `7392ee6`). Online
      SQLite backup into cold staging, integrity-checked; every run verifies the
      snapshot it wrote. Hardlinking actually works now (2nd snapshot 548 KB vs
      1.8 GB). Oversized archives skipped *and named*.
- [x] **R-04 CI re-greened and pushed** (`85dec0a`, `fe63b0b`). CI had **zero
      successful runs in 60**, back to 2026-06-02 — all three jobs failing,
      unseen. 182 lint + 32 type errors fixed at source, manifests completed,
      linters pinned.
- [x] **R-05 Drawdown breaker connected** (`3fc7cc3`). Was `in_drawdown_pause=
      False` with a TODO since the day it was written. Now account-level (also
      supplying the fleet-wide exposure cap that was missing) and fails closed.

### ✅ P1 (all closed 2026-08-17)

- [x] **R-06 Orphan-order reconciliation** (`cb46e88`). A submission that raised
      after the venue accepted left an unmanaged real position. Now reconciled
      by client order id; the false idempotency docstring corrected.
- [x] **R-07 Dependency manifests + lock file** (`fe63b0b`, `d1e8ad7`). Neither
      manifest described a working install. `requirements.lock.txt` verified by
      building a fresh 3.11 venv from it and running the suite green.
- [x] **R-08 Coverage gate extended to the arms** (`08f75ba`). Coverage was
      inversely correlated with financial risk. 44→73%, 55→78%, 21→61%, 25→62%.
      Writing the tests found three real bugs (see below).
- [x] **R-09 Test isolation from `data/`** (`cb46e88`, `f1b1b7f`).
      `profiles.PROFILES_ROOT` made lazy; all nineteen stores redirected; a
      session tripwire fails the run if the live tree changes.
- [x] **R-10 Failure alerting** (`cb46e88`). `pm/core/notify.py` + `pm/ops/health_digest.py` +
      systemd `OnFailure=` on all eleven units, pushing to phone via ntfy.
      Verified with a deliberately-failing self-test unit.
- [x] **R-11 Fleet topology in version control** (`443f950`). 39 units + crontab
      in `deploy/`, with an installer that dry-runs by default and deliberately
      never starts anything.
- [x] **R-12 Silent-handler audit** (`443f950`). Fixed the ones where swallowing
      costs money or corrupts the record — notably a failed profile restore
      leaving a process pinned to the wrong profile's ledger.
- [x] **R-13 Out-of-sample ledger** (`443f950`). `pm/ledger/oos_ledger.py` fingerprints
      selection parameters, records when they last changed, splits P&L at that
      boundary, and counts independent clusters. A re-fit resets the clock.
- [x] **R-14 Web-UI shared-secret gate** (`cb46e88`). Four LAN-facing UIs had no
      authentication; the control plane renders the real account balance.
- [x] **R-15 Retired-thesis archive** (`cb46e88`). 43 GB feed retired, archive
      verified then moved to bulk storage behind a symlink, ~40 GB reclaimed.

### 🐛 Bugs found *by* the hardening work (all fixed)

- **Tests reached the real venue** (found by R-21, fixed in `06dc2b9`). A
  `LiveBroker` orphan-adoption test left `cancel_order` unmocked and issued a
  real authenticated `DELETE` to Kalshi on every run (404), writing 70
  `cancel_*` rows into the LIVE `orders.log` — invisible to the tripwire,
  which did not watch `.log`. Now every test runs with `socket.connect`
  blocked, `ORDERS_LOG_PATH` redirected, `orders.log` watched by name; all
  2,180 tests still pass, i.e. nothing else was reaching the network.

- **Root-module doctests had never run** (found by R-17, fixed in `e200dd5`).
  `--doctest-modules` was in `addopts` while `testpaths` pointed only at
  `tests/`, so pytest collected zero of the ~88 doctests the root modules
  carry — every "doctested" pure function in the arms was a claim, and
  `docs/file-map.md` said they ran automatically. `testpaths` is now `["."]`
  (NOT a `"*.py"` glob — testpath entries bypass `--ignore`, and the glob
  imported `app.py` at collection, creating files in an empty `data/`; caught
  by the fresh-clone check); the isolation fixtures + tripwire moved to a root
  `conftest.py` so doctests get the same redirects. Three stale docstring expectations surfaced on the
  first run (all docstring-side, none a code bug).

- `fade_nearclose._open` raised `KeyError('volume')` whenever every in-band
  candidate was filtered out by category — `.map()` on an empty frame returns a
  `str`-dtype Series whose inverse drops all columns. `run_cycle` swallowed it
  and printed "discovery failed", so a routine no-op looked like a broken feed.
- `back_nearclose._open` had the same class of bug via `df[[]]`, which is
  *column* selection in pandas.
- `health_digest`'s own error handler used `fn.__name__`, turning "one check
  failed" into "monitoring died".
- `pm/ui/pages/kelly.py` read a variable ~40 lines before assignment, so the baseball
  prefill raised `NameError` on every render and was swallowed — silently dead
  while documented as working.
- `tennis/templates/index.html` 500'd whenever one player in a match was
  unrated (guarded one Elo, rounded both).

### ✅ P2 (all closed 2026-08-17)

- [x] **R-17 Attic for dead code** (`e200dd5`). Root 101 → 81 modules. Twenty
      retired modules + five tests + ten unit files `git mv`'d to `attic/`
      (excluded from ruff/mypy/pytest; `attic/README.md` is the index and holds
      the original `docs/file-map.md` rows verbatim). Rule: ❌ retired / falsified /
      scratch → attic; ⏸ stopped-but-re-enable-able stays. Ten units
      uninstalled from systemd, the orphaned primer cron retired. Coverage
      80.42% → 80.54% (direct tests replaced the incidental ones).

- [x] **R-22 Dependency vulnerability scan** (`d6c495b`). Never run before.
      New CI job `pip-audit`s the lock file `--strict` on push/PR and weekly;
      `make audit`. First result: no known vulnerabilities.
- [x] **R-19 Schema versioning + constraints on the arm stores** (`5792758`).
      `pm/core/sqlite_schema.py`: ordered `Migration`s, `PRAGMA user_version`, one
      transaction per step, fail-closed on a newer-than-code file, backup copy
      before any rebuild, race-safe across processes. All 13 root stores
      versioned; the four arms rebuilt to v2 with `CHECK`/`NOT NULL` (verified
      against every live row first — zero violations); the arms' `INSERT OR
      IGNORE` now checks rowcount because IGNORE swallows CHECK violations and
      `fade_retail` fired its live canary regardless. Rehearsed on copies of the
      live stores, then applied: rows/P&L/resolved identical.
- [x] **R-20 Graceful shutdown** (`5d45633`). `pm/core/shutdown.py`: SIGTERM/SIGINT set
      a stop flag (second escalates); loops check between cycles AND per
      market; `deferred()` masks the place-then-record window —
      `LiveBroker._place`, both arm canaries (buy+UPDATE+commit),
      `reactive_scalper` entry/exit/close. Verified in production: `SIGTERM
      received — finishing the current step` → `clean shutdown`.
- [x] **R-18 Stamp results with the producing commit** (`ab9d3a9`).
      `pm/core/provenance.py`; stamped on every arm position row (`commit_sha`, schema
      v3), `orders.log` audit line, `exits.jsonl`/`reactive_log` row, saved
      backtest run, text report, and the `oos_ledger --status` footer.
- [x] **R-21 Circuit breakers on outbound connectors** (`e254e11`).
      `pm/core/circuit_breaker.py` on every connector's HTTP entry point (one shared
      `kalshi` breaker for public + authed — an open circuit means an order is
      NOT placed). Open/close go to the health log; `health_digest` reports an
      open with no later close as a problem.

### ⬜ P3 — later, larger

- [x] **R-25 Split `docs/file-map.md`** (`8b12304`, 2026-08-23). 158 KB / 1,174 lines →
      49 KB / 553 lines. The file map (91 KB, 57%, rows up to 3,800 chars) is
      now 11 grouped sub-tables of one-to-two-line rows, each linking ↳ into
      `docs/module-notes.md` (74 sections, verbatim rationale); the signature
      block moved verbatim to `docs/api-surface.md`. Nothing deleted. Both
      gotchas sections, the DataFrame schemas and the env-var table stayed —
      cross-cutting rules belong in the file that is read every session. Sync
      checklist went 7 → 9 items so the index does not re-inflate.

- [x] **R-23** Structured logging + correlation ids — done 2026-08-24.
      `pm/core/logctx.py` (stdlib-only: ContextVar + logging.Filter):
      `bind`/`scope`/`current`/`setup` + JSONL mode via
      `POLYMARKET_LOG_FORMAT=json`. 37 per-entry-point `basicConfig` copies
      replaced by `logctx.setup()`; the arms' and strategy_runner's cycles
      bound; `execution._place` binds ticker/action/order at the live-order
      choke point; `orders.log` audit rows embed ctx — an order's audit trail
      now names the cycle that placed it (decision → order threading, as
      specified). Cycle-summary prints converted to log.info so the headline
      carries the cycle id. Verified live in journald:
      `… discovery: 1000 markets [arm=back_nearclose cycle=cyc-e5cce48e]`.
- [~] **R-24** Split the root namespace into packages; shrink `edges.py` (2,175
      lines, imported by nearly everything).
      - [x] **`edges.py` → `edges/` package** — done 2026-08-24. 2,175 lines →
            `__init__` (registry + re-exports) + `_base` + 8 source modules,
            largest now 360 lines. **Public API unchanged** (verified by diffing
            the full `dir()` + signatures + `SOURCES` + registered strategies
            before/after), so no caller, systemd unit or cron entry changed.
            Only behaviour change: patch internals at their real home
            (`edges.baseball._x`), not `edges._x`.
      - [x] **Phase A** — done 2026-08-24. The 33 modules with NO operational
            entry point moved under `pm/`: `venue` (7), `ledger` (7), `feeds`
            (10), `archive` (2), `live` (3), `trading` (4). Root 83 → 51
            modules. Strictly layered, verified 0 upward imports. 346 import
            statements + 102 patch-target strings rewritten across 133 files;
            `python -m live_gates` and `python -m oos_ledger` kept working via
            two deliberate root shims (documented operator commands). No
            systemd unit, crontab entry or daemon path changed. Proven by
            restarting `fade-nearclose` and watching a full cycle.
      - [x] **Phase B — `pm/core/`** — done 2026-08-24. profiles,
            sqlite_schema, provenance, circuit_breaker, atomic_io, shutdown,
            emergency_stop, notify, web_auth, sidecars; 79 files rewritten.
            Root 51 → 43 modules. Two more deliberate shims: `notify.py`
            (the `polymarket-alert@.service` OnFailure hook runs
            `python -m notify --unit %i` — that unit path migrates in Phase C)
            and `emergency_stop.py` (the documented kill switch must work
            during an incident).
      - [x] **Phase C — the entry points** — done 2026-08-24, user present.
            `pm/research/` (7), `pm/arms/` (8), `pm/capture/` (9),
            `pm/mention/` (7), `pm/ops/` (5). Root 43 → 8 non-shim files.
            17 unit files + 10 crontab entries + 3 scripts rewritten to
            `-m pm.<layer>.<module>`; `install.sh --install` applied; crontab
            installed (diff was exactly the -m rewrites, unrelated entries intact);
            running daemons restarted and verified cycling on the new paths
            before the monologue timers' 22:30 firing. Seven more gotcha-1b
            `__file__` anchors fixed pre-move (five silent `.env` loaders,
            the oscillation cache dir, `user_strategies/` discovery).
            user_strategies plugin contract is now
            `from pm.research import backtest`. Fifth shim: `health_digest.py`
            (RESUME §1 muscle memory).
      - [x] **Phase D — `pm/ui/`** — done 2026-08-24. `shared.py` →
            `pm/ui/shared.py`, `pages/` → `pm/ui/pages/` (15 routes; the
            `st.Page` paths in `app.py` updated — they resolve relative to the
            main script's directory). **`app.py` stays at the root
            deliberately**: the Streamlit main script's directory is what
            lands on `sys.path` for the whole process, which is the mechanism
            that makes `import pm...` work in every page. Verified two ways:
            AppTest executed the real app (all 15 st.Page paths resolved, the
            markets page rendered, no exceptions) and a headless
            `streamlit run app.py` booted to a healthy `/_stcore/health`.
            The pre-existing `pages` entry in `norecursedirs` covers
            `pm/ui/pages/` (basename pattern), so page modules stay out of
            doctest collection. R-24 root end-state: `app.py`, `conftest.py`,
            5 shims, `edges/`, `user_strategies/`.
      - [x] ~~The root namespace itself~~ — R-24 COMPLETE 2026-08-24, phases
            A→D in one day. 83 root modules → app.py + conftest.py + 5 shims.
            Optional remainder (a decision, not a task): fold `edges/` into
            `pm/research/edges/` — deliberately left where it is because its
            public API (`import edges`, `edges.<name>`, patch targets
            `edges.<src>.<internal>`) was kept stable through its own split.
            NOT started, and it is a different kind of change: 17 systemd
            `ExecStart` paths and 10 crontab entries name root modules
            directly, so moving an entry point is an operational change, not
            just an import change. Needs agreed package boundaries first —
            the import graph already suggests them (infra: `provenance`,
            `circuit_breaker`, `sqlite_schema`, `atomic_io`, `shutdown`,
            `emergency_stop`, `profiles`; venue: `kalshi_*`,
            `polymarket_client`, `provider`; arms; captures; research).
- [x] **R-26** Reproducible environment build — done 2026-08-24, the
      "or at least" path plus a container proof, deliberately NOT a
      containerized fleet (systemd+linger+journald+OnFailure IS the
      orchestrator; re-implementing it in compose on a one-machine system
      buys nothing, and the audio/GPU stack doesn't containerize anyway):
      - **Proven, not asserted**: fresh clone + clean 3.11 venv from
        `requirements.lock.txt` ONLY + empty `data/` passes the full gate
        (2,160 tests) — first time the lock was ever installed as a whole
        (CI installed requirements.txt; audit only scanned the lock).
      - `deploy/bootstrap.sh` — that proof as a script; stops before
        touching units/cron/data on purpose.
      - `Dockerfile` + `make docker-check` — the same proof in
        python:3.11-slim (gotcha #7 as an artifact); verified with a full
        --no-cache build.
      - CI job `test-lock` — installs from the lock on every push + weekly,
        so the lock can't rot between the CVE scan and the day it's needed.
      - `docs/second-machine.md` — the runbook for the rest of a machine:
        the secrets that exist NOWHERE else (.env + the Kalshi PEM — not in
        git, not in the backup; the PEM is unrecoverable, only replaceable),
        data restore + `--verify`, unit/cron install, the enabled-set as of
        today, and the same four verify commands as every resume.
- [x] **R-27** Concurrency tests for the multi-writer ledger — done
      2026-08-24. `tests/test_concurrency.py`, 6 tests, all real PROCESSES
      via spawn (threads share the fd table and would not exercise flock):
      the REAL ledger APIs under multi-writer load (`portfolio.
      record_transaction`, `position_meta` — one level above test_atomic_io's
      primitive, catching a mutation site that bypasses `mutate()`);
      SIGKILL-while-holding-the-lock (store keeps pre-crash contents AND the
      next writer proceeds immediately — flock dies with its holder, the
      property that makes it the right lock); plain-`json.load` readers never
      see a torn document under churn (os.replace atomicity for readers that
      know nothing of atomic_io); `ensure_schema`'s claimed cross-process
      init race (6 processes migrate one fresh store 0→2); and a pin on the
      stdlib `sqlite3.connect` DEFAULT timeout=5.0 busy handler, which is the
      only busy protection the stores have and is therefore load-bearing.
      **Mutation-tested**: with the flock neutered, the ledger tests fail
      deterministically with lost updates — green = the lock works, not just
      "the test ran".
- [ ] **R-28** `Decimal` for ledger money if transaction volume grows an order of
      magnitude.

---

## 🔴 Critical bugs

> Block correct behavior. Fix immediately when discovered.

- [ ] None currently known. (The JSON-string parsing bug that silently zeroed out Portfolio P&L was fixed in `6565163`.)

---

## 🟠 Real bugs / degradations

> Won't show up in tests but produce wrong-feeling output.

- [x] **Categorization now uses real `/events` tag data.** `get_events()` + `enrich_categories_from_events()` join markets to events via `event_id` and override the keyword-inferred category with the authoritative tag-derived one. End-to-end check on real data: "Other" dropped 51 → 2 markets out of 200; 64 markets corrected.
- [x] **`scan_price_divergence` now explicitly restricts to binary markets.** Previously it would silently report bogus matches for 3+ outcome markets (where `lastTradePrice` is per-market but `outcome_prices[0]` is one specific outcome, not a "YES"). For true multi-outcome divergence we'd need per-token last-trade data from CLOB — separate future feature.
- [x] **`load_markets()` cache key is the limit only.** Stale-data window surfaced in sidebar as "Page rendered at HH:MM:SS UTC".
- [x] **429 rate-limit backoff.** `_get` now detects 429, reads `Retry-After`, sleeps (clamped to [0.1s, 30s]), and retries. Other 4xx/5xx still raise immediately. Five new tests cover the path.
- [ ] **`agentic:favorite_grind` has 224 forecasts, 0 resolved.** Either the strategy targets markets resolving so far out that none have closed yet (plausible — favorites at 0.85+ often are far-dated), OR `auto_resolve_runner` has a bug specific to this source. Walk a sample of the 224 forecasts, check whether the target markets ARE in the closed-market lookup, and either confirm it's just "waiting for resolution" or find the bug.
- [ ] **Calibration log test pollution.** `agentic:_runner_test_buy` has n=1 from a leaked unit-test run. Doesn't change Brier-by-source aggregation enough to matter but the data is dirty. One-shot cleanup pass: identify any source-tag whose name starts with `_test_` / `_runner_test_` / smoke-test client_order_id prefix and delete those forecast rows.

---

## 🟡 Code quality / architecture

> Doesn't change behavior, but slows future work or hides bugs.

- [x] **Split `app.py` into Streamlit `pages/`.** `app.py` is now a thin `st.navigation` router; each page is its own file under `pages/`. Shared loaders live in `pm/ui/shared.py`. Fixed a latent bug while extracting: the sector exposure pie was nested inside `elif len(df_snaps) == 1` so it only showed on the first portfolio visit.
- [ ] **No `MarketDetail` abstraction.** Price History and Kelly pages independently re-parse `clobTokenIds`, `outcomes`, `outcome_prices` from the detail endpoint. Extract a `MarketDetail` dataclass / helper in `pm/venue/polymarket_client.py`.
- [x] **Structured logging.** Every module has `log = logging.getLogger(__name__)`. `log.info` on mutations (buy/sell, save forecast, create alert, fire alert, file migration). `log.warning` on recoverable failures (429s, mismatched market filter, etc.). Level controlled via `POLYMARKET_LOG_LEVEL`.
- [x] **`pyproject.toml` `[project]` section** added with name, version (0.1.0), description, deps, dev-extras, proprietary license marker, and `Private :: Do Not Upload` classifier.
- [x] **CI lint + typecheck.** Two new jobs in `.github/workflows/ci.yml`: `ruff check` and `mypy` on the four non-Streamlit modules. Both green locally and on push. `make check` runs all three (lint + typecheck + test) locally.
- [x] **Pre-commit hooks.** `.pre-commit-config.yaml` runs ruff (with auto-fix) + mypy + basic housekeeping (trailing whitespace, YAML/TOML validity, large-file blocking). Install with `pip install pre-commit && pre-commit install`.
- [x] **Lambdas removed from `kelly_sizing` return dict.** Result is now fully JSON-serializable. Callers do `bankroll * r["kelly_fraction"]` directly. New tests cover JSON round-trip and no-callables invariants.
- [ ] **`portfolio.json` is single-file, no concurrency guard, no backup.** Fine for solo local use; if app is multi-tab or deployed, race conditions are possible. Consider SQLite.

---

## 🔵 Feature roadmap

### High ROI

- [~] **Cross-sector inefficiency search → favorite-longshot mining.** Reframed the strategy hunt from per-strategy testing to "where is the venue mispriced?" Shipped `pm/research/inefficiency_sweep.py` (calibration-deviation map; classifies binary vs multi-outcome via Kalshi `event_ticker`; one-winner overround; venue-wide favorite-longshot table). First map (2026-06-10): sports efficient, ~89% of Kalshi is multi-outcome fields, favorite-longshot bias is the live lead. **Mined it** (`favorite_longshot_study.py`, net of fees + spread): at a tradeable near-close lead the bias does NOT produce a robust edge — uncertain bands starved (n=7 favorites in 14d), 100% win rates mean near-close prices are already right, fading longshots is fat-tail-fragile. The 24h "bias" was largely diffuse-pricing artifact. **Binding constraint = near-close sample size**, and history is unreachable (settled-events newest-first) → built `pm/capture/near_close_capture.py` (systemd idle-accrual engine: snapshots ~2h-pre-close price + outcome forward). **Next: let the panel accrue ~weeks, then re-mine** on real near-close data. See `docs/strategies.md` §20, `project_north_star_kalshi_leaderboard`.
- [x] **Portfolio transactions model.** Replace the "positions list" with an append-only transaction log (buy / sell, timestamp, price, qty). Derive open positions and realized P&L from the log. Unlocks averaging, sells, history, snapshot/calibration foundations.
- [x] **CSV export** on Markets, all Scanner tabs, Portfolio, and transaction history.
- [x] **Time-to-resolution column** on Markets and Portfolio. Days until `end_date`. Flag positions with <7 days left.
- [x] **Sector exposure chart** on Portfolio. Donut of cost basis grouped by category.
- [x] **Price alerts.** "Alert me when market X crosses $0.80." Checked on every page render against cached market data. Visible alert management page (active + triggered), sidebar badge, toast on fire. Fires once per crossing — reset to re-arm.
- [x] **Better error UI** for `load_markets()` — friendly card instead of stack trace when the API is down. Other call sites still bare.

### Medium ROI

- [ ] **Smarter dynamic bet sizing — anti-concentration, anti-overextension.** Fixed `--max-position-pct N` is a hard cap but doesn't reason about portfolio shape. Open questions to think about: (1) **Concentration penalty**: shrink the next bet when total cost basis is already heavy in one category/event/series. (2) **Drawdown-adaptive sizing**: when the profile is down N% from peak, automatically halve Kelly multiplier; restore when recovered. (3) **Edge-magnitude scaling**: a 2pp edge and a 15pp edge get the same half-Kelly fraction × bankroll today; the larger one probably deserves more weight up to a sensible cap. (4) **Price-aware caps**: a 5% cap at $0.50 risks $25 to win $25; the same cap at $0.05 risks $25 to win $475 — should the cap shrink as expected-payoff convexity grows? (5) **Per-strategy caps**: when one strategy dominates new opens (e.g. `agentic_baseball` opens 12 markets on a Saturday slate), throttle that strategy specifically rather than bankroll-wide. The fat-tail-dependent oscillation_momentum result motivates this — +8.9% backtest ROI shrinks to +4.8% with stricter caps but is more robust. Consider a `BetSizing` policy object that strategy_runner consults, mirroring `ExitPolicy`. Defer until we have 30+ resolved managed-trade outcomes from the existing cron to calibrate against.
- [x] **Volatility-aware trailing stop.** Shipped: `ExitPolicy.trail_stop_atr_mult` (K) + `trail_vol_window_hours`. Threshold = `K × σ(recent pct returns)` off peak. Per-position history fetch + per-cycle cache. Composes with fixed `trail_stop_pct` (whichever fires first wins). CLI: `--trail-stop-atr-mult 1.5 --trail-vol-window-hours 48`. 12 new tests including the quiet-vs-jumpy comparison (same drawdown, jumpy holds, quiet exits).
- [ ] **Hazard-rate / momentum-confirmed exit.** Each cycle, compute P(price reverses in next N hours | recent path) for every open position. Exit when probability > threshold. Two implementations possible: (a) simple "lower high after peak" detector + price-below-MA(20) confirmation (~50 lines, no training), or (b) learned model on historical paths from `backtest_cache/*.pkl` (real project; needs labeled "reversal" examples). Start with (a). Composes with two-stage TP — only fires after arming.
- [x] **Pre-event lottery scanner.** Shipped `pm/research/pre_event_scanner.py` + `strategy_pre_event_lottery` (marked UNTESTED in registry). Live finding on first run: Kalshi's sub-$0.10-with-1.5d-resolution universe today is **100% Drake-album tracker markets** — concentration warning correctly fires (30 markets, 1 underlying catalyst). Scanner works, but the thesis needs a broader market universe or genuinely diverse per-event lottery legs to be testable. Next step (un-done): backtest via `oscillation_backtest`-style framework once we have a 90-day cached dataset that includes resolving markets in the lottery range. Reaffirmed: the strategy needs paired model edge (baseball / LLM / crypto-spot) to be defensible, not a pure price-and-time filter.
- [x] **`oscillation_revert` strategy** ❌ failed (2026-05-23). 90-day Kalshi backtest, n=2155 markets, lead_hours=48, fees included: Brier 0.169 (vs market baseline 0.079, 2.15× worse), hit rate 25%, ROI -21.9%. Per-bucket P&L flat across `range_score` quintiles — the scanner's ranking metric is not informative. Kept in registry as a known-bad reference with a runtime warning. Full post-mortem in `docs/failed-experiments.md`. Open follow-ups: try long lead time (10d+), try inverted signal (momentum), try `range_score × range_width` composite.
- [x] **Scanner backtest framework — `oscillation_scanner` retrospective.** Built `pm/research/oscillation_backtest.py` + CLI. Initial run on 200 closed Kalshi markets (lead_hours=48, loose filters): 4 trades fired, 50% hit rate, +$— P&L (+31.6% ROI) vs market_consensus baseline at +1.3%. Sample size still too small for confidence (only 6-7 markets per bucket); needs to be re-run periodically as the closed-market universe grows, or extended to a date-range fetch instead of `limit=N`. Buckets 2 (-$—) and 3 (+$—) show high variance even with tiny n — directionally encouraging but not yet definitive.
- [x] **Calibration tracking.** Persist your probability estimates from the Kelly Sizer. After markets resolve, compute Brier score over your estimates. The killer feature for any Kelly user — tells you if your edge is real or if you systematically over/underestimate.
- [x] **Portfolio value snapshots.** Hourly auto-snapshot of cost basis + current value + realized P&L. P&L curve and max-drawdown stat on the Portfolio page.
- [~] **Live price ticker shipped via REST polling** (`shared.live_price_panel`, `@st.fragment(run_every=3)`). Delivers the live-update UX without WebSocket complexity. True push-based WebSocket integration is documented as an upgrade path in `developer-guide.md` — would require a singleton `LivePriceFeed` + background thread. Not pursued because for a single-user local app the polling UX is indistinguishable.
- [x] **Order book depth viewer.** `parse_order_book` + `order_book_stats` in the client; expander on the Price History page renders bid/ask staircase per outcome with best bid/ask/spread metrics, total liquidity per side, and a top-levels table.
- [x] **`/events` integration** for proper category data. (Event-grouping UI — showing related markets together under one event banner — is a remaining UI sub-task.)
- [ ] **Bulk price-history fetch** — currently price history page hits the CLOB API per outcome, sequentially. Parallel fetches with `concurrent.futures`.
- [x] **Estimated cash-out value column on Live Portfolio** — shipped `c2f2946`. Computes value, unrealized PnL ($ and %), status icon (🔒 closed). 6 tests.
- [ ] **"Cash out" button on each Live Portfolio position.** Two-click confirm modal showing: count being sold, expected price at current bid, expected proceeds, comparison vs holding to resolution. Sends `place_limit_order(action="sell", side=<held side>, count=|position|, price=current_bid)`. `place_limit_order` already accepts `action="sell"`; pre-flight guards mostly still apply (daily-cap, price-band, drawdown), but the per-order stake cap should NOT bite on sells. Single position only — no batch cash-out. Audit-logged like buys. Useful when (a) you want freed bankroll for another bet, (b) the market could still flip against you, or (c) resolution is far away.
- [ ] **"🧠 Analyse" link on each Portfolio row.** Unobtrusive link/button next to every position on 💼 Portfolio and 🏦 Live Portfolio that deep-links to 🧠 Deep Analysis pre-populated for that ticker. Streamlit supports query-params via `st.query_params`, so the link is just `?ticker=KXKY4R-26-TMAS&page=deep_analysis`; the Deep Analysis page reads `st.query_params.get("ticker")` at the top and seeds the lookup expander automatically when present. One small helper would let other pages reuse the pattern (Kelly Sizer, Edges, Recommendations rows). Saves the "copy ticker → switch page → paste ticker" friction every time you want to look at a position you already hold.
- [ ] **Kalshi market URL link in the UI.** Unobtrusive "View on Kalshi ↗" link next to the question title on every page that shows a selected market (Kelly Sizer, Deep Analysis, Price History, Markets row-detail, Live Portfolio reconciliation). Construction: Kalshi URLs are `https://kalshi.com/markets/<series-prefix-lowercase>/<event-slug>/<ticker-lowercase>` — we can derive the series prefix from the ticker (`KXKY4R-26-TMAS` → `kxky4r`), but the slug is venue-side and not in our schema. Two options: (a) `https://kalshi.com/markets/<series>/<ticker>` works as a redirect-friendly URL; (b) hit `events/<event_ticker>` and use the response's slug. (a) is simpler. Single helper in `pm/venue/kalshi_client.py` (`market_url(ticker) -> str`) that pages can reuse. Provider-level method would let Polymarket links work too.

### Low ROI / Niche

- [ ] **Multi-outcome Kelly.** Optimal allocation across N outcomes simultaneously (not just per-outcome). Mathematically elegant; rare in practice.
- [x] **Backtest mode** — shipped as Tier 3b (`pm/research/backtest.py` + 🧪 Backtest page + walk-forward eval + portfolio-constrained bankroll + market-baseline gate). Persistent run history added in `pm/research/backtest_history.py` + 📜 Backtest History page.
- [ ] **Correlation finder.** Cluster markets that move together for hedging suggestions.
- [ ] **Dark/light theme toggle** via Streamlit theming.
- [ ] **Saved searches / bookmarks.**

---

## 🟣 Production readiness

> Only relevant if this leaves localhost.

- [x] **Add LICENSE file.** Currently proprietary ("do not use" until the owner publishes a real license).
- [x] **Environment-based config.** API URLs, HTTP timeout, log level, and all three storage file paths now read from env vars with sensible defaults. `python-dotenv` is loaded at startup so `.env` works automatically. Template in `.env.example`.
- [~] ~~**Dockerfile + compose file.**~~ Owner doesn't use Docker; deployment stays as `./run.sh` for now. Removed from scope.
- [x] **Multi-user (Option A — file-based, single-machine).** Profile system in `pm/core/profiles.py` with `jeff`/`user2`/`sandbox` defaults; per-profile data dirs; sidebar selector; sandbox reset. Each data module now reads its storage path lazily so profile switches take effect without re-importing modules. Adequate for shared family laptop. **Option B (FastAPI + auth + SQLite for network/concurrent access)** is the still-open production-readiness item below.
- [x] **Paper-trading polish for sandbox.** Banner, watermark, walkthrough expander. Helpers in `pm/ui/shared.py`; wired into Kelly Sizer, Portfolio, Alerts pages.
- [ ] **Authentication + per-user access control (Option B).** When sharing across the web, Streamlit's session model doesn't scale to concurrent users. The replatform path is FastAPI + auth + SQLite, with the existing per-profile data directories mapping cleanly to user-ID rows. Deferred until actually needed.
  - **Role model**: `admin` (Jeff) has full access to every page, every model, every setting, every profile. `user` (everyone else) is restricted.
  - **Non-admin restrictions** (target list — not exhaustive):
    - Cannot see or switch profiles. Each user logs in to their own profile and that's it.
    - Cannot pick LLM models freely — locked to one (or a short whitelist) per user, set by admin.
    - Cannot create new profiles, reset profiles, or delete data.
    - Cannot read other users' portfolio / forecasts / orders / settings.
    - Cannot see the Kalshi authenticated pages (🏦 Live Portfolio, 📑 Orders) — the live API key belongs to the admin only; non-admins are paper-only (`is_owner_profile_active` already enforces this for the auth layer, but the page nav itself should be hidden).
    - Cannot change risk-management settings beyond bounds the admin sets.
    - Cannot run the agentic runner CLI or any other CLI surface — they don't have CLI access in the first place.
  - **Auth UX**: web-only login (username + password, or OAuth via Google/GitHub). No shared CLI; the only way in is the web app.
  - **Architectural risk**: Streamlit doesn't have first-class auth. Two paths — (a) keep Streamlit, put it behind an auth proxy (oauth2-proxy, Authelia) that injects user identity via headers, and have the app read the header to set profile + role; (b) full replatform off Streamlit to FastAPI + a real frontend. (a) is faster but uglier; (b) is the long-term right answer.
  - **Schema migration**: profile-dir-per-user maps cleanly to a `users` table when we go SQLite. Per-profile JSON files (`portfolio.json`, `forecasts.json`, `alerts.json`, `settings.json`) become rows keyed on `user_id`. The lazy-path pattern in `pm/ledger/portfolio.py` / `pm/ledger/calibration.py` / etc. means swapping JSON for SQLite touches one function per module.
- [ ] **Switch storage from JSON to SQLite.** Concurrency, indexing, transaction safety.
- [x] **Structured logging → stdout (already journalctl-friendly).** Format: `HH:MM:SS [LEVEL] module: message`. Module-level loggers in every data module. Log level configurable via `POLYMARKET_LOG_LEVEL`.
- [ ] **Health endpoint** for monitoring.
- [x] **Versioning + CHANGELOG.** v0.1.0 tagged; `CHANGELOG.md` follows Keep-a-Changelog; `pyproject.toml` carries the version.
- [ ] **If multi-user is needed, replatform off Streamlit.** FastAPI + React/Svelte. Streamlit's session model doesn't scale to concurrent users well.

---

## ❓ Open questions

- [ ] Should outcome labels be normalized? Polymarket uses `"Yes"`/`"No"` but some markets use `"Up"`/`"Down"`, candidate names, etc. Currently we pass through as-is. Probably correct — but document the choice.
- [ ] Should `kelly_sizing` apply a "max bet cap" parameter? Real-world Kelly users almost never bet full Kelly. Maybe accept a `kelly_multiplier=0.5` arg to bake in half-Kelly as default.
- [ ] When a market resolves, should the position auto-close and roll into realized P&L? Or stay as a row showing the final outcome? Today: nothing happens — the position persists with stale prices.

---

## 🏛️ Multi-venue support (Polymarket / Kalshi / Polymarket-US)

Triggered by the question "what can US persons actually trade on?" Kalshi is CFTC-regulated and US-legal; Polymarket US (via QCX) is launching on a waitlist as of mid-2025.

- [x] **Phase 1 — Provider abstraction.** `pm/venue/provider.py` with `MarketProvider` Protocol + `PolymarketProvider`. Pages route network calls through `get_provider()`; pure helpers stay where they are. `MARKET_PROVIDER` env var selects active provider. Registry supports dropping in additional providers with no changes to `pm/venue/provider.py`.
- [x] **Phase 2 — Kalshi data layer.** `pm/venue/kalshi_client.py` + `KalshiProvider` auto-registered into the provider registry. Public-read endpoints only (no auth). Schema normalized — same DataFrame columns as Polymarket so scanners/Kelly/Portfolio work unchanged. Order book adapter mirrors NO prices to YES asks so `parse_order_book` is venue-agnostic. End-to-end verified on live API. 28 new tests.
- [x] **Phase 3 — UI + make Kalshi primary.**
  - [x] Default switched to Kalshi (`DEFAULT_PROVIDER = "kalshi"` in `pm/venue/provider.py`; `.env.example` + developer guide updated). `MARKET_PROVIDER=polymarket` still works as an override.
  - [x] Sidebar provider selector in `app.py` — uses `provider.list_providers()` + `display_name` for labels; on switch calls `set_active_provider()`, clears the data cache, and reruns.
  - [x] UI-quirks audit: venue-aware page titles, CSV filenames, and copy via new `shared.venue_name()` / `venue_display()` helpers. The big real bug — Price History reaching into Polymarket-specific `clobTokenIds` — is fixed by the new `MarketProvider.list_outcome_tokens` method. Both providers verified end-to-end against all 8 routes.
- [ ] **Polymarket US (via QCX)** — drop in as a third provider once the owner gets early-access. Same Polymarket schema; different endpoint and auth.

---

## 🛡️ Recommendations roadmap

Full design: [`docs/recommendations-design.md`](docs/recommendations-design.md).

- [x] **Tier 1 — Risk management.** `pm/ledger/risk.py` ships with calibration-adjusted Kelly (Brier → multiplier), drawdown circuit breaker, sector concentration warning, per-market position-size check, and a Settings page. Used by Kelly Sizer and Portfolio.
- [x] **Tier 2 — Cross-market arbitrage scanner.** `scan_cross_market_arb` groups markets by `event_id`, sums YES prices per event, and flags deviations from 1.0 with implied edge % and total liquidity. Surfaced as the 4th tab on the Scanner page. Explicit "candidates not guarantees" warning + plausibility-band filter for the non-mutually-exclusive case.
- [~] **Tier 3a — Historical calibration backfill (shipped).** `get_closed_markets`, `market_price_at_offset_before_close`, `get_market_calibration_data` + market-side Brier/calibration-bins functions. New section on the Calibration page shows the market's baseline Brier with side-by-side comparison to the user's. Answers "can we get calibration data without waiting weeks?"
- [x] **Tier 3b — Backtest framework** (gates Tier 4+). `pm/research/backtest.py` ships `MarketSnapshot` / `Resolution` / `Signal` / `BacktestResult`, a lookahead-safe `build_dataset`, a `run_backtest` engine, four starter strategies (market_consensus, always_yes_flat, momentum, mean_reversion), Wilson-CI hit rate, drawdown anchored to $0, per-category breakdown, and a `market_baseline_brier` gate that the new Backtest page surfaces as pass/fail.
  - [x] **Walk-forward (v2).** `run_backtest_walk_forward` walks the price history at fixed steps (default 24h), tracks one in-or-out position per market, marks to market on signal flips, exits at $1/$0 on resolution. Reuses `BacktestResult` shape (extra trade-row columns: `entry_time` / `exit_time` / `exit_reason` / `holding_period_days`). Backtest page has a Single-shot/Walk-forward toggle.
  - [x] **Transaction-cost layer.** `run_backtest` gained `half_spread` + `fee_pct` so the gate models real fills (effective entry price = mid + half-spread on chosen side, fees deducted per-trade). `BacktestResult` exposes `implied_spread_cost` / `total_fees` so cost-sensitivity is visible. Default half-spread is 0.005 (½¢/side; typical of liquid prediction markets). Pre-cost tests pin the zero-cost baseline; new tests cover the cost-aware paths including "spread eats edge → Kelly fraction zero → trade skipped".
  - [x] **Portfolio-level bankroll constraint.** Both engines now thread a `portfolio_constrained` parameter (default on). Single-shot processes markets in chronological as_of order with cash settlement before each open; walk-forward uses a new `_walk_forward_constrained` helper that interleaves every market's eval + resolution events into one time-ordered queue with shared cash state. New `BacktestResult` fields: `min_cash_seen` / `n_clipped_for_cash` / `n_skipped_for_cash`. Backtest page exposes a toggle and a three-metric panel. Live demo: a 50%-per-market strategy that previously implied 9× leverage on a $1k bankroll now correctly reports 2 trades / 16 skipped.
  - [x] **User-defined strategy plugins.** `backtest.register_strategy(key, display_name)` decorator + `discover_user_strategies()` scanner. Drop a Python file in `user_strategies/` and a `@register_strategy`-decorated function appears in the Backtest dropdown on the next reload. Broken plugins are logged and skipped. Includes a starter example.
- [~] **Tier 4 — External data + LLM scoring.** Framework + first source landed; remaining sources gated on backtest results.
  - [x] **Crypto spot prices.** `pm/feeds/crypto_data.py` (Coinbase Exchange — Binance is US-geoblocked) + `edges.py` (parser, dispatcher, Edge dataclass, find_crypto_edges, registered backtest strategy). Both terminal and one-touch barrier models implemented; verb-aware dispatch ("be above on DATE" → terminal, default → barrier; "dip to $X" / "fall to $X" → barrier-from-above). 🔭 Edges page renders flagged candidates with explicit advisory framing. **Status**: surfaced as advisory; not yet allowed to influence Kelly sizing — gate is the Brier-vs-baseline check on the Backtest page using the registered "crypto_spot_lognormal" strategy.
  - [x] **Sports** (ESPN). `pm/feeds/sports_data.py` pulls MLB / NBA / NFL / NHL / WNBA scoreboards and derives vig-free win probabilities from `pickcenter` moneylines. `edges.find_sports_edges` matches questions by team-name alias and skips derivative markets (O/U, spread, props). Live-verified on Polymarket. Backtest gate exists (`sports_winprob` strategy) but requires an archival data source for historical games — open follow-up.
  - [x] **Politics** (cross-platform comparison via Manifold Markets — the cleanest free option after 538 shut down and Metaculus added auth). `pm/feeds/manifold_data.py` + `edges.find_manifold_edges` + Jaccard-similarity matching. Direction-blindness fix shipped via `llm_matcher.verify_match` as an opt-in LLM verification step (Jaccard pre-filters cheaply; LLM judges only the survivors). The "real" polling-aggregator wedge (538 historical archive for backtest only, Wikipedia polling scrape for live) is still open if needed but the v1 source is shippable.
  - [x] **Macro** (FRED). `pm/feeds/macro_data.py` wraps the unauthenticated `fredgraph.csv` endpoint (the one used for chart embeds); supports threshold questions on unemployment + 2y/10y Treasury yields + fed funds effective rate via a normal-on-level model with cadence-aware realized vol. Backtest gate is now **lookahead-honest** via `value_at` / `realized_volatility_at` — the strategy sees only data available at `snap.as_of`, modulo FRED's small publication-vs-observation-date leak (documented on `value_at`). Open follow-ups: CPI YoY (needs index→% transformation), GDP growth (annual cadence), Fed-cut-count markets (need rate-futures data or step-function model), ECB equivalent if euro-area markets show up.
  - [x] **LLM scoring** via local Ollama (`pm/feeds/llm_data.py` + `edges.py` integration). System prompt engineered for calibration discipline; UI auto-discovers pulled models via `/api/tags`; registered as the `llm_forecast` strategy in the backtest registry. Advisory until backtest Brier beats the market baseline on a category.
  - [x] **Persistent LLM cache** (`pm/feeds/llm_cache.py`). SQLite K/V at `data/shared/llm_cache.sqlite` backing both `llm_data.get_forecast` and `llm_matcher.verify_match`. Cache-by-default with `bypass_cache=True` escape hatch. Keys carry a schema-version prefix; match keys sort the pair canonically. Settings page exposes stats + clear/purge buttons. Verified ~6800× speedup on warm forecast.
  - [x] **Calibration-page integration for LLM forecasts.** Forecast schema gained `source`/`notes` (backwards-compatible); Edges page has "📝 Log N forecast(s) to Calibration"; Calibration page shows per-source Brier breakdown + source filter. Per-source `brier_by_source` lets user vs each LLM vs crypto_spot track separately without polluting hand-recorded calibration.
  - [x] **Auto-resolution.** `cal.auto_resolve_pending(closed_lookup)` matches pending forecasts against the union of `get_closed_markets()` from every registered provider. Calibration page's "🔄 Auto-resolve" section runs it on demand (cached 10min). Already-resolved forecasts are never overwritten.
- [~] **Tier 4.5 — Manual-confirm order placement (Phase 4 shipped, Kalshi-only).** `pm/venue/kalshi_auth.py` + `pm/venue/kalshi_orders.py` + 🏦 Live Portfolio + 📑 Orders pages. Three-layer guard: `KALSHI_LIVE_ENABLED=true` env, owner-profile lock (default `jeff`), pre-flight checks (stake cap, daily cap, fat-finger price band, drawdown breaker). Limit-only, single-order, audit-logged. Reconciliation panel on Live Portfolio shows paper-vs-real drift. Calibration auto-resolve consumes Kalshi settlements when authed. Phase 3 (slippage ground truth from real fills) and Phase 5 (agentic auto-execution) gated on accumulating real-fill data.
- [ ] **Phase 3 — Slippage ground truth.** Calibrate `pm/research/backtest.py`'s `half_spread` / `fee_pct` defaults from realized fill data once enough live trades have accumulated.
- [~] **Phase 5 — Automated execution (LIVE 2026-06-02).** Shipped `pm/trading/execution.py` — a `Broker` seam (`PaperBroker` default | `LiveBroker`) that lets a strategy daemon place real Kalshi orders without changing strategy logic. Triple-gated (`LIVE_TRADING_PROFILES` allow-list + `KALSHI_LIVE_ENABLED` + not emergency-stopped); dormant by default. Wired into `longshot_scalper.py` (favorite-hold the first live arm — buy-only, holds to resolution). Marketable exits (Kalshi has no native stop — verified), integer-contract + stake-cap resize for min-size, venue-truth position/cash reads, resolution auto-settles (no sell). Go-live tooling: `pm/trading/live_preflight.py` (read-only) + `go_live.sh` (guarded). `docs/live-execution-adapter.md` is the design + runbook. **Went live at min size on `agentic_favorite_hold`** — first real orders placed + filled, write path proven. Still open: wire `pm/arms/strategy_runner.py` (WNBA/tennis arms) through the broker; auto-feed the drawdown gate (`in_drawdown_pause` currently stubbed); promotion criteria (Brier < baseline, N resolved) before scaling size.

- [ ] **Environmental / contextual factors in the baseball model.** The v0.1 matchup model is intentionally minimal — regressed Pythagorean + a single global +0.161 home-field log-odds + SP-ERA. Several physical/contextual factors are well-established in sabermetric literature to meaningfully shift win probability and should be folded in once the basic model has a Brier baseline that suggests it's worth augmenting. Group these by where they architecturally belong:

  **Lives inside `baseball/` (intrinsic to the game):**

  - **Park factors** — each MLB park has stable multipliers for runs, HRs, doubles, etc. Coors Field plays ~1.30× for runs (altitude + dry air), Petco ~0.92×, Marlins Park ~0.85× pre-renovation. Calculable from the historical play-by-play we're already collecting; a `baseball/park_factors.py` derives per-park multipliers from a few years of data, applied as an adjustment to expected_run_diff before Pythagorean. **Highest expected impact** of anything in this list — Coors-Field games have a known signature (high totals, lower variance in win prob from runs since both teams score).
  - **Stadium-specific home-field advantage** — the global +0.161 log-odds is a league average. The real number varies by park: Coors home-field is ~+0.18 (altitude + crowd), domed/neutral parks like Tropicana sit closer to +0.12. Derive empirically from historical home win rates per stadium.
  - **Altitude** — Coors Field at 5,200ft has measurable ball-flight effects beyond the standard park factor. Mostly captured by park factors but worth flagging as a separate axis if we ever try to model HR-rate-dependent edges.
  - **Roof status** — retractable-roof parks (Globe Life, Minute Maid, T-Mobile) play meaningfully differently roof-open vs roof-closed (HR rate, wind, climate-control effects). Roof status is exposed in the MLB feed/live `gameData.weather` block — we already collect this in `raw_json` on each play.
  - **Travel + scheduling fatigue** — west-coast teams playing east-coast night-of-arrival historically underperform by ~2-3pp. Day-after-night-game schedules with cross-country flight ("getaway day" → cross-time-zone start) hurt the visiting team. Derive from schedule data we already have (consecutive game start times + venue locations).

  **Belongs in a separate sidecar (`weather/` or similar):**

  - **Weather** — wind direction + speed dominates HR rate (wind blowing out at Wrigley is famous), temperature affects ball flight (~3ft per 10°F), precipitation affects rain delays and bullpen usage, humidity affects ball drag. Best fit: a `weather/` sidecar that the baseball sidecar imports from (sidecars can import each other; only the market app is forbidden upstream). Sources to consider: NWS forecasts via api.weather.gov (free, no auth), OpenWeatherMap free tier, or wunderground (paid). Surface as `weather.forecast_for_game(game_pk, latlon)` → `{temp_f, wind_mph, wind_dir_deg, precip_pct, conditions, roof_likely_status}`.
  - **The baseball matchup model would consume the weather sidecar's output** via a feature adjustment in `model.predict_matchup(... weather=...)`. Wind blowing out + power-hitter team = HR-rate adjustment that flows back into expected run production.

  **Cross-cutting:**

  - **Day-night swing** — day games and night games at the same park play differently (shadows, temperature, crowd energy). Easy adjustment from game start time + park sun angle. Marginal but cheap.
  - **Bullpen workload / fatigue** — heavily-used bullpen yesterday means tonight's late innings are vulnerable. Track via play-by-play (we have it). Important for late-game win-prob calls; v1 model ignores entirely.

  **Architectural notes:**

  - Park factors + travel + day-night belong in `baseball/`. Build them as pure functions taking game_pk → adjustment, feed them through `model.predict_matchup` as new optional kwargs. Backward compatible.
  - Weather is the first sidecar-to-sidecar dependency in the project. Worth documenting the pattern in `docs/sidecar-protocol.md` when we get there — protocol probably needs no changes, but the example sets precedent.
  - **Don't build all of these at once.** Add park factors first (largest single effect, calibratable from data we have), measure Brier delta, iterate.

  **Prerequisites:**
  - A Brier baseline of the v0.1 model that says it has *some* edge to augment. Otherwise we're piling complexity onto a broken base.
  - For park factors: at least one full season of historical play-by-play. Our 30-day backfill is enough for a first-cut estimate but properly seasonal park factors need years.
  - For weather: at minimum a `weather/` sidecar scaffolded with sources and a public API.

- [ ] **Late-inning / clutch / pressure performance metrics.** The chat surface gets asked this question naturally ("do any teams excel under pressure in the late innings?") and currently has no data layer to answer it. Three distinct things, with very different signal-to-noise:

  **What's mostly noise** (per decades of sabermetric research):
  - Team "clutch hitting" — overall OPS-with-RISP, OPS-late-and-close. Doesn't repeat year-over-year. A team's clutch number is essentially random walk around their overall OPS. **DO NOT weight heavily in any model.**

  **What has real signal**:
  - **Late-inning bullpen quality** — we partially capture this with v0.2's full-bullpen ERA, but a team with a 2.50-ERA closer + a 5.00-ERA setup man behaves very differently from a 3.50-ERA pen with uniform quality. Per-leverage-tier bullpen splits would refine the existing bullpen layer.
  - **Closer reliability** — saves / blown saves / inherited runners stranded. Repeatable individual skill. Maps to "the team's late-game ceiling."
  - **WPA-from-our-own-plays** — every play in `live_plays.raw_json` carries `leverageIndex` and the win-prob delta. Aggregated per team per window, this is "did THIS team's at-bats / pitches actually move the win probability in high-leverage spots" — measured on actual games, not pre-aggregated league stats.

  **What to build (when ready, gated on v0.4 Brier showing the existing layers help)**:
  - `baseball/clutch.py` with:
    - `team_late_close_stats(team_id, season)` — pull MLB API split (sitCodes=la)
    - `team_bullpen_by_leverage(team_id, season)` — refine bullpen ERA into high/medium/low leverage tiers
    - `team_wpa_summary(team_id, window_days)` — aggregate WPA from our captured plays
  - State table `team_situational` (team_id, season, sitcode, ops, era, ...) idempotent on (team_id, season, sitcode)
  - Model integration **cautious** — possible weights to consider:
    - As bullpen ERA refinement: replace `bullpen_logodds_adj(era)` with a leveraged version that weights high-leverage innings more.
    - As a separate small layer: `CLUTCH_LOGODDS_PER_OPS_GAP = 0.5` (much smaller than SP/lineup weight reflecting the noise-heavy nature). A 0.080 late-and-close OPS gap → ~2pp swing.
    - As exposition only: surface in the Stats page + chat context but DO NOT modify predict_matchup until Brier-tested.

  **Discipline notes**:
  - All late-inning metrics need ≥30 PA / IP in the window before being reported, else fall back to season baseline.
  - Stats page should show a "regression warning" when displaying clutch numbers — they look more meaningful than they are.
  - LLM chat KNOWLEDGE_BLOCK should be updated to say "team-level clutch is mostly noise; bullpen-by-leverage IS real" so it doesn't oversell.

  **Estimate**: ~1 day for the MLB API splits + WPA aggregation + state. Another day to A/B-test integration into model.

- [ ] **Situational + matchup-pattern analysis on `live_plays`.** The play-by-play table already has every event with inning / half / score, and the `raw_json` blob carries the full MLB play context (count, runners on each base, outs, leverage index, base-out state, win-probability before/after). Four-level deepening, each only worth doing once the previous one shows a real signal:

  **Level 1 — Descriptive aggregates** (~half-day). Team event-type profiles (K rate, HR rate, walk rate by team), inning-by-inning splits, pitcher signatures (GB% vs FB%). Pure SQL on what we have. Renders as a "Team profiles" page on the baseball web UI. Useful even if nothing deeper pans out, because the existing simple model can directly absorb adjusted rates (e.g. RS adjusted for RISP performance feeds Pythagorean).

  **Level 2 — Situational** (~1 day new ETL). Parse `raw_json` into a derived `play_context` table with count, base state, outs, leverage, WPA per play. Unlocks: RISP performance, high-leverage clutch metrics, count-based tendencies (does team X swing at 3-0?), Win Probability Added per team / per player. New `baseball/situational.py` + a "Situational profiles" tab.

  **Level 3 — Matchup / pattern matching** (1-2 weeks, where "counter/receive" lives). Cluster teams into archetypes — aggressive contact hitters vs patient power, GB staffs vs K staffs, etc. — and ask "when archetype A plays archetype B, what's the historical edge?" Build a matchup-adjustment layer that modifies the base Pythagorean win-prob based on archetype interactions. **Hard requirement: at least one full season of data, ideally 2-3.** Our 30-day backfill is not enough — pattern noise dominates. Sportsbooks have entire quant teams doing this with Statcast pitch-tracking + Baseball-Reference splits + 10+ years of history; we won't beat them on inputs.

  **Level 4 — ML feature model** (months, real overfitting risk). Train XGBoost on (situation features, team archetypes) → P(outcome). Probably not worth it. The simpler stats model is more honest and the model-vs-consensus Brier gate would have to be very generous for an ML model to look better than noise.

  **Prerequisites** (in order):
  - Brier baseline of the basic Pythagorean+SP-ERA model vs `sports_winprob` (ESPN consensus). If basic model loses head-to-head, no point adding complexity — fix the simple thing first.
  - At least one season of forward play-by-play collection. Historical backfill is observational only; combining with forward predictions for proper validation needs forward data.
  - Decide the integration point: is "patterns" a separate analysis sidecar surface, OR is it a feature-engineering layer feeding the existing matchup model? My current read: the actionable version is the latter (the simple model absorbs adjusted features like "team_X_effective_RS = RS × RISP_performance_factor") rather than a parallel analytics module the trader ignores.

  **Failure modes to avoid**:
  - Building Level 3-4 on small data → finds patterns in noise → backtest looks great but live performance reverts to baseline + spread costs
  - Treating "patterns" as a standalone analytics page that doesn't feed the trading model → looks impressive in demos but doesn't move Brier
  - Skipping Level 1-2 to jump to ML → no descriptive baseline means you can't sanity-check whether the model is finding real signals or hallucinating

  **What's safe to do anytime**: Level 1 descriptive aggregates. They're useful as exploration tools regardless of model integration. Treat the rest as gated on having data + a Brier baseline that says the basic model has some edge.

- [~] **Lead-lag analysis: live MLB events × market book reactions.** The baseball sidecar's `live.py` collector is now running, persisting every play of every in-progress MLB game to `live_plays`. The existing `book_poller` separately snapshots Kalshi MLB order books every 5min. Join the two streams and ask: after a game event of type X (home run, key strikeout, error in scoring position), how long does the Kalshi market take to reprice, and by how much? If there's a consistent reaction window measurable in seconds-to-minutes, a front-run strategy becomes plausible. Concrete deliverables: (a) `baseball/analysis.py` with `lead_lag_for_event(event_type, window_minutes)` returning the empirical reaction curve; (b) a small analysis page on the baseball web UI showing the curves; (c) IF the lag is exploitable: a `pm/capture/mlb_book_poller.py` for higher-frequency MLB-only book polling (30-60s vs the current 5min), plus a strategy adapter in `edges.py`. **Don't build the trading layer until the analysis tells us a window exists** — failure mode is shipping a low-latency trader on top of "wishful thinking" reaction times.
  - **Status update (2026-06-05): analysis done → window exists → arm built.** `reaction_decay.py` measured the latency-decay curve: full-sample reaction ≈0, but HIGH-LEVERAGE plays (the edge is entirely leverage-dependent) show **~+2.5¢ that persists ~60s** — far longer than our ~15–30s pipeline, so reachable. Foundational finding from the same dig: the MLB game-winner market is **efficient on a static basis** (calibrated across the full price range incl. extremes) → static mispricing strategies are dead; speed is the only surviving edge. Built `pm/arms/reactive_scalper.py` (`reactive-scalper.service`, paper, profile `agentic_reactive`), **WPA-conditioned** (`|WPA| ≥ 0.10` via `baseball.analysis.wpa_for_play`) — catches high-impact non-scoring plays, skips low-impact runs. Unvalidated (fat-tailed, thin vs spread) → gathering forward P&L. See `docs/strategies.md §19`, `project_strategy_research_findings`. Open extensions: other play types / base-out states.
  - **Status update (2026-06-06): reactive arm taken LIVE at low value.** First *high-frequency* live arm — `go_live.sh --profile agentic_reactive --service reactive-scalper --max-stake 2` ($2/order, `LIVE_MAX_ORDERS_PER_DAY`=100). First real order filled (`KXMLBGAME-26JUN062210NYMSD-NYM`, 2 contracts @ $0.71). Fixed two go-live bugs found en route: (1) `reactive_scalper` never pinned `POLYMARKET_ACTIVE_PROFILE` / loaded `.env` → daemon silently ran paper despite go-live; (2) `react_entry` was logged for *every* `broker.buy` even on rejection (OrderResult doesn't raise) → now only real fills are `react_entry`, non-fills are `react_reject` (honest live track record). At $2/order the test validates **execution of a fast multi-order strategy**, not the edge (fees ~$0.05 are a large % of stake). Caveats + deferred refinements: `project_strategy_research_findings`.

- [~] **Baseball win-prediction model with persistent season state — sidecar architecture.** Standalone stats engine, not a strategy plugin. The prediction-market app queries it when it needs an MLB probability; the engine itself runs on its own schedule, maintains its own state, and could grow its own UI. Multi-component but well-bounded.

  **Architecture**: Sub-package in this repo to start (`baseball/` directory: `data.py`, `model.py`, `state.py`, `runner.py`). Same repo, same `data/shared/` SQLite store, same profile system, but a clean import surface — the market app only ever calls `from baseball import predict_game(home, away, date, pitchers=None)`. No domain-bleed: nothing in `baseball/` imports `kalshi_client`, nothing in `kalshi_client` imports `baseball`. If the engine ever gets reused by a stock-research spin-off or another app, promote to its own repo + pip install (Option B); not worth the multi-repo overhead at single-machine scale today.

  **Generalizes to other sports**: this is the *first* of N sport engines. NBA, NFL, NHL, WNBA each become their own sidecar sub-package (`nba/`, `nfl/`, etc.) with the same shape — `data.py` + `model.py` + `state.py` + `runner.py`, exposing `predict_game(home, away, date, **sport_kwargs)`. The market app's MLB strategy adapter generalizes to a sport-routing adapter that picks the right engine per ticker prefix. Build baseball well; subsequent sports become template work.

  **Components** (inside the sidecar):

  **1. Team strength model** — Bradley-Terry-style ratings updated nightly. Per-team rolling stats (runs scored, runs allowed, Pythagorean win expectancy, run differential) over multiple windows (last 10 / last 30 / season-to-date). Bayesian prior on opening-day expectation regresses cold teams less, hot teams more.

  **2. Matchup model** — given Team A (home) vs Team B (away), compute P(A wins). Inputs: team strength ratings, home-field advantage (~54% for MLB), starting pitcher quality (huge factor — a high-end SP can shift P(win) by 10pp), bullpen depth, recent head-to-head, lineup composition (key bats in/out).

  **3. News-driven adjustments** — late-breaking news between the nightly stats update and game time. Starting pitcher scratched: re-weight to bullpen baseline. Key player on injury list. Lineup announcement (manager rests stars). Hooks into the news-feed poller when that ships.

  **4. Persistent state** — `data/shared/baseball_state.sqlite` (or per-table JSON if simpler). Keyed by date + team. Schema: rolling stats, pitcher quality scores, injury list, lineup. Updated nightly by `baseball_runner.py` cron. Retention: full season (~180 days).

  **5. Data sources** — MLB Stats API at `statsapi.mlb.com` (free, no auth, comprehensive). Game logs, box scores, probable pitchers, injury list. ESPN's `pickcenter` (already wired into `pm/feeds/sports_data.py`) for vig-free consensus moneylines as a sanity-check anchor. Optionally Baseball-Reference / Fangraphs for advanced stats (scrape carefully).

  **6. Integration with Kalshi markets** — for each open MLB market on Kalshi, compute model probability, surface as an edge via a new `find_baseball_edges()` in `edges.py` and a registered strategy `baseball_winprob` in `bt.STRATEGIES`. Same gate as everything else: Brier vs market baseline before any live execution.

  **7. Module layout** (sidecar sub-package):
  - `baseball/__init__.py` — exposes the public surface (`predict_game`, `team_rating`, `refresh_all`)
  - `baseball/data.py` — MLB Stats API client + caching (mirrors `pm/feeds/sports_data.py` pattern)
  - `baseball/model.py` — pure functions: rate teams, score matchups, apply news adjustments
  - `baseball/state.py` — read/write the SQLite state store
  - `baseball/runner.py` — cron-able nightly updater (refresh stats, recompute ratings)
  - `pm/ui/pages/baseball.py` (optional) — UI lives in the market-app repo for now, queries the sidecar: per-team ratings table, today's games with model vs market, hit-rate over time
  - `tests/test_baseball_*.py` — unit tests for each layer
  - **Import discipline**: `baseball/` does NOT import `kalshi_client`, `provider`, etc. The market app imports `baseball.predict_game` in exactly one place (the strategy adapter that converts MLB predictions into Kalshi edges).

  **8. Validation** — Brier on resolved MLB markets vs the market-baseline Brier. Walk-forward backtest using historical MLB data (the model trained on data up to date T predicts game on T+1). Strategy doc entry under `docs/strategies.md` linking back here.

  Estimate: 1-2 weeks of careful work. Highest-leverage piece if MLB is the largest active market category on Kalshi during the season (most Kalshi sports volume IS MLB). Strategy-doc entry stub it under the "ideas — needs backtest validation" tier once we have a prototype.

  **Status update (2026-05-21)**: v0.1 shipped. Statistical model only (regressed Pythagorean + log-odds home-field + SP-ERA delta). FastAPI multi-user UI on port 8770. `baseball.runner` cron at 04:15 refreshes 7-day schedule + per-team season stats + pitchers + predictions. `baseball.live` daemon polls MLB `feed/live` every 60s for in-progress games → `live_plays` table. `find_baseball_edges` + `strategy_baseball_winprob` registered in `edges.py`; `agentic_baseball` strategy_runner cron at :10,:40 paper-trades flagged edges. Wired into main Streamlit app in 6 inline places (sidebar health badge, Markets model column, Kelly Sizer prefill, Price History overlay, Deep Analysis prompt anchor, Portfolio model-opinion column) via `baseball.predict_for_ticker()`. Components 3 (news adjustments) and v0.2 model improvements (lineups, park factors, FIP, recent form, AI augmentation) remain open — the gating question is whether v0.1's Brier beats `sports_winprob` (ESPN consensus) head-to-head, which requires accumulated resolved-paper-trade data.

  **Status update (2026-05-22 evening)**: model is now at **v0.4.0**, four layers deep.
  - **v0.2** added park factors (`baseball/park_factors.py`, Bayesian-regressed) AND bullpen ERA delta. Both shipped + tested.
  - **v0.3** added recent-form blend (last 15 days, ≤30% weight, ramps with games-in-window).
  - **v0.4** added posted-lineup OPS adjustment (`LINEUP_LOGODDS_PER_OPS=3.0`, min 50 AB per included batter, requires ≥5 reliable batters in lineup, in-day refresh cron at /30min).
  - Sidecar resolver (`baseball.resolve_pending_tickers`) closes the 4-day Kalshi settlement window; live daemon also syncs game Finals back to games table for ~15-min total resolution latency.
  - MLB-specific book poller (`pm/capture/mlb_book_poller.py` + cron-free daemon) captures Kalshi books at 60s, paired with live's 60s play polling. **Lead-lag dataset is now complete in both halves** — see separate TODO entry above.
  - Lead-lag analysis scaffold (`baseball/analysis.py` + `/analysis` page) joins the two streams with discipline (returns `None` on <5 paired samples to prevent false confidence).
  - Stats page surfaces all of this: model rankings, park factors, recent hot/cold, current edges, league leaders.
  - LLM chat surface (`/chat`) feeds the full state snapshot to a local Ollama model.
  - **Open**: weather sidecar (designed but not built — would be first sidecar-to-sidecar dependency), AI-augmented projection (only after Brier baseline), news sidecar (lineup announcements + starter scratches), Brier-vs-baseline gate finally measurable as forecasts resolve.

- [ ] **News-driven alerts + targeted auto-trigger — sidecar architecture for the feed half.** Full design captured from a real-user conversation. The *poller* is naturally a sidecar (own data sources, own state, own update cadence, reusable by other consumers); the *watchlist + trigger rules + alert mechanism* live in the market app because they're tightly tied to Kalshi positions. Clean split:
  - `news/` sub-package (sidecar): RSS sources + polling + SQLite store + relevance classifier + a small public surface like `news.recent_since(timestamp, tags=)`, `news.relevant_to_ticker(ticker)`. Domain-isolated — doesn't know what a Kalshi ticker is beyond "the consumer asks about one."
  - Market app: watchlist persistence, trigger-rule evaluation, alert mechanism, order placement. Subscribes to the news sidecar.

  This way the same news sidecar could feed a stock-research app's headline panel later. Five concepts (the feed is sidecar #1; the rest are market-app-side):
  1. **News feed poller** (in `news/` sidecar) — pluggable source registry, NOT a hardcoded list. Each source is its own small module that registers via `news.register_source(...)` (mirrors `bt.register_strategy` + `user_strategies/` pattern). The poller iterates the registry each cycle; adding a new source is "drop a file in `news/sources/`, import it once, done."

     **Source contract** — every source implements:
     ```python
     @dataclass
     class NewsSource:
         key: str                 # "fed_register", "wh_briefing", "google_news_eo", "nyt_politics", "espn_mlb", ...
         display_name: str
         tags: list[str]          # ["politics", "official"], ["sports", "mlb"], etc. — drives relevance routing
         fetch_since(since: datetime) -> list[NewsItem]
         is_available() -> bool   # cheap health check (no full fetch)
     ```

     **Source families we anticipate** (each becomes one or more sources):
     - **Government / regulatory**: Federal Register (EOs, rules), Congress.gov (bills/votes), SEC EDGAR (filings), Fed press releases, BLS releases
     - **News aggregators**: Google News RSS (per-topic queries), NewsAPI free tier, Bing News (paid)
     - **Specific outlets**: Reuters topic feeds, AP, NYT RSS, Bloomberg headlines, WSJ
     - **Sports**: ESPN news feeds, MLB.com cuts, NBA.com news (consumed by per-sport sidecars too)
     - **Polling / elections**: 538-successor aggregates, RealClearPolitics, state SOS filings feeds
     - **Crypto**: CoinTelegraph RSS, public exchange status pages, on-chain event subscriptions
     - **Weather / climate**: NWS / NOAA RSS for weather markets
     - **User-defined**: a "generic RSS" source that just takes a URL + tags so users can add ad-hoc feeds without writing code

     **Storage shape stays uniform across sources** — `NewsItem(source_key, title, summary, url, published_ts, fetched_ts, tags, raw_metadata: dict)`. The `raw_metadata` field is the escape hatch for source-specific extras (Federal Register's EO number, ESPN's event_id, NYT's section, etc.) so the schema doesn't fragment.

     **Per-source rate-limit + retry**: registry includes `min_seconds_between_calls` per source so the poller doesn't hammer Google News every cycle while still polling Federal Register every minute. Backoff on 429/5xx with the standard pattern.

     SQLite store at `data/shared/news_feed.sqlite` with timestamp + dedup (on `(source_key, url)`). Same patterns as `book_archive`.
  2. **Watchlist** (third bucket alongside paper portfolio + real positions). Per-profile. Schema: `{ticker, added_at, tags: [str], notes: str, trigger_rule: {...} | None}`. Stored as `data/<profile>/watchlist.json` for now; SQLite later if it grows.
  3. **Trigger rules** on watchlist entries — pre-configured order parameters that fire automatically when news + re-analysis cross a threshold. Example shape: `{side: "yes"|"no", max_stake_dollars: 20, price_condition: "yes_ask <= 0.35", prob_condition: ">= 0.70", expires_at: <iso>}`. User pre-approves; system enforces. Critical: this is a NARROW automation specifically scoped to the user's pre-approved (ticker, rule) pairs — NOT general "let the LLM trade."
  4. **News-driven re-analysis loop**. When a poller cycle finds new headlines, run relevance match against held positions + watchlist. For matches: queue a Deep Analysis run with `include_web_context=True`. Compare new probability to the most recent prior. If meaningful delta (>10pp by default):
     - **Held position**: alert only (user decides).
     - **Watched with satisfied trigger rule**: place the configured order via `place_limit_order`, audit-log + alert.
     - **Watched without trigger rule, or rule conditions not met**: alert only.
  5. **Alert mechanism**. Streamlit toasts when the app is open are insufficient for "wake me when something happens." Need an out-of-app push path — pick one: desktop `notify-send` (Linux), email via SMTP, or ntfy.sh / Pushover. Configurable per-user.

  **Relevance matching**: default to user-curated tags on the watchlist entry (cheapest, most accurate); fall back to LLM-classify (`include_web_context=True` mini-prompt: "does this headline materially change market X?") when no tags. Keyword-overlap is too noisy.

  **Storage**: news_feed retention should be capped — last 30 days by default, settings-page knob to extend. The SQLite store needs a `purge_older_than(days)` helper like book_archive has. Estimated growth: ~100 headlines/day × 1KB each × 30 days = ~3 MB. Trivial.

- [x] **🚨 Backup strategy for `data/`** — `scripts/backup.sh` rsyncs to `/mnt/backups/kalshi-trader/<utc-timestamp>/` nightly at 00:30 with `--link-dest` hardlinking. 90-day retention, ~194MB total disk for two snapshots (vs 193MB for one) confirming dedup works. Excludes `shared/*.log` (rotated separately). Logs to `data/shared/backup.log`.
  - [ ] **Still wanted: off-site / off-machine copy of the critical subset.** A house fire / theft / OS drive failure still kills both the SSD and the spinning disk together. Push `forecasts.json` across profiles + `kalshi_orders.log` to S3-compatible / Backblaze / restic-to-remote / encrypted git-annex. Tiny payload, big risk reduction.
  - [ ] **Restoration drill.** Pick a snapshot, rsync it to a scratch dir, point `POLYMARKET_*` env vars at it, confirm pages render with the historical state. Catches "we backed up the wrong thing" before it matters.

- [x] **Cron lockfiles to prevent pile-up** — all 13 cron entries (book_poller, 3 × strategy_runner, auto_resolve, 6 × bulk_deep_analysis, backup, baseball.runner) now wrapped with `flock -n /tmp/polymarket_<job>.lock -c '...'`. Logrotate excluded (idempotent + self-locking). Eliminates double-writes when a cycle runs long.

- [ ] **Emergency stop switch.** `KALSHI_LIVE_ENABLED=false` stops new live orders but doesn't cancel resting orders, pause cron jobs, or disarm trigger rules (when those land). Single file flag `data/.emergency_stop` checked at the top of every runner + Streamlit page + order-placement path. If present: halt new activity, optionally cancel resting orders, show banner. One file to create / delete to control the whole system in a panic. Critical for when Phase 5 lands.

- [ ] **P&L by source, not just Brier.** A source can have great Brier (forecasts close to actual outcomes) but lose money because the edges are smaller than spread + fees. Phase 5 promotion needs *both*. Add a `pnl_by_source(df_forecasts, df_fills)` helper that joins forecasts to actual fills by ticker + timestamp; surfaces "qwen3:32b: Brier 0.18, realized PnL −$— across 12 trades" — the calibration-alone view would say "promote" when reality says "no." Should be visible on the Recommendations page Track Record column next to Brier.

- [ ] **Per-source Brier by category.** `brier_score_by_category(df)` exists in `pm/ledger/calibration.py` but isn't surfaced on the Recommendations page or anywhere user-visible. The interesting Phase-5 question is "is qwen3:32b good *across categories* or just in Politics?" — easy to compute, currently invisible. Add a sub-table to the Recommendations Track Record section.

- [ ] **Prompt version tagging on forecasts.** When the system prompt for deep analysis changes (we've already iterated on it), forecasts from before and after the change are pooled in `brier_by_source` as if they're the same source. Tag every forecast with a `prompt_version` string (hash of the active system prompt at fire time); group Brier by `(source, prompt_version)`. Without this we can't honestly compare prompt experiments.

- [ ] **Stop-loss orders** (the symmetric counterpart to take-profit, with the Warren Seat doubling-down loss as the motivating case). Kalshi doesn't natively support stop orders; need a polling watcher that queries position prices on a schedule and submits a market sell when the threshold is crossed. Complexity: latency (5-min poll = 5-min slippage window), false triggers on thin books, and the watcher itself becomes critical infrastructure. Worth doing eventually — gates a real risk-management gap — but not until calibration data tells us automation is sound.

- [x] **Sidecar protocol formal spec.** `pm/core/sidecars.py` ships `Sidecar` typing.Protocol + `SidecarHealth` / `SidecarActivityEvent` dataclasses + module-style registry (`register_sidecar` / `list_sidecars` / `get_sidecar`) + `safe_health` wrapper. `docs/sidecar-protocol.md` is the canonical spec — import discipline, file layout, storage conventions, cron conventions, what the control plane shows, what the protocol intentionally doesn't specify, open questions to revisit when sidecar #2 (news) lands. Baseball sidecar registered as the first instance; tests cover registration, missing-field rejection, exception-swallowing, and re-register-overwrite.

- [ ] **🛰️ Sidecar control-plane UI.** Single page (or its own small Streamlit app) that surfaces every sidecar engine in one view, so we don't have to remember which CLI to invoke or which log file to tail. Anticipating that the sidecar family will grow (news, baseball, eventually NBA / NFL / politics / macro), this becomes the "is everything ticking?" dashboard.

  Each sidecar registers itself by exposing a small introspection surface that the control-plane reads — exact API TBD when the second sidecar exists and we can see the actual shared shape. Initial sketch of what each sidecar exposes:

  ```python
  # baseball/__init__.py (and similar for news/, eventually nba/, etc.)
  def health() -> dict:
      """Returns {status, last_update_ts, next_update_ts, errors_24h, ...}"""
  def stats() -> dict:
      """Returns {rows, file_bytes, source-specific summary, ...}"""
  def recent_activity(limit=20) -> list[dict]:
      """Returns recent events: what got fetched / computed / failed."""
  ```

  **What the control-plane page shows** (per sidecar):
  - **Status**: green ✅ / yellow ⚠️ / red 🛑 based on `last_update_ts` vs expected cadence + error count
  - **Stats**: row counts, file size, sport-/source-specific summaries (e.g. baseball: # teams rated, last game ingested; news: feeds polled, headlines stored)
  - **Recent activity**: tail of what happened (last 20 fetches / computations / failures)
  - **Quick actions**: "refresh now," "view full log," "clear cache" (where safe)

  **Tie-them-together features** (the interesting part — not just monitoring, but cross-sidecar queries):
  - "What does each sidecar say about today's Kalshi market X?" — calls baseball.predict_game(...) AND news.relevant_to_ticker(...) AND existing edges sources, displays all opinions side-by-side
  - "Show me markets where ≥2 sidecars disagree with the venue price" — discovers candidates
  - "Trace": for a recent Kalshi order, show every sidecar's view on that market at order time

  **Chat interface** (eventual, lower priority): a local-LLM-backed chat that can answer "what's happening with the Yankees right now?" by querying the baseball sidecar + the news sidecar + the LLM cache. Routes natural-language questions to the right sidecar(s) and synthesizes responses. Probably easiest to build as a wrapper around the existing Ollama plumbing — a system prompt that knows what tools/sidecars are available and emits tool-call JSON the page dispatches.

  **Architecture notes**:
  - The control plane is *market-app-side*, not its own sidecar (it's a UI that *aggregates* sidecars; making it a sidecar itself would create a chicken/egg).
  - Define a small `Sidecar` protocol (Python typing.Protocol) that every engine implements: `name`, `display_name`, `health() / stats() / recent_activity()`. Control plane discovers and renders generically.
  - "Refresh now" triggers the sidecar's own runner — the control plane should NOT contain the refresh logic, just shell out to the existing cron-able entry point.
  - Don't build this until at least the second sidecar exists. With one sidecar there's nothing to "tie together." With two (baseball + news), the patterns become real.

  **Estimate**: ~2 days for the basic dashboard once the Sidecar protocol is settled; ~3-5 more days for the chat interface depending on how robust the tool-routing needs to be.

- [ ] **RAG (retrieval-augmented generation) over project docs + historical data.** Currently the baseball LLM chat (`/chat` page) does prompt-stuffing: every turn injects ~7.2k chars of system prompt (team rankings, edges, methodology, Kalshi quirks). Works for now but doesn't scale as docs grow and we want the LLM to answer questions against historical data ("how did the model do on AL East games in May?") that can't fit in a prompt.

  **Architecture**: own sidecar (`rag/`) so it's usable by baseball chat AND the main app's Deep Analysis AND any future chat surface. Embed-once, query-everywhere. Per the protocol: `rag/__init__.py` exposes `retrieve(query, k=5) -> list[Chunk]`; `rag/index.py` does ingestion; `rag/state.py` owns the vector store.

  **Storage**: ChromaDB or LanceDB — both run local, file-based, no server. Alternative: SQLite with FTS5 (keyword-only, simpler, often adequate for doc retrieval). Vector DB matters more once we're indexing news / unstructured text.

  **Embeddings**: Ollama serves embedding models on the local GPU — `nomic-embed-text` (137M params, fast) or `mxbai-embed-large` (335M, higher quality). Free; same hardware we already use for chat models.

  **Sources to index** (priority order):
  1. **Project docs** — every `.md` file in `docs/` + per-package READMEs. Chunked by section. Methodology / API-quirk questions answer themselves with citations.
  2. **Glossary entries** — each term is a discrete chunk. Perfect for retrieval ("what's regressed Pythagorean?").
  3. **Strategy doc entries** — each strategy is one chunk with its design + status.
  4. **TODO entries** — same shape.
  5. **Forecast log + resolution outcomes** — per-forecast row indexed with model_version, source, category, outcome. Enables "how did v0.3 do on Politics?" queries.
  6. **Activity log** — runner / strategy_runner / auto_resolve events. Enables debug-style queries ("why didn't the May 22 ATH @ LAA forecast resolve?").
  7. **Game writeups** (future) — when news sidecar lands, auto-summarize each game's plays into a paragraph and index it.

  **Integration in baseball chat**: replace the bulky `_KNOWLEDGE_BLOCK` in `llm_chat.py` with a per-turn retrieval. `chat()` runs `rag.retrieve(user_message, k=3)` first, injects only the relevant chunks. Saves prompt tokens + lets the chat answer questions about content we couldn't fit verbatim.

  **Reindexing trigger**: `rag/runner.py` cron at low frequency (daily?) walks the indexed paths, compares mtimes, re-embeds changed files. New `data/shared/*.log` rotation events trigger an incremental embed of new activity lines.

  **Caveats**: vector retrieval is fuzzy. For exact-match queries ("what's the BULLPEN_LOGODDS_PER_RUN constant?") fall back to grep / read directly — don't trust embeddings for code references. Hybrid: keyword + vector with the higher-confidence path winning.

  **Estimate**: ~2-3 days for v1 (docs only, no historical data). Another 1-2 days to add forecast/activity log indexing. Could be deferred forever; current prompt-stuffing works fine until the next chat surface (news? deep-analysis?) needs the same content.

- [ ] **Storage location abstraction (`POLYMARKET_DATA_ROOT`).** Today every storage module has its own env-var path override — `BOOK_ARCHIVE_PATH`, `LLM_CACHE_PATH`, `BACKTEST_HISTORY_DIR`, `POLYMARKET_PORTFOLIO_FILE`, etc. Moving "all the data" to a new disk means changing 7+ env vars. Centralize to a single `POLYMARKET_DATA_ROOT` env var (default `./data`) with each module reading its subdir off that root via a small `storage_paths.py` helper. Per-module overrides still work (back-compat) but become optional. One env-var change + `mv data /new/location` = full relocation. Stops short of pluggable backends (S3, network DB) — that's a separate, much bigger project.

---

## 🪴 Documentation maintenance

Living docs are part of every change. See [Developer Guide § Documentation Maintenance](docs/developer-guide.md#documentation-maintenance) for the explicit table of what to update when.

- [x] Comprehensive doc refresh after the transactions + calibration features landed: `user-guide.md` rewritten for the new Portfolio (Buy/Sell/History tabs, sector exposure, time-to-resolution) and new Calibration page; `developer-guide.md` updated with all three modules' current public APIs and DataFrame schemas; `docs/file-map.md` updated with new function signatures, new DataFrame columns, and the `condition_ids` snake_case gotcha; `README.md` feature table and project layout refreshed; `api-reference.md` documents the silent filter failure quirk; "Documentation Maintenance" section added to the developer guide.
- [ ] **Doc sweep after exit-management + oscillation work.** docs/file-map.md is current, but `docs/user-guide.md`, `docs/developer-guide.md`, `docs/strategies.md`, and `README.md` are stale on: (a) take-profit/stop-loss/trailing-stop/time-stop in the agentic runner; (b) `position_meta.json` and `exits.jsonl` per-profile artifacts; (c) `exits_analysis` module API; (d) `agentic_*_managed` sibling profiles + the A/B comparison workflow; (e) `oscillation_scanner` + `strategy_oscillation_revert` + `--snapshot-history-days` runner flag.

---

## ✅ Done

- **Two silent data losses fixed** (2026-10-13). (1) `candle_panel` volume/OI
  were 0 in all 19.4M rows: Kalshi renamed them `volume_fp`/`open_interest_fp`.
  Fixed forward; pre-fix rows keep 0 (backfill not done). (2) The monologue
  recorder had captured nothing since 08-16 (tuner moved IP on DHCP; recorder
  exited 0; no OnFailure hook). Lazy `device_ip()`, nonzero exit, alert hook.
  Still owed: DHCP reservation for the HDHomeRun.
- **Backup no longer silently drops over-cap databases** (2026-10-13). The
  2048 MB cap had excluded the live candle panel (2.6 GB) since ~10-01 while
  every run reported `ok` and the snapshot kept its stale Oct-1 staged copy.
  Cap → 8192 MB; skipped names now land in `backup_status.json` and the health
  digest pages on any skip not in `BACKUP_SKIP_EXPECTED` (default: the frozen
  `book_archive.sqlite`). Also `disable --now`'d `advisory-capture`, which a
  reboot had revived because it was stopped but never disabled.
- **Retired `fade_nearclose` + `back_nearclose` to `attic/`; live posture
  zeroed** (2026-08-24). The candle_panel retrospective closed the near-close
  thesis (edge log §2026-08-24): founding −6pp mispricing now +0.4pp on
  14,400 markets; no month ever CI-positive; three instruments agree.
  Services stopped/disabled, units archived, stores + OOS history retained;
  `LIVE_TRADING_PROFILES` emptied and both canary flags removed from `.env`
  (`live_gates`: allow-list empty, all paper, zero notes). Fleet 9 → 7
  services. Five whole test files → `attic/tests/`; five in-file fragments
  archived to `attic/tests/test_nearclose_fragments.py`. **R-27's
  concurrency suite caught a real race during this work**: six processes
  racing `PRAGMA journal_mode=WAL` on a fresh store hit `database is locked`
  (the DELETE→WAL conversion takes a lock the sqlite3 busy handler does NOT
  cover; instrumented 20/20 at the pragma, 0/30 in ensure_schema). Fix:
  `sqlite_schema.set_wal()` with bounded retry, wired into all 9 store
  `_con()`s; probe re-run 0/30, test suite 8/8 stable.

- **Retired `fade_retail_longshot` + `fade_props` to `attic/`** (2026-08-23).
  Both failed out-of-sample and were kept running past the 2026-07-30 verdict
  to grow the sample; the extra sample confirmed the verdict — retail fade
  **−$—** over 200 resolutions (mean −$—/position), prop fade
  **−$—** over 224 (mean −$0.75), against a backtest that had promised
  +11.8% ROI / 86% win by independent event. Services stopped + disabled, unit
  files removed from `~/.config/systemd/user` and `deploy/systemd/` (archived
  to `attic/deploy/systemd/`), modules + tests moved to `attic/` and
  `attic/tests/test_fade_retail_props.py`. **SQLite stores retained** as
  evidence. Fleet 11 services → 9. Live code updated: `health_digest`
  EXPECTED_UNITS, `live_gates.ARM_FLAGS`, `control_plane/collect.py` (and
  `back_nearclose` gained the dashboard panel it had always been missing —
  without it the board would have shown one of the two surviving arms),
  `pyproject.toml` coverage gates; `tests/test_provenance.py`'s
  "arm rows carry commit_sha" test repointed to `back_nearclose` rather than
  dropped. Post-mortem in `docs/failed-experiments.md`; `strategy-scoreboard.md`
  §5 rewritten (§5b added for the surviving near-close pair) and the compounding
  layer retargeted. The **Gate held** — no real capital was ever sized onto
  this. See `docs/failed-experiments.md` §"Favourite-longshot fades at a
  multi-day lead".

- **Live-trading bug fix + data crunch + favorite-hold arm** (`5471ab8` on main): (1) Fixed `pre_position._current_game_states()` — it filtered `status='Scheduled'` but the MLB feed leaves status stuck at 'Pre-Game'/'Warmup', so the scalpers saw 0 live games and never traded; now keys off recent `live_plays`. This unblocked the whole scalper line. (2) After two nights of paper data, ran a counterfactual crunch (`scratch_*.py`) over `scalper_observer.jsonl` price paths + known outcomes: **every scalp/longshot/close-game variant loses**; the only positive-EV thread is **buying moderate favorites (forecast 0.55–0.70) and holding to resolution** (+$— 73% win in backtest — the market underprices favorites). (3) Longshot daemon RETIRED (−$— paper); close-game arms kept but weak; new `favorite` mode + `favorite-hold.service` deployed. See `docs/strategies.md §15-17`.
- Longshot scalper + flip + free-roll (`longshot_scalper.py`, profile `agentic_longshot_scalp`) — ❌ RETIRED (−$— paper; cheap-underdog entry net-negative). Buys underdog $0.10–$0.35, sells +25% net pop, flips to opponent on −15% net, `--freeroll-profit` recycles TP profit into the opposite longshot. See `docs/strategies.md §15`.
- Close-game scalp mode (`longshot_scalper.py --mode close_game`, 🧪 paper, weak) — buy the underpriced side of a coin-flip game (forecast ∈ [0.40,0.60], ask ≥5pp below forecast, **ask ≥ 0.25 price floor**) early and ride the pop. Two arms live (deep + consensus); both net-negative as games resolved. Pluggable `--forecast-source`. See `docs/strategies.md §16`.
- Favorite-hold mode (`longshot_scalper.py --mode favorite`, 🧪 paper, **current best hypothesis**) — buy the moderate-favorite side (forecast 0.55–0.70) early, HOLD to resolution, no scalp/flip. `favorite-hold.service` → `agentic_favorite_hold`. Hypothesis-grade (backtest bands n=14–16); deployed to gather a clean forward sample. See `docs/strategies.md §17`.
- MLB gemma3 deep primer (`mlb_deep_primer.py`) — pre-game: discover today's `KXMLBGAME` games → Pythagorean pre-filter to coin-flip-ish → gemma3:27b deep → log to `agentic_deep`. Feeds close-game scalp's `--forecast-source deep`. See `docs/strategies.md §16`. (uncommitted)

- Primer deployed: cron `mlb_deep_primer.py` at 15:00 + 22:00 UTC; `closegame-scalper.service` flipped to `--forecast-source deep`. First run logged 18 gemma3 forecasts (9 coin-flip games) to `agentic_deep`; scalper reads them. (uncommitted)

### Upcoming (next session)
- **Post-hardening state (2026-08-17):** P0, P1 **and P2** are closed and pushed; CI is green; real
  money is off. The next session is **P3**, listed in the hardening section at
  the top of this file — R-25 is done (`8b12304`); start with R-24 (packages),
  which R-17's attic made materially easier. None of it is blocking.
- Also still open from the research side: continue accumulating clean forward
  results under frozen rules (`python -m oos_ledger --status`), and decide on
  that evidence whether to re-arm real money and at what size. Scaling is gated
  on out-of-sample evidence, not on the cumulative number.
