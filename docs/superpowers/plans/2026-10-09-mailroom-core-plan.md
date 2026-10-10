# Mailroom Core Plan (single source of truth)

> **This is the only live plan.** It replaces and supersedes the three earlier
> plans, the TUI handoff and the issue-1 status draft (all now under
> [`../archive/`](../archive/)), and the content repo's
> `docs/IMPLEMENTATION_PLAN.md` (kept in place there as history).
> New work is added here as a new workstream ID; finished workstreams are
> collapsed into the ledger in Section 3. Do not create a second plan file.

**Goal:** Take `mailroom-reloaded` (the pipeline, `/tui`, trace replay, sandbox)
and its helper `mailroom-sandbox-content` (the synthetic content pack) from
"built and mostly merged" to "verified, released, and consistently organised",
without re-opening anything already done.

**As of:** 2026-10-10 (status refreshed against `main` @ `ab6b715`; the Section 4 audit text below is otherwise the 2026-10-09 audit). A parallel agent is still pushing. Before acting on any
row marked `[PR]`, run `git fetch --all --prune` in both repos and re-check
the PR state; the audit tables in Section 4 will go stale within hours.

| Repo | GitHub | `origin/main` at audit | Open PRs at audit |
| --- | --- | --- | --- |
| mailroom-reloaded | `Exios66/mailroom-reloaded` | `df249e2` (2026-10-10: `ab6b715`) | #23 (`feat/sandbox-correspondent-tuning`, head `64b8400`), #44 (`feat/heldout-boss-mailbox-agents`, `021ecf1`), #45 (`fix/jev-integration-issue-14`, `75da8a1`). **Since the audit:** #45, #54, #55, #56, #57 merged; #23 and #44 still open (the mailbox code is not on `main`) |
| mailroom-sandbox-content | `Exios66/mailroom-sandbox-content` | `f650cfd` | #5 (`feat/heldout-h-series`, `27acdba`); 2026-10-10: #5 and #6 merged, #7 open (see K-00); #15 merged (issue forms, PR template, `AGENTS.md`; see Governance) |

**Evidence rule.** A status below is **verified** only where it says so. The
audit was static (files, symbols, commits, PR state). **No Python test run was
possible** (no deps, offline). The only executed checks: `node --test` on
`origin/main` (198/198 pass), and the content repo's `bash tools/ci.sh` (exit 0;
88 scenarios, 0 errors, 118 unit tests OK). Test counts quoted from PR bodies
(#44: 1876 passed, 1 env-dependent failure; #45: 1831 passed) are the authors'
own and unverified, and they differ because the branches differ.

**Baseline (2026-10-09, this machine, R-01 done).** On `main` @ `44c8b0f`:
`uv run pytest -q` → **1886 passed, 3 skipped, 2 deselected**; `uv run ruff
check .` → clean; `node --test tests/tui/js/*.test.mjs` → **198 pass**. These counts predate #45 and #54-#57 and were **not re-run on 2026-10-10**; re-baseline (R-01) before quoting them for the new `main`. The one
audited failure (`test_api.py::test_jev_status_off_by_default`) was a
test-isolation defect: a developer `.env` sets `MAILROOM_JEV_PROVIDER` and
pydantic environment values win over `.env`, so deleting the key could not
neutralize it; the test now forces `off` explicitly.

---

## 1. Where additions land (file-organisation contract)

Read this before adding any file. If a new file does not fit a row, **stop and
ask the owner**; do not invent a top-level directory.

### 1.1 `mailroom-reloaded`

| What you are adding | Goes in | Rules |
| --- | --- | --- |
| Pipeline / agent / eval / storage code | `src/mailroom_reloaded/<subpackage>/` using an existing subpackage: `agents`, `api`, `config`, `eval`, `ingest`, `intake`, `llm`, `obs`, `pipeline`, `prompts`, `sandbox`, `schemas`, `scoring`, `showcase`, `storage` | No new top-level package without an owner decision. `settings.py`, `cli.py`, `tools.py`, `watcher.py`, `review.py` stay single modules. |
| Pydantic / wire models | `src/mailroom_reloaded/schemas/` | One module per concern (`extraction`, `manifest`, `audit`, `ledger`, `replay`). |
| Prompts | `src/mailroom_reloaded/prompts/` | Packaged for `importlib.resources`; sha256 lock stays in `prompts/loader.py`. Never top-level `prompts/`. |
| Replay / trace code | `src/mailroom_reloaded/obs/replay/` (timeline, sessions, OTLP import), `obs/` (run context, scores, tracing), `storage/{span_store,ledger,retention,anchor}.py` | |
| Browser terminal (`/tui`) | `src/mailroom_reloaded/api/tui/` (`commands/<name>.js`, `replay/*.js`, root `*.js`, `tokens.css`, `tui.css`) | Vanilla JS, no build step. One command per file under `commands/`. `/ui` stays `api/ui/index.html`; only add links there when the plan names them. |
| Sandbox server / client code | `src/mailroom_reloaded/sandbox/` | |
| Sandbox contract JSON schemas | **root** `schemas/*.json` | Reloaded **owns** these (see X-03). Not the same as the package `schemas/`. |
| Python tests | `tests/<subpackage>/test_<module>.py`, mirroring `src/` | Top-level `tests/test_*.py` only for root modules (`cli`, `settings`, `tools`, `watcher`). |
| JS tests | `tests/tui/js/<name>.test.mjs`; sandbox UI: `tests/sandbox/js/` | Run with `node --test`. |
| Test fixtures / fakes | `tests/fixtures/<topic>/`, `tests/fakes/`, per-package `conftest.py` | Generated binary fixtures are built in conftest, not committed. |
| Operator / reference docs | `docs/<UPPER_SNAKE>.md` (existing set: ARCHITECTURE, CONFIGURATION, DEV_SERVER, EVALUATION, JEV, OPERATIONS, RUNBOOK, SANDBOX_CONTENT, SANDBOX_SERVER, TESTING, TUI, `gmail-intake.md`) | Update the doc that owns the topic; add a new doc only for a new topic, and link it from `README.md` Docs list. |
| Specs, reviews, archive | `docs/superpowers/specs/`, `docs/superpowers/reviews/`, `docs/superpowers/archive/` | `plans/` holds **exactly one file**: this plan. |
| UI / browser evidence (screenshots, check output) | `docs/evidence/<YYYY-MM-DD>-<topic>/` | New convention proposed by this plan (decision D8). Never inside `tests/` or `docs/*.md`. |
| Dev / ops scripts | `scripts/` (`*.sh`, `*.py`, `*.mjs`), seed data in `scripts/tui_seed*/` | Thin wrappers only; logic belongs in `src/`. |
| Deployment, compose, Grafana, collector | `deploy/` (`grafana/`, `anchor/`, `llamafile/`) | |
| Content pin | `sandbox/content.lock` only | Written by `mailroom sandbox content bump`, never by hand. |
| Every PR | `CHANGELOG.md` entry under Unreleased | Required (trace-replay stacks 13-15 shipped without one; see R-12). |

### 1.2 `mailroom-sandbox-content`

| What you are adding | Goes in |
| --- | --- |
| Scenario | `scenarios/<Series>/<ID>_<snake_name>.yaml` (`A`-`H`, `S`, `T`; name regex `^[A-HST][0-9]+_[a-z0-9_]+$` once H lands); IDs from `ids/ranges.yaml` only; `scenarios_index.csv` is generated (`tools/validate.py --generate-indexes`) |
| Inbound / reply template | `gen/templates/*.j2`, replies in `gen/templates/replies/`; generation specs `gen/specs/*.yaml` |
| Client / persona data | `clients/*.csv`; `personas/personas.csv`, `personas/behavior/<persona_id>.yaml` |
| Frozen / hand-written emails | `emails/frozen/<series>.jsonl`, `emails/handwritten/*.md` (`emails/` = message bodies) |
| Email infrastructure contracts | `email/` (config: sender pool, overlay, recipient policy). **`email/` is not `emails/`**; a rename was considered and rejected (D9) because paths are baked into bundles and docs. |
| Attachments | `attachments/{synthetic,offtaxonomy,adversarial}/` + `attachments/manifest.csv` |
| Attack-only fixtures | `adversary/` (never compiled into the registry) |
| Protocol / policy | `protocol/` |
| Taxonomy | `taxonomy/strata.csv` is **generated** by `tools/sync_strata.py`; hand-edit only `offtaxonomy.csv` and `migrations/` |
| Schemas | `schemas/` (copy of reloaded's root `schemas/`; see X-03) |
| Tooling / tests | `tools/*.py`, `tools/*.sh`; tests in `tests/` (unittest) |
| Docs | `CONTENT_SPEC.md`, `README.md`, `CHANGELOG.md`, `docs/` (history only; the live plan is this file in reloaded) |
| Generated / never commit | `dist/`, `release/`, `.cache/` |

### 1.3 Hard rules (both repos)

1. One live plan, here. Status updates go in this file's ledger, not in new docs.
2. No loose files at repo root. Root currently carries `DISCUSSION_BOARD.md`
   (reloaded) and `content.json` (content, intentional pin metadata); see D10.
3. Never edit generated files by hand (`scenarios_index.csv`, `strata.csv`, `dist/`, `uv.lock`).
4. Never consume content from a branch tip; only a tagged release via `content.lock`.
5. Mirror `src/` in `tests/`; a PR that adds a module adds its test file.
6. Every PR: `uv run pytest -q`, `uv run ruff check .`, `node --test tests/tui/js/*.test.mjs` (reloaded);
   `bash tools/ci.sh` (content). Paste the result lines in the PR body.
7. Commits end with the attribution trailer the session specifies; no model identifiers in code or docs.

---

## 2. What was merged from what

| Superseded document | Scope | Result |
| --- | --- | --- |
| `2026-10-07-mailroom-reloaded.md` (24 tasks) + design spec | Pipeline, eval, deploy | 22 done, 2 partial (Section 3.1) |
| `2026-10-08-mailroom-tui.md` (8 tasks) + `HANDOFF-tui.md` | `/tui` | 7 done, 1 partial (3.2) |
| `2026-10-09-mailroom-trace-replay.md` (19 tasks) | Replay, ledger, anchor | At audit: 14 done, 3 partial, 2 deferred. 2026-10-10: 19 done (7, 10, 11, 12 landed in #55-#57; 6 via R-13); none open (3.3) |
| `mailroom-sandbox-content/docs/IMPLEMENTATION_PLAN.md` | Content pack v0.1-v1.0 | Phases 1-2 done; 3 open (3.4) |
| `ISSUE-1-status.md` | Tracker draft | Stale; folded into 3.1 |

The design spec (`docs/superpowers/specs/2026-10-07-mailroom-reloaded-design.md`)
stays live as the design reference; it is not a plan.

---

## 3. Ledger: what is done (do not redo)

### 3.1 Core pipeline (original 24 tasks)

DONE and present on `main`: Tasks 1-21 and 23. Verified by file/symbol presence
and 135 of 136 planned test names existing (static only).

| Task | State | Remaining (carried to Section 4) |
| --- | --- | --- |
| 22 Docker topology | PARTIAL | R-04: compose smoke never run; R-05: `mailroom.eval.*` gauges specified, never emitted |
| 24 Conformance suite | PARTIAL | R-06: live `mailroom conformance` run needs a provider |
| 23 Modal deploy | code + tests only | R-07: real deploy/teardown/spend check never run |
| 20 Dataset + runner | done, spec gap | R-08: `bert_manifest_overlap` (`eval/dataset.py:303`) never called by `run_eval` |

Accepted divergences from the original plan (record, do not "fix"): prompts live
in `src/mailroom_reloaded/prompts/`; flow uses `do_extract`/`do_verify`/`do_boss`
labels with a deterministic `_drive` (CrewAI rejected the literal wiring);
`fit_calibration` lives in `eval/train_gate.py`; `LengthFinishReasonError` lives in
`llm/tooling.py`; test `test_fit_refuses_test_split` is `test_fit_refuses_non_train_split`.
PR #5 audit: findings 1-9 resolved in code, 10 addressed but unverified; chromadb
Dependabot alerts open (`uv.lock:368`, R-09).

### 3.2 `/tui`

DONE: Tasks 1-7 (serve, engine, api client, terminal, boot, pipeline + shell
commands). Task 8 PARTIAL: R-10 (nine-point live browser checklist re-run with
committed evidence; still open 2026-10-10, no `docs/evidence/` exists). `feat/tui-brand-theme` is fully merged (PR #13); nothing
stranded. `docs/TUI.md` matches code (22 commands, 6 themes; file map completed by R-11). Out-of-plan
additions that exist and are accepted: `ledger`, `replay`, `jev` commands,
`/ui` replay links (broader than the "one link" constraint; accepted).

### 3.3 Trace replay (19 tasks; branch numbers are stack positions, not task numbers)

DONE: 1-19 (6 via R-13; 7, 10, 11, 12 landed 2026-10-10 via #55-#57). All 12 remaining
`claude/trace-replay-*` branches are 0 commits ahead of `main`; nothing stranded.

| Task | State | Carried to |
| --- | --- | --- |
| 6 OTLP import + `mailroom replay` CLI | DONE (R-13): `obs/replay/otlp_import.py`, `mailroom replay import\|export\|sessions` | R-13 (done) |
| 7 API | DONE (#55): `GET /v1/replay/live` SSE | R-14 (done) |
| 10 Replay command/grid | DONE (#56): `panels.js`/`registerPanel` (grid is `grid.js`, plan said `view.js`) | R-15 (done) |
| 11 Follow-live | DONE (#55): `replay --follow`, `f` key, `replay/live.js` | R-14 (done) |
| 12 Dev harness | DONE (#57): replay seed in `scripts/tui_dev.sh` | R-16 (done) |
| 18 Anchor | DONE in code; Supabase path never run on a live staging project | R-17 |

Recorded deviations: env var is `MAILROOM_TRACE_STORE_PATH` (plan: `MAILROOM_TRACE_STORE`);
LLM calls get their own `mailroom.llm.<role>` span; takeover tests are inside
`terminal.test.mjs`. The plan header's "nothing implemented" line was stale and is retired with the archive move.

### 3.4 Content pack (`mailroom-sandbox-content`)

DONE: phases 1.1-1.7, 2.1-2.5, CD19 (no GitHub Actions; local `tools/ci.sh`).
Verified: `bash tools/ci.sh` exit 0 on `f650cfd`. 88 scenarios (A17 B8 C11 D6 E13 F5 G12 S10 T6);
30 `review`, 57 `draft`, 1 deprecated, **0 frozen**. Smoke set matches reloaded's
`sandbox/fixtures/smoke/manifest.json` (A1, A3, B1, D1, E1, F1).
Open: 1.8 and 2.6 (no tag or release exists, but `content.lock` already pins `v0.5.0`), phase 3.

---

## 4. Remaining work

Phases run in order. Within a phase, IDs are independent unless a `Needs:` is given.
Steps are checkboxes; tick them in this file as part of the PR that completes them.

### Phase 0: establish a trusted baseline

#### R-01: Run the full suite once, on current `main`, with deps (reloaded)
**Why:** every pass count in the repo's docs is unverified (673, 593, 1831, 1876 all differ).
**Where:** a machine with network; no repo changes except recording the numbers in this ledger.
- [x] `cd mailroom-reloaded && git checkout main && git pull`
- [x] `uv sync --extra dev && uv run pytest -q && uv run ruff check . && node --test tests/tui/js/*.test.mjs` (1886 passed / 3 skipped / 2 deselected; ruff clean; 198 node tests)
- [x] If anything fails, open a fix PR first; do not start Phase 1 on a red baseline. (Jev test-isolation fix folded into PR1.)
- [x] Replace the "unverified" line in the Evidence rule above with the real counts and the date.

### Phase 1: land what is already in flight

#### X-01: Release and H-series sequence (both repos). Needs: D2
Content `feat/heldout-h-series` (PR #5) adds scenarios H1-H28 (116 total), 27 templates, changes the name
pattern to `^[A-HST]...`; reloaded PR #44 changes the same pattern and adds `mailroom sandbox conformance --heldout`.
`content.lock` pins `v0.5.0` at `f650cfd`, which is **not published**, so `mailroom sandbox content pull` fails today.
Recommended order (so `v0.5.0` still means `f650cfd`):
- [ ] Content: **K-01 first** (the sha256 changes with the `zstandard` version; only 0.25.0 reproduces the lock). Then on clean `main` run `tools/release.sh --push` to publish `v0.5.0` + bundle; confirm the sha256 equals the lock's `7a32e86e...`.
- [ ] Content: merge PR #5 (H-series); in its own PR bump `content.json` version to `0.6.0`, run `tools/release.sh --push`.
- [x] Reloaded: merge PR #44 (done, `8e8522a`); then `mailroom sandbox content bump --tag v0.6.0` in its own PR (updates `sandbox/content.lock`).
- [ ] Verify: `mailroom sandbox content pull && mailroom sandbox content validate && mailroom sandbox conformance --content smoke` all pass.
- [ ] Content: update the GitHub repo description (still says "83 scenarios").

#### R-02: Merge PR #45 (`fix/jev-integration-issue-14`), closes issue #14. MERGED (`d8e428f`); one verification step still open
Scope (from its body): `eval/dataset.py` bool parsing + derived `retry_expected`/`review_expected`; neutral
operating points in `eval/jev_calibration.py`; `--dataset-repo/--config` flags; opt-in fixtures mirror
`Lucius-Morningstar/mailroom-reloaded-fixtures` (core `v9.2` `1eb5b42c`). Default `ed7576b6` unchanged.
- [x] Re-fetch; confirm it merges cleanly onto current `main` (merge commit `d8e428f` on `main`; the full suite was not re-run after it, see Baseline).
- [ ] Independently verify the mirror's claim of byte-identity with core `v9.2` (hash the `fixtures` config both sides).
- [x] Merge. `DEFAULT_REVISION` is still `ed7576b6` (`eval/dataset.py:43`, D7). Issue #14's closed state is not checkable from the tree; confirm on GitHub.

#### R-03: Decide and resolve the two Correspondent/Boss mailbox PRs. Needs: D1
PR #44 and PR #23 implement near-identical mailbox code (`sandbox/server/mailbox.py`, `ui/mailbox.js`, `tests/sandbox/js/mailbox.test.mjs`).
PR #23 is CONFLICTING (CHANGELOG.md, `agents/judge.py`, `ingest/clerk.py`, `llm/retry.py`), its 3-dot diff touches 61 files despite saying "no pipeline changes", and it still carries the Correspondent-v2 triage (53/88 scenarios pass alone; author calls the LOFO numbers optimistic).
**Status 2026-10-10:** #44 is MERGED (`8e8522a`; `sandbox/server/mailbox.py`, held-out harness and `AGENTS.md` are on `main`). #23 is a duplicate implementation that additionally carries the Correspondent-v2 triage, with code conflicts in `agents/judge.py`, `ingest/clerk.py` and `llm/retry.py`; it is left for the owner (D1). Nothing below is ticked.
Recommended:
- [x] Land #44 (fresh from `main`, mailbox + held-out harness + `AGENTS.md` + `docs/HELD_OUT_SCENARIOS.md`).
- [ ] Re-cut #23's triage v2 as a new PR on top of #44 (rebase, then diff against `main` to separate real changes from formatting; no pipeline files unless justified).
- [ ] Close #23 with a link to the replacement. Port #44's recorded follow-up: make committed Boss decisions authoritative on recovery.

### Phase 2: salvage stranded work

#### R-18: Port valuable unmerged branches (reloaded). DONE via #57 (salvage of the abandoned lucid branch) and earlier test salvage
Each is one small PR from a fresh branch off `main`, never a merge of the old branch.
- [x] `coderabbit/add-pull-request-tests/6bacca95` (landed as `7b4220d` "salvage coderabbit unit-coverage tests" + `be3337a` alignment; commit `451de3f`, +806 test lines across api/eval/ingest/llm/obs/pipeline/cli, applies cleanly): cherry-pick, run suite, PR.
- [x] `coderabbit/add-pull-request-tests/a10e0c34` (`tests/deploy/test_runbook.py` exists, `2894e18`; `4926976`, `tests/deploy/test_runbook.py`): cherry-pick, confirm it passes against current `docs/RUNBOOK.md`, PR.
- [x] `coderabbit/add-coderabbit-skills-fix-bugs/dfe7b81f` (flow bounds/resume fixes `bbd729f`, `d869b9a`, `704b7cd`, `741af2e`, atomic upload publish `a9b8b3c`/`ce6bdba`, `SUPPORTED_EXTENSIONS` in `ingest/clerk.py`; `a33892b`, 28 files, conflicts in Makefile, `pipeline/flow.py`, `tests/api/test_api.py`, `tests/pipeline/test_flow_units.py`, `tests/sandbox/test_contract_schemas.py`): port the **tests first**; for each, check whether the bug still reproduces on `main` (PRs #9/#11/#12 fixed several); only then port the fix.
- [x] `c45b630` docstring commit (docstrings re-applied across `src` in #57, e.g. `309a7c6`; on `claude/mailroom-reloaded-build` and `coderabbit/document-pull-request-functions/c69dc8cd`; 5 files conflict): optional. Re-apply by hand where the docstrings still apply, or drop (D4).

### Phase 3: finish partial plan scope

#### Core pipeline
- **R-04 Compose smoke.** Needs Docker. [ ] `scripts/smoke.sh` -> record `SMOKE OK`, Phoenix `:6006` 200, Grafana health, and `docker compose config -q` per profile (`tests/deploy/test_compose.py`). Evidence to `docs/evidence/<date>-compose-smoke/`.
- **R-05 `mailroom.eval.*` gauges.** [ ] Either emit them from `eval/runner.py` (`obs/metrics.py` namespace `M`) and point `deploy/grafana/dashboards/quality.json` at them, or amend `docs/OPERATIONS.md` to state the dashboard reads app counters. Test in `tests/obs/` and `tests/deploy/test_grafana_links.py`. Owner picks; default recommendation: amend the doc (cheaper, matches shipped behaviour).
- **R-06 Live conformance.** Needs a provider. [ ] `uv run mailroom conformance --provider llamafile|vllm`, commit the card under `docs/evidence/`, summarise pass rates in `docs/EVALUATION.md`.
- **R-07 Modal.** [ ] Deploy, run, `modal app stop mailroom-vllm`, record spend in `deploy/README.md`. Owner credentials required.
- **R-08 Leakage check.** [ ] Call `bert_manifest_overlap` from `run_eval` (or amend spec section 5) + test in `tests/eval/test_dataset_runner.py`.
- **R-09 chromadb alerts.** [ ] Dismiss on GitHub as "vulnerable code not used", or pin/replace if a patched version appears.

#### `/tui`
- **R-10 Live checklist.** PARTLY DONE 2026-10-10 (evidence: `docs/evidence/2026-10-10-tui-live-check/README.md`, screenshots `docs/demo/`). [x] Ran `scripts/tui_replay_check.mjs` (default run, seeded run, token variant: all checks passed) and the nine-point checklist by script against `scripts/tui_dev.sh`; points 1-5, 7, 8, 9 passed. [x] Committed screenshots/output to `docs/evidence/2026-10-10-tui-live-check/` and `docs/demo/`. [ ] Point 6 only partly evidenced: mid-session kill passes, but "reload shows `mailroom closed`" needs a served page with a dead API, so it was simulated by blocking `/v1`; reword the `docs/TUI.md` item. [ ] Walk was headless and scripted, not done by hand on a physical keyboard or in a headed browser; leave Task 8 Step 3 unticked until a person has done that once.
- **R-11 Doc drift.** DONE (#57; `docs/TUI.md` file map lists `ledger.js`, `replay.js`, `deeplink.js`, `replay/`). [x] Add `ledger.js`, `replay.js`, `deeplink.js`, `replay/` to the `docs/TUI.md` file map.

#### Trace replay
- **R-12 CHANGELOG.** DONE (#57; stacks 13, 14, 15 entries are in `CHANGELOG.md` `[Unreleased]`). [x] Add entries for stacks 13 (viewer), 14 (deep link), 15 (dev harness) to `CHANGELOG.md`.
- **R-13 OTLP import + CLI (old Task 6).** DONE (`obs/replay/otlp_import.py` and the `mailroom replay` CLI group; tests `tests/obs/test_replay_otlp.py`, `tests/test_cli.py`). [x] Files: `src/mailroom_reloaded/obs/replay/otlp_import.py`, CLI group `mailroom replay import|export|sessions` in `cli.py`, `deploy/otel-collector.yaml`; tests `tests/obs/test_replay_otlp.py`, `tests/test_cli.py`.
- **R-14 SSE + follow-live (old Tasks 7b, 11).** DONE (#55; `api/app.py` `/replay/live`, `api/tui/replay/live.js`, tests `tests/api/test_replay_routes.py`, `tests/tui/js/replay-live.test.mjs`, `replay-command.test.mjs`). [x] `GET /v1/replay/live` in `api/app.py` (`StreamingResponse`, `text/event-stream`), follow mode in `api/tui/commands/replay.js` + `api/tui/replay/`; tests `tests/api/test_replay_routes.py`, `tests/tui/js/replay-command.test.mjs`.
- **R-15 Panels (old Task 10).** DONE (#56). [x] `api/tui/replay/panels.js` with `registerPanel`; test `tests/tui/js/replay-panels.test.mjs`.
- **R-16 Dev seed (old Task 12).** DONE (#57; `scripts/tui_dev.sh` runs `scripts/tui_seed_replay/seed_replay.py`, six docs: happy, retry, failed, parked, boss, happy). [x] Add a replay eval seed (retry, failure, parked, boss) to `scripts/tui_dev.sh` using `scripts/tui_seed*/`.
- **R-17 Anchor staging run.** OPEN (owner). Owner Supabase project. [ ] Run `deploy/anchor/mailroom_anchor.sql` against staging; verify DDL, grants, trigger and key header per `docs/OPERATIONS.md`; record result there.

#### Observability UI links (new workstream). DONE (#54)

- **R-19 Grafana & Phoenix deep links in the UI.** The Grafana dashboards already link *into* the viewer (Task 4 `replay ↗`/`phoenix ↗`, tested in `tests/deploy/test_grafana_links.py`), but neither `/ui` nor the replay viewer links *out* to Grafana or Phoenix, and the two `/ui` header links hardcode `localhost`. Files: `settings.py`, `api/app.py` (`GET /links`), `api/ui/index.html`, `api/tui/replay/grid.js` + `commands/replay.js`, `docs/OPERATIONS.md`, `docs/TUI.md`, `CHANGELOG.md`.
  - [x] Link config: `MAILROOM_PUBLIC_URL` / `MAILROOM_PHOENIX_URL` / `MAILROOM_GRAFANA_URL` (defaults `http://localhost:8000|6006|3000`) exposed by a public `GET /links`; `/ui` header and per-run links build from it.
  - [x] `/ui` runs table: add `grafana ↗` (`` `${grafana_url}/d/mailroom-quality?var-run_id=<id>` ``) and `phoenix ↗` per run beside `replay ↗`.
  - [x] Replay viewer: the inspector shows the run's `phoenix ↗` / `grafana ↗`; `o` / `g` open them (noopener).
  - [x] Tests: `tests/api/test_api.py` (`/links` + per-run links), `tests/tui/js/replay-command.test.mjs` (viewer links/keys); docs + CHANGELOG.
  - Decisions (assumed unless the owner says otherwise): link-out (not embed); Grafana target dashboard `mailroom-quality`; URLs from env with localhost defaults.

#### Deferred follow-ups from PRs #54-#57

Found in review of the merged work; none blocks it. Each is its own small PR (add an ID when one is picked up).
- [ ] `review_approved` is never cleared after a re-extraction consumes it.
- [ ] A review restore failure leaves the manifest `processing`; the retried resolve then 404s.
- [ ] Follow mode never adds entities that start after the first snapshot.
- [ ] The server's `seen` set and the `MAX_FRAMES=0` (unlimited) stream grow without bound.
- [ ] The 100k-row read cap is not surfaced to the client as an error frame.
- [ ] `followSeek` uses the client clock, not the server's.
- [ ] The footer legend clips `q quit` below about 100 columns.
- [ ] Panel `key` metadata is accepted by `registerPanel` but unused.
- [ ] Reserved panel ids are not rejected by `registerPanel`.
- [ ] Custom panel rows are not sanitised.
- [ ] `phoenix_project` in `GET /links` is read from `os.environ` rather than `settings.py`.

#### Governance (landed with the governance PR; content repo PR #15)

Issue forms in `.github/ISSUE_TEMPLATE/*.yml` (`bug_report`, `feature_request`, `agent_task`, `docs_drift`, `follow_up`, plus `config.yml`), a PR template (`.github/pull_request_template.md`) that ends in an `agent-report` YAML block, and `AGENTS.md` conventions. Verified here: the five forms, `config.yml` and the template's `agent-report` block are in the tree. `AGENTS.md` is not in this tree (it arrives with #44 per R-03, or lives in the content repo), so it is recorded but not verified in `mailroom-reloaded`. Hard rule 6 still applies; the template's validation checklist is the place to paste result lines.

### Phase 4: content pack to v1.0 (`mailroom-sandbox-content`)

- **C-01 Dataset join (old 3.1).** [ ] Where huggingface.co is reachable: `python3 tools/build_attachments.py --hf --counts --select 3`; fills the 54 `rows_unverified` rows in `taxonomy/strata.csv` via the generator, never by hand.
- **C-02 Relation truth (3.2).** Needs C-01. [ ] Verify the dataset's relationship vocabulary against reloaded's 8 `relation_kinds.v1.json`, then populate `relations/relations_truth.csv`.
- **C-03 Frozen emails (3.3).** Needs an OpenRouter key (owner). [ ] Generate into `emails/frozen/`, set provenance, keep `emails/emails_index.csv` generated.
- **C-04 Finalise E, S, T (3.4) and review log (3.5).** [ ] Every attack class covered; review log for promoted evasions in `adversary/` + `protocol/`.
- **C-05 Promotion.** Human step. [ ] `review` -> `frozen` for A13-17, B1-8, C1-11, D1-6 (30 scenarios); H-series stays `draft` until its first single-run (see `docs/HELD_OUT_SCENARIOS.md` in reloaded).
- **C-06 Gen specs.** [ ] 32 scenarios name a gen mode without a `gen_spec` (the 1 validator warning); add `gen/specs/*.yaml`.
- **C-07 v1.0.0 and nightly run (3.6).** Needs C-01..C-05.

### Phase 4b: harden the content pack and its hand-off to reloaded (K-series)

Added 2026-10-09 after a fault-injection and integration audit. Design, evidence and
rejected options: [`../specs/2026-10-09-content-pack-hardening-design.md`](../specs/2026-10-09-content-pack-hardening-design.md).
Every number below was measured on content `f650cfd` / reloaded `df249e2` unless it says "reported".
Order: K-00 first; K-01 gates X-01 (do it before publishing `v0.5.0`); K-02..K-06 in any order, each its own PR; K-08 rides with R-03.
K-01..K-07 are content-repo work (branch from content `main`; `bash tools/ci.sh` must pass; paste the result line in the PR). K-08 is reloaded-side.

#### K-00: Reconcile with work already in flight. Do this first.
**Why:** four things overlap K-series scope and must not be redone or contradicted.
- [x] Content PR #5 (H-series): both CodeRabbit findings landed in `c8336b9` (H6 claim grouping; held-out/freeze wording). #5 and #6 are merged to content `main` (`67b9a3e`). Hazard now live: content `main` carries H1-H28 and fails reloaded `main`'s loader until reloaded #44 lands, and content CI cannot see it (K-06). Content PR #7 (K-series work, branch `claude/upbeat-euler-85ifix`) is open.
- [ ] **The scenario-patch PR (owner reports one is waiting).** At the time of writing no open PR in either repo patches existing scenarios: #5 only adds H1-H28, and reloaded #23 states it makes no content-repo change. When it lands, do **not** redo K-03; verify it against the K-03 decision table (below) and tick K-03 only if every row is covered and the K-03 lint passes.
- [x] Reloaded #44 must merge before any pin to an H-series bundle (K-07 shows why).
- [ ] Re-run `git fetch --all --prune` in both repos; update this block if PR state changed.

#### K-01: Make the bundle sha256 reproducible. Blocks X-01.
**Why (measured):** `tools/build_bundle.py` is documented as "same commit, same sha256", but the digest depends on the `zstandard` version. Same commit `f650cfd`, same tar: zstandard 0.25.0 gives `7a32e86e...` (the value pinned in `sandbox/content.lock`); zstandard 0.23.0 gives `c6dafadb...`. Anyone rebuilding with another version cannot reproduce the pin, and the lock's own comment says the pin is of a *local* build that is not published.
**Where:** content `tools/build_bundle.py`, `tools/release.sh`, new `tools/requirements.txt`, `CONTENT_SPEC.md` section on releases.
- [ ] Pin `zstandard==0.25.0` in `tools/requirements.txt`; `build_bundle.py` always prints the version and, under `--release` (passed by `release.sh` only; unit tests build without it), refuses any other version with a clear message.
- [x] Also write `tar_sha256` (digest of the uncompressed deterministic tar, which does not depend on zstd) to a separate `BUILD_INFO` release asset with the `zstandard` version, so reproducibility can be checked across compressor versions. `SHA256SUMS` stays two lines (an extra line would break `sha256sum -c`). Do **not** change the lock's six fields (the schema in `sandbox/content/lock.py` rejects extras). Done in content `840e4e8` (pinned `zstandard==0.25.0` in `tools/requirements.txt`; `--release` refuses other versions).
- [ ] Test: build twice, same bytes; build with a wrong zstandard version, expect a refusal (unittest with the version string patched).
- [ ] Doc: state that the **published asset's bytes** are the verification authority for a download; rebuilds are an audit.
- [ ] After merge, X-01 publishes `v0.5.0` from `f650cfd` **using the pinned version**; confirm the asset sha256 equals `7a32e86e...` before touching the lock.

#### K-02: Content tooling must never crash or silently accept bad data
**Why (measured):** 28 faults injected into a copy of the repo, `tools/validate.py --strict-coverage` run on each: 21 fail cleanly, **4 crash with a traceback**, **2 corrupt data are accepted**, and **1 oversized scenario is accepted** (informational: no size cap), totaling 28 faults. The harness also includes a valid CRLF control: 29 cases = 21 CLEAN-FAIL + 4 CRASH + 4 MISSED (the two corrupt inputs, oversized scenario and CRLF control):

| Fault | Result today |
| --- | --- |
| UTF-8 BOM on `clients/clients.csv` | CRASH `KeyError: 'client_id'` (header becomes `﻿client_id`) |
| CSV row with too few cells | CRASH `AttributeError: 'NoneType' ... 'endswith'` (`csv.DictReader` yields `None`) |
| NUL bytes in a CSV | CRASH, same `NoneType` path |
| Corrupt YAML in `ids/ranges.yaml` | CRASH (raw `yaml` traceback; the other YAML loads are guarded) |
| CSV row with extra cells | accepted (extra values land under a `None` key) |
| Duplicate primary key in `clients.csv` | accepted |
| 5 MB scenario file | accepted (informational: no size cap) |

**Where:** content `tools/validate.py` (`read_csv` ~line 145, `load_yaml`, ID-range check ~line 1194), new `tests/test_fault_injection.py`; seed harness is `tools/fault_inject.py` (committed on content branch `claude/upbeat-euler-85ifix`).
- [ ] One strict CSV reader used everywhere: `utf-8-sig`, reject NUL, reject rows whose cell count differs from the header, reject duplicate keys for every file whose key is declared in `schemas/content_files.json`. Each failure is a reported ERROR naming file and line.
- [ ] `load_yaml` and the ID-range check report parse errors as ERRORs (no raw traceback anywhere).
- [ ] Outer guard in `main()`: any other unexpected exception becomes `ERROR internal: <type>: <msg>` and exit code 2, never a bare traceback.
- [ ] Optional cap (e.g. 1 MB) on scenario/template files, as a WARN first.
- [ ] Turn `tools/fault_inject.py` into unittest cases (each mutation has an expected outcome; `csv_crlf` is a legitimate accept). Acceptance: 0 CRASH and 0 unexpected MISSED.

#### K-03: Settle the contradictory scenario expectations (the pack-owner list)
**Why:** the Correspondent cannot pass scenarios whose expectations contradict each other, and every unresolved row inflates the "fail" count that reloaded reads as a code problem. Reloaded #23 (`docs/SANDBOX_SERVER.md` on that branch) lists these as bucket 1, "for the pack owner". Two were **verified here**: S1/S2/S3 render the identical template `routine_status_check` with `intent: unrelated` yet expect `fyi` priority `normal`/`normal`/`high`; D6 expects `outbox: []` for `status_request` while G4 and G7 (same intent) expect a draft reply. The rest are **reported by #23, not yet verified here**.
**Rule (D12):** `protocol/delegation_matrix.csv` and `protocol/correspondent_boss_protocol.md` decide the right outcome. Never edit an expectation to match what the Correspondent does.
- [ ] Verify each reported row by reading the YAML: S1-S10 (fyi priority), A9 (deprecated placeholder), T5 and T6 (self-test text labelled `general_question`), C4 (`quarantine_attachments` together with `benign_hard_actions: 0`), E7 and E2/E5 (`payment_fraud` priority critical vs high), A4 vs A5/A12/C5 (reply to first send), D6 vs G4/G7/G8, G9 vs G10 (urgent-deadline ack), F5 vs E13 (out-of-profile clarifying draft). **Status 2026-10-09:** audit done (content `docs/CONTRADICTION_AUDIT.md`); owner decided R1 and S-series low; applied to 14 submission scenarios, G9, H21 and S1-S3, S5-S10. Not yet applied: B5/H5, B4, D3/D4/D6, T6 family, F1/H16/H22-H24 priority; open rulings listed in content `CONTENT_SPEC.md` Appendix A.
- [x] For each, write one row in a decision table (scenario, conflict, matrix row relied on, decision) in `CONTENT_SPEC.md` appendix; edit the scenario; keep `status` unchanged. (first batch; Appendix A)
- [x] Add a validator lint: scenarios sharing the same `template` and `intent` must agree on `outbox` shape and signal priority unless one carries a `contrast:` tag naming the reason. ERROR once the table is applied. (done as `tools/lint_contradictions.py --strict` in `ci.sh`; groups by template set and intent; the `contrast:` tag is not needed yet and would require a schema change)
- [ ] Do not promote anything to `frozen` here (that is C-05, a human step).

#### K-04: Release and hook robustness
**Why:** there is no hosted CI, so `tools/ci.sh` is the only gate and it has two weak points: the strata-drift step fetches from GitHub, and `tools/release.sh` can skip it.
- [x] `ci.sh`: when the drift fetch fails (offline), fail with an explicit message and the exact `--skip-drift` / `MAILROOM_RELOADED=` remedy instead of a git error; never treat a failed fetch as a pass.
- [x] `release.sh`: always run drift (no skip); run K-01's version check; refuse to tag when `dist/registry.yaml` is older than any tracked input.
- [x] `release.sh --push`: print a ready-to-paste recovery line for each partial-failure point (tag pushed but release creation failed is already handled; add "tag exists locally but push failed").
- [x] Document `tools/install-hooks.sh` as required once per clone in `README.md`; add a note that `--no-verify` bypasses it and review (CodeRabbit) is the second gate.

#### K-05: Schema ownership, corrected (replaces the X-03 recommendation)
**Why (measured):** the earlier audit had the direction reversed. Diffing the two copies, reloaded is the stricter one on `gen_spec.v1.json` (extra `allOf` forbidding real brands/URLs/links/phone numbers, and `required: ["forbidden"]`) and on `persona_behavior.v1.json` (`additionalProperties: false` at four levels; content has none). Content only differs by also allowing relation `unknown` in `scenario.v2.json`. All 142 pack files (scenarios, gen specs, persona behaviors) already validate against reloaded's schemas, so adopting them is safe.
- [ ] Content copies reloaded's `schemas/*.json` byte-for-byte; drop `unknown`.
- [ ] Add the schema drift check to `tools/ci.sh` next to strata drift (`MAILROOM_RELOADED`, same pinned-commit fetch).
- [ ] Update X-03 and D3 accordingly (done in this edit).

#### K-06: Content CI must load the pack through the consumer's own loader
**Why (measured):** content branch `feat/heldout-h-series` passes its own `tools/ci.sh` but fails reloaded `main`'s `load_content` (28 schema errors: name `H1_...` does not match `^[A-GST]...`). Content CI cannot see consumer-side contract changes, so the break is found only after pinning. On `main`, the raw checkout and the extracted bundle both load clean (88 scenarios, 14 personas, 40 gen specs, 0 errors).
- [x] `tools/ci.sh` gains a step that, when `MAILROOM_RELOADED` is set, imports `mailroom_reloaded.sandbox.content.loader.load_content` (parent packages stubbed so heavy dependencies are not needed) and asserts 0 errors on the compiled pack. Keep it skippable like drift, and loud when skipped.
- [x] Merge order rule recorded in X-01: reloaded #44 (consumer accepts H) before content #5 is pinned.

#### K-07: Content carried over from earlier phases (no new scope)
C-01 to C-07 above remain the content-completeness list. K-series items do not replace them; they make the pack safe to ship while those finish. One addition to C-06: record the 32 scenarios that lack a `gen_spec` by name in the PR, so the single validator WARN can become an ERROR when it reaches zero.

#### K-08: Reloaded-side hardening that the pack cannot fix
- [x] **Loader reports instead of raising.** `sandbox/content/loader.py` records per-file content read and JSON/YAML parse failures in `ValidationReport.errors` with the file path, including smoke manifest digest reads. `CompatError` still raises. `mailroom sandbox content validate` and the initial `build` load surface the report and exit 1. Regression coverage: `tests/sandbox/test_content_validation.py` and `tests/sandbox/test_content_cli.py`.
- [ ] **Boss decision lifecycle must be recoverable.** PARTLY DONE in #44's review fixes: a decision is recorded in a persisted `deciding` state, retries resume the remaining steps and conflicting retries get 409; a crash between queuing a draft and recording its id can still duplicate one draft. Both CodeRabbit reviews (#23, #44) report that a Boss decision can be marked terminal before its effects (attachment actions, drafting) finish, and that restart does not reconcile mailbox, review state and attachment state. Already recorded as follow-up in #44; when R-03 re-cuts the work, require an idempotent, decision-keyed apply step and startup reconciliation. Also keep hard quarantine distinct from a soft review hold (the same reviews flag that a `legitimate` decision can release a hard-quarantined handoff).
- [ ] Docstring coverage warnings on #23 and #44 (63.9% and 77.9% against 80%) are cosmetic but block the pre-merge check; handle with the R-18 docstring decision (D4).

### Phase 5: cross-repo contract and hygiene

#### X-03: Schema ownership. Needs: D3
Superseded by **K-05** (the direction in the first audit was reversed; see K-05 for the measurement). Kept here only as the pointer.
- [ ] Do K-05. Reloaded's root `schemas/` remains the contract owner (content-plan CD8).

#### X-02: Retire merged and dead branches (destructive; needs owner approval, D4)
_Status 2026-10-10: branch retirement is in progress; no box below is ticked here because branch state is not visible from the tree._
- [ ] Safe (fully merged): reloaded `docs/mailroom-reloaded-design`, `feat/mailroom-reloaded-completion`, `feat/tui-brand-theme`, all 12 `claude/trace-replay-*`.
- [ ] After R-18 is done: `claude/mailroom-reloaded-build`, `coderabbit/*` (all four).
- [ ] After D5: `revert-26-claude/mailroom-trace-replay-plan-5nj1nk` (reverts rev 4 of the old plan; conflicts with main, which built on it; no PR).
- [ ] After R-03: `feat/sandbox-correspondent-tuning`. After X-01 merges: reloaded `feat/heldout-boss-mailbox-agents`, content `feat/heldout-h-series`. After R-02: `fix/jev-integration-issue-14`.
- [ ] Local only: fast-forward reloaded local `main` (16 behind), delete stray local branch in content.

#### X-04: Issue tracker and housekeeping
- [ ] Reloaded issues: #1 (24-task tracker) comment with link to this plan and close; #14 closes via R-02; #18 (ingress simulator, likely done by PR #22) verify and close; #24 (replay viewer plan) close after R-12; #8 (Jev) keep open for calibrated scorer.
- [ ] Content issue #2 (build-out tracker): retarget to Phase 4.
- [ ] `DISCUSSION_BOARD.md`: header still names a merged branch (D10).
- [ ] `HANDOFF` items: execution-method decision is moot (retired with the archive).
- [ ] Verify tracked `gen/` in content is intentional generator output (119 files) and say so in `CONTENT_SPEC.md`.

---

## 5. Decisions needed from the owner

| ID | Question | Recommendation |
| --- | --- | --- |
| D1 | Which mailbox copy is canonical, #44 or #23? | #44; re-cut #23's triage v2 on top |
| D2 | Publish `v0.5.0` at `f650cfd` first, then H as `v0.6.0`? Or skip v0.5.0 and re-pin straight to H? | Publish `v0.5.0` first (the lock already says so) |
| D3 | Schema direction. **Corrected:** reloaded's copies are the stricter ones (K-05), so nothing needs upstreaming. | Content adopts reloaded's `schemas/` byte-for-byte; drop `unknown`; add drift check |
| D4 | Approve deleting the branches in X-02 (and whether to keep the docstring commit) | Yes for merged; drop docstring commit unless wanted |
| D5 | Delete the `revert-26-*` branch? | Yes |
| D6 | Build the deferred replay items (R-13, R-14) now or later? | Later, after Phases 0-2 |
| D7 | Adopt the fixtures mirror as eval default? | No; keep opt-in (`ed7576b6` stays default) |
| D8 | Adopt `docs/evidence/<date>-<topic>/` for screenshots and check output? | Yes |
| D9 | Rename content `email/` vs `emails/`? | No; document the distinction (done in 1.2) |
| D10 | Move `DISCUSSION_BOARD.md` into `docs/superpowers/` (as `WORKLOG.md`) or retire it? | Move and update its header |
| D11 | Bundle verification authority (K-01): the published asset's bytes, with `zstandard` pinned for rebuilds and an extra `tar_sha256` line for audits? Or change the lock to pin the uncompressed tar? | **Confirmed by owner 2026-10-09.** Asset bytes + pinned zstandard + `tar_sha256` line; do not change the lock's six fields (consumer schema is closed) |
| D12 | Who decides a contested scenario expectation (K-03)? | **Confirmed by owner 2026-10-09.** The delegation matrix and Correspondent-Boss protocol, never the Correspondent's behaviour; the owner breaks ties the matrix cannot |
| D13 | Where does the fault-injection suite live (K-02)? | **Confirmed by owner 2026-10-09.** Content `tests/` (unittest), seeded from `tools/fault_inject.py`; reloaded gets only the K-06 loader step |

---

## 6. Definition of done for this plan

- [x] R-01 baseline recorded (2026-10-09 @ `44c8b0f`). [ ] Re-run on current `main` (not done after #45, #54-#57); `main` green in both repos is unconfirmed.
- [ ] `v0.5.0` and `v0.6.0` published; `content.lock` resolves; `mailroom sandbox content pull` works from a clean checkout.
- [x] PR #45 merged (with #54-#57). [x] PR #44 merged (`8e8522a`). [ ] #23 resolved (owner); no open PR without an owner.
- [ ] No unmerged branch holding work not in `main` (X-02 complete).
- [ ] Phase 3 partials closed or explicitly re-deferred with a dated note in this file. 2026-10-10: closed R-11, R-12, R-14, R-15, R-13, R-16, R-19; open R-04..R-10, R-17 (owner).
- [ ] K-00..K-06 done in the content repo: fault-injection suite shows 0 crashes and 0 unexpected accepts; the bundle sha256 rebuilds identically with the pinned `zstandard`; no un-annotated scenario contradiction remains; content CI loads the pack through reloaded's loader.
- [ ] K-08 loader item merged in reloaded; the Boss-decision recovery item is tracked in the re-cut of #23.
- [ ] `docs/superpowers/plans/` still contains only this file.
