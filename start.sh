#!/usr/bin/env bash
# Idempotent board-server launcher. Safe to call from any number of Claude
# Code sessions / terminals: if OUR server is already up (verified via
# /api/health identity, not just "something answers") it reports the URL;
# otherwise it starts one DETACHED (nohup) so it outlives the calling session.
# Ports occupied by other programs (e.g. an old pre-rename copy of this
# project) are skipped — the server walks up from BOARD_PORT until it binds.
# Prints the ACTUAL url (may not be :8090). Log: <data root>/board.log
set -euo pipefail

BASE_PORT="${BOARD_PORT:-8090}"
PORT_TRIES="${BOARD_PORT_TRIES:-20}"
APP_ID='"app": "chatgpt-endless-canvas"'
DATA="${IMAGE_GEN_DATA:-$HOME/Documents/chatgpt-endless-image-gen}"
LOG="${DATA}/board.log"

# Scan the port range for a server that identifies as ours; echo its port.
find_ours() {
  local p
  for p in $(seq "${BASE_PORT}" $((BASE_PORT + PORT_TRIES - 1))); do
    if curl -s --max-time 1 "http://127.0.0.1:${p}/api/health" 2>/dev/null \
        | grep -qF "${APP_ID}"; then
      echo "${p}"
      return 0
    fi
  done
  return 1
}

if PORT="$(find_ours)"; then
  echo "board already running: http://127.0.0.1:${PORT}"
  exit 0
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${HERE}/.venv/bin/python"
[ -x "${PY}" ] || PY="python3"
mkdir -p "${DATA}"
nohup "${PY}" "${HERE}/board_server.py" --port "${BASE_PORT}" \
  --port-tries "${PORT_TRIES}" >> "${LOG}" 2>&1 &
disown 2>/dev/null || true

for _ in $(seq 1 40); do
  if PORT="$(find_ours)"; then
    echo "board started: http://127.0.0.1:${PORT}  (log: ${LOG})"
    exit 0
  fi
  sleep 0.3
done
echo "board failed to start — check ${LOG}" >&2
exit 1
