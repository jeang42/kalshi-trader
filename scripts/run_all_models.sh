#!/usr/bin/env bash
# Run bulk_deep_analysis across every recommended model, sequentially.
# Streams each model's output to the terminal and to a per-model log.
#
# Usage:
#   scripts/run_all_models.sh [N_MARKETS] [MAX_DAYS] [-- extra args]
#
# Examples:
#   scripts/run_all_models.sh 15              # 15 markets per model, 30-day window
#   scripts/run_all_models.sh 25 60           # 25 markets per model, 60-day window
#   scripts/run_all_models.sh 10 30 --no-web-context
#
# Defaults:
#   N_MARKETS = 10
#   MAX_DAYS  = 30
#   --skip-age-hours 0 is set automatically so a manual run after the
#   nightly cron doesn't skip everything as "recently analyzed."
#
# Sequential by design — one 30B+ model at a time saturates the GPU.
# Running in parallel would just thrash VRAM and slow each one down.
#
# Each model's full stdout/stderr is also written to
# data/shared/run_all_models_<model-key>_<timestamp>.log for later
# inspection. Logrotate sweeps these via the existing data/shared/*.log glob.

set -euo pipefail

# ─── Args ─────────────────────────────────────────────────────────────────
N_MARKETS="${1:-10}"
MAX_DAYS="${2:-30}"
shift $(( $# >= 2 ? 2 : ($# >= 1 ? 1 : 0) )) 2>/dev/null || true
EXTRA_ARGS="$@"

# ─── Models ───────────────────────────────────────────────────────────────
# Keep in sync with llm_deep_analysis.RECOMMENDED_MODELS.
MODELS=(
    "qwen3:32b"
    "qwen2.5:32b"
    "deepseek-r1:32b"
    "gemma3:27b"
    "qwen2.5:14b"
    "llama3.1:latest"
)

# ─── Paths ────────────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ ! -x .venv/bin/python ]]; then
    echo "ERROR: .venv/bin/python not found. Run ./run.sh once to set up." >&2
    exit 1
fi

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG_DIR="data/shared"
mkdir -p "$LOG_DIR"

# ─── Header ───────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════════════"
echo "  bulk_deep_analysis across ${#MODELS[@]} models"
echo "  N_MARKETS=$N_MARKETS  MAX_DAYS=$MAX_DAYS  extras=$EXTRA_ARGS"
echo "  started: $(date -u +'%Y-%m-%d %H:%M:%S UTC')"
echo "════════════════════════════════════════════════════════════════════"

TOTAL_START=$(date +%s)
declare -A MODEL_RESULTS

# ─── Run each model ───────────────────────────────────────────────────────
for MODEL in "${MODELS[@]}"; do
    # Sanitize model name for log filename: replace : and / with _
    MODEL_SAFE=$(echo "$MODEL" | tr ':/' '__')
    LOG_FILE="$LOG_DIR/run_all_models_${MODEL_SAFE}_${TIMESTAMP}.log"

    echo
    echo "─── $MODEL ─────────────────────────────────────────────────"
    echo "    log: $LOG_FILE"
    echo

    START=$(date +%s)
    # tee streams to both terminal and log file; pipefail catches the
    # python exit code through the tee.
    if .venv/bin/python -m pm.ops.bulk_deep_analysis \
        --model "$MODEL" \
        --max-markets "$N_MARKETS" \
        --max-days "$MAX_DAYS" \
        --skip-age-hours 0 \
        $EXTRA_ARGS \
        2>&1 | tee "$LOG_FILE"; then
        STATUS="ok"
    else
        STATUS="FAILED"
    fi
    ELAPSED=$(( $(date +%s) - START ))
    MODEL_RESULTS["$MODEL"]="$STATUS in ${ELAPSED}s"
    echo
    echo "    → $MODEL: $STATUS in ${ELAPSED}s"
done

# ─── Footer ───────────────────────────────────────────────────────────────
TOTAL_ELAPSED=$(( $(date +%s) - TOTAL_START ))
echo
echo "════════════════════════════════════════════════════════════════════"
echo "  SUMMARY  (total: ${TOTAL_ELAPSED}s = $(( TOTAL_ELAPSED / 60 )) min)"
echo "════════════════════════════════════════════════════════════════════"
for MODEL in "${MODELS[@]}"; do
    printf "  %-25s  %s\n" "$MODEL" "${MODEL_RESULTS[$MODEL]:-not run}"
done
echo
echo "  Logs in $LOG_DIR/run_all_models_*_${TIMESTAMP}.log"
