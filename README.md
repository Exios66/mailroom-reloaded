# mailroom-reloaded

The compressed and deployment-ready package of the Digital Mailroom, built on CrewAI Flows.

## Setup

```bash
uv sync --extra dev
uv run pytest
```

Providers: `llamafile`, `openrouter`, `vllm`, `mock` (default). Copy `.env.example` to `.env` to configure.
The taxonomy (classes, confidence thresholds, per-class run conditions) lives in
`src/mailroom_reloaded/config/taxonomy.yaml`.

Extras: `bert`, `eval`, `embeddings`, `deploy`, `dev`, `parity`.

## Quick start (Docker)

```bash
cp .env.example .env            # set MAILROOM_API_TOKEN and GRAFANA_ADMIN_PASSWORD (the container binds 0.0.0.0)
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
docker compose -f deploy/docker-compose.yml --env-file .env --profile local-llm up -d --build  # + llamafile
docker compose -f deploy/docker-compose.yml --env-file .env --profile gpu up -d                # + vLLM and DCGM
scripts/smoke.sh                # drops a fixture in the inbox and waits for "archived"
```

| Service | URL |
| --- | --- |
| API, `/ui` | http://localhost:8000 |
| Phoenix traces | http://localhost:6006 |
| Prometheus | http://localhost:9090 |
| Grafana (Pipeline, Serving & GPU, Quality) | http://localhost:3000 |

Use the `split-watcher` profile with `MAILROOM_EMBED_WATCHER=0` to run the watcher as its own container.
Set `GRAFANA_ADMIN_PASSWORD` (required). Set `VLLM_METRICS_URLS` (comma-separated `host:port` list, plus `VLLM_METRICS_SCHEME=https` if needed) to scrape remote vLLM endpoints such as Modal. Observability and engine ports bind to 127.0.0.1 only.
