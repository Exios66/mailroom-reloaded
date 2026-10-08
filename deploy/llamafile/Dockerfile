FROM debian:bookworm-slim

# llamafile sidecar (OpenAI-compatible server). GGUF weights are never baked in;
# they live in the llamafile_models volume mounted at /models.
ARG LLAMAFILE_VERSION=0.10.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /llamafile
RUN curl -fsSL -o llamafile \
      "https://github.com/mozilla-ai/llamafile/releases/download/${LLAMAFILE_VERSION}/llamafile-${LLAMAFILE_VERSION}" \
    && chmod +x llamafile

COPY run.sh /usr/local/bin/llamafile-run
RUN chmod +x /usr/local/bin/llamafile-run

ENV LLAMAFILE_MODEL=/models/model.gguf \
    LLAMAFILE_ALIAS=qwen3:7b \
    LLAMAFILE_CTX=16384 \
    LLAMAFILE_GPU=disable

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/llamafile-run"]
