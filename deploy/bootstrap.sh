#!/usr/bin/env bash
# Second-machine bootstrap (P3/R-26): fresh clone → verified environment.
#
# What it does, in order — each step gated on the one before:
#   1. venv from requirements.lock.txt ONLY (the reproducible path; run.sh
#      installs from requirements.txt, which floats)
#   2. the full local gate: ruff + mypy + the test suite (needs no network;
#      the root conftest blocks sockets)
#   3. dry-run of the systemd unit install so you can SEE what would change
#
# What it deliberately does NOT do:
#   - install units for real, install cron, enable services, or restore
#     data/ — those are operator steps with a checklist of their own:
#     docs/second-machine.md. A bootstrap that silently started a fleet on a
#     half-restored machine would be worse than one that stops here.
#   - create .env or the Kalshi PEM. Those exist ONLY on the original
#     machine (gitignored, and NOT in the nightly data/ backup) — see the
#     runbook's "secrets" section before expecting live-anything to work.
#
# Usage: ./deploy/bootstrap.sh          (from the repo root, after clone)
set -euo pipefail

cd "$(dirname "$0")/.."
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
    for cand in python3.11 python3.12 python3.13 python3; do
        command -v "$cand" >/dev/null && PY="$cand" && break
    done
fi
[[ -n "$PY" ]] || { echo "no python3 found"; exit 1; }
say "python: $PY ($($PY --version 2>&1)) — the lock was generated on 3.11"

if [[ ! -d .venv ]]; then
    say "creating .venv"
    "$PY" -m venv .venv
fi

say "installing from requirements.lock.txt (pinned, transitive deps included)"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.lock.txt

say "running the gate: lint + typecheck + tests (no network needed)"
make lint typecheck test-fast

say "systemd units — DRY RUN (nothing installed; see docs/second-machine.md)"
bash deploy/install.sh || true

say "bootstrap complete"
cat <<'NEXT'
Environment verified. The machine is NOT yet operational — continue with
docs/second-machine.md:
  - secrets   (.env + Kalshi PEM live only on the original machine)
  - data      (restore from the newest /mnt/backups/kalshi-trader snapshot)
  - units     (deploy/install.sh --install; then enable the service list)
  - cron      (crontab deploy/cron/crontab.txt — after diffing)
  - verify    (python -m live_gates; python health_digest.py --print)
NEXT
