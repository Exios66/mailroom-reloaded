> **SUPERSEDED 2026-10-09** by [the core plan](../../plans/2026-10-09-mailroom-core-plan.md). Kept for history only; do not edit or add tasks here.

# mailroom-reloaded Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. **Execution method chosen by the user: subagent-driven, with every implementer and reviewer subagent dispatched with `model: "sonnet"`.** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the compressed Digital Mailroom. It runs end to end locally on CrewAI Flows, a ModernBERT primary classifier and a deterministic route gate, with OTel/Phoenix/Grafana observability, SAND-37-parity evaluation cards, and Docker/Modal deployment.

**Architecture:**
- A CrewAI Flow orchestrates the pipeline: deterministic nodes (ingest, BERT, gates, report, archive), two LLM calls per clean document (sorter, specialist), and CrewAI agents (judge, arbiter, boss) only on escalation paths.
- Durability is filesystem bins, JSON manifests and a SQLite hash-chained audit log.
- Evaluation joins blind outputs to `mailroom-dataset` ground truth and scores them with a vendored dojo subset.

**Tech Stack:** Python 3.11 · crewai ≥1.8,<2 · openai · pydantic v2 · FastAPI/uvicorn · SQLite (sqlalchemy) · pypdf/pdfplumber/pymupdf · OpenTelemetry SDK + OTLP · openinference-instrumentation-{crewai,openai} · arize-phoenix (container) · Prometheus/Grafana · mailroom-ml[serve] (optional) · datasets (eval extra) · scikit-learn (dev extra) · modal==1.5.5 (deploy extra) · pytest

**Spec:** `docs/superpowers/specs/2026-10-07-mailroom-reloaded-design.md`

**Source repos** (read-only; port from these paths):
- `L` = `/Users/luciusjmorningstar/Downloads/llm-mailroom`
- `D` = `/Users/luciusjmorningstar/Downloads/llm-dojo-scoring`
- `S` = `/Users/luciusjmorningstar/Downloads/local-mailroom-sandbox`

## Global Constraints

- Python `>=3.11,<3.13`; package `mailroom_reloaded` under `src/`; console script `mailroom`.
- No `langgraph`, `langchain*`, `langfuse`, `braintrust` or `litellm` in dependencies or imports (enforced by test).
- `crewai>=1.8,<2`. Models are addressed as `openai/<model>` with an explicit `base_url`. Set `CREWAI_DISABLE_TELEMETRY=true` before `crewai` is imported.
- Providers: `llamafile`, `openrouter`, `vllm`, `mock`. No Ollama.
- Frozen prompt bytes are sha256-locked to `prompts/frozen_v1/lineage.json` (from `L/src/llm/frozen_v1/lineage.json`) and `prompts/sand37/lineage.json` (from `S/config/prompts/eval_environment_lineage.json`).
- Dataset `Lucius-Morningstar/mailroom-dataset`: default revision `ed7576b6` (SAND-37, the BERT canon and llm-mailroom 0.8.0); `v9.2` opt-in; seed 42.
- The gate and calibration are fitted on `split="train"` only; KPIs are reported on `split="test"` only.
- vLLM requests send `extra_body={"chat_template_kwargs":{"enable_thinking":False}}` (SAND-37 engine condition).
- Tool rounds never carry `response_format`; the final turn uses `tool_choice="none"` with the strict `response_format`.
- GPU price default: L4 `$0.80` per GPU-hour.
- Confidence defaults: `low 0.70`, `high 0.95`, `retry_max 2`. `judge_band_high` by class: contract 0.97, merger_agreement 0.97, corporate_record 0.97, correspondence 0.94, insurance_claim 0.92.
- Specialist run conditions (input cap chars / output cap tokens / temperature):

  | Class | Input cap | Output cap | Temperature |
  | --- | ---: | ---: | ---: |
  | insurance_claim | 13,500 | 8,192 | 0.1 |
  | contract | 24,000 | 8,192 | 0.7 |
  | corporate_record | 15,000 | 8,192 | 0.1 |
  | correspondence | 12,000 | 8,192 | 0.1 |
  | merger_agreement | 30,000 head+tail | 8,192 | 0.7 |

  All classes use retries 2.
- Merger dagger settings: windows of 47,000 chars with 6,500 overlap, `top_p 0.8`, `top_k 20`, `presence_penalty 1.0`, output cap 6,144, one re-sample on a length cap.
- The ground truth is never visible to the sorter or the specialists.
- Off-loopback API bind refuses to start without `MAILROOM_API_TOKEN`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Huge documents** (a median merger runs about 380k chars): ingest must not stall, BERT defers, the specialist caps or chunks, and nothing sends more than the cap. *Test in Tasks 10 and 12.*
2. **Scanned, empty or corrupt files:** the document goes to `failed/` with an `ingest_failed` audit entry and the watcher keeps running. *Test in Tasks 9 and 17.*
3. **Endpoint without tool calling** (many llamafile models): inline-tool fallback still yields schema-valid output. *Test in Task 7.*
4. **Modal cold start and length-capped output:** retries don't burn the confidence budget, and `LengthFinishReasonError` is recorded as the error kind. *Test in Tasks 7 and 12.*
5. **Two workers against one inbox, and a crash mid-document:** exactly one claim wins, and a restart resumes from the manifest without duplicate audit entries. *Test in Tasks 5 and 17.*

---

### Task 1: Scaffold, settings, taxonomy, dependency fence

**Files:**
- Create: `pyproject.toml`, `src/mailroom_reloaded/__init__.py`, `src/mailroom_reloaded/settings.py`, `src/mailroom_reloaded/config/taxonomy.yaml`, `tests/conftest.py`, `tests/test_settings.py`, `tests/test_dependency_fence.py`, `.env.example`, `.gitignore`
- Modify: `README.md`

**Interfaces:**
- Produces:
  - `load_taxonomy() -> Taxonomy`, cached.
  - `Taxonomy` has `.classes: dict[str, DocClass]`, `.confidence_for(doc_type: str | None) -> Thresholds`, `.agent(name) -> AgentCfg`, `.bert: BertCfg`, `.specialist_conditions(doc_type) -> RunConditions`.
  - `DocClass(key, label, schema, specialist, description, field_types: dict[str,str])`
  - `Thresholds(low, high, judge_band_high, retry_max)`
  - `BertCfg(defer_classes: list[str], max_trusted_windows: int, enabled: bool)`
  - `RunConditions(input_cap_chars, output_cap_tokens, temperature, retries, merger_mode: Literal["frozen","dagger"])`
  - `Settings` (pydantic-settings) with `base_dir: Path`, `provider`, `vllm_base_url`, `openrouter_api_key`, `llamafile_base_url`, `api_token`, `trace_mask: bool`, `gpu_usd_per_hour=0.80`.
  - `get_settings() -> Settings`.

**Steps:**

- [x] **Step 1: Write the failing tests.** Expected assertions:
  - `test_taxonomy_has_five_live_classes`: `set(load_taxonomy().classes) == {"contract","merger_agreement","corporate_record","correspondence","insurance_claim"}`.
  - `test_confidence_by_class`: `confidence_for("insurance_claim").judge_band_high == 0.92`, `confidence_for(None).low == 0.70`.
  - `test_specialist_conditions`: `specialist_conditions("contract") == RunConditions(24000, 8192, 0.7, 2, "frozen")`.
  - `test_no_forbidden_imports`: walk `src/` with `ast`; no import root in `{"langgraph","langchain","langchain_core","langchain_openai","langfuse","braintrust","litellm"}`. Also parse `pyproject.toml` dependencies for the same names.
- [x] **Step 2: Run the tests and confirm they fail.** Run `uv run pytest tests/test_settings.py tests/test_dependency_fence.py -v`. Expected: ImportError.
- [x] **Step 3: Write `pyproject.toml`, `settings.py` and `taxonomy.yaml`.**
  - `pyproject.toml` (hatchling):
    - Core deps: crewai, openai, pydantic, pydantic-settings, pyyaml, structlog, fastapi, uvicorn, sqlalchemy, pypdf, pdfplumber, pymupdf, python-docx, reportlab, watchdog, httpx, python-dateutil, jellyfish, numpy, opentelemetry-sdk, opentelemetry-exporter-otlp, openinference-instrumentation-crewai, openinference-instrumentation-openai.
    - Extras: `bert` (mailroom-ml[serve] @ git+https://github.com/LLM-Mailroom-Services/mailroom-ml.git), `eval` (datasets, pandas, matplotlib), `embeddings` (scipy, sentence-transformers), `deploy` (modal==1.5.5), `dev` (pytest, pytest-asyncio, respx, scikit-learn, ruff), `parity` (llm-dojo-scoring @ git tag v0.21.0, same URL as in `L/pyproject.toml`).
  - `taxonomy.yaml`: copy from `L/src/config/taxonomy.yaml`, keeping `pipeline.bins`, `llm_retry`, `vllm_model_map`, `llamafile_model_map`, `chunking`, `cost_models`, `confidence`, `field_scoring`, `vision`, `doc_classes`, `file_extensions`, and `agents` (sorter, intake, arbiter, the 5 specialists, reporter, boss, pdf_transcriber, image_extractor, judge).
    - Drop `gateway`, `free_model_swarm`, `free_quota`, `ollama_model_map`, `relations`, `gmail_triage`, `sorter_reviewer`.
    - Add a `bert:` block (`enabled: false`, `defer_classes: [contract, merger_agreement]`, `max_trusted_windows: 1`).
    - Add a `specialist_conditions:` block with the Global Constraints values.
- [x] **Step 4: Run the tests and confirm they pass.** Same command. Expected: PASS.
- [x] **Step 5: Commit.** `git add -A && git commit -m "feat: scaffold package, taxonomy contract, dependency fence"`

### Task 2: Vendored scoring subset

**Files:**
- Create: `src/mailroom_reloaded/scoring/{__init__,config,gt_metadata,intents,equivalences,corpus,field_scoring,extraction_metrics,classification,maud,intake}.py`, `src/mailroom_reloaded/scoring/PARITY.md`, `tests/scoring/test_scoring.py`, `tests/scoring/test_parity.py`

**Interfaces:**
- Produces these re-exports from `scoring/__init__.py`, with upstream signatures unchanged:
  - `score_extraction(doc_type, predicted, expected) -> ExtractionScoreResult`
  - `score_field`
  - `extraction_binary_metrics(result) -> dict` (keys `tp, fp, fn, precision, recall, f1`)
  - `merge_extraction_counts(rows) -> dict`
  - `fbeta(precision, recall, *, beta=1.0) -> float`
  - `exact_match`, `accuracy`, `confusion_matrix`, `per_class_stats`, `macro_prf`, `normalize_label`
  - `canonical_maud_class`, `maud_question_catalog`
  - `looks_messy`, `INTAKE_SPAN_KEYS`
  - `normalize_corpus_subclass`
  - `subclass_vocab(doc_type) -> list[str]`: new, built from `config.CONTRACT_SUBTYPE_KEYS`, `MAUD_CONSIDERATION_TYPES` and the other per-class subtype lists in dojo `config.py`.

**Steps:**

- [x] **Step 1: Copy the modules.** Copy them verbatim from `D/llm_dojo_scoring/` and rewrite `llm_dojo_scoring.` imports to relative imports. Strip unused functions only if they import modules outside this set. Record the upstream tag and per-file sha256 in `PARITY.md`.
- [x] **Step 2: Write the tests.**
  - `test_fbeta_f2`: `fbeta(0.5, 1.0, beta=2) == pytest.approx(0.8333, abs=1e-4)`.
  - `test_money_exact_after_normalisation`: `score_field("money", "$1,000.00", "1000")` == 1.0.
  - `test_date_partial_credit`: same year and month but a different day scores between 0 and 1.
  - `test_entity_list_hungarian`: two reordered names score 1.0.
  - `test_subclass_vocab_covers_dataset_strata`: the union over the 5 classes has `len >= 55` and contains `"all_cash"`.
  - `test_parity_with_upstream`: `pytest.importorskip("llm_dojo_scoring")`. For 10 fixture pairs in `tests/scoring/fixtures/pairs.json` (copied from `L/src/tests` dojo parity fixtures), vendored `score_extraction(...).overall` equals upstream.
- [x] **Step 3: Run and fix until all pass.** Run `uv run pytest tests/scoring -v`. Expected: PASS (parity is SKIPPED without the `parity` extra).
- [x] **Step 4: Commit.** `git commit -am "feat: vendor slim llm-dojo-scoring v0.21.0 subset"`

### Task 3: Prompts with sha256 lock

**Files:**
- Create:
  - `src/mailroom_reloaded/prompts/loader.py`
  - `prompts/frozen_v1/{contracts,merger_agreement,corporate_records,correspondence,insurance_claims}_specialist.txt` + `lineage.json`
  - `prompts/sand37/*.txt` + `lineage.json`
  - `prompts/merger_agreement_specialist_maud_v1.txt`, `prompts/sorter_v14.md`, `prompts/sorter_subclass_scope.md`
  - `prompts/judge_completeness.md`, `prompts/judge_grade.md`, `prompts/arbiter.md`, `prompts/boss.md`, `prompts/vision.md`
  - `tests/test_prompts.py`

**Interfaces:**
- Produces:
  - `load_prompt(name: str, *, prompt_set: Literal["frozen_v1","sand37"]="frozen_v1") -> str`
  - `PromptLockError(Exception)`
  - `prompt_sha256(name, prompt_set) -> str`
  - `SPECIALISTS: tuple[str, ...]` = the 5 `*_specialist` names.

**Steps:**

- [x] **Step 1: Export the prompt bytes.**
  - frozen_v1: write `D.prompts.get_prompt(agent, family="production_prompts").text` for each agent and check it against `L/src/llm/frozen_v1/lineage.json` sha256. Store the text after the provenance HTML comment only if that is what the lineage hash covers; verify both ways and keep the variant whose sha matches.
  - sand37: copy `S/config/prompts/<sandbox_stem>.txt` byte-identical and copy the lineage JSON.
  - maud_v1: copy from `S/config/prompts/merger_agreement_specialist_maud_v1.txt`.
  - sorter: copy `D/.../templates/sorter.production.md`.
  - Judge, arbiter and boss: copy from `judge.production.md`, `judge-correctness.production.md`, `arbiter.production.md` and `boss.production.md`.
  - `judge_grade.md`: new. It instructs field-by-field grading against supplied ground truth, using the labels `correct|partial|wrong|missing|hallucinated` and JSON output matching `JudgeGrade` (Task 14).
  - `sorter_subclass_scope.md`: new. It covers the locked `doc_type`, the allowed subclass list placeholder `{subclasses}`, and the `doc_type_disagree` escape.
- [x] **Step 2: Write the tests.**
  - `test_every_frozen_prompt_matches_lineage` (both sets, all 5 specialists).
  - `test_tampered_prompt_raises`: monkeypatch the read bytes; expect `PromptLockError`.
  - `test_unknown_prompt_keyerror`.
- [x] **Step 3: Implement `loader.py`.** Use `importlib.resources`. Check the sha256 for any name listed in a lineage file; cache with `lru_cache`.
- [x] **Step 4: Run the tests.** Run `uv run pytest tests/test_prompts.py -v`. Expected: PASS.
- [x] **Step 5: Commit.** `git commit -am "feat: vendor frozen_v1 and sand37 prompts with sha256 lock"`

### Task 4: Extraction schemas and schema compliance

**Files:**
- Create: `src/mailroom_reloaded/schemas/extraction.py`, `tests/schemas/test_extraction.py`

**Interfaces:**
- Produces:
  - The models `ContractExtraction`, `MergerAgreementExtraction`, `CorporateRecordExtraction`, `CorrespondenceExtraction` and `InsuranceClaimExtraction`, ported from `L/src/schemas/documents.py`.
  - `get_extraction_schema(doc_type) -> type[BaseModel]`
  - `response_format(doc_type) -> dict`, an OpenAI `{"type":"json_schema","json_schema":{"name","strict":True,"schema"}}`. Every property is required and nullable; `additionalProperties: false`.
  - `assess_payload(doc_type, raw: str | dict) -> SchemaAssessment(parsed: dict | None, schema_valid: bool, parse_error: str | None, coerced_fields: list[str])`, ported from `S/src/mailroom_sandbox/eval/schema_adherence.py` (`coerce_predicted_payload`, `_parse_object_text`, `_pydantic_valid`).

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_response_format_is_strict` covers every class: `additionalProperties is False`, and `required` equals all property names.
  - `test_insurance_gt_shape_validates`: a dict built from the dataset column list in the spec validates. Money arrives as a string `"$12,500.00"`, and the dates are ISO strings.
  - `test_assess_parses_fenced_json`: input `"```json\n{...}\n```"` gives `schema_valid` True.
  - `test_assess_reports_parse_error`.
- [x] **Step 2: Run the tests and confirm they fail.**
- [x] **Step 3: Implement.** Add `field_validator(mode="before")` coercers for money and date fields so the insurance schema accepts the value formats the model emits. This is the "optimized schema" change that addresses the 0.20–0.30 schema-valid rate SAND-37 measured.
- [x] **Step 4: Run the tests and confirm they pass.** Run `uv run pytest tests/schemas -v`.
- [x] **Step 5: Commit.** `git commit -am "feat: strict extraction schemas and compliance assessment"`

### Task 5: Bins, manifests and atomic claim

**Files:**
- Create: `src/mailroom_reloaded/storage/bins.py`, `src/mailroom_reloaded/schemas/manifest.py`, `tests/storage/test_bins.py`

**Interfaces:**
- Produces:
  - `Bins(base: Path)` with `.inbox`, `.processing(worker_id)`, `.classified`, `.review`, `.failed`, `.archive` and `.manifests` (all `Path`).
  - `Bins.claim(path: Path, worker_id: str) -> Path | None`, an atomic `os.rename`. It returns `None` if another worker won.
  - `Bins.move(path, bin_name) -> Path`
  - `Manifest(doc_id, filename, content_sha256, status: Literal["processing","parked","failed","archived"], completed_nodes: list[str], state: dict, updated_at)`
  - `save_manifest(bins, m)` (atomic write via tmp + rename)
  - `load_manifest(bins, doc_id) -> Manifest | None`
  - `doc_id_for(path) -> str`, the first 16 hex characters of the content sha256.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_claim_race_single_winner`: 8 threads claim one file and exactly one gets a non-None path.
  - `test_manifest_roundtrip_atomic`.
  - `test_resume_point`: a manifest with `completed_nodes=["ingest","bert_primary","sort"]` has `next_node == "gate_classify"`. The node order is supplied by Task 16 as `NODE_ORDER` and imported lazily; until then the test uses a local list.
- [x] **Step 2: Implement** by porting `L/src/pipeline/bins.py`. Keep it to the functions above.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/storage/test_bins.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: filesystem bins, manifests, atomic claim"`

### Task 6: SQLite catalog and hash-chained audit log

**Files:**
- Create: `src/mailroom_reloaded/storage/{db,catalog,audit_log}.py`, `src/mailroom_reloaded/schemas/audit.py`, `tests/storage/test_audit.py`

**Interfaces:**
- Produces:
  - `AuditLogEntry(doc_id, seq, node, event, payload: dict, ts, prev_hash, entry_hash)`
  - `append(doc_id, node, event, payload) -> AuditLogEntry`
  - `entries(doc_id) -> list[AuditLogEntry]`
  - `verify_chain(entries) -> ChainResult(ok: bool, broken_at: int | None)`
  - `catalog.upsert(record: CatalogRecord)`
  - `catalog.get(doc_id)`
  - `catalog.list(limit, offset, status=None)`
  - `init_db(path) -> Engine`, with the WAL journal mode enabled.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_chain_verifies`: 5 appends, then `verify_chain(...).ok`.
  - `test_tamper_detected`: edit row 3's payload in SQL; expect `broken_at == 3`.
  - `test_append_idempotent_per_node`: appending the same `(doc_id, node, event)` twice with an identical payload stores one entry. This prevents duplicate entries on resume (Review Focus 5).
- [x] **Step 2: Implement** by porting `L/src/schemas/audit.py` (hash = sha256 over canonical JSON of the entry without `entry_hash`, plus `prev_hash`), `storage/audit_log.py`, `storage/catalog.py` and `storage/db.py`.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/storage -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: sqlite catalog and sha256 audit chain"`

### Task 7: LLM client, retry and tool loop (standalone LLMs)

**Files:**
- Create:
  - `src/mailroom_reloaded/llm/{__init__,client,retry,usage,tooling}.py`
  - `tests/fakes/openai_server.py`
  - `tests/llm/test_client.py`, `tests/llm/test_tooling.py`

**Interfaces:**
- Consumes: `Settings` and `load_taxonomy` from Task 1.
- Produces:
  - `resolve(role: str) -> ResolvedModel(provider, model, base_url, api_key, supports_tools: bool | None, supports_logprobs: bool)`
  - `make_llm(role, **overrides) -> crewai.LLM`, using `model=f"openai/{m}"`, `base_url`, `api_key` and the temperature and max_tokens from the overrides.
  - `call_structured(role, messages, *, schema_doc_type: str | None, tools: list[ToolDef] = (), logprobs=False, **sampling) -> LLMResult(content: str, parsed: dict | None, usage: Usage, finish_reason: str, label_logprob: float | None, tool_rounds: int)`
  - `Usage(prompt_tokens, completion_tokens, latency_s, calls)`
  - `LengthFinishReasonError(Exception)`
  - `with_retry(fn, *, cold_start_s=90, max_attempts=4)`

  `call_structured` uses the `openai` SDK directly against the same resolved endpoint. That gives deterministic control over `tool_calls`, `logprobs` and `finish_reason`. `make_llm` is used by the CrewAI agents.
- Fake server: `FakeOpenAI` is a fixture that runs a uvicorn FastAPI app on a free port and holds a queue of scripted responses. Helpers: `.reply(content)`, `.tool_call(name, args)`, `.length_capped()`, `.reject_tools()`, `.with_logprobs(tokens)`. It records `requests`.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_resolve_vllm_uses_base_url`
  - `test_tool_loop_executes_and_finishes`: script `tool_call("list_subclasses", {"doc_type":"correspondence"})` then a JSON reply. Assert that `tool_rounds == 1` and that the second request's messages contain a `role:"tool"` message with the tool output.
  - `test_tool_loop_cap_three_rounds`: 4 consecutive tool calls; the 4th request carries `tool_choice="none"`.
  - `test_inline_fallback_when_tools_rejected`: the server returns 400 on any request with `tools`. The client retries without `tools`, with the tool results inlined in the system message, and `parsed` is schema-valid.
  - `test_length_cap_raises`: `finish_reason="length"` raises `LengthFinishReasonError`.
  - `test_cold_start_retry_not_budgeted`: two 503 responses then 200 succeed. The `LLMResult` is returned, and `Usage.calls` counts only the successful call.
  - `test_two_phase_tools_then_schema`: no request carries both `tools` and `response_format`. The final request has `tool_choice="none"` and the strict `response_format`.
  - `test_label_logprob_from_label_line`: the model emits `LABEL: correspondence/memo` and then the JSON. `exp(label_logprob)` equals the product of the scripted token probabilities on the label line only.
  - `test_vllm_disables_thinking`: vLLM provider requests carry `chat_template_kwargs.enable_thinking == False`.
  - `@pytest.mark.live test_live_tool_call_and_schema`: against the configured provider.
- [x] **Step 2: Run the tests and confirm they fail.**
- [x] **Step 3: Implement.**
  - Port the provider resolution and model maps from `L/src/llm/client.py`, and the backoff from `L/src/llm/retry.py`, minus gateway and free-swarm.
  - `tooling.run_tool_loop(client, req, tools, max_rounds=3)`.
  - Label logprob: find the label value span in the streamed or returned tokens and sum its logprobs.
- [x] **Step 4: Run the tests and confirm they pass.** Run `uv run pytest tests/llm -v -m "not live"`.
- [x] **Step 5: Commit.** `git commit -am "feat: OpenAI-compatible client, retry, tool loop with inline fallback"`

### Task 8: Tools (one definition, two surfaces)

**Files:**
- Create: `src/mailroom_reloaded/tools.py`, `tests/test_tools.py`

**Interfaces:**
- Consumes: `load_taxonomy`, `subclass_vocab`, `get_extraction_schema`.
- Produces:
  - `ToolDef(name, description, params_model: type[BaseModel], fn: Callable[..., str], eval_only: bool=False)`
  - `TOOLS: dict[str, ToolDef]` with `get_taxonomy`, `list_subclasses`, `get_extraction_schema`, `get_field_types`, `search_source`, `get_ground_truth`.
  - `openai_spec(td) -> dict`
  - `crewai_tool(td, *, context: ToolContext) -> crewai.tools.BaseTool`
  - `tools_for(role, context) -> list[ToolDef]`
  - `ToolContext(doc_text: str, doc_id: str, eval_mode: bool, ground_truth: Callable[[str], dict] | None)`

  `search_source` returns up to 3 snippets of 400 chars each around case-insensitive matches.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_role_tool_matrix`: sorter → `{get_taxonomy, list_subclasses}`; each specialist → `{get_extraction_schema, get_field_types}`; judge → `{get_extraction_schema, get_field_types, search_source, get_ground_truth}`; arbiter → `{get_taxonomy, get_extraction_schema, search_source}`; boss → `{get_taxonomy, list_subclasses, search_source}`.
  - `test_ground_truth_absent_outside_eval`: `tools_for("judge", ToolContext(eval_mode=False, ...))` has no `get_ground_truth`.
  - `test_crewai_tool_runs`: `crewai_tool(TOOLS["list_subclasses"], ctx).run(doc_type="merger_agreement")` contains `"all_cash"`.
  - `test_unknown_args_return_error_string`: the call does not raise.
- [x] **Step 2: Implement**, porting the handlers from `L/src/langchain_agents/toolkit.py`.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/test_tools.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: shared tool definitions for agents and standalone LLMs"`

### Task 9: Deterministic ingest (clerk, PDF, vision)

**Files:**
- Create: `src/mailroom_reloaded/ingest/{__init__,clerk,pdf,vision}.py`, `tests/ingest/test_ingest.py`, `tests/ingest/fixtures/{letter.txt,text.pdf,scanned.pdf,corrupt.pdf}`

**Interfaces:**
- Produces:
  - `ingest(path: Path) -> IngestResult(text: str, method: Literal["text","pdf_text","vision"], pages: int, stats: dict, clerk: dict, error: str | None)`
  - `apply_intake(text, filename) -> tuple[str, dict]` and `validate_intake(result, text)`, ported from `L/src/agents/intake.py` (deterministic parts only; drop the LLM intake class).
  - `transcribe_pages(pdf_path) -> str`, from `L/src/llm/vision.py` + `agents/pdf_transcriber.py`, using `call_structured(role="pdf_transcriber")`.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_txt_ingest`
  - `test_pdf_text_layer`: method `pdf_text`.
  - `test_scanned_pdf_uses_vision`: monkeypatch `transcribe_pages`; method `vision`.
  - `test_corrupt_pdf_sets_error`: `error` is set and nothing is raised.
  - `test_400k_chars_under_2s`: a synthetic 400,000-char text ingests in under 2 s. This covers Review Focus 1.
- [x] **Step 2: Implement.** Generate the fixtures with reportlab in a conftest helper; do not commit binaries if they can be generated.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/ingest -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: deterministic ingest with PDF and vision fallback"`

### Task 10: ModernBERT adapter and handoff policy

**Files:**
- Create: `src/mailroom_reloaded/ingest/bert.py`, `tests/ingest/test_bert.py`

**Interfaces:**
- Consumes: `BertCfg`.
- Produces:
  - `BertVerdict(available, reason: Literal["flag_off","no_package","no_model","error","ok"], doc_type, subclass, calibrated_confidence, margin, window_agreement, n_windows, route)`
  - `classify_primary(text: str) -> BertVerdict`: calls `mailroom_ml.inference.classify_document` and never raises.
  - `SortMode(Enum)` with values `FULL` and `SUBCLASS_ONLY`
  - `Handoff(mode: SortMode, locked_doc_type: str | None, prior: str, reason: str)`
  - `decide_handoff(v: BertVerdict, cfg: BertCfg) -> Handoff`, applying the rules in spec §5 in order.

**Steps:**

- [x] **Step 1: Write the failing tests.** One test per row of the spec §5 table, plus these:
  - `test_contract_defers` and `test_merger_defers`: mode `FULL`, and `prior` mentions the BERT class.
  - `test_multiwindow_defers`: `n_windows=3` with a correspondence fast path gives `FULL`.
  - `test_fast_path_correspondence_subclass_only`: `locked_doc_type == "correspondence"`.
  - `test_unavailable_full`
  - `test_package_missing_reason`: monkeypatch the import to fail; `reason == "no_package"`.
  - `test_long_doc_does_not_call_model_past_cap`: a 400k-char doc is accepted, the classifier is not called, and the result is `FULL` with `reason="too_long"` when the length exceeds `8192*4*max_trusted_windows` chars. This covers Review Focus 1.
- [x] **Step 2: Implement**, porting the fail-open structure from `L/src/agents/bert_intake.py`.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/ingest/test_bert.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: ModernBERT primary classifier with contract/merger deferral"`

### Task 11: Sorter (FULL / SUBCLASS_ONLY)

**Files:**
- Create: `src/mailroom_reloaded/agents/sorter.py`, `tests/agents/test_sorter.py`

**Interfaces:**
- Consumes: `call_structured`, `tools_for("sorter")`, `load_prompt("sorter_v14")`, `Handoff`, `subclass_vocab`.
- Produces:
  - `SortResult(doc_type, doc_subclass, confidence: float, raw_confidence: float, calibrated: bool, mode: SortMode, confidence_source: Literal["logprob","self_report"], doc_type_disagree: bool, disagree_reason: str | None, usage: Usage)`
  - `sort(text: str, handoff: Handoff, *, attempt: int = 0) -> SortResult`

  In `SUBCLASS_ONLY` mode the system prompt is `sorter_v14 + "\n\n" + sorter_subclass_scope.format(doc_type=..., subclasses=...)`, and the output schema enum restricts `doc_subclass` to that vocabulary. In `FULL` mode the BERT `prior` is appended to the user message.

**Steps:**

- [x] **Step 1: Write the failing tests** (FakeOpenAI).
  - `test_subclass_only_prompt_scoped`: the system message contains `sorter_v14` verbatim as a prefix and the allowed list. The JSON schema enum equals `subclass_vocab("correspondence")`.
  - `test_disagree_flag_propagates`
  - `test_confidence_from_logprobs`: `confidence_source == "logprob"`, and `raw_confidence` is kept beside the calibrated `confidence`.
  - `test_calibration_applied`: with a `models/calibration.json` holding temperature T for (provider, model, class), `confidence == sigmoid(logit(raw)/T)`. With no file, `calibrated is False`.
  - `test_self_report_fallback`
  - `test_sorter_uses_list_subclasses_tool`: a scripted tool call, and the result is valid.
  - `test_input_truncated_to_sorter_cap`: the cap comes from `taxonomy.agents.sorter`.
- [x] **Step 2: Implement.**
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/agents/test_sorter.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: sorter with BERT-scoped subclass mode"`

### Task 12: Specialists (frozen v1, structured, merger dagger)

**Files:**
- Create: `src/mailroom_reloaded/agents/specialists.py`, `tests/agents/test_specialists.py`

**Interfaces:**
- Consumes: `call_structured`, `response_format`, `assess_payload`, `load_prompt`, `RunConditions`, `tools_for(<specialist>)`.
- Produces:
  - `ExtractResult(doc_type, data: dict | None, schema_valid: bool, parse_error: str | None, confidence: float | None, error_kind: str | None, calls: int, usage: Usage)`
  - `extract(text: str, doc_type: str, doc_subclass: str | None, *, prompt_set="frozen_v1", tools: bool | None = None, attempt=0) -> ExtractResult`. `tools=None` means `taxonomy.agents.<specialist>.tools`, forced to False for `sand37`.
  - `extraction_confidence(schema_valid, coverage, mean_token_prob) -> float`, the spec §6 formula, using `taxonomy.yaml` `required_fields.<doc_type>`.
  - `prepare_input(text, doc_type, cond) -> list[str]`, which returns:
    - one capped string for the frozen classes;
    - head+tail 15k/15k for frozen merger;
    - 47k/6.5k windows for dagger merger.
  - `merge_merger_windows(results: list[dict]) -> dict`: union `maud_clauses` keyed by canonical MAUD question (`canonical_maud_class`); first non-null scalar wins.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_system_prompt_is_frozen_bytes`: the request's system message equals `load_prompt(...)` exactly for all 5 classes.
  - `test_response_format_sent`
  - `test_conditions_applied`: the request has `max_tokens=8192`, and temperature 0.1 for insurance or 0.7 for contract.
  - `test_frozen_merger_head_tail_30k`: a 381,761-char doc is sent as ≤ 30,000 chars of document text.
  - `test_dagger_windows`: a 381,761-char doc gives `ceil((381761-6500)/(47000-6500))` windows, each ≤ 54,000 chars. Sampling includes `top_p 0.8`, `top_k 20` and `presence_penalty 1.0`, and `max_tokens 6144`.
  - `test_dagger_resample_once_on_length`: length, then OK, gives `calls == 2` for that window.
  - `test_length_cap_error_kind`: frozen contract with a length cap gives `error_kind == "LengthFinishReasonError"` and `data is None`.
  - `test_malformed_json_one_repair`
  - `test_parity_mode_tools_off`: with `prompt_set="sand37"` or `tools=False`, the requests carry no `tools` key.
  - `test_extraction_confidence_formula`: with schema_valid True, coverage 0.5 and mean token probability 0.9, `confidence == 0.6*0.5 + 0.4*0.9`. With schema_valid False it is 0.
  - `test_no_ground_truth_in_messages` uses the GT leak helper `assert_no_gt(requests, gt_row)` from `tests/helpers.py` (created here, reused in Task 20).
- [x] **Step 2: Implement**, porting the class prompts' user-message template from `L/src/agents/<class>_specialist.py` and the dagger windowing from `S/src/mailroom_sandbox/job/specialist_posture.py`.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/agents/test_specialists.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: frozen-v1 specialists with merger dagger mode"`

### Task 13: Route gate (bands + learned logistic)

**Files:**
- Create: `src/mailroom_reloaded/agents/gate.py`, `src/mailroom_reloaded/eval/train_gate.py`, `tests/agents/test_gate.py`

**Interfaces:**
- Produces:
  - `GateFeatures(stage: Literal["classify","extract"], doc_type, confidence, attempts, bert_confidence, bert_margin, bert_window_agreement, schema_valid, field_coverage, length_capped, doc_type_disagree, resorted: bool)`
  - `GateDecision(action: Literal["proceed","retry","re_sort","verify","boss","human_review"], reason: str, source: Literal["band","model","rule"])`
  - `RouteGate` (Protocol) with `decide(f) -> GateDecision`
  - `BandGate(taxonomy)`
  - `LearnedGate(band: BandGate, coef_path: Path)`, which overrides only when `low <= confidence < high`
  - `load_gate() -> RouteGate`
  - `fit_calibration(rows, out: Path) -> dict`: per (provider, model, doc_type), temperature scaling by 1-D minimisation of NLL. Writes `models/calibration.json` and returns ECE before and after.
  - `ece(confidences, correct, bins=10) -> float`
  - `train_gate(rows: list[dict], out: Path) -> dict` (metrics). It fits sklearn `LogisticRegression` per stage on labels from `retry_expected` / `review_expected` and writes `{"stage": {"features": [...], "coef": [...], "intercept": float, "threshold": float}}`.

**Decision rules (BandGate)**

*Classify stage:*
1. `doc_type_disagree and not resorted` → `re_sort`.
2. `confidence >= high` → `proceed`.
3. Otherwise, if `attempts < retry_max` → `retry`.
4. Otherwise → `human_review`.

*Extract stage:*
1. `length_capped or not schema_valid`: `retry` while `attempts < retry_max`, then `human_review`.
2. `confidence >= judge_band_high` → `proceed`.
3. `low <= confidence < judge_band_high` → `verify`.
4. `confidence < low`: `retry` while `attempts < retry_max`, then `boss`.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - A parametrized table covering each rule above at exact boundaries: 0.70, 0.9499 and 0.95 for classify; 0.92 and 0.94 by class for extract.
  - `test_learned_gate_only_in_medium_band`: a model coefficient that always says `human_review` does not change a 0.99 or a 0.10 decision.
  - `test_deterministic`: identical features give an identical decision over 100 calls.
  - `test_train_gate_writes_json` uses 40 synthetic rows.
  - `test_fit_refuses_test_split`: `train_gate` and `fit_calibration` raise `ValueError` when any row has `split == "test"`.
  - `test_fit_calibration_reduces_ece`: on synthetic overconfident data, ECE after calibration is below ECE before.
- [x] **Step 2: Implement.**
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/agents/test_gate.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: deterministic route gate replacing reviewer nodes"`

### Task 14: CrewAI agents: judge, arbiter, boss

**Files:**
- Create: `src/mailroom_reloaded/agents/{judge,arbiter,boss}.py`, `tests/agents/test_crew_agents.py`

**Interfaces:**
- Consumes: `make_llm`, `crewai_tool`, `tools_for`, `load_prompt`.
- Produces:
  - `JudgeVerdict(label: Literal["complete","partial","incomplete"], score: float, field_findings: list[FieldFinding])`
  - `FieldFinding(field, verdict: Literal["correct","partial","wrong","missing","hallucinated","gt_suspect"], rationale)`
  - `JudgeGrade(doc_id, doc_type, fields: list[FieldFinding], classification: ClassificationFinding(verdict: Literal["correct","incorrect","gt_suspect"], rationale), overall: float, usage: Usage)`
  - `judge_verify(text, doc_type, data, ctx) -> JudgeVerdict`
  - `judge_grade(text, doc_type, data, ctx) -> JudgeGrade` (requires `ctx.eval_mode`; otherwise raises `ValueError`)
  - `ArbiterDecision(action: Literal["accept","accept_with_caveats","re_extract","escalate"], caveats: list[str])`
  - `arbitrate(text, doc_type, data, verdict, ctx) -> ArbiterDecision`
  - `BossDecision(action: Literal["reassign_class","accept","human_review"], doc_type: str | None, doc_subclass: str | None, reason)`
  - `escalate(text, state_summary, ctx) -> BossDecision`

  Each function builds `Agent(role, goal, backstory=<prompt>, llm=make_llm(role), tools=[...], max_iter=4, allow_delegation=False)` and a `Task(expected_output=..., output_pydantic=<Model>)`, then runs `Crew(agents=[a], tasks=[t]).kickoff()`.

**Steps:**

- [x] **Step 1: Write the failing tests** (FakeOpenAI as the agents' endpoint).
  - `test_judge_grade_calls_ground_truth_tool`: script a `get_ground_truth` tool call, then the final JSON. Assert the tool was invoked with the doc_id and that the output is a valid `JudgeGrade`.
  - `test_judge_verify_has_no_gt_tool`: the request `tools` list has no `get_ground_truth`.
  - `test_arbiter_returns_decision`
  - `test_boss_reassign`
  - `test_judge_uses_configured_model`: the judge LLM resolves `agents.judge.model`, not the specialist model, and the card flag `judge_same_model` is set when they match.
  - `test_judge_grade_gt_suspect_allowed`: `FieldFinding.verdict` accepts `gt_suspect`.
  - `test_judge_grades_classification`: `JudgeGrade.classification` has `verdict` and `rationale`.
  - `test_crewai_telemetry_disabled`: `os.environ["CREWAI_DISABLE_TELEMETRY"] == "true"` after `import mailroom_reloaded.agents`.
  - `@pytest.mark.live test_live_agent_tool_use`
- [x] **Step 2: Implement.** Set the env var in `mailroom_reloaded/__init__.py` before anything imports crewai.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/agents/test_crew_agents.py -v -m "not live"`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: CrewAI judge (live + GT grading), arbiter, boss"`

### Task 15: Deterministic report writer and archivist

**Files:**
- Create: `src/mailroom_reloaded/pipeline/{report,archivist}.py`, `tests/pipeline/test_report_archive.py`

**Interfaces:**
- Consumes: `Bins`, `save_manifest`, `audit_log.append`, `catalog.upsert`.
- Produces:
  - `compile_report(state: MailroomState) -> dict`, ported from `L/src/agents/reporter.py`. It covers classification, extraction, arbiter caveats, route trail, usage totals and cost.
  - `archive_document(bins, manifest, state) -> ArchiveResult(path, file_sha256, sidecar_path)`, ported from `L/src/agents/archivist.py`. It moves the file to `archive/<doc_type>/`, writes a `<name>.report.json` sidecar, and appends an audit entry `archived` with `file_sha256`.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_report_includes_caveats`
  - `test_report_no_llm`: patch `call_structured` to raise; the report still succeeds.
  - `test_archive_sha_matches_file`
  - `test_archive_audit_chain_verifies`
- [x] **Step 2: Implement.**
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/pipeline/test_report_archive.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: deterministic report writer and archivist"`

### Task 16: MailroomFlow

**Files:**
- Create: `src/mailroom_reloaded/pipeline/{state,guards,flow}.py`, `tests/pipeline/test_flow.py`

**Interfaces:**
- Consumes: Tasks 5–15.
- Produces:
  - `MailroomState(BaseModel)`. Fields:
    - identity and text: `doc_id`, `path`, `text`;
    - routing: `ingest`, `bert: BertVerdict | None`, `handoff`, `sort: SortResult | None`, `extract: ExtractResult | None`, `verdict`, `arbiter`, `boss`;
    - retry counters: `classify_attempts`, `extract_attempts`, `resorted`;
    - trail and run outputs: `route_trail: list[str]`, `report`, `status`, `eval_mode: bool`, `grade: JudgeGrade | None`, `usage_total: Usage`.
  - `NODE_ORDER: tuple[str, ...]`
  - `class MailroomFlow(Flow[MailroomState])` with methods:
    - `@start() ingest`
    - `@listen(ingest) bert_primary`
    - `@listen(bert_primary) sort`
    - `@router(sort) gate_classify` → `"extract"|"sort"|"re_sort"|"human_review"`
    - `@listen("extract") extract`
    - `@router(extract) gate_extract` → `"report"|"extract"|"verify"|"boss"|"human_review"`
    - `@listen("verify") verify` (judge + arbiter, then routes via an arbiter router)
    - `@listen("boss") boss`
    - `@listen("report") report_catalog_archive`
    - `@listen("human_review") park`
  - `run_document(path: Path, *, worker_id: str, resume_from: str | None = None, overrides: dict | None = None, eval_ctx: EvalContext | None = None) -> MailroomState`
  - `guarded(node_name, deadline_s, token_budget)`, a decorator. It writes the manifest, appends the audit entry, emits the OTel span name `mailroom.node.<name>`, and skips nodes already in `manifest.completed_nodes`.

**Steps:**

- [x] **Step 1: Write the failing tests.** Each uses the `mock` provider via FakeOpenAI and monkeypatched `classify_primary`, and asserts `route_trail`, the final bin and the LLM call count.
  - `test_fast_path_two_llm_calls`: correspondence fast path. Trail `ingest,bert_primary,sort,gate_classify,extract,gate_extract,report_catalog_archive`; exactly 2 LLM calls; file in `archive/correspondence/`.
  - `test_contract_deferred_full_sort`
  - `test_subclass_disagree_resorts_once`
  - `test_classify_low_conf_retries_then_parks`: the file ends in `review/`, status `parked`.
  - `test_extract_medium_band_verifies`: judge then arbiter `accept_with_caveats`; the report carries the caveats.
  - `test_extract_low_conf_boss_reassign`: boss reassigns the class and extraction reruns with the new class.
  - `test_resume_skips_completed_nodes`: crash injected after `sort`. The rerun makes no sorter call and the audit chain verifies with no duplicate `sort` entry.
  - `test_deadline_guard_fails_doc`
- [x] **Step 2: Implement.**
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/pipeline/test_flow.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: CrewAI MailroomFlow with gate routing and manifest resume"`

### Task 17: Watcher and human-review resume

**Files:**
- Create: `src/mailroom_reloaded/{watcher,review}.py`, `tests/test_watcher_review.py`

**Interfaces:**
- Produces:
  - `Watcher(bins, worker_id, concurrency: int).run_forever()` and `.drain_once() -> int`. It polls with watchdog, holds a `watcher.lock` flock, and resumes any manifest still in status `processing` at startup.
  - `resolve_review(doc_id, action: Literal["approve","correct","reject"], doc_type=None, doc_subclass=None, reviewer: str) -> MailroomState | None`

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_two_workers_one_file`: two `drain_once` calls in threads give one archived doc and one manifest.
  - `test_corrupt_file_goes_failed_and_watcher_continues`: a corrupt and a good file give 1 failed + 1 archived.
  - `test_startup_resumes_processing_manifest`
  - `test_review_correct_resumes_at_extract`: no sorter call; extraction runs with the corrected class; audit `review_resolved`.
  - `test_review_reject_moves_failed`
- [x] **Step 2: Implement**, porting from `L/src/pipeline/watcher.py` (loop and lock only) and `L/src/pipeline/review_resolve.py`.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/test_watcher_review.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: watcher with lock and resume; review resolve"`

### Task 18: Observability (traces + metrics)

**Files:**
- Create: `src/mailroom_reloaded/obs/{tracing,metrics}.py`, `tests/obs/test_obs.py`
- Modify: `src/mailroom_reloaded/__init__.py` (call `setup_tracing()` before importing crewai)

**Interfaces:**
- Produces:
  - `setup_tracing(service_name="mailroom", exporter: SpanExporter | None = None)`. It sets up OTLP HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4318`), installs `CrewAIInstrumentor` and `OpenAIInstrumentor`, adds resource attributes `service.instance.id`, `container.id` (read from `/proc/self/cgroup` when present) and `gpu.replica`, and installs a masking span processor when `trace_mask`.
  - `setup_metrics(reader: MetricReader | None = None)`
  - `M`: a namespace of the instruments named in spec §8 "Live OTel metrics": `documents`, `node_duration`, `llm_calls`, `gate_decisions`, `bert_route`, `schema_valid`, `length_capped`, `cost_usd`, `queue_depth`, `inflight`, `token_usage`, `operation_duration`.
- Wire into: `guarded` (node duration and documents), `call_structured` (llm_calls, token_usage, operation_duration, length_capped, cost), the gate (gate_decisions), bert (bert_route), the watcher (queue_depth, inflight).

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_flow_emits_node_spans`: an InMemorySpanExporter receives `mailroom.node.ingest` … spans under one trace per document.
  - `test_llm_span_has_genai_attrs`: an OpenInference LLM span carries the token counts.
  - `test_masking_redacts_content`: with `trace_mask`, input and output values are `"<masked>"`.
  - `test_metric_names_emitted`: an InMemoryMetricReader collects every name in `M` after one fast-path document.
- [x] **Step 2: Implement.**
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/obs -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: OTel tracing via OpenInference and pipeline metrics"`

### Task 19: FastAPI /v1 and the localhost runs UI

**Files:**
- Create: `src/mailroom_reloaded/api/app.py`, `src/mailroom_reloaded/api/ui/index.html`, `src/mailroom_reloaded/cli.py`, `tests/api/test_api.py`

**Interfaces:**
- Produces these endpoints:

  | Endpoint | Purpose |
  | --- | --- |
  | `GET /health` | health check |
  | `POST /v1/documents` (multipart) | write to inbox; returns `{doc_id}` |
  | `GET /v1/documents?status=&limit=` | list documents |
  | `GET /v1/documents/{id}` | manifest + report |
  | `GET /v1/audit/{id}` | entries + `verify_chain` |
  | `POST /v1/review/{id}/resolve` | resolve a parked document |
  | `GET /v1/runs` | eval runs from SQLite |
  | `GET /v1/runs/{run_id}/cards` | card JSONs |
  | `GET /ui` | static page |

  `/ui` (vanilla JS, no build step) shows documents, review queue, audit verify, eval runs and cards, and links to Phoenix `:6006` and Grafana `:3000`. Bearer auth applies when `api_token` is set. Startup refuses a non-loopback host without a token.
- CLI (typer or argparse):
  - `mailroom serve`
  - `mailroom watch`
  - `mailroom run <file>`
  - `mailroom eval ...` (Task 20)
  - `mailroom card ...` (Task 21)
  - `mailroom train-gate ...`

**Steps:**

- [x] **Step 1: Write the failing tests** (TestClient).
  - `test_upload_then_process`: `drain_once` gives `GET` status `archived`.
  - `test_audit_verify_ok`
  - `test_review_resolve_endpoint`
  - `test_ui_served`
  - `test_offbind_without_token_refuses`
  - `test_bearer_required_when_token_set`
- [x] **Step 2: Implement**, porting the relevant handlers from `L/src/api/main.py`.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/api -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: /v1 API, localhost runs UI, CLI"`

### Task 20: Dataset loader and eval runner

**Files:**
- Create: `src/mailroom_reloaded/eval/{dataset,runner}.py`, `tests/eval/test_dataset_runner.py`, `tests/eval/fixtures/mini_dataset/{default.jsonl,ground_truth.jsonl}`
- Modify: `tests/helpers.py` (`assert_no_gt`)

**Interfaces:**
- Produces:
  - `BlindDoc(filename, doc_text, content_sha256)`. Frozen, with no label fields.
  - `GroundTruth(filename, expected, expected_subclass, fields: dict, cuad_clause_labels, maud_clause_labels, retry_expected, review_expected, expected_stage)`
  - `load_split(revision="ed7576b6", split="test", *, local_dir: Path | None = None) -> tuple[list[BlindDoc], dict[str, GroundTruth]]`. It verifies `content_sha256` and raises `DatasetIntegrityError` on a mismatch.
  - `sample(docs, gts, *, per_class: int, seed=42, classes=None) -> list[BlindDoc]`. It is nested: the n=20 draw is a prefix of the n=50 draw.
  - `EvalContext(run_id, ground_truth: dict[str, GroundTruth])`
  - `run_eval(cfg: EvalConfig) -> run_id`
  - `EvalConfig(revision, per_class, seed, classes, concurrency, posture_label, gpu, gpus, prompt_set, merger_mode, mode: Literal["pipeline","specialist_cell"])`

  Runs use `MailroomFlow.kickoff_async` under `asyncio.Semaphore(concurrency)`. `EvalConfig` also has `judge_sample_rate: float = 1.0` (seeded). `pipeline` mode runs the full flow. `specialist_cell` mode feeds GT-class docs straight to `extract`, as SAND-37 does. Per-document records go to the SQLite table `eval_docs`: `run_id`, `filename`, `stage` outputs, `latency`, `tokens`, `calls`, `error_kind`, `schema_valid`, `gate features` and `judge grade`. A `grade` step runs after archive only when an `eval_ctx` is present.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_blind_doc_has_no_label_attrs`
  - `test_sha_mismatch_raises`
  - `test_nested_sampling`
  - `test_concurrency_bounded`: with concurrency 4, at most 4 FakeOpenAI requests are in flight at once.
  - `test_judge_sample_rate`: rate 0.5 with seed 42 grades a deterministic half.
  - `test_eval_run_records_rows` uses the mini dataset (10 docs, 2 per class) with the mock provider.
  - `test_no_gt_leak_in_agent_requests`: `assert_no_gt` over every FakeOpenAI request except the judge's `get_ground_truth` tool result.
- [x] **Step 2: Implement.** Use `datasets.load_dataset(REPO, "default"|"ground_truth", revision=...)` behind the `eval` extra. `local_dir` reads JSONL for tests and offline runs.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/eval/test_dataset_runner.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: blind/GT dataset loader and eval runner"`

### Task 21: KPIs, SAND-37 cards, vLLM telemetry, cost

**Files:**
- Create: `src/mailroom_reloaded/eval/{metrics,cards,vllm_telemetry,cost}.py`, `tests/eval/test_metrics_cards.py`, `tests/eval/fixtures/vllm_metrics_{before,after}.txt`

**Interfaces:**
- Consumes: the `eval_docs` rows and the scoring subset.
- Produces:
  - `sorter_kpis(rows) -> {"exact_match","primary_accuracy","subclass_accuracy","subclass_accuracy_given_primary","confusion","bert":{"fast_path_rate","defer_rate","fast_path_primary_accuracy"}}`
  - `specialist_kpis(rows, doc_type) -> {"precision","recall","f1","f2","micro_f1","mean_doc_f1","suite_mean","sd","min","max","schema_valid_rate","parse_errors","error_kinds","clause":{...},"maud":{"accuracy","coverage","precision_answered"},"judge_scorer_agreement"}`. The `clause` block holds `precision`, `recall`, `f1`, `docs_labeled`, `value_checked` and `value_correct`.
  - `scrape(url) -> dict[str, float]` and `telemetry_delta(before, after) -> ReplicaTelemetry(requests, length_finishes, preemptions, prefix_cache_hit_rate, ttft_mean_seconds, kv_cache_usage_perc)`
  - `cell_cost(wall_s, gpus, usd_per_hour, ok, total, tokens) -> {"busy_gpu_usd","usd_per_document","usd_per_ok_document","usd_per_million_tokens"}`
  - `token_split(rows) -> {"instruction_per_call","chars_per_token","method"}`, a least-squares fit of `prompt = I*calls + chars/r`.
  - `build_card(run_id, doc_type) -> dict`, schema `mailroom.card/v1`, with the blocks listed in spec §8.
  - `render_card_md(card) -> str`
  - `build_master(run_ids) -> (dict, str)`, with the tables of `S/reports/SAND-37/SAND-37-MASTER-SCORE-COST-CARD.md`: posture table, serving efficiency, quality and cost by specialist, cost.

  TP/FP/FN follow the table in spec §8, with `match_threshold` 0.5. `sorter_kpis` also returns `by_path` (`SUBCLASS_ONLY`/`FULL`), `resort_rate` and `ece`. `gate_kpis(rows)` returns the decision mix and agreement with `retry_expected`/`review_expected`/`expected_stage`. `cell_cost` takes `pricing: Literal["gpu_hour","per_token"]`; per-token pricing uses `cost_models` in the taxonomy. Judge tokens and cost go in a separate `judge` block. Micro F1 pools TP/FP/FN across documents. F2 is `fbeta(P, R, beta=2)` on pooled counts. `judge_scorer_agreement` is the share of fields where the judge verdict ∈ {correct, partial} iff the field score ≥ 0.5.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_sorter_kpis_exact_vs_primary`: 4 rows = (right/right, right/wrong-sub, wrong/right-sub-name, wrong/wrong). Expected `exact_match 0.25`, `primary_accuracy 0.5`, `subclass_accuracy 0.5`, `subclass_accuracy_given_primary 0.5`.
  - `test_micro_vs_mean_f1`: doc A has TP=9, FN=1; doc B has TP=0, FP=1. Expected micro F1 = 2·9/(2·9+1+1) = 0.9; mean doc F1 = (0.947+0)/2.
  - `test_f2_weights_recall`
  - `test_wrong_value_counts_fp_and_fn`: a ground-truth value "ACME" predicted as "Globex" gives tp=0, fp=1, fn=1.
  - `test_sorter_kpis_by_path`
  - `test_per_token_cost`: 1M prompt tokens + 1M completion tokens at $0.2/$0.6 cost $0.80.
  - `test_telemetry_delta_from_fixtures`: the fixture texts are real Prometheus exposition. Prefix hit rate = Δhits/Δqueries, TTFT mean = Δsum/Δcount.
  - `test_cost_matches_sand37_cell`: wall 37.8 s, 1 GPU, $0.80 gives busy $0.0084. With ok 20/20, $/ok doc 0.00042.
  - `test_token_split_recovers_known_I`: synthetic rows with I=2702, r=4.46 recover I ± 1%.
  - `test_card_has_all_sand37_blocks`: keys ⊇ `{conditions,cost,documents,engine_telemetry,latency,quality,throughput,time,tokens,concurrency}`.
  - `test_master_md_has_tables`
- [x] **Step 2: Implement**, porting the card math from `S/src/mailroom_sandbox/job/{metrics,grid_cards,grid_master}.py` (formulas only, not the CLI or plotting). The vLLM metric names are listed in spec §8.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/eval/test_metrics_cards.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: sorter/specialist KPIs and SAND-37-parity cards"`

### Task 22: Docker topology and observability stack

**Files:**
- Create:
  - `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/otel-collector.yaml`, `deploy/prometheus.yml`
  - `deploy/grafana/provisioning/{datasources,dashboards}/*.yaml`, `deploy/grafana/dashboards/{pipeline,serving-gpu,quality}.json`
  - `deploy/llamafile/{Dockerfile,run.sh}`
  - `tests/deploy/test_compose.py`, `scripts/smoke.sh`
- Modify: `README.md` (quick start)

**Interfaces:**
- Services, ports and profiles as listed in spec §9.
- Collector pipelines:
  - traces: otlp → batch → `otlphttp/phoenix` (`http://phoenix:6006/v1/traces`)
  - metrics: otlp + `prometheus` receiver (scrape `vllm:8001/metrics`, `dcgm-exporter:9400/metrics` and each URL in `VLLM_METRICS_URLS`) + `docker_stats` → batch → `prometheus` exporter `:8889`
- Prometheus scrapes `otel-collector:8889`.
- Volumes: `mailroom_data`, `hf_cache` (shared by `vllm` and `app`), `llamafile_models`, `phoenix_data`, `prometheus_data`, `grafana_data`.
- The Phoenix project comes from resource attribute `openinference.project.name`: `mailroom-live` for live runs, `eval-<run_id>` for eval runs (set in `setup_tracing(project=...)`, Task 18). Grafana dashboards take a `run_id` variable.
- Dashboards:
  - **Pipeline:** documents by status, node p95, gate decisions, BERT route mix, queue depth, cost.
  - **Serving & GPU:** TTFT, TPOT, running/waiting requests, KV usage, preemptions, prefix-cache hit rate, GPU util/memory/power per GPU, tokens/s per replica.
  - **Quality:** latest eval-run KPIs, exported by `mailroom eval` as OTel gauges `mailroom.eval.*` labelled `run_id`, `doc_type`.
- `vllm` service:
  - image `vllm/vllm-openai:v0.29.0`
  - `--model ${VLLM_MODEL:-Qwen/Qwen3-8B-AWQ} --tensor-parallel-size ${VLLM_TP:-1} --data-parallel-size ${VLLM_DP:-1} --enable-prefix-caching --kv-cache-dtype fp8 --max-model-len ${VLLM_MAX_LEN:-32768}`
  - `deploy.resources.reservations.devices` for NVIDIA.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_compose_config_valid`, parametrized over `[], ["local-llm"], ["gpu"], ["split-watcher"], ["local-llm","gpu"]`. Run `docker compose -f deploy/docker-compose.yml --profile ... config -q`; exit 0. Skip if `docker` is absent.
  - `test_collector_config_parses`: YAML loads, and the pipelines reference only defined receivers and exporters.
  - `test_dashboards_valid_json_with_datasource_uid`
- [x] **Step 2: Implement.** Base the Dockerfile on `L/Dockerfile`: builder, model stage (`ML_BUILD_NONE`), runtime as uid 10001 with a HEALTHCHECK.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/deploy -v`. Expected: PASS.
- [ ] **Step 4: Run the smoke test.** **NOT DONE (blocked):** the full compose smoke (`scripts/smoke.sh`, Phoenix/Grafana health) was never run end to end; only `docker compose ... config -q` validation and `tests/deploy` pass. Needs a host with Docker, model weights and a provider.
  1. `docker compose -f deploy/docker-compose.yml --profile local-llm up -d --build`
  2. `scripts/smoke.sh` drops `tests/ingest/fixtures/letter.txt` into the inbox and polls `/v1/documents` until it is `archived`.
  3. Check that `curl -s localhost:6006` returns 200 and that Grafana `localhost:3000/api/health` is OK.
  4. Expected: the smoke script prints `SMOKE OK`.
- [x] **Step 5: Commit.** `git commit -am "feat: docker topology with OTel collector, Phoenix, Prometheus, Grafana, GPU profile"`

### Task 23: Modal vLLM deploy

**Files:**
- Create: `deploy/modal_vllm.py`, `deploy/README.md`, `tests/deploy/test_modal_config.py`

**Interfaces:**
- Produces a Modal app `mailroom-vllm` served through a `@modal.web_server` OpenAI-compatible vLLM, trimmed from `L/deploy/modal_vllm.py`:
  - Env: `MODAL_GPU` (default `L4`), `MODAL_GPU_COUNT` (default 1, maps to TP), `MODAL_MAX_CONTAINERS` (replicas), `VLLM_MODEL`, `VLLM_MAX_LEN`, `VLLM_API_KEY` secret.
  - `/metrics` is exposed through the same web server.
  - `scaledown_window` is configurable, and teardown is `modal app stop mailroom-vllm`.
- `deploy_config() -> dict` is pure, so it can be tested without Modal credentials.

**Steps:**

- [x] **Step 1: Write the failing tests.**
  - `test_deploy_config_defaults`: gpu `"L4"`, count 1, model `Qwen/Qwen3-8B-AWQ`, flags include `--enable-prefix-caching` and `--kv-cache-dtype fp8`.
  - `test_gpu_count_sets_tp`: `MODAL_GPU_COUNT=2` gives `--tensor-parallel-size 2`.
  - `test_module_imports_without_modal`: `importorskip` semantics; the config function works without the `modal` import.
- [x] **Step 2: Implement.** In the README, cover deploy, pointing the app (`DEFAULT_PROVIDER=vllm`, `VLLM_BASE_URL`, `VLLM_METRICS_URLS`), running a SAND-37-style posture (`mailroom eval --mode specialist_cell --per-class 50 --gpus 2 --concurrency 32 --revision ed7576b6 --prompt-set sand37`), and teardown with a spend check.
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/deploy/test_modal_config.py -v`. Expected: PASS.
- [x] **Step 4: Commit.** `git commit -am "feat: Modal vLLM deploy with metrics and posture runbook"`

### Task 24: Behavioural conformance suite

**Files:**
- Create: `src/mailroom_reloaded/eval/conformance.py`, `tests/eval/test_conformance.py`
- Modify: `src/mailroom_reloaded/cli.py` (`mailroom conformance --provider X`)

**Interfaces:**
- Consumes: Tasks 11, 12, 14, 20.
- Produces:
  - `Invariant(role, name, check: Callable[[RoleRun], bool])`
  - `INVARIANTS`: the spec §11 conformance table.
  - `run_conformance(provider, *, per_class=2, revision="ed7576b6") -> ConformanceCard(provider, model, roles: dict[str, RoleStats(tool_call_success_rate, invariant_pass_rate, failures: list[str])])`. Fixtures come from the train split. Results are written as JSON and Markdown under `runs/conformance/`.

**Steps:**

- [x] **Step 1: Write the failing tests** (FakeOpenAI scripted per role).
  - `test_sorter_subclass_only_doc_type_change_without_flag_fails_invariant`
  - `test_specialist_extra_key_fails_invariant`
  - `test_judge_live_mode_gt_tool_call_fails_invariant`
  - `test_card_rates`: 3 of 4 tool calls succeed, so `tool_call_success_rate == 0.75`.
- [x] **Step 2: Implement.**
- [x] **Step 3: Run the tests.** Run `uv run pytest tests/eval/test_conformance.py -v`. Expected: PASS.
- [ ] **Step 4: Run live.** Run `uv run mailroom conformance --provider llamafile` (and later `vllm`). Expected: a card is written; record the pass rates in the PR description. **NOT DONE (blocked):** no live provider (llamafile/vllm) is available in this environment, so no live conformance card or pass rates exist. Card generation is proven only against FakeOpenAI.
- [x] **Step 5: Commit.** `git commit -am "feat: behavioural conformance suite for all LLM roles"`

---

## Self-review notes

- **Spec coverage:**

  | Spec section | Task(s) |
  | --- | --- |
  | §2 stack | 1, 7, 14, 18 |
  | §3 layout | all |
  | §4 pipeline / deterministic nodes / durability / review | 9, 15, 16, 17 |
  | §5 BERT | 10, 11 |
  | §6 gate | 13 |
  | §7 prompts / tools / judge | 3, 8, 12, 14 |
  | §8 metrics / cards / telemetry | 18, 20, 21 |
  | §9 Docker / Modal | 22, 23 |
  | §10 error handling | 7, 9, 12, 16, 17 |
  | §11 testing | per task, plus 24 (conformance) |

- **Type names used across tasks:** `Handoff`/`SortMode` (10 → 11, 16), `SortResult`/`ExtractResult` (11, 12 → 13, 16), `GateFeatures`/`GateDecision` (13 → 16), `ToolContext` (8 → 14, 16, 20), `EvalContext` (20 → 16), `Usage`/`LLMResult` (7 → all callers), `MailroomState` (16 → 15, 17, 19).
- **Open item:** the "Jev" model named in the request was not found in any repo or doc. `RouteGate` is the slot for it; Task 13 ships the deterministic default.


## Status (2026-10-08)

Audited against the code on branch `feat/jev-tui-hardening`. Checkboxes above are ticked only where the named files exist and the covering tests pass. 22 of 24 tasks are fully done, Task 22 is partial and Task 24 is partial. Both gaps are environment-blocked (no Docker stack run, no live provider), not skipped work.

| Task | Status | Note |
| --- | --- | --- |
| 1 Scaffold, settings, taxonomy, fence | done | |
| 2 Vendored scoring subset | done | parity test skipped without the `parity` extra, as planned |
| 3 Prompts with sha256 lock | done | prompts live under `src/mailroom_reloaded/prompts/`, not top-level `prompts/` |
| 4 Extraction schemas | done | |
| 5 Bins, manifests, claim | done | |
| 6 Catalog and audit chain | done | |
| 7 LLM client and tool loop | done | |
| 8 Tools | done | |
| 9 Ingest | done | PDF fixtures are generated in a conftest helper, not committed |
| 10 ModernBERT adapter | done | |
| 11 Sorter | done | |
| 12 Specialists | done | |
| 13 Route gate | done | extended later by the opt-in Jev gate |
| 14 CrewAI agents | done | live-marked tests are deselected by default |
| 15 Report and archivist | done | |
| 16 MailroomFlow | done | resume/retry crash windows hardened in PRs #11 and #12 |
| 17 Watcher and review | done | hardened in PRs #9 and #11 |
| 18 Observability | done | |
| 19 API and runs UI | done | `/tui` and `/v1/jev` added on top |
| 20 Dataset and eval runner | done | |
| 21 KPIs, cards, telemetry, cost | done | |
| 22 Docker topology | partial | files, config and `tests/deploy` pass; Step 4 smoke run NOT done |
| 23 Modal vLLM deploy | done (code and tests) | no real Modal deploy or spend check was performed |
| 24 Conformance suite | partial | Steps 1-3 and 5 done; Step 4 live run blocked (no provider) |

### Evidence

- `uv run pytest -q`: 673 passed, 1 skipped, 2 deselected (live-marked).
- `node --test tests/tui/js/*.test.mjs`: 117 tests, 117 pass, 0 fail.
- `uv run ruff check .`: all checks passed.
- Every file named in the tasks' Files lists exists (checked by script), except the generated PDF fixtures noted above.

### Deviations from the plan

- Prompt files are packaged inside `src/mailroom_reloaded/prompts/` so `importlib.resources` works from an installed wheel.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5`, not Opus 5.5 as the constraint says, because the session model changed.
- Added beyond the plan: Gmail intake route (optional extra), dedicated dev server (`scripts/dev.sh`, `docker-compose.dev.yml`, `deploy/mock_openai.py`, `deploy/mock_jev.py`), the operator docs set, the opt-in Jev probabilistic gate with calibration, gate audit entries and `GET /v1/jev`, and the `/tui` browser terminal (see the TUI plan).
- Review hardening landed after the plan: PR #5 audit findings 1-12 (PRs #9, #11, #12), parked documents are now catalogued, `.env` support for `JEV_*`, OTLP protocol/endpoint settings honoured, no vacuous conformance pass rates.
- The pytest `live` marker is registered once with `--strict-markers` and `addopts = "-m 'not live'"`.

### Incomplete or blocked

1. Task 24 Step 4: no live `mailroom conformance --provider ...` run, so no pass rates have been recorded.
2. Task 22 Step 4: compose smoke (`SMOKE OK`, Phoenix and Grafana health) never run.
3. Task 23: Modal deploy and teardown spend check never run against a real account.
4. Jev extract-stage gating is unreachable locally: dev correspondence extraction confidence never lands in the extract medium band, so only the classify gate consults Jev (see `docs/JEV.md`).
