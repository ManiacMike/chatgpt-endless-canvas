#!/usr/bin/env bash
# Idempotent board-server launcher. Safe to call from any number of Claude
# Code sessions / terminals: if the server is already up it just reports the
# URL; otherwise it starts one DETACHED (nohup), so it keeps running after
# the calling session exits. Log: ~/Documents/chatgpt-endless-image-gen/board.log
set -euo pipefail

PORT="${BOARD_PORT:-8090}"
URL="http://127.0.0.1:${PORT}"

if curl -s --max-time 2 "${URL}/api/state" >/dev/null 2>&1; then
  echo "board already running: ${URL}"
  exit 0
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="${HOME}/Documents/chatgpt-endless-image-gen/board.log"
mkdir -p "$(dirname "${LOG}")"
nohup python3 "${HERE}/board_server.py" >> "${LOG}" 2>&1 &
disown 2>/dev/null || true

for _ in $(seq 1 30); do
  if curl -s --max-time 1 "${URL}/api/state" >/dev/null 2>&1; then
    echo "board started: ${URL}  (log: ${LOG})"
    exit 0
  fi
  sleep 0.3
done
echo "board failed to start — check ${LOG}" >&2
exit 1
