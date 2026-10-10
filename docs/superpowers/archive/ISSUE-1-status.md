> **SUPERSEDED 2026-10-09** by [the core plan](../plans/2026-10-09-mailroom-core-plan.md). Kept for history only; do not edit or add tasks here.

# Status comment for issue #1 (mailroom-reloaded plan tracker)

Draft, not posted. Date: 2026-10-08. Version: 0.2.0.

## Progress

- Plan `2026-10-07-mailroom-reloaded.md`: 22 of 24 tasks done, Task 22 and Task 24 partial (environment-blocked steps only).
- Plan `2026-10-08-mailroom-tui.md`: 7 of 8 tasks done, Task 8 partial (live browser checklist done in-session, screenshots not committed).
- Per-task tables and deviations: the "Status (2026-10-08)" section at the end of each plan.

## PR history

| PR | Title | State |
| --- | --- | --- |
| #2 | feat: mailroom-reloaded build | merged |
| #3 | Document pipeline function contracts and error behavior | merged |
| #4 | design docs merge | merged |
| #5 | feat: complete mailroom-reloaded pipeline (Tasks 11-24) + Gmail intake, dev server, docs | open |
| #6 | Expand unit coverage for API, CLI, telemetry, dataset integrity, BERT routing | open |
| #9 | fix(review): PR #5 audit findings 1-4 | merged |
| #11 | fix(review): PR #5 audit findings 5 and 9 | merged |
| #12 | fix(flow): PR #5 audit findings 6, 7 and 10 | merged |
| #13 | feat(tui): /tui browser terminal | merged |
| #10 | Feat/tui brand theme | open (superseded by #13) |

## Test evidence (branch `feat/jev-tui-hardening`)

- `uv run pytest -q`: 673 passed, 1 skipped, 2 deselected (live-marked)
- `node --test tests/tui/js/*.test.mjs`: 117 / 117 pass
- `uv run ruff check .`: clean
- `uv lock --check`: consistent at 0.2.0

## Open items

1. **Task 24 Step 4:** the live `mailroom conformance --provider llamafile|vllm` run needs a configured provider. Blocked; no pass rates recorded yet.
2. **Task 22 Step 4 / Task 23:** the compose smoke (`SMOKE OK`) and a real Modal deploy plus spend check were never run.
3. **chromadb Dependabot alerts:** transitive and unused by this project; dismiss as "vulnerable code is not actually used".
4. **Light theme:** ships as a selectable `/tui` theme (labelled proposed by the brand kit), not the default.
5. **Jev extract stage:** unreachable locally because dev extraction confidence never enters the extract medium band; only the classify gate consults Jev (see `docs/JEV.md`).
6. TUI live-browser checklist should be re-run with committed screenshots.
