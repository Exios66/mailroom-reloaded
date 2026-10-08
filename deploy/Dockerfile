# syntax=docker/dockerfile:1
# mailroom-reloaded runtime image: multi-stage, uv-installed, non-root (uid 10001).
#
#   Lean (OpenRouter / vLLM only): docker build -f deploy/Dockerfile --build-arg ML_BUILD_NONE=1 --build-arg UV_EXTRAS= .
#   Default (ModernBERT bundle):   docker build -f deploy/Dockerfile .
# Build context is the repository root.

ARG PYTHON_IMAGE=python:3.11-slim-bookworm
ARG UV_EXTRAS="--extra bert"
ARG ML_MODEL_REPO=Lucius-Morningstar/mailroom-modernbert-classifier
ARG ML_MODEL_REVISION=main
ARG ML_BUILD_NONE=0

FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG UV_EXTRAS
# Dependencies first (cache-friendly), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project ${UV_EXTRAS}
COPY src ./src
RUN uv sync --frozen --no-dev ${UV_EXTRAS}


# --- Model stage: pinned ModernBERT bundle; ML_BUILD_NONE=1 stages an empty dir.
FROM ${PYTHON_IMAGE} AS model
WORKDIR /ml
ARG ML_MODEL_REPO
ARG ML_MODEL_REVISION
ARG ML_BUILD_NONE
RUN pip install --no-cache-dir "huggingface_hub>=0.30" \
    && if [ "$ML_BUILD_NONE" != "1" ]; then \
         hf download "$ML_MODEL_REPO" --revision "$ML_MODEL_REVISION" --local-dir /ml/bundle; \
       else \
         mkdir -p /ml/bundle; \
       fi


FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 mailroom \
    && useradd --system --uid 10001 --gid mailroom --home-dir /app --shell /usr/sbin/nologin mailroom \
    && mkdir -p /app /data /hf_cache /models \
    && chown -R mailroom:mailroom /app /data /hf_cache \
    && chmod -R a+rX /models
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=model /ml/bundle /models/mailroom-modernbert-classifier/

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MAILROOM_BASE_DIR=/data \
    HF_HOME=/hf_cache \
    ML_MODEL_DIR=/models/mailroom-modernbert-classifier \
    CREWAI_DISABLE_TELEMETRY=true

USER 10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["mailroom", "serve", "--host", "0.0.0.0", "--port", "8000"]
