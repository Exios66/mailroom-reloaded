# Quickstart

From a fresh clone to a running dev stack, the browser TUI, a real document
processed end-to-end, traces in Phoenix, and a production vLLM on Modal. Every
command is copy-pasteable from the repository root; the local stack needs **no
GPU, no API key and no model download** (the provider defaults to the bundled
`mock`).

- API / `/ui` : <http://localhost:8000>
- **TUI**      : <http://localhost:8000/tui>
- Phoenix     : <http://localhost:6006>  (traces)
- Prometheus  : <http://localhost:9090>
- Grafana     : <http://localhost:3000>  (`admin` / `admin` by default)

## What this repo is (provenance)

`mailroom-reloaded` is the compressed, deployment-ready Digital Mailroom. Its
pipeline is ported from **[`Exios66/llm-mailroom`](https://github.com/Exios66/llm-mailroom)**,
with one deliberate change:

- **Stages** (`src/mailroom_reloaded/pipeline/state.py`, `NODE_ORDER`):
  `ingest → bert_primary → sort → gate_classify → extract → gate_extract →
  report_catalog_archive`, plus the conditional `verify` / `boss` / `grade`
  branches. These correspond to `llm-mailroom`'s `src/pipeline/` and
  `src/graph/`.
- **Agent layer replaced.** `llm-mailroom` ran its graph on **LangGraph**
  (`src/graph/build_graph.py`, `routing.py`, `state.py`) with **LangChain**
  agents (`src/langchain_agents/`: `sorter_agent`, `specialist_agents`,
  `classifier`, …). Here that layer is **CrewAI**: the agents live in
  `src/mailroom_reloaded/agents/` (`sorter`, `specialists`, `gate`, `judge`,
  `arbiter`, `boss`, `jev`), and control flow is owned by a deterministic driver,
  `MailroomFlow._drive_nodes` (`src/mailroom_reloaded/pipeline/flow.py`) — not
  CrewAI's event engine. The `@start`/`@listen`/`@router` methods exist only so
  the class is routable/plottable.
- **No LangGraph/LangChain bytes.** The dependency fence
  (`tests/test_dependency_fence.py`) forbids `langgraph`, `langchain*`,
  `langfuse`, `braintrust` and `litellm` under `src/` — the LangGraph/LangChain
  agent packages are intentionally absent.

The production **Modal vLLM** deploy (`deploy/modal_vllm.py`) is also carried
here; see §6.

## 0. Prerequisites

- **Docker Desktop** installed and **running** (not paused — check the whale menu;
  a paused engine answers every `docker` command with *"Docker Desktop is manually
  paused"*).
- `docker compose` v2 on `PATH` (`docker compose version`).
- Optional, for pulling a dataset sample on the host: `uv` (`uv --version`).
- Optional, for the Modal deploy in §6: the `modal` client (`uv sync --extra deploy`).

## 1. Start the local dev stack + TUI

```bash
# from the repository root
scripts/dev.sh up          # or: make dev
scripts/dev.sh status      # app / phoenix / prometheus / grafana should print OK
```

`scripts/dev.sh up` builds `mailroom-reloaded-dev:latest` from
`deploy/Dockerfile.dev` and starts `app` (`uvicorn --reload`), a separate
`watcher`, the `mock` provider, and the observability stack. `app` publishes
`127.0.0.1:8000` only. Wait until `scripts/dev.sh status` prints four `OK` lines,
then open <http://localhost:8000/tui> — a static ES-module terminal served by the
API, no build step.

Useful TUI commands:

| Command | What it does |
| --- | --- |
| `ls` | list documents / runs |
| `runs` | list evaluation + pipeline runs |
| `inspect <doc_id>` | show one document's manifest, route trail and spans |
| `replay` | open the replay viewer for a run |
| `ledger` | show the archive-ledger chain |
| `audit` | verify the audit chain |
| `inbox` | open the sandbox Correspondent inbox |
| `auth <token>` | set the bearer token (only needed when `MAILROOM_API_TOKEN` is set) |

## 2. Upload a real document from `mailroom-dataset`

Pull one document from `Lucius-Morningstar/mailroom-dataset` (the blind `default`
config) and POST it to the running API:

```bash
# host, via uv
uv run python scripts/fetch_dataset_sample.py --upload http://localhost:8000

# or inside the app container
docker exec mailroom-dev-app-1 \
  python scripts/fetch_dataset_sample.py --upload http://127.0.0.1:8000
```

The helper downloads the `default/test` parquet shard, writes the row's
`doc_text` to `data/samples/<name>.txt`, and uploads it (`POST /v1/documents`).
Flags: `--index N` (row within the split), `--config`/`--split`, `--out PATH`,
`--revision` (pin a dataset commit), `--upload URL`. Without `--upload` it only
writes the file:

```bash
uv run python scripts/fetch_dataset_sample.py --out data/samples/sample.txt
```

The `watcher` container drains the inbox and runs the pipeline
(`ingest → bert_primary → sort → extract → … → archive`); it reaches `archived`
in a few seconds.

## 3. Inspect and replay

In the TUI: `ls` → find the document (its filename is the dataset name) →
`inspect <doc_id>` → `replay`. Or hit the API directly:

```bash
curl -s http://localhost:8000/v1/documents | python3 -m json.tool | head
```

## 4. Traces land in Phoenix — pipeline, not health

The `app` and `watcher` export OTLP spans to the `otel-collector`, which forwards
traces to **Phoenix** (project `mailroom-live`). Each processed document produces:

```
mailroom.document
└─ mailroom.node.<stage>          (ingest, bert_primary, sort, extract, …, archive)
   └─ mailroom.llm.sorter / mailroom.llm.<specialist> / ChatCompletion
```

Health-check and static-asset requests are **excluded** from tracing
(`create_app()` passes `telemetry={"exclude": _telemetry_exclude,
"operation_spans": False}`), so the container healthcheck (`GET /health` every
few seconds) and the TUI's `/tui/assets/*` loads do **not** create spans. Phoenix
therefore holds the mailroom pipeline traces rather than health noise.

Verify:

```bash
curl -s "http://localhost:6006/v1/projects/mailroom-live/spans?limit=200" \
 | python3 -c "import sys,json;from collections import Counter;\
d=json.load(sys.stdin);print(Counter(s['name'] for s in d['data']).most_common())"
```

Expect `mailroom.node.*` / `mailroom.llm.*` / `ChatCompletion` and **no**
`GET /health` or `fastapi.*` spans.

## 5. State and the associated volume

State lives in the repository `./data` directory, bind-mounted into the
containers at `/data`, plus named volumes for the observability stack:

```bash
# what the app container has mounted
docker inspect mailroom-dev-app-1 \
  --format '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
#   bind .../mailroom-reloaded/data -> /data      <- app/watcher state (inspectable)
#   bind .../mailroom-reloaded/src  -> /app/src
#   bind .../mailroom-reloaded/tests -> /app/tests

# named volumes (Phoenix / Prometheus / Grafana / OTel)
docker volume ls | grep mailroom
#   mailroom-dev_phoenix_data  mailroom-dev_prometheus_data
#   mailroom-dev_grafana_data  mailroom-dev_otel_targets
```

Because `./data` is a bind mount, the bins, SQLite databases and manifests are
inspectable directly on the host (e.g. `ls data/`, `sqlite3 data/*.sqlite`) and
survive `scripts/dev.sh down`. The VS Code **image-mode** devcontainer mounts the
same `./data` at `/data` (see `.devcontainer/devcontainer.json`).

## 6. Production: vLLM on Modal

`deploy/modal_vllm.py` runs one Modal app, `mailroom-vllm`, that serves an
OpenAI-compatible vLLM (`/v1/*`) **and** its Prometheus `/metrics` from one
`@modal.web_server`, so the app's `VLLM_BASE_URL` and the Collector's
`VLLM_METRICS_URLS` share a host. vLLM is pinned to the same version as the
compose image, with the SAND-37 engine flags (prefix caching, fp8 KV cache).

```bash
# once
uv sync --extra deploy && modal token new
modal secret create mailroom-vllm-api-key VLLM_API_KEY="$(openssl rand -hex 24)"

# deploy (defaults: L4, 1 GPU, scale-to-zero after 300s idle)
MODAL_GPU=L4 MODAL_GPU_COUNT=1 modal deploy deploy/modal_vllm.py
```

Then point the app at the printed URL:

```bash
DEFAULT_PROVIDER=vllm
VLLM_BASE_URL=https://<workspace>--mailroom-vllm-serve.modal.run/v1
VLLM_API_KEY=<the secret value>
VLLM_METRICS_URLS=https://<workspace>--mailroom-vllm-serve.modal.run/metrics
```

Teardown (billing stops when the app is stopped, and it also scales to zero after
`MODAL_SCALEDOWN_WINDOW` idle seconds):

```bash
modal app stop mailroom-vllm
modal app list            # confirm no running containers
modal billing report --for today
```

Full settings table, the SAND-37 posture run (`MODAL_GPU_COUNT=2 …`) and compose
notes: [deploy/README.md](../deploy/README.md). Env reference:
[docs/CONFIGURATION.md](CONFIGURATION.md).

## 7. Stop, reset, logs

```bash
scripts/dev.sh logs        # follow logs (or: make dev-logs)
scripts/dev.sh ps          # container status
scripts/dev.sh down        # stop the stack, keep ./data + volumes
scripts/dev.sh reset       # stop, remove volumes, delete ./data
```

## 8. VS Code Dev Container (alternative to the host stack)

- **Image mode** — Command Palette → **Dev Containers: Reopen in Container**.
  Builds `deploy/Dockerfile.dev`, mounts the repo at `/app` and `./data` at
  `/data`, and `postStartCommand` starts the mock + API. Then open
  <http://localhost:8000/tui>.
- **Full stack** — **Dev Containers: Open Folder in Container…** and pick
  `.devcontainer/compose/devcontainer.json` (runs the whole Compose topology).

See [deploy/README.md](../deploy/README.md#vs-code-dev-container).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Docker Desktop is manually paused` | Unpause from the whale menu / Dashboard. |
| `/tui` returns 502 / connection refused | `scripts/dev.sh status`; wait for `app` `OK`, then `scripts/dev.sh logs app`. |
| Upload accepted but never `archived` | The `watcher` container drains the inbox: `scripts/dev.sh logs watcher`. |
| Phoenix empty | Give the batch exporter a few seconds; check `scripts/dev.sh logs otel-collector`. |
| Port 8000 already in use | `lsof -nP -iTCP:8000 -sTCP:LISTEN`; stop the other process or edit the port in `deploy/docker-compose.dev.yml`. |
| `fetch_dataset_sample.py` needs deps | `uv sync --extra eval` (provides `pyarrow`). |
| `modal deploy` fails on auth | `modal token new`; the `deploy` extra must be installed. |

## See also

- [README.md](../README.md) — overview, CLI/API tables.
- [docs/DEV_SERVER.md](DEV_SERVER.md) — local dev-server workflow.
- [docs/TUI.md](TUI.md) — `/tui` commands, replay viewer, themes, auth.
- [docs/CONFIGURATION.md](CONFIGURATION.md) — every env var and `taxonomy.yaml` block.
- [deploy/README.md](../deploy/README.md) — Modal vLLM deploy, dev container, docker smoke.
- [docs/RUNBOOK.md](RUNBOOK.md) — clone-to-demo paths A/B/C.
