# main verification — 2026-10-11

Verification of merged `main` (`a80866e`) after the v1 trim (#98, PRs #99/#100/#101): the
offline ingress simulator is off `main` and preserved on `feature/ingress-sandbox`; the core
pipeline and the `/tui` replay viewer are fully operational.

## Environment

- Checkout: `mailroom-reloaded` @ `main` `a80866e`.
- Stack: `scripts/tui_dev.sh up` — mock LLM (`deploy/mock_openai.py`, :8899) + `mailroom serve` (:8000) + embedded watcher; 4 seeded documents.
- Browser: Playwright Chromium 156.0.8078.4 (headless shell), Node 22.

## Gates (on merged `main`)

- `ruff check .` → All checks passed!
- `uv run pytest -q` → 1597 passed, 3 skipped, 3 deselected, 0 failed
- `node --test tests/tui/js/*.test.mjs` → 290 pass / 0 fail
- `node --test tests/tui/js/replay-*.test.mjs` → 96 pass / 0 fail

## Core pipeline (mock provider, end to end)

`GET /v1/documents` → 4 documents, every one `status: archived`, `doc_type: correspondence`:
`08268ac00d4c0d10`, `353244b98227f7b8`, `3da79f63ca29f042`, `c84aa3b74a4d40f4`.

## Browser checks

- `scripts/tui_replay_check.mjs` → **all 11 checks passed**: viewer opens (`role=application`),
  clock advances, space pauses, arrow keys seek, digit jumps, inspector opens, ledger panel
  settles with a chain state, no stray elements, `q` ends the viewer, keyboard handed back, no page errors.
- Full TUI drive against the running stack — all passed:
  - TUI boots; `help` lists `replay` and **no** `inbox` command.
  - `ls` lists the archived documents; `runs` lists run `7e57d0c0ffee`.
  - `ledger` and `audit` respond.
  - `replay run:7e57d0c0ffee` opens the viewer; the `#replay=run:7e57d0c0ffee` deep link opens it too.
  - `/ui` renders the runs/documents page.
  - No page errors across all pages.

## Screenshots

- `tui-replay-viewer.png` — `/tui` replay viewer playing `run:showcase-judge-arbiter`; boot checks
  green (`api /health`, `auth`, `catalog · 4 documents`, `eval runs · 1`); status bar shows `ui ↗`
  only (the `inbox ↗` link is gone).
- `tui-replay-inspector.png` — inspector panel open in the viewer.
- `ui-runs.png` — `/ui`: 4 archived documents, eval run `7e57d0c0ffee` with replay/grafana/phoenix links.
