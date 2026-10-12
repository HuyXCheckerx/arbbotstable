#!/usr/bin/env bash
set -e

# ==============================================================================
# Standalone USDP Arbitrage Scanner Launcher for Linux VPS & macOS
# Automatically resolves Python environment (.venv, venv, system python3)
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN=""
if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
elif [ -x "$SCRIPT_DIR/venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
else
    echo "Error: Python interpreter not found. Please create a virtual environment or install python3." >&2
    exit 1
fi

exec "$PYTHON_BIN" usdp_arb.py "$@"
