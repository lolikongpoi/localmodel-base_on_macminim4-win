#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "请先运行：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

HOST="${REMOTE_WEB_HOST:-0.0.0.0}"
PORT="${REMOTE_WEB_PORT:-8788}"
exec .venv/bin/python -m uvicorn app:app --host "$HOST" --port "$PORT"

