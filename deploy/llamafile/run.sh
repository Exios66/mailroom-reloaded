#!/bin/sh
# llamafile sidecar entrypoint: OpenAI-compatible server configured from env.
#   LLAMAFILE_MODEL  GGUF path inside the container (required)
#   LLAMAFILE_ALIAS  served model id (default qwen3:7b)
#   LLAMAFILE_CTX    context size in tokens (default 16384)
#   LLAMAFILE_GPU    auto|nvidia|amd|apple|vulkan|disable (default disable)
set -e

: "${LLAMAFILE_MODEL:?set LLAMAFILE_MODEL (GGUF path inside the container)}"

exec /llamafile/llamafile \
  --server \
  --host 0.0.0.0 \
  --port 8080 \
  -m "$LLAMAFILE_MODEL" \
  -a "${LLAMAFILE_ALIAS:-qwen3:7b}" \
  --jinja \
  --ctx-size "${LLAMAFILE_CTX:-16384}" \
  --no-webui \
  -np 1 \
  --gpu "${LLAMAFILE_GPU:-disable}"
