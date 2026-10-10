#!/usr/bin/env bash
# Docker smoke test (issue #84): build the lean app image and the sandbox image,
# run them, and check the compose files statically. Prints "DOCKER SMOKE OK".
#
#   scripts/docker_smoke.sh [--no-build] [--keep] [--only app|sandbox|compose]
#
# Exit codes: 0 ok, 1 a check failed, 2 a prerequisite is missing (no docker CLI,
# daemon unreachable, registry unreachable) so a restricted host reports "skipped".
#
# Everything it creates is named mrl-smoke* and removed on exit (images too, unless
# --no-build or --keep). Env: DOCKER_SMOKE_APP_PORT (18001),
# DOCKER_SMOKE_SANDBOX_PORT (18100), DOCKER_SMOKE_LOG_DIR (container logs on
# failure; kept), DOCKER_SMOKE_APP_DOCKERFILE (default deploy/Dockerfile; used to
# prove the script fails on a broken Dockerfile).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_IMAGE=mrl-smoke:app
SANDBOX_IMAGE=mrl-smoke:sandbox
APP_CTR=mrl-smoke-app
SANDBOX_CTR=mrl-smoke-sandbox
APP_VOL=mrl-smoke-data
APP_PORT="${DOCKER_SMOKE_APP_PORT:-18001}"
SANDBOX_PORT="${DOCKER_SMOKE_SANDBOX_PORT:-18100}"
APP_DOCKERFILE="${DOCKER_SMOKE_APP_DOCKERFILE:-$ROOT/deploy/Dockerfile}"
LOG_DIR="${DOCKER_SMOKE_LOG_DIR:-}"
CSP_EXPECTED="default-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
TOKEN=smoke-token

NO_BUILD=0
KEEP=0
ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build) NO_BUILD=1; shift ;;
    --keep) KEEP=1; shift ;;
    --only)
      case "${2:-}" in
        app|sandbox|compose) ONLY="$2"; shift 2 ;;
        *) echo "docker_smoke: --only takes app|sandbox|compose" >&2; exit 2 ;;
      esac ;;
    *) echo "docker_smoke: unknown option $1" >&2; exit 2 ;;
  esac
done

want() { [[ -z "$ONLY" || "$ONLY" == "$1" ]]; }

cleanup() {
  local rc=$?
  if [[ $KEEP -eq 0 ]]; then
    docker rm -f "$APP_CTR" "$SANDBOX_CTR" >/dev/null 2>&1 || true
    docker volume rm "$APP_VOL" >/dev/null 2>&1 || true
    if [[ $NO_BUILD -eq 0 ]]; then
      docker image rm "$APP_IMAGE" "$SANDBOX_IMAGE" >/dev/null 2>&1 || true
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT

fail() {
  # fail <step> [container]: report, keep the container log, exit 1.
  echo "DOCKER SMOKE FAIL: $1" >&2
  if [[ -n "${2:-}" ]] && docker inspect "$2" >/dev/null 2>&1; then
    if [[ -n "$LOG_DIR" ]]; then
      mkdir -p "$LOG_DIR"
      docker logs "$2" >"$LOG_DIR/$2.log" 2>&1 || true
    fi
    echo "--- last 50 log lines of $2 ---" >&2
    docker logs --tail 50 "$2" >&2 2>&1 || true
  fi
  exit 1
}

wait_healthy() {
  # wait_healthy <container> <seconds>
  local status=""
  local deadline=$((SECONDS + $2))
  while (( SECONDS < deadline )); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$1" 2>/dev/null || echo missing)"
    [[ "$status" == healthy ]] && return 0
    [[ "$status" == unhealthy || "$status" == missing ]] && break
    sleep 2
  done
  fail "$1 not healthy (last status: ${status:-none})" "$1"
}

echo "== prerequisites"
command -v docker >/dev/null 2>&1 || { echo "docker_smoke: docker CLI not found" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker_smoke: Docker daemon unreachable" >&2; exit 2; }
if [[ $NO_BUILD -eq 0 ]] && { want app || want sandbox; }; then
  for ref in ghcr.io/astral-sh/uv:0.8.22 python:3.11-slim-bookworm; do
    docker pull -q "$ref" >/dev/null 2>&1 || { echo "docker_smoke: registry unreachable ($ref)" >&2; exit 2; }
  done
fi

if want app; then
  echo "== app image"
  if [[ $NO_BUILD -eq 0 ]]; then
    docker build -q -f "$APP_DOCKERFILE" --build-arg ML_BUILD_NONE=1 --build-arg UV_EXTRAS= \
      -t "$APP_IMAGE" "$ROOT" >/dev/null || fail "app image build ($APP_DOCKERFILE)"
  fi
  path="$(docker run --rm "$APP_IMAGE" python -c 'import mailroom_reloaded; print(mailroom_reloaded.__file__)')" \
    || fail "import mailroom_reloaded"
  [[ "$path" == /opt/venv/* ]] || fail "package resolves outside /opt/venv: $path"
  [[ "$(docker run --rm "$APP_IMAGE" id -u)" == 10001 ]] || fail "image does not run as uid 10001"
  docker volume rm "$APP_VOL" >/dev/null 2>&1 || true
  docker run --rm -v "$APP_VOL:/data" "$APP_IMAGE" sh -c 'touch /data/.w && rm /data/.w' \
    || fail "/data not writable on a fresh named volume"

  docker run -d --name "$APP_CTR" -p "127.0.0.1:$APP_PORT:8000" -e "MAILROOM_API_TOKEN=$TOKEN" \
    -v "$APP_VOL:/data" "$APP_IMAGE" >/dev/null
  wait_healthy "$APP_CTR" 120   # the image's own HEALTHCHECK (start period 40s)
  curl -fsS "http://127.0.0.1:$APP_PORT/health" >/dev/null || fail "GET /health" "$APP_CTR"
  docker rm -f "$APP_CTR" >/dev/null

  out="$(docker run --rm "$APP_IMAGE" 2>&1 || true)"
  grep -q "Refusing to bind" <<<"$out" || fail "CMD without a token did not refuse to bind"
  out="$(docker run --rm --entrypoint python "$APP_IMAGE" -m uvicorn mailroom_reloaded.api.app:app \
    --host 0.0.0.0 --port 8000 2>&1 || true)"
  grep -q "Refusing to bind" <<<"$out" || fail "uvicorn 0.0.0.0 without a token did not refuse to bind"
  echo "app image ok"
fi

if want sandbox; then
  echo "== sandbox image"
  if [[ $NO_BUILD -eq 0 ]]; then
    docker build -q -f "$ROOT/deploy/Dockerfile.sandbox" -t "$SANDBOX_IMAGE" "$ROOT" >/dev/null \
      || fail "sandbox image build"
  fi
  # Same hardening and loopback-only publish as deploy/docker-compose.sandbox.yml.
  docker run -d --name "$SANDBOX_CTR" -p "127.0.0.1:$SANDBOX_PORT:8100" \
    -e MAILROOM_ALLOW_UNAUTHENTICATED_BIND=1 -e OTEL_SDK_DISABLED=true \
    --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /tmp \
    --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=3)\"" \
    --health-interval 3s --health-start-period 10s --health-retries 10 \
    "$SANDBOX_IMAGE" >/dev/null
  wait_healthy "$SANDBOX_CTR" 90
  base="http://127.0.0.1:$SANDBOX_PORT"
  for p in /health /ui /ui/route.js; do
    curl -fsS -o /dev/null "$base$p" || fail "GET $p" "$SANDBOX_CTR"
  done
  headers="$(curl -fsS -D - -o /dev/null "$base/ui" | tr -d '\r')"
  csp="$(sed -n 's/^[Cc]ontent-[Ss]ecurity-[Pp]olicy: //p' <<<"$headers")"
  [[ "$csp" == "$CSP_EXPECTED" ]] || fail "CSP is '$csp'"
  if grep -qi '^access-control-allow-origin:' <<<"$headers"; then fail "CORS header present"; fi
  echo "sandbox image ok"
fi

if want compose; then
  echo "== compose (static)"
  dc() { docker compose -p mrl-smoke "$@"; }
  MAILROOM_API_TOKEN=t GRAFANA_ADMIN_PASSWORD=g dc -f "$ROOT/deploy/docker-compose.yml" config -q \
    || fail "docker-compose.yml config"
  for profile in mock local-llm gpu split-watcher; do
    MAILROOM_API_TOKEN=t GRAFANA_ADMIN_PASSWORD=g \
      dc -f "$ROOT/deploy/docker-compose.yml" --profile "$profile" config -q \
      || fail "docker-compose.yml --profile $profile config"
  done
  dc -f "$ROOT/deploy/docker-compose.dev.yml" config -q || fail "docker-compose.dev.yml config"
  dc -f "$ROOT/deploy/docker-compose.sandbox.yml" config -q || fail "docker-compose.sandbox.yml config"
  set_env="$(MAILROOM_API_TOKEN=t GRAFANA_ADMIN_PASSWORD=g MOCK_BASE_URL=http://mock:8000/v1 \
    MAILROOM_ANCHOR=file dc -f "$ROOT/deploy/docker-compose.yml" config --format json \
    | python3 -c 'import json,sys; e=json.load(sys.stdin)["services"]["app"]["environment"]; print(e.get("MOCK_BASE_URL"), e.get("MAILROOM_ANCHOR"))')"
  [[ "$set_env" == "http://mock:8000/v1 file" ]] || fail "set passthrough vars not forwarded: $set_env"
  unset_env="$(env -u MOCK_BASE_URL -u MAILROOM_ANCHOR MAILROOM_API_TOKEN=t GRAFANA_ADMIN_PASSWORD=g \
    docker compose -p mrl-smoke -f "$ROOT/deploy/docker-compose.yml" config --format json \
    | python3 -c 'import json,sys; e=json.load(sys.stdin)["services"]["app"]["environment"]; print(e.get("MOCK_BASE_URL"), e.get("MAILROOM_ANCHOR"))')"
  # An unset passthrough renders as a null value, which compose does not pass to the container.
  [[ "$unset_env" == "None None" ]] || fail "unset passthrough vars got values: $unset_env"
  echo "compose ok"
fi

echo "DOCKER SMOKE OK"
