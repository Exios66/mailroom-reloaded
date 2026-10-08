# Discussion Board — mailroom-reloaded

Lightweight work log. Each agent appends an entry when it lands a unit: what it
built, the files, the evidence, and the commit SHA. Newest first. No ceremony —
this is a ledger, not a governance board.

**Branch:** `feat/mailroom-reloaded-completion` (from `main` @ `ce1c1ff`)
**Plan:** `docs/superpowers/plans/2026-10-07-mailroom-reloaded.md`
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

### [F1] `required_fields` taxonomy block missing — lucius (HF dataset)
- **Status:** needs_attention / in_progress
- **Finding:** `extraction_confidence` reads `taxonomy.raw["required_fields"][doc_type]`
  (`agents/specialists.py`) but `config/taxonomy.yaml` has no such block, so the
  coverage denominator silently falls back to all schema fields. Spec §6 requires
  the per-class lists derived from train-split GT presence ≥ 0.8.
- **Owner:** `lucius` — derive from `Lucius-Morningstar/mailroom-dataset` @ `ed7576b6`
  (train split, `ground_truth` config); do not fabricate values.
- **Commit:** <pending>

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
