# Mailroom Trace Replay Viewer Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Status:** draft for review. This plan proposes work; nothing below is implemented yet.

**Goal:** Add `replay`, an alternative viewer launched from `/tui`. It plays a mailroom pipeline session back from captured OpenTelemetry spans (falling back to the audit log) as a live, interactive, scrubbable visualisation. It has play/pause, speed and seek, a node "track" with documents moving along it, a document leaderboard, an event ticker, a per-document inspector, pluggable insight panels, and a follow-live mode.

**Inspiration:** [IAmTomShaw/f1-race-replay](https://github.com/IAmTomShaw/f1-race-replay). Its features: a timeline with play/pause, 0.5–4× speed and seek; a track map with cars as dots; a live leaderboard; click-to-select driver telemetry; pluggable "pit wall" insight windows over a telemetry stream; and a processed-data cache (`computed_data/`) so a session is computed once and replayed many times.

**Architecture:** Four layers that meet at one versioned JSON contract (`replay/v1`):
1. **Capture.** Richer `mailroom.*` span attributes and events, plus a durable local span store.
2. **Timeline.** A pure builder turns spans (or the audit log) into an event-sourced session timeline.
3. **API.** Token-gated `/v1/replay/*` routes, plus an SSE stream for live follow.
4. **Viewer.** Pure JS clock and model modules plus a DOM view, mounted through a new `ctx.takeover` hook in the existing `/tui` terminal.

Because the contract is viewer-agnostic, a later viewer (a `/ui` panel, a desktop app, a Grafana plugin) can consume the same payload.

**Tech Stack:** Only what the repo already ships. `opentelemetry-sdk` (`SpanExporter`, `BatchSpanProcessor`), SQLAlchemy and SQLite, Pydantic, FastAPI `StreamingResponse` for SSE, vanilla ES modules with no build step, and `node --test`. No new Python or npm dependencies.

**Spec / sources:**
- Today's tracing in `src/mailroom_reloaded/obs/tracing.py` and `pipeline/flow.py` (`_drive`, `_guard_node`, `_fail_node`, `_classify_route`, gate audit at about line 472).
- The audit log in `storage/db.py` and `storage/audit_log.py`.
- The TUI in `api/tui/` (see `docs/TUI.md`).
- The brand kit's Terminal edition and pipeline-state colours (Claude artifact "The Mailroom", `project/guidelines/30-pipeline-states.md`), the same source used by `docs/superpowers/plans/2026-10-08-mailroom-tui.md`.

---

## Why this needs a capture layer first

Nothing in the repo saves traces durably today:

- **Spans leave the process and are not kept locally.** They go out over OTLP to `otel-collector` and on to Phoenix (`deploy/otel-collector*.yaml`). There is no file, JSONL or SQLite span exporter, and no Phoenix client.
- **Span attributes are thin.** `mailroom.document` and `mailroom.node.<name>` carry only `mailroom.doc_id`. Run id, attempt, gate action, route and final status are not on spans.
- **The audit log is durable but has gaps.** Rows are `(doc_id, seq, node, event, payload, ts, prev_hash, entry_hash)` with `completed` / `node_failed` (`elapsed_s`), `gate_decision` (`action, reason, source, confidence`), `parked`, `archived` and `review_resolved`. There are **no start events**: start times can only be reconstructed as `ts − elapsed_s`.
- **Eval runs are durable but per-document only.** They live in `eval_docs` (keyed `(run_id, filename)`) with `route_trail`, `latency_s`, tokens and calls, but no timing per node.

So the MVP can replay **approximately** from the audit log on day one, and full fidelity arrives once spans are enriched and stored.

## Concept mapping (F1 → mailroom)

| f1-race-replay | Mailroom replay |
| --- | --- |
| Race / session | `run:<run_id>` (eval run), `doc:<doc_id>` (one document), or `window:<from>..<to>` (live or watcher traffic) |
| Car / driver | Document (`doc_id`, filename, final `doc_type`) |
| Track and sectors | `NODE_ORDER` lanes: `ingest → bert_primary → sort → gate_classify → extract → gate_extract → report_catalog_archive` |
| Pit lane | Retry loops (`retry_sort`, `re_sort`, `retry_extract`) and `verify` (judge → arbiter) / `boss` detours |
| Car in the garage | Parked in `human_review` |
| "OUT" / DNF | `failed` with a reason (`deadline_exceeded`, `token_budget_exceeded`, exception) |
| Chequered flag | `archived` |
| Lap time | End-to-end document latency; sector time = node duration |
| Race-control messages | Ticker: `gate_decision`, `node_failed`, `parked`, `review_resolved`, `archived` |
| Driver telemetry (speed, gear, DRS) | Inspector: current node, attempt, tokens and cost so far, LLM calls, gate confidence and source, Phoenix deep link by `trace_id` |
| Pit-wall insight windows (`PitWallWindow`) | Replay panel registry (`registerPanel`) |
| `computed_data/` cache | Built timelines cached per session id and source watermark |
| Telemetry stream | `GET /v1/replay/live` SSE |

## Sketch of the viewer

Character-cell grid, Terminal edition, `--term-*` tokens. Sizes are illustrative.

```
replay run:3f9c0a1b2d4e · 48 docs · source spans                    ▶ 4×   02:13 / 07:40
───────────────────────────────────────────────────────────────────────────────────────
 ingest   bert    sort    gate_c   extract  gate_x   archive  ▸ ✓ 31
   ●●       ●     ●●●●      ●       ●●●●●     ●●        ●
                   ↺ retry 2                ↺ retry 1
 ▌ parked 3      ✕ failed 1
───────────────────────────────────────────────────────────────────────────────────────
 pos doc_id      file                 node        t      tok    │ ▸ 7b2e… invoice_0412.pdf
  1  a91f…       invoice_0412.pdf     ✓ archive   41.2s  3.1k   │   node     extract (attempt 2)
  2  7b2e…       lease_scan.pdf       extract     38.0s  5.4k   │   gate     do_extract · 0.91 · jev
  …                                                             │   tokens   5.4k · $0.012
───────────────────────────────────────────────────────────────────────────────────────
 02:11  gate_classify  c03d…  retry_sort — low confidence 0.42
 02:12  extract        9e10…  ✕ deadline_exceeded 30.4s
[━━━━━━━━━━━━━┿━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━]   space play · ←→ seek · ↑↓ speed · q quit
```

## Global Constraints

- **No new dependencies.** No Python or npm additions. The dependency fence (`tests/test_dependency_fence.py`) stays green.
- **Read-only viewer.** It never resolves, re-runs or mutates documents. Existing commands (`resolve`, `upload`) remain the only write paths.
- **Rendering safety.** `textContent` only; no `innerHTML`, as in the rest of `/tui`. Hostile filenames and gate reasons render as text.
- **Brand.** Terminal edition `--term-*` tokens only. Stage colours come from the kit's pipeline-states table. The `prefers-reduced-motion` setting gives stepwise playback with no tweening; `hc` drops glows. Voice is lower-case, Unix-TTY, with glyphs limited to `· — ↗ ▸ ● ✓ ✕ ▌` plus `↺ ━ ┿` for the track and progress bar.
- **Privacy.**
  - The span store sits **after** `MaskingSpanProcessor`, so when `MAILROOM_TRACE_MASK=1` it only ever sees masked values.
  - The timeline payload carries **no prompt or completion content**, only names, timings, counts and gate metadata.
  - Inspector links to Phoenix for content; it does not copy it.
- **Durability isolation.** Spans go to a separate `<base_dir>/traces.db` (WAL), never into the hash-chained `mailroom.db` audit log.
- **Performance budget.**
  - 500 documents × about 10 segments at 30 fps on a mid laptop.
  - A timeline payload of 2 MB or less per chunk (windowed above that).
  - Building a 500-document timeline takes under 1 s from SQLite.
- **API contract.** Every `/v1/replay/*` route uses the existing `require_token` dependency. Payloads carry `"version": "replay/v1"`.

## Review Focus

- **Masking order.** A test proves stored span attributes are masked when `trace_mask` is on (Task 2).
- **Approximate timelines.** Audit-only sessions are labelled `source audit (approx)` in the header, and segments carry `approx: true` (Task 3).
- **Hostile strings.** A `<img src=x onerror=…>` filename and a 10k-character gate reason in the leaderboard, ticker and inspector stay text and get truncated (Task 8).
- **Key capture.** `takeover` must not leak keystrokes into the prompt. It must release the keyboard on `q`/`esc`, on a thrown error and on Ctrl+C, and the prompt history must be untouched (Task 7).
- **Large and empty sessions.** For an empty run, the viewer does not open and prints `replay: no timeline for <session> — run 'runs' to list sessions`. A run over the cap is windowed, and the progress bar shows the loaded range (Tasks 5 and 8).
- **Live mode.** It stops on abort and when the tab is hidden, reconnects with backoff, and keeps a bounded buffer (Task 9).
- **Clock correctness.** Seeking backwards recomputes state from the timeline (no incremental drift), and `stateAt(t)` is identical whether reached by playing or by seeking (Task 6).

## File Structure

```
src/mailroom_reloaded/
  pipeline/flow.py            (modify) span attributes + events in _drive/_guard_node/_fail_node/gates
  obs/tracing.py              (modify) attach SqliteSpanExporter after masking when trace_store on
  obs/span_store.py           (new)    SqliteSpanExporter, spans table, retention prune, query helpers
  obs/replay/__init__.py      (new)
  obs/replay/timeline.py      (new)    build_timeline(session) — spans source + audit fallback
  obs/replay/sessions.py      (new)    session id grammar, list_sessions(), source watermark, cache
  obs/replay/otlp_import.py   (new)    OTLP-JSON (collector file exporter) → span store
  schemas/replay.py           (new)    Pydantic replay/v1 models
  settings.py                 (modify) trace_store, trace_store_path, trace_store_days
  cli.py                      (modify) `mailroom replay import|export|sessions`
  api/app.py                  (modify) /v1/replay/sessions, /v1/replay/{session}, /v1/replay/live
  api/tui/terminal.js         (modify) ctx.takeover(factory)
  api/tui/main.js             (modify) registerReplay(registry); #replay= deep link
  api/tui/commands/replay.js  (new)    `replay` command, man page, completion
  api/tui/replay/clock.js     (new)    pure playback clock
  api/tui/replay/model.js     (new)    pure stateAt/leaderboard/ticker
  api/tui/replay/view.js      (new)    DOM render (textContent only)
  api/tui/replay/panels.js    (new)    panel registry + first panels
  api/tui/tui.css             (modify) .replay-* layout using --term-* tokens
deploy/otel-collector*.yaml   (optional, Task 4) commented `file` exporter example
tests/obs/test_span_attrs.py, tests/obs/test_span_store.py, tests/obs/test_replay_timeline.py
tests/api/test_replay_api.py
tests/tui/js/replay_clock.test.mjs, replay_model.test.mjs, replay_view.test.mjs, takeover.test.mjs
tests/fixtures/replay/        small span + audit fixtures (3 docs: happy, retry, failed; 1 parked)
docs/TUI.md, docs/OPERATIONS.md, CHANGELOG.md
```

## `replay/v1` contract

```
Timeline {
  version: "replay/v1"
  session: { id, kind: run|doc|window, t0_iso, duration_s, source: spans|audit, approx: bool,
             window: { from_s, to_s, complete: bool } }
  lanes:    [ { node, order, kind: main|detour|bay } ]          # NODE_ORDER + verify/boss + parked/failed bays
  entities: [ { doc_id, filename, doc_type?, final_status, trace_id?, t_start, t_end? } ]
  segments: [ { doc_id, node, t0, t1, attempt, status: ok|failed|running, reason?,
                tokens?, cost_usd?, llm_calls?, span_id?, approx? } ]   # sorted by t0
  events:   [ { t, doc_id, kind: gate_decision|route|node_failed|parked|review_resolved|archived,
                node?, payload } ]                                # sorted by t; payload has no content
}
```

Times are seconds relative to `session.t0`. The format is event-sourced, not frame-sampled. f1-race-replay precomputes fixed-rate frames because cars move continuously. Documents occupy one node at a time, so piecewise-constant segments with a binary search for `stateAt(t)` are smaller and exact.

---

### Task 1: Enrich pipeline spans

**Files:** Modify `pipeline/flow.py`; Create `tests/obs/test_span_attrs.py`.

**Interfaces:**
- Consumes: `self.state` (`doc_id`, `route_trail`, `classify_attempts`, `usage_total`, `status`, `doc_type`), `self._eval_ctx`, `worker_id`.
- Produces:
  - **`mailroom.document` attributes:**
    - `mailroom.doc_id`, `mailroom.run_id` (from `eval_ctx`, or `MAILROOM_RUN_ID` / watcher batch id when set), `mailroom.worker_id` and `mailroom.filename` (basename only);
    - on exit, `mailroom.status`, `mailroom.doc_type` and `mailroom.route_trail` (comma-joined node names).
  - **`mailroom.node.<name>` attributes:** `mailroom.doc_id`, `mailroom.run_id`, `mailroom.node`, `mailroom.attempt` and `mailroom.tokens.used`. On failure: `mailroom.status=failed`, `mailroom.fail_reason` and span status ERROR.
  - **Span events:**
    - `mailroom.gate_decision`, with `action`, `reason` (truncated to 256 characters), `source` and `confidence`, emitted beside the existing `audit_log.append(... "gate_decision" ...)`;
    - `mailroom.route`, with `from` and `to`, at each `_drive_nodes` transition.
- [ ] **Step 1: Write failing tests.**
  - Run one document through `run_document` with `fake_openai` and `_attach_exporter()` (reuse the fixtures in `tests/obs/test_obs.py`).
  - Assert the attributes above on the root and node spans.
  - Assert a `retry_sort` path yields `mailroom.attempt=2` on the second `sort` span.
  - Assert a `deadline_exceeded` override sets `mailroom.fail_reason`.
  - Assert no attribute value contains document text.
- [ ] **Step 2: Run** `uv run pytest tests/obs -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Use small helpers (`_span_attrs()`, `_emit_gate_event()`) so `_guard_node` stays readable.
- [ ] **Step 4: Run** `uv run pytest tests/obs -v`. Expected: PASS, and the existing `test_flow_emits_node_spans` is still green.
- [ ] **Step 5: Commit** `feat(obs): mailroom.* span attributes and gate/route events for replay`.

### Task 2: Durable local span store

**Files:** Create `obs/span_store.py`, `tests/obs/test_span_store.py`; Modify `obs/tracing.py`, `settings.py`, `docs/CONFIGURATION.md`.

**Interfaces:**
- **Settings:**
  - `trace_store: bool` (env `MAILROOM_TRACE_STORE`; default `False`, set `true` in `deploy/docker-compose.dev.yml` and `scripts/tui_dev.sh`);
  - `trace_store_path` (default `<base_dir>/traces.db`);
  - `trace_store_days: int = 14`.
- **`SqliteSpanExporter(path)`** implements `SpanExporter.export/shutdown`.
  - Table `spans(trace_id, span_id PK, parent_id, name, start_ns, end_ns, status, doc_id, run_id, attrs JSON, events JSON)`, with indexes `(run_id, start_ns)`, `(doc_id, start_ns)` and `(start_ns)`. Opened in WAL mode.
  - Stores `mailroom.*`, `openinference.span.kind`, `llm.token_count.*`, `llm.model_name` and cost attributes only, through an **allow-list**, so no `input.value` or `output.value` ever lands even unmasked.
- **`prune(older_than_days)`**, called at most once per hour from `export`.
- **Query helpers:** `spans_for_run(run_id)`, `spans_for_doc(doc_id)`, `spans_between(t0_ns, t1_ns)` and `latest_end_ns()` (the watermark).
- **`setup_tracing`** adds `BatchSpanProcessor(SqliteSpanExporter)` **after** `MaskingSpanProcessor` when `trace_store` is on. It stays idempotent, and is skipped under pytest unless a test attaches it explicitly.
- [ ] **Step 1: Write failing tests.**
  - Round-trip the spans produced by one fake run; filter by run and by document.
  - The allow-list drops `input.value` even with masking off.
  - With masking on, stored attributes contain no raw values.
  - `prune` deletes old rows.
  - Two exporters on the same file do not corrupt it (WAL).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** with SQLAlchemy Core, the same style as `storage/db.py`.
- [ ] **Step 4: Run** `uv run pytest tests/obs -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(obs): sqlite span store for local trace replay`.

### Task 3: `replay/v1` schema and timeline builder

**Files:** Create `schemas/replay.py`, `obs/replay/{__init__,timeline,sessions}.py`, `tests/obs/test_replay_timeline.py`, `tests/fixtures/replay/*`.

**Interfaces:**
- **Session id grammar:** `run:<run_id>`, `doc:<doc_id>`, `window:<iso>..<iso>`. A bare `<run_id>` is accepted as `run:`.
- **`list_sessions(limit)`:**
  - eval runs from `eval_docs` (with document count, first and last timestamp);
  - recent `run_id`s from the span store;
  - a synthetic `window:` for the last hour of traffic.
- **`build_timeline(session_id, *, from_s=None, to_s=None) -> Timeline`:**
  - **Spans source**, used when the span store has rows for the session:
    - each `mailroom.document` span becomes an entity;
    - each `mailroom.node.*` span becomes a segment, with tokens and LLM calls rolled up from descendant OpenInference spans;
    - span events become timeline events;
    - `trace_id` is kept for Phoenix links.
  - **Audit fallback:**
    - `completed` and `node_failed` rows become segments with `t0 = ts − elapsed_s` and `approx=true`;
    - `gate_decision`, `parked`, `review_resolved` and `archived` rows become events;
    - for `run:` sessions, the document list comes from `eval_docs`, joined to `audit_log` by `doc_id`.
  - Output is sorted, relative to `t0` and windowed.
- **Cache:** a small LRU keyed `(session_id, source watermark)`. The watermark is the latest span end or the maximum audit `seq`.
- [ ] **Step 1: Write failing tests** against fixtures:
  - a happy path, a `retry_sort` path (two `sort` segments, attempt 1 and 2), a `deadline_exceeded` failure, and a parked document;
  - spans and audit sources for the same fixture agree on node order and on durations within 50 ms;
  - an empty session raises `SessionNotFound`;
  - windowing sets `complete=false`;
  - the payload has no field holding document text.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** with pure functions over row iterables. All database access stays in `sessions.py` and `span_store.py`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(replay): replay/v1 timeline from spans with audit-log fallback`.

### Task 4: Import and export for offline replay

**Files:** Create `obs/replay/otlp_import.py`; Modify `cli.py` and `deploy/otel-collector.yaml` (a commented `file` exporter example only); Tests in `tests/obs/test_replay_timeline.py`.

**Interfaces:**
- `mailroom replay import <otlp.json|jsonl>` loads collector `file`-exporter OTLP JSON into the span store, through the same allow-list as Task 2.
- `mailroom replay export <session> [-o file]` writes a `replay/v1` JSON file for sharing or bug reports.
- `mailroom replay sessions` lists sessions.
- [ ] **Steps:** failing tests (import a fixture OTLP-JSON file, then build the timeline; export and re-read the JSON, then validate against the Pydantic model) → implement → pass → commit `feat(replay): otlp-json import and replay/v1 export cli`.

### Task 5: API routes

**Files:** Modify `api/app.py`; Create `tests/api/test_replay_api.py`.

**Interfaces:**
- `GET /v1/replay/sessions?limit=50` returns `[{id, kind, documents, started_at, duration_s, source}]`.
- `GET /v1/replay/{session}?from=&to=` returns a `Timeline`.
  - 404 `{"detail": "no timeline for <session>"}` when the session has no data.
  - When over the cap, the response is windowed with `window.complete=false`, and the client fetches the next chunk.
- `GET /v1/replay/live?since=<watermark>` is `text/event-stream`. It emits `segment` and `event` messages as the store or audit log grows, polling the store every second server-side, plus a heartbeat every 15 s, and closes on client disconnect.
- [ ] **Step 1: Write failing tests.**
  - All three routes return 401 without a token when `MAILROOM_API_TOKEN` is set.
  - The happy path returns the fixture timeline.
  - An unknown session returns 404.
  - Path-traversal-looking session ids are rejected with 422.
  - The SSE route yields at least one heartbeat and one segment when a span is added mid-stream.
- [ ] **Step 2–4:** run (FAIL), implement, then run (PASS).
- [ ] **Step 5: Commit** `feat(api): /v1/replay sessions, timeline and live stream`.

### Task 6: Pure playback clock and model (JS)

**Files:** Create `api/tui/replay/clock.js`, `api/tui/replay/model.js`, `tests/tui/js/replay_clock.test.mjs`, `tests/tui/js/replay_model.test.mjs`.

**Interfaces:**
- **`createClock({duration, now = performance.now, speeds = [0.25, 0.5, 1, 2, 4, 8, 16]})`** returns `{play, pause, toggle, seek(t), nudge(dt), faster, slower, setSpeed(i), tick() -> t, state}`.
  - It clamps to `[0, duration]` and auto-pauses at the end.
  - It has no DOM and no `requestAnimationFrame`; the view drives `tick`.
- **`createModel(timeline)`** returns `{stateAt(t), leaderboard(t), eventsBetween(t0, t1), nextEventAfter(t), prevEventBefore(t), append(chunk)}`.
  - `stateAt` gives each document's lane, attempt, status and cumulative tokens.
  - Leaderboard order: archived (by finish time), then furthest lane, then elapsed time ascending. Failed documents sort last and are marked `✕`.
- [ ] **Step 1: Write failing tests.**
  - `stateAt` from play equals `stateAt` from seek.
  - Seeking backwards across a retry restores attempt 1.
  - Speed bounds hold.
  - The end-of-timeline auto-pause works.
  - `append` of a live chunk keeps ordering.
  - With 500 documents × 10 segments, `stateAt` takes under 2 ms (rough bench, skipped in CI if slow).
- [ ] **Step 2–4:** run (FAIL), implement, then run (PASS).
- [ ] **Step 5: Commit** `feat(tui): pure replay clock and timeline model`.

### Task 7: Terminal takeover hook

**Files:** Modify `api/tui/terminal.js` and `tui.css`; Create `tests/tui/js/takeover.test.mjs`.

**Interfaces:**
- **`ctx.takeover(factory) -> Promise<summary>`.**
  - It hides the scrollback and prompt and mounts a `.takeover` root (`role="application"` with `aria-label`).
  - It routes `keydown` to the view's `onKey(e)` instead of the prompt.
  - It resolves when the view calls `exit(summary)`, when the command's `ctx.signal()` aborts, or when the view throws.
  - On every path it restores the prompt and focus, and prints `summary` as one scrollback line.
- **`factory({root, exit, signal, setStatus})`** returns `{onKey, destroy}`.
- [ ] **Step 1: Write failing tests** with a fake DOM (the minimal stub used by the existing terminal tests):
  - keys go to the view and not to history;
  - `exit` restores the prompt;
  - a thrown error restores it too and prints `replay: viewer crashed — <message>`;
  - Ctrl+C restores it.
- [ ] **Step 2–4:** run (FAIL), implement, then run (PASS).
- [ ] **Step 5: Commit** `feat(tui): ctx.takeover for full-screen viewers`.

### Task 8: Replay command, view and panels

**Files:** Create `api/tui/commands/replay.js`, `api/tui/replay/view.js`, `api/tui/replay/panels.js`, `tests/tui/js/replay_view.test.mjs`; Modify `api/tui/main.js` and `tui.css`.

**Interfaces:**
- **Command:** `replay [session] [--speed N] [--doc ID] [--follow]`.
  - With no session, it lists `GET /v1/replay/sessions` as a table and hints `type 'replay <id>'`.
  - With a session, it fetches the timeline and opens it with `ctx.takeover`.
  - Tab completes session ids from the last listing.
  - The man page uses the NAME/SYNOPSIS/DESCRIPTION/KEYS layout.
  - Errors reuse the `ApiError.kind` messages from `commands/pipeline.js`.
- **View areas:**
  - header (session · documents · source · play state · speed · clock);
  - track strip (lanes with document glyphs `●`, a count when crowded, retry `↺` and the parked `▌` and failed `✕` bays);
  - leaderboard (pos, doc_id short, filename truncated, node, elapsed, tokens);
  - inspector (the selected document's current segment, attempt, gate decision, tokens and cost, `phoenix ↗` link built from `trace_id` and the configured Phoenix URL);
  - ticker (the last N events up to `t`);
  - progress bar (playhead plus event tick marks; failures in `--term-red`).
- **Keys:**

  | Key | Action |
  | --- | --- |
  | `space` | Play or pause |
  | `←` / `→` | Seek ±5 s, or ±5% with `alt` |
  | `shift+←` / `shift+→` | Previous or next event |
  | `↑` / `↓` | Speed |
  | `1`–`7` | Speed preset |
  | `r` | Restart |
  | `j` / `k` | Move the selection |
  | `enter` | Pin the inspector |
  | `l` | Toggle labels |
  | `p` | Cycle panels |
  | `f` | Follow live (Task 9) |
  | `?` | Key help |
  | `q` / `esc` | Exit with `replay: <session> · watched 02:13 of 07:40 · 31 archived · 1 failed · 3 parked` |

- **`panels.js`:** `registerPanel({id, title, key, render(frame, out)})`. It is the counterpart of f1-race-replay's `PitWallWindow`, so new insight views drop in without touching `view.js`. The first three panels:
  - `tokens`: a cumulative tokens and cost sparkline;
  - `gates`: the gate action mix so far (do_extract / retry / park);
  - `latency`: p50 and p95 per node so far.
- **Deep link:** `main.js` reads `location.hash` `#replay=<session>` after boot and dispatches `replay <session>`.
- [ ] **Step 1: Write failing tests** with `makeCtx` and the fixture timeline:
  - a hostile filename renders as text in the leaderboard, ticker and inspector;
  - an empty session message is exact;
  - an approximate source shows `source audit (approx)`;
  - `shift+→` lands exactly on the next event time;
  - the exit summary text is exact;
  - a panel registered from a test appears in the `p` cycle.
- [ ] **Step 2–4:** run (FAIL), implement (`requestAnimationFrame` loop driving `clock.tick`, re-rendering only changed rows), then run (PASS).
- [ ] **Step 5: Commit** `feat(tui): replay viewer with track, leaderboard, ticker, inspector and panels`.

### Task 9: Follow-live mode

**Files:** Modify `api/tui/replay/view.js` and `commands/replay.js`; Tests in `replay_view.test.mjs`.

**Interfaces:**
- `--follow` or `f` opens an `EventSource`-like reader on `/v1/replay/live`. It uses `fetch` with a streamed body so the bearer header is sent, since `EventSource` cannot set headers.
- It `append`s chunks to the model and pins the playhead to `now − 2 s`. Scrubbing back leaves follow mode, and `f` re-enters it.
- It stops when `document.hidden` or on abort, the same as `watch`, and reconnects with backoff of 1, 2, 4 and 8 s (maximum 30 s).
- The buffer is bounded by keeping the last 2 hours, or the last 5,000 segments.
- [ ] **Steps:** failing tests (chunks append in order, hidden tab stops reading, reconnect after drop, scrub back exits follow) → implement → pass → commit `feat(tui): replay follow-live over sse`.

### Task 10: Dev harness, docs and live verification

**Files:** Modify `scripts/tui_dev.sh` (set `MAILROOM_TRACE_STORE=1`; seed a small eval run with a retry, a failure and a parked document), `docs/TUI.md` (a `replay` row, keys, a screenshot description), `docs/OPERATIONS.md` (trace store, retention, import and export, privacy notes), `docs/CONFIGURATION.md` and `CHANGELOG.md` (`[Unreleased]`).

- [ ] **Step 1:** In a real Chromium (Playwright; preinstalled at `/opt/pw-browsers`), open `/tui#replay=run:<seeded>` and check the following:
  1. The viewer opens and plays.
  2. Seek and next-event work.
  3. Speed cycles.
  4. `j`/`k` plus `enter` show the inspector.
  5. The `phoenix ↗` link points at the right trace.
  6. `p` cycles panels.
  7. `q` returns to the prompt with the summary.
  8. Reduced motion steps without tweening.
  9. The `hc` theme is legible.
  10. With the dev stack running, `--follow` shows a newly uploaded document moving along the track.
  11. The console has no errors.
- [ ] **Step 2: Run** `uv run pytest -q` and `uv run ruff check .`. Expected: all pass, ruff clean.
- [ ] **Step 3: Commit** `docs(replay): tui replay docs, dev harness seed and changelog`.

## Phasing

| Phase | Tasks | Result |
| --- | --- | --- |
| **MVP: replay what we already have** | 3 (audit fallback only), 5 (sessions and timeline), 6, 7, 8 | Approximate replay of existing eval runs from `audit_log` + `eval_docs`, with no capture changes. |
| **Full fidelity** | 1, 2, then 3 (spans source) | Exact node start and end times, attempts, tokens and cost per segment, and Phoenix deep links. |
| **Live** | 5 (SSE), 9 | Follow production or dev traffic as it happens. |
| **Portability** | 4 | Replay traces captured on another host, and share `replay/v1` files in bug reports. |

## Non-goals

- An OpenGL / Arcade desktop app; this is a `/tui` viewer.
- Replacing Phoenix: the inspector links out for prompt and content detail.
- A Phoenix REST importer in v1; OTLP-JSON import covers offline use.
- Grafana panels.
- Any mutation from the viewer.
- Fixing per-run Phoenix projects (design spec §9) or the missing `run_id` metric label; both are related and listed under open questions.
- Video or GIF export of replays.

## Open questions

1. **Track rendering:** character grid (recommended, for brand fit and `textContent` safety) or `<canvas>` for smoother motion with many documents.
2. **Store location:** separate `traces.db` (recommended) or a `spans` table inside `mailroom.db`.
3. **Retention default:** 14 days is proposed. Should eval-run spans be kept indefinitely?
4. **Watcher sessions:** should `mailroom watch` and `serve` stamp a synthetic `run_id` per batch or per hour, so live traffic gets named sessions beyond `window:`?
5. **Grafana gap:** add a `run_id` label to the `mailroom.*` metrics in the same effort, so dashboards' `$run_id` variable works and can link to `/tui#replay=run:<id>`?
6. **`/ui` entry point:** add a `replay ↗` link per eval run in `/ui`'s Eval runs section (a one-line change once Task 8 lands)?

## Self-review

- **Coverage of the request:** live, interactive replays (Tasks 6, 8 and 9); built on captured OTel and trace data (Tasks 1–4); modular and reusable (the `replay/v1` contract and the panel registry); an alternative viewer reachable from the TUI (the `replay` command, `ctx.takeover` and the deep link).
- **Coverage of f1-race-replay features:** play, pause, speed and seek; the track map; the leaderboard; driver selection and telemetry; insight windows; the telemetry stream; the processed-data cache. Each maps to a task.
- **Consistency:** the `replay/v1` field names match across Tasks 3, 5, 6 and 8; session id grammar is the same in the CLI, API and TUI.
- **Proportion:** code appears only as interfaces, payload shape and test assertions.
