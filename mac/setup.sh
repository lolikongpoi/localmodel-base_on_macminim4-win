#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"
PYTHON="${PYTHON_BIN:-/opt/homebrew/bin/python3.13}"

if [[ ! -x "$PYTHON" ]]; then
  echo "Python 3.11+ is required. Install it with Homebrew, then set PYTHON_BIN."
  exit 1
fi

if [[ ! -d .venv ]]; then
  "$PYTHON" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo "Setup complete. Run ./start-lan.sh next. The first start downloads the ~3.1 GB model."
