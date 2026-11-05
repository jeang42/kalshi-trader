#!/usr/bin/env bash
#
# Prepare the environment and launch the Polymarket Analyzer.
#
# Usage:
#   ./run.sh                       # default port 8501
#   ./run.sh --server.port 8502    # pass-through args go straight to streamlit
#   PORT=8080 ./run.sh             # or set PORT via env var
#
# What it does:
#   1. Verifies Python 3.11+ is installed
#   2. Creates ./.venv if it doesn't exist
#   3. Installs/updates requirements.txt into the venv
#   4. Launches `streamlit run app.py` with any extra args
#
# The pip install step is idempotent — already-satisfied packages are
# skipped quickly, so re-running this script costs only a few seconds
# after the first run.

set -euo pipefail

cd "$(dirname "$0")"

# ── Colors for output ────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
else
    BLUE=''; GREEN=''; YELLOW=''; RED=''; NC=''
fi
info()  { printf "${BLUE}▸${NC} %s\n" "$*"; }
ok()    { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}!${NC} %s\n" "$*"; }
fail()  { printf "${RED}✗${NC} %s\n" "$*" >&2; exit 1; }

# ── 1. Verify Python 3.11+ ───────────────────────────────────────────────
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 \
    || fail "Python 3 not found. Install Python 3.11+ or set PYTHON=/path/to/python3."

PY_VERSION=$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$("$PY" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
if [[ "$PY_OK" != "1" ]]; then
    fail "Python 3.11+ required, found $PY_VERSION. Set PYTHON=/path/to/python3.11+ or install a newer version."
fi
ok "Python $PY_VERSION"

# ── 2. Create venv if missing ────────────────────────────────────────────
VENV_DIR=".venv"
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment in $VENV_DIR"
    "$PY" -m venv "$VENV_DIR"
    ok "Virtualenv created"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

# ── 3. Install / update dependencies ─────────────────────────────────────
info "Installing dependencies (this is fast if already up to date)"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
ok "Dependencies ready"

# ── 4. Launch Streamlit ──────────────────────────────────────────────────
PORT="${PORT:-8501}"
# The Streamlit console has NO authentication and (when the live gates are
# enabled in .env) can place real orders — so it binds loopback by default.
# To serve it on a network interface, set BIND_HOST=0.0.0.0 (or an address)
# explicitly, and understand that anyone who can reach it can act as you.
BIND_HOST="${BIND_HOST:-127.0.0.1}"

# If user passed --server.port / --server.address, respect them; otherwise
# inject our defaults.
INJECT_PORT=true
INJECT_ADDR=true
for arg in "$@"; do
    if [[ "$arg" == --server.port* ]]; then INJECT_PORT=false; fi
    if [[ "$arg" == --server.address* ]]; then INJECT_ADDR=false; fi
done

EXTRA_ARGS=("$@")
if $INJECT_PORT; then
    EXTRA_ARGS+=(--server.port "$PORT")
fi
if $INJECT_ADDR; then
    EXTRA_ARGS+=(--server.address "$BIND_HOST")
fi

info "Launching Streamlit on http://localhost:${PORT} (provider: ${MARKET_PROVIDER:-kalshi})"
echo
exec streamlit run app.py "${EXTRA_ARGS[@]}"
