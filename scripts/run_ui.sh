#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8765}"
ENV_NAME="${ENV_NAME:-py311}"

cd "$ROOT_DIR"

if command -v micromamba >/dev/null 2>&1; then
  exec micromamba run -n "$ENV_NAME" python -m uvicorn ui.app:app --host "$HOST" --port "$PORT"
fi

exec python3 -m uvicorn ui.app:app --host "$HOST" --port "$PORT"
