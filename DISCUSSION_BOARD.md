# Discussion Board — mailroom-reloaded

Lightweight work log. Each agent appends an entry when it lands a unit: what it
built, the files, the evidence, and the commit SHA. Newest first. No ceremony —
this is a ledger, not a governance board.

**Branch:** `feat/mailroom-reloaded-completion` (from `main` @ `ce1c1ff`)
**Plan:** `docs/superpowers/plans/2026-10-09-mailroom-core-plan.md`
**Spec:** `docs/superpowers/specs/2026-10-07-mailroom-reloaded-design.md`

---

## Entry template

```
### [Task NN] <title> — <owner>
- **Status:** in_progress | done | needs_attention
- **Files:** <paths>
- **Evidence:** <exact command(s) + result line>
- **Commit:** <sha> <subject>
- **Notes:** <deviations, seams, follow-ups>
```

---

### [Jev gate + docs] Opt-in Jev probabilistic scorer / route gate landed on PR #5 — orchestrator
- **Status:** done (Task 24 Step 4 live run still env-blocked)
- **Sources (issue #8):** hosted Jev `typesafe/jev-1.13` via OpenRouter Decisions API
  (`OPENROUTER_API_KEY`, 32k ctx) and TypeSafe native `/v1/systemone` (`TYPESAFE_API_KEY`,
  64k ctx); offline `alibiserikbay/JevK5` (4.2B `qwen3_5_text`, read letter logits / 1.22)
  via a local server.
- **Files:** `agents/jev.py`, `eval/jev_calibration.py`, `agents/gate.py`, `cli.py` (`jev`),
  `settings.py`, `config/taxonomy.yaml`, `.env.example`, `docs/JEV.md`,
  README/ARCHITECTURE/CONFIGURATION/EVALUATION.
- **Evidence:** `uv run pytest -q` → 610 passed, 1 skipped, 2 deselected;
  `uv run ruff check .` clean; Jev tests proven no-network (fake transport).
- **Commits:** `6691524` Jev scorer + gate; `007a0de` docs; `6d20939` consume
  `verify_threshold`; `22a2689` adversarial fixes; `32060e8` doc corrections; `3025e2e` `.env`
  support + `scripts/jev_harvest.py`.
- **Calibration (live, 2026-10-08):** `scripts/jev_harvest.py` sampled the train split
  (50/class, 250 docs) and asked Jev the doc-type `choice` question — accuracy **0.952**
  (238/250); `mailroom jev calibrate` fit `data/models/jev_calibration.json`: temperature
  **1.520**, accept **0.777**, verify **0.741**, ECE **0.0271 → 0.0245** (n=250). With
  `MAILROOM_JEV_PROVIDER=openrouter` (from `.env`), `load_gate()` returns a **`JevGate`**.
- **Notes:** Jev is off by default and needs `<base_dir>/models/jev_calibration.json`. `JevGate`
  uses the official three-tier pattern: `<verify` → `human_review`; `verify <= c < accept` →
  `verify` (never downgrading a Jev escalation choice); `>= accept` → the chosen action.
  `JEV_*` now resolves from `.env` like other settings (os.environ wins). Artifact is gitignored
  (domain-specific). **Caveat:** 68/250 states were truncated to 60k chars (OpenRouter
  `max_tokens_exceeded`); a features-based harvest over `_jev_state(GateFeatures)` is the
  faithful follow-up. Shipped calibration does not transfer (issue #8).
- **Features-mode harvest (live, 2026-10-08):** `scripts/jev_harvest.py --mode features` now
  sends the compact production `_jev_state(GateFeatures)` + `_jev_questions()` (never
  truncated); `scripts/jev_export_gate_features.py` exports `eval_docs.gate_features` + GT
  labels. Ran a bounded train eval (125 docs → 91 usable gate-feature rows, Jev route
  agreement 0.923). **Refit is degenerate:** every `retry_expected`/`review_expected` in the
  dataset is `"false"`, so the threshold search plateaus at `accept=1.0` and ECE worsens
  (0.329→0.367) — the labels carry no positive escalation examples. **Restored** the
  docs-mode fit (temp 1.520, accept 0.777, verify 0.741, ECE 0.0245, n=250); the
  features-mode code path is correct and will fit meaningfully once labels are non-degenerate.
- **Also fixed:** `eval/runner._write_doc` now creates parent dirs, so nested Enron filenames
  (`owner/folder/n.`) no longer abort the eval run (regression test added).

---

### [Tasks 21/24 + Dev server + Docs] scorecards, conformance, dev server, docs — subagents + orchestrator
- **Status:** done
- **Files:** `eval/{metrics,cards,vllm_telemetry,cost,conformance}.py`, `cli.py`,
  `tests/eval/*`, `tests/test_cli.py`; `deploy/{Dockerfile.dev,docker-compose.dev.yml}`,
  `scripts/dev.sh`, `scripts/dev_test.sh`, `Makefile`, `docs/DEV_SERVER.md`,
  `tests/deploy/test_dev_compose.py`; `README.md`,
  `docs/{ARCHITECTURE,CONFIGURATION,EVALUATION,OPERATIONS,TESTING}.md`.
- **Evidence:** `uv run pytest -q` → **488 passed, 1 skipped, 2 deselected**;
  `uv run ruff check .` → clean; `docker compose -f deploy/docker-compose.dev.yml config -q`
  → rc 0; `uv run pytest tests/eval/test_conformance.py -v` → 5 passed;
  `uv run pytest tests/deploy -v` → 26 passed.
- **Commits:** `4e78005` conformance + card CLI; `bb5cd43` dev server + live marker;
  `816bebf` docs set. Earlier this session: `39448ef`/`f9e9592` Task 21 KPIs;
  `6d6b9a8` Gmail intake (optional `gmail` extra).
- **Notes:** `mailroom card` and `mailroom conformance` placeholders replaced with
  real commands; pytest `live` marker + `addopts = "-m 'not live'"` added (plan
  Task 1); eval package exports sorted. Dev server = `make dev` (`scripts/dev.sh up`):
  app (reload) + split watcher + OTel/Phoenix/Prometheus/Grafana, mock provider, no GPU.

---

### [Adversarial + test-suite review] PR #5 audit and hardening — reviewers + orchestrator
- **Status:** done (one item environment-blocked)
- **Verdict:** revise — no fabrication; the green suite and ruff claims reproduced exactly.
- **Reviewed:** the full `main...feat/mailroom-reloaded-completion` diff (Tasks 11–24,
  Gmail intake, dev server, docs).
- **Verified independently:** `uv run pytest -q` → 494 passed, 1 skipped, 2 deselected;
  `uv run ruff check .` clean; `docker compose -f deploy/docker-compose.dev.yml config -q`
  rc 0; CrewAI 1.15.25 Flow/Agent/Task usage matches docs.crewai.com; no new mandatory
  deps; dependency fence + `CREWAI_DISABLE_TELEMETRY` intact.
- **Fixed after review:** conformance vacuous pass rates (empty run / zero tool calls now
  render `n/a`, never 1.0); added recorder-error, invariant-failure, empty-role,
  judge-live-seam and CLI-wiring tests; `--strict-markers` + single `live` registration;
  refreshed stale doc line-refs.
- **needs_attention (env):** Task 24 Step 4 — live `mailroom conformance --provider
  llamafile|vllm` was NOT run (no `.env`/provider in this environment). Offline card
  generation is proven; live pass rates are deferred to a configured host.
- **Commits:** `2ad2f79` conformance hardening; `57b3e41` doc-ref refresh; `7d7187c` ruff fix.

---

### [Task 19 + HF audit] API/CLI/UI + ModernBERT & dataset verification — subagent + lucius
- **Status:** done; BERT-1 / BERT-3 / HF-DS-3 fixed by `db75692`
- **Files:** `src/mailroom_reloaded/api/*`, `cli.py`, `tests/api/*`;
  `config/taxonomy.yaml` (`required_fields`), `tests/agents/test_specialists.py`.
- **Evidence:** `uv run pytest tests/api -v` → 7 passed; `uv run pytest -q` → 297 passed, 3 skipped.
- **Commits:** `6d7f2cc` /v1 API, runs UI and CLI; `d76b12a` dataset-derived `required_fields`.
- **HF verification (lucius):**
  - Model `Lucius-Morningstar/mailroom-modernbert-classifier` @ `ac8948c0`
    (tag `m9a-local-20260927-014429`); 8192 context; 5 trainable doc classes.
  - Dataset `mailroom-dataset` @ `ed7576b6`; training/eval = `mailroom-modernbert-training`
    `documents` (2680/299/**323**); test split overlaps training `content_sha256` 323/323.
  - **F1 fixed:** `required_fields` derived from train-split GT presence ≥ 0.8.
  - **BERT-1 (RESOLVED by `db75692`):** `ingest/bert.py` now prefers
    `classify_document_default(text, filename=...)` (see `ingest/bert.py:66-69`).
  - **BERT-3 (RESOLVED by `db75692`):** `no_model` / `bundle_missing` markers route
    through `classify_document_default`.
  - **HF-DS-3 (RESOLVED by `db75692`):** `eval/dataset.py:198-259` reads the blind-row
    `metadata` blob and the `gt_fields` JSON; `content_sha256` is read/verified from
    `ground_truth`.
  - **§5 gap (still open):** the BERT-manifest `content_sha256` overlap check remains a
    helper (`eval/dataset.py:302-311`), not called by the runner.

---

### [Tasks 17, 20 + adversarial review] Watcher/review, eval runner, docstring pass — subagents
- **Status:** done
- **Files:** `src/mailroom_reloaded/watcher.py`, `review.py`, `eval/dataset.py`,
  `eval/runner.py`, `tests/test_watcher_review.py`, `tests/eval/*`;
  docstring pass across `agents/*`, `pipeline/*`, `eval/train_gate.py`.
- **Evidence:** `uv run pytest tests/test_watcher_review.py -v` → 5 passed;
  `uv run pytest tests/eval -v` → 7 passed; `uv run pytest -q` → 287 passed, 3 skipped;
  `interrogate` in-scope → 100% (was 36.5%).
- **Commit:** `8cf6d3d` Reloaded unit tests
- **Notes:** adversarial reviewer verdict "revise": no fabricated work, no fictional
  passes; F1 is the only substantive gap. F2 (length-capped usage undercount),
  F3 (audit event named `node_failed` not `ingest_failed`), F4 (cooperative deadline
  cannot preempt a hung node), F5 (weak `test_report_no_llm` seam) noted, non-blocking.

---

### [Tasks 13, 15, 16] Gate training + pipeline spine — subagents + CodeRabbit
- **Status:** done
- **Files:** `src/mailroom_reloaded/eval/train_gate.py`,
  `pipeline/{state,guards,report,archivist,flow}.py`, `tests/agents/test_gate.py`,
  `tests/pipeline/*`.
- **Evidence:** `uv run pytest tests/pipeline -v` → 13 passed;
  `uv run pytest tests/agents/test_gate.py -v` → 61 passed;
  `uv run pytest -q` → 275 passed, 3 skipped.
- **Commits:** `6ad17ec` MailroomFlow with gate routing, guards, report and archive;
  CodeRabbit autofixes `f1707f3` (train-only splits), `fd0b53e` (docstrings).
- **Notes:** CrewAI `@listen("extract")` on method `extract` is rejected by
  crewai 1.15.25 (self-loop) — route labels renamed to `do_extract`/`do_verify`/
  `do_boss`; `run_document` owns deterministic control flow.

---

### [Board] Discussion board created — orchestrator
- **Status:** done
- **Files:** `DISCUSSION_BOARD.md`
- **Commit:** `545922bc5ae6cb7eb960d800b97e423b750ef686` docs: discussion board for agent work log and commit SHAs

---

### [Tasks 11, 12, 14] Sorter, specialists, CrewAI judge/arbiter/boss — implementer subagents
- **Status:** done
- **Files:**
  - `src/mailroom_reloaded/agents/sorter.py` (Task 11)
  - `src/mailroom_reloaded/agents/specialists.py` (Task 12)
  - `src/mailroom_reloaded/agents/judge.py`, `arbiter.py`, `boss.py` (Task 14)
  - `src/mailroom_reloaded/__init__.py` (CREWAI_DISABLE_TELEMETRY)
  - `tests/agents/test_sorter.py` (7), `tests/agents/test_specialists.py` (11),
    `tests/agents/test_crew_agents.py` (8 + 1 live), `tests/agents/conftest.py`,
    `tests/helpers.py`
- **Evidence:**
  - `uv run pytest tests/agents/test_sorter.py tests/agents/test_specialists.py -v` → `18 passed`
  - `uv run pytest tests/agents/test_crew_agents.py -v -m "not live"` → passes (in full suite)
  - `uv run pytest -q` → `244 passed, 3 skipped`
- **Commit:** `ce1c1ff` CrewAi Enhancements
- **Notes:** `extract(..., cond=None)` additive kwarg for per-call dagger mode;
  `tools=None` reads `taxonomy.raw["agents"][<specialist>].get("tools", True)`;
  frozen-merger `prepare_input` returns `[head, tail]` (joined in `extract`).
  Calibration file format: nested `{provider: {model: {doc_type: T}}}`.

---

### [Tasks 1–10, 13(runtime), 22, 23] Foundation — prior build (Claude)
- **Status:** done (inherited)
- **Files:** settings/taxonomy, scoring subset, prompt lock, schemas, bins/audit/catalog,
  llm client/retry/tooling, tools, ingest (clerk/pdf/vision/bert), route gate runtime,
  deploy (compose, OTel collector, Grafana, Prometheus, Modal).
- **Evidence:** `uv run pytest -q` → baseline `217 passed, 2 skipped`
- **Commit:** `afef7f9` (merge of PR #4) and its ancestors
- **CodeRabbit:** 3 review comments fixed in `1f44201` (compose `:?` token,
  `field_is_ambiguous` type bands, bin filename-collision guard). Docstring PR #3
  (`921e33f`) was orphaned by the PR #4 rebuild — re-integration pending.
