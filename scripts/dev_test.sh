#!/usr/bin/env bash
# Dev test suite for mailroom-reloaded (plan Task: DEV SERVER).
#
#   scripts/dev_test.sh                 # sync dev deps, ruff, unit tests (live deselected)
#   scripts/dev_test.sh --live          # ... and also run the `live` end-to-end tests
#   scripts/dev_test.sh --no-lint       # skip ruff check
#   scripts/dev_test.sh -k watcher      # extra args are passed through to pytest
#
# Steps:
#   1. uv sync --extra dev     (the ONLY step that may reach the network)
#   2. uv run ruff check .     (vendored scoring files are excluded in pyproject)
#   3. uv run pytest ...       (live marker is deselected via addopts)
#
# Live tests need a running `scripts/dev.sh up` stack and/or a live provider;
# they are off by default so CI and a plain checkout stay hermetic.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "dev_test.sh: 'uv' is required but was not found on PATH." >&2
  echo "Install it from https://docs.astral.sh/uv/ and retry." >&2
  exit 127
fi

usage() {
  cat <<'EOF'
Usage: scripts/dev_test.sh [--live] [--no-lint] [-- <pytest args>]

  --live      include tests marked `live` (needs a running dev stack)
  --no-lint   skip the ruff check step
  --          pass everything after it through to pytest

Default: uv sync --extra dev, ruff check, pytest (live deselected).
EOF
}

live=0
lint=1
pytest_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --live) live=1 ;;
    --no-lint) lint=0 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; pytest_args+=("$@"); break ;;
    *) pytest_args+=("$1") ;;
  esac
  shift
done

echo "==> uv sync --extra dev"
uv sync --extra dev

if [[ "$lint" -eq 1 ]]; then
  echo "==> uv run ruff check ."
  uv run ruff check .
fi

if [[ "$live" -eq 1 ]]; then
  echo "==> uv run pytest -m live (live end-to-end selected)"
  uv run pytest -m live "${pytest_args[@]+"${pytest_args[@]}"}"
else
  echo "==> uv run pytest (live deselected by pyproject addopts)"
  uv run pytest "${pytest_args[@]+"${pytest_args[@]}"}"
fi
