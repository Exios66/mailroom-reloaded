# Plan: full-pipeline TUI, Docker, Modal and Phoenix/Grafana wiring (Phase 6)

> Parent: `2026-10-09-mailroom-core-plan.md` (Phase 6). Status of this file: **draft for owner review**, written 2026-10-10 against `main` @ `502995a`. Nothing here is implemented yet. Items are checkboxes; tick them in the PR that ships them, and cite the evidence path.

## 0. Goal

Make the `/tui` browser terminal the single operator console for the whole Mailroom pipeline: start and watch ingest, work the review queue, launch evals, see health and cost, replay traces, and jump to (or glance at) Phoenix and Grafana, on both the local Docker stack and a Modal-hosted vLLM. Every capability is a small, separately testable module behind the existing command registry, and every command has a Playwright check and a staged screenshot (`docs/demo/`, policy in 6.7).

Non-goals: replacing Phoenix or Grafana (the TUI links to them and shows a thin summary, it does not re-implement them); a multi-user auth model; changing pipeline behaviour.

## 1. Baseline (what exists today)

| Area | Present | Absent |
| --- | --- | --- |
| TUI commands (22) | help man clear history neofetch theme crt skyline ls inspect audit jev review resolve runs cards health upload watch auth ledger replay | start/stop watcher, batch or folder ingest, Gmail poll, review queue with transcript, eval start, queue/metrics, config, `/links`, logs, readiness, provider/GPU/cost, Phoenix/Grafana/Prometheus probe, replay export |
| TUI structure | `tui/main.js` `registerAll` registers `pipeline`, `ledger`, `replay`, `shell` modules; `tui/engine.js` `createRegistry`/`dispatch` | plugin loader; only extension point is `registerPanel` in `replay/panels.js`, and it is replay-only |
| API | `/health`, public `GET /links`, `/v1/replay/*` (incl. `export`, `live` SSE), ledger, intake, `POST /v1/intake/gmail/poll` | `/ready`, `/metrics`, `/v1/config`, `/v1/watcher`, eval-start route; TUI never calls `export` or `gmail/poll` |
| CLI without a TUI twin | serve, watch, run, eval, train-gate, conformance, jev decide/calibrate, gmail auth/poll/watch, sandbox serve/conformance/content | |
| Docker (`deploy/docker-compose.yml`) | app, watcher (profile `split-watcher`), otel-collector, vllm-targets, phoenix, prometheus, grafana, llamafile (`local-llm`), vllm and dcgm-exporter (`gpu`); loopback ports app 8000, phoenix 6006, prometheus 9090, grafana 3000, otel 4317/4318/8889, llamafile 8080, vllm 8001, dcgm 9400, sandbox 8100; Dockerfile `/health` check | healthchecks for phoenix, prometheus, grafana; compose smoke never run (R-04); `docs/evidence/` empty |
| Modal (`deploy/modal_vllm.py`) | app `mailroom-vllm`, secret `mailroom-vllm-api-key`, `deploy_config()` env knobs, teardown `modal app stop mailroom-vllm`, app consumes `VLLM_BASE_URL`/`VLLM_API_KEY` | deploy/status surface, GPU telemetry, cost, a conformance doc; never deployed (R-07) |
| Observability | settings `public_url`/`phoenix_url`/`grafana_url`, `MAILROOM_PHOENIX_PROJECT`, OTLP to Collector, traces to Phoenix, metrics exporter 8889, dashboards `pipeline.json` `quality.json` `serving-gpu.json` | `mailroom.eval.*` is specified but never emitted (R-05); aggregated health; any TUI view of traces/metrics outside replay |

## 2. Architecture rules

1. **One module per concern.** New commands live in `tui/commands/<name>.js` exporting `register(registry, ctx)`, listed in `registerAll`. No command imports another command; shared code goes in `tui/lib/` (HTTP client, table renderer, poller, formatters).
2. **A real plugin boundary (P6-A).** `tui/plugins.js` defines `registerModule({id, commands, panels, onInit})` so a module can contribute commands *and* panels (generalising `registerPanel`). `registerAll` becomes a list of modules. Reserved ids are rejected, duplicates throw, rows are sanitised (the same fixes as replay follow-up 8, done once, centrally).
3. **API stays thin and read-mostly.** New routes sit in `api/routes/<name>.py` (not more code in `app.py`), are token-gated like `/v1/*`, and return the shapes in section 5. Control endpoints (watcher, eval) are POST, idempotent, and refuse when the server is not configured to allow them (`MAILROOM_ALLOW_CONTROL=1`, default off outside dev).
4. **No secrets reach the browser.** `/links` and `/v1/config` expose only credential-free http(s) URLs and non-secret settings. Tokens stay in `sessionStorage`. Probes of Phoenix/Prometheus/Grafana run **server-side** (`/ready`), so the browser never needs CORS or their credentials.
5. **Degrade, never crash.** Each probe has a 2 s timeout and reports `ok | degraded | down | unconfigured`; a missing backend is a status, not an error.
6. **Every item ships with**: unit tests (node or pytest), a Playwright check in `scripts/tui_*_check.mjs`, a docs/TUI.md entry, a CHANGELOG line, and a screenshot staged per 6.7.

## 3. Phases

### P6-A: Foundations (no user-visible behaviour change)
- [ ] **A1 plugin boundary** (rule 2) with tests: duplicate/reserved id rejection, panel rows sanitised, module init failure isolated (one bad module does not stop the TUI booting).
- [ ] **A2 `tui/lib/`**: `http.js` (auth header, timeout, typed errors, 401 → "run `auth`"), `table.js`, `poll.js` (visibility-aware interval with abort), `fmt.js` (bytes, durations, USD).
- [ ] **A3 `api/routes/` split** for new routes only; existing routes untouched.
- [ ] **A4 `/ready`** (5): aggregate app DB, ledger, span store, Collector, Phoenix, Prometheus, Grafana, LLM provider. Public summary status code only; per-component detail behind the token.
- Depends on: nothing. Risk: low.

### P6-B: Operator commands (TUI ↔ existing API)
Each command is read-only unless marked (control).
- [ ] **B1 `links`** prints `/links` (now including `sandbox_url`) and offers `o`/`g`-style open (`links open phoenix|grafana|prometheus`). Reuses the credential-free URL validation from #54.
- [ ] **B2 `ready`** (`health --all` alias) renders the `/ready` table with colour-coded status and latency; `watch ready` re-polls.
- [ ] **B3 `config`** shows non-secret effective settings (`GET /v1/config`): provider, model, base URL host, public URLs, retention, feature flags. Secrets are shown as `set`/`unset` only.
- [ ] **B4 `replay export <session> [--out file]`** wires `GET /v1/replay/sessions/{id}/export` (browser download). Mirrors CLI `mailroom replay export`.
- [ ] **B5 `queue`/`review queue`**: list items awaiting review with transcript view (`review show <id>` renders the conversation, verdict, rubric scores); `resolve` already exists for decisions.
- [ ] **B6 `ingest` (control)**: `ingest upload` (exists as `upload`), `ingest folder <path>` (server-side path inside the configured intake dir only), `ingest gmail poll` (`POST /v1/intake/gmail/poll`). Result is streamed into the existing `watch` view.
- [ ] **B7 `watcher start|stop|status` (control)** against `/v1/watcher` (5). Shows the watcher mode (in-process vs `split-watcher` container) and refuses start when split.
- [ ] **B8 `eval run <suite> [--sandbox] [--scenarios …]` (control)** posts to `/v1/evals`, returns an eval id, then `eval status|log <id>` follows it; finished evals link to `replay` and Phoenix (project filter).
- [ ] **B9 `metrics`**: queue depth, docs/min, p50/p95 stage latency, error rate, token/cost totals from `/v1/metrics/summary` (5), with sparklines; `g` opens the matching Grafana dashboard.
- [ ] **B10 `logs [--follow] [--level]`**: tail from `/v1/logs` (bounded ring buffer, SSE for follow). Redaction filter applied server-side.
- [x] **B11 `inbox [--tab] [--scenario] [--message] [--thread] [--no-mailbox] [--print]`** (shipped): opens the sandbox UI's Correspondent inbox (Ingress queue plus the Boss mailbox dock, `role=correspondent`) in a new tab so a running simulation can be watched live; `/tui#inbox[=tab]` is the inbound link and `MAILROOM_SANDBOX_URL` / `GET /links` `sandbox_url` supplies the base. The sandbox is another origin with no CORS or framing, so the TUI only builds the link (the UI polls every 2 s); the token is never put in the URL. A server-side sandbox proxy for an in-TUI panel stays under D14.
- Depends on: A1–A4. Acceptance per item in 6.6.

### P6-C: Phoenix and Grafana wiring
- [ ] **C1 deep links everywhere**: `links`, the `replay` viewer (`o`/`g`, exists), and now `inspect`, `eval status`, `review show` show an `[phoenix]` link built from `MAILROOM_PHOENIX_PROJECT` + trace id; Grafana links carry `var-run=<run id>` and a time window.
- [ ] **C2 trace summary panel** registered through P6-A (`registerModule`): in the replay viewer (`p` key) and as `trace <run>` standalone, showing span counts, slowest spans, error spans, token use, sourced from the local span store (not Phoenix), plus a "open in Phoenix" key.
- [ ] **C3 Grafana dashboard provisioning check**: `ready` verifies the three dashboards (`pipeline`, `quality`, `serving-gpu`) are loaded via Grafana's HTTP API using the read-only service account; reports `missing dashboards`.
- [ ] **C4 `mailroom.eval.*` (R-05)**: either emit the specified spans/metrics from the eval runner (preferred, small) or amend the spec to match what is emitted; the `quality.json` dashboard must have real data in the smoke test.
- [ ] **C5 optional server-side Phoenix proxy**: *decision D14* (6.8). Default is **no proxy**; links open in a new tab.
- Depends on: A4, B1; C4 needs the eval runner (B8 can ship first with C4 following).

### P6-D: Docker deployment
- [ ] **D1 healthchecks** for phoenix (`/healthz`), prometheus (`/-/healthy`), grafana (`/api/health`), otel-collector (health_check extension), with `depends_on: condition: service_healthy` for app → collector.
- [ ] **D2 profiles audit**: documented matrix of `default`, `split-watcher`, `local-llm`, `gpu`; `docker compose config` run for each profile in CI (no daemon needed).
- [ ] **D3 `deploy/docker-compose.override.example.yml`** for the TUI-first local flow, and `scripts/stack_up.sh` / `stack_down.sh` wrappers that refuse to start without `MAILROOM_API_TOKEN` and `GRAFANA_ADMIN_PASSWORD`.
- [ ] **D4 compose smoke (R-04)**: run on a host with Docker: bring the stack up, hit `/ready`, run one sample document through, confirm a trace in Phoenix and a series in Prometheus, capture `docs/evidence/<date>-compose-smoke/` (command log, `docker compose ps`, `/ready` JSON, screenshots of the TUI, Phoenix and Grafana). **Needs an owner or CI runner with Docker.**
- [ ] **D5 CI job** `compose-smoke` (workflow_dispatch + nightly) running D4 headlessly; uploads evidence as an artifact.
- [ ] **D6 `docs/OPERATIONS.md`**: one runbook for ports, volumes, backups, upgrade, reset, and how each `ready` state maps to a fix.

### P6-E: Modal infrastructure
- [ ] **E1 `deploy/modal_vllm.py` hardening**: pin image tags, health route used by the app, `MODAL_*` knobs documented in one table, scaledown and max-containers defaults chosen for cost.
- [ ] **E2 provider discovery**: `VLLM_BASE_URL` pointing at a `*.modal.run` host is detected and reported by `/ready` and `config` (provider = `modal-vllm`), with a warm/cold latency probe.
- [ ] **E3 `modal` TUI command**: `modal status` (deployed? URL host, GPU type/count from config, last request latency, container warm/cold), `modal wake` (control, sends one cheap request), `modal teardown` prints the exact `modal app stop mailroom-vllm` command (the TUI never holds Modal credentials and never stops the app itself).
- [ ] **E4 cost surface**: token counts from spans × a configurable price table (`MAILROOM_PRICE_TABLE` JSON) in `metrics`; labelled **estimate**. GPU-seconds are shown only when `dcgm-exporter` or Modal usage data is available, otherwise "n/a".
- [ ] **E5 conformance (R-06/R-07)**: `docs/MODAL.md` documents deploy, secret creation, smoke call, teardown; `mailroom conformance --provider modal` runs the same checks as the other providers. **R-07 evidence needs owner Modal credentials**; until then these stay unticked and the doc says "not run".
- [ ] **E6 Dashboard**: extend `serving-gpu.json` with a Modal row (requests, latency, cold starts) fed by the app's own metrics, since Modal exposes no scrape endpoint.

### P6-F: End-to-end verification and release proof
- [ ] **F1 `scripts/tui_pipeline_check.mjs`**: Playwright walkthrough of every new command against a dev stack with a stub provider (offline), asserting visible output, no console errors, no secrets in the DOM.
- [ ] **F2 demo regeneration**: `scripts/demo_capture.mjs` (Phase 6 extends the manifest) re-captures all screenshots; CI fails if `manifest.json` hashes drift from the files.
- [ ] **F3 release notes**: `scripts/demo_release_notes.py --sha <tag-sha>` output pasted into the GitHub release body (no create-release tool exists in the agent environment; the owner or a workflow publishes).
- [ ] **F4 docs**: `docs/TUI.md` command reference regenerated from the registry (`help --md`), so docs cannot drift.

## 4. Dependencies and order

```
A1 A2 A3 ──► A4 ──► B1 B2 B3 B4 ──► C1 ──► C2
              │                      │
              ├──► B5 B6 B7 B8 ──► C4 ──► C3
              └──► B9 B10 ──► E4 E6
D1 D2 D3 ──► D4 ──► D5            (independent of TUI work; D4 needs Docker)
E1 ──► E2 ──► E3 ──► E5           (E5 needs Modal credentials)
All ──► F1 F2 F3 F4
```
Suggested PR slicing (each PR small, independently green): (1) A1–A3; (2) A4 + B1–B3; (3) B4–B5; (4) D1–D3 (parallel); (5) B6–B8; (6) B9–B10 + C1; (7) C2–C4; (8) E1–E4; (9) F1–F4; (10) D4/D5/E5 evidence PRs when the environment exists.

## 5. API contract sketches

All routes require the bearer token when `MAILROOM_API_TOKEN` is set, except where noted. JSON, `schema_version: 1`.

- `GET /ready` (public status code, detail behind token) → `{status: "ok|degraded|down", components: [{name, status, latency_ms, detail?}]}`. Components: `db`, `ledger`, `span_store`, `collector`, `phoenix`, `prometheus`, `grafana`, `llm_provider`, `watcher`. HTTP 200 for ok/degraded, 503 for down.
- `GET /v1/config` → non-secret effective settings; secret fields as `"set"|"unset"`.
- `GET /v1/watcher` / `POST /v1/watcher {action: "start"|"stop"}` → `{mode: "in-process|split|off", running, last_poll_at}`; 403 unless control is enabled; 409 in split mode.
- `POST /v1/evals {suite, sandbox?, scenarios?}` → `202 {eval_id}`; `GET /v1/evals/{id}` → `{state, progress, run_id?, error?}`; `GET /v1/evals/{id}/log` (SSE).
- `GET /v1/metrics/summary?window=15m` → queue depth, throughput, latency p50/p95 per stage, error rate, tokens, estimated cost.
- `GET /v1/logs?level=&limit=` and `/v1/logs/stream` (SSE); redacted ring buffer, capped.
- `GET /v1/review/queue`, `GET /v1/review/{id}` (with transcript) if not already covered by existing review routes (verify first; reuse them if present).

## 6. Testing, acceptance and evidence

6.1 **Unit**: every TUI module has `node --test` coverage with a fake `ctx` (no network); every route has pytest with `TestClient` and an auth-required case.
6.2 **Contract**: route response shapes are asserted against JSON-schema files in `schemas/` so the TUI and API cannot drift.
6.3 **Browser**: `scripts/tui_pipeline_check.mjs` (F1) plus the existing `tui_replay_check.mjs`; fails on any console error or uncaught rejection.
6.4 **Docker**: D4 smoke against the real compose stack; `docker compose config` for every profile in ordinary CI.
6.5 **Modal**: E5 conformance when credentials exist; otherwise stub-server tests prove the TUI/API handle `modal.run` URLs, cold starts and 5xx.
6.6 **Acceptance per item**: command listed in `help`; documented in `docs/TUI.md`; works with no token (clear "auth" hint), with a bad token (401 handled), and with the backend down (status, no exception); one staged screenshot; CHANGELOG line.
6.7 **Demo screenshots policy**: `docs/demo/` holds PNGs (1200×800, ≤400 KB, no secrets, taken from seeded showcase data) with `manifest.json` (file, caption, command, source commit, sha256, size, viewport) and a README. PR bodies and releases embed them via `https://github.com/<repo>/blob/<sha>/docs/demo/<file>?raw=true` generated by `scripts/demo_release_notes.py`. A PR that changes TUI output regenerates the affected shots. Evidence for infrastructure runs (Docker, Modal) goes in `docs/evidence/<date>-<topic>/`.
6.8 **Cannot be verified from the agent sandbox** (must stay unticked until an owner/CI with the resources runs them): D4/D5 (Docker daemon), E5/E3 against a live deployment (Modal credentials), C3 against a real Grafana, any GPU telemetry.

## 7. Risks

| Risk | Mitigation |
| --- | --- |
| Control endpoints (watcher, eval) expand the attack surface | Off by default, token-gated, loopback-only compose ports, audit-logged to the ledger |
| Phoenix/Grafana URLs or tokens leak via `/links`/`config` | Reuse #54 URL validation; secrets as `set/unset`; tests assert no `://user:pass@` and no known secret env values in any response |
| Probes slow the UI or hang | 2 s timeouts, concurrent probes, cached 5 s |
| Cost numbers mistaken for billing | Always labelled "estimate"; price table is owner-supplied |
| Scope creep into re-implementing Grafana | Rule: summaries and links only |
| `app.py` growth | Rule 3: new routes under `api/routes/` |

## 8. Decisions needed

| ID | Question | Recommendation |
| --- | --- | --- |
| D14 | Add a server-side reverse proxy to Phoenix/Grafana (single origin, one login)? | No for now; open in a new tab. Revisit if embedding is wanted |
| D15 | Allow control endpoints (watcher start/stop, eval start) from the browser at all, and under what flag? | Yes, behind `MAILROOM_ALLOW_CONTROL=1`, default off |
| D16 | Who runs D4/D5/E5 (Docker host, Modal account)? | Owner, or a self-hosted runner with a Modal token secret |
| D17 | Cost price table source of truth | Owner-supplied JSON; no hard-coded prices |
| D18 | Is `mailroom.eval.*` emitted (C4) or removed from the spec? | Emit; it is what `quality.json` needs |

## 9. Definition of done

- [ ] Every row of the section 1 "Absent" column is either shipped (with evidence) or listed here as deferred with a dated reason.
- [ ] `help` lists all new commands; `docs/TUI.md` is generated from the registry.
- [ ] `scripts/tui_pipeline_check.mjs` green; `docs/demo/manifest.json` hashes verified in CI.
- [ ] `docs/evidence/` contains a compose-smoke run and (owner) a Modal conformance run, or the master plan records them as not run.
