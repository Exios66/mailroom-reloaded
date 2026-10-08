# Handoff: browser TUI (`/tui`)

Status as of 2026-10-08: **plan written, no code yet.** Pick up at Task 1.

- Branch: `feat/tui-brand-theme` (from `feat/mailroom-reloaded-completion` @ `3865485`). Not merged; PR #5 is a separate, still-open effort.
- Plan: [plans/2026-10-08-mailroom-tui.md](plans/2026-10-08-mailroom-tui.md) (8 tasks, each ends in a commit).
- Prior build context: [plans/2026-10-07-mailroom-reloaded.md](plans/2026-10-07-mailroom-reloaded.md), tracking issue Exios66/mailroom-reloaded#1.

## Working rules

1. **Use a worktree, not the main checkout.** Another agent edits Jev files in `/Users/luciusjmorningstar/Downloads/mailroom-reloaded` on `feat/mailroom-reloaded-completion`. Switching branches there moves their uncommitted work onto yours. Create your own:
   `git worktree add ../mailroom-reloaded-tui feat/tui-brand-theme` (already exists on the original author's machine).
2. Never edit `agents/jev.py`, `eval/jev_calibration.py`, `settings.py` or `docs/JEV.md` on this branch.
3. Execute subagent-driven with every subagent on `model: "sonnet"` (the owner's standing choice), or inline via `superpowers:executing-plans`. The owner has not yet picked a method for this plan.
4. Baseline before starting: `uv run pytest -q` passes and `uv run ruff check .` is clean on the base commit.

## Where the design comes from

The brand kit is a Claude artifact, not a repo file: **"The Mailroom"** (Design System type), https://claude.ai/artifact/RDJg16TnNR4R1go6phL8pW, version `1791433356-cf55`. Read files with the Artifact tool's `read` action and `path`:

| Need | Path in the artifact |
| --- | --- |
| Terminal tokens (dark, light, hc) | `project/tokens.css` (`--term-*`) |
| Component CSS | `project/components/bundle.css` (terminal section) |
| Component rules | `project/components/Term*/README.md`, `Term*/preview.html` |
| Motion timings | `project/guidelines/40-motion.md` |
| Layout and z-index | `project/guidelines/20-layout.md` |
| Voice | `project/guidelines/70-voice.md` |
| Accessibility, `hc` | `project/guidelines/60-accessibility.md` |
| Stage colours | `project/guidelines/30-pipeline-states.md` |

Only the **Terminal** edition applies. Do not mix console (unprefixed) or `obs-` tokens.

## Facts discovered while planning

- Mailroom-reloaded had **no TUI**; its only browser UI is `api/ui/index.html` (`/ui`). `/tui` is new.
- API surface the TUI uses (`api/app.py`): `GET /health`, `GET/POST /v1/documents`, `GET /v1/documents/{id}`, `GET /v1/audit/{id}`, `POST /v1/review/{id}/resolve`, `GET /v1/runs`, `GET /v1/runs/{id}/cards`. `/ui` and `/health` are public; `/v1` needs `Authorization: Bearer <MAILROOM_API_TOKEN>` when set.
- Local run without Docker: the `mock` provider needs `MOCK_BASE_URL` (`llm/client.py:108`); `deploy/mock_openai.py` is a standalone FastAPI fake that answers correspondence schemas only (so seed fixtures must be correspondence-like).
- `node` is available on the author's machine; JS unit tests skip when it is missing.

## Open decisions (ask the owner)

1. ~~Banner art~~ **Resolved:** the kit's banner is damaged, so `banner.txt` / `banner-compact.txt` are original `MAILROOM` wordmarks (block + box-drawing). Swap them if the owner supplies the original.
2. **Execution method** for the plan (subagent-driven vs inline).
3. Whether the kit's *proposed* light theme should ship (the plan includes it as a selectable theme).

## Definition of done

All eight tasks committed; `uv run pytest -q` and `uv run ruff check .` green; the nine-point live browser checklist in Task 8 passed with screenshots; `docs/TUI.md` written; PR opened against `feat/mailroom-reloaded-completion` (or `main` after PR #5 merges) linking issue #1.
