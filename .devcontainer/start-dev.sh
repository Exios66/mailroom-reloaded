#!/usr/bin/env bash
# Boot the mailroom dev stack inside the VS Code dev container (image mode).
#
#   mock provider -> 127.0.0.1:8001   (DEFAULT_PROVIDER=mock, MOCK_BASE_URL)
#   API           -> 0.0.0.0:8000     (uvicorn --reload over /app/src)
#
# Idempotent: safe to run from postStartCommand or the "dev: restart" VS Code
# task. Logs land in /tmp/mock.log and /tmp/app.log.
set -euo pipefail

start_bg() { # <name> <cmd...>
  local name="$1"
  shift
  local pidfile="/tmp/${name}.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "${name}: already running (pid $(cat "$pidfile"))"
    return 0
  fi
  nohup "$@" >"/tmp/${name}.log" 2>&1 &
  echo $! >"$pidfile"
  echo "${name}: started (pid $!) -> /tmp/${name}.log"
}

start_bg mock \
  uvicorn mock_openai:app --app-dir /app/deploy --host 0.0.0.0 --port 8001

start_bg app \
  uvicorn mailroom_reloaded.api.app:app --host 0.0.0.0 --port 8000 \
  --reload --reload-dir /app/src

echo "mailroom: API http://localhost:8000/ui   mock http://localhost:8001/health"
