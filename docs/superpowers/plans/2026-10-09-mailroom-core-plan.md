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

**As of:** 2026-10-09. A parallel agent is still pushing. Before acting on any
row marked `[PR]`, run `git fetch --all --prune` in both repos and re-check
the PR state; the audit tables in Section 4 will go stale within hours.

| Repo | GitHub | `origin/main` at audit | Open PRs at audit |
| --- | --- | --- | --- |
| mailroom-reloaded | `Exios66/mailroom-reloaded` | `df249e2` | #23 (`feat/sandbox-correspondent-tuning`, head `64b8400`), #44 (`feat/heldout-boss-mailbox-agents`, `021ecf1`), #45 (`fix/jev-integration-issue-14`, `75da8a1`) |
| mailroom-sandbox-content | `Exios66/mailroom-sandbox-content` | `f650cfd` | #5 (`feat/heldout-h-series`, `27acdba`) |

**Evidence rule.** A status below is **verified** only where it says so. The
audit was static (files, symbols, commits, PR state). **No Python test run was
possible** (no deps, offline). The only executed checks: `node --test` on
`origin/main` (198/198 pass), and the content repo's `bash tools/ci.sh` (exit 0;
88 scenarios, 0 errors, 118 unit tests OK). Test counts quoted from PR bodies
(#44: 1876 passed, 1 env-dependent failure; #45: 1831 passed) are the authors'
own and unverified, and they differ because the branches differ.

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
| Tooling / tests | `tools/*.py|sh`; tests in `tests/` (unittest) |
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
| `2026-10-09-mailroom-trace-replay.md` (19 tasks) | Replay, ledger, anchor | 14 done, 3 partial, 2 deferred (3.3) |
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
committed evidence). `feat/tui-brand-theme` is fully merged (PR #13); nothing
stranded. `docs/TUI.md` matches code (22 commands, 6 themes). Out-of-plan
additions that exist and are accepted: `ledger`, `replay`, `jev` commands,
`/ui` replay links (broader than the "one link" constraint; accepted).

### 3.3 Trace replay (19 tasks; branch numbers are stack positions, not task numbers)

DONE: 1, 2, 3, 4, 5, 8, 9, 13, 14, 15, 16, 17, 18, 19. All 12 remaining
`claude/trace-replay-*` branches are 0 commits ahead of `main`; nothing stranded.

| Task | State | Carried to |
| --- | --- | --- |
| 6 OTLP import + `mailroom replay` CLI | NOT STARTED (deferred by plan phasing) | R-13 |
| 7 API | PARTIAL: REST done, `GET /v1/replay/live` SSE absent | R-14 |
| 10 Replay command/grid | PARTIAL: `panels.js`/`registerPanel` absent (grid is `grid.js`, plan said `view.js`) | R-15 |
| 11 Follow-live | NOT STARTED (needs R-14) | R-14 |
| 12 Dev harness | PARTIAL: no replay seed in `scripts/tui_dev.sh` | R-16 |
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
- [ ] `cd mailroom-reloaded && git checkout main && git pull`
- [ ] `uv sync --extra dev && uv run pytest -q && uv run ruff check . && node --test tests/tui/js/*.test.mjs`
- [ ] If anything fails, open a fix PR first; do not start Phase 1 on a red baseline.
- [ ] Replace the "unverified" line in the Evidence rule above with the real counts and the date.

### Phase 1: land what is already in flight

#### X-01: Release and H-series sequence (both repos). Needs: D2
Content `feat/heldout-h-series` (PR #5) adds scenarios H1-H28 (116 total), 27 templates, changes the name
pattern to `^[A-HST]...`; reloaded PR #44 changes the same pattern and adds `mailroom sandbox conformance --heldout`.
`content.lock` pins `v0.5.0` at `f650cfd`, which is **not published**, so `mailroom sandbox content pull` fails today.
Recommended order (so `v0.5.0` still means `f650cfd`):
- [ ] Content: on clean `main` run `tools/release.sh --push` to publish `v0.5.0` + bundle; confirm the sha256 equals the lock's `7a32e86e...`.
- [ ] Content: merge PR #5 (H-series); in its own PR bump `content.json` version to `0.6.0`, run `tools/release.sh --push`.
- [ ] Reloaded: merge PR #44; then `mailroom sandbox content bump --tag v0.6.0` in its own PR (updates `sandbox/content.lock`).
- [ ] Verify: `mailroom sandbox content pull && mailroom sandbox content validate && mailroom sandbox conformance --content smoke` all pass.
- [ ] Content: update the GitHub repo description (still says "83 scenarios").

#### R-02: Merge PR #45 (`fix/jev-integration-issue-14`), closes issue #14
Scope (from its body): `eval/dataset.py` bool parsing + derived `retry_expected`/`review_expected`; neutral
operating points in `eval/jev_calibration.py`; `--dataset-repo/--config` flags; opt-in fixtures mirror
`Lucius-Morningstar/mailroom-reloaded-fixtures` (core `v9.2` `1eb5b42c`). Default `ed7576b6` unchanged.
- [ ] Re-fetch; confirm it merges cleanly onto current `main` and R-01 stays green.
- [ ] Independently verify the mirror's claim of byte-identity with core `v9.2` (hash the `fixtures` config both sides).
- [ ] Merge. Confirm issue #14 closes. Do **not** change the default `DEFAULT_REVISION` (D7).

#### R-03: Decide and resolve the two Correspondent/Boss mailbox PRs. Needs: D1
PR #44 and PR #23 implement near-identical mailbox code (`sandbox/server/mailbox.py`, `ui/mailbox.js`, `tests/sandbox/js/mailbox.test.mjs`).
PR #23 is CONFLICTING (CHANGELOG.md, `agents/judge.py`, `ingest/clerk.py`, `llm/retry.py`), its 3-dot diff touches 61 files despite saying "no pipeline changes", and it still carries the Correspondent-v2 triage (53/88 scenarios pass alone; author calls the LOFO numbers optimistic).
Recommended:
- [ ] Land #44 (fresh from `main`, mailbox + held-out harness + `AGENTS.md` + `docs/HELD_OUT_SCENARIOS.md`).
- [ ] Re-cut #23's triage v2 as a new PR on top of #44 (rebase, then diff against `main` to separate real changes from formatting; no pipeline files unless justified).
- [ ] Close #23 with a link to the replacement. Port #44's recorded follow-up: make committed Boss decisions authoritative on recovery.

### Phase 2: salvage stranded work

#### R-18: Port valuable unmerged branches (reloaded)
Each is one small PR from a fresh branch off `main`, never a merge of the old branch.
- [ ] `coderabbit/add-pull-request-tests/6bacca95` (commit `451de3f`, +806 test lines across api/eval/ingest/llm/obs/pipeline/cli, applies cleanly): cherry-pick, run suite, PR.
- [ ] `coderabbit/add-pull-request-tests/a10e0c34` (`4926976`, `tests/deploy/test_runbook.py`): cherry-pick, confirm it passes against current `docs/RUNBOOK.md`, PR.
- [ ] `coderabbit/add-coderabbit-skills-fix-bugs/dfe7b81f` (`a33892b`, 28 files, conflicts in Makefile, `pipeline/flow.py`, `tests/api/test_api.py`, `tests/pipeline/test_flow_units.py`, `tests/sandbox/test_contract_schemas.py`): port the **tests first**; for each, check whether the bug still reproduces on `main` (PRs #9/#11/#12 fixed several); only then port the fix.
- [ ] `c45b630` docstring commit (on `claude/mailroom-reloaded-build` and `coderabbit/document-pull-request-functions/c69dc8cd`; 5 files conflict): optional. Re-apply by hand where the docstrings still apply, or drop (D4).

### Phase 3: finish partial plan scope

#### Core pipeline
- **R-04 Compose smoke.** Needs Docker. [ ] `scripts/smoke.sh` -> record `SMOKE OK`, Phoenix `:6006` 200, Grafana health, and `docker compose config -q` per profile (`tests/deploy/test_compose.py`). Evidence to `docs/evidence/<date>-compose-smoke/`.
- **R-05 `mailroom.eval.*` gauges.** [ ] Either emit them from `eval/runner.py` (`obs/metrics.py` namespace `M`) and point `deploy/grafana/dashboards/quality.json` at them, or amend `docs/OPERATIONS.md` to state the dashboard reads app counters. Test in `tests/obs/` and `tests/deploy/test_grafana_links.py`. Owner picks; default recommendation: amend the doc (cheaper, matches shipped behaviour).
- **R-06 Live conformance.** Needs a provider. [ ] `uv run mailroom conformance --provider llamafile|vllm`, commit the card under `docs/evidence/`, summarise pass rates in `docs/EVALUATION.md`.
- **R-07 Modal.** [ ] Deploy, run, `modal app stop mailroom-vllm`, record spend in `deploy/README.md`. Owner credentials required.
- **R-08 Leakage check.** [ ] Call `bert_manifest_overlap` from `run_eval` (or amend spec section 5) + test in `tests/eval/test_dataset_runner.py`.
- **R-09 chromadb alerts.** [ ] Dismiss on GitHub as "vulnerable code not used", or pin/replace if a patched version appears.

#### `/tui`
- **R-10 Live checklist.** [ ] Run `scripts/tui_replay_check.mjs` and the nine-point checklist in `docs/TUI.md` against `scripts/tui_dev.sh`; commit screenshots/output to `docs/evidence/<date>-tui-live-check/`; tick Task 8 Step 3 (retire it from this plan).
- **R-11 Doc drift.** [ ] Add `ledger.js`, `replay.js`, `deeplink.js`, `replay/` to the `docs/TUI.md` file map.

#### Trace replay
- **R-12 CHANGELOG.** [ ] Add entries for stacks 13 (viewer), 14 (deep link), 15 (dev harness) to `CHANGELOG.md`.
- **R-13 OTLP import + CLI (old Task 6).** Deferred. Build only after R-01, R-10 are green. Files: `src/mailroom_reloaded/obs/replay/otlp_import.py`, CLI group `mailroom replay import|export|sessions` in `cli.py`, `deploy/otel-collector.yaml`; tests `tests/obs/test_replay_otlp.py`, `tests/test_cli.py`.
- **R-14 SSE + follow-live (old Tasks 7b, 11).** Deferred. `GET /v1/replay/live` in `api/app.py` (`StreamingResponse`, `text/event-stream`), follow mode in `api/tui/commands/replay.js` + `api/tui/replay/`; tests `tests/api/test_replay_routes.py`, `tests/tui/js/replay-command.test.mjs`.
- **R-15 Panels (old Task 10).** [ ] `api/tui/replay/panels.js` with `registerPanel`; test `tests/tui/js/replay-panels.test.mjs`.
- **R-16 Dev seed (old Task 12).** [ ] Add a replay eval seed (retry, failure, parked, boss) to `scripts/tui_dev.sh` using `scripts/tui_seed*/`.
- **R-17 Anchor staging run.** Owner Supabase project. [ ] Run `deploy/anchor/mailroom_anchor.sql` against staging; verify DDL, grants, trigger and key header per `docs/OPERATIONS.md`; record result there.

### Phase 4: content pack to v1.0 (`mailroom-sandbox-content`)

- **C-01 Dataset join (old 3.1).** [ ] Where huggingface.co is reachable: `python3 tools/build_attachments.py --hf --counts --select 3`; fills the 54 `rows_unverified` rows in `taxonomy/strata.csv` via the generator, never by hand.
- **C-02 Relation truth (3.2).** Needs C-01. [ ] Verify the dataset's relationship vocabulary against reloaded's 8 `relation_kinds.v1.json`, then populate `relations/relations_truth.csv`.
- **C-03 Frozen emails (3.3).** Needs an OpenRouter key (owner). [ ] Generate into `emails/frozen/`, set provenance, keep `emails/emails_index.csv` generated.
- **C-04 Finalise E, S, T (3.4) and review log (3.5).** [ ] Every attack class covered; review log for promoted evasions in `adversary/` + `protocol/`.
- **C-05 Promotion.** Human step. [ ] `review` -> `frozen` for A13-17, B1-8, C1-11, D1-6 (30 scenarios); H-series stays `draft` until its first single-run (see `docs/HELD_OUT_SCENARIOS.md` in reloaded).
- **C-06 Gen specs.** [ ] 32 scenarios name a gen mode without a `gen_spec` (the 1 validator warning); add `gen/specs/*.yaml`.
- **C-07 v1.0.0 and nightly run (3.6).** Needs C-01..C-05.

### Phase 5: cross-repo contract and hygiene

#### X-03: Schema ownership. Needs: D3
Reloaded's root `schemas/` is the contract owner (content-plan CD8). Today, three content schemas differ from reloaded:
- `scenario.v2.json`: content allows relation `unknown`; reloaded rejects it (latent; no scenario uses it).
- `gen_spec.v1.json`, `persona_behavior.v1.json`: content is **stricter** (`additionalProperties: false`, required `forbidden`).
- [ ] Decide direction (D3). Recommended: upstream the stricter content versions into reloaded, remove `unknown` from content, then make content copy reloaded's files byte-for-byte.
- [ ] Add a drift check to `tools/ci.sh` (same mechanism as the existing strata drift using `MAILROOM_RELOADED`) so this cannot recur.

#### X-02: Retire merged and dead branches (destructive; needs owner approval, D4)
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
| D3 | Schema direction: upstream stricter content schemas to reloaded, or loosen content? | Upstream the stricter ones; drop `unknown` |
| D4 | Approve deleting the branches in X-02 (and whether to keep the docstring commit) | Yes for merged; drop docstring commit unless wanted |
| D5 | Delete the `revert-26-*` branch? | Yes |
| D6 | Build the deferred replay items (R-13, R-14) now or later? | Later, after Phases 0-2 |
| D7 | Adopt the fixtures mirror as eval default? | No; keep opt-in (`ed7576b6` stays default) |
| D8 | Adopt `docs/evidence/<date>-<topic>/` for screenshots and check output? | Yes |
| D9 | Rename content `email/` vs `emails/`? | No; document the distinction (done in 1.2) |
| D10 | Move `DISCUSSION_BOARD.md` into `docs/superpowers/` (as `WORKLOG.md`) or retire it? | Move and update its header |

---

## 6. Definition of done for this plan

- [ ] R-01 baseline recorded; `main` green in both repos.
- [ ] `v0.5.0` and `v0.6.0` published; `content.lock` resolves; `mailroom sandbox content pull` works from a clean checkout.
- [ ] PR #44, #45 merged; #23 resolved; no open PR without an owner.
- [ ] No unmerged branch holding work not in `main` (X-02 complete).
- [ ] Phase 3 partials closed or explicitly re-deferred with a dated note in this file.
- [ ] `docs/superpowers/plans/` still contains only this file.
