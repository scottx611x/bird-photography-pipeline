#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$HOME/.birb_host.pid"
PYTHON="$HOME/.pyenv/versions/3.12.11/bin/python3"

# Use the pyenv python — it has browser_cookie3 (system python3 may not)
export BUFFER_COOKIES="$("$PYTHON" "$DIR/extract_buffer_cookies.py")"
docker compose -f "$DIR/docker-compose.yml" down
docker compose -f "$DIR/docker-compose.yml" up --build -d

# Kill any stale lr_host.py right before restarting it
if [ -f "$PID_FILE" ]; then
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  rm -f "$PID_FILE"
fi
lsof -ti:8766 | xargs kill -9 2>/dev/null || true
sleep 0.5

"$PYTHON" "$DIR/lr_host.py" >> "$HOME/.birb_host.log" 2>&1 &
echo $! > "$PID_FILE"
echo "lr_host.py started (pid $(cat "$PID_FILE"))  log: ~/.birb_host.log"
