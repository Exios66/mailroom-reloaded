#!/usr/bin/env bash
# Local DEV server helper for mailroom-reloaded (deploy/docker-compose.dev.yml).
#
#   scripts/dev.sh up        build + start the full dev stack (app, watcher, observability)
#   scripts/dev.sh down      stop and remove the dev containers/network
#   scripts/dev.sh logs      follow the dev stack logs
#   scripts/dev.sh ps        show dev stack container status
#   scripts/dev.sh reset     stop everything and delete ./data and the dev volumes
#   scripts/dev.sh status    curl /health, Phoenix, Grafana and Prometheus
#   scripts/dev.sh smoke     upload a fixture to POST /v1/documents and wait for "archived"
#
# The stack uses the `mock` provider by default and never requires a GPU.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/docker-compose.dev.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "dev.sh: docker is required but was not found on PATH." >&2
  echo "Install Docker Desktop (or the engine + compose plugin) and retry." >&2
  exit 127
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "dev.sh: the 'docker compose' plugin is required but is not available." >&2
  exit 127
fi

cd "$ROOT"

GRAFANA_PASSWORD_LABEL="${GRAFANA_ADMIN_PASSWORD:+<configured>}"
GRAFANA_PASSWORD_LABEL="${GRAFANA_PASSWORD_LABEL:-admin (default)}"
COMPOSE=(docker compose -f "$COMPOSE_FILE")
if [[ -f "$ROOT/.env" ]]; then
  settings_file="$(mktemp)"
  if ! python3 "$ROOT/scripts/dev_env.py" "$ROOT/.env" > "$settings_file"; then
    rm -f "$settings_file"
    exit 1
  fi
  while IFS= read -r -d '' key && IFS= read -r -d '' value; do
    export "$key=$value"
  done < "$settings_file"
  rm -f "$settings_file"
  COMPOSE+=(--env-file "$ROOT/.env")
fi

# Match the image user to the host owner of the bind-mounted source and state.
export APP_UID="$(id -u)"

API="${MAILROOM_API_URL:-http://localhost:8000}"
PHOENIX="${PHOENIX_URL:-http://localhost:6006}"
PROMETHEUS="${PROMETHEUS_URL:-http://localhost:9090}"
GRAFANA="${GRAFANA_URL:-http://localhost:3000}"
TIMEOUT="${SMOKE_TIMEOUT:-300}"

usage() {
  cat <<'EOF'
Usage: scripts/dev.sh <command>

Commands:
  up        build + start the full dev stack (app, watcher, observability)
  down      stop and remove the dev containers/network
  logs      follow the dev stack logs
  ps        show dev stack container status
  reset     stop everything and delete ./data and the dev volumes
  status    curl /health, Phoenix, Grafana and Prometheus
  smoke     upload a fixture to POST /v1/documents and wait for "archived"
EOF
}

fail() { echo "dev.sh: $*" >&2; exit 1; }

http_code() { curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$1" 2>/dev/null || true; }

cmd="${1:-}"
shift || true

case "$cmd" in
  up)
    [[ "$APP_UID" -gt 0 ]] || fail "run dev.sh as a non-root host user"
    mkdir -p "$ROOT/data"
    "${COMPOSE[@]}" up -d --build "$@"
    echo
    echo "mailroom dev stack is starting (mock provider, no GPU):"
    echo "  API /ui    ${API}"
    echo "  Phoenix    ${PHOENIX}"
    echo "  Prometheus ${PROMETHEUS}"
    echo "  Grafana    ${GRAFANA}  (admin / ${GRAFANA_PASSWORD_LABEL})"
    echo "  data       ${ROOT}/data"
    echo "Run 'scripts/dev.sh status' once healthy."
    ;;
  down)
    "${COMPOSE[@]}" down "$@"
    ;;
  logs)
    "${COMPOSE[@]}" logs -f --tail=200 "$@"
    ;;
  ps)
    "${COMPOSE[@]}" ps "$@"
    ;;
  reset)
    "${COMPOSE[@]}" down -v --remove-orphans "$@"
    rm -rf "$ROOT/data"
    echo "dev stack and ./data removed."
    ;;
  status)
    rc=0
    check() { # label url
      local code
      code="$(http_code "$2")"
      if [[ "$code" =~ ^2 ]]; then
        printf 'OK    %-11s %s -> %s\n' "$1" "$2" "$code"
      else
        printf 'DOWN  %-11s %s -> %s\n' "$1" "$2" "${code:-no response}"
        rc=1
      fi
    }
    check app "$API/health"
    check phoenix "$PHOENIX/"
    check prometheus "$PROMETHEUS/-/healthy"
    check grafana "$GRAFANA/api/health"
    exit "$rc"
    ;;
  smoke)
    FIXTURE="${SMOKE_FIXTURE:-$ROOT/tests/ingest/fixtures/letter.txt}"
    [[ -f "$FIXTURE" ]] || fail "fixture not found: $FIXTURE"
    name="dev-smoke-$(date +%s)-$(basename "$FIXTURE")"
    AUTH=()
    if [[ -n "${MAILROOM_API_TOKEN:-}" ]]; then
      AUTH=(-H "Authorization: Bearer ${MAILROOM_API_TOKEN}")
    fi

    echo "Uploading $FIXTURE to $API/v1/documents ..."
    resp="$(curl -fsS ${AUTH[@]+"${AUTH[@]}"} \
      -F "file=@${FIXTURE};filename=${name}" "$API/v1/documents")" \
      || fail "POST /v1/documents failed"
    doc_id="$(printf '%s' "$resp" | python3 -c '
import json, sys
print(json.load(sys.stdin).get("doc_id", ""))
')"
    echo "accepted doc_id=${doc_id} name=${name}"

    [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || fail "SMOKE_TIMEOUT must be a number of seconds"
    deadline=$((SECONDS + 10#$TIMEOUT))
    status=""
    while (( SECONDS < deadline )); do
      body="$(curl -fsS ${AUTH[@]+"${AUTH[@]}"} "$API/v1/documents" || true)"
      status="$(printf '%s' "$body" | python3 -c '
import json, sys
name = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
docs = data.get("documents", data) if isinstance(data, dict) else data
for d in docs:
    if name in str(d.get("filename", d.get("name", ""))):
        print(d.get("status", "")); break
' "$name")"
      [[ "$status" == "archived" ]] && break
      [[ "$status" == "failed" ]] && fail "document failed"
      sleep 3
    done
    [[ "$status" == "archived" ]] \
      || fail "document not archived within ${TIMEOUT}s (last status: '${status}')"

    code="$(http_code "$PHOENIX/")"
    [[ "$code" == "200" ]] || fail "phoenix returned ${code:-no response}"
    curl -fsS "$GRAFANA/api/health" | grep -q '"database": *"ok"' \
      || fail "grafana health not ok"
    echo "DEV SMOKE OK"
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "dev.sh: unknown command '$cmd'" >&2
    usage >&2
    exit 2
    ;;
esac
