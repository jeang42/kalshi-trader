# Release audit — 01: Baseline

Date: 2026-11-04. Auditor: automated pre-release hardening pass (Claude Code),
verifying the external pre-release review findings against the tree at commit
`0f46fbd` (branch `main`, clean worktree).

## Repository state

- Branch `main`, 11 commits, all authored via a GitHub noreply address.
  History starts at "Initial public release" — it was already squashed for
  publication. No tags. Remote: `github.com/jeang42/kalshi-trader`.
- Working tree clean. No `data/` directory exists in this checkout.
- No kalshi-trader services are running on this machine (checked `ps` and
  `systemctl --user`): the fleet is stopped. This audit is for a **final
  open-source release**, not a redeployment.

## Toolchain

- Python 3.13.5; dependencies via `requirements.txt` (+ `requirements.lock.txt`),
  `uv` available. Packaging metadata in `pyproject.toml` (no build-system —
  the project is an app, not an installable library).
- Tests: pytest, `testpaths=["."]` + `--doctest-modules`. `make test-fast`
  = `pytest --no-cov -q`.
- Lint: ruff; typecheck: mypy on an explicit module list (see `Makefile`).

## Test baseline

`pytest --no-cov -q` at `0f46fbd`: **2104 passed, 2 skipped, 0 failed** (~28 s).

The suite is already strongly isolated (root `conftest.py`):

- every `socket.connect` raises — no test can touch the network;
- `POLYMARKET_DATA_DIR`, `ORDERS_LOG_PATH` and ~25 store paths are
  redirected to `tmp_path` per test;
- a session-scoped tripwire fails the run if anything writes into a real
  `data/` tree.

All verification in this audit used mocks at the `kalshi_auth` boundary.
**No live order, cancellation, or authenticated write was performed.**

## Architecture relevant to the findings

Layers (bottom-up) on the real-money path:

1. `pm/venue/kalshi_auth.py` — signed HTTP. Writes go through
   `_authed_request` (default `retries=1`, i.e. **no retry on writes**) under
   a shared circuit breaker. Reads retry 3×.
2. `pm/venue/kalshi_orders.py` — the only module that POSTs/DELETEs orders:
   `place_limit_order`, `cash_out_position`, `place_take_profit`,
   `cancel_order`. Audit-first JSONL log (`ORDERS_LOG_PATH`, default
   `data/shared/audit/orders.log`). `pre_flight_check()` is a **pure advisory
   function** — callers must act on it.
3. `pm/trading/execution.py` — `PaperBroker` (default) / `LiveBroker`.
   `get_broker(profile)` returns live only when the profile is in
   `LIVE_TRADING_PROFILES` AND `is_trading_enabled()` AND the emergency stop
   is inactive. `LiveBroker._place` runs pre-flight (incl. account-level
   drawdown breaker, fail-closed), clamps stake, generates the
   `client_order_id` itself, and reconciles orphans after submission errors.
4. Gates: `KALSHI_LIVE_ENABLED` env, `kalshi_auth.is_configured()` (key +
   PEM + authorized active profile), `LIVE_TRADING_PROFILES` allow-list,
   file-based emergency stop (`data/.emergency_stop`).

## Live-order entry points (callers of the four write functions)

Excluding `tests/` and `attic/` (retired code, not collected):

| # | Caller | Function |
|---|--------|----------|
| 1 | `pm/trading/execution.py` `LiveBroker._place_unguarded` | `place_limit_order` |
| 2 | `pm/trading/execution.py` cancel-remainder + `LiveBroker.cancel` | `cancel_order` |
| 3 | `pm/trading/execution.py` `place_resting_take_profit` (no live callers today) | `place_limit_order` |
| 4 | `pm/ui/pages/kelly.py` (manual buy) | `place_limit_order` |
| 5 | `pm/ui/pages/live_portfolio.py` (manual TP / cash-out) | `place_take_profit`, `cash_out_position` |
| 6 | `pm/ui/pages/orders.py` (manual cancel) | `cancel_order` |
| 7 | `scripts/smoke_test.py` (gated 1-contract live smoke) | `place_limit_order`, `cancel_order` |

The only live autonomous daemon is `pm/arms/reactive_scalper.py` (via
`get_broker`). No code bypasses `kalshi_orders` to write to Kalshi directly.

## Network-facing services

| Service | Default bind (at baseline) | Auth (at baseline) |
|---|---|---|
| Streamlit console (`app.py`, via `run.sh`) | Streamlit default = all interfaces | **none** (can place real orders when live-gated env is set) |
| Control plane (`control_plane/web.py`) | `0.0.0.0:8773` | `WEB_UI_TOKEN` gate; **fails open when unset**; `/health.json` returned the full snapshot incl. real balance even with a token set |
| Baseball / WNBA / Tennis sidecars (`*/web.py`) | `0.0.0.0:8770–8772` | `WEB_UI_TOKEN` gate; fails open when unset |

## Verification environment

- All tests on this machine, offline at the venue boundary (socket guard).
- No production service existed to disturb; no credentials were read,
  printed, or transmitted; `.env` was not modified.

See `02-findings.md` for the finding-by-finding verification results.
