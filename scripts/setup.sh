#!/usr/bin/env bash
# One-shot setup for macOS / Linux:  bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env - add your GOOGLE_API_KEY to it."; }
.venv/bin/python -m threadroute seed
echo
echo "Setup complete. Activate with:  source .venv/bin/activate"
echo "Then try:  python -m threadroute demo     (no API key needed)"
