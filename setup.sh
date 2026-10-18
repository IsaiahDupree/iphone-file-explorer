#!/bin/zsh
set -e

echo "=== iPhone File Explorer Setup ==="

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install via Homebrew: brew install python"
  exit 1
fi

PYTHON=$(command -v python3)
echo "Using Python: $PYTHON ($($PYTHON --version))"

# Create virtualenv
if [ ! -d "venv" ]; then
  echo "Creating virtualenv..."
  $PYTHON -m venv venv
fi

source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# .env
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "  Created .env from .env.example"
  echo "  --> Edit .env and set ANTHROPIC_API_KEY before running."
  echo ""
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "To start the server:"
echo "  source venv/bin/activate"
echo "  uvicorn main:app --host 0.0.0.0 --port 5562 --reload"
echo ""
echo "Then open: http://localhost:5562"
