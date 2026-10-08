# Discussion Board — mailroom-reloaded

Lightweight work log. Each agent appends an entry when it lands a unit: what it
built, the files, the evidence, and the commit SHA. Newest first. No ceremony —
this is a ledger, not a governance board.

**Branch:** `feat/mailroom-reloaded-completion` (from `main` @ `afef7f9`)
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
