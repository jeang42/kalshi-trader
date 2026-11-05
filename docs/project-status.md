# Project status

**As of 2026-08-17**, immediately after the P0+P1+P2
hardening passes that followed the 2026-08-08 engineering review. Regenerate
the numbers with the commands at the bottom.

## At a glance

| | |
|---|---|
| Python | **262 files, ~81,691 LOC** (51,014 app/runners + 24,459 tests + 6,218 in `attic/`) |
| Tests | **2,180 passing**, CI-gated, **80.5% coverage** against an 80% floor; root doctests collected; tests cannot reach the network or write `data/` |
| CI | **green**, four jobs (lint, typecheck, test, audit) — first successful run was 2026-08-09 after 60 consecutive failures back to 2026-06-02 |
| Docs | ~10,465 lines of Markdown |
| Commits | **439** total, single author, first 2026-05-16 |
| Data on disk | **2.5G** (was ~43 GB larger before the retired-thesis archive moved to bulk storage) |
| Live ops | **11 services running**, 0 failed; 29 unit files + the crontab version-controlled in `deploy/` (10 retired units moved to `attic/deploy/`) |
| Profiles | 27 data profiles |
| Real-money trading | **off** — deliberately, pending out-of-sample evidence |

## Health

| Area | State |
|---|---|
| Data collection | Running continuously; freshness checked daily by `pm/ops/health_digest.py` |
| Paper arms | 4 running; results split in-sample vs out-of-sample by `oos_ledger` |
| Real-money path | Armed by a single flag; currently off. Verify with `python -m live_gates` |
| Alerting | Failures reach a phone (ntfy) + a health log + the control plane |
| Backups | Consistent SQLite snapshots, integrity-verified after every run |
| Build | lint + typecheck + tests + dependency audit green, linters pinned to match pre-commit |
| Stores | all 13 arm/capture/archive SQLite stores schema-versioned; arm stores constrained |
| Shutdown | daemons finish their current step on SIGTERM; live place-then-record is atomic w.r.t. signals |
| Dependencies | every outbound connector behind a circuit breaker; open circuits reported by the daily digest |

## What changed most recently

The review graded the system **B−** — strong analytical engineering on a weak
operational base — and produced a prioritised plan. Every release blocker (P0),
every P1 item and every P2 item is now closed:

- Ledger writes are atomic and locked; a torn file no longer destroys an arm's
  record or crash-loops its daemon.
- The drawdown circuit breaker is connected to the automated path for the first
  time, measured account-wide, failing closed.
- Backups take consistent snapshots and verify that they restore.
- Alerting exists at all.
- The web UIs require a token.
- Out-of-sample evidence is tracked separately from the data strategies were
  fitted on.
- (P2, 2026-08-17) Retired code moved to `attic/`; the dependency tree is
  audited in CI; every store is schema-versioned and the arm stores are
  constrained; daemons shut down gracefully with the live order path
  protected; every result is stamped with its producing commit; every outbound
  connector has a circuit breaker. Found on the way and fixed: root doctests
  had never run, and a test had been reaching the real venue.

See `TODO.md` (hardening section) for the full list with commit SHAs.

## Where the strategies stand

Two approaches show positive out-of-sample results since their rules were
frozen, each over a few hundred independent events. Both are reported as
**"encouraging, not yet a result"**, and scaling capital is gated on that
evidence rather than on cumulative P&L. Several other approaches were tested
and abandoned — the intended outcome of the process, not a setback.

## How these numbers were measured

```bash
git ls-files '*.py' | xargs wc -l | tail -1          # LOC
git ls-files 'tests/*.py' | xargs wc -l | tail -1    # test LOC
git rev-list --count HEAD                            # commits
du -sh data/                                         # data on disk
systemctl --user list-units --type=service --state=running --no-legend --plain
make check                                           # lint + typecheck + tests
```
