# AGENTS.md — operating contract for `mailroom-reloaded`

This is the governance and operating contract for **any** AI agent (OpenCode,
Cursor, Claude Code, Codex) working in this repository. Read it first, every
session. It is the truth for *how to work here*; `docs/` is the truth for
*behavior*, and `DISCUSSION_BOARD.md` is the work log.

- **Canonical checkout:** a clone of `Exios66/mailroom-reloaded`. Work happens
  on a feature branch cut from `main` (see §8.1); `main` is the only
  long-lived branch.
- **Master plan:** `docs/superpowers/plans/2026-10-09-mailroom-core-plan.md` is
  the single source of truth for what is done and what remains (item ids such
  as `R-13`). Cite item ids in issues and PRs; update the plan in the same PR
  that completes an item.
- If a statement here conflicts with `README.md`, `docs/ARCHITECTURE.md`, the
  design spec (`docs/superpowers/specs/2026-10-07-mailroom-reloaded-design.md`)
  or the plan (`docs/superpowers/plans/2026-10-07-mailroom-reloaded.md`), the
  more specific document wins, and this file must be corrected in the same
  change that reveals the conflict.

---

## 1. What this repo is

`mailroom-reloaded` is the **compressed, deployment-ready Digital Mailroom**: a
CrewAI-Flows pipeline that ingests documents, classifies and extracts them, and
archives them with a verifiable audit chain, plus an evaluation harness that
scores runs against `Lucius-Morningstar/mailroom-dataset`.

- **Dev source of truth.** This checkout is where all code, docs, schemas and
  contracts are authored and reviewed. Do not treat a deployed image, a
  `data/` tree or a sandbox state dir as authoritative.
- **Two-repo sandbox split.** This repo owns the *contracts* (top-level
  `schemas/`, loader, server, tests); the content lives in
  `Exios66/mailroom-sandbox-content`. The **content pack is consumed, never
  edited here**: it is pinned by `sandbox/content.lock` (repo, tag, commit,
  `bundle_sha256`, `schema_version`, `dataset_revision`) and pulled with
  `mailroom sandbox content pull`. Never hand-edit a pulled bundle or rewrite
  the lock by hand — use `mailroom sandbox content bump`.
- **Package/release law (DMR-074).** Where an external package repo mirrors a
  subtree, the monorepo is the dev source of truth and propagation is via the
  packaging/sync tooling — never by hand-editing a mirror or pushing a
  standalone repo directly. A deliverable that must reach an external repo is a
  **sync unit on the card**: plan it, execute it, and cite the sync evidence.
- **Two capabilities live on `main`** (see §7.6 and §7.7): the
  **Boss mailbox** (a durable Correspondent↔Boss channel over the sandbox API)
  and **held-out scenarios** (`mailroom sandbox conformance --heldout`).

## 2. Startup ritual (every session)

1. **State your harness and canonical checkout.** Name the tool you are running
   under (OpenCode / Cursor / Claude Code / Codex) and the absolute path of the
   checkout you consider canonical. Do not silently operate in a mirror.
2. **Read the governing documents for the mission scope**, in order:
   `AGENTS.md` (this file) → `DISCUSSION_BOARD.md` (work log) → the relevant
   `docs/*.md` → the plan/spec under `docs/superpowers/`. For a package task,
   also read that package's own `AGENTS.md`/README if one exists.
3. **Prefer read-only inspection and cheap mocks before live spend or deploy.**
   The default provider is `mock`; the sandbox runs offline on an in-process
   mock LLM. Do not reach for a live provider, a GPU, Modal, or a network pull
   until a cheaper path has been ruled out and the task actually needs it.
4. **Claim before you edit.** For board-tracked work, claim the card (lane +
   Owner) before touching code; label before code; close with proof.
5. **Do not run git write commands** (add/commit/checkout/push/tag) unless the
   task explicitly asks for them. Reading git (`status`, `log`, `diff`,
   `show`) is always allowed.
6. **Leave the tree consistent:** a behavior change carries its doc/changelog
   update and its test in the same change. Docs-only changes say so.

## 3. Architecture & routing

### 3.1 Pipeline

The canonical stage order is `NODE_ORDER` in `src/mailroom_reloaded/pipeline/state.py`:

```
ingest ─▶ bert_primary ─▶ sort ─▶ gate_classify ─┬─▶ extract ─▶ gate_extract ─┬─▶ report ─▶ catalog ─▶ archive ─▶ [grade]
 (det.)      (local)      (LLM 1)                │   (LLM 2)                 ├─▶ retry_extract (≤ retry_max)
                                                 ├─▶ re_sort (FULL, once)    ├─▶ verify: judge ─▶ arbiter
                                                 └─▶ human_review (park)     ├─▶ boss (escalation)
                                                                             └─▶ human_review (park)
```

- **Two-LLM-call fast path:** a clean text-layer document costs exactly two LLM
  calls (`sort` + `extract`); routing is a deterministic gate, not a reviewer
  LLM (`pipeline/flow.py`). `ingest` and `bert_primary` are local.
- **Owner of control flow:** `MailroomFlow._drive_nodes` (a deterministic
  driver), not CrewAI's event engine. The `@start`/`@listen`/`@router` methods
  exist so the class is routable/plottable.
- **Gate:** `src/mailroom_reloaded/agents/gate.py` — `BandGate` by default,
  `LearnedGate` when `models/route_gate.json` exists, `JevGate` when the opt-in
  `MAILROOM_JEV_PROVIDER` is set and a calibration exists. Jev overrides only the
  medium band, never a hard rule (`docs/JEV.md`).
- **Durability:** filesystem bins + JSON manifests + a SQLite hash-chained audit
  log (`storage/bins.py`, `storage/audit_log.py`); a crash resumes from the
  manifest's last completed node (`watcher.py`).
- **Archive ledger:** a second, global hash chain over every run
  (`storage/ledger.py`), with optional off-host anchoring
  (`storage/anchor.py`, `mailroom audit ...`).

### 3.2 Agent roster (`src/mailroom_reloaded/agents/`)

| Agent | File | Role | LLM? |
| --- | --- | --- | --- |
| sorter | `agents/sorter.py` | Standalone structured document sorter (**LLM call 1**) | yes |
| specialists | `agents/specialists.py` | Per-class structured extraction (**LLM call 2**) | yes |
| gate | `agents/gate.py` | Deterministic route gate; replaces reviewer nodes | no |
| judge | `agents/judge.py` | Grades extraction against ground truth (`verify` / eval `grade`) | yes |
| arbiter | `agents/arbiter.py` | Dispositions the judge: accept / accept_with_caveats / re_extract / escalate | yes |
| boss | `agents/boss.py` | Escalation / class reassignment | yes |
| jev | `agents/jev.py` | Opt-in calibrated decision model backing the gate | yes (opt-in) |

### 3.3 Sandbox: Correspondent + Boss-Desk

The offline ingress simulator (`src/mailroom_reloaded/sandbox/server/`) runs two
flows on the same simulated message:

- **Flow A — Correspondent:** a clearly labelled deterministic **stand-in**
  (`correspondent.py`, `rule-based-standin/v2`) behind the replaceable
  `CorrespondentAgent` interface. It performs pre-filter, safety screen, trust
  level, intent, signals, attachment lanes, relation proposals and drafts.
  Intent is chosen by the scored triage in `triage.py` (intent lexicons, abstain
  path). `llm_correspondent.py` is an optional, default-off Correspondent that
  delegates only the triage step to a loopback endpoint and falls back to the
  rules on any failure.
- **Boss Desk:** a stand-in (`bossdesk.py`, `rule-based-standin-bossdesk/v1`)
  that turns a Correspondent result into typed Boss actions from
  `protocol/delegation_matrix.csv`.
- **Flow B — the real pipeline:** runs in-process in an isolated data dir on an
  offline mock LLM (`pipeline_runner.py`, `mock_llm.py`). Dataset-draw attachments
  are materialised as deterministic placeholder PDFs (`synthetic.py`).
- **Boss mailbox:** a durable, append-only SQLite two-way channel between the
  Correspondent and the Boss (`sandbox/server/mailbox.py`; see §7.6).

### 3.4 How a task routes to a surface

| If the task concerns… | Work surface |
| --- | --- |
| Pipeline stage / routing / durability | `src/mailroom_reloaded/pipeline/`, `storage/`, `watcher.py`, `review.py` |
| A model-backed agent or prompt | `src/mailroom_reloaded/agents/`, `prompts/`, `config/taxonomy.yaml` |
| API / read surface | `src/mailroom_reloaded/api/` |
| Browser UIs | `src/mailroom_reloaded/api/ui/` (`/ui`), `src/mailroom_reloaded/api/tui/` (`/tui`) |
| Sandbox server / Correspondent / triage / LLM Correspondent / Boss Desk / mailbox | `src/mailroom_reloaded/sandbox/server/` |
| Content contracts / loader / pin | top-level `schemas/`, `src/mailroom_reloaded/sandbox/content/`, `sandbox/content.lock` |
| Evaluation / cards / calibration | `src/mailroom_reloaded/eval/`, `cli.py` (`eval`, `card`, `train-gate`, `conformance`) |
| Observability / replay / ledger | `src/mailroom_reloaded/obs/`, `storage/{ledger,span_store,retention,anchor}.py` |
| Deploy / compose / Modal | `deploy/` |

## 4. Directory map

| Path | What lives here |
| --- | --- |
| `src/mailroom_reloaded/` | The package: `settings.py`, `cli.py`, `watcher.py`, `review.py`, `tools.py` and the subpackages below. |
| `src/mailroom_reloaded/agents/` | sorter, specialists, gate, judge, arbiter, boss, jev. |
| `src/mailroom_reloaded/api/` | FastAPI `/v1` + `/ui` (`app.py`, `ui/`), and the `/tui` browser terminal (`tui/`). |
| `src/mailroom_reloaded/eval/` | Dataset loader, runner, metrics, cards, cost, vLLM telemetry, gate/calibration fitting, conformance. |
| `src/mailroom_reloaded/ingest/` | `clerk.py`, `pdf.py`, `vision.py`, `bert.py` (ModernBERT adapter + handoff). |
| `src/mailroom_reloaded/intake/` | Gmail attachment intake (`gmail.py`). |
| `src/mailroom_reloaded/llm/` | Provider resolution + structured calls, retry, usage, tool loop. |
| `src/mailroom_reloaded/obs/` | OpenTelemetry + OpenInference tracing, metrics, run context, scores. |
| `src/mailroom_reloaded/pipeline/` | `state.py`, `flow.py` (`MailroomFlow`), `guards.py`, `report.py`, `archivist.py`. |
| `src/mailroom_reloaded/prompts/` | Frozen prompts: `frozen_v1/`, `sand37/`, sorter/judge/boss/arbiter, `loader.py`, `lineage.json`. |
| `src/mailroom_reloaded/sandbox/` | `content/` (loader, compat, lock, bundle, CLI), `server/` (offline ingress sim; `triage.py` scored intent triage, `llm_correspondent.py` optional loopback LLM Correspondent, `synthetic.py` placeholder PDFs, `conformance.py` + LOFO, `mailbox.py`), `fixtures/` (smoke + vendored policy), `showcase/`. |
| `src/mailroom_reloaded/schemas/` | Extraction response schemas, manifest, audit, ledger models. |
| `src/mailroom_reloaded/scoring/` | Vendored `llm-dojo-scoring` v0.21.0 subset (`PARITY.md`); lint-excluded. |
| `src/mailroom_reloaded/storage/` | Bins, SQLite DB/catalog/audit, ledger, span store, retention, anchor. |
| `src/mailroom_reloaded/config/` | `taxonomy.yaml` — classes, field types, bands, agents, maps. |
| `schemas/` | JSON Schema contracts (scenario v2, registry v1, overlay, gen_spec, persona_behavior, enums, `content_files.json`). |
| `sandbox/content.lock` | The content pin (repo/tag/commit/sha256/schema_version/dataset_revision). |
| `scripts/` | Dev/sandbox/smoke helpers and JEV harvest tools (see §5). |
| `tests/` | Tiered test suite (see §6). |
| `deploy/` | Dockerfiles, compose files (base/dev/sandbox), OTel collector, Prometheus, Grafana, Modal vLLM. |
| `docs/` | Operator + design docs; `docs/superpowers/{plans,specs,reviews}`. |
| `data/` | Local runtime state (gitignored): bins, SQLite, manifests, models, runs. |
| `DISCUSSION_BOARD.md` | Agent work log: what landed, files, evidence, commit SHA. |
| `CHANGELOG.md` | Keep-a-Changelog `[Unreleased]` + release sections. |
| `Makefile` | Thin wrappers over `scripts/` and `uv` (see §5). |
| `pyproject.toml` | Dependencies/extras, `mailroom` console script, pytest/ruff config. |

## 5. Scripts & commands

### 5.1 `Makefile` targets (verbatim)

| Target | Wraps |
| --- | --- |
| `make` / `make help` | prints the target list (default goal) |
| `make dev` | `scripts/dev.sh up` |
| `make dev-down` | `scripts/dev.sh down` |
| `make dev-logs` | `scripts/dev.sh logs` |
| `make dev-ps` | `scripts/dev.sh ps` |
| `make dev-status` | `scripts/dev.sh status` |
| `make dev-reset` | `scripts/dev.sh reset` |
| `make dev-test` | `scripts/dev_test.sh` |
| `make sandbox` | `scripts/sandbox.sh run` |
| `make sandbox-down` | `scripts/sandbox.sh down` |
| `make sandbox-status` | `scripts/sandbox.sh status` |
| `make lint` | `uv run ruff check .` |
| `make test` | `uv run pytest` |
| `make smoke` | `scripts/dev.sh smoke` |
| `make eval` | `uv run mailroom eval` |
| `make gmail` | `uv run mailroom gmail` |

### 5.2 `mailroom` CLI groups (defined in `src/mailroom_reloaded/cli.py` + the two sandbox CLIs)

Console script `mailroom = mailroom_reloaded.cli:main` (`pyproject.toml`). There
is **no `mailroom tui` subcommand** — `/tui` is a browser route served by the
API (see §7.5).

| Command | Purpose |
| --- | --- |
| `mailroom serve [--host] [--port] [--watch/--no-watch]` | FastAPI app + `/ui`, optionally with the embedded watcher. |
| `mailroom watch [--worker-id] [--concurrency 1..32]` | Standalone inbox watcher. |
| `mailroom run <file> [--worker-id]` | One document through the pipeline; prints `doc_id`/`status`/`doc_type`/`route_trail`. |
| `mailroom eval [flags]` | Evaluation posture; prints its `run_id`. Flags: `--revision`, `--per-class`, `--seed`, `--classes`, `--concurrency`, `--posture-label`, `--gpu`, `--gpus`, `--prompt-set`, `--merger-mode`, `--mode pipeline\|specialist_cell`, `--judge-sample-rate`, `--split`, `--local-dir`, `--gpu-usd-per-hour`, `--bert-manifest`. |
| `mailroom train-gate --rows R [--out] [--calibration]` | Fit the route gate or sorter temperature calibration from JSONL. |
| `mailroom card --run-id ID [--doc-type T] [--master] [--out]` | Write SAND-37 cards (repeat `--run-id` for the master). |
| `mailroom conformance [--provider] [--per-class] [--revision] [--split] [--local-dir] [--out]` | Behavioural conformance suite + card. |
| `mailroom audit verify [--run ID] [--external]` | Verify the archive ledger chain (exit 0/1/3/4/5). |
| `mailroom audit anchor` | Push the ledger head to the external anchor now. |
| `mailroom audit export-head [--out/-o PATH]` | Print the ledger head JSON for off-host pinning. |
| `mailroom jev decide --state S --type choice\|noul\|score --instructions I [--criteria K=D] [--criteria-list A,B]` | Ask Jev one typed question. |
| `mailroom jev calibrate --rows R [--out]` | Fit the Jev temperature + thresholds. |
| `mailroom gmail auth` | OAuth installed-app flow; caches a token. |
| `mailroom gmail poll [--limit] [--process/--no-process] [--worker-id]` | Fetch Gmail attachments into `inbox/`. |
| `mailroom gmail watch [--interval] [--limit] [--process/--no-process] [--worker-id]` | Poll Gmail on an interval until Ctrl-C. |
| `mailroom sandbox serve [--host] [--port] [--content smoke\|locked\|<dir>] [--data-dir] [--egress closed\|egress] [--autonomy human\|sandbox] [--expected/--no-expected]` | Offline ingress sandbox. |
| `mailroom sandbox conformance [--content] [--data-dir] [--json] [--only ID]... [--heldout] [--slim] [--lofo PATH]` | Isolated-scenario conformance + LOFO tables. |
| `mailroom sandbox content status [--lock]` | Pin, smoke version/tag match, validity, scenario count. |
| `mailroom sandbox content validate [path]` | Validate a content dir (default: committed smoke). |
| `mailroom sandbox content pull (--from-bundle \| --from-dir \| --url) [--allow-network] [--dest] [--lock]` | Materialize pinned content. |
| `mailroom sandbox content build [--from-dir] [--out]` | Validate content and regenerate smoke fixtures via its `tools/export_smoke.py`. |
| `mailroom sandbox content bump --bundle B --tag T --commit C [--lock]` | Rewrite `content.lock` for a new bundle. |

### 5.3 `scripts/`

| Script | Commands / modes |
| --- | --- |
| `scripts/dev.sh` | `up`, `down`, `logs`, `ps`, `reset`, `status`, `smoke` (docker-compose dev stack). |
| `scripts/dev_test.sh` | `[--live] [--no-lint] [-- <pytest args>]`; the only entry point that runs `uv sync`. |
| `scripts/sandbox.sh` | `up [--expose]`, `down`, `status`, `logs`, `run`, `smoke`, `reset`. |
| `scripts/sandbox_lofo.sh` | `<content dir> [out_dir]` — leave-one-family-out conformance report. |
| `scripts/smoke.sh` | End-to-end compose smoke (copies a fixture into the app inbox; prints `SMOKE OK`). |
| `scripts/tui_dev.sh` | `up`, `down`, `status` — host-only mock stack for `/tui` (no Docker). |
| `scripts/tui_replay_check.mjs` | Headless-browser check of the replay viewer. |
| `scripts/jev_harvest.py` | Harvest Jev answers (`docs` default; `features` mode over production `GateFeatures`). |
| `scripts/jev_export_gate_features.py` | Export `eval_docs.gate_features` + GT labels for the features harvest. |
| `scripts/jev_dev_rows.py` | Deterministic synthetic Jev calibration rows for the dev stack. |
| `scripts/dev_env.py` | Safe `.env` parser used by `scripts/dev.sh` (no shell execution/expansion). |
| `scripts/tui_seed/`, `scripts/tui_seed_jev/` | Seed documents copied into the TUI dev inbox. |

## 6. Test tiers

`pytest` (auto async mode) + `ruff`. Config lives in `pyproject.toml`:
`testpaths = ["tests"]`, `asyncio_mode = "auto"`,
`addopts = "--strict-markers -m 'not live and not docker'"`, and two registered
markers, `live` and `docker`.

| Tier | Directory | Covers |
| --- | --- | --- |
| Agents | `tests/agents/` | sorter, specialists, gate, CrewAI judge/arbiter/boss |
| Pipeline | `tests/pipeline/` | flow routes, guards/state, report + archive |
| Eval | `tests/eval/` | dataset loader, runner, metrics/cards, train-gate |
| Observability | `tests/obs/` | spans, GenAI attrs, masking, metric names |
| API | `tests/api/` | TestClient `/v1` routes, auth, UI |
| Deploy | `tests/deploy/` | compose config, collector config, Modal config |
| Intake | `tests/intake/` | Gmail attachment decoding/ingest |
| Ingest | `tests/ingest/` | clerk/pdf/vision, BERT adapter |
| LLM | `tests/llm/` | client resolve/structured calls, tooling, retry |
| Scoring | `tests/scoring/` | vendored dojo subset + upstream parity |
| Schemas/Storage | `tests/schemas/`, `tests/storage/` | extraction schemas, audit chain, bins |
| Sandbox | `tests/sandbox/` | content loader/pin, sandbox API, Correspondent, triage (`test_server_triage.py`), LLM Correspondent (`test_server_llm_correspondent.py`), ingress/egress, conformance, mailbox, UI |
| TUI | `tests/tui/` | Python engine/stations tests; JS under `tests/tui/js/` via `node --test` |
| Top-level | `tests/test_*.py` | dependency fence, prompt lock, settings, tools, watcher/review |

Run:

```bash
uv sync --extra dev                       # core + pytest/ruff
uv run pytest -q                          # live deselected by addopts
uv run pytest tests/pipeline -v           # one tier
MAILROOM_LIVE=1 uv run pytest -m live -v  # live tests (needs a configured provider)
uv run ruff check .                       # lint (vendored scoring excluded)
node --test tests/tui/js/*.test.mjs       # TUI JS unit tests
uv run pytest tests/test_dependency_fence.py -q
```

- **`live` marker.** Tests that hit a real provider are `@pytest.mark.live` and
  are deselected by default; `-m live` (with `MAILROOM_LIVE=1`) selects them.
- **`docker` marker.** `tests/deploy/test_docker_smoke.py::test_docker_smoke_script`
  runs `scripts/docker_smoke.sh` (builds images); deselected by default, `-m docker`
  selects it and it skips when the daemon or registry is unavailable.
- **`parity` extra.** The scoring-parity test skips unless
  `llm-dojo-scoring` v0.21.0 is installed (`uv sync --extra parity`).
- **Dependency fence.** No `langgraph`, `langchain*`, `langfuse`, `braintrust`
  or `litellm` under `src/` or in any dependency.
- **Prompt lock.** `tests/test_prompts.py` verifies every vendored prompt's
  sha256 against `lineage.json`.
- **Extras:** `bert`, `eval`, `gmail`, `embeddings`, `deploy`, `anchor`,
  `sandbox`, `dev`, `parity` (`pyproject.toml`). Never `uv sync --all-extras`
  (torch).

## 7. Workflows

### 7.1 Dev server stack (Docker)

```bash
scripts/dev.sh up        # or: make dev   — mock provider, no GPU
scripts/dev.sh status
scripts/dev.sh logs
scripts/dev.sh smoke     # upload a fixture, wait for "archived"
scripts/dev.sh down      # or: make dev-down
```

Brings up `app` (`--reload`), a **separate** `watcher`, the `mock` provider,
`otel-collector`, `phoenix`, `prometheus`, `grafana`. All ports loopback.
Never run the embedded watcher and the standalone watcher against the same data
dir (the `watcher.lock` flock admits one draining process).

### 7.2 Offline sandbox (smoke vs locked content)

```bash
scripts/sandbox.sh run                 # or: make sandbox — host mode, uv, :8100
scripts/sandbox.sh smoke               # inject A1 + E1, print trace summary
SANDBOX_CONTENT=locked scripts/sandbox.sh run
```

- `smoke` = committed fixtures under `src/mailroom_reloaded/sandbox/fixtures/smoke/`.
- `locked` = the `content.lock` pull result (verify + point at it with
  `SANDBOX_CONTENT`; the server never downloads).
- Network is blocked while it runs (`sandbox/server/guard.py`); outbound mail is
  captured, never sent. See `docs/SANDBOX_SERVER.md` for what is real vs stand-in.

### 7.3 Evaluation posture

```bash
uv run mailroom eval --classes contract,merger_agreement --per-class 10
uv run mailroom eval --mode specialist_cell --gpus 2 --concurrency 32 --prompt-set sand37
uv run mailroom train-gate --rows rows.jsonl --out models/route_gate.json
```

Fit the gate/calibration on `split=train` only; report KPIs on `test`
(`docs/EVALUATION.md`). Write cards with `mailroom card`; serve them from
`<base_dir>/runs/<run_id>/cards/`.

### 7.4 Gmail intake

```bash
uv run mailroom gmail auth
uv run mailroom gmail poll --limit 25 --process
uv run mailroom gmail watch --interval 30 --process
```

Needs the `gmail` extra; OAuth token/state live under the base dir
(`docs/gmail-intake.md`). The Pub/Sub push route is `POST /v1/intake/gmail`.

### 7.5 TUI (`/tui`) and web UI (`/ui`)

- `/tui` is a browser terminal served by the API (`src/mailroom_reloaded/api/tui/`),
  static ES modules, no build step. Commands include `ls`, `inspect`, `audit`,
  `jev`, `review`, `resolve`, `runs`, `ledger`, `replay`, `inbox`, `cards`, `auth`
  (`docs/TUI.md`).
- `/ui` is the vanilla-JS runs/documents page (`api/ui/index.html`).
- Host harness: `scripts/tui_dev.sh up|down|status` (mock LLM + API + embedded
  watcher; state under `data/tui-dev/`). `JEV=1` also starts/calibrates the mock
  Jev.

### 7.6 Boss mailbox

A named, durable, two-way Correspondent↔Boss channel stored in the sandbox data
dir as `boss_mailbox.sqlite` (`src/mailroom_reloaded/sandbox/server/mailbox.py`;
wired in `service.py`). Entries are append-only (DB triggers reject UPDATE and
DELETE); status changes (`new → read → acted | expired`) go to a separate
append-only status log and are mirrored as `mailbox.*` events. Reading it as an
operator changes nothing.

Sandbox API surface (`/api/sandbox/v1`, bearer-token gated when
`MAILROOM_API_TOKEN` is set):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/boss/mailbox` | List entries (filters: `direction`, `role`, `thread`, `message`, `status`, `kind`, `since`, `limit`); never marks read. |
| GET | `/boss/mailbox/{entry_id}` | One entry + status history; 404 if absent. |
| GET | `/boss/pending` | Pending review cases + count; no state change. |
| GET | `/boss/decisions` | Non-pending review cases + count. |
| POST | `/boss/decisions` | Record a decision (`legitimate` / `quarantine`, optional `category`) and apply release/quarantine effects; 404/409/403 as documented. |

The sandbox `/ui` exposes a mailbox panel (`sandbox/server/ui/mailbox.js`).
Tests: `tests/sandbox/test_server_boss_review.py`,
`tests/sandbox/test_server_ui_mailbox.py`.

### 7.7 Held-out scenarios

A held-out scenario is one the Correspondent was not tuned against; its pass
rate is the honest generalization number. Protocol: `docs/HELD_OUT_SCENARIOS.md`.

- Naming: the **`H` series**, `^[A-HST][0-9]+_[a-z0-9_]+$` (e.g.
  `H1_heldout_status_confirm`); `schemas/scenario.v2.json` reserves `H` for this.
- Tag every held-out scenario `heldout` and freeze it (`status: frozen` + a seed).
- Run only the frozen batch and report the held-out rate + LOFO:

```bash
uv run --extra sandbox mailroom sandbox conformance --content <pack> --heldout
uv run --extra sandbox mailroom sandbox conformance \
  --content <pack> --heldout --json /tmp/heldout.json --slim
```

Without `--heldout`, the harness falls back to the positional split
(`index % 3 == 2`), which is **not** a clean measure once those scenarios have
been read. Template:
`tests/sandbox/examples/scenario_H1_template.yaml`. A HELD-OUT RATE IS NOT
AVERAGED WITH TUNING-FAMILY RATES; report the counts and the freeze provenance
(commit SHA, author, unchanged since).

## 8. Conventions

- **Python.** `requires-python = ">=3.11,<3.13"`. Keep `ruff check .` clean.
  The vendored `src/mailroom_reloaded/scoring/` files are **excluded from lint
  and must keep upstream bytes** (see `scoring/PARITY.md`).
- **Tests.** `pytest-asyncio` in **auto mode** (no `@pytest.mark.asyncio`
  needed). Use the registered markers only (`--strict-markers`); mark live tests
  `live`. `tests/conftest.py` clears the settings/taxonomy `lru_cache` around
  every test, so do not cache config across tests.
- **Commit messages (observed, not enforced).** The repo mixes **Conventional
  Commits scopes** with plain imperative subjects:
  - `feat(<scope>): …`, `fix(<scope>): …`, `test(<scope>): …`, `docs: …`,
    `docs(<scope>): …` — scopes seen: `tui`, `sandbox`, `replay`, `obs`, `jev`,
    `anchor`, `metrics`, `ledger`, `deploy`, `board`, `plans`, `review`, `llm`.
  - Plain subjects like `Add /ui replay links and #replay= deep link in /tui`,
    `Replay viewer: …`, `Generate docstrings for PR #28`.
  - Merge commits: `Merge pull request #N from Exios66/<branch>`.
  - There is **no commitlint/CI gate** observed. Follow the dominant pattern:
    a scoped prefix for typed changes, a plain imperative subject otherwise; a
    body explaining the "why"; `Co-Authored-By:` / `Claude-Session:` trailers
    where the harness adds them.
- **Changelog.** `CHANGELOG.md` follows Keep a Changelog; behavior-changing
  work adds its `[Unreleased]` entry **in the same change**, naming files and
  the numbers that motivated it. Docs-only changes declare themselves as such.
- **Content pinning.** `sandbox/content.lock` is the single pin; consumers never
  track a branch. Schema MAJOR must match `content.json.schema_version`; a
  consumer refuses a mismatched major. Change the pin only via
  `mailroom sandbox content bump` (never by hand).
- **Targeted staging.** Stage only the files in the card's scope; never
  reformat or reflow unrelated files.
- **No GitHub Actions assumptions.** Do not assume a CI runner exists for your
  change; run the tiers locally (`scripts/dev_test.sh`, or `uv run pytest` +
  `uv run ruff check .`) and cite the output.
- **Timestamps.** UTC ISO-8601 (`%Y-%m-%dT%H:%M:%SZ`) in evidence and Owner
  cells; bare dates are UTC by convention.
- **Derived artifacts.** Never hand-edit generated/derived files — regenerate
  them (e.g. smoke fixtures via `mailroom sandbox content build`).

### 8.1 Git, branches, PRs and review

- **Branches.** `main` only long-lived. Feature branches are `feat/<topic>`,
  `fix/<topic>` or `docs/<topic>`; agent sessions use the `claude/<topic>`
  branch the harness names. Never push to a branch you were not assigned; never
  force-push or rewrite another session's branch (merge `main` in instead).
- **Stacked PRs.** Base a PR on its parent's branch and say so in the PR
  template (`stack_parent`). After the parent merges, retarget the child to
  `main`, merge `main` into it, and re-run the gates before merging it.
- **Commits.** Scoped prefix per §8, body explaining why. Agent commits end with
  the trailers the harness supplies (`Co-Authored-By:`, `Claude-Session:`).
  Never put a model identifier in a commit, PR, code comment or changelog.
- **Merging.** Merge commits (no squash, no rebase) via the GitHub API with the
  full 40-character `expectedHeadSha`. A PR is mergeable only when its gates
  pass locally, the `[Unreleased]` entry exists, and every CodeRabbit finding is
  either applied or answered (below).
- **Merge state.** Verify per branch, never from a summary: after
  `git fetch --all --prune`, run `git merge-base --is-ancestor origin/<branch>
  origin/main` (exit 0 = merged) or `git branch -r --merged origin/main`. A note,
  plan or PR list is not evidence.
- **CodeRabbit.** Do not wait for it to finish when it is overloaded. Apply each
  finding unless it is flawed; verify against current code first (trace a real
  caller to the failure). Reply on a thread only to explain why a finding is not
  applied. Nits and optional findings ride along with the next real push.
  Salvage stranded tests from `coderabbit/*` branches into a normal PR; never
  merge those branches directly.
- **Comments on GitHub** are rare and end with the footer
  `---` then `_Generated by [Claude Code](https://claude.ai/code)_`. PR
  descriptions end with the harness attribution lines.
- **Branch pruning.** Delete a remote branch only after verifying it is merged
  into `origin/main` (or that its useful content is salvaged and cited). Never
  delete the branch of an open PR.
- **Workers.** An orchestrating agent may run workers in parallel (medium-effort
  Sonnet or high-effort Haiku, effort set explicitly). Workers edit and test but
  never touch git; one adversarial reviewer vets the diff before the
  orchestrator commits.

### 8.2 Issue forms and the PR template (machine-readable)

Forms live in `.github/ISSUE_TEMPLATE/*.yml`; blank issues are disabled. GitHub
renders each field as a `### <Label>` heading in the issue body, so agents can
parse an issue by heading. Field ids are stable; do not rename them.

| Form | Label applied | Field ids |
| --- | --- | --- |
| `bug_report.yml` | `bug` | `area`, `observed`, `expected`, `reproduce`, `version`, `suspected_paths` |
| `feature_request.yml` | `enhancement` | `problem`, `proposal`, `alternatives`, `acceptance_criteria`, `plan_item` |
| `agent_task.yml` | `agent-task` | `goal`, `scope_paths`, `out_of_scope`, `acceptance_criteria`, `gates_to_run`, `constraints`, `autonomy_level`, `definition_of_done` |
| `docs_drift.yml` | `documentation` | `doc_path`, `claim`, `reality`, `authoritative` |
| `follow_up.yml` | `follow-up` | `source`, `finding`, `why_deferred`, `severity` |

- **Working an `agent-task` card:** touch only `scope_paths`, never
  `out_of_scope`; do not exceed `autonomy_level` (`read-only analysis` < `edit
  locally, no git` < `branch + commit` < `branch + commit + open PR`); run the
  listed gates; finish with the `definition_of_done` report.
- **Deferred review findings** become `follow_up` issues (or entries in the
  master plan's follow-up list), not silent TODOs.
- **PR template.** `.github/pull_request_template.md` ends with a fenced
  `agent-report` YAML block (`schema: 1`) with `change_type`, `scopes`,
  `linked_issues`, `plan_items`, `stack_parent`, `gates`
  (`ruff`, `pytest`, `pytest_sandbox`, `node_test`, `browser_check`: each
  `pass|fail|skipped`), `coderabbit`, `not_verified`, `follow_ups`. Keep the exact
  shape, fill every key (`null` or `[]` when empty), and never mark a gate
  `pass` you did not run. The content repo uses the same block shape with its own
  gate keys. A PR that changes root `schemas/` must state the content-repo mirror
  follow-up (K-05) and the merge order (**this repo first, then the content
  mirror**), so `tools/check_schema_drift.py` in the content repo clears.
- **Gates for this repo:** `ruff check src tests`;
  `PYTHONPATH=src pytest -p no:cacheprovider tests -q --ignore=tests/sandbox`;
  `pytest tests/sandbox -q` for sandbox changes;
  `node --test tests/tui/js/*.test.mjs` (and `tests/sandbox/js/*.test.mjs`);
  `scripts/tui_replay_check.mjs` for TUI changes; `scripts/docker_smoke.sh`
  (optional, only when `deploy/**` or a Dockerfile input changes; prune the build
  cache afterwards on a small disk). There are no GitHub Actions workflows (the
  owner's account cannot run them; `docker-smoke.yml` was removed 2026-10-10), so
  there is no hosted CI: paste the result lines into the PR.

## 9. Evidence contract

Every deliverable carries evidence that a reviewer can re-run.

- **Cite exact paths, commands and test names.** No "should work" without a run.
  State the command and the result line.
- **Classify every finding** as one of: *harness* (agent tooling/config) |
  *vendored upstream* (pinned third-party bytes/code) | *operator/env* (missing
  key, no Docker, no provider) | *model* (LLM behaviour).
- **Docs changes:** show the drift sites before/after and the gate command that
  proves the docs now match reality (reference audit, a `--check`/validate run,
  a content `validate`).
- **Board work:** show the card's lane/Owner/Evidence before/after, the board
  check result, and (for synced cards) the issue link.
- **Test claims:** name the tier and the command, not just a count; a green
  number without the command is not evidence.
- **Ledger/audit claims:** `mailroom audit verify` (and `--external` when the
  anchor is in scope); report exit codes.
- **If you cannot run commands,** list the exact commands and what a passing run
  looks like — never imply you ran them.

## 10. Subagent delegation roster

One specialist per concern. The caller **owns and ships** the work; the
specialist produces a bounded deliverable with its own evidence. Brief like a
card (scope, files, definition of done, evidence). Chain specialists, never
impersonate one; verify upstream docs before writing config.

> No in-repo subagent registry exists yet; the names below are the canonical
> roster established by this file. When a harness owns its own roster ids, map
> them here rather than forking new names.

| Task type | Specialist subagent | Trigger surface | Deliverable | Evidence contract | Boundaries |
| --- | --- | --- | --- | --- | --- |
| Planning + dispatch of a multi-step mission | `orchestrator-governor` | "plan this", multi-file/multi-specialist work, a new card | Decomposed card(s): scope, owner, sync units, definition of done | The plan + which specialists were dispatched and why | Plans and delegates; does not implement specialist code itself |
| Sandbox server, API, TUI, board site (software/UI) | `software-ui` | `sandbox/server/`, `api/`, `api/ui/`, `api/tui/`, board-site assets | Code + tests; UI/JS kept textContent-only | The touched tier(s) green (`tests/sandbox`, `tests/api`, `node --test tests/tui/js/*`) | No prompt/model or credential changes |
| Repo hygiene, packaging, mirrors, sync units | `systems-repo-hygiene` | drift reports, package sync, wheel/packaging, path/ref rot | Clean `git status` for scope; sync evidence; regenerated artifacts | `git status --short`, sync `status`, `uv build`/import check | Never hand-edit a mirror; propagate only via the sync tooling |
| Docs + board | `docs-board` (atom) | README/AGENTS/`docs/*`, `CHANGELOG.md`, `DISCUSSION_BOARD.md`, wiki | Doc/changelog/board edit matching reality | Before/after drift sites + the gate command; board lane/Owner/Evidence | Docs/board only; never edits code to make docs true |
| Code analysis / architecture tracing | `code-analysis` | "how does X route?", a change with unclear blast radius | A cited map of the code path (file:line) and risks | The traced path + any counterexample | Read-only; does not ship code |
| Test-suite auditing / adversarial review | `test-suite-auditor` | new suite, coverage gap, "review this PR", vacuous-pass risk | Findings with a verdict (accept/revise) + missing-test list | Each claim reproduced by a named test/command; no fabrication | Reviews; the caller applies fixes |
| Data + databases (SQLite, schemas, storage) | `data-databases` | `storage/`, `schemas/`, migrations, chain/queries | Schema/query change + tests; back-compat note | `tests/schemas`, `tests/storage`, chain-verify output | No live data loss; derived files regenerated, not hand-edited |
| Hugging Face datasets / data science / eval | `hf-data-science` | dataset revisions, eval metrics/cards, calibration fitting | Reproducible eval/calibration result + card | The exact eval/train command, revision pin, and numbers | Fit on `train` only; never leak labels to the blind path |
| vLLM / serving / GPU | `vllm-serving` | serving config, model map, GPU telemetry, cost | Serving config + telemetry wiring | Config that `config -q`/import-loads; telemetry names present | No provider-key handling (hand off to `provider-credentials`) |
| Modal / deploy / compose / Docker | `modal-deploy` | `deploy/`, `deploy/modal_vllm.py`, compose profiles | Deploy config + runbook delta | `docker compose config -q`, a documented smoke result | No secret values in files; reference env vars only |
| Prompt engineering / eval loop | `prompt-engineer` | `prompts/`, frozen sets, `lineage.json`, prompt lock | Prompt/spec change + the lock hash update | `tests/test_prompts.py` green; eval delta or a stated reason | Changing a frozen prompt is a new set, never an in-place edit |
| Security / privacy | `security-privacy` | auth surfaces, network guard, masking, traversal/upload paths | Threat findings + minimal fixes | Reproduced failure + the test that now guards it | No weakening of token/off-loopback/masking guarantees |
| Provider credentials / secrets | `provider-credentials` | keys, `.env`, compose credentials, key resolution | Correct precedence + redaction; no values in files | A no-network/redaction proof; `git status` shows no secret | Never commit or print a secret; never inline a key in code |
| Trace / observability | `trace-observability` | `obs/`, `storage/{ledger,span_store,retention,anchor}.py`, replay | Span/metric/ledger change + names verified | `tests/obs` + a ledger/replay verify command | Never record prompts, completions or document text in spans |

Dispatch laws:

1. **One specialist per concern.** Do not ask one specialist to do another's
   job; chain them.
2. **Brief like a card.** Give scope, files, definition of done, evidence.
3. **The caller owns and ships the work.** A specialist's output is an input to
   your deliverable, not a substitute for it.
4. **Verify upstream docs before writing config.** Do not guess an API shape.
5. **Chain, never impersonate.** Attribute a specialist's contribution; do not
   restate it as your own finding.

## 11. Do / Don't

**Do**

- Read `AGENTS.md` + the relevant `docs/` before editing.
- Prefer the mock/offline/mock-LLM path before live spend or deploy.
- Add the `[Unreleased]` changelog entry and the test with the behavior change.
- Keep docs, `AGENTS.md` and the board reconciled in the same change.
- Pin content via `content.lock`; propagate package work via the sync tooling.
- Run the touched tier(s) and cite the command + result.

**Don't**

- **Never commit secrets** (keys, tokens, `.env` values) or print them; use
  env vars and redaction.
- **Don't hand-edit the pulled content bundle** or rewrite `content.lock` by
  hand — use `mailroom sandbox content bump`.
- **Don't fork long-lived copies** of agent configs or mirrors without syncing
  them back to their home.
- **Don't weaken** the token gate, the off-loopback bind refusal, the network
  guard, or trace masking to make a test pass.
- **Don't run git write commands** unless the task asks; never force-push, skip
  hooks, or create empty commits.
- **Don't edit vendored `scoring/` bytes** or files lint-excluded on purpose.
- **Don't average a held-out rate with tuning-family rates**, and don't call the
  positional split "held-out" once `--heldout` exists.
- **Don't report a green suite without the command that produced it.**

## 12. Pointers

- [README.md](README.md) — overview, install, CLI/API tables, quickstart.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — pipeline, durability, audit, module map.
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every env var and `taxonomy.yaml` block.
- [docs/DEV_SERVER.md](docs/DEV_SERVER.md) — local dev-server workflow.
- [docs/EVALUATION.md](docs/EVALUATION.md) — dataset, train/test discipline, cards.
- [docs/JEV.md](docs/JEV.md) — the opt-in Jev decision model.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — watcher, review, audit/ledger, observability, failure modes.
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — clone-to-demo paths A/B/C.
- [docs/SANDBOX_CONTENT.md](docs/SANDBOX_CONTENT.md) — content/schema contracts, IDs, pinning.
- [docs/SANDBOX_SERVER.md](docs/SANDBOX_SERVER.md) — offline ingress simulation, real vs stand-in, API.
- [docs/HELD_OUT_SCENARIOS.md](docs/HELD_OUT_SCENARIOS.md) — held-out authoring protocol and measurement.
- [docs/TESTING.md](docs/TESTING.md) — tiers, `live` marker, dependency fence, parity.
- [docs/superpowers/plans/2026-10-09-mailroom-core-plan.md](docs/superpowers/plans/2026-10-09-mailroom-core-plan.md) — master plan: status, item ids, decisions.
- [docs/TUI.md](docs/TUI.md) — `/tui` commands, replay viewer, themes, auth.
- [docs/gmail-intake.md](docs/gmail-intake.md) — Gmail intake setup and limits.
- [deploy/README.md](deploy/README.md) — Modal vLLM deployment and teardown.
- [DISCUSSION_BOARD.md](DISCUSSION_BOARD.md) — agent work log (newest first).
- [CHANGELOG.md](CHANGELOG.md) — Keep a Changelog.
- [docs/superpowers/specs/2026-10-07-mailroom-reloaded-design.md](docs/superpowers/specs/2026-10-07-mailroom-reloaded-design.md)
  and [docs/superpowers/plans/](docs/superpowers/plans/) — design and plans.
