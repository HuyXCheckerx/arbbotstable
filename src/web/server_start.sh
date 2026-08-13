#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

GIT_REPOSITORY_URL="${GIT_REPOSITORY_URL:-https://github.com/HuyXCheckerx/arbbotstable.git}"
GIT_BRANCH="${GIT_BRANCH:-main}"
NEEDS_GIT_BOOTSTRAP=0

if ! command -v git >/dev/null 2>&1; then
  echo "Git is required but is not installed."
  exit 1
fi

if [[ ! -d "$ROOT_DIR/.git" ]]; then
  echo "Creating .git repository..."
  git init -b "$GIT_BRANCH"
  NEEDS_GIT_BOOTSTRAP=1
elif ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  NEEDS_GIT_BOOTSTRAP=1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "$GIT_REPOSITORY_URL"
fi

if [[ "${SKIP_GIT_PULL:-0}" != "1" ]]; then
  if [[ "$NEEDS_GIT_BOOTSTRAP" == "1" ]]; then
    echo "Connecting .git to $GIT_REPOSITORY_URL ($GIT_BRANCH)..."
    git fetch origin "$GIT_BRANCH"
    # Attach the current files to the fetched commit without overwriting .env,
    # runtime state, or any other files already present on the server.
    git reset --mixed "origin/$GIT_BRANCH"
    git branch --set-upstream-to="origin/$GIT_BRANCH" "$GIT_BRANCH"
  else
    echo "Updating code from GitHub..."
    git pull --ff-only
  fi
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
PIP_FLAGS=()
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  PIP_FLAGS+=(--user)
fi

"$PYTHON_BIN" -m pip install "${PIP_FLAGS[@]}" -r requirements.txt
exec "$PYTHON_BIN" app.py
