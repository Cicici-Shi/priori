#!/bin/bash
# Double-click to start Priori. If it's already running, just open the browser;
# otherwise start the server and then open it. Closing this Terminal window stops the server.
# No hardcoded paths: the project dir is this script's own folder; uv is found on PATH.

PORT=8000
URL="http://localhost:${PORT}"

# This script's directory (the project root) — resolved even when double-clicked
DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$DIR" || { echo "Project directory not found: $DIR"; exit 1; }

# Locate uv (fall back to the common install location if it's not on PATH)
UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
[ -z "$UV" ] && { echo "uv not found. Install it first: https://docs.astral.sh/uv/"; exit 1; }

# Already running? Just open the browser, don't start a second instance.
if curl -s -o /dev/null "$URL"; then
  echo "Priori is already running, opening the browser…"
  open "$URL"
  exit 0
fi

echo "Starting Priori… (closing this window stops the server)"
"$UV" run uvicorn app.main:app --port "$PORT" &
SERVER_PID=$!

# Wait for the port to come up (up to ~30s), then open the browser
for i in $(seq 1 60); do
  if curl -s -o /dev/null "$URL"; then
    echo "Ready, opening → $URL"
    open "$URL"
    break
  fi
  sleep 0.5
done

# Stay attached to the server process so the window stays open; closing it stops the server
wait $SERVER_PID
