#!/usr/bin/env bash
# Launch Chrome with a remote-debugging port and a dedicated profile so the
# ChatGPT image generator (generate_chatgpt_image.py) can attach over CDP and
# reuse your logged-in chatgpt.com session.
#
# Run this once, log into ChatGPT in the window that opens, and leave it open.
# The profile dir persists your login between launches. This dedicated profile
# never clashes with your everyday Chrome.
#
# Override with env:
#   CHROME_BIN   path to Chrome binary
#   DEBUG_PORT   remote debugging port (default 9222; match CHATGPT_CDP_URL)
#   PROFILE_DIR  user-data dir (default ~/.chatgpt-image-gen-chrome)
set -euo pipefail

DEBUG_PORT="${DEBUG_PORT:-9222}"
PROFILE_DIR="${PROFILE_DIR:-$HOME/.chatgpt-image-gen-chrome}"

CHROME_BIN="${CHROME_BIN:-}"
if [[ -z "${CHROME_BIN}" ]]; then
  for candidate in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "$(command -v google-chrome || true)" \
    "$(command -v chromium || true)"; do
    if [[ -n "${candidate}" && -x "${candidate}" ]]; then
      CHROME_BIN="${candidate}"
      break
    fi
  done
fi
if [[ -z "${CHROME_BIN}" || ! -x "${CHROME_BIN}" ]]; then
  echo "Chrome not found. Set CHROME_BIN=/path/to/chrome and retry." >&2
  exit 1
fi

mkdir -p "${PROFILE_DIR}"
echo "Launching Chrome on debug port ${DEBUG_PORT} (profile: ${PROFILE_DIR})"
echo "Log into chatgpt.com in the window, then keep it open."
exec "${CHROME_BIN}" \
  --remote-debugging-port="${DEBUG_PORT}" \
  --user-data-dir="${PROFILE_DIR}" \
  --no-first-run \
  --no-default-browser-check \
  "https://chatgpt.com/"
