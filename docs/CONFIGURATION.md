# Configuration

Two contracts configure `mailroom-reloaded`: **environment variables** (loaded by
`pydantic-settings` into `Settings`, `settings.py:113-139`) and the packaged
**`config/taxonomy.yaml`** document contract (`settings.py:95-110`). This page
lists every operator-facing env var and taxonomy block with its default, its
effect, and the source that reads it.

`.env` is read automatically (copy `.env.example`); the env prefix is
`MAILROOM_`, with several aliases accepted (see the table). After changing the
taxonomy or a model map you must restart the process: `load_taxonomy` and
`get_settings` are `lru_cache`d (`settings.py:107-110`, `settings.py:138-140`).

## Environment variables

### Core

| Variable | Alias / read | Default | Effect |
| --- | --- | --- | --- |
| `MAILROOM_BASE_DIR` | `settings.py:118` | `./data` | Root of all bins, manifests, SQLite, models and runs. |
| `DEFAULT_PROVIDER` | alias `MAILROOM_PROVIDER` (`settings.py:119-121`) | `mock` | Global provider; **overrides** the per-agent `provider` in taxonomy (`llm/client.py:85-113`). One of `mock`, `openrouter`, `vllm`, `llamafile`. |
| `MAILROOM_API_TOKEN` | `settings.py:133` | unset | Bearer token required on every `/v1` route; off-loopback bind refuses to start without it (`api/app.py:87-117`). |
| `MAILROOM_API_HOST` | `cli.py:37-39` | `127.0.0.1` | Default `mailroom serve --host`. |
| `MAILROOM_API_PORT` | `cli.py:42-47` | `8000` | Default `mailroom serve --port`; `PORT` wins when set. |
| `MAILROOM_MAX_UPLOAD_BYTES` | `api/app.py:60-62` | `52428800` (50 MB) | Upload size cap; read at import. |
| `MAILROOM_EMBED_WATCHER` | `api/app.py:339-346` | off in dev, `1` in compose | `1/true/yes/on` runs the watcher inside the API process. |
| `MAILROOM_TRACE_MASK` | `settings.py:134` | `0` (false) | When true, prompt/completion span attributes are replaced with `<masked>` (`obs/tracing.py:67-99`, `obs/tracing.py:197-209`). |
| `MAILROOM_TRACE_STORE_PATH` | `settings.py` | `<base_dir>/traces.db` | SQLite file of the local span store (`storage/span_store.py`): allow-listed spans for replay, never prompts, completions or document text (`llm.*messages`, LLM `input/output.value`, `exception.message` are dropped whether or not `MAILROOM_TRACE_MASK` is set). Skipped under pytest unless this variable is set. |
| `MAILROOM_GPU_USD_PER_HOUR` | `settings.py:135` (`gpu_usd_per_hour`) | `0.80` | Default GPU-hour price for cost cards; `mailroom eval --gpu-usd-per-hour` overrides. |

### Providers

| Variable | Read | Default | Effect |
| --- | --- | --- | --- |
| `MOCK_BASE_URL` | `llm/client.py:108-112` | unset | Required for the `mock` provider; points at an OpenAI-compatible fake (tests use `tests/fakes/openai_server.py`). |
| `OPENROUTER_API_KEY` | alias `MAILROOM_OPENROUTER_API_KEY` (`settings.py:125-128`) | unset | Required for `openrouter`; the mock placeholder `mock-key` is rejected (`llm/client.py:102-107`). |
| `OPENROUTER_BASE_URL` | `llm/client.py:106` | `https://openrouter.ai/api/v1` | Override the OpenRouter endpoint. |
| `VLLM_BASE_URL` | alias `MAILROOM_VLLM_BASE_URL` (`settings.py:122-124`) | `http://localhost:8000/v1` (`llm/client.py:48`) | vLLM endpoint; model ids are rewritten through `vllm_model_map`. |
| `VLLM_API_KEY` | `llm/client.py:96` | `not-needed` | Bearer for a guarded vLLM `/v1`. |
| `LLAMAFILE_BASE_URL` | alias `MAILROOM_LLAMAFILE_BASE_URL` (`settings.py:129-132`) | `http://localhost:8080/v1` (`llm/client.py:48`) | Llamafile endpoint; ids remapped through `llamafile_model_map`. |

### Jev (TypeSafe System One)

Opt-in route-gate model (`settings.py`; `JevConfig`, `jev_config`, `docs/JEV.md`).
Each field resolves `MAILROOM_JEV_<FIELD>` → `JEV_<FIELD>` → the taxonomy `jev:`
block → a default (`_jev_env`/`_jev_field`/`_jev_scalar`). `jev_config()` is
**not** cached, so env changes apply immediately.

| Variable (or `JEV_` alias) | Read | Default | Effect |
| --- | --- | --- | --- |
| `MAILROOM_JEV_PROVIDER` | `jev_config` | `off` | `off` \| `openrouter` \| `typesafe` \| `local`; unknown values collapse to `off`. `off` leaves the band/learned gate unchanged. |
| `MAILROOM_JEV_MODEL` | `jev_config` | per provider | Blank picks the provider default: `typesafe/jev-1.13` / `jev-latest` / `jevk5` (`_JEV_PROVIDER_DEFAULTS`). |
| `MAILROOM_JEV_BASE_URL` | `jev_config` | per provider | Blank picks the provider endpoint (`_JEV_PROVIDER_DEFAULTS`). |
| `MAILROOM_JEV_TEMPERATURE` | `jev_config` | `1.0` | Local-usage/default knob reserved for the offline JevK5 transport (which reads letter logits at ~1.22). It is **not** included in hosted OpenRouter/TypeSafe requests (the request body is `{"model", "state", "questions"}`; `JevClient.ask`). |
| `MAILROOM_JEV_ACCEPT_THRESHOLD` | `jev_config` | `0.8` | Minimum Jev confidence for the chosen action to be trusted; below it the gate maps to `verify` (down to the verify threshold) or `human_review`, and a `noul` escalation also maps to `human_review` (`JevGate.decide`). |
| `MAILROOM_JEV_TIMEOUT_S` | `jev_config` | `10.0` | Per-request HTTP timeout. |
| `MAILROOM_JEV_MAX_RETRIES` | `jev_config` | `2` | Retries on `429`/`5xx` with backoff; other non-2xx raise immediately (`JevClient._post_with_retry`). |
| `MAILROOM_JEV_API_KEY` / `JEV_API_KEY` | `_jev_api_key` | unset | API key; explicit override tried first. Then the provider-preferred hosted key (`OPENROUTER_API_KEY` for `openrouter`, `TYPESAFE_API_KEY` for `typesafe`), then the other, then `settings.openrouter_api_key`. The `local` provider may run keyless. |

### Observability

| Variable | Read | Default | Effect |
| --- | --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `obs/tracing.py:151-153`, `obs/metrics.py:94-98` | `http://localhost:4318` | OTLP HTTP collector; `/v1/traces` and `/v1/metrics` are appended. Under pytest with no endpoint, exporters are skipped. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | compose (`docker-compose.yml:18`) | `http/protobuf` | Collector transport. |
| `OTEL_SERVICE_NAME` | compose (`docker-compose.yml:19`) | `mailroom` | `service.name` resource attribute. |
| `MAILROOM_INSTANCE_ID` | `obs/tracing.py:113-115` | `hostname:pid` | `service.instance.id`. |
| `MAILROOM_GPU_REPLICA` (or `GPU_REPLICA`) | `obs/tracing.py:118-124` | `0` | `gpu.replica` resource attribute. |
| `MAILROOM_PHOENIX_PROJECT` | `obs/tracing.py:133-135` | `mailroom-live` | `openinference.project.name` (Phoenix project). |

### Docker / build (compose and Modal)

These are consumed by `deploy/docker-compose.yml`, `deploy/docker-compose.dev.yml`
and `deploy/modal_vllm.py`, not by the Python runtime.

| Variable | Default | Effect |
| --- | --- | --- |
| `MAILROOM_API_TOKEN` | required in prod compose | The app binds `0.0.0.0`; compose refuses to start without it (`docker-compose.yml:15`). |
| `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` | `admin` / required in prod | Grafana admin login (`docker-compose.yml:130-131`). |
| `VLLM_METRICS_URLS` | empty | Comma-separated `host:port` extra vLLM `/metrics` scrapes (`docker-compose.yml:83-102`). |
| `VLLM_METRICS_SCHEME` | `http` | Scheme for the remote scrape (`otel-collector.yaml:29`). |
| `VLLM_MODEL` | `Qwen/Qwen3-8B-AWQ` | Served model id (`docker-compose.yml:163`). |
| `VLLM_TP` / `VLLM_DP` | `1` / `1` | Tensor / data parallel size (`docker-compose.yml:165-166`). |
| `VLLM_MAX_LEN` | `32768` | Context window (`docker-compose.yml:169`). |
| `HF_TOKEN` | empty | Hugging Face token for vLLM weight download (`docker-compose.yml:172`). |
| `ML_BUILD_NONE` | `0` | `1` gives a lean image without the ModernBERT bundle (`docker-compose.yml:26`). |
| `UV_EXTRAS` | `--extra bert` | Extras installed in the app image (`docker-compose.yml:27`). |
| `LLAMAFILE_MODEL` / `LLAMAFILE_ALIAS` / `LLAMAFILE_CTX` / `LLAMAFILE_GPU` | `/models/model.gguf` / `qwen3:7b` / `16384` / `disable` | Llamafile sidecar (`docker-compose.yml:149-152`). |
| `MODAL_GPU`, `MODAL_GPU_COUNT`, `MODAL_MAX_CONTAINERS`, `MODAL_SCALEDOWN_WINDOW` | `L4`, `1`, `1`, `300` | Modal vLLM shape (`deploy/README.md:9-14`). |

### Archive ledger anchor

Optional off-host anchor of the archive-ledger head (`storage/anchor.py`). Only
`(seq, entry_hash)` of the head is pushed. See [OPERATIONS.md](OPERATIONS.md#ledger-anchor)
for commands, exit codes and the threat model.

| Variable | Default | Effect |
| --- | --- | --- |
| `MAILROOM_ANCHOR` | `none` | `none`, `export`, `postgres` or `supabase`. Blank means `none`; the value is lower-cased. `export` pushes nothing (use `mailroom audit export-head` and pin the output off-host). An unknown value is treated as not configured: pushes are skipped, `audit verify --external` and `audit anchor` exit 4, and startup does not fail. |
| `MAILROOM_ANCHOR_URL` | unset | Supabase project URL or Postgres DSN. Required for `supabase` and `postgres`. Use HTTPS: a non-HTTPS Supabase URL is refused unless the host is loopback (`localhost`, `127.0.0.1`, `::1`). A non-loopback Postgres DSN gets `sslmode=require` unless the DSN sets its own `sslmode`. The URL is hidden from `repr` and logs because a DSN may carry a password. |
| `MAILROOM_ANCHOR_KEY` | unset | Writer credential. Required for `supabase`. For `postgres` no key is needed; if set it is the role password. |
| `MAILROOM_ANCHOR_KEY_FILE` | unset | File holding the key (Docker/Kubernetes secrets style). `MAILROOM_ANCHOR_KEY` wins when both are set. The file is capped at 8192 bytes, must be a regular file (symlinks are followed) holding non-empty UTF-8 text, and a warning is printed by `audit verify --external` if it is world-readable. |

The Supabase key must be the key of the dedicated INSERT-only role created by
`deploy/anchor/mailroom_anchor.sql`, never the `service_role` key (which bypasses
row-level security and can rewrite the anchor table). The key is redacted from logged errors.

The `postgres` backend needs the optional extra and outbound TCP to the database:
`pip install mailroom-reloaded[anchor]` (or `uv sync --extra anchor`). The default install
does not include `psycopg`. The `supabase` backend uses `httpx` and needs no extra.

### Gmail intake

`MAILROOM_GMAIL_CREDENTIALS`, `MAILROOM_GMAIL_TOKEN`, `MAILROOM_GMAIL_QUERY`,
`MAILROOM_GMAIL_EXTENSIONS`, `MAILROOM_GMAIL_MAX_ATTACHMENT_BYTES`,
`MAILROOM_GMAIL_STATE` are read in `intake/gmail.py:259-276` and fully documented
in [gmail-intake.md](gmail-intake.md).

## `taxonomy.yaml` blocks

`load_taxonomy` parses the packaged file and builds `Taxonomy`
(`settings.py:95-110`). Path: `src/mailroom_reloaded/config/taxonomy.yaml`.

### `agents` — models and providers {#agents}

Per-role `provider`, `model`, `tier`, `temperature`, `max_tokens`,
`max_input_chars`, `reasoning_effort` (`settings.py:62-70`, rows at
`taxonomy.yaml:419-553`). Roles: `sorter`, `intake`, `arbiter`, the five
specialists, `reporter` (procedural, unused by `get_llm`), `boss`,
`pdf_transcriber`, `image_extractor`, `judge`. Effect: the standalone calls and
CrewAI agents read their model/temperature/caps from here unless the global
`DEFAULT_PROVIDER` overrides the provider (`llm/client.py:80-113`). There is **no
top-level `providers:` block**; provider choice is this per-role key plus the
global env var. `reasoning_effort` is sent as OpenRouter `reasoning.effort`
(`llm/client.py:194-195`).

### `confidence` — band thresholds {#confidence}

`high`, `low`, `retry_max`, `judge_band_high` (`taxonomy.yaml:100-135`), read by
`BandGate` via `Taxonomy.confidence_for` (`settings.py:81-86`,
`agents/gate.py:71-100`). Per-class `by_class` overrides win once `doc_type` is
known (`settings.py:83-86`). Effect:

- classify: `≥ high` → proceed; `< high` → retry until `retry_max`; then park.
- extract: `≥ judge_band_high` → proceed; `low ≤ c < judge_band_high` → verify
  (judge + arbiter); `< low` → retry until `retry_max`; then boss.

Defaults: global `high 0.95`, `low 0.70`, `retry_max 2`,
`judge_band_high 0.95`. `arbiter_retry_max`, `judge_max_passes` and
`conflict_threshold` (`taxonomy.yaml:110-114`) are **not read by current code**
(kept for config compatibility) — flagged below.

### `jev` — opt-in decision gate {#jev}

`provider`, `model`, `base_url`, `temperature`, `accept_threshold`, `timeout_s`,
`max_retries` (`taxonomy.yaml:142-160`), read by `settings.jev_config`
(`jev_config`). Every key is only a fallback for the matching
`MAILROOM_JEV_*` / `JEV_*` env var (see above). Defaults: `provider: off`,
`model`/`base_url` blank (provider defaults, `_JEV_PROVIDER_DEFAULTS`),
`temperature 1.0`, `accept_threshold 0.8`, `timeout_s 10.0`, `max_retries 2`
(`_JEV_DEFAULTS`). Effect: a non-`off` provider makes `load_gate()`
prefer a `JevGate` once `<base_dir>/models/jev_calibration.json` exists;
otherwise the deterministic band/learned gate is unchanged
(`agents/gate.py:169-180`, `load_jev_gate` in `agents/jev.py`). Full detail:
[JEV.md](JEV.md).

### `specialist_conditions` — per-class run caps {#specialist-conditions}

`input_cap_chars`, `output_cap_tokens`, `temperature`, `retries`, `merger_mode`
per class (`taxonomy.yaml:144-178`), parsed into `RunConditions`
(`settings.py:46-59`, `settings.py:91-92`) and used by `extract`
(`agents/specialists.py:259-275`). `merger_mode: frozen|dagger` selects the
merger-agreement path: `frozen` = one cap or 15k head + 15k tail; `dagger` =
47,000-char windows / 6,500 overlap with SAND-37 sampling (`agents/specialists.py:39-51`,
`agents/specialists.py:95-116`). Defaults per class: insurance 13,500, contract
24,000, corporate 15,000, correspondence 12,000, merger 30,000 (all 8,192 output,
temp 0.1 except contract/merger 0.7, retries 2).

### `bert` — local classifier handoff {#bert}

`enabled` (`false`), `defer_classes` (`[contract, merger_agreement]`),
`max_trusted_windows` (`1`), `pass_subclass_hint` (`false`) (`taxonomy.yaml:137-141`),
parsed into `BertCfg` (`settings.py:39-43`) and applied in `decide_handoff`
(`ingest/bert.py:138-149`). Effect: `enabled: false` (or a missing package/model
or any error) fails open to a `FULL` sort; deferred classes and multi-window
verdicts also use `FULL`; a trusted `fast_path` verdict locks `doc_type` and runs
`SUBCLASS_ONLY`.

### `required_fields` — extraction confidence coverage {#required-fields}

Per-class list of extraction-schema fields (`taxonomy.yaml:376-403`). Read by
`_coverage` in `agents/specialists.py:236-245`; the share non-empty feeds the
deterministic extraction confidence
`schema_valid × (0.6·coverage + 0.4·1.0)` (`agents/specialists.py:83-89`). Remove
the block to fall back to "all schema fields except `reasoning`/`confidence`"
(`agents/specialists.py:240-242`).

### `cost_models` — per-token pricing {#cost-models}

`{model: {input_per_million, output_per_million}}` (`taxonomy.yaml:68-98`). Read
by `eval/cost.model_prices` for `per_token` card cost (`eval/cost.py:34-61`) and
by `pipeline/report._cost_usd` for the per-document estimate
(`pipeline/report.py:54-71`). Unknown models estimate cost as `0.0`. Self-hosted
Modal tiers price at `0.0` because cost is GPU-time, not tokens.

### Other blocks

| Block | Lines | Read by | Effect |
| --- | --- | --- | --- |
| `pipeline.bins` | `taxonomy.yaml:6-17` | **nothing** | Descriptive; `Bins` hardcodes bin names (`storage/bins.py:34-58`). |
| `pipeline.pdf_direct_chars_per_page` | `taxonomy.yaml:17` | **nothing** | Inert in this repo (no code reads it). |
| `llm_retry` | `taxonomy.yaml:19-29` | `llm/retry.py:66-84` | `base_delay`, `max_delay`, `jitter`, `rate_limit_base_delay`, `modal_cold_start_max_delay` drive backoff. `max_attempts: 5` is **not** read (the code default is 4, `llm/retry.py:87`). |
| `vllm_model_map` | `taxonomy.yaml:35-39` | `llm/client.py:95` | Rewrites champion ids to served HF ids when `DEFAULT_PROVIDER=vllm`. |
| `llamafile_model_map` | `taxonomy.yaml:45-49` | `llm/client.py:100` | Rewrites ids to the single GGUF alias. |
| `chunking` | `taxonomy.yaml:60-63` | **nothing** | Inert; windowing is implemented at `agents/specialists.py:95-116`. |
| `field_scoring` | `taxonomy.yaml:193-221` | only if `scoring.configure_from_taxonomy` is called | **Not auto-wired**: no repo code calls it, so scoring uses library defaults (`scoring/field_scoring.py:77-81`, `scoring/config.py:591`). |
| `vision` | `taxonomy.yaml:235-260` | vision path | Model substring allow/exclude lists and `max_pages`/`dpi` for image input. |
| `doc_classes` | `taxonomy.yaml:262-354` | `settings.py:96`, sorter/specialists | The five classes, their schema names, specialist roles, descriptions and `field_types`. |
| `file_extensions` | `taxonomy.yaml:405-417` | **not** the API | The API keeps its own allow-list as `.txt .md .pdf .docx .rtf .html .htm` (`api/app.py:64-66`); ingest accepts `.txt/.md/.text` and `.pdf` (`ingest/clerk.py:38`, `ingest/clerk.py:149-166`). |

**Unconfirmed / inert blocks:** `pipeline.bins`, `pipeline.pdf_direct_chars_per_page`,
`chunking`, and the `field_scoring` auto-wiring are present in the file but not
read at runtime. `llm_retry.max_attempts`, `confidence.arbiter_retry_max`,
`confidence.judge_max_passes` and `confidence.conflict_threshold` are also not
read by current code. Do not rely on them until wired.

## Sorter calibration and learned gate

Optional JSON files under `<base_dir>/models/` change routing:

- `models/calibration.json` — nested `{provider: {model: {doc_type: temperature}}}`
  used by the sorter to temperature-scale raw confidence
  (`agents/sorter.py:61-113`, `agents/sorter.py:198-201`). Fit with
  `mailroom train-gate --calibration`.
- `models/route_gate.json` — `{stage: {features, coef, intercept, threshold}}`
  used by `LearnedGate` only inside the medium band (`agents/gate.py:114-175`).
  Fit with `mailroom train-gate`.
- `models/jev_calibration.json` — Jev calibration
  (`load_jev_gate` in `agents/jev.py`). Fit with `mailroom jev calibrate`; only read when
  `MAILROOM_JEV_PROVIDER` is not `off` ([JEV.md](JEV.md)).

All are optional; without them the deterministic bands decide
(`agents/gate.py:169-180`). See [EVALUATION.md](EVALUATION.md) for training.

## Cross-links

- [ARCHITECTURE.md](ARCHITECTURE.md) — how the settings are consumed.
- [JEV.md](JEV.md) — the opt-in Jev decision model and its calibration.
- [OPERATIONS.md](OPERATIONS.md) — observability env vars in context.
- [gmail-intake.md](gmail-intake.md) — the Gmail env vars.
- [DEV_SERVER.md](DEV_SERVER.md) — the dev compose stack.
