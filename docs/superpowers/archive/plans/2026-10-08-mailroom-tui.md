> **SUPERSEDED 2026-10-09** by [the core plan](../../plans/2026-10-09-mailroom-core-plan.md). Kept for history only; do not edit or add tasks here.

# Mailroom Browser TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `/tui`, a browser terminal for mailroom-reloaded in the Mailroom brand kit's *Terminal* edition (amber phosphor CRT, boot sequence, ambient skyline), whose commands drive the existing `/v1` API, and verify it in a real local browser.

**Architecture:** Static ES modules served by FastAPI (no build step, no npm dependency, same stance as `/ui`). A pure `engine.js` (parse, registry, history, completion) is unit-tested with `node --test`; DOM modules render with `textContent` only. Every printed value comes from a live API call; if the API is unreachable the terminal says so and prints nothing canned.

**Tech Stack:** FastAPI + Starlette `StaticFiles` (already installed), vanilla ES modules, JetBrains Mono (Google Fonts, local-mono fallback), `node --test` (skipped when `node` is absent).

**Spec:** There is no separate spec; the brand kit is the design source. Claude artifact "The Mailroom" (Design System, https://claude.ai/artifact/RDJg16TnNR4R1go6phL8pW): `project/README.md`, `project/tokens.css`, `project/components/Term*/README.md`, `project/guidelines/{20-layout,40-motion,60-accessibility,70-voice,80-surfaces,90-open-issues}.md`. Executors re-read these with the Artifact `read` action (`path`), they are not copied into this repo.

## Global Constraints

- Terminal edition only: use `--term-*` tokens, never mix console (unprefixed) or `--obs-*` tokens.
- Font `"JetBrains Mono"` 300–700 from Google Fonts, 14px/1.55 body, 11px status bar, local `ui-monospace` fallback so it works offline.
- Brand dark values exact: `--term-bg-deep #050709`, `--term-bg #0a0d11`, `--term-amber #ffb86c`, `--term-green #4ade80`, `--term-cyan #67e8f9`, `--term-phosphor #a3e635`, `--term-red #ef4444`. Light and `hc` blocks copied from kit `tokens.css`; light is labelled "proposed" in a comment.
- Motion: cursor blink 1.06s `steps(2,end)`; line print 0.16s fade up 3px; CRT power-on 0.55s; banner 0.9s from brightness 3; scanline roll 4px/6s; flicker 7s; error shake 0.3s ±3px; theme change 0.35s flash; man-page type-out ~1.4 ms/char. `prefers-reduced-motion` sets every animation to 0.001ms and spawns no sparks; `hc` removes glows, sparks, scanlines and the CRT overlay.
- Layers: skyline z 1, terminal z 2, status bar z 3, grain z 99, CRT z 100, all ambient `pointer-events:none`. Breakpoint 640px (12px base, only `.essential` status items, skyline 22vh).
- Voice: lower-case, Unix-TTY; hints quote the command (`type 'help' to begin.`); unknown command `<cmd>: command not found — try help`; em-dash status `<state> — <reason>`; middle-dot meta; glyphs limited to `· — ↗ ▸ ● ✓ ✕ ▌`; no emoji, no icon font.
- Corners: `--term-radius-sm` 2px (code, man page), `--term-radius-md` 3px (pre); status dot is the only round shape.
- No new Python or npm dependencies. Do not edit `agents/jev.py`, `eval/jev_calibration.py`, `settings.py` or Jev docs (another agent owns them in the other checkout). Work only in this worktree (`../mailroom-reloaded-tui`, branch `feat/tui-brand-theme`).
- `/ui` stays as is except one `tui ↗` header link.

## Review Focus

- **Hostile API strings** (upload and Gmail filenames such as `<img src=x onerror=alert(1)>.txt`, 10k-char names): rendered via `textContent`, wrapped, never executed. Tests in Tasks 4 and 6.
- **API down / 401 at boot:** boot never prints `[ ok ]` for a failed check; offline shows `mailroom closed — no api connection` and no data; 401 prints `[ !! ] api token required — type 'auth <token>'`. Task 5.
- **Token handling:** `auth <token>` is masked in scrollback and history, stored only in `sessionStorage`, sent only as `Authorization: Bearer`; `auth --clear` removes it. Task 3.
- **Huge or empty results:** `ls` with 0 docs prints `no documents — drop a file in the inbox or run 'upload'`; scrollback capped at 1000 lines; `watch` stops on Ctrl+C and when the tab is hidden. Tasks 4 and 6.
- **Input edge cases:** unbalanced quotes, empty line, Tab with no match, pasted multi-line text (runs the first line, warns), IME composition not submitted early, click anywhere refocuses the hidden input. Tasks 2 and 4.

## File Structure

```
src/mailroom_reloaded/api/tui/
  index.html      shell: status bar, #output, prompt line, ambient layers
  tokens.css      vendored --term-* tokens (dark, light, hc, amber/green/cyan phosphor)
  tui.css         component styles (.terminal .line .prompt-line .man-page .listing table.mr .post ...)
  engine.js       pure: parseLine, createRegistry, createHistory
  api.js          fetch client, token store, ApiError
  terminal.js     DOM: scrollback writer (ctx.out), prompt line, key handling
  boot.js         banner + real boot checks
  ambient.js      skyline, sparks, crt/grain toggles, theme apply
  commands/       pipeline.js (ls inspect audit review resolve runs cards health upload watch auth)
                  shell.js (help man clear neofetch theme crt skyline history)
  banner.txt      kit ASCII wordmark, verbatim
  main.js         wires everything
tests/api/test_tui_serving.py
tests/tui/test_engine_js.py   runs tests/tui/js/*.test.mjs with node
scripts/tui_dev.sh            mock provider + mailroom serve + seed docs
docs/TUI.md
```

---

### Task 1: Serve `/tui` with vendored brand tokens

**Files:** Create `api/tui/index.html`, `api/tui/tokens.css`, `api/tui/tui.css` (empty shell rules for now), `tests/api/test_tui_serving.py`; Modify `api/app.py:71` (add `_TUI_DIR`) and `api/app.py:398-404` (routes).

**Interfaces:**
- Produces: `GET /tui` and `GET /tui/` (HTML), static mount `/tui/assets/*` for every file in `api/tui/`. `/tui` is public like `/ui`; `/v1` stays token-gated. `index.html` links `assets/tokens.css`, `assets/tui.css`, and `<script type="module" src="assets/main.js">`.

- [x] **Step 1: Write failing tests** in `tests/api/test_tui_serving.py` using the `TestClient` fixture pattern from `tests/api/test_api.py`: `test_tui_page_served` (200, `text/html`, body contains `id="output"` and `assets/main.js`), `test_tui_assets_served` (`/tui/assets/tokens.css` 200 and contains `--term-amber: #ffb86c` and `--term-bg-deep: #050709`), `test_tui_asset_traversal_rejected` (`/tui/assets/..%2f..%2fsettings.py` is 404), `test_tui_public_with_token_set` (token configured, `/tui` 200, `/v1/documents` 401), `test_ui_unchanged` (`/ui` still 200).
- [x] **Step 2: Run** `uv run pytest tests/api/test_tui_serving.py -v`. Expected: FAIL (404).
- [x] **Step 3: Implement.** Extract every `--term-*` declaration from the kit's dark `:root`, the `light` block and the `hc` block of `project/tokens.css` into `tokens.css` under selectors `:root`, `:root[data-theme="light"]`, `:root[data-theme="hc"]`; add `:root[data-phosphor="green"]` (maps `--term-amber`/`-bright` to `--term-phosphor`/`-bright`) and `[data-phosphor="cyan"]` likewise; add the shape/spacing tokens (`--term-pad-y/x`, `--term-list-gap`, radii). Header comment names the artifact URL and version `1791433356-cf55`. `index.html` is the skeleton: `header.status-bar`, `main#output.terminal`, `.prompt-line`, `.ambient-skyline`, `.crt-overlay`, `.grain`, plus a `<noscript>` line `tui needs javascript — the runs page works without it: /ui`. Add `StaticFiles(directory=_TUI_DIR)` mounted at `/tui/assets` and the two page routes in `create_app()`.
- [x] **Step 4: Run** the same command. Expected: PASS. Then `uv run pytest tests/api -q` stays green.
- [x] **Step 5: Commit** `feat(tui): serve /tui with vendored terminal brand tokens`.

### Task 2: Pure command engine

**Files:** Create `api/tui/engine.js`, `tests/tui/js/engine.test.mjs`, `tests/tui/test_engine_js.py`.

**Interfaces:**
- Produces (ES exports from `engine.js`):
  - `parseLine(line: string): {cmd: string, args: string[], flags: Record<string, string|true>, error?: string}` — whitespace split, single/double quotes group, `--k v`, `--k=v`, `--k` (true); unbalanced quote returns `error: "unterminated quote"`.
  - `createRegistry(): {register(spec), get(name), names(): string[], complete(line): {matches: string[], ghost: string}}` where `spec = {name, summary, usage, man, run(ctx, args, flags): Promise<void>, complete?(ctx, args): string[]}`; `register` throws on duplicate names.
  - `createHistory(max = 200): {push(line), prev(): string|undefined, next(): string|undefined, reset(), all(): string[]}`; skips empty lines and consecutive duplicates; `push` is never called for masked commands (caller's job).
  - `dispatch(registry, ctx, line): Promise<'ok'|'unknown'|'error'>` — unknown prints `<cmd>: command not found — try help` via `ctx.out.line(text, 'error')`; a throwing `run` prints `<cmd>: <message>` as error and returns `'error'`.
- [x] **Step 1: Write failing node tests** (`node:test`): `parseLine('ls --status parked "a b"')` → cmd `ls`, flags `{status:'parked'}`, args `['a b']`; `parseLine('x "oops')` has `error`; `--k=v` form; history dedupe and `prev`/`next` walk; `complete('he')` → matches `['help']`, ghost `'lp'`; `complete('')` → no ghost; duplicate `register` throws; `dispatch` unknown message exact text; `dispatch` of a throwing command returns `'error'` and prints once.
- [x] **Step 2: Wire pytest.** `tests/tui/test_engine_js.py::test_engine_js_suite` runs `node --test tests/tui/js/*.test.mjs` via `subprocess.run`, asserts returncode 0; `pytest.skip` when `shutil.which("node")` is None. Run it. Expected: FAIL (module missing).
- [x] **Step 3: Implement `engine.js`** (no DOM, no imports).
- [x] **Step 4: Run** `uv run pytest tests/tui -v`. Expected: PASS.
- [x] **Step 5: Commit** `feat(tui): pure command engine with node tests`.

### Task 3: API client and token store

**Files:** Create `api/tui/api.js`, `tests/tui/js/api.test.mjs`.

**Interfaces:**
- Consumes: nothing.
- Produces: `class ApiError extends Error {status: number|null, kind: 'offline'|'unauthorized'|'http'}`; `createApi({fetchImpl = fetch, storage = sessionStorage, base = ''}): {get(path, query?), post(path, body?), upload(file), setToken(t), clearToken(), hasToken(): boolean, health(): Promise<{ok: boolean, status: number|null}>}`. Token lives only in `storage` key `mailroom.tui.token`; all requests send `Authorization: Bearer <t>` when set. Network failure → `ApiError(kind:'offline')`; 401 → `kind:'unauthorized'`; other non-2xx → `kind:'http'` with `detail` from the JSON body.
- [x] **Step 1: Write failing tests** with an injected fake `fetchImpl` and in-memory `storage`: bearer header present only after `setToken`; 401 → `unauthorized`; rejected fetch → `offline`; 404 detail surfaced; `clearToken` removes the key; the token never appears in the request URL; `upload` posts multipart to `/v1/documents`.
- [x] **Step 2: Run** `node --test tests/tui/js/api.test.mjs`. Expected: FAIL.
- [x] **Step 3: Implement `api.js`.**
- [x] **Step 4: Run** `uv run pytest tests/tui -v`. Expected: PASS.
- [x] **Step 5: Commit** `feat(tui): api client with session-scoped bearer token`.

### Task 4: Terminal renderer and prompt line

**Files:** Create `api/tui/terminal.js`, `api/tui/main.js`; fill `api/tui/tui.css` with the Term component rules (read `project/components/bundle.css` terminal section and `TermOutput`, `TermPrompt`, `TermTable`, `TermListing`, `TermPost`, `TermManPage`, `TermStatusBar` READMEs). Tests: `tests/tui/js/terminal.test.mjs` using a tiny DOM stub (`node` has none): export pure helpers from `terminal.js` — `capScrollback(lines: Element[], max=1000)`, `maskCommand(line: string): string`.

**Interfaces:**
- Consumes: `createRegistry`, `createHistory`, `dispatch` (Task 2); `createApi` (Task 3).
- Produces: `createTerminal({root, registry, history, api}): {ctx, run(line), focus()}` where `ctx.out = {line(text, cls?), pre(text), table(headers: string[], rows: string[][]), kv(pairs: [string, string][]), listing(items: {name, kind: 'dir'|'md'|'hidden'|'file'}[]), divider(), man(text, {instant?: boolean}), clear()}`, `ctx.api`, `ctx.registry`, `ctx.signal(): AbortSignal` (aborted by Ctrl+C), `ctx.setStatus(key, value)`. Every method builds nodes with `textContent`/`createElement` only; none accepts HTML.
- Behaviour: hidden real `<input>` over `.input-display` with `.block-cursor` and `.ghost-text`; Enter runs, ↑/↓ history, Tab completes ghost, Ctrl+L clears, Ctrl+C aborts the running command and prints `^C`; echo is `<cwd> $ <line>` in `cmd-echo` (masked via `maskCommand`: `auth <token>` → `auth ••••`, and masked lines are not pushed to history); click anywhere in `#output` refocuses; `compositionstart/end` guard for IME; multi-line paste runs line 1 and prints `warn: pasted 3 lines — ran the first` once.
- [x] **Step 1: Write failing tests:** `capScrollback` keeps the last 1000; `maskCommand('auth s3cret')` is `'auth ••••'` and leaves `ls` untouched; `maskCommand('auth --clear')` is unchanged. Hostile-string test: `renderTable` helper given `<img src=x onerror=alert(1)>` yields a text node, not an element (assert on the stub's `children`/`textContent`).
- [x] **Step 2: Run** `uv run pytest tests/tui -v`. Expected: FAIL.
- [x] **Step 3: Implement** `terminal.js`, `main.js` (wires engine, api, terminal; no commands yet beyond a temporary `echo`), and the CSS.
- [x] **Step 4: Run** `uv run pytest tests/tui tests/api -q`. Expected: PASS. Manually load `/tui` once (Task 8 harness not needed: `uv run mailroom serve --no-watch`) and confirm the prompt accepts typing.
- [x] **Step 5: Commit** `feat(tui): terminal renderer, prompt line and scrollback`.

### Task 5: Boot sequence and banner

**Files:** Create `api/tui/boot.js`, `api/tui/banner.txt`, `tests/tui/js/boot.test.mjs`; Modify `main.js`.

**Interfaces:**
- Consumes: `ctx.out`, `api.health()`, `api.get`.
- Produces: `async function boot(ctx, {reducedMotion: boolean, signal: AbortSignal}): Promise<{state: 'live'|'locked'|'closed'}>`. Sequence: `.title-card pre.banner` (0.9s fade from brightness 3), neofetch line `mailroom@floor — mailroom-reloaded visual engine`, dim line `boot: tty · crt on · theme amber`, then real checks, one `[ ok ]`/`[ !! ]` line each: `api /health` (`/health`), `auth` (`GET /v1/documents?limit=1`), `catalog · N documents` (count from that response), `eval runs · N` (`/v1/runs`). Health fail → `[ !! ] api unreachable`, state `closed`, status bar `mailroom closed — no api connection`, remaining checks skipped. 401 → `[ !! ] api token required — type 'auth <token>'`, state `locked`. Any key or click skips the animation (lines print instantly) but the checks still run. Reduced motion prints instantly. Ends with motd lines and `type 'help' to begin.` in amber.
- [x] **Step 1: Write failing tests** with a fake `ctx`: health down → no line contains `[ ok ]`, state `closed`; 401 → locked line text exact; healthy with 3 docs → `catalog · 3 documents`; aborted signal prints remaining lines without delay.
- [x] **Step 2: Run** `uv run pytest tests/tui -v`. Expected: FAIL.
- [x] **Step 3: Implement.** `banner.txt` (67 cols, 6-row ANSI-Shadow block wordmark `MAILROOM`) and `banner-compact.txt` (22 cols, 3-row box-drawing wordmark) already exist, authored for this repo because the kit's `TermBanner/preview.html` art is escape-damaged and spells no name. Load both; use the compact one when the viewport is under 640px. Both use only box-drawing/block glyphs (verified monospace-safe in JetBrains Mono).
- [x] **Step 4: Run** `uv run pytest tests/tui -v`. Expected: PASS.
- [x] **Step 5: Commit** `feat(tui): real-check boot sequence with banner and closed state`.

### Task 6: Pipeline commands

**Files:** Create `api/tui/commands/pipeline.js`, `tests/tui/js/pipeline.test.mjs`; Modify `main.js` to register them.

**Interfaces:**
- Consumes: `ctx.out`, `ctx.api`, `ctx.signal()`.
- Produces `registerPipeline(registry)` adding:
  - `ls [--status S] [--limit N]` → `GET /v1/documents`; table `doc_id · filename · type · status`; status coloured by the Pipeline-states table (archived `success`, failed `error`, parked/review `warn`, processing `info`); empty → the empty-state line from Review Focus.
  - `inspect <doc_id>` → `GET /v1/documents/{id}`; TermRunStory layout: amber title, kv grid (status, doc_type, confidence), one line per `route_trail` stage (stage in cyan).
  - `audit <doc_id>` → `GET /v1/audit/{id}`; prints entry count and `chain: ok` (success) or `chain: broken at <n>` (error), using the response's `chain` fields.
  - `review` → `GET /v1/documents?status=parked`.
  - `resolve <doc_id> <approve|correct|reject> [--type T] [--subclass S] [--reviewer R]` → `POST /v1/review/{id}/resolve`; `correct` without `--type` fails locally with `resolve: correct needs --type <doc_type>`; echoes the returned status and trail.
  - `runs` → `GET /v1/runs`; `cards <run_id>` → `GET /v1/runs/{id}/cards`, one `kv` per card.
  - `health` → `GET /health`.
  - `upload` → opens a file picker (`<input type=file>`; accepted `.txt .md .pdf .docx .rtf .html .htm`), posts via `api.upload`, prints `queued <file> · <doc_id>`.
  - `watch [--interval 3]` → polls `GET /v1/documents?limit=20` and prints one line per status change or new doc until `ctx.signal()` aborts or `document.hidden`.
  - `auth <token> | --clear` → `api.setToken` / `clearToken`, then re-checks `GET /v1/documents?limit=1` and prints `auth: ok` or `auth: token rejected — 401`.
  Errors: `ApiError.kind` `offline` → `<cmd>: api unreachable — mailroom closed`; `unauthorized` → `<cmd>: 401 — type 'auth <token>'`; 404 → `<cmd>: no such document <id>`.
- [x] **Step 1: Write failing tests** with a fake `ctx.api` and recording `ctx.out`: `ls` renders rows; `ls` with a hostile filename keeps it text; `ls` empty message exact; `inspect` stage lines in order; `resolve correct` without `--type` makes no request; 404 and 401 and offline messages exact; `watch` stops after abort and prints only changes (second poll with no change prints nothing); `auth bad` prints `token rejected`.
- [x] **Step 2: Run** `uv run pytest tests/tui -v`. Expected: FAIL.
- [x] **Step 3: Implement** each command as a registry spec with a `man` page in the NAME/SYNOPSIS/DESCRIPTION layout.
- [x] **Step 4: Run** `uv run pytest tests/tui -v`. Expected: PASS.
- [x] **Step 5: Commit** `feat(tui): pipeline commands over the v1 api`.

### Task 7: Shell commands, ambient layer and themes

**Files:** Create `api/tui/commands/shell.js`, `api/tui/ambient.js`, `tests/tui/js/shell.test.mjs`; Modify `tui.css` (ambient, CRT, grain, status bar), `main.js`.

**Interfaces:**
- Produces `registerShell(registry, {ambient})` adding `help` (lists name + summary from the registry; `help <cmd>` = `man <cmd>`), `man <cmd>` (typed out at ~1.4 ms/char, instant under reduced motion, unknown → `man: no manual entry for <cmd>`), `clear`, `history`, `neofetch` (banner + edition, api base, doc/run counts from live calls), `theme [dark|light|hc|amber|green|cyan]` (no arg lists current; invalid prints usage), `crt on|off`, `skyline on|off`.
- `ambient.js` exports `createAmbient(root, {reducedMotion}): {setCrt(on), setSkyline(on), setTheme(name), state()}`; `buildSkylinePath(rnd: () => number): string` reproducing the kit's `buildSkyline` (1440×120, peaks 30–76px wide and 22–74px tall, overlap 0.55); nine 3px amber sparks (none under reduced motion); grain overlay; `crt off` fades the overlay over 0.4s, `skyline off` over 0.6s; `hc` forces both off. Theme choice persists in `localStorage` key `mailroom.tui.theme` inside try/catch (page works when storage throws).
- [x] **Step 1: Write failing tests:** `buildSkylinePath` with a seeded rnd returns a closed path starting `M0 120` and ending `Z`, deterministic; `theme bogus` prints `theme: usage theme [dark|light|hc|amber|green|cyan]`; `man nope` message exact; `help` lists every registered command once; `theme hc` calls `setCrt(false)` and `setSkyline(false)`; storage throwing does not break `theme`.
- [x] **Step 2: Run** `uv run pytest tests/tui -v`. Expected: FAIL.
- [x] **Step 3: Implement.**
- [x] **Step 4: Run** `uv run pytest tests/tui tests/api -q`. Expected: PASS.
- [x] **Step 5: Commit** `feat(tui): shell commands, CRT/skyline ambience and themes`.

### Task 8: Local browser test harness, cross-link, docs, live verification

**Files:** Create `scripts/tui_dev.sh`, `scripts/tui_seed/` (three small `.txt` fixtures plus one named `<b>hostile</b>.txt`), `docs/TUI.md`; Modify `api/ui/index.html` (one header link `tui ↗` to `/tui`), `README.md` (a `/tui` row and link), `docs/DEV_SERVER.md` (one pointer line), `tests/api/test_api.py` or `test_tui_serving.py` (assert the `/ui` page contains `href="/tui"`).

**Interfaces:**
- Produces: `scripts/tui_dev.sh up|down|status` — runs `uvicorn deploy.mock_openai:app` on 127.0.0.1:8899 (confirm the import path; `deploy/` is not a package, so use `--app-dir deploy mock_openai:app`), exports `MOCK_BASE_URL` per `llm/client.py:108-112` (match the dev compose value's path suffix), sets a temp base dir via the `base_dir` setting (`settings.py:121`) under `./data/tui-dev`, runs `uv run mailroom serve --port 8000` with the embedded watcher, copies `scripts/tui_seed/*` into the inbox, writes pids under `data/tui-dev/`. Loopback only, no token.
- [x] **Step 1: Failing test** that `/ui` links to `/tui`. Run, expect FAIL; add the link; expect PASS.
- [x] **Step 2: Write and shell-check the script** (`bash -n`, `shellcheck` if present). `scripts/tui_dev.sh up` then `curl -fsS localhost:8000/health` returns ok and `GET /v1/documents` lists the seeded files after the watcher drains.
- [ ] **Step 3: Live browser verification** with the built-in browser (`preview_start` url `http://127.0.0.1:8000/tui`). For each check record a screenshot in the scratchpad (not committed) and fix defects before continuing: **PARTIAL:** performed live in-session on 2026-10-08 (see HANDOFF-tui.md and fix commit `4cde721`), but screenshots were not committed and the checklist was not re-run in this audit; the pure-logic parts are covered by `node --test`.
  1. Boot plays: banner fade, four `[ ok ]` lines with real counts, `type 'help' to begin.`; keypress skips the animation.
  2. `help`, `man ls`, unknown command (`flor`) shakes and prints the exact message.
  3. `ls`, `inspect <id>`, `audit <id>` show live data; the hostile filename renders as literal text (check `document.querySelectorAll('#output img').length === 0`).
  4. Tab ghost completion, ↑ history, Ctrl+L, Ctrl+C during `watch`.
  5. `theme green`, `theme cyan`, `theme light`, `theme hc`; reload keeps the choice; `crt off`, `skyline off`.
  6. Kill the server mid-session: next command prints the offline message; reload shows `mailroom closed — no api connection` with no data.
  7. Set `MAILROOM_API_TOKEN`, restart: boot says `api token required`; `auth wrong` rejected; `auth <right>` accepted and the token is absent from scrollback.
  8. Mobile preset (375px): status bar shows only essential items, base 12px, no horizontal page scroll; reduced-motion via `emulate` is unavailable, so assert in `javascript_tool` that `matchMedia` branch code paths run by calling `createAmbient(..., {reducedMotion: true})` and that no spark nodes exist.
  9. Console clean: `read_console_messages` has no errors.
- [x] **Step 4: Write `docs/TUI.md`** (commands table, themes, auth, boot checks, how to run `tui_dev.sh`, the manual checklist above, non-goals) and the README/DEV_SERVER pointers.
- [x] **Step 5: Run** `uv run pytest -q` and `uv run ruff check .`. Expected: all pass, ruff clean. Commit `feat(tui): local browser test harness, cross-links and docs`.

## Non-goals

`mail` compose (the app has no send path), `sound`, a corpus command (not in the API), Gmail intake commands, any change to the `/v1` API, and promoting the kit's *proposed* light scheme beyond a selectable theme.

## Self-review

- Spec coverage: banner, boot, prompt/cursor/ghost, scrollback, man pages, tables/listing/run-story, status bar, ambient (skyline, sparks, CRT, grain), three themes + phosphor swap, reduced-motion and `hc`, voice rules, z-order, breakpoint: each has a task. Unported by design: `mail`, `sound`, `corpus`, `TermPost` (no markdown-rendering command; reintroduce if `cat` of a report is wanted).
- Types: `ctx.out` method names match across Tasks 4–7; `ApiError.kind` values match their use in Task 6.
- Proportion: code appears only as signatures, message strings and test assertions.


## Status (2026-10-08)

Audited against the code on branch `feat/jev-tui-hardening`. Merged to the completion branch via PR #13, then hardened on this branch.

| Task | Status | Note |
| --- | --- | --- |
| 1 Serve `/tui` with brand tokens | done | `tests/api/test_tui_serving.py` (6 tests) |
| 2 Pure command engine | done | `engine.js`, `engine.test.mjs`, pytest wrapper |
| 3 API client and token store | done | |
| 4 Terminal renderer and prompt | done | |
| 5 Boot sequence and banner | done | see deviations |
| 6 Pipeline commands | done | plus a `jev` command added in hardening |
| 7 Shell commands, ambient, themes | done | |
| 8 Harness, cross-link, docs, live check | partial | all files and docs done; Step 3 live checklist done in-session but not re-run or committed as screenshots |

### Evidence

- `node --test tests/tui/js/*.test.mjs`: 117 tests, 117 pass.
- `uv run pytest -q`: 673 passed, 1 skipped, 2 deselected; `uv run ruff check .` clean.
- Files present: `api/tui/{index.html,tokens.css,tui.css,engine.js,api.js,terminal.js,boot.js,ambient.js,main.js,banner.txt,banner-compact.txt,commands/pipeline.js,commands/shell.js}`, `scripts/tui_dev.sh`, `scripts/tui_seed/`, `docs/TUI.md`, `/ui` links to `/tui`.

### Deviations from the plan

- Banner: the kit's art was damaged and spells no name, so `banner.txt` and `banner-compact.txt` are original `MAILROOM` wordmarks, not the kit art "verbatim".
- The hostile seed fixture is `<b>hostile<b>.txt` (a `/` cannot appear in a filename).
- The "do not edit Jev files" constraint applied only to the original parallel checkout. On this branch Jev work was added: `jev` command, gate audit display in `inspect`/`audit`, `scripts/tui_seed_jev/`, `GET /v1/jev`.
- Light theme ships as a selectable theme (labelled proposed), not promoted to default.
- Parked documents are now catalogued, so `review` and `ls --status parked` see them.
- Live browser checks were manual and their screenshots were not committed.

### Incomplete

- Task 8 Step 3 has no committed evidence beyond the HANDOFF record and commit `4cde721`.
- Execution method (subagent vs inline) was never formally decided; the work was done in sessions with Sonnet.
