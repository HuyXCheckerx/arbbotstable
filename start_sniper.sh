#!/usr/bin/env bash
set -e

# ==============================================================================
# Cross-Platform Sniper Launcher for Linux VPS & macOS
# Automatically finds virtual environment (.venv, venv) or system python3
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

ARGS=("$@")
if [ $# -eq 0 ]; then
    ARGS=(--live --confirm-live EXECUTE_PROFIT_SNIPER)
fi

echo "==> Running sniper via $PYTHON_BIN"
exec "$PYTHON_BIN" sniper.py "${ARGS[@]}"
