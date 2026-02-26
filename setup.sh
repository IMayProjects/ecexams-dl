#!/usr/bin/env bash
# setup.sh – Sets up a virtual environment and installs all dependencies
# Works on macOS and Linux.
# Usage: bash setup.sh

set -e

VENV_DIR="venv"
PYTHON=""

echo ""
echo "╔══════════════════════════════════════╗"
echo "║      ECExams Scraper – Setup         ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. Find Python 3.8+ ────────────────────────────────────────────────────
for cmd in python3 python3.12 python3.11 python3.10 python3.9 python3.8 python; do
  if command -v "$cmd" &>/dev/null; then
    version=$("$cmd" -c 'import sys; print(sys.version_info[:2])')
    if "$cmd" -c 'import sys; exit(0 if sys.version_info >= (3,8) else 1)' 2>/dev/null; then
      PYTHON="$cmd"
      echo "✓ Found Python: $("$cmd" --version)"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "✗ Python 3.8+ not found. Please install it from https://www.python.org/downloads/"
  exit 1
fi

# ── 2. Create virtual environment ──────────────────────────────────────────
if [ -d "$VENV_DIR" ]; then
  echo "✓ Virtual environment already exists at ./$VENV_DIR"
else
  echo "→ Creating virtual environment at ./$VENV_DIR ..."
  "$PYTHON" -m venv "$VENV_DIR"
  echo "✓ Virtual environment created"
fi

# ── 3. Activate and install ────────────────────────────────────────────────
source "$VENV_DIR/bin/activate"

echo "→ Upgrading pip..."
pip install --upgrade pip --quiet

echo "→ Installing dependencies..."
pip install -r requirements.txt

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  ✓ Setup complete!                                   ║"
echo "║                                                      ║"
echo "║  To start the web UI:                                ║"
echo "║    source venv/bin/activate                          ║"
echo "║    python app.py                                     ║"
echo "║    → open http://localhost:5000                      ║"
echo "║                                                      ║"
echo "║  Or use the CLI scraper directly:                    ║"
echo "║    python ecexams_scraper.py --help                  ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
