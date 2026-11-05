#!/usr/bin/env bash
# Launch the tennis sidecar web UI on port 8772.
#   scripts/tennis_web.sh              # foreground
#   scripts/tennis_web.sh --background # detach + log
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ "${1:-}" == "--background" ]]; then
    LOG="data/shared/tennis_web.log"; mkdir -p "$(dirname "$LOG")"
    nohup .venv/bin/python -m tennis.web >> "$LOG" 2>&1 &
    echo "tennis web UI started (PID $!) — http://localhost:8772"
else
    exec .venv/bin/python -m tennis.web
fi
