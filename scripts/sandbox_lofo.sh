#!/usr/bin/env bash
# Leave-one-family-out conformance report for the Correspondent stand-in (offline, local only).
#
#   scripts/sandbox_lofo.sh /path/to/mailroom-sandbox-content [out_dir]
#
# Runs every scenario alone (mailroom sandbox conformance), prints the per-scenario table and the
# per-fold report, and writes <out_dir>/conformance.json and <out_dir>/lofo.json (default
# tests/sandbox/). Protocol: lexicons and weights may be changed only while looking at the training
# families of a fold; the held-out family's pass rate is that fold's number.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTENT="${1:?usage: sandbox_lofo.sh <content dir> [out_dir]}"
OUT="${2:-$ROOT/tests/sandbox}"
export OTEL_SDK_DISABLED=true
cd "$ROOT"
mkdir -p "$OUT"
STATE="$(mktemp -d "${TMPDIR:-/tmp}/sandbox-lofo-state.XXXXXX")"
trap 'rm -rf "$STATE"' EXIT
uv run --offline --extra sandbox mailroom sandbox conformance --content "$CONTENT" \
  --data-dir "$STATE" --json "$OUT/conformance_baseline.json" --slim --lofo "$OUT/lofo_baseline.json"
