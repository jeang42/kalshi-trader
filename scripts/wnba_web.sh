#!/usr/bin/env bash
# Launch the WNBA sidecar web UI on port 8771.
#
# Usage:
#   scripts/wnba_web.sh                # foreground
#   scripts/wnba_web.sh --background   # detach + log to data/shared/wnba_web.log
#
# LAN-only by default (binds 0.0.0.0). Tunnel via SSH for off-LAN access.
# No auth — see TODO.md → "Authentication + per-user access control".

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
