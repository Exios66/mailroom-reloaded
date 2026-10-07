# mailroom-reloaded — Design

**Date:** 2026-10-07 · **Status:** draft for review · **Source system:** `llm-mailroom` (+ `llm-dojo-scoring` v0.21.0, `local-mailroom-sandbox` SAND-37)

## 1. Intent

`mailroom-reloaded` is the compressed Digital Mailroom: only the plumbing needed to run the pipeline locally end to end, evaluate it against `mailroom-dataset`, and serve it on local or cloud GPUs. LangGraph, LangChain, Langfuse, Braintrust and the LiteLLM gateway are removed.

**Success criteria**

1. A document dropped in `inbox/` is classified, extracted, reported, catalogued and archived with a verifiable SHA-256 audit chain, using llamafile, OpenRouter, local vLLM or Modal vLLM.
2. Runs are viewable on localhost: traces in Phoenix (`:6006`), metrics dashboards in Grafana (`:3000`), runs and scorecards at `/ui` on the API (`:8000`).
3. `mailroom eval` reproduces the SAND-37 card: every metric in `SAND-37-MASTER-APPENDIX.md` plus the sorter and specialist KPIs in §8.
4. Specialists use the frozen v1 prompts byte for byte (sha256-locked).
5. Reviewer LLM nodes are gone. Routing is decided by a deterministic route gate, so a clean document costs at most 2 LLM calls (sorter + specialist).

**Out of scope:** Gmail intake/triage, relations scan, legalbench, Postgres, The-Mailroom visualizer (Langfuse-only), the free-model swarm and quota, Ollama, prompt mutation lineage beyond frozen v1.

## 2. Stack

| Concern | Choice |
| --- | --- |
| Orchestration | CrewAI **Flows** (`@start`, `@listen`, `@router`); Pydantic flow state |
| Agents | Boss, Arbiter, Judge as CrewAI `Agent`s with tools; sorter and specialists as single structured `crewai.LLM` calls with function calling |
| Model access | OpenAI-compatible endpoints only (`openai/` prefix + `base_url`); providers `llamafile`, `openrouter`, `vllm` (local or Modal URL), `mock` |
| Primary classifier | ModernBERT (`Lucius-Morningstar/mailroom-modernbert-classifier`) via `mailroom-ml[serve]` (ONNX/CPU), fail-open |
| Tracing | OpenTelemetry SDK → OTel Collector → Phoenix; OpenInference `CrewAIInstrumentor` + `OpenAIInstrumentor` |
| Metrics | OTel metrics → Collector → Prometheus → Grafana; Collector also scrapes vLLM `/metrics` and DCGM exporter |
| Storage | Filesystem bins + JSON manifests; SQLite (catalog, audit chain, eval runs) |
| API | FastAPI `/v1` + static `/ui` |
| Scoring | Vendored slim `llm-dojo-scoring` v0.21.0 subset |
| Deploy | Docker Compose (profiles), `deploy/modal_vllm.py` |

CrewAI's own telemetry is disabled (`CREWAI_DISABLE_TELEMETRY=true`, `OTEL_SDK_DISABLED` untouched). Instrumentation is installed before `crewai` is imported. Prompt/completion capture is on locally and masked by `MAILROOM_TRACE_MASK=1`.

## 3. Package layout

```
src/mailroom_reloaded/
  settings.py           env + taxonomy.yaml loader (single config contract)
  config/taxonomy.yaml  trimmed: classes, field types, confidence bands, agents, chunking, vision, model maps
  schemas/              extraction.py (5 Pydantic models + strict response_format), manifest.py, audit.py
  prompts/              frozen_v1/ (5 specialists + lineage.json), sand37/ (SAND-37 bytes), merger_maud_v1.txt,
                        sorter_v14.md, judge*.md, boss.md, arbiter.md, loader.py (sha256 check)
  scoring/              vendored dojo subset + PARITY.md
  llm/                  client.py (provider → crewai.LLM), retry.py, usage.py, tooling.py (tool loop + fallback)
  tools.py              one definition → CrewAI BaseTool and OpenAI function spec
  ingest/               clerk.py, pdf.py, vision.py, bert.py (adapter + handoff policy)
  agents/               sorter.py, specialists.py, gate.py, judge.py, arbiter.py, boss.py
  pipeline/             state.py, flow.py (MailroomFlow), guards.py, report.py, archivist.py
  storage/              bins.py, db.py, catalog.py, audit_log.py
  watcher.py, review.py
  obs/                  tracing.py, metrics.py
  api/                  app.py, ui/index.html
  eval/                 dataset.py, runner.py, metrics.py, cards.py, vllm_telemetry.py, cost.py
  cli.py                `mailroom run|watch|serve|eval|card|train-gate`
deploy/  Dockerfile, docker-compose.yml, otel-collector.yaml, prometheus.yml, grafana/, llamafile/, modal_vllm.py
```

## 4. Pipeline

```
ingest ─▶ bert_primary ─▶ sort ─▶ gate_classify ─┬▶ extract ─▶ gate_extract ─┬▶ report ─▶ catalog ─▶ archive ─▶ [grade]
  (det.)     (local)     (LLM 1)                  │   (LLM 2)                  ├▶ retry_extract (≤ retry_max)
                                                  ├▶ re_sort (FULL, once)      ├▶ verify: judge ─▶ arbiter
                                                  └▶ human_review (park)       ├▶ boss (escalation)
                                                                               └▶ human_review (park)
```

Deterministic nodes are ingest, bert_primary, gate_*, report, catalog and archive. They are ported from `agents/intake.py`, `agents/reporter.py` and `agents/archivist.py`, and they make no LLM calls.

**Ingest** proceeds in this order:

1. Run the clerk (`apply_intake`, `validate_intake`).
2. Extract text with pypdf, falling back to pdfplumber and then pymupdf.
3. If the text layer is empty, transcribe page images with the vision model.

**Durability** comes from the bins and the manifest:

- Bins are `inbox → processing/<worker> → classified|review|failed → archive`.
- A worker claims a file by atomic rename.
- Every node writes the manifest and appends an audit entry before returning.
- On restart the watcher resumes from the manifest's last completed node.
- CrewAI flow persistence is not used.

**Human review** works like this:

1. `human_review` parks the document in `review/` and ends the run with status `PARKED`.
2. `POST /v1/review/{doc_id}/resolve` takes `approve | correct{doc_type, doc_subclass} | reject`.
3. Approve or correct starts a new kickoff with `resume_from="extract"` and the corrected classification. Reject moves the document to `failed/`.

## 5. ModernBERT as primary classifier

`ingest/bert.py` returns a `BertVerdict`: `available`, `reason`, `doc_type`, `subclass`, `calibrated_confidence`, `margin`, `window_agreement`, `n_windows`, `route`. A `HandoffPolicy` turns that verdict into a `SortMode`. Rules are evaluated in order:

| Condition | SortMode | Sorter task |
| --- | --- | --- |
| BERT unavailable / error / flag off | `FULL` | primary + subclass |
| `doc_type ∈ bert.defer_classes` (default `contract`, `merger_agreement`) | `FULL` + BERT prior | primary + subclass |
| `n_windows > bert.max_trusted_windows` (default 1) | `FULL` + BERT prior | primary + subclass |
| `route == "fast_path"` | `SUBCLASS_ONLY`, `doc_type` locked | subclass within that class's vocabulary |
| otherwise | `FULL` + BERT prior | primary + subclass |

**How the sorter adapts.** In `SUBCLASS_ONLY` mode the sorter may return `doc_type_disagree: true` with a reason. `gate_classify` then routes once to `re_sort` in `FULL` mode. Subclass vocabularies come from the vendored dojo config (55 strata).

## 6. Route gate (replaces the reviewer nodes)

The `sorter_reviewer` and the review/retry LLM nodes are removed. `agents/gate.py` implements `RouteGate.decide(features: GateFeatures) -> GateDecision`. It is deterministic and makes zero LLM calls.

1. **Bands.** Per-class `low` / `high` / `judge_band_high` / `retry_max` come from `taxonomy.yaml` `confidence:`. Defaults are `low 0.70`, `high 0.95`, `retry_max 2`, and `judge_band_high` per class: contract 0.97, merger 0.97, corporate_record 0.97, correspondence 0.94, insurance 0.92. Confidence at or above `high` proceeds. Confidence below `low` retries, or parks once `retry_max` is spent.
2. **Learned gate.** This applies only inside the `[low, high)` band. It is a logistic model stored as plain JSON coefficients in `models/route_gate.json` and evaluated in numpy.
   - Features: BERT confidence, margin and window agreement; sorter confidence; attempts; schema validity; field coverage; whether the output hit the length cap; one-hot `doc_type`.
   - Labels: the dataset's evaluation contract (`retry_expected`, `review_expected`, `expected_stage`), joined to features logged by eval runs.
   - Training: `mailroom train-gate` (scikit-learn, dev extra).
   - Without the JSON file, the bands decide alone.
3. **Confidence source.** The sorter's confidence is `exp(Σ logprob)` over the tokens of the emitted label when the endpoint returns logprobs (vLLM, llamafile). Otherwise the model's self-reported confidence is used.

Any model (for example a hosted small classifier) can implement `RouteGate`. The flow depends only on the protocol.

## 7. Agents, prompts and tools

**Specialists** (contracts, merger_agreement, corporate_records, correspondence, insurance_claims):

- Prompts are the frozen v1 bytes from `llm-mailroom/src/llm/frozen_v1/lineage.json`, the current production lineage refrozen on 2026-10-05.
- The `sand37` prompt set holds the sandbox `*_simplified` bytes from `eval_environment_lineage.json`, so SAND-37 is exactly reproducible.
- Output is structured: the strict JSON schema of the class's Pydantic model goes in `response_format`, so vLLM guided decoding enforces it.
- Per-class run conditions come from SAND-37:

  | Class | Input cap (chars) | Output cap (tokens) | Temperature | Retries |
  | --- | ---: | ---: | ---: | ---: |
  | insurance | 13,500 | 8,192 | 0.1 | 2 |
  | contracts | 24,000 | 8,192 | 0.7 | 2 |
  | corporate | 15,000 | 8,192 | 0.1 | 2 |
  | correspondence | 12,000 | 8,192 | 0.1 | 2 |
  | merger | 30,000 head+tail | 8,192 | 0.7 | 2 |

- **Merger `dagger` mode** (config switch) uses the SAND-37 † settings:
  - Prompt `merger_agreement_specialist_maud_v1`.
  - The whole agreement in 47,000-char windows with 6,500-char overlap, merged afterwards.
  - Sampling `top_p 0.8`, `top_k 20`, `presence_penalty 1.0`; output cap 6,144.
  - One re-sample when the output hits the length cap.

**Sorter:** prompt `sorter_v14`, with a scoped suffix block in `SUBCLASS_ONLY` mode. The frozen text is never edited.

**Tools.** Each tool is defined once in `tools.py` and exposed two ways: as a CrewAI `BaseTool` for agents, and as an OpenAI function spec for the standalone LLM calls.

| Tool | Used by |
| --- | --- |
| `get_taxonomy` | sorter, boss, arbiter |
| `list_subclasses(doc_type)` | sorter, boss |
| `get_extraction_schema(doc_type)` | specialists, judge, arbiter |
| `get_field_types(doc_type)` | specialists, judge |
| `search_source(query)` | judge, arbiter, boss |
| `get_ground_truth(doc_id)` | judge only, eval mode only |

- **Standalone tool loop:** at most 3 rounds, then a forced final answer.
- **Endpoints without tool calling** (detected from a 400 response or no `tool_calls` support): fall back to a tool-free prompt with the tool results inlined.

**Judge**, a proper LLM-as-a-judge, runs in two modes:

- **Live** (`judge_verify`, no ground truth): source-grounded completeness and correctness check. Output: `JudgeVerdict{label, score, field_findings[]}`.
- **Eval** (`grade` step): receives the ground-truth fields for the document through `get_ground_truth` and grades every field `correct | partial | wrong | missing | hallucinated` with a rationale. Output: `JudgeGrade`.
- The deterministic dojo scorer stays authoritative for the KPIs. The judge's grade is recorded alongside, and its agreement with the scorer is reported as `judge_scorer_agreement`.

**Arbiter** settles disagreements between judge and specialist (`accept | accept_with_caveats | re_extract | escalate`). **Boss** handles escalations (`reassign_class | accept | human_review`).

## 8. Evaluation and metrics

**Ground truth** comes from `Lucius-Morningstar/mailroom-dataset`. The default revision is `v9.2`; `ed7576b6` is used to reproduce SAND-37 and the classifier canon. Configs are `default` (blind) and `ground_truth`, joined on `filename`, and `content_sha256` is checked at load.

The ground truth stays out of the agents' path by construction:

- Blind `Document` objects carry no label fields.
- Ground truth lives only in `eval/dataset.py` and is reachable from just two places: the `grade` step and the `get_ground_truth` tool.
- A leak test asserts that no sorter or specialist message contains a ground-truth value.

**Sorter KPIs:** exact match (doc_type and subclass both correct), primary-class accuracy, subclass accuracy (overall, and conditional on a correct primary), per-class confusion matrix. **BERT stage:** fast-path rate, defer rate, BERT primary accuracy on fast-path documents.

**Specialist KPIs**, per class:

- Field-level precision, recall, F1, F2 and micro F1 (pooled TP/FP/FN), plus mean per-document F1.
- Contracts: CUAD clause precision, recall, micro F1, labeled-doc mean F1 and value accuracy.
- Merger: MAUD accuracy, coverage, and precision on answered questions.
- Efficiency: tokens per document, $ per ok document, tokens/s/GPU.
- Schema compliance: schema-valid rate, parse errors, error kinds.
- Total run time: wall time, latency p50/p95/max.
- Mean suite score (comparable to SAND-37).

**SAND-37 card parity.** `eval/cards.py` emits `mailroom.card/v1` JSON and Markdown per specialist cell and per posture, with the same blocks as the sandbox card:

- `conditions`: model, gpu, gpu_usd_per_hour, replicas, concurrency, dataset repo/revision/seed/split, engine flags, prompt, caps, temperature.
- `cost`: busy, idle, $/doc, $/ok doc, $/1M tokens.
- `documents[]`.
- `engine_telemetry` per replica: requests, length finishes, preemptions, prefix-cache hit rate, mean TTFT, KV-cache usage.
- `latency`: mean, p50, p95, max.
- `quality`: ok, errors, error_kinds, parse_errors, schema_valid_rate, overall mean/sd/min/max, clause and MAUD blocks.
- `throughput`: docs/min, tok/s, tok/s/GPU.
- `time`: wall, gpu_seconds, cold_boot.
- `tokens`: prompt, completion, p95, max, share, plus the instruction/document/output split fitted as `prompt = I × calls + chars ÷ r`.
- `concurrency`: occupancy, parallelism, slot utilization.

A master card aggregates postures, matching `SAND-37-MASTER-SCORE-COST-CARD.md`.

**Engine telemetry** is the delta of vLLM `/metrics` counters per replica, taken before and after each cell. Counters scraped:

- `vllm:request_success_total`, `vllm:num_preemptions_total`
- `vllm:prefix_cache_hits_total` / `vllm:prefix_cache_queries_total`
- `vllm:prompt_tokens_total`, `vllm:generation_tokens_total`
- `vllm:time_to_first_token_seconds_{sum,count}`
- `vllm:kv_cache_usage_perc`
- length finishes from `vllm:request_success_total{finished_reason="length"}`

**Live OTel metrics** (multi-container, multi-GPU):

- **Application** (exported with `service.name`, `service.instance.id`, `container.id`, `gpu.replica` resource attributes):
  - `gen_ai.client.token.usage`, `gen_ai.client.operation.duration` (GenAI semconv)
  - `mailroom.documents` (counter by stage, status, doc_type)
  - `mailroom.node.duration` (histogram by node)
  - `mailroom.llm.calls` (by role, provider, model)
  - `mailroom.gate.decisions` (by stage, decision)
  - `mailroom.bert.route` (fast_path, defer, unavailable)
  - `mailroom.schema.valid` and `mailroom.length_capped` (counters)
  - `mailroom.cost.usd` (counter)
  - `mailroom.queue.depth` (gauge per bin)
  - `mailroom.inflight` (gauge per worker)
- **Engine**, scraped by the Collector: `vllm:num_requests_running|waiting`, `vllm:kv_cache_usage_perc`, `vllm:e2e_request_latency_seconds`, `vllm:time_to_first_token_seconds`, `vllm:time_per_output_token_seconds`, `vllm:iteration_tokens_total`, `vllm:num_preemptions_total`, prefix-cache counters.
- **GPU**, from DCGM: `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_FB_USED`, `DCGM_FI_DEV_POWER_USAGE`, `DCGM_FI_DEV_SM_CLOCK`, `DCGM_FI_DEV_GPU_TEMP`, `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`.
- **Container**, from the Collector `docker_stats` receiver: CPU, memory, network.

## 9. Docker topology

There is one `deploy/docker-compose.yml` with profiles:

| Service | Profile | Port | Role |
| --- | --- | --- | --- |
| `app` | default | 8000 | API + embedded watcher + `/ui` |
| `watcher` | `split-watcher` | — | standalone watcher (holds `watcher.lock`; `app` runs with `MAILROOM_EMBED_WATCHER=0`) |
| `otel-collector` | default | 4317/4318, 8889 | OTLP in; traces → Phoenix; metrics → Prometheus; scrapes vLLM + DCGM + docker_stats |
| `phoenix` | default | 6006 | trace UI |
| `prometheus` | default | 9090 | metrics store |
| `grafana` | default | 3000 | provisioned dashboards: Pipeline, Serving & GPU, Quality |
| `llamafile` | `local-llm` | 8080 | CPU/GPU llamafile server |
| `vllm` | `gpu` | 8001 | local vLLM, `--tensor-parallel-size` / `--data-parallel-size` from env, `/metrics` |
| `dcgm-exporter` | `gpu` | 9400 | GPU metrics |

**Image build:** multi-stage, non-root uid 10001, ModernBERT bundle staged from the Hub at build (`ML_BUILD_NONE=1` gives a lean image).

**Off-loopback bind** requires `MAILROOM_API_TOKEN`.

**Modal:** `deploy/modal_vllm.py` serves an OpenAI-compatible vLLM with `/metrics`, an L4 default, and a configurable GPU count and replicas. The app points `VLLM_BASE_URL` at it, and the Collector scrapes its `/metrics` through `VLLM_METRICS_URLS`.

## 10. Error handling

| Failure | Behaviour |
| --- | --- |
| Endpoint cold start / 5xx / timeout | `llm/retry.py` exponential backoff; Modal cold start allowance 90 s; transient errors do not consume the confidence retry budget |
| Output hits length cap | error_kind `LengthFinishReasonError`; merger dagger re-samples once; otherwise counted as error, document → `failed/` |
| Malformed JSON | one repair re-ask; then `parse_error`, → retry_extract budget |
| Unknown tool name / bad tool args | tool returns an error string to the model; loop limit 3 |
| Endpoint lacks tool calling | inline-tool fallback (§7) |
| BERT missing or failing | fail-open to `FULL` sort |
| Unreadable / empty document | `failed/` with audit entry `ingest_failed` |
| Node deadline / token budget exceeded (`guards.py`) | node aborts, manifest `status=failed`, audit entry |
| Watcher crash mid-document | manifest resume from last completed node |

## 11. Testing

- **Unit tests per module.** An in-process fake OpenAI-compatible server (`tests/fakes/openai_server.py`) scripts responses: content, `tool_calls`, logprobs, `finish_reason="length"`, 400 for tools.
- **Flow tests** with the `mock` provider cover every route: fast path, contract defer, subclass disagree → re-sort, retry → park, verify → arbiter, boss escalation, human review resume.
- **Scoring parity:** when the pinned `llm-dojo-scoring` is installed, `scoring/` outputs match upstream on fixtures.
- **Prompt lock:** sha256 of every vendored prompt matches `lineage.json`.
- **GT leak test.**
- **Dependency fence:** no `langgraph`, `langchain`, `langfuse` or `litellm` imports anywhere in `src/`.
- **Compose:** `docker compose config` passes for every profile combination.
- **Live tests** are marked `@pytest.mark.live`: tool calling and structured output against the configured endpoint.
