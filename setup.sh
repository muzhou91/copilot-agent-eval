#!/usr/bin/env bash
# One-time setup for Copilot Agent Eval on macOS / Linux.
# Usage: bash setup.sh
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Creating Python virtual environment (.venv)..."
python3 -m venv .venv
source .venv/bin/activate

echo "==> Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing Playwright Chromium browser..."
playwright install chromium

# Create config and cases from examples if they don't exist
[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f cases.csv ]  || cp cases.example.csv cases.csv

echo ""
echo "Setup complete. Next steps:"
echo "  1. Edit config.yaml if needed (default is fine for most cases)"
echo "  2. Edit cases.csv with your test queries"
echo "  3. export DL_SECRET=\"your Direct Line secret\""
echo "  4. source .venv/bin/activate"
echo "  5. python -m copilot_agent_eval --auth-only   # verify sign-in first"
echo "  6. python -m copilot_agent_eval -v            # run all cases"
