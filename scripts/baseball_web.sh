#!/usr/bin/env bash
# Launch the baseball sidecar web UI on port 8770.
#
# Usage:
#   scripts/baseball_web.sh                # foreground
#   scripts/baseball_web.sh --background   # detach + log to data/shared/baseball_web.log
#
# LAN-only by default (binds 0.0.0.0). Tunnel via SSH for off-LAN access.
# No auth — see TODO.md → "Authentication + per-user access control".

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
