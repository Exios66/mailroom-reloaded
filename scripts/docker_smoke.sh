#!/bin/bash
# mailroom-reloaded Docker smoke test: build, start, and validate images & configs.
#
# Usage: scripts/docker_smoke.sh [--no-build] [--keep] [--only app|sandbox|compose]
#
# Options:
#   --no-build        Skip the build step (assume images exist).
#   --keep            Keep containers and logs on exit (for debugging).
#   --only app|sandbox|compose  Run only one category of tests.
#
# Exit codes:
#   0  Success
#   1  Test failure
#   2  Prerequisite missing (no docker, daemon unreachable, registry unreachable)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKER_SMOKE_LOG_DIR="${DOCKER_SMOKE_LOG_DIR:-/tmp/mailroom_smoke_$$}"
APP_IMAGE="mrl-smoke:app"
SANDBOX_IMAGE="mrl-smoke:sandbox"
APP_CONTAINER="mrl-smoke-app"
SANDBOX_CONTAINER="mrl-smoke-sandbox"
APP_VOLUME="mrl-smoke-data"
APP_PORT="${DOCKER_SMOKE_APP_PORT:-18001}"
SANDBOX_PORT="${DOCKER_SMOKE_SANDBOX_PORT:-18100}"

NO_BUILD=0
KEEP=0
ONLY_TESTS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build) NO_BUILD=1; shift ;;
    --keep) KEEP=1; shift ;;
    --only)
      case "${2:-}" in
        app|sandbox|compose) ONLY_TESTS="$2"; shift 2 ;;
        *) echo "Error: --only must be app|sandbox|compose" >&2; exit 1 ;;
      esac
      ;;
    *) echo "Error: unknown option $1" >&2; exit 1 ;;
  esac
done

cleanup() {
  local exit_code=$?
  if [[ $KEEP -eq 0 ]]; then
    docker stop "$APP_CONTAINER" "$SANDBOX_CONTAINER" 2>/dev/null || true
    docker rm "$APP_CONTAINER" "$SANDBOX_CONTAINER" 2>/dev/null || true
    docker volume rm "$APP_VOLUME" 2>/dev/null || true
    docker rmi "$APP_IMAGE" "$SANDBOX_IMAGE" 2>/dev/null || true
    rm -rf "$DOCKER_SMOKE_LOG_DIR"
  fi
  return $exit_code
}

trap cleanup EXIT

# Check docker
if ! command -v docker &>/dev/null || ! docker info &>/dev/null; then
  echo "Docker daemon unreachable" >&2
  exit 2
fi

echo "mailroom-reloaded Docker smoke test"
echo "===================================="
echo "Checking registry access..."

if ! docker pull ghcr.io/astral-sh/uv:0.8.22 &>/dev/null; then
  echo "Registry unreachable" >&2
  exit 2
fi

mkdir -p "$DOCKER_SMOKE_LOG_DIR"

# TEST: app image
if [[ -z "$ONLY_TESTS" ]] || [[ "$ONLY_TESTS" == "app" ]]; then
  echo "--- Testing app image ---"

  if [[ $NO_BUILD -eq 0 ]]; then
    echo "Building app image..."
    if ! docker build -f "$REPO_ROOT/deploy/Dockerfile" \
        --build-arg ML_BUILD_NONE=1 --build-arg UV_EXTRAS= \
        -t "$APP_IMAGE" "$REPO_ROOT"; then
      echo "FAIL: app image build" >&2
      exit 1
    fi
  fi

  docker volume rm "$APP_VOLUME" 2>/dev/null || true
  docker volume create "$APP_VOLUME" &>/dev/null

  echo "Testing Python imports..."
  if ! docker run --rm "$APP_IMAGE" python -c "import mailroom_reloaded"; then
    echo "FAIL: mailroom_reloaded import" >&2
    exit 1
  fi

  echo "Testing UID..."
  uid=$(docker run --rm "$APP_IMAGE" id -u)
  if [[ "$uid" != "10001" ]]; then
    echo "FAIL: expected uid 10001, got $uid" >&2
    exit 1
  fi

  echo "Testing /data writability..."
  if ! docker run --rm -v "$APP_VOLUME:/data" "$APP_IMAGE" touch /data/test; then
    echo "FAIL: /data not writable" >&2
    exit 1
  fi

  echo "Starting app with token..."
  docker rm "$APP_CONTAINER" 2>/dev/null || true
  docker run -d --name "$APP_CONTAINER" \
    --health-cmd="python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)\"" \
    --health-interval=5s --health-timeout=3s --health-start-period=10s --health-retries=3 \
    -p "127.0.0.1:$APP_PORT:8000" -v "$APP_VOLUME:/data" \
    -e "MAILROOM_API_TOKEN=test_smoke" "$APP_IMAGE" &>/dev/null

  echo "Waiting for app health (90s timeout)..."
  for i in {1..90}; do
    if docker ps --filter "name=^$APP_CONTAINER$" --filter "health=healthy" --format "{{.ID}}" &>/dev/null; then
      break
    fi
    sleep 1
    if [[ $i -eq 90 ]]; then
      docker logs "$APP_CONTAINER" > "$DOCKER_SMOKE_LOG_DIR/app.log" 2>&1
      echo "FAIL: app health timeout" >&2
      tail -50 "$DOCKER_SMOKE_LOG_DIR/app.log"
      exit 1
    fi
  done

  echo "Testing /health endpoint..."
  if ! curl -sf "http://127.0.0.1:$APP_PORT/health" &>/dev/null; then
    docker logs "$APP_CONTAINER" | tail -50
    echo "FAIL: /health failed" >&2
    exit 1
  fi

  echo "Testing rejection without token..."
  docker rm "$APP_CONTAINER" 2>/dev/null || true
  if ! docker run --rm -p "127.0.0.1:$APP_PORT:8000" -v "$APP_VOLUME:/data" \
      "$APP_IMAGE" mailroom serve --host 0.0.0.0 --port 8000 2>&1 | grep -q "Refusing to bind"; then
    echo "FAIL: did not reject missing token" >&2
    exit 1
  fi

  echo "Testing uvicorn without token..."
  if ! docker run --rm "$APP_IMAGE" \
      python -m uvicorn mailroom_reloaded.api.app:app --host 0.0.0.0 2>&1 | grep -q "Refusing to bind"; then
    echo "FAIL: uvicorn did not reject missing token" >&2
    exit 1
  fi

  echo "✓ app image passed"
fi

# TEST: sandbox image
if [[ -z "$ONLY_TESTS" ]] || [[ "$ONLY_TESTS" == "sandbox" ]]; then
  echo "--- Testing sandbox image ---"

  if [[ $NO_BUILD -eq 0 ]]; then
    echo "Building sandbox image..."
    if ! docker build -f "$REPO_ROOT/deploy/Dockerfile.sandbox" \
        -t "$SANDBOX_IMAGE" "$REPO_ROOT"; then
      echo "FAIL: sandbox build" >&2
      exit 1
    fi
  fi

  echo "Starting sandbox..."
  docker rm "$SANDBOX_CONTAINER" 2>/dev/null || true
  docker run -d --name "$SANDBOX_CONTAINER" \
    --health-cmd="python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=3)\"" \
    --health-interval=5s --health-timeout=3s --health-start-period=10s --health-retries=5 \
    -p "127.0.0.1:$SANDBOX_PORT:8100" \
    -e "MAILROOM_API_TOKEN=" -e "MAILROOM_ALLOW_UNAUTHENTICATED_BIND=1" \
    "$SANDBOX_IMAGE" &>/dev/null

  echo "Waiting for sandbox health (90s timeout)..."
  for i in {1..90}; do
    if docker ps --filter "name=^$SANDBOX_CONTAINER$" --filter "health=healthy" --format "{{.ID}}" &>/dev/null; then
      break
    fi
    sleep 1
    if [[ $i -eq 90 ]]; then
      docker logs "$SANDBOX_CONTAINER" > "$DOCKER_SMOKE_LOG_DIR/sandbox.log" 2>&1
      echo "FAIL: sandbox health timeout" >&2
      tail -50 "$DOCKER_SMOKE_LOG_DIR/sandbox.log"
      exit 1
    fi
  done

  echo "Testing /health..."
  if ! curl -sf "http://127.0.0.1:$SANDBOX_PORT/health" &>/dev/null; then
    docker logs "$SANDBOX_CONTAINER" | tail -50
    echo "FAIL: /health" >&2
    exit 1
  fi

  echo "Testing /ui..."
  if ! curl -sf "http://127.0.0.1:$SANDBOX_PORT/ui" &>/dev/null; then
    echo "FAIL: /ui" >&2
    exit 1
  fi

  echo "Testing /ui/route.js..."
  if ! curl -sf "http://127.0.0.1:$SANDBOX_PORT/ui/route.js" &>/dev/null; then
    echo "FAIL: /ui/route.js" >&2
    exit 1
  fi

  echo "Testing CSP header..."
  csp=$(curl -sf -I "http://127.0.0.1:$SANDBOX_PORT/ui" 2>/dev/null | grep -i "^content-security-policy:" | tr -d '\r' || true)
  if [[ "$csp" != "Content-Security-Policy: default-src 'self'" ]]; then
    echo "FAIL: CSP header wrong: '$csp'" >&2
    exit 1
  fi

  echo "Testing no ACAO header..."
  if curl -sf -I "http://127.0.0.1:$SANDBOX_PORT/ui" 2>/dev/null | grep -qi "^access-control-allow-origin:"; then
    echo "FAIL: ACAO header present" >&2
    exit 1
  fi

  echo "✓ sandbox passed"
fi

# TEST: compose configs
if [[ -z "$ONLY_TESTS" ]] || [[ "$ONLY_TESTS" == "compose" ]]; then
  echo "--- Testing compose configs ---"

  echo "Validating docker-compose.yml..."
  if ! env MAILROOM_API_TOKEN=test GRAFANA_ADMIN_PASSWORD=test \
      docker compose -f "$REPO_ROOT/deploy/docker-compose.yml" config -q 2>/dev/null; then
    echo "FAIL: docker-compose.yml" >&2
    exit 1
  fi

  if [[ -f "$REPO_ROOT/deploy/docker-compose.dev.yml" ]]; then
    echo "Validating docker-compose.dev.yml..."
    if ! docker compose -f "$REPO_ROOT/deploy/docker-compose.dev.yml" config -q 2>/dev/null; then
      echo "FAIL: docker-compose.dev.yml" >&2
      exit 1
    fi
  fi

  echo "Validating docker-compose.sandbox.yml..."
  if ! docker compose -f "$REPO_ROOT/deploy/docker-compose.sandbox.yml" config -q 2>/dev/null; then
    echo "FAIL: docker-compose.sandbox.yml" >&2
    exit 1
  fi

  echo "Testing env var forwarding..."
  config=$(env MAILROOM_API_TOKEN=test GRAFANA_ADMIN_PASSWORD=test \
      MOCK_BASE_URL="http://mock:8000" MAILROOM_ANCHOR="http://anchor" \
      docker compose -f "$REPO_ROOT/deploy/docker-compose.yml" config 2>/dev/null)

  if ! echo "$config" | grep -q "MOCK_BASE_URL: http://mock:8000"; then
    echo "FAIL: MOCK_BASE_URL not forwarded" >&2
    exit 1
  fi

  if ! echo "$config" | grep -q "MAILROOM_ANCHOR: http://anchor"; then
    echo "FAIL: MAILROOM_ANCHOR not forwarded" >&2
    exit 1
  fi

  config_unset=$(env MAILROOM_API_TOKEN=test GRAFANA_ADMIN_PASSWORD=test \
      docker compose -f "$REPO_ROOT/deploy/docker-compose.yml" config 2>/dev/null)

  if echo "$config_unset" | grep -q "MOCK_BASE_URL: ''"; then
    echo "FAIL: empty MOCK_BASE_URL should be absent" >&2
    exit 1
  fi

  echo "✓ compose passed"
fi

echo ""
echo "DOCKER SMOKE OK"
