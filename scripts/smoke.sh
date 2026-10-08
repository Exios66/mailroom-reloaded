#!/usr/bin/env bash
# Smoke test for the compose stack. Run after:
#   docker compose -f deploy/docker-compose.yml --profile local-llm up -d --build
# Drops a fixture into the inbox, polls /v1/documents until it is archived,
# then checks Phoenix and Grafana. Prints "SMOKE OK" on success.
set -euo pipefail

API="${MAILROOM_API_URL:-http://localhost:8000}"
PHOENIX="${PHOENIX_URL:-http://localhost:6006}"
GRAFANA="${GRAFANA_URL:-http://localhost:3000}"
FIXTURE="${SMOKE_FIXTURE:-tests/ingest/fixtures/letter.txt}"
COMPOSE=(docker compose -f deploy/docker-compose.yml)
TIMEOUT="${SMOKE_TIMEOUT:-300}"
AUTH=()
if [[ -n "${MAILROOM_API_TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer ${MAILROOM_API_TOKEN}")
fi

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

[[ -f "$FIXTURE" ]] || fail "fixture not found: $FIXTURE"
name="smoke-$(date +%s)-$(basename "$FIXTURE")"

# Copy into the app container's inbox (volume mailroom_data at /data).
"${COMPOSE[@]}" cp "$FIXTURE" "app:/data/inbox/$name" || fail "could not copy fixture into inbox"

deadline=$((SECONDS + TIMEOUT))
status=""
while (( SECONDS < deadline )); do
  body="$(curl -fsS "${AUTH[@]}" "$API/v1/documents" || true)"
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
[[ "$status" == "archived" ]] || fail "document not archived within ${TIMEOUT}s (last status: '${status}')"

code="$(curl -s -o /dev/null -w '%{http_code}' "$PHOENIX")"
[[ "$code" == "200" ]] || fail "phoenix returned $code"

curl -fsS "$GRAFANA/api/health" | grep -q '"database": *"ok"' || fail "grafana health not ok"

echo "SMOKE OK"
