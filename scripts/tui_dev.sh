#!/usr/bin/env bash
# Local harness for the /tui browser terminal: mock LLM + API + embedded watcher.
#   scripts/tui_dev.sh up|down|status
#   MAILROOM_API_TOKEN=secret scripts/tui_dev.sh up    # exercise the auth path
# Loopback only. State lives under ./data/tui-dev (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STATE="$ROOT/data/tui-dev"
BASE="$STATE/base"
MOCK_PORT="${TUI_MOCK_PORT:-8899}"
API_PORT="${TUI_API_PORT:-8000}"
API="http://127.0.0.1:${API_PORT}"
MOCK_PID="$STATE/mock.pid"
API_PID="$STATE/api.pid"

alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

wait_http() { # url label pidfile
  for _ in $(seq 1 60); do
    curl -fsS "$1" >/dev/null 2>&1 && return 0
    alive "$3" || break
    sleep 1
  done
  echo "tui_dev: $2 did not become healthy (see $STATE/*.log)" >&2
  return 1
}

stop_one() {
  if alive "$1"; then
    kill "$(cat "$1")" 2>/dev/null || true
    for _ in $(seq 1 10); do alive "$1" || break; sleep 0.5; done
    alive "$1" && kill -9 "$(cat "$1")" 2>/dev/null || true
  fi
  rm -f "$1"
}

up() {
  if alive "$API_PID" || alive "$MOCK_PID"; then
    echo "tui_dev: already running (use 'down' first)" >&2
    exit 1
  fi
  mkdir -p "$STATE" "$BASE"
  uv run uvicorn --app-dir deploy mock_openai:app --host 127.0.0.1 --port "$MOCK_PORT" \
    >"$STATE/mock.log" 2>&1 &
  echo $! >"$MOCK_PID"
  wait_http "http://127.0.0.1:${MOCK_PORT}/health" "mock provider" "$MOCK_PID"

  MOCK_BASE_URL="http://127.0.0.1:${MOCK_PORT}/v1" \
  DEFAULT_PROVIDER=mock \
  MAILROOM_BASE_DIR="$BASE" \
  MAILROOM_API_TOKEN="${MAILROOM_API_TOKEN:-}" \
    uv run mailroom serve --host 127.0.0.1 --port "$API_PORT" >"$STATE/api.log" 2>&1 &
  echo $! >"$API_PID"
  wait_http "$API/health" "api" "$API_PID"

  mkdir -p "$BASE/inbox"
  cp scripts/tui_seed/* "$BASE/inbox/"
  echo "tui_dev: up"
  echo "  tui   $API/tui"
  echo "  ui    $API/ui"
  echo "  mock  http://127.0.0.1:${MOCK_PORT}"
  if [[ -n "${MAILROOM_API_TOKEN:-}" ]]; then echo "  token set"; else echo "  token none"; fi
  echo "  pids  mock=$(cat "$MOCK_PID") api=$(cat "$API_PID")"
}

down() {
  stop_one "$API_PID"
  stop_one "$MOCK_PID"
  echo "tui_dev: down"
}

status() {
  local rc=0
  for pair in "mock:$MOCK_PID" "api:$API_PID"; do
    if alive "${pair#*:}"; then echo "${pair%%:*}: running (pid $(cat "${pair#*:}"))"
    else echo "${pair%%:*}: stopped"; rc=1; fi
  done
  if curl -fsS "$API/health" >/dev/null 2>&1; then echo "health: ok"; else echo "health: unreachable"; rc=1; fi
  return "$rc"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *) echo "usage: $0 up|down|status" >&2; exit 2 ;;
esac
