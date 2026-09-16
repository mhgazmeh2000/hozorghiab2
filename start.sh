#!/usr/bin/env sh
# Start the Hozor attendance app on Linux/macOS (equivalent of start.bat).
# Usage:  ./start.sh          (start detached, logs in ./logs)
#         ./start.sh stop     (stop a running instance)
#         ./start.sh status
set -u
cd "$(dirname "$0")"

PORT_WEB="${PORT_WEB:-5000}"
LOG_DIR="logs"
mkdir -p "$LOG_DIR"

pid_on_port() {
  # first LISTEN pid on the given port (lsof or ss fallback)
  if command -v lsof >/dev/null 2>&1; then
    lsof -t -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -n1
  elif command -v ss >/dev/null 2>&1; then
    ss -lptn "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2
  fi
}

case "${1:-start}" in
  stop)
    PID="$(pid_on_port "$PORT_WEB")"
    if [ -n "${PID:-}" ]; then
      kill "$PID" && echo "Stopped (PID $PID)."
    else
      echo "Not running."
    fi
    ;;
  status)
    PID="$(pid_on_port "$PORT_WEB")"
    if [ -n "${PID:-}" ]; then
      echo "Running (PID $PID) at http://127.0.0.1:$PORT_WEB"
    else
      echo "Not running."
    fi
    ;;
  start|*)
    PID="$(pid_on_port "$PORT_WEB")"
    if [ -n "${PID:-}" ]; then
      echo "Already running (PID $PID) at http://127.0.0.1:$PORT_WEB"
      exit 0
    fi
    PYTHON="${PYTHON:-python3}"
    if ! command -v "$PYTHON" >/dev/null 2>&1; then PYTHON=python; fi
    echo "Starting with $PYTHON..."
    nohup "$PYTHON" app.py >>"$LOG_DIR/server.log" 2>>"$LOG_DIR/server.err.log" &
    echo "Started (PID $!) at http://127.0.0.1:$PORT_WEB"
    ;;
esac
