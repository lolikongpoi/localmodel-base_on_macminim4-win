#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "Please run ./setup.sh first."
  exit 1
fi
if [[ -z "${TAGGER_API_KEY:-}" ]]; then
  echo "TAGGER_API_KEY must be set. Example:"
  echo "  export TAGGER_API_KEY='choose-a-long-random-secret'"
  exit 1
fi

HOST="${TAGGER_HOST:-0.0.0.0}"
PORT="${TAGGER_PORT:-8787}"
MODEL_PRESET="${MODEL_PRESET:-4b}"
case "$MODEL_PRESET" in
  4b) export MODEL_ID="${MODEL_ID:-mlx-community/Qwen3-VL-4B-Instruct-4bit}" ;;
  8b) export MODEL_ID="${MODEL_ID:-mlx-community/Qwen3-VL-8B-Instruct-4bit}" ;;
  *) echo "MODEL_PRESET must be 4b or 8b."; exit 1 ;;
esac
# Keep all model weights and Hugging Face metadata in this project instead of
# the user's global cache.  Override HF_HOME only when deliberately desired.
export HF_HOME="${HF_HOME:-$PWD/models/huggingface}"
export HF_HUB_CACHE="${HF_HUB_CACHE:-$HF_HOME/hub}"
# Some networks interfere with Hugging Face's optional Xet transfer layer.
# Plain HTTPS is slower but more dependable for an unattended LAN appliance.
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
mkdir -p "$HF_HUB_CACHE"
exec .venv/bin/python -m uvicorn tagger_api:app --host "$HOST" --port "$PORT"
