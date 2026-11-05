#!/usr/bin/env bash
# Launch the WNBA sidecar web UI on port 8771.
#
# Usage:
#   scripts/wnba_web.sh                # foreground
#   scripts/wnba_web.sh --background   # detach + log to data/shared/wnba_web.log
#
# Binds loopback-only unless WEB_UI_TOKEN (shared-secret gate) or
# WEB_BIND_HOST is set in .env — see pm/core/web_auth.py.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "${1:-}" == "--background" ]]; then
    LOG="data/shared/wnba_web.log"
    mkdir -p "$(dirname "$LOG")"
    nohup .venv/bin/python -m wnba.web >> "$LOG" 2>&1 &
    echo "wnba web UI started in background (PID $!)"
    echo "log: $LOG"
    echo "open: http://localhost:8771"
else
    exec .venv/bin/python -m wnba.web
fi
