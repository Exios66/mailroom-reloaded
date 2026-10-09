# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Trace replay stack 10: span retention (`storage/retention.py`, `MAILROOM_TRACE_KEEP` = `pinned`/`recent:<N>`/`all`). Startup and daily prune of spans older than the live window, keeping pinned runs and four built-in showcase runs (seeded from digest-verified `showcase/*.json`); `pin`/`unpin`/`set_policy` are ledger entries, each pruned run gets one `pruned` entry, and ledger rows are never deleted.
- Trace replay stack 8: external anchor for the archive ledger head (`storage/anchor.py`; `MAILROOM_ANCHOR` = `none`/`export`/`postgres`/`supabase`, `MAILROOM_ANCHOR_URL`, `MAILROOM_ANCHOR_KEY`, `MAILROOM_ANCHOR_KEY_FILE`; optional `anchor` extra for `psycopg`). `mailroom audit verify [--run] [--external]`, `audit anchor` and `audit export-head` with exit codes 0/1/3/4/5; pushes run in a background thread and never fail the pipeline. Protects truncation, rollback and rewrite up to the last anchor against an attacker without the writer credential, not the unanchored tail, open runs or a credential holder; the Supabase grants, trigger and key header are unverified until tried on staging (`deploy/anchor/mailroom_anchor.sql`).
- Trace replay stack 7: every metric data point now carries `run_id` and `environment` (current run scope; `unscoped` outside one), new decision counters `mailroom.retries{kind}`, `mailroom.escalations{to}` and `mailroom.review.causes{cause}`, and the Grafana dashboards link to the replay viewer (`replay ↗`, per-run row links) and Phoenix (`phoenix ↗`), gain a *Decisions* row, and the Quality dashboard now queries metrics that are actually emitted (it queried `mailroom_eval_*`, which nothing emitted).
- Trace replay stack 6: span capture. Root and node spans carry the `mailroom.*` attributes (run, doc, station, phase, stage, attempt, retry kind, deadline and budget, tokens, cost, failure reason and class, per-node results), decision events (`route`, `gate_decision`, `retry`, `escalation`, `judge_gate`, `arbiter`, `boss`, `parked`, `archived`, `llm_retry`), and scores as `mailroom.score.<name>` span attributes from a tiered registry (`obs/scores.py`), including review causes (`obs/reconsideration.py`, a port of The-Mailroom) and, for eval, ground-truth and grounded field-level scores on the `grade` span. Each `call_structured` call gets a `mailroom.llm.<role>` span (tokens, cost, model; no content).
- Trace replay stack 5: the local span store (`storage/span_store.py`, `traces.db`, WAL). A batched `SpanExporter` with an attribute and event-key allow-list (no prompts, completions, document text or exception messages, masked or not), bounded strings, a per-run row cap and retention primitives; `RunScopeSpanProcessor` stamps every span (including LLM spans from the instrumentors) with `mailroom.run_id` / `session.id` / `mailroom.environment`. Attached by `setup_tracing`.
- Trace replay stack 4: ledger intake. Every document invocation writes one `doc_closed` (outcome, bounded failure reason and class, the document's `audit_log` head, per-invocation `usage_by_role` and `usage_complete`, metric rows), live runs get a daily `run_opened`/`run_closed` with lazy rollover and a startup closeout, eval runs are opened and sealed by `run_eval` (including cell mode and error rows). Best effort: a ledger failure never changes a document's outcome or masks its exception.
- Trace replay stack 3: the archive ledger (`storage/ledger.py`, `schemas/ledger.py`, tables `ledger` and `ledger_metrics`). One global hash chain over every run (same canonical-JSON hashing as the audit log, which is untouched), a single writer thread with batched `BEGIN IMMEDIATE`, a per-run metric row cap (5,000) with one `gap` entry, a per-payload allow-list, and a Merkle root over each run's `doc_closed` digests. Nothing writes to it yet.
- Trace replay stack 2: complete per-role LLM usage. Judge, arbiter and boss token usage (CrewAI `token_usage`) and vision transcription usage are now counted in `state.usage_total`, the token-budget guards and the LLM metrics; `state.usage_by_role` and `usage_partial_nodes` record the split; report cost sums each role at its own price; the eval grader is tracked separately from pipeline totals.
- Trace replay stack 1: `obs/run_context.py` (`run_scope`, `ensure_run_scope`, daily `live-<YYYYMMDD>` run id; opened inside `run_document` and `run_eval`) and `obs/attrs.py` (attribute keys, station map, span kinds, bounded `failure_class` / `failure_reason` vocabularies). No behaviour change yet.
- Sandbox ingress simulator (stacked on the M6 content loader): `mailroom sandbox serve [--host 127.0.0.1] [--port 8100] [--content smoke|locked|<dir>] [--data-dir PATH]` starts an offline FastAPI app (`mailroom_reloaded.sandbox.server`) with a single-page `/ui` and a JSON API under `/api/sandbox/v1`. It renders scenario timelines from the content pack, meters them with `email/ingress_policy.yaml`, runs a clearly labelled deterministic **stand-in** Correspondent and Boss Desk behind a replaceable `CorrespondentAgent` interface (flow A) and feeds the attachments it hands off into the real pipeline in an isolated data dir on an in-process offline mock LLM (flow B), captures outbound mail in a virtual outbox honouring `recipient_policy.yaml` and the send caps (never transmitted), and links ingress, Correspondent decision, pipeline results and egress in one trace per message with scenario expected-vs-actual. A socket/SMTP guard blocks every non-loopback connection while it runs. Vendored policy files under `sandbox/fixtures/policy/`, `deploy/docker-compose.sandbox.yml` + `deploy/Dockerfile.sandbox`, `scripts/sandbox.sh` (`up|down|status|logs|run|smoke|reset`), `make sandbox`, `jinja2` added to the `sandbox` extra, `docs/SANDBOX_SERVER.md` and tests under `tests/sandbox/test_server_*.py`.
- Sandbox M6 content loader (stacked on M0): `mailroom_reloaded.sandbox.content` (loader, compat check, `sandbox/content.lock` + bundle sha256 verifier, bundle extraction), `mailroom sandbox content pull|validate|build|bump|status`, committed smoke fixtures under `src/mailroom_reloaded/sandbox/fixtures/smoke/` (content v0.5.0) and `sandbox/content.lock` pinning v0.5.0 (tag not yet published).
- Sandbox M0 contracts: top-level `schemas/` (scenario v2, registry v1, overlay v1, gen_spec v1, persona_behavior v1, content-file schemas copied from content v0.5.0; new relation-kind, signal-kind and event-kind enum schemas), `docs/SANDBOX_CONTENT.md` (IDs, vocabulary, schema-major compat policy), `sandbox` extra (`jsonschema`) and example-validation tests.

## [0.2.0] - 2026-10-08

Everything since `main`'s "CrewAi Enhancements" commit (`ce1c1ff`): the
completion branch (plan Tasks 11-24), the Jev gate, the browser TUI and the
PR #5 audit hardening (PRs #9, #11, #12, #13).

### Added

- Jev features-mode harvest over production `GateFeatures` (`scripts/jev_harvest.py --mode features`, `scripts/jev_export_gate_features.py`); eval runner creates parent dirs for nested inbox filenames.

- CrewAI `MailroomFlow` with deterministic route gate, guards, report writer and archivist, plus manifest-based crash resume (Tasks 15-16).
- `/v1` FastAPI API, localhost runs UI (`/ui`) and the `mailroom` CLI (Task 19).
- Blind/ground-truth dataset loader and eval runner, SAND-37 KPIs and cards, vLLM telemetry and cost (Tasks 20-21).
- Behavioural conformance suite and `mailroom conformance --provider` card CLI (Task 24, live run still pending).
- Dataset-derived `required_fields` for extraction confidence.
- Gmail intake route (optional extra) for demo and document upload.
- Dedicated dev server (`scripts/dev.sh`, `docker-compose.dev.yml`, mock OpenAI provider), dev test runner and a pytest `live` marker.
- Operator documentation set: architecture, configuration, evaluation, operations, testing, dev server.
- Opt-in calibrated Jev probabilistic scorer and three-tier route gate, `.env` support for `JEV_*` settings, a train-split calibration harvester, and gate-feature export scripts (#12).
- Jev gate decisions recorded in the audit log and exposed at `GET /v1/jev`; mock Jev dev stack.
- `/tui` browser terminal in the Mailroom brand kit's Terminal edition: command engine, boot sequence, pipeline and shell commands, CRT/skyline ambience, six themes, local browser test harness and `docs/TUI.md` (#13), with a `jev` command and gate audit display added in hardening.
- Telemetry and unit-test coverage for evaluation, pipeline routing, archival, watcher and review edge cases.
- `DISCUSSION_BOARD.md` agent work log.

### Changed

- Prompt files are packaged inside `src/mailroom_reloaded/prompts/`.
- The TUI banner uses original `MAILROOM` wordmarks because the brand kit's art was damaged.
- Parked documents are now catalogued, so `review` and `ls --status parked` see them.
- Conformance pass rates are never reported vacuously; pytest runs with `--strict-markers` and excludes `live` tests by default.
- The API's reported version now comes from `mailroom_reloaded.__version__`.
- Project version bumped to 0.2.0.

### Fixed

- PR #5 audit findings 1-4 (#9), 5 and 9 (#11), 6, 7 and 10 (#12): atomic rename claim of a parked document so concurrent resolves run once; gate-driven retries re-execute nodes completed before a crash-resume; archive crash window reconciled and pending catalog upserts retried at startup; true inflight gauge, `fcntl`-less lock fallback and an honest embedded-watcher startup log.
- Duplicate Gmail intake, pipeline recovery and evaluation metric errors.
- OTLP protocol and signal-specific endpoint settings are now honoured.
- Watcher shutdown, extraction concurrency, pipeline retries and tool usage metrics.
- Jev: schema-valid `noul`, crash-safe calibration, provider key precedence, and `verify_threshold` consumed by the three-tier decision.
- Dev mock provider, dotenv parsing, state permissions and collector isolation.
- Gate and calibration fitting rejects missing and non-train splits.

### Security

- API and UI hardening: review paths secured against traversal, evaluation datasets validated, state file permissions tightened (8c3752d).
- TUI renders every API string with `textContent` only (hostile filenames stay inert), masks `auth <token>` in scrollback and history, keeps the token in `sessionStorage` and sends it only as a bearer header.
- Off-loopback API bind refuses to start without `MAILROOM_API_TOKEN`.
- Open item: Dependabot alerts on `chromadb` (pulled in transitively, unused by this project) are to be dismissed as unused.

## [0.1.0] - 2026-10-08

Baseline on `main`: the design spec, the 24-task plan, and the first build (PR #2, "mailroom-reloaded build"), function-contract documentation (#3) and the "CrewAi Enhancements" commit.

[Unreleased]: https://github.com/Exios66/mailroom-reloaded/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Exios66/mailroom-reloaded/compare/ce1c1ff...v0.2.0
[0.1.0]: https://github.com/Exios66/mailroom-reloaded/commits/ce1c1ff
