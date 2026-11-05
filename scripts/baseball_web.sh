#!/usr/bin/env bash
# Launch the baseball sidecar web UI on port 8770.
#
# Usage:
#   scripts/baseball_web.sh                # foreground
#   scripts/baseball_web.sh --background   # detach + log to data/shared/baseball_web.log
#
# Binds loopback-only unless WEB_UI_TOKEN (shared-secret gate) or
# WEB_BIND_HOST is set in .env — see pm/core/web_auth.py.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "${1:-}" == "--background" ]]; then
    LOG="data/shared/baseball_web.log"
    mkdir -p "$(dirname "$LOG")"
    nohup .venv/bin/python -m baseball.web >> "$LOG" 2>&1 &
    echo "baseball web UI started in background (PID $!)"
    echo "log: $LOG"
    echo "open: http://localhost:8770"
else
    exec .venv/bin/python -m baseball.web
fi
