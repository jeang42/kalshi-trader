# Release audit — 05: Test results

## Baseline (main @ `0f46fbd`)

`pytest --no-cov -q`: **2104 passed, 2 skipped, 0 failed** (~28 s).

## Final (this branch)

`pytest --no-cov -q`: **2133 passed, 2 skipped, 0 failed** (~27 s).
`ruff check .`: clean. `mypy` on the gated module list (incl.
`kalshi_orders`, `kalshi_auth`, `execution`, `risk`): clean.

The 2 skips are pre-existing and unrelated (environment-conditional).

## New tests (+29)

`tests/test_order_policy.py` (new file, 17 tests):

- **P0-A** — mandatory caps with pre-flight deliberately skipped: buy over
  the default stake cap refused before HTTP with no reservation burned;
  env raises the cap; sells exempt; bad env falls back; buy/sell refused at
  the daily cap; take-profit/cash-out not daily-capped; env raises the
  daily cap.
- **P0-D** — 10 threads with a barrier start race a cap of 5: exactly 5
  accepted, 5 refused, exactly 5 submit rows in the audit log.
- **P1-B** — unwritable audit log refuses buy / take-profit / cash-out
  before any HTTP call; cancel still proceeds.
- **P0-C** — venue-silent timeout raises `OrderOutcomeUnknown` carrying the
  client_order_id for all three submit paths; audit rows record
  `outcome=unknown`; an `HTTPError` stays a confirmed rejection
  (`outcome=rejected`).
- **P0-C/P1-D at LiveBroker level** — unknown outcome with failed
  reconciliation reported as UNKNOWN (not a confirmed failure), confirmed
  failure reports `submitted_count=0`, failed remainder-cancel surfaces
  "may still be resting", clamped buy reports requested=100 /
  submitted=50 / filled=50.

`tests/test_web_auth.py` (+6):

- `default_bind_host`: loopback with no token, `0.0.0.0` with a token,
  explicit `WEB_BIND_HOST` override, whitespace token = no token.
- Control-plane `/health.json` reachable without a token but carrying no
  account/fleet data; dashboard still 401s without the token.

`tests/test_kalshi_orders.py` (+2): bankroll limit labeled advisory and
never blocks; threshold configurable.

`tests/test_kalshi_auth.py` (1 updated): the owner-profile check now
**fails closed** when the profile lookup raises (test previously asserted
the fail-open behavior).

## How the required scenarios are covered

| Scenario (from the brief) | Test |
|---|---|
| Unauthorized direct order submission | `TestMandatoryStakeCap` / `TestMandatoryDailyCap` (bypass `pre_flight_check` entirely) |
| Missing authentication / public bind without credentials | `TestDefaultBindHost`, `TestControlPlaneHealthLeak`, pre-existing `TestGateClosed`/`TestDisabledByDefault` |
| Exchange submission timeout / unknown acceptance | `TestUnknownOutcome`, `TestLiveBrokerReporting`, pre-existing orphan-adoption tests in `test_execution.py` |
| Cancellation timeout or failure | `test_failed_remainder_cancel_is_surfaced_not_swallowed` |
| Reconciliation failure | `test_unknown_outcome_with_no_reconciliation_is_reported_unknown` |
| Duplicate order attempt | pre-existing resting-order idempotency tests (`test_execution.py`) + venue coid de-dupe (documented; writes never retry per `test_kalshi_auth.py`) |
| Concurrent submissions near a limit | `TestConcurrentSubmission` |
| Process restart with unresolved orders | pre-existing: venue-truth position reads, persistent drawdown high-water mark (`test_execution_drawdown.py`), shutdown-mask tests; residual documented in `03-order-safety.md` |
| Audit persistence failure | `TestAuditFailClosed` |
| Exposure-cap boundary values | stake-cap boundary tests + pre-existing `TestPreFlight` |
| Requested vs submitted mismatch | `test_clamped_buy_reports_requested_vs_submitted` |

## Isolation guarantees (unchanged, verified)

Every test runs with `socket.connect` disabled, all data paths redirected to
`tmp_path`, and a session tripwire that fails the run if the real `data/`
tree is touched. **No live order, cancellation, or authenticated write was
performed at any point in this audit.** No production service existed or was
touched (fleet stopped; verified via `ps`/`systemctl --user`).

## Remaining test limitations

- The live Kalshi API is never exercised; response-shape assumptions are
  mock-encoded. `pm/trading/live_preflight.py` exists for a future operator
  to re-verify read shapes before any order (documented as required in the
  README maintenance section).
- The cross-process flock is exercised with threads (separate fds, same
  semantics) rather than separate OS processes.
- Streamlit pages are tested at module level, not through a browser.
