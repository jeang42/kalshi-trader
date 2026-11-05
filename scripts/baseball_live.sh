#!/usr/bin/env bash
# Run the baseball live-event collector as a long-lived daemon.
#
# Usage:
#   scripts/baseball_live.sh                # foreground (Ctrl-C to stop)
#   scripts/baseball_live.sh --background   # detach, log to data/shared/baseball_live.log
#   scripts/baseball_live.sh --stop         # stop the background daemon
#
# Polls MLB feed/live for every in-progress game every 60s. Cheap when
# no games are running (one local SQLite query + no HTTP). Safe to leave
# running 24/7.
#
# Storage: appends to data/shared/baseball_state.sqlite (live_plays table).

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PID_FILE="data/shared/baseball_live.pid"
LOG_FILE="data/shared/baseball_live.log"

case "${1:-}" in
    --background)
        if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "already running (PID $(cat "$PID_FILE"))"
            exit 0
        fi
        mkdir -p "$(dirname "$LOG_FILE")"
        nohup .venv/bin/python -m baseball.live --loop --interval 60 >> "$LOG_FILE" 2>&1 &
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
        exec .venv/bin/python -m baseball.live --loop --interval 60
        ;;
    *)
        echo "usage: $0 [--background|--stop|--status]" >&2
        exit 2
        ;;
esac
