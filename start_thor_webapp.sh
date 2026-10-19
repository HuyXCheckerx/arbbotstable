#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -f ".venv/bin/python3" ]]; then
    PYTHON_BIN=".venv/bin/python3"
elif [[ -f "venv/bin/python3" ]]; then
    PYTHON_BIN="venv/bin/python3"
else
    PYTHON_BIN="python3"
fi

exec "$PYTHON_BIN" thor_webapp.py "$@"
