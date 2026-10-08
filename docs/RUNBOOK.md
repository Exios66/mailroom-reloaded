# Runbook: from `git clone` to a running demo

Three paths, fastest first. Everything runs from the repo root.

| Path | Needs | Time | Use for |
| --- | --- | --- | --- |
| [A. Host demo](#a-host-demo-no-docker-no-keys) | Python, uv, Node (optional) | ~3 min | Demo, `/tui`, Jev gate, local testing |
| [B. Docker dev stack](#b-docker-dev-stack-cpu-no-keys) | Docker | ~5 min | Full topology incl. observability |
| [C. Live server](#c-live-server-real-provider) | Linux host, Docker, a provider | ~15 min | Real documents, real LLM |

## 0. Prerequisites

- Python `>=3.11,<3.13` and [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- git; Docker + compose plugin for B and C; Node 20+ only for the TUI unit tests

```bash
git clone https://github.com/Exios66/mailroom-reloaded.git
cd mailroom-reloaded
uv sync --extra dev
```

## A. Host demo (no Docker, no keys)

Starts a mock LLM, the API with the embedded watcher, the `/ui` and `/tui` front ends, and (with `JEV=1`) a mock Jev decision model that is calibrated and seeded with documents.

```bash
JEV=1 scripts/tui_dev.sh up       # omit JEV=1 for the plain pipeline
scripts/tui_dev.sh status         # all three should say "running", health: ok
```

| What | URL |
| --- | --- |
| Browser terminal | http://127.0.0.1:8000/tui |
| Web UI | http://127.0.0.1:8000/ui |
| API docs | http://127.0.0.1:8000/docs |
| Jev gate state | `http://127.0.0.1:8000/v1/jev` — API request; requires `Authorization: Bearer <token>` when `MAILROOM_API_TOKEN` is set |

Try it in `/tui`: `help`, `ls`, `inspect <doc_id>`, `audit <doc_id>`, `jev`, `review`, `upload` (or drop a `.txt` into `data/tui-dev/base/inbox/`). Quick pipeline check: `cp tests/ingest/fixtures/letter.txt data/tui-dev/base/inbox/` and the document reaches `archived` within ~15 s. Seeded Jev documents land in `review` (parked) so you can resolve them.

```bash
scripts/tui_dev.sh down           # stop everything; state stays in data/tui-dev
rm -rf data/tui-dev               # reset to a clean slate
```

Options: `MAILROOM_API_TOKEN=secret scripts/tui_dev.sh up` exercises the auth path (paste the token with `auth <token>` in `/tui`). Ports: `TUI_API_PORT`, `TUI_MOCK_PORT`, `TUI_JEV_PORT`. Details: [TUI.md](TUI.md), [JEV.md](JEV.md).

The mock LLM answers correspondence-style documents only. For other content use path C.

## B. Docker dev stack (CPU, no keys)

```bash
scripts/dev.sh up                 # builds, starts, runs the smoke test
scripts/dev.sh status
scripts/dev.sh logs
scripts/dev.sh down
```

Same API on :8000 plus the mock provider in containers. Full description: [DEV_SERVER.md](DEV_SERVER.md).

## C. Live server (real provider)

Run on the host that will serve traffic.

1. **Configure**

   ```bash
   cp .env.example .env
   ```

   Edit `.env`:

   - Run `openssl rand -hex 24` in your shell, then paste the output into `.env` as `MAILROOM_API_TOKEN=<generated value>` (required: the app binds `0.0.0.0` inside the container). Compose does not execute shell commands in `.env`.
   - `GRAFANA_ADMIN_PASSWORD=<strong password>`
   - `DEFAULT_PROVIDER=openrouter` with `OPENROUTER_API_KEY=...`, or `vllm` with `VLLM_BASE_URL`/`VLLM_API_KEY` (Modal: [deploy/README.md](../deploy/README.md)), or `llamafile` with `LLAMAFILE_BASE_URL`
   - Optional Jev: `MAILROOM_JEV_PROVIDER=openrouter|typesafe|local` and a calibration file at `<base_dir>/models/jev_calibration.json` ([JEV.md](JEV.md))

2. **Start**

   ```bash
   docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
   scripts/smoke.sh                 # drops a fixture in the inbox, waits for "archived"
   ```

   Profiles: `--profile local-llm` (llamafile), `--profile gpu` (vLLM + DCGM), `--profile split-watcher` (set `MAILROOM_EMBED_WATCHER=0` on `app`).

3. **Check**

   ```bash
   curl -fsS http://localhost:8000/health
   curl -fsS -H "Authorization: Bearer $MAILROOM_API_TOKEN" http://localhost:8000/v1/documents
   curl -fsS -H "Authorization: Bearer $MAILROOM_API_TOKEN" http://localhost:8000/v1/jev
   ```

4. **Expose safely.** `/ui`, `/tui` and `/health` are public; `/v1` needs the bearer token. Compose publishes the app only on `127.0.0.1:8000` so remote clients cannot bypass the TLS proxy. Put TLS in front (Caddy or nginx reverse proxy to `127.0.0.1:8000`) and do not publish Phoenix (:6006), Prometheus (:9090) or Grafana (:3000); they bind to loopback by design. The Gmail Pub/Sub push route needs an auth proxy: see [gmail-intake.md](gmail-intake.md).

5. **Use it.** Open `/tui`, run `auth <token>`, then `ls`. Or drop files into `data/inbox/`, or `POST /v1/documents`.

> Path C has not been run end to end by the maintainers' tooling (no Docker daemon or provider was available when this runbook was written). Treat the first deploy as a rehearsal and run `scripts/smoke.sh`.

## Verify the checkout

```bash
uv run pytest -q                              # expect ~690 passed
uv run ruff check .
node --test tests/tui/js/*.test.mjs           # TUI unit tests
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `address already in use` on 8000/8898/8899 | An older stack is running: `scripts/tui_dev.sh down`, or change `TUI_*_PORT`. `status` reporting `stopped` while `health: ok` means another process owns the port. |
| `/tui` says `401` | Server has a token: `auth <token>` (tokens are kept in sessionStorage only). |
| `Failed to spawn: pytest` | Run `uv sync --extra dev`. |
| `uv sync --all-extras` fails on torch | Don't use `--all-extras`; pick the extras you need. |
| Jev shows `enabled: false` | Set `MAILROOM_JEV_PROVIDER`; it also needs `models/jev_calibration.json`. |
| Documents stuck in `review` | They are parked by design: `review` in `/tui`, or `POST /v1/review/{id}/resolve`. |
| Compose: no `docker.sock` | Start the Docker daemon, then re-run `up`. |

More: [OPERATIONS.md](OPERATIONS.md), [CONFIGURATION.md](CONFIGURATION.md).
