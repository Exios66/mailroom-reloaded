# Local DEV server

`deploy/docker-compose.dev.yml` is a self-contained, CPU-only topology for
running and live-testing the mailroom pipeline on a laptop. It does **not** need
`deploy/docker-compose.yml`, a GPU, a model download, or any API key: the
provider defaults to `mock`.

If you only want to know how to drive it:

```bash
scripts/dev.sh up          # or: make dev
scripts/dev.sh status
scripts/dev.sh logs
scripts/dev.sh down        # or: make dev-down
```

## What `scripts/dev.sh up` does, step by step

1. Verifies `docker` and the `docker compose` plugin are on `PATH`; exits 127
   with an install hint if either is missing.
2. Changes to the repository root and, if a `.env` file exists, loads it (values
   become both the shell environment and the `--env-file` for compose).
3. Creates the host state directory `./data/` if absent.
4. Runs `docker compose -f deploy/docker-compose.dev.yml up -d --build`, which:
   - builds `mailroom-reloaded-dev:latest` from `deploy/Dockerfile.dev`
     (`uv sync --extra dev`, no ModernBERT stage, no `bert` extra);
   - starts `app` running `uvicorn mailroom_reloaded.api.app:app --reload`
     with `./src` bind-mounted at `/app/src`, so code edits reload live;
   - starts a **separate** `watcher` container (`mailroom watch`) that shares the
     same `./data` bind mount and drains `inbox/` into the pipeline;
   - starts `otel-collector`, `phoenix`, `prometheus`, and `grafana`;
   - runs the `otel-targets` one-shot that writes an empty Prometheus file-SD
     targets file (dev has no vLLM to scrape).
5. Prints the service URLs. The `local-llm` profile (CPU llamafile) is not
   started unless you ask for it.

## Services and ports

| Service | Profile | Host port | Role |
| --- | --- | --- | --- |
| `app` | default | 127.0.0.1:8000 | FastAPI `/v1`, `/health`, `/ui` (`--reload`, src bind-mounted) |
| `watcher` | default | — | split watcher `mailroom watch`, shares `./data` |
| `otel-collector` | default | 4317/4318, 8889 | OTLP in; traces → Phoenix; metrics → Prometheus |
| `phoenix` | default | 127.0.0.1:6006 | trace UI |
| `prometheus` | default | 127.0.0.1:9090 | metrics store |
| `grafana` | default | 127.0.0.1:3000 | provisioned dashboards (admin/admin by default) |
| `llamafile` | `local-llm` | 127.0.0.1:8080 | optional CPU llamafile server |

All published ports are bound to loopback. `app` and `watcher` share
`../data:/data`; `otel-targets` seeds the collector's `vllm.json` targets file,
so the collector starts cleanly with no vLLM/DCGM present. No service requests
a GPU.

State lives in the repo at `./data/` (bins, SQLite, manifests, `watcher.lock`)
and in the named volumes `phoenix_data`, `prometheus_data`, `grafana_data`,
`otel_targets`, `llamafile_models`.

## Running the dev test suite

```bash
scripts/dev_test.sh            # uv sync --extra dev; ruff check; pytest (live deselected)
scripts/dev_test.sh --live     # also run tests marked `live` (needs `scripts/dev.sh up`)
scripts/dev_test.sh --no-lint  # skip ruff
```

`scripts/dev_test.sh` is the only entry point that installs dependencies. Its
steps are `uv sync --extra dev` (the only network step), `uv run ruff check .`,
then `uv run pytest`. `pyproject.toml` registers a `live` marker and defaults
`addopts` to `-m 'not live'`, so a plain `uv run pytest` (and `make test`)
deselects end-to-end tests. Pass `-m live` explicitly to select them.

## End-to-end check (the real pipeline)

```bash
scripts/dev.sh up
scripts/dev.sh smoke          # or: make smoke
```

`smoke` mirrors `scripts/smoke.sh`: it `POST`s a fixture to `/v1/documents`,
then polls `GET /v1/documents` until the watcher has drained and archived the
document, and finally checks Phoenix and Grafana are healthy. It prints
`DEV SMOKE OK` on success.

## make targets

| Target | Delegates to |
| --- | --- |
| `make dev` | `scripts/dev.sh up` |
| `make dev-down` | `scripts/dev.sh down` |
| `make dev-logs` | `scripts/dev.sh logs` |
| `make dev-ps` / `make dev-status` / `make dev-reset` | matching `scripts/dev.sh` command |
| `make dev-test` | `scripts/dev_test.sh` |
| `make lint` | `uv run ruff check .` |
| `make test` | `uv run pytest` |
| `make smoke` | `scripts/dev.sh smoke` |
| `make eval` | `uv run mailroom eval` |
| `make gmail` | `uv run mailroom gmail` |

## Configuration

Override any compose value through the environment or a repo-root `.env`:

| Variable | Default | Notes |
| --- | --- | --- |
| `DEFAULT_PROVIDER` | `mock` | `llamafile`/`openrouter` need the matching profile/key |
| `MAILROOM_API_TOKEN` | empty | when empty the loopback API is unauthenticated |
| `MAILROOM_BASE_DIR` | `/data` | container state root |
| `GRAFANA_ADMIN_PASSWORD` | `admin` | Grafana admin password |
| `SMOKE_TIMEOUT` | `300` | seconds `dev.sh smoke` waits for `archived` |

## Troubleshooting

- **`dev.sh: docker is required but was not found on PATH`** — install Docker
  Desktop or the engine + `docker compose` plugin.
- **`the 'docker compose' plugin is required`** — install the Compose v2 plugin;
  the legacy `docker-compose` v1 binary is not used.
- **`bind: address already in use`** — the dev stack shares the documented ports
  (8000/6006/9090/3000) with the base compose. Stop the other stack
  (`make dev-down`) or edit the loopback port mapping in
  `deploy/docker-compose.dev.yml`.
- **Document stays `accepted` / never `archived`** — check the watcher logs
  (`scripts/dev.sh logs watcher`); a stale `watcher.lock` from a killed
  container is cleared by `scripts/dev.sh reset`. Confirm only the `watcher`
  container is draining (`MAILROOM_EMBED_WATCHER=0` on `app`).
- **`config -q` fails** — `docker compose -f deploy/docker-compose.dev.yml
  config -q` must exit 0; a syntax error here is a broken checkout, not an
  environment problem.
- **Phoenix/Grafana show nothing** — give the stack ~30 s to become healthy;
  `scripts/dev.sh status` reports each endpoint.
- **Wipe everything** — `scripts/dev.sh reset` removes the containers, the
  named volumes, and `./data`.
