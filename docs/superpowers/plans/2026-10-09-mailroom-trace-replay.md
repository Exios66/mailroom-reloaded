# Mailroom Trace Replay Viewer Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Status:** draft for review (revision 4, 2026-10-09). This plan proposes work; nothing below is implemented yet.
>
> **Decisions so far:**
> 1. The track is drawn as a **character grid**, not a `<canvas>`.
> 2. Metrics get a **`run_id` label**, so the Grafana `$run_id` filter works and can link into the replay.
> 3. The traces must capture **all** data that could help the visualisation, in the style of the original `llm-mailroom`. That is everything `llm-mailroom` traced, everything `The-Mailroom` visualizer consumed, and the per-document and per-field scores from `llm-dojo-scoring`, plus the routing decisions the originals only ever logged.
> 4. **Capture everything, show a tier.** Every metric is recorded; the viewer, `/ui` and Grafana links show tiers T0/T1 by default (`--tier 2|3` reveals more). Capture and display are separate.
> 5. **Retention is a toggle.** Showcase runs (shipped) and pinned runs are hard-saved. Ordinary runs are not kept by default, to avoid a backlog. `MAILROOM_TRACE_KEEP=pinned|recent:<N>|all` switches this, also from the TUI.
> 6. **One archive ledger.** Every metric from traces and logs goes into a single hash-chained, auditable **archive ledger** that takes in all pipeline runs (live and eval) and is viewable and verifiable from the TUI (`ledger`). The ledger is never pruned; only heavy span data is.
> 8. **Reuse the hardened audit log; stay lean.** `storage/audit_log.py` is not modified. The ledger reuses its canonical-JSON hashing (`schemas/audit.py:44-49`, unchanged) and links to each document's chain by `(seq, entry_hash)`. Reserved budget: 8 new modules, 3 new tables, 5 env vars, no new dependency in the default install.
> 9. **Per-run row cap.** About 5,000 `ledger_metrics` rows per run; beyond that one `gap` entry and a dropped counter in `run_closed`.
> 10. **External anchor, user-selectable.** `MAILROOM_ANCHOR=none (default) | export | postgres | supabase` pushes the ledger head `(seq, entry_hash)` off-host so truncation and rewrite are detectable. Postgres and Supabase are first-class; `export` is for manual operator pinning.
> 11. **Showcase runs** shipped for new users: a clean run; one with retries and a boss escalation; one with parked and failed documents; one heavy on the judge and arbiter.
> 12. **Complete LLM usage.** Judge, boss, arbiter, vision (and eval grading) usage is captured per role, flows into `usage_total`, metrics, the report, `eval_docs`, the audit ledger and the replay (Task 19). `usage_complete` is true unless a node raised mid-call.
> 13. **Anchor defaults to HTTPS.** Supabase over PostgREST/httpx is the recommended backend, using a dedicated insert-only database role (never the service key); Postgres stays optional behind the locked `anchor` extra. `anchor_key` can come from an environment variable or a secrets file (`MAILROOM_ANCHOR_KEY_FILE`).
> 7. **`replay ↗` link in `/ui`** on each eval run, plus a `/tui#replay=run:<id>` deep link that actually opens the viewer.

**Goal:** Add `replay`, an alternative viewer launched from `/tui`. It plays a mailroom pipeline session back from captured OpenTelemetry spans (falling back to the audit log) as a live, interactive, scrubbable visualisation. It has play/pause, speed and seek, a station "track" with documents moving along it, a document leaderboard, an event ticker, a per-document inspector, pluggable insight panels, and a follow-live mode.

**Inspiration:**
- **[IAmTomShaw/f1-race-replay](https://github.com/IAmTomShaw/f1-race-replay):** a timeline with play/pause, 0.5–4× speed and seek; a track map with cars as dots; a live leaderboard; click-to-select driver telemetry; pluggable "pit wall" insight windows over a telemetry stream; and a processed-data cache.
- **`Exios66/The-Mailroom`**, the original visualizer:
  - an envelope floor whose stations are driven by span names and `output.stage`;
  - an inspector showing the run, observations, LLM generations and scores;
  - REVIEW, SESSIONS, HISTORY with **REPLAY**, METRICS and CONSOLE tabs;
  - a Rich TUI and a static terminal site.

  Its replay is stepwise and wall-clock-clamped, with no scrubbing. This plan keeps its data vocabulary and adds real timing, seek and live follow.

**Architecture:** Four layers that meet at one versioned JSON contract (`replay/v1`):
1. **Capture.** A full `mailroom.*` / OpenInference attribute set on spans, scores as span attributes, routing and decision events, a `run_id` on metrics, and a durable local span store.
2. **Timeline.** A pure builder turns spans (or the audit log) into an event-sourced session timeline.
3. **API.** Token-gated `/v1/replay/*` routes, plus an SSE stream for live follow.
4. **Viewer.** Pure JS clock and model modules plus a character-grid DOM view, mounted through a new `ctx.takeover` hook in the existing `/tui` terminal.

Because the contract is viewer-agnostic, a later viewer (a `/ui` panel, a revived The-Mailroom floor, a Grafana plugin) can consume the same payload.

**Tech Stack:** Only what the repo already ships. `opentelemetry-sdk` (`SpanExporter`, `BatchSpanProcessor`, metrics), SQLAlchemy and SQLite, Pydantic, FastAPI `StreamingResponse` for SSE, vanilla ES modules with no build step, and `node --test`. No new Python or npm dependencies.

**Spec / sources:**
- **mailroom-reloaded today:**
  - tracing in `obs/tracing.py` and `pipeline/flow.py` (`_drive`, `_guard_node`, `_fail_node`, `_classify_route`, gate audit at about line 472);
  - metrics in `obs/metrics.py`, with call sites in `pipeline/flow.py`, `llm/client.py:_record_usage_metrics` and `watcher.py`;
  - the audit log in `storage/db.py` and `storage/audit_log.py`;
  - the TUI in `api/tui/` (see `docs/TUI.md`);
  - Grafana dashboards in `deploy/grafana/dashboards/{pipeline,quality}.json`.
- **llm-mailroom** (`Exios66/llm-mailroom`):
  - `src/observability/{tracing,langfuse_setup,scores,langfuse_field_scoring,suite_scoring,masking}.py`;
  - `src/graph/build_graph.py` (root trace and node spans);
  - `src/graph/routing.py` (routing log events);
  - `src/schemas/audit.py`.
- **The-Mailroom** (`Exios66/The-Mailroom`):
  - `mailroom_ui/{trace_interpreter,pipeline_schema,models,metrics,reconsideration,classification}.py`;
  - `mailroom_ui/phoenix_source.py`, its OTel/OpenInference adapter and the closest precedent for this design;
  - `web/js/{floor,inspector}.js`, `hosted/js/app.js` (replay) and `tui/views.py`.
- **llm-dojo-scoring** (`Exios66/llm-dojo-scoring`, v0.21.0):
  - `field_scoring.score_extraction`, `extraction_metrics`, `trace_knobs.capture_trace_knobs`;
  - `mailroom.py` (`NODE_OBSERVATION_TYPES`, judge score names, the score-name aliases);
  - `registry.py` (metric tiers, units and rollup).

  mailroom-reloaded vendors only the maths (`src/mailroom_reloaded/scoring/`). The emitter, registry, cost and serving modules were dropped.
- **Brand kit:** the Terminal edition and pipeline-state colours (Claude artifact "The Mailroom", `project/guidelines/30-pipeline-states.md`).

---

## Why this needs a capture layer first

Nothing in mailroom-reloaded saves traces durably today, and much less is traced than in the original system:

- **Spans leave the process and are not kept locally.** They go out over OTLP to `otel-collector` and on to Phoenix. There is no file, JSONL or SQLite span exporter.
- **Span attributes are thin.** `mailroom.document` and `mailroom.node.<name>` carry only `mailroom.doc_id`. `llm-mailroom` put session, environment, tags, release, run id, attempt, source, dataset and taxonomy versions, ground truth and a curated input and output on every trace. It also put the `stage` on every node span, and attached about 50 named scores. None of this exists in mailroom-reloaded.
- **Routing decisions were never traced, even in the original.** In `llm-mailroom` they existed only as structlog events (`medium_confidence_retry`, `judge_gate_engaged`, `arbiter_escalated`, `transient_retry`, …) and in the audit chain. A replay needs them on the timeline.
- **Metrics have no `run_id`.** The Grafana `pipeline` and `quality` dashboards filter on `run_id=~"$run_id"` and fill the dropdown from `label_values(mailroom_documents_total, run_id)`. No metric call site sets that label, so the dropdown is empty.
- **The audit log is durable but has gaps.** It has no start events, so start times can only be reconstructed as `ts − elapsed_s`. Eval runs in `eval_docs` have per-document totals, but no timing per node.

So the MVP can replay **approximately** from the audit log on day one, and full fidelity arrives once capture is complete.

## Capture inventory (parity with the originals, plus the gaps they left)

Attribute naming follows **The-Mailroom's `phoenix_source.py` adapter** wherever it already defined a convention:
- `session.id`, `mailroom.tags`, `mailroom.environment`;
- `input.value` / `output.value` as JSON;
- `openinference.span.kind`, `llm.*`;
- `mailroom.score.<name>`.

Following it means The-Mailroom's existing Phoenix source can read mailroom-reloaded traces with little change, and the replay builder reads the same keys. All other new keys use the `mailroom.*` prefix. Content-bearing values (`input.value`, `output.value`, messages) stay under `MaskingSpanProcessor`. **The span store applies an allow-list and never stores content** (see Task 3).

### A. Root span `mailroom.document` (≈ llm-mailroom `document-pipeline` trace)

| Attribute | Value / source in mailroom-reloaded | Original equivalent |
| --- | --- | --- |
| `openinference.span.kind` | `CHAIN` | root type `chain` |
| `mailroom.doc_id`, `mailroom.filename` (basename) | `state.doc_id`, `Path(state.path).name` | input `filename`, `doc_id` |
| `mailroom.run_id` | eval `run_id` from `eval_ctx`; else `MAILROOM_RUN_ID`; else `live-<YYYYMMDD>` | metadata `run_id` |
| `session.id` | eval: `eval-<run_id>`; live: `matter_id` if present, else `run_id` | `session_id` (matter / pilot / experiment) |
| `mailroom.environment` | `MAILROOM_ENVIRONMENT` (default `live`; eval sets `eval`) | `environment` |
| `mailroom.tags` | JSON list `["mailroom", <env>, "source-<source>", "run-<n>"?]` | `tags` |
| `mailroom.release` | `mailroom@<package version>` | `release` |
| `mailroom.source` | intake source: `watch`, `upload`, `gmail` or `eval:<dataset>` | metadata `source` |
| `mailroom.attempt`, `mailroom.resumed`, `mailroom.resume_from` | resume / re-run info from `run_document(resume_from=…)` | `attempt`, `resumed`, `interrupt_resume` |
| `mailroom.worker_id`, `service.instance.id`, `gpu.replica` | `worker_id` plus the existing resource attributes | *not traced in the original (gap)* |
| `mailroom.run_deadline` | epoch deadline when overrides set one | `run_deadline` |
| `mailroom.dataset.name`, `mailroom.dataset.revision`, `mailroom.taxonomy.version` | eval dataset identity, `config/taxonomy.yaml` version | `dataset_trace_metadata()` |
| `mailroom.gt.*` (`expected_doc_class`, `expected_subclass`, `expected_stage`) | eval ground truth (no `expected_fields` values) | flattened GT metadata |
| `mailroom.intake.*` (`messy`, `changed`, `method`, `chars`, `raw_chars`, `vision_pages`, `bert_route`) | `state.ingest`, `state.bert` | `normalize-intake` output |
| `input.value` (JSON) | `{filename, doc_id, attempt, source}` | trace `input` |
| `output.value` (JSON, set on exit) | `{stage, status, doc_type, doc_subclass, classification_confidence, extraction_confidence, review_decision, escalation_reason, error_message, run_aborted, failure_class}` | `root.update(output=…)` |
| `mailroom.stage`, `mailroom.status`, `mailroom.doc_type`, `mailroom.doc_subclass`, `mailroom.route_trail`, `mailroom.failure_class` | the same fields as flat attributes, for SQL indexing | trace output |
| `mailroom.usage.*` (`calls`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_usd`), `mailroom.usage.by_role` (JSON) | `state.usage_total` plus a per-role roll-up | `usage_summary()`, `by_agent` |

### B. Node spans `mailroom.node.<name>` (≈ llm-mailroom node observations)

**Attributes on every node span:**
- `openinference.span.kind`, using the llm-dojo-scoring `NODE_OBSERVATION_TYPES` vocabulary mapped onto mailroom-reloaded's nodes:

  | Kind | Nodes |
  | --- | --- |
  | `CHAIN` | root |
  | `AGENT` | `sort`, `extract`, `boss` |
  | `EVALUATOR` | `verify`, `grade` |
  | `GUARDRAIL` | `gate_classify`, `gate_extract` |
  | `RETRIEVER` | `ingest` (file / vision read) |
  | `SPAN` | `bert_primary`, `report_catalog_archive`, `human_review` |

- `mailroom.node`, `mailroom.station` and `mailroom.phase` (see the station map below);
- `mailroom.doc_id`, `mailroom.run_id`, `session.id`;
- `mailroom.attempt` (per-node count: `classify_attempts` / `extract_attempts`);
- `mailroom.retry_kind` (`retry_sort` | `re_sort` | `retry_extract` | none);
- `mailroom.deadline_s`, `mailroom.token_budget`, `mailroom.tokens.used`, `mailroom.cost_usd`, `mailroom.llm_calls`;
- `mailroom.stage` (the document's stage **after** the node; this is what moved envelopes on The-Mailroom's floor);
- `input.value` / `output.value` holding the curated `_state_summary` / `_result_summary` JSON as in llm-mailroom (`doc_id`, `doc_type`, `stage`, confidences, `review_decision`, `error_message`; never document text).

**On failure:**
- span status ERROR;
- `mailroom.fail_reason` (`deadline_exceeded`, `token_budget_exceeded` or the exception class);
- `mailroom.failure_class`, using The-Mailroom's vocabulary: `llm_timeout`, `llm_auth`, `llm_rate_limit`, `llm_transient`, `io_error`, `schema_error`, `run_budget`, `unexpected`.

**Extra attributes per node:**

| Node | Extra attributes |
| --- | --- |
| `ingest` | `mailroom.intake.*`, `mailroom.vision.pages`, `mailroom.vision.method` |
| `bert_primary` | `mailroom.bert.route`, `mailroom.bert.class`, `mailroom.bert.confidence`, `mailroom.bert.latency_ms`, `mailroom.bert.available`, `mailroom.bert.reason` (≈ `intake-ml-triage`) |
| `sort` | `mailroom.sort.doc_type`, `.doc_subclass`, `.confidence`, `.label_logprob`, `.reasoning_present` |
| `gate_classify`, `gate_extract` | `mailroom.gate.action`, `.reason` (≤256 chars), `.source` (`gate` / `jev`), `.confidence`, `.features` (JSON `GateFeatures`) |
| `extract` | `mailroom.extract.doc_type`, `.confidence`, `.schema_valid`, `.n_fields`, `.n_empty`, `.length_capped`, `.conflict_detected` |
| `verify` | `mailroom.judge.label`, `.score`, `mailroom.arbiter.decision`, `.fields_to_fix`, `.retry_count` |
| `boss` | `mailroom.boss.decision`, `.context_records` |
| `human_review` | `mailroom.review.reason`, `mailroom.review.causes` (The-Mailroom `review_causes` vocabulary) |
| `report_catalog_archive` | `mailroom.archive.path`, `.file_sha256`, `.sidecar` |
| `grade` (eval) | `mailroom.grade.*` (see section D) |

### C. Span events: decisions and routing (gaps in the original, needed for the timeline)

| Event | Attributes | Replaces llm-mailroom log event |
| --- | --- | --- |
| `mailroom.route` | `from`, `to`, `reason` | the `_drive_nodes` transition |
| `mailroom.gate_decision` | `stage`, `action`, `reason`, `source`, `confidence` | `gate_decision` audit row / `*_confidence_retry/review` |
| `mailroom.retry` | `kind` (`retry_sort`, `re_sort`, `retry_extract`), `attempt`, `max_attempts`, `confidence` | `medium_confidence_retry`, `extraction_retry` |
| `mailroom.judge_gate` | `engaged`, `extraction_confidence`, `passes`, `max_passes` | `judge_gate_engaged`, `judge_skipped`, `judge_max_passes_exhausted` |
| `mailroom.arbiter` | `decision`, `handoff`, `fields_to_fix`, `retry_count`, `retry_max` | `arbiter_retry_approved`, `arbiter_escalated` |
| `mailroom.escalation` | `to` (`boss` / `human_review`), `reason` | `conflict_escalation`, `routed_to_review` |
| `mailroom.parked`, `mailroom.review_resolved`, `mailroom.archived` | the same payloads as the audit rows | the audit events of the same names |
| `mailroom.llm_retry` (on the LLM span's parent) | `attempt`, `max_attempts`, `error_type`, `retry_in_s`, `status_code` | `llm_retry` (from `llm/retry.py:with_retry`) |
| `mailroom.length_capped` | `role`, `max_tokens` | `length_capped` metric only |
| `mailroom.schema_invalid` / `mailroom.hollow_extraction` | `doc_type`, `detail` | `extraction_schema_invalid_*`, `extraction_hollow_*` |
| `exception` (OTel standard) | `exception.type`, `exception.message` | — (The-Mailroom reads these) |

### D. Scores as span attributes `mailroom.score.<name>` (≈ llm-mailroom Langfuse scores)

Scores are attached to the **span they describe**, not only the trace. This is an improvement on llm-dojo-scoring's emitter, which could only target a trace id. Values are numeric, boolean or string.

The **full name is always used**. The Langfuse 35-character alias (`extraction_verified_precision`) is not needed for OTel. When the score is read back for The-Mailroom compatibility, `canonical_score_name` handles both spellings.

| Group | Score names | Span | Computed by |
| --- | --- | --- | --- |
| **Every run** | `parse_error`, `schema_valid`, `stage_completed`, `guardrail_triggered`, `success_rate` (first pass), `run_aborted`, `classification_confidence`, `extraction_confidence`, `run_duration_seconds`, `total_tokens`, `estimated_cost_usd`, `llm_call_count`, `classification_attempts`, `extraction_attempts` | root | `pipeline/flow.py` at document end (port of `emit_pipeline_scores` / `compute_run_metrics`) |
| **Intake** | `intake_prep_completeness`, `intake_changed_rate`, `intake_messy_rate`, `intake_hyphen_unwraps`, `intake_collapsed_blanks` | `ingest` | vendored `scoring/intake.py` |
| **Judge (Lane B)** | `completeness`, `completeness_label`, `judge_notes` (truncated), `mailroom-pipeline-judge` (CORRECT/PARTIAL/MISS), `mailroom-pipeline-quality` (0–1) | `verify` | judge / arbiter results |
| **Ground truth (eval)** | `class_correct`, `stage_correct`, `confidence_calibration_error`, `expected_field_presence` | root | `eval/runner.py` grading |
| **Field-level (grounded)** | `extraction_field_score.<field>` (one per field), `extraction_overall_score`, `extraction_needs_judge_review`, `extraction_ambiguous_fields` (JSON), `entity_list_precision`, `entity_list_recall`, `entity_list_f1`, `extraction_overall_verified_precision`, `extraction_hallucination_rate`, `extraction_category_presence`, `deterministic_verdict` | `extract` (or `grade`) | vendored `scoring/field_scoring.score_extraction` and `extraction_metrics` |
| **Binary extraction counts** | `extraction_precision`, `extraction_recall`, `extraction_f1`, `extraction_f2`, `tp`, `fp`, `fn`, `n_correctly_empty`, `n_spurious_fill` | `extract` / `grade` | `scoring/extraction_metrics.extraction_binary_metrics` |
| **Trace knobs** | `confidence_gate` (pass / below_min / in_band / missing), `calibration_error`, `n_reasoning_entries` | `extract` | `scoring/trace_knobs.capture_trace_knobs` (reasoning text is **not** stored) |
| **Reconsideration** | `review_causes` (JSON list: `class_miss`, `subclass_miss`, `extraction_miss`, `judge_miss`, `judge_partial`, `schema_invalid`, `guardrail`, `parse_error`, `reporting_incomplete`, `needs_judge_review`, `hollow_extraction`), `needs_reconsideration` | root | port of The-Mailroom `reconsideration.collect_review_causes` (GT and verdicts only, never self-reported confidence) |
| **Suite extras (when applicable)** | `determination_consistency`, `amount_exactness`, `content_topic_*`, `sentiment_*`, `maud_*` | root | **not vendored today.** Listed so the schema reserves the names; ported only if those suites return. |

**Tiers (Decision 4).** Every spec also carries a `tier`, borrowed from llm-dojo-scoring's `registry.py` / `pruning.py` (`DEFAULT_DASHBOARD_TIER = 1`): **T0 headline** (status, doc type, verdict, quality, latency, cost), **T1 core** (confidences, tokens, attempts, retries, gate mix), **T2 deep** (per-field scores, calibration, per-call LLM detail), **T3 log-only** (everything else). All tiers are captured and stored in the ledger. Tier only decides what the viewer, `/ui` and dashboards draw, so nothing is lost and the screen stays readable.

A small `obs/scores.py` registry holds `SCORE_SPECS = {name: (data_type, unit, rollup, span_scope, tier)}`. It is a slim port of the `SCORE_CONFIGS` and dojo `registry.py` tier/unit/rollup data. It is used to validate names at emit time, to drive the inspector's grouping, and to drive run-level roll-ups in the timeline (mean / sum / none).

### E. LLM spans (OpenInference, already emitted by `OpenAIInstrumentor` / `CrewAIInstrumentor`)

**Kept:**
- `llm.model_name`, `llm.provider`;
- `llm.token_count.{prompt,completion,total}`;
- `llm.invocation_parameters.max_tokens` (only that key).

**Added by `llm/client.py` on the current LLM span:**
- `mailroom.role` (the `call_structured(role, …)` value, ≈ llm-mailroom generation `name`/`agent`);
- `mailroom.prompt.name` and `mailroom.prompt.version` (from the `prompts/*/lineage.json` lineage);
- `llm.cost.total`, `llm.cost.prompt`, `llm.cost.completion` (from `_token_cost`; this is the cost attribute The-Mailroom's Phoenix adapter reads);
- `mailroom.served_model`, `mailroom.fallback_from` (when the resolver falls back);
- `mailroom.transport_attempts` (from `with_retry`);
- `mailroom.length_capped`;
- `mailroom.schema_valid`;
- `mailroom.ttft_s`, only when a streaming call or vLLM provides it. TTFT was never captured in the original, so this field is optional.

### F. Metrics: the `run_id` label (Decision 2)

- **`obs/run_context.py`** holds a `contextvars.ContextVar` `current_run` with fields `run_id`, `environment` and `source`.
  - `MailroomFlow._drive` sets it per document. `eval/runner.py:run_eval` sets it per run. `watcher.py` sets `live-<YYYYMMDD>` (or `MAILROOM_RUN_ID`).
- **The `M` namespace in `obs/metrics.py`** wraps each instrument so every `add`, `record` and `set` merges `{"run_id": …, "environment": …}` into its labels. No call site changes, and the `gen_ai.*` instruments get it too.
- **Cardinality guard.** Eval run ids are bounded (one per `mailroom eval`). Live traffic uses one daily bucket, not one per document. `doc_id` is **never** a metric label. A test asserts that the set of label keys is fixed.
- **Grafana:**
  - `pipeline.json` and `quality.json` gain a dashboard link `replay ↗` → `${public_url}/tui#replay=run:${run_id}`, plus a `phoenix ↗` link.
  - Panel data links on per-run series go to the same targets.
  - Each dashboard gets a `public_url` template variable (default `http://localhost:8000`) and a `phoenix_url` variable. As built there is no `MAILROOM_PUBLIC_URL` environment variable and no provisioning change.
- **Per-run Phoenix projects are dropped.** `build_resource` reads the project once (`obs/tracing.py:132`) and `setup_tracing` is a singleton (`:210`), so a per-run provider would fight the global one. Links to Phoenix filter on the `mailroom.run_id` span attribute instead.
- **Known dashboard gap.** `quality.json` queries `mailroom_eval_.*` (lines 26, 29, 49), which no code emits, so the `run_id` label alone does not make those panels work. Task 4 fixes the queries to the metrics that are really emitted.
- **Event metrics llm-mailroom never had.** `mailroom.retries{kind}`, `mailroom.escalations{to}` and `mailroom.review.causes{cause}` counters, so Grafana can show the same decision mix the replay ticker shows.

### Station map (from the internal node to The-Mailroom's station, used by the track and inspector)

| mailroom-reloaded node | `mailroom.station` | `mailroom.phase` | The-Mailroom stage | Track colour token |
| --- | --- | --- | --- | --- |
| `ingest`, `bert_primary` | `intake` | `intake_sort` | intake | `--term-fg-dim` |
| `sort` (+ `retry_sort` / `re_sort`) | `sorter` | `intake_sort` | classify / retry_classify | `--term-cyan` |
| `gate_classify`, `gate_extract` | `gate` | `intake_sort` / `extraction` | (new: deterministic gate) | `--term-phosphor` |
| `extract` (+ `retry_extract`) | `specialist` | `extraction` | extract / retry_extract | `--term-amber` |
| `verify` | `judge` | `extraction` | judge_verify / arbiter | `--term-station-judge` (= `--term-magenta`) |
| `boss` | `boss` | `extraction` | boss | `--term-red` |
| `human_review` | `review` (bay) | `review` | review | `--term-station-review` (= `--term-yellow`) |
| `report_catalog_archive` | `archive` | `reporting` | report / catalog / archive | `--term-green` |
| terminal `failed` | `failed` (bay) | `terminal` | failed | `--term-red` |

`--term-magenta` (`#f472b6`) and `--term-yellow` (`#facc15`) were already vendored in `tokens.css` (dark, light and hc). The viewer takes them through the `--term-station-judge` and `--term-station-review` role aliases, added in the brand-kit follow-up.

## Archive ledger (Decision 6)

**Reuse first.** `storage/audit_log.py` stays as it is: per-document, hash-chained, deduplicating, battle-tested. The ledger is a **separate table pair in the same `mailroom.db`** (`create_all` adds new tables without a migration), because a high-volume metrics stream does not belong in the per-document chain: `append` (`audit_log.py:53-64`) takes `BEGIN IMMEDIATE`, loads the whole chain on every call and drops identical `(node, event, payload)` appends, so a shared chain would be O(N²) and would swallow legitimate repeats (pin, unpin, pin). Hashing reuses `schemas/audit.py:44-49` unchanged (no `hash_version` change, which would alter every existing chain).

**Tables**
- `ledger(seq INTEGER PK global, kind, run_id, doc_id NULL, ts, payload JSON, digest, prev_hash, entry_hash)`: one global chain, genesis `""`, tail-only append, no dedupe.
- `ledger_metrics(run_id, doc_id, tier, name, value, span_id, ts)`: the full flat metric set, all tiers. **Cap about 5,000 rows per run**, then a single `gap` entry and a dropped counter in `run_closed`.

**Writer thread.** One writer drains a bounded queue and appends batches in a single `BEGIN IMMEDIATE` transaction, so heavy runs never contend with the document audit chain. It flushes on `run_closed`, every few seconds and at shutdown. A full queue drops metric rows (and writes `gap`) but never an integrity-critical chain entry. A ledger failure is logged and never masks the original exception or fails a document.

**Merkle root.** `run_closed` carries a binary Merkle root over the run's `doc_closed` digests (ordered by `seq`), plus counts and tier totals. `ledger verify --run` recomputes it; inclusion proofs can follow later.

**Entry kinds**

| kind | When | Payload (allow-list only; bounded strings; no document text, filenames, judge notes, prompts or completions) |
| --- | --- | --- |
| `run_opened` | run/session starts | run_id, kind (eval/live), mode, posture_label, model, prompt_set, config_sha, environment, pid. Fixes the missing run header (`eval_docs` stores only `run_id` and `mode`). |
| `doc_closed` | one per document invocation | doc_id, invocation, outcome (completed/failed/parked/aborted/reconciled), doc_type, `audit_head` ({seq, entry_hash} of that document's `audit_log` chain at write time, which links the two chains), metrics digest, rows, `usage_by_role` {sorter, specialist, judge, arbiter, boss, vision, grader: prompt/completion tokens, calls, cost_usd}, `usage_complete`, `usage_partial_nodes` |
| `gap` | row cap reached or queue overflow | reason, count |
| `run_closed` | run ends | counts, `expected` (eval: selected count; live: null), `merkle_root`, dropped_rows, closed_by (completed/interrupted/reconcile) |
| `checkpoint` | every 100 documents or at rollover | open runs' heads |
| `pinned` / `unpinned` / `policy` / `pruned` | retention | run id, actor, policy value, counts |
| `anchor` | `ledger anchor` | head, count |
| `score_late` | reserved | not written until an offline judge exists |

**Intake (all best-effort).** Scope is set inside `run_document` in the worker thread (ContextVars do not cross the watcher's thread pool), the bucket is pinned at open, and counters sit under a lock.
- `flow._drive`: try/finally. Usage is snapshotted at entry and per-invocation **deltas** are taken at exit, because `_configure` restores `usage_total` from the manifest on resume (`flow.py:585-590`). It accumulates from `_record_node`, `_audit_gate` and `_fail_node`. `reconcile_archived` writes `reconciled`.
- `eval.run_eval`: opens before the gather and closes in `finally` with `expected`. The eval exception path and `specialist_cell` rows (`runner.py:429-466`, which never touch the flow) are recorded explicitly.
- `watcher`: lazy rollover. The first document of a new day or process closes the previous bucket as `completed` when this process opened it; a stale run left by an earlier process is closed as `interrupted` by `resume_processing`. The manifest `processing` status is the open marker, so there is no `doc_open` entry.
- LLM usage is captured for **every** role (Decision 12, Task 19), so `doc_closed` carries `usage_by_role` and `usage_complete`. It is false only when a node raised mid-call and its sub-call usage was lost; that node is named in `usage_partial_nodes`.
- `failure_reason` uses a bounded enum, because today's strings embed filenames and exception text (`clerk.py:156-168`, `flow.py:164`).

**API (token-gated like the rest of `/v1`; static routes declared before path-param routes)**
- `GET /v1/ledger?kind&run_id&doc_id&since&limit&offset`, `GET /v1/ledger/{seq}`, `GET /v1/ledger/head`, `GET /v1/ledger/verify[?run_id][&external=1]` (returns `{ok, broken_at, head, count}`).
- `/v1/runs` is extended in place (live buckets and the kept class; the `app.py:361` run-id regex is relaxed to `[A-Za-z0-9_-]{1,64}`); `GET /v1/runs/{id}/ledger` returns a run's entries plus `verify_run`.

**TUI (`api/tui/commands/ledger.js`; existing `audit <doc>` is untouched)**

```
mailroom@floor:~$ ledger --run 3fa9c1d20b7e
 SEQ   KIND         RUN           DOC        HASH          PREV
 412   run_opened   3fa9c1d20b7e  —          9c41e07a3b52  genesis
 413   doc_closed   3fa9c1d20b7e  a1b2c3d4…  0e77d2a9c1f4  9c41e07a3b52
 …     run_closed   3fa9c1d20b7e  —          b84c11f2096d  …
 chain: ok  (4 entries, head b84c11f2096d, merkle ok)
```

- Commands: `ledger` (tail), `ledger ls [--run ID] [--kind K] [--doc ID]`, `ledger show <seq|run> [--tier 2|3]`, `ledger verify [--run ID] [--external]`, `ledger head`, `ledger anchor`, `ledger export-head`.
- Style mirrors The-Mailroom's TUI: uppercase headers, 12-character hashes in `--term-fg-dim`, `genesis` on the first row, kind colours from the station roles (`--term-station-judge`, `--term-station-review`, green archived, red failed/pruned), and a final `chain: ok | broken at <seq>` line like `audit`. All text via `textContent`.
- `replay` and `inspect` gain a `ledger` panel (the run's or document's entries and the link from a `doc_closed` entry to the document's audit chain head).

### External anchor (Decision 10)

`storage/anchor.py` (one module, drivers imported lazily) pushes `(seq, entry_hash)` of the **ledger head**. Nothing else is hashed; timestamps are never hashed (the stored `ts` uses `+00:00` while the pydantic dump uses `Z`).
- **Toggle** `MAILROOM_ANCHOR` = `none` (default) | `export` | `postgres` | `supabase`. Settings: `anchor`, `anchor_url` (Supabase project URL or Postgres DSN), `anchor_key`, `anchor_key_file`. Secrets are plain `str` with `repr=False`. Blanks normalise to unset through an extended `_empty_to_none`. Configuration is validated lazily, so a bad value never crashes startup or the pipeline; it shows in `ledger verify --external` and health.
- **Remote table:** `mailroom_anchor(seq bigint PRIMARY KEY, entry_hash text NOT NULL, anchored_at timestamptz NOT NULL DEFAULT now())` plus a `BEFORE INSERT` trigger that rejects `seq <= max(seq)`; the writer role gets INSERT and SELECT only; `REVOKE UPDATE, DELETE, TRUNCATE`. On Supabase also revoke from `anon`, `authenticated` and `service_role`: `service_role` bypasses RLS, so grants are the real control.
- **Transport (decided):** HTTPS is the primary path. Supabase over PostgREST with httpx needs no new dependency and works with proxied HTTPS egress. Postgres through `psycopg` stays available in a **locked** optional extra `anchor` for deployments with direct TCP egress (the Dockerfile's `uv sync --frozen` ignores an unlocked extra). `export` prints or writes the head for off-host operator pinning.
- **Secrets file (decided):** `anchor_key` may come from `MAILROOM_ANCHOR_KEY` or from a file named by `MAILROOM_ANCHOR_KEY_FILE` (Docker/Kubernetes secrets style). The file is read at use time, stripped, size-capped, never logged, and a world-readable file produces a warning in `ledger verify --external`. The environment variable wins if both are set. Same rule for the Postgres DSN password via `anchor_url`.
- **Duplicates** are resolved by read-back (same hash = success, different hash = conflict), never by relying on a 409 or `Prefer: resolution=ignore-duplicates`, which returns 201 without a row.
- **Triggers:** after `run_closed`, after `pinned/unpinned/policy/pruned`, at startup, and `mailroom audit anchor`. A coalesced flag drives a daemon thread (3 attempts at 1/4/16 s, 5 s timeouts). The pipeline never waits on or raises from a push. Errors are logged with the DSN and key redacted by literal replacement. "Pending" is derived (local head seq greater than the store head), so there is no state file.
- **`mailroom audit verify [--run ID] [--external]`:** exit 0 ok, 1 tamper (`TRUNCATED`, `REWRITTEN` or chain broken), 3 store unreachable (never reported as ok), 4 not configured. Exit code 2 is avoided because Click uses it for usage errors. `STALE` is raised only when the oldest unanchored entry is older than 24 h, so idle systems do not alarm.
- **Threat model.** It protects against truncation, rollback (including restoring an old backup) and rewrite of entries at or before the last anchor that was pushed before compromise, against an attacker without the writer credential. It does not protect the unanchored tail, runs still open, content correctness, or a holder of the writer credential, who can backfill anchors over a rewritten tail (only an off-host `export` copy helps). The plpgsql trigger, grants, RLS and the Supabase key header can only be tested against a live project; the `live`-marked test is deselected in CI, so they need a manual staging run.

## Retention and pinning (Decision 5)

There is no precedent in the originals (llm-mailroom, The-Mailroom and llm-dojo-scoring have no run registry, retention, pin or golden run), and nothing in mailroom-reloaded is ever deleted today.

- **Classes.** `showcase` (shipped, always kept), `pinned` (user-kept), `ordinary` (pruned).
- **Heavy vs light.** Heavy = span rows in `traces.db`. Light and permanent = the ledger.
- **`MAILROOM_TRACE_KEEP`** = `pinned` (default: showcase and pinned runs plus a 3-day rolling window of live spans), `recent:<N>` (also the last N ordinary runs) or `all`. The runtime commands `runs keep|pin|unpin` write `policy`, `pinned` and `unpinned` ledger entries; the latest entry overrides the environment default. These entries only influence span retention, so they are not evidence.
- **Pruning** runs at startup and daily, deletes span rows only, then writes `pruned` (run id, digest, counts). A pruned run still lists and verifies, shown as `data pruned`.
- **Showcase** (new users can watch these with no pipeline run): four curated runs shipped as `replay/v1` files in `src/mailroom_reloaded/showcase/` (package data, because only `src/mailroom_reloaded` ships in the wheel): (1) a clean run, (2) retries plus a boss escalation, (3) parked and failed documents, (4) a judge- and arbiter-heavy run. They are seeded idempotently on first start, and each file's digest is recorded in the ledger so a modified file fails verification.
- **TUI:** `runs pin <id>`, `runs unpin <id>`, `runs keep [pinned|recent:N|all]`, `runs --kept`.

### Settings added

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAILROOM_TRACE_KEEP` | `pinned` | `pinned` \| `recent:<N>` \| `all` |
| `MAILROOM_ANCHOR` | `none` | `none` \| `export` \| `postgres` \| `supabase` |
| `MAILROOM_ANCHOR_URL` | unset | Supabase project URL or Postgres DSN |
| `MAILROOM_ANCHOR_KEY` | unset | Supabase key or Postgres password (never logged) |
| `MAILROOM_ANCHOR_KEY_FILE` | unset | Path to a secrets file holding the key (env var wins if both are set) |

Constants, documented in `docs/CONFIGURATION.md` rather than environment variables: the per-run row cap (5,000), the 3-day live span window, the default displayed tier (T1), and the idempotent showcase seed. Dropped env vars from revision 3: `MAILROOM_LEDGER`, `MAILROOM_TRACE_STORE`, `MAILROOM_RUN_BUCKET`, `MAILROOM_REPLAY_TIER`, `MAILROOM_SEED_SHOWCASE`, `MAILROOM_TRACE_LIVE_DAYS`.

## Concept mapping (F1 → mailroom)

| f1-race-replay | Mailroom replay |
| --- | --- |
| Race / session | `run:<run_id>` (eval run, or a daily `live-<date>` run), `session:<session.id>`, `doc:<doc_id>`, or `window:<from>..<to>` |
| Car / driver | Document (`doc_id`, filename, `doc_type` stamp colour) |
| Track and sectors | Stations: intake → sorter → gate → specialist → gate → archive |
| Pit lane | Retry loops (`retry_sort`, `re_sort`, `retry_extract`) and `verify` / `boss` detours |
| Car in the garage | Parked in `human_review` (REVIEW siding, as on The-Mailroom's floor) |
| "OUT" / DNF | `failed`, with a `failure_class` |
| Chequered flag | `archived` |
| Lap time / sector time | `run_duration_seconds` / node span duration |
| Race-control messages | Ticker: `mailroom.*` decision events (section C) |
| Driver telemetry | Inspector, with The-Mailroom's RUN / OBSERVATIONS / LLM GENERATIONS / SCORES panels, at time *t* |
| Pit-wall insight windows | Panel registry (`registerPanel`) |
| `computed_data/` cache | Built timelines cached per session and source watermark |
| Telemetry stream | `GET /v1/replay/live` SSE |

## Sketch of the viewer (character grid, Decision 1)

Terminal edition, `--term-*` tokens, one `<pre>`-like grid of `<span>` cells written via `textContent`. Sizes are illustrative.

```
replay run:3f9c0a1b2d4e · eval · 48 docs · source spans                ▶ 4×   02:13 / 07:40
────────────────────────────────────────────────────────────────────────────────────────
 intake   sorter   gate   specialist   gate   judge   archive  ▸ ✓ 31
   ●●       ●●●     ●       ●●●●●       ●●      ●        ●
            ↺ 2                ↺ 1             boss ●
 ▌ review 3 (class_miss 2 · judge_partial 1)          ✕ failed 1 (llm_timeout)
────────────────────────────────────────────────────────────────────────────────────────
 pos doc_id  file                 station     t      tok    $      verdict │ ▸ 7b2e… lease_scan.pdf
  1  a91f…   invoice_0412.pdf     ✓ archive   41.2s  3.1k  .008    CORRECT │  RUN   contract/lease · attempt 1
  2  7b2e…   lease_scan.pdf       specialist  38.0s  5.4k  .012    —       │  GATE  do_extract · 0.91 · jev
  …                                                                        │  GEN   extract · qwen3 · 2.1k/600 · 4.2s
                                                                           │  SCORE field 0.83 · halluc 0.00
────────────────────────────────────────────────────────────────────────────────────────
 02:11  gate      c03d…  retry_sort — low confidence 0.42
 02:12  specialist 9e10… ✕ deadline_exceeded 30.4s · llm_timeout
[━━━━━━━━━━━━━┿━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━]  space play · ←→ seek · ↑↓ speed · q quit
```

## Global Constraints

- **No new dependencies.** No Python or npm additions. The dependency fence (`tests/test_dependency_fence.py`) stays green (no langfuse; the OTel attributes replace it).
- **Read-only viewer.** It never resolves, re-runs or mutates documents.
- **Character grid only (Decision 1).**
  - `textContent` only; no `innerHTML` or `<canvas>`.
  - Hostile filenames and reasons render as text.
  - Each glyph is one cell, and crowding collapses to counts (`●×12`).
- **Brand.**
  - Terminal edition `--term-*` tokens; station colours come from the table above.
  - `prefers-reduced-motion` gives stepwise playback with no tweening, as The-Mailroom's Observatory replay does. `hc` drops glows.
  - Lower-case voice; glyphs limited to `· — ↗ ▸ ● ✓ ✕ ▌ ↺ ━ ┿`.
- **Privacy.**
  - The span store sits **after** `MaskingSpanProcessor` **and** applies an attribute allow-list. `input.value` / `output.value` are stored only for node and root spans, where they are curated summaries. LLM message content and document text are never stored.
  - `judge_notes` and gate reasons are truncated to 256 characters.
  - The inspector links to Phoenix for prompt and completion content.
- **Durability isolation.** Spans go to a separate `<base_dir>/traces.db` (WAL), never into the hash-chained `mailroom.db` audit log.
- **Metric cardinality.** The `run_id` label is bounded (eval runs plus one daily live bucket). `doc_id` is never a metric label.
- **Performance budget.**
  - 500 documents × about 15 segments plus about 40 events each, at 30 fps.
  - A timeline payload of 2 MB or less per chunk (windowed above that).
  - Building a timeline takes under 1 s for 500 documents.
  - Span capture overhead is under 2% of document latency.
- **API contract.** Every `/v1/replay/*` route uses `require_token`. Payloads carry `"version": "replay/v1"`.

## Review Focus

- **Ledger:** writer contention with the document audit chain; the allow-list must never leak document text, prompts or completions; prune must never remove ledger rows; hostile filenames and run ids in `ledger` output; verify cost on long chains (range-scoped verify); showcase files carry a ledger digest.
- **Attribute allow-list and masking.**
  - Stored spans contain no `llm.input_messages.*`, `llm.output_messages.*` or document text, even with masking off.
  - `input.value` is kept only on root and node spans (Task 3).
- **Score correctness.** Field-level scores match the vendored `score_extraction` output for a fixture byte for byte. `review_causes` matches The-Mailroom's `collect_review_causes` on its own test vectors (Task 2).
- **Metric labels.** Every `M.*` emission carries `run_id` and `environment`, and never `doc_id`. The Grafana `label_values(mailroom_documents_total, run_id)` query returns the seeded run (Task 4).
- **Approximate timelines.** Audit-only sessions are labelled `source audit (approx)` (Task 5).
- **Hostile strings and large or empty sessions.** Same checks as the TUI plan (Task 10).
- **Key capture.** `takeover` never leaks keystrokes into the prompt, and always releases (Task 9).
- **Clock correctness.** `stateAt(t)` gives the same result whether reached by play or by seek (Task 8).

## File Structure

```
src/mailroom_reloaded/
  obs/run_context.py          (new)    current_run ContextVar (run_id, environment, source, session_id)
  obs/attrs.py                (new)    attribute key constants, station map, failure_class mapper, span-kind map
  obs/scores.py               (new)    SCORE_SPECS registry + emit_score(span, name, value)
  obs/reconsideration.py      (new)    port of The-Mailroom collect_review_causes
  obs/metrics.py              (modify) auto-merge run_id/environment labels; retries/escalations/review.causes
  obs/tracing.py              (modify) attach the span store (batch, masks internally); no per-run provider
  storage/ledger.py           (new)    archive ledger: tables, writer thread, Merkle root, verify (shares hashing with schemas/audit.py; audit_log.py untouched)
  storage/anchor.py           (new)    optional external anchor: none|export|postgres|supabase
  storage/retention.py        (new)    keep classes, prune, showcase seed, pin/unpin
  schemas/ledger.py           (new)    Pydantic ledger entry + hash version
  storage/span_store.py       (new)    batch SpanExporter, allow-list, internal masking, row cap, prune, query helpers
  obs/replay/{__init__,timeline,sessions,otlp_import}.py   (new)
  schemas/replay.py           (new)    Pydantic replay/v1 models
  pipeline/flow.py            (modify) root/node attributes, decision events, scores
  llm/client.py, llm/retry.py (modify) role/prompt/cost/served-model/retry attributes and events; record_usage()/cost_for() for CrewAI-routed roles
  llm/usage.py, tools.py, agents/{judge,arbiter,boss}.py, ingest/{vision,clerk}.py, pipeline/{state,report}.py (modify) per-role usage capture (Task 19)
  eval/runner.py              (modify) run context, GT attrs, grading scores on spans
  watcher.py                  (modify) live run context
  settings.py                 (modify) trace_keep, anchor, anchor_url, anchor_key, public_url; _empty_to_none extended
  cli.py                      (modify) `mailroom replay export|sessions` (import deferred); `mailroom audit verify|anchor|export-head`
  api/app.py                  (modify) /v1/replay/sessions, /v1/replay/{session}, /v1/replay/live
  api/tui/terminal.js         (modify) ctx.takeover(factory)
  api/tui/main.js             (modify) registerReplay; #replay= deep link
  api/tui/commands/replay.js  (new)
  api/tui/commands/ledger.js  (new)    ledger, ledger verify|head|anchor; runs pin|unpin|keep in pipeline.js
  api/ui/index.html           (modify) Eval runs: third column `replay ↗`
  showcase/*.json             (new)    four showcase replay/v1 files (package data)
  api/app.py also: /v1/ledger*, /v1/runs/{id}/ledger
  api/tui/replay/{clock,model,view,panels,stations}.js     (new)
  api/tui/tokens.css, tui.css (modify) .replay-* layout; station role tokens (done)
deploy/grafana/dashboards/{pipeline,quality}.json          (modify) replay ↗ / phoenix ↗ links, decision panels
deploy/otel-collector*.yaml   (optional) commented `file` exporter example
tests/obs/test_run_context.py, test_span_attrs.py, test_scores.py, test_metrics_run_id.py,
      test_span_store.py, test_replay_timeline.py
tests/obs/test_ledger.py, test_retention.py
tests/api/test_replay_api.py, test_ledger_api.py
tests/tui/js/ledger.test.mjs
tests/tui/js/replay_{clock,model,view}.test.mjs, takeover.test.mjs
tests/deploy/test_grafana_links.py
tests/fixtures/replay/        span + audit fixtures (happy, retry, failed, parked, boss)
docs/TUI.md, docs/OPERATIONS.md, docs/CONFIGURATION.md, CHANGELOG.md
```

## `replay/v1` contract

```
Timeline {
  version: "replay/v1"
  session:  { id, kind: run|session|doc|window, environment, t0_iso, duration_s,
              source: spans|audit, approx: bool, window: { from_s, to_s, complete } ,
              links: { phoenix_project?, grafana? } }
  stations: [ { id, label, phase, order, kind: main|detour|bay, color_token } ]
  entities: [ { doc_id, filename, trace_id?, session_id?, doc_type?, doc_subclass?,
                expected_doc_class?, expected_subclass?, final_stage, final_status,
                failure_class?, review_causes[], verdict?, quality?, t_start, t_end?,
                totals: { tokens, cost_usd, llm_calls, duration_s } } ]
  segments: [ { doc_id, node, station, t0, t1, attempt, retry_kind?, status: ok|failed|running,
                reason?, tokens?, cost_usd?, llm_calls?, span_id?, approx? } ]       # sorted by t0
  generations: [ { doc_id, span_id, parent_span_id, role, model, served_model?, prompt_version?,
                   t0, t1, prompt_tokens, completion_tokens, cost_usd, transport_attempts?,
                   ttft_s?, length_capped?, schema_valid? } ]                       # no content
  events:   [ { t, doc_id, kind, station?, payload } ]                                # section C kinds
  scores:   [ { doc_id, span_id?, name, value, data_type, t } ]                       # section D
  rollups:  { per_station: {p50_s, p95_s, n}, verdict_counts, review_causes, cost_usd, tokens,
              first_pass_rate, avg_<score> … }                                        # The-Mailroom metrics.py set
}
```

Times are seconds relative to `session.t0`. The format is event-sourced (no fixed-rate frames). The client finds `stateAt(t)` by binary search. Scores carry `t` (their span's end), so the inspector and panels only show scores that existed at the playhead.

---

### Task 1: Run context and attribute vocabulary

**Files:** Create `obs/run_context.py`, `obs/attrs.py`, `tests/obs/test_run_context.py`; Modify `settings.py` (`environment`, `run_id`, `public_url`).

**Revision 4 amendments:** the scope is set inside `run_document` (in the worker thread) and in `run_eval`; a ContextVar set outside does not cross `ThreadPoolExecutor` (`watcher.py:187-212`). Run context stays in `obs/run_context.py`; `settings.py` gains no `run_id` or `environment` fields.

**Interfaces:**
- `run_scope(run_id, environment, source, session_id)` is a context manager.
- `current_run()` returns the current scope.
- `obs/attrs.py` defines:
  - the key constants for sections A–E;
  - `STATIONS` and `station_for(node)`;
  - `SPAN_KIND_FOR_NODE`;
  - `failure_class_for(exc | reason)`, mapping to The-Mailroom's vocabulary.
- [ ] **Step 1: Write failing tests.**
  - Nested scopes restore their state.
  - The context propagates into worker threads used by the watcher.
  - `failure_class_for` maps the timeout, auth, 429, transient, IO, schema and budget cases.
  - Every `NODE_ORDER` node, plus `verify`, `boss`, `human_review` and `grade`, has a station.
- [ ] **Step 2–4:** run (FAIL), implement, then run (PASS).
- [ ] **Step 5: Commit** `feat(obs): run context and replay attribute vocabulary`.

### Task 2: Full span capture (sections A–E)

**Files:** Modify `pipeline/flow.py`, `llm/client.py`, `llm/retry.py`, `eval/runner.py`, `watcher.py`; Create `obs/scores.py`, `obs/reconsideration.py`, `tests/obs/test_span_attrs.py`, `tests/obs/test_scores.py`.

**Interfaces:**
- **`flow.py`:**
  - `_drive` opens the run scope and sets the root attributes (section A).
  - `_guard_node` sets the node attributes (section B) through a `_node_span_attrs()` helper.
  - The gate, retry, judge, arbiter, escalation, park and archive points emit section-C events through `_emit_event(kind, **attrs)`, next to their existing audit writes.
  - At document end, `emit_run_scores(span, state)` sets section D "every run", "reconsideration" and "intake" scores.
- **`llm/client.py`** sets the section E attributes on the current span. `with_retry` emits `mailroom.llm_retry`.
- **`eval/runner.py`:**
  - wraps `run_eval` in `run_scope(run_id, "eval", f"eval:{dataset}")`;
  - adds GT attributes;
  - emits the ground-truth, field-level and binary-count scores on the `grade` span (or the root, when `grade` is skipped) from the vendored `scoring/` functions it already calls.
- **`obs/scores.py`:** `SCORE_SPECS`, and `emit_score(span, name, value)`, which validates name and type, writes `mailroom.score.<name>`, and adds a `mailroom.score` event with `name`, `value` and `t`.
- [ ] **Step 1: Write failing tests**, using `fake_openai` and `_attach_exporter()` from `tests/obs/test_obs.py`:
  - **Happy run:** the root has every section-A key, and every node span has `mailroom.station`, `mailroom.stage` and the span kind.
  - **`retry_sort` path:** a `mailroom.retry` event fires, and the second `sort` span has `mailroom.attempt=2`.
  - **Deadline override:** the `mailroom.fail_reason` and `mailroom.failure_class` attributes are set.
  - **Gate:** a `mailroom.gate_decision` event matches the audit row.
  - **LLM spans:** they carry `mailroom.role`, `llm.cost.total` and `mailroom.prompt.version`.
  - **Eval fixture:** it yields `extraction_field_score.<field>` values equal to `score_extraction(...)`.
  - **`review_causes`:** equals The-Mailroom's vectors (copied as fixtures).
  - **No content:** no attribute anywhere contains the fixture document's body text.
  - **Unknown names:** an unknown score name raises in tests and logs in production.
- [ ] **Step 2: Run** `uv run pytest tests/obs -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Keep the helpers in `obs/` so `flow.py` changes stay a few lines per node.
- [ ] **Step 4: Run.** Expected: PASS. The existing `test_flow_emits_node_spans` and the eval tests stay green.
- [ ] **Step 5: Commit** `feat(obs): llm-mailroom-parity span attributes, decision events and scores`.

### Task 3: Durable local span store

**Files:** Create `storage/span_store.py`, `tests/storage/test_span_store.py`; Modify `obs/tracing.py`, `settings.py`, `docs/CONFIGURATION.md`, `deploy/docker-compose.dev.yml`, `scripts/tui_dev.sh`.

**Interfaces:**
- **Settings:**
  - the store is always on (a toggle would make production replay silently empty);
  - `trace_store_path` (default `<base_dir>/traces.db`);
  - retention is `MAILROOM_TRACE_KEEP` (see Retention); `trace_store_days` is dropped.
- **`SqliteSpanExporter`:**
  - Tables:
    - `spans(trace_id, span_id PK, parent_id, name, kind, start_ns, end_ns, status, doc_id, run_id, session_id, station, attrs JSON, events JSON)`, indexed on `(run_id, start_ns)`, `(session_id, start_ns)`, `(doc_id, start_ns)` and `(start_ns)`;
    - `runs(run_id PK, environment, source, first_ns, last_ns, docs, pinned)`, upserted on export.
  - **Allow-list:** `mailroom.*`, `session.id`, `openinference.span.kind`, `llm.model_name`, `llm.provider`, `llm.token_count.*`, `llm.cost.*` and `exception.*`. `input.value` / `output.value` are kept only when `kind` is `CHAIN` or the span name starts with `mailroom.node.`.
- **Revision 4 amendments:** the span store lives in `storage/span_store.py` (not `obs/`); `run_id` and session are columns, no separate `runs` table is needed (`ledger` is the run registry); processor is a `BatchSpanProcessor` (the default `setup_tracing(exporter=)` path is synchronous and drops errors); masking is applied **inside** the store regardless of `trace_mask` (masking is off by default, `settings.py:188`, and an exporter attached before the masker sees raw attributes); only `exception.type` is kept, never `exception.message`; all strings are bounded; attach points are assigned for every entry point (`cli`, `watcher`, `api`, `eval`; `__init__.py:9` calls `setup_tracing()` with no exporter today); a per-run row cap applies. Prune removes span rows for non-kept runs per `MAILROOM_TRACE_KEEP`.
- **`prune()`** runs at startup and daily and skips showcase and pinned runs.
- **Query helpers:** `spans_for_run`, `spans_for_session`, `spans_for_doc`, `spans_between`, `list_runs` and `watermark()`.
- **`setup_tracing`** attaches the exporter behind `BatchSpanProcessor` after `MaskingSpanProcessor`.
- [ ] **Step 1: Write failing tests.**
  - Round trip of the spans from a fake run.
  - The allow-list drops `llm.input_messages.*` and an LLM span's `input.value`, even unmasked.
  - With masking on, kept `input.value` holds `<masked>`.
  - `runs` is upserted.
  - Pruning skips pinned runs.
  - Concurrent writers to the WAL file are safe.
- [ ] **Step 2–4:** run (FAIL), implement with SQLAlchemy Core in the `storage/db.py` style, then run (PASS).
- [ ] **Step 5: Commit** `feat(obs): sqlite span store with allow-list for local trace replay`.

### Task 4: Metrics `run_id`, decision metrics and Grafana links (Decision 2)

**Files:** Modify `obs/metrics.py`, `obs/tracing.py`, `eval/runner.py`, `deploy/grafana/dashboards/{pipeline,quality}.json`, `deploy/grafana/provisioning/*`, `deploy/docker-compose*.yml` (unchanged as built); Create `tests/obs/test_metrics_run_id.py`, `tests/deploy/test_grafana_links.py`.

**Interfaces:**
- **`M.<instrument>`** returns a thin wrapper whose `add`, `record` and `set` merge `current_run()` labels (`run_id`, `environment`). It falls back to `run_id="unscoped"` outside a scope.
- **New counters:** `mailroom.retries{kind}`, `mailroom.escalations{to}`, `mailroom.review.causes{cause}`.
- **Grafana:**
  - Dashboard `links`: `replay ↗` (`${public_url}/tui#replay=run:${run_id}`) and `phoenix ↗` (Phoenix project filtered on `mailroom.run_id=${run_id}`).
  - Panel data links on the per-run tables.
  - A new "decisions" row (retries, escalations, review causes by `run_id`).
- **`quality.json`** panels that query `mailroom_eval_*` are rewritten against emitted metrics (check `obs/metrics.py` and the collector's Prometheus names first).
- [ ] **Step 1: Write failing tests.**
  - An `InMemoryMetricReader` sees `run_id` and `environment` on every `mailroom.*` and `gen_ai.*` data point from a fake run, and never `doc_id`.
  - The live watcher uses `live-<date>`.
  - Grafana JSON has both links, and the `run_id` variable query is unchanged.
  - The `$run_id` variable query returns the seeded run and every `quality.json` panel query matches a metric name that is actually emitted.
  - Concurrency: with `Watcher(concurrency>1)` no data point is labelled `unscoped`.
- [ ] **Step 2–4:** run (FAIL), implement, then run (PASS).
- [ ] **Step 5: Commit** `feat(obs): run_id metric label, decision metrics, grafana replay links`.

### Task 5: `replay/v1` schema and timeline builder

**Files:** Create `schemas/replay.py`, `obs/replay/{__init__,timeline,sessions}.py`, `tests/obs/test_replay_timeline.py`, `tests/fixtures/replay/*`.

**Interfaces:**
- **Session ids:** `run:`, `session:`, `doc:`, `window:`. A bare id is treated as `run:`.
- **`list_sessions(limit)`** reads the `runs` table, `eval_docs` and recent `session.id` values.
- **`build_timeline(session_id, *, from_s=None, to_s=None)`:**
  - **Spans source (primary).** Section A builds `entities`, section B builds `segments`, LLM spans build `generations`, section C builds `events`, and section D builds `scores`.
  - **Roll-ups** follow The-Mailroom `metrics.compute_metrics` semantics:
    - verdict counts, review causes, first-pass rate;
    - p50 and p95 per station;
    - averages of the grounded scores.

    Score precedence is the same as in The-Mailroom: a score value beats summed generations.
  - **Audit fallback** (approximate):
    - `completed` / `node_failed` rows become segments, with `t0 = ts − elapsed_s`;
    - `gate_decision`, `parked`, `review_resolved` and `archived` rows become events;
    - for `run:` sessions, the documents come from `eval_docs`, and its tokens, cost and route trail fill in the entity totals.
- **Cache:** an LRU keyed `(session_id, watermark)`.
- [ ] **Step 1: Write failing tests** against fixtures (happy, retry, failed, parked, boss):
  - the spans and audit sources agree on station order and on durations within 50 ms;
  - scores appear at their span's end time;
  - roll-ups equal The-Mailroom `compute_metrics` on the same runs (vectors copied);
  - windowing sets `complete=false`;
  - no content fields are present.
- [ ] **Step 2–4:** run (FAIL), implement, then run (PASS).
- [ ] **Step 5: Commit** `feat(replay): replay/v1 timeline from spans with audit-log fallback`.

### Task 6: Import and export for offline replay

**Files:** Create `obs/replay/otlp_import.py`; Modify `cli.py` and `deploy/otel-collector.yaml` (a commented `file` exporter example).

**Interfaces:**
- `mailroom replay import <otlp.json|jsonl>` goes through the Task 3 allow-list.
- `mailroom replay export <session> [-o file]` writes `replay/v1` JSON.
- `mailroom replay sessions` lists sessions.
- [ ] **Steps:** failing tests (import then build; export then validate) → implement → pass → commit `feat(replay): otlp-json import and replay/v1 export cli`.

### Task 7: API routes

**Files:** Modify `api/app.py`; Create `tests/api/test_replay_api.py`.

**Interfaces:**
- `GET /v1/replay/sessions?limit=50` returns `[{id, kind, environment, documents, started_at, duration_s, source}]`.
- `GET /v1/replay/{session}?from=&to=` returns a `Timeline`. It returns 404 `no timeline for <session>` when there is no data, and is windowed when over the cap.
- `GET /v1/replay/live?since=<watermark>` is `text/event-stream`. It carries `segment`, `generation`, `event` and `score` messages, plus a heartbeat every 15 s.
- [ ] **Steps:**
  - Failing tests: 401 without a token on all three routes; the happy path; 404; 422 on a malformed session id; SSE yields a heartbeat plus a new segment.
  - Then implement, run (PASS) and commit `feat(api): /v1/replay sessions, timeline and live stream`.

### Task 8: Pure playback clock and model (JS)

**Files:** Create `api/tui/replay/{clock,model,stations}.js`, `tests/tui/js/replay_{clock,model}.test.mjs`.

**Interfaces:**
- **`createClock({duration, speeds:[0.25,0.5,1,2,4,8,16]})`** has `play`, `pause`, `toggle`, `seek`, `nudge`, `faster`, `slower`, `setSpeed` and `tick`. It clamps to the timeline and auto-pauses at the end.
- **`createModel(timeline)`** has `stateAt(t)`, `leaderboard(t)`, `eventsBetween`, `nextEventAfter`, `prevEventBefore`, `scoresAt(doc, t)`, `generationsAt(doc, t)`, `rollupsAt(t)` and `append(chunk)`.
  - `stateAt(t)` gives each document's station, attempt, status and cumulative tokens and cost.
  - Leaderboard order: archived (by finish time), then furthest station, then elapsed time ascending. Failed documents sort last.
- **`stations.js`** holds the station table mirrored from `obs/attrs.py`. The Python test asserts the two stay in sync.
- [ ] **Steps:**
  - Failing tests:
    - `stateAt` from play equals `stateAt` from seek;
    - seeking backwards across a retry restores attempt 1;
    - scores are hidden before their `t`;
    - `rollupsAt(t)` is monotonic for counts;
    - a 500 × 15 `stateAt` takes under 2 ms.
  - Then implement, run (PASS) and commit `feat(tui): pure replay clock and timeline model`.

### Task 9: Terminal takeover hook

**Files:** Modify `api/tui/terminal.js` and `tui.css`; Create `tests/tui/js/takeover.test.mjs`.

**Interfaces:**
- `ctx.takeover(factory) -> Promise<summary>` mounts a `.takeover` root (`role="application"`) and routes `keydown` to `onKey`.
- It restores the prompt and focus on `exit`, abort or a throw, and prints the summary line.
- [ ] **Steps:**
  - Failing tests: keys are not added to history; `exit`, a throw and Ctrl+C all restore the prompt.
  - Then implement, run (PASS) and commit `feat(tui): ctx.takeover for full-screen viewers`.

### Task 10: Replay command, character-grid view and panels

**Files:** Create `api/tui/commands/replay.js`, `api/tui/replay/{view,panels}.js`, `tests/tui/js/replay_view.test.mjs`; Modify `api/tui/main.js`, `tokens.css` (if approved) and `tui.css`.

**Interfaces:**
- **Command:** `replay [session] [--speed N] [--doc ID] [--follow]`.
  - With no session, it lists sessions.
  - Tab completion and a man page in the NAME / SYNOPSIS / DESCRIPTION / KEYS layout.
  - `ApiError` messages are reused from `commands/pipeline.js`.
- **Grid view (Decision 1):**
  - a fixed-width cell grid, diffed per frame so only changed cells are rewritten;
  - areas: header, station track with bays (REVIEW siding, FAILED), leaderboard, inspector, ticker, progress bar with event ticks;
  - a resize recomputes columns, and below 640 px the inspector stacks under the leaderboard.
- **Inspector** (The-Mailroom panels, at the playhead):
  - RUN: identity, GT, confidences, `failure_class`, `review_causes`, totals;
  - OBSERVATIONS: segments with station, kind, status, latency and error;
  - LLM GENERATIONS: role, model, tokens in/out, cost, latency, prompt version, retries;
  - SCORES: grouped by `SCORE_SPECS` group;
  - `phoenix ↗`: the trace URL in the run's Phoenix project.
- **`panels.js`:** `registerPanel({id, title, key, render(frame, out)})`, the counterpart of f1-race-replay's `PitWallWindow`. The first panels:
  - `metrics`: The-Mailroom METRICS tiles at *t*;
  - `tokens`: a cost and tokens sparkline;
  - `decisions`: gate, retry and escalation mix;
  - `latency`: p50 and p95 per station;
  - `fields`: per-field score heat row for the selected document.
- **Keys:**

  | Key | Action |
  | --- | --- |
  | `space` | Play or pause |
  | `←` / `→` | Seek ±5 s (`alt` for ±5%) |
  | `shift+←` / `shift+→` | Previous or next event |
  | `↑` / `↓` and `1`–`7` | Speed |
  | `r` | Restart |
  | `j` / `k` | Select a document |
  | `enter` | Pin the inspector |
  | `tab` | Cycle inspector sections |
  | `l` | Labels |
  | `p` | Panels |
  | `f` | Follow |
  | `?` | Help |
  | `q` / `esc` | Exit with a summary line |

- **Deep link:** `#replay=<session>`, which the Grafana links from Task 4 use.
- [ ] **Steps:**
  - Failing tests:
    - a hostile filename stays text in every area;
    - the empty-session message is exact;
    - approximate sessions are labelled;
    - `shift+→` lands on the next event;
    - an inspector score is hidden before its `t`;
    - a registered panel appears in the cycle;
    - a grid diff rewrites only changed cells.
  - Then implement, run (PASS) and commit `feat(tui): character-grid replay viewer with inspector and panels`.

### Task 11: Follow-live mode

**Files:** Modify `api/tui/replay/view.js` and `commands/replay.js`.

**Interfaces:**
- A `fetch`-streamed SSE reader, so the bearer header can be sent.
- The playhead is pinned to `now − 2 s`. Scrubbing back leaves follow mode, and `f` re-enters it.
- It stops when the tab is hidden or on abort, and reconnects with backoff of 1 s up to 30 s.
- The buffer keeps the last 2 hours, or the last 5,000 segments.
- [ ] **Steps:** failing tests → implement → pass → commit `feat(tui): replay follow-live over sse`.

### Task 12: Dev harness, docs and live verification

**Files:** Modify `scripts/tui_dev.sh` (seed an eval run with a retry, a failure, a parked document and a boss escalation), `docs/TUI.md`, `docs/OPERATIONS.md` (capture inventory, store, retention, metrics labels, Grafana links), `docs/CONFIGURATION.md` and `CHANGELOG.md`.

- [ ] **Step 1:** In Chromium (Playwright, `/opt/pw-browsers`), check the following:
  1. Open `/tui#replay=run:<seeded>`.
  2. Play, seek, jump to the next event and change speed.
  3. Use the inspector sections and confirm scores appear only after their span.
  4. Cycle the panels.
  5. Press `q` to exit and check the summary line.
  6. Check reduced motion and the `hc` theme.
  7. In Grafana, the `$run_id` dropdown lists the seeded run, and `replay ↗` opens the viewer on it.
  8. With the dev stack running, `--follow` shows a newly uploaded document moving along the track.
  9. The console has no errors.
- [ ] **Step 2:** `uv run pytest -q` and `uv run ruff check .` pass.
- [ ] **Step 3: Commit** `docs(replay): tui replay docs, dev harness seed and changelog`.

### Task 13: Archive ledger store, writer thread, Merkle root and verification

**Files:** Create `storage/ledger.py`, `schemas/ledger.py`, `tests/storage/test_ledger.py`; modify `storage/db.py` (add the two tables to the metadata). `storage/audit_log.py` is **not** modified.

**Interfaces:** `ledger.append(kind, run_id, doc_id=None, payload=..., metrics=...)` (queues), `ledger.flush()`, `ledger.entries(...)`, `ledger.verify(run_id=None) -> LedgerVerify{ok, broken_at, head, count, merkle_ok}`, `merkle_root(digests)`; hashing helpers shared with `schemas/audit.py`.

- [ ] **Step 1: Failing tests.** Genesis `""` and linked chain; tampering with a payload, a `prev_hash` or a deleted middle row is detected at the right `seq`; the Merkle root matches an independent recomputation and detects a changed or missing `doc_closed`; canonical JSON is byte-identical to the existing audit canonicalisation; `ledger_metrics` round-trips; the 5,000-row cap writes exactly one `gap`; 8 threads appending keep one linear chain; a repeated identical payload is **not** dropped; queue overflow drops metric rows but never a chain entry; flush on shutdown; an empty run fails verification when `run_closed` exists.
- [ ] **Step 2:** Implement with a single writer thread and batched `BEGIN IMMEDIATE`; record the throughput of the writer at concurrency 32 against the existing about 8 `audit_log` appends per document, and the verify time for a 5,000-row run.
- [ ] **Step 3: Commit** `feat(ledger): global hash-chained ledger, writer thread and merkle root`.

### Task 14: Ledger intake hooks and allow-list

**Files:** Modify `pipeline/flow.py` (`_drive`, `_record_node`, `_audit_gate`, `_fail_node`, `reconcile_archived`), `eval/runner.py` (`run_eval`, the exception path, `specialist_cell` rows), `watcher.py` (`run_document` scope, lazy rollover, `resume_processing` closeout), `obs/scores.py`; tests `tests/storage/test_ledger_intake.py`.

- [ ] **Step 1: Failing tests.**
  - One `doc_closed` per document invocation (live and eval) carrying the document's `audit_log` head.
  - `run_closed` Merkle root matches; `expected` mismatches are reported.
  - The hook fires on success, `NodeFailed`, other exceptions and a parked document, and a failure inside the hook never masks the original exception or turns a success into an eval error row.
  - A resumed document reports per-invocation deltas (no double-counted spend); an eval run does not inherit a live parked manifest's usage.
  - `usage_complete=false` when judge, boss or arbiter usage is missing.
  - A hostile fixture (filenames, exception text, judge notes, run ids, `reviewer`) produces no leaked content, with `trace_mask=False`.
  - Lazy rollover of a bucket opened by the current process closes it as `completed` (the day ended normally); a stale run left by a previous process (a kill during `_drive`, or a restart) is closed as `interrupted` on the next start.
- [ ] **Step 2:** Implement; the ledger is passed through `overrides` like `bins`; failures are logged, never raised.
- [ ] **Step 3: Commit** `feat(ledger): intake hooks for eval and live runs with allow-list`.

### Task 15: Retention, pinning, prune and showcase seed

**Files:** Create `storage/retention.py`, `src/mailroom_reloaded/showcase/*.json`; modify `storage/span_store.py`, `settings.py`; tests `tests/storage/test_retention.py`.

- [ ] **Step 1: Failing tests.** Default `pinned` keeps showcase and pinned runs and drops other span data older than the live window; `recent:2` keeps the last two; `all` keeps everything; the latest `policy` ledger entry overrides the environment default; pruning never removes ledger rows and appends `pruned`; a pruned run still lists and verifies as `data pruned`; showcase import is idempotent; a modified showcase file fails digest verification; the four showcase files load and replay.
- [ ] **Step 2:** Implement keep classes, prune on startup and daily, pin/unpin, showcase seed (clean; retries plus boss escalation; parked and failed; judge/arbiter heavy).
- [ ] **Step 3: Commit** `feat(retention): keep classes, pin/unpin, prune and showcase seed`.

### Task 16: Ledger API and TUI commands

**Files:** Modify `api/app.py` (`/v1/ledger*`, `/v1/runs` extension, relaxed run-id regex), `api/tui/main.js` (register `ledger`), `api/tui/commands/pipeline.js` (`runs pin|unpin|keep|--kept`); create `api/tui/commands/ledger.js`; tests `tests/api/test_ledger_api.py`, `tests/tui/js/ledger.test.mjs`; update the registered-names assertion in `pipeline.test.mjs`.

- [ ] **Step 1: Failing tests.** Token gate on every route; pagination bounds; `verify` returns `broken_at` on a tampered row; static routes win over path-param routes; `ledger` renders `SEQ KIND RUN DOC HASH PREV` with 12-character hashes, `genesis` on seq 1 and `chain: ok | broken at <seq>`; the `HOSTILE` fixture renders as text; an empty ledger prints `ledger empty`; man pages exist; `runs pin` rejects invalid ids; `live-<date>` ids pass.
- [ ] **Step 2:** Implement routes and commands (`textContent` only; reuse `fail()`).
- [ ] **Step 3: Commit** `feat(tui): ledger commands, /v1/ledger routes, runs pin/keep`.

### Task 17: `/ui` replay link and `#replay=` deep link

**Files:** Modify `api/ui/index.html` (`loadRuns` at about lines 162-174, header row at 72-73), `api/tui/main.js` (`start()` hash handler); docs `docs/TUI.md` (a "Deep links" section and the command table), `docs/OPERATIONS.md` ("Audit verification" gains the ledger; fix the stale `app.py:208-217` reference, the handler is at `:284-288`); tests in `tests/api/test_api.py` and `tests/tui/js/boot.test.mjs`.

- [ ] **Step 1: Failing tests.** `/ui` Eval runs has a third column with `href="/tui#replay=run:<encoded id>"`, `target=_blank rel=noopener`, and clicking it does not trigger the row's `loadCards`; the empty row `colspan` is 3; a hostile run id is encoded and rendered as text; `start()` with `#replay=run:<id>` runs `replay` after boot; an unknown or malformed hash is ignored.
- [ ] **Step 2:** Implement with `createElement` and `textContent` only.
- [ ] **Step 3: Commit** `feat(ui): replay link per eval run and #replay deep link`.

### Task 18: External anchor (Postgres / Supabase / export)

**Status:** implemented in PR #34 (`claude/trace-replay-08-anchor`). The SQL in `deploy/anchor/` and the Supabase key header are unverified against a live project.

**Files:** Create `storage/anchor.py`, `tests/storage/test_anchor.py`; modify `settings.py` (`anchor`, `anchor_url`, `anchor_key`, `anchor_key_file`, extended `_empty_to_none`), `cli.py` (`audit` sub-app: `verify`, `anchor`, `export-head`, lazy imports like `gmail_app`), `pyproject.toml` and `uv.lock` (optional `anchor` extra with `psycopg[binary]`), `docs/CONFIGURATION.md`, `docs/OPERATIONS.md`, `docs/ARCHITECTURE.md` (about lines 132-149), the `audit_log.py` docstring, `.env.example`, `CHANGELOG.md`.

- [ ] **Step 1: Failing tests (offline).** Real SQLite via `init_db` plus the ledger; `httpx.MockTransport` fakes in the style of `tests/agents/test_jev.py`; the Postgres table is exercised on `sqlite://` for conflict and idempotency. Cover: ok; unanchored tail; truncated; rewritten; store unreachable (exit 3); not configured (exit 4); Supabase non-2xx with read-back; duplicate-seq conflict; the pipeline append succeeds when a push raises; blank env means `none`; the key is read from the secrets file when only `anchor_key_file` is set and the env var wins when both are set; an unreadable or oversized key file fails closed with exit 4; a world-readable key file warns; secrets hidden from `repr` and from error text; idle system is not `STALE`; two concurrent pushers.
- [ ] **Step 2:** Implement with lazy driver imports; document that the plpgsql trigger, grants, RLS and the Supabase key header need a manual run against a staging project.
- [ ] **Step 3: Commit** `feat(anchor): optional postgres/supabase/export anchor for the ledger head`.

### Task 19: Complete LLM usage capture (judge, boss, arbiter, vision, grader)

**Why.** Today only the sorter and the class specialist reach `state.usage_total` (`flow.py:190`, `:211`). The judge, arbiter and boss run through CrewAI (`agents/judge.py:_run`, `arbiter.py`, `boss.py`) and discard `result.token_usage`; the existing `_usage()` helper at `agents/judge.py:82` is used only by eval-only `judge_grade`. Vision transcription calls `call_structured` once per page (`ingest/vision.py:53-89`) and drops each `res.usage`. The CrewAI paths also never reach `_record_usage_metrics` (`llm/client.py:210`), so `mailroom.llm_calls`, token and cost metrics miss them, `report.py:_cost_usd` prices everything at the sorter's rates, and `eval_docs` undercounts.

**Files:** Modify `llm/client.py`, `llm/usage.py`, `tools.py` (`ToolContext`), `agents/{judge,arbiter,boss}.py`, `ingest/{vision,clerk}.py`, `pipeline/{state,flow,report}.py`, `eval/runner.py`; tests `tests/agents/test_usage_capture.py`, `tests/ingest/test_vision_usage.py`, `tests/pipeline/test_usage_roles.py`.

**Interfaces**
- `llm/usage.py`: add `Usage.__sub__` (clamped at zero) for per-invocation deltas, and `RoleUsage = dict[str, Usage]` with an `add(role, usage)` helper.
- `llm/client.py`: a public `record_usage(role, usage)` that resolves the role's model and emits the same metrics `_record_usage_metrics` does (calls, tokens, duration where known, `cost_usd`), plus `cost_for(role, usage)` built on `_token_cost`. `call_structured` keeps its behavior and now calls the shared helper.
- `tools.py`: `ToolContext` gains a mutable `usage_sink` (a small list the agent functions append to), so the agent function signatures and return types stay unchanged.
- `agents/judge.py`, `arbiter.py`, `boss.py`: after `kickoff()`, convert `result.token_usage` with the existing `_usage()` helper, append it to `ctx.usage_sink` under the agent's role, and call `record_usage`. `judge_grade` does the same under role `grader`.
- `ingest/clerk.py` and `vision.py`: `IngestResult` gains `usage: Usage` (default empty); `transcribe_pages` sums every page's `res.usage` and records it under role `vision`; `_node_ingest` adds it to `state.usage_total`.
- `pipeline/state.py`: `MailroomState.usage_by_role: dict[str, Usage]` (default empty, so existing manifests load). `flow.py`: `_node_verify` and `_node_boss` drain the sink into `usage_total` and `usage_by_role`; the sorter and specialist add themselves by role; `_node_grade` records `grader`.
- `pipeline/report.py`: `_cost_usd` sums `cost_for(role, usage)` per role instead of the sorter's price.
- `eval/runner.py`: `eval_docs` token and call columns now include all roles; grading usage is stored as separate columns or in the existing JSON field so it does not inflate the pipeline totals.
- Ledger/replay: `doc_closed.usage_by_role`, per-node segment tokens/cost/calls in the timeline, and the inspector's usage section show every role. A node that raises keeps whatever its sink already collected and is listed in `usage_partial_nodes`; `usage_complete` is false only in that case.

- [ ] **Step 1: Failing tests.**
  - Fake `CrewOutput.token_usage` objects for judge, arbiter, boss and grader land in `usage_by_role`, `usage_total` and the right metrics (`llm_calls`, token usage, `cost_usd` labelled with the role's model).
  - A 3-page scanned PDF with a fake `call_structured` adds three calls and the summed tokens to the ingest result and to `usage_total`.
  - A resumed document reports only this invocation's delta (the restored manifest usage is not double-counted).
  - A node that raises after one sub-call keeps that partial usage and is flagged in `usage_partial_nodes`.
  - `compile_report` cost equals the sum of per-role costs; `eval_docs` tokens equal the sum of all roles and exclude grader usage from the pipeline totals.
  - Token-budget guards (`_guard_node`) see the larger totals and still pass for nodes with budget 0.
  - Manifests written before the change still load (default `usage_by_role`).
- [ ] **Step 2:** Run (FAIL), implement, run (PASS). Check what CrewAI's `token_usage` reports for this `base_url` and whether a litellm success callback is a better single capture point; if the object is empty for some providers, record `usage_complete=false` rather than guessing.
- [ ] **Step 3: Commit** `feat(usage): capture judge, arbiter, boss, vision and grader usage per role`.

## Phasing

Execution order (revision 4). Task numbers refer to the detailed tasks above; Tasks 6 (the OTLP-import half) and 11 are deferred.

| Phase | Tasks | Result |
| --- | --- | --- |
| **0. Capture, ledger, anchor, metrics** (start here; useful on its own) | 1, 19, 13, 14, 3, 2, 4, 18 | Run scope and vocabulary; complete per-role LLM usage; the ledger recording every run with its Merkle root; the span store; full span and score capture; `run_id` metric labels, decision counters and Grafana links; the optional external anchor. |
| **1. Replay MVP** | 5, 15, 7 (routes), 16, 8, 9, 10, 17, 12 | Timeline from spans (with the `eval_docs` approximate fallback), retention with four shipped showcase runs, the ledger API and TUI, the viewer, the `/ui` replay link, the dev seed and docs. |
| **2. Live** | 7 (SSE), 11 | Follow dev or production traffic as it happens. |
| **3. Portability** | 6 (OTLP file import) | Replay traces from another host. |

## Non-goals

- An OpenGL / Arcade desktop app, or a `<canvas>` track (Decision 1).
- Replacing Phoenix: the inspector links out for prompt and completion content.
- Re-introducing Langfuse: the dependency fence stays. Parity is achieved through OTel attributes.
- Per-run Phoenix projects (the tracer provider is a singleton; links filter on `mailroom.run_id`).
- Porting the LegalBench, relations and Gmail-triage traces from llm-mailroom; those features are out of scope in the design spec.
- Porting the dojo suite extras (`maud_*`, `content_topic_*`, …) beyond reserving their score names.
- Any mutation from the viewer, and video or GIF export.

## Open questions

Resolved: brand tokens (role aliases `--term-station-judge` / `--term-station-review`), The-Mailroom compatibility (Exios66/The-Mailroom#39), live `run_id` granularity (daily bucket for metric labels, per-document in the ledger), retention (Decision 5), hosted-judge equivalence (in-pipeline `verify` stays authoritative; `score_late` reserved), the `/ui` link, showcase selection (four runs), row cap (5,000 with a `gap`), and the anchor target (Postgres and Supabase, with a toggle).

Decided after review: the anchor uses HTTPS (Supabase over PostgREST) by default with Postgres optional; `anchor_key` also comes from a secrets file (`MAILROOM_ANCHOR_KEY_FILE`); and judge, boss, arbiter, vision and grader usage is captured in full (Task 19) for the audit ledger, visualization and replay.

Still open:
1. What does CrewAI's `token_usage` report for each provider configured in `taxonomy.yaml`? Task 19 checks this first; where a provider reports nothing, the entry says `usage_complete=false` instead of guessing.
2. ~~Should the Supabase key be a dedicated insert-only database role or the service key?~~ **Decided:** a dedicated insert-only role. The anchor DDL creates `mailroom_anchor_writer` with INSERT and SELECT on `mailroom_anchor` only; the service key is never used or documented as an option.

## Design review (revision 4)

The decisions above come from three proposer and three adversarial-reviewer passes, then a reconciliation and a code-reality review (all read-only Haiku subagents). Findings that changed the design:
- The shared-chain idea (`run-<id>` chains inside `audit_log`) was rejected for cost (`append` scans the whole chain) and dedupe (it would drop repeated pin/unpin payloads). The ledger tables with a single writer replace it; `audit_log.py` stays untouched.
- An eval hook placed on `run_document` never fires: eval reaches `_drive` directly. Hooks sit in `_drive`'s `finally`, `run_eval` and the cell path.
- Copying metrics back from a document chain mixes runs, because `doc_id` is the content sha and chains are shared. Metrics come from the invocation's own accumulator, as deltas.
- Anchoring "the global head" had nothing to anchor before the ledger existed; the ledger head is now the single anchored chain, and `doc_closed` pins each document chain head.
- Several claims were wrong and are corrected above: the wheel ships only `src/mailroom_reloaded`; `psycopg` is not locked (so HTTPS/Supabase is the default path); `SecretStr` is unused; `ts` strings differ between the hash and the column; exit code 2 collides with Click; per-run Phoenix projects fight the tracing singleton; `quality.json` queries metrics that are not emitted.

## Self-review

- **Decision 1** (character grid): Task 10, Global Constraints, and Non-goals.
- **Decision 2** (`run_id` metrics → Grafana → replay): Task 4, plus the Grafana check in Task 12.
- **Decision 3** (capture everything, llm-mailroom style):
  - Sections A–E cover every llm-mailroom trace field, node observation, score and log decision.
  - They cover every field The-Mailroom's interpreter consumed (`stage`, `routing_path` inputs, GT, intake flags, confidences, `review_decision`, `escalation_reason`, `failure_class`, verdict, quality, generations with usage, cost and prompt version, plus the score set).
  - They cover dojo per-field scoring and trace knobs.
  - They also capture the original's gaps: routing decisions as events, worker id, transport retries, served model, and optional TTFT.
- **f1-race-replay feature coverage:** play, pause, speed and seek; the track; the leaderboard; selection and telemetry; insight windows; the stream; the cache. Each maps to a task.
- **Consistency:** the station table is mirrored in `obs/attrs.py` and `replay/stations.js` and tested for sync. Score names live in one registry (`obs/scores.py`).
- **Proportion:** code appears only as interfaces, payload shape and test assertions.
- **Decisions 4-7** (revision 3):
  - Tiers (Decision 4): section D, `MAILROOM_REPLAY_TIER`, Task 14 and the `--tier` option in Tasks 10 and 16.
  - Retention and showcase (Decision 5): the Retention section and Task 15.
  - Archive ledger (Decision 6): the Archive ledger section and Tasks 13, 14 and 16. No ledger row is ever pruned, and prune is a ledger event.
  - `/ui` link and deep link (Decision 7): Task 17.
- **Decisions 8-11** (revision 4): reuse-first and the budget (Archive ledger section, `audit_log.py` untouched), the row cap (Task 13), the external anchor (Task 18), and the four showcase runs (Task 15).
- **Decisions 12-13** (revision 4, after review): complete LLM usage capture (Task 19, feeding the ledger `usage_by_role`, metrics, the report and the replay inspector) and the HTTPS-first anchor with a secrets-file key (Task 18).
