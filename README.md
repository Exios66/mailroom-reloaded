# mailroom-reloaded

The compressed, deployment-ready **Digital Mailroom**: a CrewAI-Flows pipeline
that ingests documents, classifies and extracts them, and archives them with a
verifiable audit chain — plus an evaluation harness that scores runs against
`Lucius-Morningstar/mailroom-dataset`.

**Architecture in three lines**

- `ingest → bert_primary → sort → gate_classify → extract → gate_extract →
  report → catalog → archive`, with `verify` / `boss` / `human_review`
  escalation paths.
- A clean text-layer document costs exactly **two LLM calls** (sorter +
  specialist); routing is a deterministic gate, not a reviewer LLM
  (`pipeline/flow.py:176-207`).
- Durability is filesystem bins + JSON manifests + a SQLite hash-chained audit
  log; a crash resumes from the manifest's last completed node
  (`watcher.py:121-149`, `storage/bins.py:79-92`).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full pipeline and module
map.

## Install

Requires Python `>=3.11,<3.13` and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev                              # core + pytest/ruff
uv sync --extra dev --extra eval --extra bert    # + dataset loader + ModernBERT
```

Optional extras (`pyproject.toml:37-49`): `bert` (ModernBERT via
`mailroom-ml[serve]`), `eval` (`datasets`, `pandas`, `matplotlib`), `gmail`
(Google client stack), `embeddings` (`sentence-transformers`), `deploy`
(`modal`), `dev` (`pytest`, `scikit-learn`, `ruff`), `parity`
(`llm-dojo-scoring` v0.21.0 for scoring parity tests).

## Providers

Set `DEFAULT_PROVIDER` in `.env` (copy `.env.example`); the global setting wins
over the per-agent `provider` in `taxonomy.yaml` (`llm/client.py:80-113`).

| Provider | Needs | Notes |
| --- | --- | --- |
| `mock` (default) | `MOCK_BASE_URL` | Points at an OpenAI-compatible fake; used by the test suite, not a real run by itself. |
| `openrouter` | `OPENROUTER_API_KEY` | Hosted models; per-token cost from `taxonomy.yaml` `cost_models`. |
| `vllm` | `VLLM_BASE_URL`, optional `VLLM_API_KEY` | Local or Modal vLLM; model ids remapped through `vllm_model_map`. |
| `llamafile` | `LLAMAFILE_BASE_URL` | Single-GGUF sidecar; ids remapped through `llamafile_model_map`. |

### Jev route gate (opt-in)

The deterministic gate can be swapped for the **Jev (TypeSafe System One)**
probabilistic decision model. It is opt-in via `MAILROOM_JEV_PROVIDER=off|openrouter|typesafe|local`
(default `off`) and only takes effect once a calibration exists at
`models/jev_calibration.json`; a `JevGate` then overrides **only the medium
confidence band** and never a hard rule. Transports, calibration and caveats are
documented in [docs/JEV.md](docs/JEV.md).

## Quickstart

```bash
cp .env.example .env
# edit .env: set DEFAULT_PROVIDER and its credentials (openrouter / vllm / llamafile)
uv run mailroom run tests/ingest/fixtures/letter.txt   # one document, prints a summary
```

`mailroom run` with the default `mock` provider needs `MOCK_BASE_URL` pointing
at the fake OpenAI server (`tests/fakes/openai_server.py`); use a real provider
for a live run. Full env reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

### Docker

```bash
cp .env.example .env   # set MAILROOM_API_TOKEN and GRAFANA_ADMIN_PASSWORD (app binds 0.0.0.0)
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
scripts/smoke.sh       # drops a fixture in the inbox and waits for "archived"
```

| Service | URL |
| --- | --- |
| API, `/ui` | http://localhost:8000 |
| `/tui` browser terminal ([docs/TUI.md](docs/TUI.md)) | http://localhost:8000/tui |
| Phoenix traces | http://localhost:6006 |
| Prometheus | http://localhost:9090 |
| Grafana (Pipeline, Serving & GPU, Quality) | http://localhost:3000 |

Compose profiles (`deploy/docker-compose.yml`):

| Profile | Adds | Notes |
| --- | --- | --- |
| *(default)* | `app`, `otel-collector`, `phoenix`, `prometheus`, `grafana`, `vllm-targets` | API + embedded watcher + observability. |
| `local-llm` | `llamafile` | CPU/GPU llamafile sidecar. |
| `gpu` | `vllm`, `dcgm-exporter` | Local vLLM and GPU metrics. |
| `split-watcher` | `watcher` | Standalone watcher; run `app` with `MAILROOM_EMBED_WATCHER=0`. |

Observability and engine ports bind to `127.0.0.1` only. For the day-to-day dev
stack (hot reload, `docker-compose.dev.yml`, `Makefile`, helper scripts), see
[docs/DEV_SERVER.md](docs/DEV_SERVER.md). Modal deployment is documented in
[deploy/README.md](deploy/README.md).

## CLI

Console script `mailroom` (`pyproject.toml:51-52`), defined in
`src/mailroom_reloaded/cli.py`.

| Command | Key flags (defaults) | What it does |
| --- | --- | --- |
| `mailroom serve` | `--host`, `--port`, `--watch/--no-watch` (on) | Runs the FastAPI app + `/ui` under uvicorn, optionally with the embedded watcher (`cli.py` `serve`). |
| `mailroom watch` | `--worker-id cli-watcher`, `--concurrency 1` (1–32) | Drains `inbox/` forever as a standalone watcher (`cli.py` `watch`). |
| `mailroom run <file>` | `--worker-id cli` | Runs one document through the pipeline, prints `doc_id`/`status`/`doc_type`/`route_trail` (`cli.py` `run`). |
| `mailroom eval` | see below | Runs an evaluation posture, prints its `run_id` (`cli.py` `eval`). |
| `mailroom train-gate` | `--rows` (required), `--out models/route_gate.json`, `--calibration` | Fits the route gate or temperature calibration from JSONL rows (`cli.py` `train_gate_command`). |
| `mailroom jev` | `decide --state --type choice/noul/score --instructions [--criteria K=D] [--criteria-list A,B]`; `calibrate --rows --out models/jev_calibration.json` | Asks the opt-in Jev decision model one typed question, or fits its calibration. See [docs/JEV.md](docs/JEV.md). |
| `mailroom card` | `--run-id` (repeatable), `--doc-type`, `--master`, `--out runs` | Writes a `mailroom.card/v1` JSON+MD per run (or the aggregated SAND-37 master card) and echoes the Markdown path (`cli.py` `card`). |
| `mailroom conformance` | `--provider`, `--per-class 2`, `--revision ed7576b6`, `--split train`, `--local-dir`, `--out runs/conformance` | Runs the spec §11 behavioural conformance suite and writes a JSON+MD card (`cli.py` `conformance`). |
| `mailroom gmail auth\|poll\|watch` | `--limit`, `--process/--no-process`, `--worker-id` | Gmail attachment intake; needs the `gmail` extra. See [docs/gmail-intake.md](docs/gmail-intake.md). |

`mailroom eval` flags (`cli.py` `eval`): `--revision ed7576b6`, `--per-class 20`,
`--seed 42`, `--classes ""`, `--concurrency 8`, `--posture-label pipeline`,
`--gpu L4`, `--gpus 1`, `--prompt-set frozen_v1`, `--merger-mode frozen`,
`--mode pipeline|specialist_cell`, `--judge-sample-rate 1.0`, `--split test`,
`--local-dir`, `--gpu-usd-per-hour 0.80`. Details:
[docs/EVALUATION.md](docs/EVALUATION.md).

## API

The app is a thin, read-mostly surface over the bins, manifests, SQLite catalog
and audit log (`api/app.py`). Every `/v1` route requires the bearer token when
`MAILROOM_API_TOKEN` is set; binding off loopback without a token refuses to
start (`api/app.py:87-117`). `/health`, `/` and `/ui` stay public.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness (`api/app.py:388-391`). |
| `GET` | `/` | Redirect to `/ui` (`api/app.py:393-396`). |
| `POST` | `/v1/documents` | Multipart upload → `inbox/`; returns `{doc_id, file, status}` (202) (`api/app.py:136-174`). |
| `GET` | `/v1/documents` | Catalog listing; `?status=&limit=&offset=` (`api/app.py:177-188`). |
| `GET` | `/v1/documents/{doc_id}` | Manifest + compiled report + catalog row (`api/app.py:191-205`). |
| `GET` | `/v1/audit/{doc_id}` | Audit entries + hash-chain verification (`api/app.py:208-217`). |
| `POST` | `/v1/review/{doc_id}/resolve` | Disposition a parked document: `approve` / `correct` / `reject` (`api/app.py:220-240`). |
| `GET` | `/v1/runs` | Eval runs and document counts from SQLite `eval_docs` (`api/app.py:243-246`). |
| `GET` | `/v1/runs/{run_id}/cards` | Card JSONs on disk; empty until cards are written (`api/app.py:249-260`). |
| `POST` | `/v1/intake/gmail` | Gmail Pub/Sub push; ingests in the background, returns 204 (`api/app.py:297-321`). |
| `POST` | `/v1/intake/gmail/poll` | On-demand Gmail fetch (`api/app.py:324-333`). |
| `GET` | `/ui` | Vanilla-JS runs page, no build step (`api/app.py:398-404`). |

Uploads are capped at 50 MB (`MAILROOM_MAX_UPLOAD_BYTES`) and accept
`.txt .md .pdf .docx .rtf .html .htm` (`api/app.py:60-66`).

### `/ui`

The packaged page (`api/ui/index.html`) lists documents (filter by status), the
review queue, audit-chain verification, eval runs and their cards, and a
document detail pane. Enter the API token in the header when one is configured,
and click through to Phoenix (`:6006`) and Grafana (`:3000`).

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — pipeline, bins/manifest
  durability, audit chain, determinism, module map.
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every env var and
  `taxonomy.yaml` block, with defaults and effect.
- [docs/EVALUATION.md](docs/EVALUATION.md) — dataset, blind/ground-truth split,
  train/test discipline, `mailroom eval`, cards.
- [docs/JEV.md](docs/JEV.md) — the opt-in Jev (TypeSafe System One) decision
  model: transports, typed answers, the `jev:` config block, CLI and calibration.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — watcher, review, audit
  verification, observability, cost, failure modes.
- [docs/TESTING.md](docs/TESTING.md) — test tiers, the `live` marker, the
  dependency fence and ruff.
- [docs/gmail-intake.md](docs/gmail-intake.md) — Gmail intake setup and limits.
- [docs/DEV_SERVER.md](docs/DEV_SERVER.md) — local dev-server workflow.
- [deploy/README.md](deploy/README.md) — Modal vLLM deployment.
- Design and plan: `docs/superpowers/specs/2026-10-07-mailroom-reloaded-design.md`,
  `docs/superpowers/plans/2026-10-07-mailroom-reloaded.md`.

## Tests

```bash
uv sync --extra dev
uv run pytest -q            # live tests are deselected via the `live` marker
MAILROOM_LIVE=1 uv run pytest -m live -v   # hits a configured real provider
uv run ruff check .
```

The scoring-parity test skips unless the `parity` extra is installed. See
[docs/TESTING.md](docs/TESTING.md) for the test tiers, the dependency fence and
the dev-server suite.
