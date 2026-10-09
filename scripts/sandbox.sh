#!/usr/bin/env bash
# Offline ingress SANDBOX helper for mailroom-reloaded (deploy/docker-compose.sandbox.yml).
#
#   scripts/sandbox.sh up [--expose]  build + start the sandbox container (127.0.0.1:8100 by default)
#   scripts/sandbox.sh down           stop and remove it
#   scripts/sandbox.sh status         curl /health and /api/sandbox/v1/status
#   scripts/sandbox.sh logs           follow the logs
#   scripts/sandbox.sh run            NO docker: run on the host with uv (127.0.0.1:8100)
#   scripts/sandbox.sh smoke          inject A1 + E1 into a running sandbox and print the trace summary
#   scripts/sandbox.sh reset          stop and delete the sandbox state volume
#
# Environment: SANDBOX_PORT (8100), SANDBOX_CONTENT (smoke | locked | path), SANDBOX_EGRESS (closed|egress),
# MAILROOM_API_TOKEN (required for --expose), SANDBOX_STATE_DIR (host mode, default ./.sandbox-state).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/docker-compose.sandbox.yml"
PORT="${SANDBOX_PORT:-8100}"
BASE="${SANDBOX_URL:-http://127.0.0.1:${PORT}}"

usage() {
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}
fail() { echo "sandbox.sh: $*" >&2; exit 1; }

need_docker() {
  command -v docker >/dev/null 2>&1 || { echo "sandbox.sh: docker not found; use 'scripts/sandbox.sh run' for host mode." >&2; exit 127; }
  docker compose version >/dev/null 2>&1 || { echo "sandbox.sh: the 'docker compose' plugin is required." >&2; exit 127; }
}

auth_header() {
  if [[ -n "${MAILROOM_API_TOKEN:-}" ]]; then printf 'Authorization: Bearer %s' "$MAILROOM_API_TOKEN"; else printf 'X-Sandbox: none'; fi
}

cmd="${1:-}"
shift || true
cd "$ROOT"

case "$cmd" in
  up)
    need_docker
    expose=0
    for a in "$@"; do [[ "$a" == "--expose" ]] && expose=1; done
    if [[ "$expose" == 1 ]]; then
      [[ -n "${MAILROOM_API_TOKEN:-}" ]] || fail "--expose needs MAILROOM_API_TOKEN (the sandbox refuses an unauthenticated off-loopback bind)"
    fi
    export APP_UID="$(id -u)"
    [[ "$APP_UID" -gt 0 ]] || fail "run as a non-root host user"
    if [[ "$expose" == 1 ]]; then
      export SANDBOX_BIND="${SANDBOX_BIND:-0.0.0.0}" SANDBOX_ALLOW_UNAUTH=0
      echo "WARNING: exposing on ${SANDBOX_BIND}:${PORT}; put TLS in front (reverse proxy) before using a public network." >&2
    fi
    docker compose -f "$COMPOSE_FILE" up -d --build
    echo
    echo "mailroom sandbox is starting (offline: mock LLM, rule-based Correspondent stand-in, mail captured only):"
    echo "  UI   ${BASE}/ui"
    echo "  API  ${BASE}/api/sandbox/v1/status"
    echo "Run 'scripts/sandbox.sh status' once healthy."
    ;;
  down)
    need_docker
    docker compose -f "$COMPOSE_FILE" down "$@"
    ;;
  logs)
    need_docker
    docker compose -f "$COMPOSE_FILE" logs -f --tail=200 "$@"
    ;;
  reset)
    need_docker
    docker compose -f "$COMPOSE_FILE" down -v --remove-orphans "$@"
    echo "sandbox container and state volume removed."
    ;;
  status)
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/health" 2>/dev/null || true)"
    if [[ "$code" =~ ^2 ]]; then
      echo "OK    health $BASE/health -> $code"
      curl -fsS --max-time 5 -H "$(auth_header)" "$BASE/api/sandbox/v1/status" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print("      content", d["content"]["kind"], d["content"]["version"], "| messages", d["messages"], "| transmitted mail", d["egress"]["transmitted"], "| guard blocked", d["network_guard"]["blocked_attempts"])' \
        || echo "      (status needs MAILROOM_API_TOKEN)"
    else
      echo "DOWN  health $BASE/health -> ${code:-no response}"
      exit 1
    fi
    ;;
  run)
    command -v uv >/dev/null 2>&1 || fail "uv is required for host mode (https://docs.astral.sh/uv/)"
    state="${SANDBOX_STATE_DIR:-$ROOT/.sandbox-state}"
    host="${SANDBOX_HOST:-127.0.0.1}"
    export OTEL_SDK_DISABLED=true
    exec uv run --extra sandbox mailroom sandbox serve --host "$host" --port "$PORT" \
      --content "${SANDBOX_CONTENT:-smoke}" --data-dir "$state" --egress "${SANDBOX_EGRESS:-closed}" "$@"
    ;;
  smoke)
    H="$(auth_header)"
    echo "injecting A1 + E1 ..."
    curl -fsS -H "$H" -H 'Content-Type: application/json' -X POST "$BASE/api/sandbox/v1/inject" \
      -d '{"scenario_ids":["A1_status_inquiry","E1_lookalike_wire_change"],"wait":true}' >/dev/null || fail "inject failed"
    curl -fsS -H "$H" "$BASE/api/sandbox/v1/conformance" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for r in d["results"]:
    print("%-30s %s (pass %d, fail %d, n/a %d)" % (r["scenario"], r["verdict"], r["passed"], r["failed"], r["unchecked"]))
sys.exit(1 if d["fail"] else 0)'
    echo "SANDBOX SMOKE OK"
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "sandbox.sh: unknown command '$cmd'" >&2
    usage >&2
    exit 2
    ;;
esac
