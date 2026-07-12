#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -d "$ROOT_DIR/.git" ]]; then
  echo "This directory is not a Git clone. Clone the repository here first."
  exit 1
fi

if [[ "${SKIP_GIT_PULL:-0}" != "1" ]]; then
  echo "Updating code from GitHub..."
  git pull --ff-only
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
PIP_FLAGS=()
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  PIP_FLAGS+=(--user)
fi

"$PYTHON_BIN" -m pip install "${PIP_FLAGS[@]}" -r requirements.txt
exec "$PYTHON_BIN" app.py
