#!/usr/bin/env bash
# Run the MLB book poller as a long-lived daemon — captures Kalshi
# order books for every open KXMLBGAME-* market at 60s cadence so
# the lead-lag dataset (plays × market reactions) has its market half.
#
# Usage:
#   scripts/mlb_book_poller.sh                # foreground (Ctrl-C to stop)
#   scripts/mlb_book_poller.sh --background   # detach
#   scripts/mlb_book_poller.sh --status       # is it running?
#   scripts/mlb_book_poller.sh --stop         # stop background daemon
#
# Storage: writes to data/shared/book_archive.sqlite (existing schema,
# venue='kalshi'). Idempotent — re-runs overwrite the same-second
# snapshot for a given (venue, market, ts).

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PID_FILE="data/shared/mlb_book_poller.pid"
LOG_FILE="data/shared/mlb_book_poller.log"

case "${1:-}" in
    --background)
        if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "already running (PID $(cat "$PID_FILE"))"
            exit 0
        fi
        mkdir -p "$(dirname "$LOG_FILE")"
        nohup .venv/bin/python -m pm.capture.mlb_book_poller --loop --interval 60 >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "started background daemon (PID $!)"
        echo "log: $LOG_FILE"
        ;;
    --stop)
        if [[ -f "$PID_FILE" ]]; then
            PID=$(cat "$PID_FILE")
            if kill -0 "$PID" 2>/dev/null; then
                kill "$PID"
                echo "stopped PID $PID"
            else
                echo "PID $PID not running"
            fi
            rm -f "$PID_FILE"
        else
            echo "no pidfile; nothing to stop"
        fi
        ;;
    --status)
        if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "running (PID $(cat "$PID_FILE"))"
            echo "tail -f $LOG_FILE  to follow"
        else
            echo "not running"
        fi
        ;;
    "")
        exec .venv/bin/python -m pm.capture.mlb_book_poller --loop --interval 60
        ;;
    *)
        echo "usage: $0 [--background|--stop|--status]" >&2
        exit 2
        ;;
esac
