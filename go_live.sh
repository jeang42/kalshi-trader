#!/usr/bin/env bash
# go_live.sh — guarded enabler for taking ONE strategy daemon live at
# minimum size. Refuses to enable unless the read-only preflight passes.
#
# Dry-run (default): runs the preflight for the target profile and reports
# go/no-go. Changes NOTHING.
#   ./go_live.sh --profile agentic_favorite_hold
#
# Enable (real money): only after preflight passes, upserts the live toggles
# into .env and restarts the service.
#   ./go_live.sh --profile agentic_favorite_hold --service favorite-hold \
#                --max-stake 2 --enable
#
# To STOP at any time: python -m emergency_stop activate
set -euo pipefail
cd "$(dirname "$0")"

PROFILE="agentic_favorite_hold"
SERVICE="favorite-hold"
MAX_STAKE="2"
ENABLE=0
PY=".venv/bin/python"

while [ $# -gt 0 ]; do
  case "$1" in
    --profile)   PROFILE="$2"; shift 2 ;;
    --service)   SERVICE="$2"; shift 2 ;;
    --max-stake) MAX_STAKE="$2"; shift 2 ;;
    --enable)    ENABLE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "=================================================================="
echo "GO-LIVE  profile=$PROFILE  service=$SERVICE  max_stake=\$$MAX_STAKE"
echo "  mode: $([ $ENABLE -eq 1 ] && echo 'ENABLE (real money)' || echo 'dry-run (no changes)')"
echo "=================================================================="

# 1. Read-only preflight, with the owner lock pointed at the target profile.
echo "Running read-only preflight (places nothing)…"
if ! KALSHI_OWNER_PROFILE="$PROFILE" "$PY" live_preflight.py --profile "$PROFILE"; then
  echo ""
  echo "PREFLIGHT FAILED — refusing to go live. Nothing changed." >&2
  exit 1
fi

if [ $ENABLE -eq 0 ]; then
  echo ""
  echo "Dry-run OK. Re-run with --enable to go live at \$$MAX_STAKE/order."
  echo "(This will set KALSHI_OWNER_PROFILE=$PROFILE — the live account is"
  echo " then owned by that profile; the UI trades live only when it's active.)"
  exit 0
fi

# 2. Enable: upsert the live toggles into .env (idempotent), then restart.
echo ""
echo "Preflight passed. Writing live toggles to .env…"
touch .env
upsert() {  # upsert KEY=VALUE into .env
  local key="$1" val="$2"
  if grep -qE "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
  echo "  $key=$val"
}
upsert KALSHI_LIVE_ENABLED true
upsert KALSHI_OWNER_PROFILE "$PROFILE"
upsert LIVE_TRADING_PROFILES "$PROFILE"
upsert LIVE_MAX_STAKE_DOLLARS "$MAX_STAKE"

echo ""
echo "Restarting $SERVICE.service…"
systemctl --user restart "${SERVICE}.service"
sleep 2
systemctl --user is-active "${SERVICE}.service"
echo ""
echo "LIVE. Watch the audit log:  tail -f data/shared/audit/orders.log"
echo "STOP anytime:               $PY -m emergency_stop activate"
