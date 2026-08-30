#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Google Cloud Ubuntu VM Setup & Reset Script for arbbotstable
# =============================================================================

REPO_URL="https://github.com/HuyXCheckerx/arbbotstable.git"
PROJECT_DIR="$HOME/arbbotstable"

echo "==> Stopping any existing bot / python / node processes..."
pkill -f "sniper.py" || true
pkill -f "crosschain_sniper.py" || true
pkill -f "solana_flash_arb.ts" || true
pkill -f "webapp.py" || true
pkill -f "app.py" || true

echo "==> Updating apt packages..."
sudo apt-get update -y
sudo apt-get install -y git curl build-essential python3 python3-pip python3-venv tmux

# Install Node.js 20.x if not installed or outdated
if ! command -v node >/dev/null 2>&1 || [[ "$(node -v | cut -d'.' -f1 | tr -d 'v')" -lt 20 ]]; then
  echo "==> Installing Node.js 20.x..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi

# Backup existing .env if present
if [[ -f "$PROJECT_DIR/.env" ]]; then
  echo "==> Backing up existing .env..."
  cp "$PROJECT_DIR/.env" "$HOME/.env.backup"
fi

echo "==> Cleaning old project directories..."
rm -rf "$PROJECT_DIR"

echo "==> Cloning fresh repository from GitHub..."
git clone "$REPO_URL" "$PROJECT_DIR"
cd "$PROJECT_DIR"

echo "==> Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
if [[ -f "requirements-eth.txt" ]]; then
  pip install -r requirements-eth.txt || true
fi

echo "==> Installing Node.js dependencies..."
npm install

# Restore .env if backup exists, otherwise copy template
if [[ -f "$HOME/.env.backup" ]]; then
  echo "==> Restoring backed up .env..."
  cp "$HOME/.env.backup" "$PROJECT_DIR/.env"
  chmod 600 "$PROJECT_DIR/.env"
elif [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "==> Creating .env from example..."
  cp .env.example .env
  chmod 600 .env
  echo "!! IMPORTANT: Edit $PROJECT_DIR/.env with your real private keys and RPC URLs !!"
fi

echo "==> Testing build & types..."
npm run typecheck || true

echo "================================================================="
echo "  Setup Complete! Repository is ready at: $PROJECT_DIR"
echo ""
echo "  To start the sniper in a persistent background tmux session:"
echo "    cd $PROJECT_DIR"
echo "    source venv/bin/activate"
echo "    tmux new -s sniper 'python sniper.py --live --confirm-live EXECUTE_PROFIT_SNIPER'"
echo ""
echo "  To detach tmux session: Ctrl+B then D"
echo "  To re-attach later: tmux attach -t sniper"
echo "================================================================="
