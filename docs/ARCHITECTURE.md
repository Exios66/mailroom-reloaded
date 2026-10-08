# Architecture

`mailroom-reloaded` is a CrewAI-Flows pipeline over a filesystem-backed,
crash-resumable journal. This page explains the pipeline, the two-LLM-call fast
path, durability, the audit chain, and where each module lives. For the
configuration surface see [CONFIGURATION.md](CONFIGURATION.md); for running and
operating it see [OPERATIONS.md](OPERATIONS.md).

## Pipeline

```
ingest ─▶ bert_primary ─▶ sort ─▶ gate_classify ─┬─▶ extract ─▶ gate_extract ─┬─▶ report ─▶ catalog ─▶ archive ─▶ [grade]
 (det.)      (local)      (LLM 1)                │   (LLM 2)                 ├─▶ retry_extract (≤ retry_max)
                                                 ├─▶ re_sort (FULL, once)    ├─▶ verify: judge ─▶ arbiter
                                                 └─▶ human_review (park)     ├─▶ boss (escalation)
                                                                             └─▶ human_review (park)
```

The canonical node order is `NODE_ORDER` in `pipeline/state.py:28-36`:
`ingest, bert_primary, sort, gate_classify, extract, gate_extract,
report_catalog_archive`. `gate_classify` and `gate_extract` are pure route
computations (no LLM call, not persisted as completed work); `verify`, `boss`,
`human_review` and the eval-only `grade` are dispatched by the driver outside
that tuple.

The flow is a genuine `CrewAI Flow[MailroomState]` (`pipeline/flow.py:91`), but
control flow is owned by a deterministic driver, `MailroomFlow._drive_nodes`
(`pipeline/flow.py:572-656`), not CrewAI's event engine. The `@start`/`@listen`/
`@router` methods exist so the class is routable/plottable and delegate to the
same guarded node implementations (`pipeline/flow.py:1-17` docstring). Reasons
for the explicit driver: CrewAI rejects the literal self-loop wiring, and
crash-resume/deadline enforcement need a single deterministic traversal.

### Nodes

| Node | Kind | What it does | Source |
| --- | --- | --- | --- |
| `ingest` | deterministic | Clerk-normalise text; PDF text layer (pypdf→pdfplumber→pymupdf), else vision transcription; unsupported/empty → fail | `ingest/clerk.py:144-174` |
| `bert_primary` | local model | ModernBERT classify + handoff policy; fail-open to `FULL` | `ingest/bert.py:44-149` |
| `sort` | **LLM call 1** | Standalone structured sorter (no CrewAI agent) | `agents/sorter.py:161-214` |
| `gate_classify` | deterministic | Route gate decision for the classify stage | `pipeline/flow.py:386-409` |
| `extract` | **LLM call 2** | Class specialist structured extraction | `agents/specialists.py:248-315` |
| `gate_extract` | deterministic | Route gate decision for the extract stage | `pipeline/flow.py:411-439` |
| `verify` | LLM | `judge_verify` then `arbitrate` (live mode, no ground truth) | `pipeline/flow.py:209-217` |
| `boss` | LLM | Escalation / class reassignment | `pipeline/flow.py:219-233` |
| `report_catalog_archive` | deterministic | Compile report, move to `archive/`, upsert catalog | `pipeline/report.py:74-103`, `pipeline/archivist.py:54-79` |
| `human_review` | deterministic | Park in `review/`, status `parked` | `pipeline/flow.py:365-383` |
| `grade` | LLM (eval only) | Grade extraction against ground truth; never fails the doc | `pipeline/flow.py:260-278` |

## The two-LLM-call deterministic fast path

Success criterion 5 of the design
(`docs/superpowers/specs/2026-10-07-mailroom-reloaded-design.md:15`):
reviewer LLM nodes are gone; routing is a deterministic gate. A clean text-layer
document therefore costs exactly **two LLM calls**:

1. `sort` — the sorter, one `call_structured` call (`agents/sorter.py:171-177`).
   `ingest` and `bert_primary` are local: `_drive_nodes` walks the main path with
   `_NEXT = {ingest→bert_primary→sort→gate_classify→extract→gate_extract}`
   (`pipeline/flow.py:72-77`). `_node_sort` increments the logical `_llm_calls`
   counter (`pipeline/flow.py:176-185`).
2. `extract` — the class specialist, one structured call per window
   (`agents/specialists.py:248-315`); `_node_extract` increments `_llm_calls`
   (`pipeline/flow.py:187-207`).

`report`, `catalog` and `archive` make no LLM calls (`pipeline/report.py:74-103`,
`pipeline/archivist.py:54-79`). The gate is `agents/gate.py`, whose module
docstring is "Deterministic route gate (replaces the reviewer nodes). Zero LLM
calls." (`agents/gate.py:1`). `BandGate.decide` applies per-class thresholds
(`agents/gate.py:64-100`); a learned logistic model in
`<base_dir>/models/route_gate.json` may override the band only inside the medium
band (`agents/gate.py:114-175`). Tool rounds and vision/merger-window retries are
counted separately from the logical two calls (`pipeline/flow.py:240-241`).

The gate slot is pluggable. `load_gate()` (`agents/gate.py:169-180`) chooses the
deterministic `BandGate` by default, a `LearnedGate` when
`models/route_gate.json` exists, or — when the opt-in `MAILROOM_JEV_PROVIDER` is
set **and** `models/jev_calibration.json` is present — a `JevGate` backed by the
TypeSafe System One decision model (`load_jev_gate` in `agents/jev.py`). Jev overrides only
the medium confidence band and never a hard `rule` decision, so the deterministic
contract above is preserved whenever Jev is off (the default) or uncalibrated.
See [JEV.md](JEV.md).

The gate maps to driver targets: classify → `do_extract | retry_sort | re_sort |
human_review`; extract → `report | retry_extract | do_verify | do_boss |
human_review` (`pipeline/flow.py:404-439`). The arbiter route maps `accept` /
`accept_with_caveats` → report, `re_extract` → retry, `escalate` → boss
(`pipeline/flow.py:441-451`).

BERT fail-open: `classify_primary` returns an unavailable verdict for flag-off,
missing package/model, oversize, or any error, and `decide_handoff` then forces
`SortMode.FULL` (`ingest/bert.py:44-111`, `ingest/bert.py:138-149`). BERT
`defer_classes` (default `contract`, `merger_agreement`) and multi-window
verdicts also fall back to `FULL`.

## Bins and manifest durability

Bins are directories under the configured base dir, created on demand
(`storage/bins.py:22-58`): `inbox/`, `processing/<worker_id>/`, `classified/`,
`review/`, `failed/`, `archive/`, `manifests/`. The taxonomy's `pipeline.bins`
block is descriptive only; `Bins` hardcodes these names (`storage/bins.py:10`,
`storage/bins.py:34-58`).

- **Claim by atomic rename.** `Bins.claim` renames the file into
  `processing/<worker_id>/`; the loser of a race gets `None` and moves on
  (`storage/bins.py:60-68`). `Bins.move` relocates to a terminal bin
  (`storage/bins.py:70-76`).
- **Manifest.** `Manifest` records `doc_id`, `filename`, `content_sha256`,
  `status` (`processing|parked|failed|archived`), `completed_nodes` and the
  serialised state (`schemas/manifest.py:14-21`). `save_manifest` is an
  atomic temp-write + `os.replace` (`storage/bins.py:79-85`); `load_manifest`
  reads it back (`storage/bins.py:88-92`). `doc_id` is the first 16 hex of the
  file's content sha256 (`storage/bins.py:13-19`).
- **Per-node checkpoint.** `guarded(...)` wraps each node so that, after a
  successful call, the node is appended to `completed_nodes`, the state is
  snapshotted into the manifest, and a hash-chained audit entry is appended
  (`pipeline/guards.py:1-17`; `MailroomFlow._record_node`,
  `pipeline/flow.py:320-339`). If a node raises, the completed prefix is
  persisted before the exception surfaces (`pipeline/flow.py:306-310`).
- **Resume.** `next_node(manifest, NODE_ORDER)` returns the first node not in
  `completed_nodes` (`schemas/manifest.py:24-30`). On startup the watcher calls
  `resume_processing`, which re-runs every manifest still in status `processing`
  from that node (`watcher.py:121-149`); nodes already done are skipped by
  `_guard_node` (`pipeline/flow.py:291-292`).

The `split-watcher` Docker profile exists because `watcher.lock` (an exclusive
`flock`) admits only one draining process: `run_forever` raises `WatcherLockHeld`
if a second process holds it (`watcher.py:39-56`, `watcher.py:194-224`). The
embedded API watcher and the standalone `watcher` service would otherwise
conflict (see [OPERATIONS.md](OPERATIONS.md)).

## Hash-chained audit

The audit log is an append-only SQLite table (`audit_log`) keyed by
`(doc_id, seq)`; each entry stores `node`, `event`, a JSON `payload`, `ts`,
`prev_hash` and `entry_hash` (`storage/db.py:25-36`). `compute_entry_hash` is a
sha256 over the canonical JSON of the entry excluding `entry_hash`, with
`prev_hash` included in the hashed body (`schemas/audit.py:44-49`).

- `append` takes `BEGIN IMMEDIATE` to serialise concurrent appenders, retries on
  a seq collision, and is **resume-safe**: a repeat of `(doc_id, node, event)`
  with an identical payload returns the existing entry and writes nothing
  (`storage/audit_log.py:33-91`). This is why resumed or corrected runs do not
  duplicate chain entries.
- `entries(doc_id)` returns the chain ordered by `seq` (`storage/audit_log.py:94-100`).
- `verify_chain` returns the first seq where `seq`, `prev_hash` or
  `compute_entry_hash` fails (`storage/audit_log.py:103-116`). The chain detects
  edits, middle deletions and reordering; **tail truncation is undetectable
  without an external anchor** (documented at `storage/audit_log.py:44-45`).

Events written by the pipeline include `completed` per node
(`pipeline/flow.py:337-339`), `node_failed` (`pipeline/flow.py:357-362`),
`parked` (`pipeline/flow.py:382`), `archived` with the file sha256
(`pipeline/archivist.py:69-79`) and `review_resolved` (`review.py:76`, `review.py:93`).

## Storage layout under `MAILROOM_BASE_DIR`

```
<base_dir>/
  inbox/  processing/<worker>/  classified/  review/  failed/  manifests/
  archive/<doc_type>/<uuid>_<name>          # archived file
  archive/<doc_type>/<uuid>_<name>.report.json   # report sidecar
  mailroom.db                               # SQLite: audit_log, catalog, eval_docs
  models/route_gate.json                    # optional learned gate
  models/calibration.json                   # optional sorter temperature scaling
  models/jev_calibration.json               # optional Jev gate calibration (opt-in)
  runs/<run_id>/cards/*.json                # card JSON (served by GET /v1/runs/{id}/cards)
  gmail_state.json, gmail_credentials.json, gmail_token.json   # if Gmail intake is used
```

The SQLite schema is created lazily; the engine uses WAL and `busy_timeout=5000`
(`storage/db.py:56-73`). The **catalog** holds one row per `doc_id`
(`storage/db.py:38-50`), upserted best-effort after archive (`pipeline/flow.py:243-256`,
`storage/catalog.py:21-30`). The **eval runner** creates `eval_docs` itself
(`eval/runner.py:46-74`). Cards are written under `runs/<run_id>/cards/` (and the
master under `runs/master.*`) by `mailroom card`, and read back by the API
(`api/app.py:249-260`); see [EVALUATION.md](EVALUATION.md).

## Module map

```
src/mailroom_reloaded/
  settings.py           env (pydantic-settings) + taxonomy.yaml contract
  config/taxonomy.yaml  classes, field types, confidence bands, agents, maps
  schemas/              extraction.py (strict response_format), manifest.py, audit.py
  prompts/              frozen_v1/ (5 specialists + lineage.json), sand37/, sorter_v14.md,
                        judge_*.md, boss.md, arbiter.md, loader.py
  scoring/              vendored llm-dojo-scoring v0.21.0 subset + PARITY.md
  llm/                  client.py (provider resolve + structured calls), retry.py,
                        usage.py, tooling.py (tool loop + inline fallback)
  tools.py              one definition → CrewAI BaseTool and OpenAI function spec
  ingest/               clerk.py, pdf.py, vision.py, bert.py (adapter + handoff)
  agents/               sorter.py, specialists.py, gate.py, judge.py, arbiter.py, boss.py
  pipeline/             state.py, flow.py (MailroomFlow), guards.py, report.py, archivist.py
  storage/              bins.py, db.py, catalog.py, audit_log.py
  watcher.py            filesystem watcher + resume
  review.py             human-review resolution
  obs/                  tracing.py (OpenTelemetry + OpenInference), metrics.py
  api/                  app.py (FastAPI /v1 + /ui), ui/index.html
  eval/                 dataset.py, runner.py, metrics.py, cards.py, cost.py,
                        vllm_telemetry.py, train_gate.py
  cli.py                mailroom serve|watch|run|eval|train-gate|card|conformance|gmail
deploy/                 Dockerfile, docker-compose.yml (+ dev), otel-collector.yaml,
                        prometheus.yml, grafana/, llamafile/, modal_vllm.py
```

## Cross-links

- Configuration surface: [CONFIGURATION.md](CONFIGURATION.md)
- Opt-in Jev route gate: [JEV.md](JEV.md)
- Evaluation harness, cards and train/test discipline: [EVALUATION.md](EVALUATION.md)
- Watching, review, audit ops, observability and failure modes: [OPERATIONS.md](OPERATIONS.md)
- Tests and the dependency fence: [TESTING.md](TESTING.md)
- Gmail intake: [gmail-intake.md](gmail-intake.md) · local dev stack: [DEV_SERVER.md](DEV_SERVER.md)
