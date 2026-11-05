# Release audit — 07: Change summary

Branch `release-hardening` off `main@0f46fbd`. Six commits, reviewable
independently. Nothing was pushed; no production config, credentials, or
services were touched. 34 files changed (+1007/−78), of which ~450 lines are
new tests and ~350 are these audit documents.

## By file

| File | Change | Why | Compatibility |
|---|---|---|---|
| `pm/venue/kalshi_orders.py` | Mandatory stake/daily caps inside `place_limit_order` (`mandatory_caps()`), atomic flock reservation for the daily cap, fail-closed pre-submission audit (`_audit(required=True)`), `OrderOutcomeUnknown` + error classification on all submit paths and cancel, advisory wording + configurable `max_bankroll_pct` in `pre_flight_check`, docstring updates | P0-A, P0-D, P1-B, P0-C, P1-A | **Behavioral:** callers that previously bypassed pre-flight can now get `OrderRejected` at the caps (raise `LIVE_MAX_STAKE_DOLLARS`/`LIVE_MAX_ORDERS_PER_DAY` explicitly to loosen); an unwritable audit log now refuses orders; venue-silent errors raise `OrderOutcomeUnknown` (a new `RuntimeError` subclass — generic `except Exception` callers unaffected). `pre_flight_check` gained an optional kwarg (source-compatible). |
| `pm/venue/kalshi_auth.py` | `is_owner_profile_active()` fails closed on lookup errors | P0-A | **Behavioral:** CLI contexts with a broken/absent profile system are now denied; set `POLYMARKET_ACTIVE_PROFILE` to an authorized profile instead. |
| `pm/trading/execution.py` | `OrderResult.submitted_count`; unknown-outcome detail wording; failed remainder-cancel surfaced on `detail` | P1-D, P0-C | Additive dataclass field (default 0); existing consumers unchanged. |
| `pm/core/web_auth.py` | `default_bind_host()`; docstring reflects loopback default | P0-B | Additive. |
| `control_plane/web.py` | `/health.json` → liveness only; bind via `default_bind_host()`; docstring | P0-B (incl. new balance-leak finding) | **Breaking for consumers of `/health.json`'s old full snapshot** — use `/` with the token. |
| `baseball/web.py`, `wnba/web.py`, `tennis/web.py` | Bind via `default_bind_host()`; docstrings | P0-B | **Breaking for open-LAN deployments:** set `WEB_UI_TOKEN` or `WEB_BIND_HOST=0.0.0.0`. |
| `run.sh`, `Makefile` | Streamlit binds `127.0.0.1` unless `BIND_HOST` set | P0-B | Same migration: `BIND_HOST=0.0.0.0 ./run.sh` restores LAN serving (not recommended — no auth). |
| `scripts/baseball_web.sh`, `scripts/wnba_web.sh` | Stale "no auth / binds 0.0.0.0" comments corrected | P0-B | Comment-only. |
| `pm/ui/pages/kelly.py` | `OrderOutcomeUnknown` handler with reconcile instructions; passes `max_market_position_pct` to pre-flight | P0-C, P1-A | UI-only. |
| `pm/ui/pages/live_portfolio.py` | Same unknown-outcome handlers for TP/cash-out | P0-C | UI-only. |
| `pm/ledger/risk.py` | `max_market_position_pct` docstring: advisory, and where the hard cap actually lives | P1-A | Doc-only. |
| `pm/core/emergency_stop.py` | Docstring named a nonexistent exception | S-2 | Doc-only. |
| `pyproject.toml` | Apache-2.0 license + classifier; alpha status; keep `Private :: Do Not Upload` (commented why) | P1-E | Metadata-only. |
| `QUICKSTART.md`, `CHANGELOG.md`, `README.md` | QUICKSTART no longer denies order capability; stale private-repo link fixed; README gains Maintenance-status section + bind-default note | P1-E, project-status brief | Doc-only. |
| `NOTICE` | research/*.json flagged as Kalshi content outside the Apache grant | P1-E | Doc-only; rights decision still open (06-release-checklist). |
| `.gitignore`, `.dockerignore` | keys/caches/build dirs; `.env*` + `.git` out of Docker contexts | P1-E | Hygiene. |
| `.env.example`, `docs/file-map.md` | `WEB_UI_TOKEN` / `WEB_BIND_HOST` documented (token was previously undocumented in the example) | P0-B | Doc-only. |
| `go_live.sh`, `docs/user-guide.md`, `docs/live-execution-adapter.md`, `docs/module-notes.md` | `live_preflight` invoked as `python -m pm.trading.live_preflight` (old root path no longer exists — the script always failed) | P1-E | Fixes broken tooling. |
| `tests/test_order_policy.py` (new), `tests/test_web_auth.py`, `tests/test_kalshi_orders.py`, `tests/test_kalshi_auth.py` | +29 regression tests, 1 inverted (fail-open → fail-closed) | all | Test-only. |
| `docs/release-audit/01…07` | This audit | deliverables | Doc-only. |

## Operational migration (for anyone redeploying a fork)

1. LAN access to the FastAPI UIs now requires `WEB_UI_TOKEN` in `.env`
   (or an explicit `WEB_BIND_HOST=0.0.0.0`).
2. The Streamlit console needs `BIND_HOST=0.0.0.0` for LAN serving — do not
   do this; it has no authentication and can trade.
3. Monitoring that parsed control-plane `/health.json` for fleet data must
   move to `/` with the token; `/health.json` is liveness-only.
4. If an arm legitimately needs orders larger than $50 or more than 10/day,
   set `LIVE_MAX_STAKE_DOLLARS` / `LIVE_MAX_ORDERS_PER_DAY` — the venue
   layer now enforces these as the outer floor for every caller.
5. Point `ORDERS_LOG_PATH` somewhere durable and writable: an unwritable
   audit log now blocks order submission (by design).
