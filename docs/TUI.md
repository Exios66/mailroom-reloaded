# `/tui` browser terminal

`/tui` is a browser terminal for mailroom-reloaded in the Mailroom brand kit's
Terminal edition (amber phosphor CRT, boot sequence, ambient skyline). It is
static ES modules served by FastAPI, with no build step and no npm dependency.
Every printed value comes from a live `/v1` call; if the API is unreachable the
terminal says so and prints nothing canned. A command that fails prints the reason
(the API's error body when there is one, otherwise `api unreachable`, `401 — type
'auth <token>'`, and so on); an unexpected failure inside the shell prints
`internal error: <message>` and a throwing replay-view key handler prints
`viewer error: <message>`, so nothing fails silently. `/tui` and its assets are public
like `/ui`; `/v1` stays token-gated when `MAILROOM_API_TOKEN` is set.

## `/ui` (runs console)

`/ui` is the read-only runs console: documents (with a status filter), the review
queue, eval runs with `replay ↗` / `grafana ↗` / `phoenix ↗` links, a document
detail pane and an audit-verify box. It is the same Terminal edition as `/tui`: the
page links the vendored `/tui/assets/tokens.css` for the palette and adds only
`/ui/assets/ui.css` for layout, so both screens share one palette, the `light`/`hc`
themes and the phosphor variants; headings, hairlines, 2–3 px radii, tables and
status pills use the same `--term-*` tokens as `tui.css`. Its header is a status bar
matching `/tui`'s, with the `tui ↗` sister link mirroring `/tui`'s `ui ↗` and the
API-token field + `refresh` on the right. The HTML and its assets stay public while
`/v1` is token-gated; the page renders only `textContent` (no `innerHTML`).

## Commands

| Command | Usage | What it does |
| --- | --- | --- |
| `help` | `help [command]` | Lists commands, or shows one manual page. |
| `man` | `man <command>` | Types out a command's manual page. |
| `clear` | `clear` | Clears the scrollback (Ctrl+L does the same). |
| `history` | `history` | Lists commands entered this session; token lines are never kept. |
| `neofetch` | `neofetch` | Prints a system summary. |
| `theme` | `theme [dark\|light\|hc\|amber\|green\|cyan]` | Shows or sets the theme; remembered across reloads. |
| `crt` | `crt on\|off` | Toggles the CRT overlay. |
| `skyline` | `skyline on\|off` | Toggles the skyline. |
| `ls` | `ls [--status S] [--limit N]` | Lists documents. |
| `inspect` | `inspect <doc_id>` | Shows one document. |
| `audit` | `audit <doc_id>` | Verifies the audit chain. |
| `jev` | `jev` | Shows the Jev gate: provider, model, gate in use, calibration thresholds. |
| `review` | `review` | Lists parked documents. |
| `resolve` | `resolve <doc_id> <approve\|correct\|reject> [--type T] [--subclass S] [--reviewer R]` | Dispositions a parked document. |
| `runs` | `runs [pin <run_id> \| unpin <run_id> \| keep [set <pinned\|all\|recent:N>]]` | Lists eval runs; `pin`/`unpin` protect a run's spans from pruning, `keep` shows the retention policy and `keep set` changes it. |
| `ledger` | `ledger [--run ID] [--kind K] [--limit N] \| head \| verify [run_id]` | Lists archive ledger entries (newest first), shows the head, or re-verifies the hash chain. |
| `replay` | `replay [--limit N] \| replay <run_id\|session id> [--at SECONDS] [--speed N] [--follow]` | Lists replayable sessions, or opens the character-grid replay viewer for one. |
| `replay export` | `replay export <session> [--out file]` | Downloads a full `replay/v1` JSON timeline; `--out` names the browser download, not a server file. |
| `cards` | `cards <run_id>` | Shows a run's cards. |
| `links` | `links [open phoenix\|grafana\|prometheus]` | Shows public observability URLs from `/links`, or opens a configured HTTP(S) target in a new tab. |
| `ready` | `ready [--watch [--interval N]]` | Shows `/ready` aggregate and component status/latency; optionally polls every 1–60 seconds (default 5). |
| `config` | `config` | Shows the read-only `/v1/config` summary as dotted keys; secret settings are `set`/`unset`. |
| `health` | `health [--all]` | Checks `/health`; `--all` shows the same readiness table as `ready`. |
| `upload` | `upload` | Opens a file picker and queues the file. |
| `watch` | `watch [ready] [--interval N]` | Follows document status (default 3 seconds), or readiness with `watch ready` (default 5 seconds); Ctrl+C stops. |
| `auth` | `auth <token> \| --clear` | Sets or clears the API token. |

Sources: `src/mailroom_reloaded/api/tui/commands/shell.js`,
`commands/pipeline.js`, `commands/ledger.js`, `commands/replay.js` (with `replay/`),
`commands/links.js`, `commands/ready.js` and `commands/config.js`
(each command carries its man page). Keys: Tab ghost
completion, Up/Down history, Ctrl+L clear, Ctrl+C stop `watch`, any key skips
the boot animation.

### Operator summaries: `links`, `ready`, `config`

- `links` prints `public_url`, `phoenix_url`, `grafana_url` and `phoenix_project`
  from public `GET /links`. `links open phoenix`, `links open grafana` and
  `links open prometheus` accept only validated HTTP(S) URLs and request a new
  tab with `noopener,noreferrer`. Missing targets print `not configured`;
  invalid URLs are not opened. Prometheus opens only if the server publishes
  `prometheus_url`; the current `/links` response does not publish it.
- `ready` (also `health --all`) reads public `GET /ready`. Component rows include
  status and latency;
  `ok` is green, `degraded` amber, `down` red and `unconfigured` dim. An HTTP 503
  body is still rendered. When component details are hidden, the command prints
  an `auth <token>` hint rather than inventing rows. `ready --watch --interval 3`
  or `watch ready --interval 3` polls every three seconds; it prints again only
  when statuses change (not latency alone), deduplicates repeated poll errors,
  and stops on Ctrl+C. Readiness requests have a three-second client deadline,
  allowing the server's two-second probe budget to return degraded component rows.
- `config` reads `GET /v1/config`, requiring `auth <token>` when the API has a
  token configured. Nested settings appear as dotted key/value rows; secret-looking
  keys accept only the exact markers `set` or `unset`, otherwise showing
  `withheld`. This is an inspection command, not a settings editor. See
  [Configuration](CONFIGURATION.md#read-only-summary-get-v1config) for the route's
  allow-list and limitations.

### `replay export`

`replay export run:showcase-judge-arbiter --out showcase.replay.json` downloads
`GET /v1/replay/sessions/{id}/export` without opening the viewer. A bare run ID
is normalized to `run:<id>`; `run:`, `session:` and `doc:` session IDs are
accepted. With no `--out`, the normalized session ID has non-filename characters
replaced by `_` and gets the suffix `.replay.json` (for example,
`run:demo` becomes `run_demo.replay.json`).

`--out` is a filename only: 1–200 ASCII characters, beginning with a letter or
digit, followed by letters, digits, `.`, `_` or `-`. Paths and extra export flags
are rejected. The browser decides where to save it; the command does not write
to the API server's filesystem. Authentication uses the session bearer token,
not a token in a URL. Failed requests print an error rather than downloading an
error response, and no download is triggered after Ctrl+C aborts the request.

The API export and `mailroom replay export <session> --out file` use the same
full `replay/v1` timeline builder; the CLI's `--out` is a local filesystem path,
unlike the browser command. This describes the implementation, not a claim of a
fresh CLI/browser byte-comparison run. The timeline is the replay payload, not
an export of document text or raw prompts/completions.

### `replay` viewer

`replay <run_id>` reads `GET /v1/replay/sessions/{id}/timeline` (the `replay/v1`
payload) and takes over the output area with a character grid: a station track
(grouped under phase headers when the payload carries phases), a scrub bar with
event ticks, run metrics, and an inspector, ledger or insight panel. All
text is rendered with `textContent`; bidi and control characters are replaced.
Station colours come from each station's `color_token` (`--term-*`, mapped to the
grid's classes). Keys: Space play/pause, Left/Right seek 5s (Shift 30s), `[` `]`
speed, `0`-`9` jump, `r` restart from the start, `j`/`k` select a document, `i`
inspector, `l` ledger panel (fetched on
demand, with a chain-verify line), `p` cycles insight panels (metrics, tokens,
decisions, latency, fields, leaderboard, ticker — pluggable via `registerPanel` in `replay/panels.js`: a panel is
`{ id, title, render }`; the id is 1-32 letters, digits, `_` or `-` and may not be `none`,
`inspector`, `ledger` or a built-in id; the title is cleaned and clamped to 40 characters;
the grid sanitises the rows `render` returns; there is no per-panel key, `p` cycles them),
`e` next event, `f` follow the live stream, `o` opens the run's Phoenix project and `g` the Grafana quality
dashboard in a new tab, `?` shows the key list, `q`/Esc quit. Ctrl+C always
releases the keyboard. The tokens panel draws token- and cost-rate sparklines; the
leaderboard ranks documents by how far they have progressed (finished documents
first) and highlights the selected one; the ticker lists the most recent events. The
scrub bar shades the run's station-phase "sectors" (alternating heavy/light fill with
a rule at each boundary) and the leaderboard and inspector show each document's
elapsed time, tokens and cost at the playhead. A pruned run answers 410 and points at `ledger --run`.
The viewer fetches `GET /links` once on open for the Phoenix and Grafana base URLs;
the inspector lists both URLs for the run (the Grafana one carries `var-run_id`), and
`o`/`g` are a no-op when the config or a browser opener is unavailable.
The viewer needs a physical keyboard (the input stays read-only while it is open).

`--follow` (or the `f` key) starts a live Server-Sent Events reader on
`GET /v1/replay/live?session=<id>` (`replay/live.js`): it appends each new
`entity`/`segment`/`generation`/`event`/`score` frame to the timeline (a new document
is added, a finished one replaces its entry), rebuilds the model and pins the playhead
to the server's `now - 2s` (from the `heartbeat` clock, not the browser's). A backward scrub (Left, Home, a digit jump) leaves
follow, and `f` re-enters; the reader pauses while the tab is hidden and stops on
`q`/Esc. The route is token-gated like the rest of `/v1` and emits `ready`, item,
`heartbeat` and `error` frames. When the span read hits its 100,000-row cap the
route sends an `error` frame with `code: "row_cap"` and ends; the viewer stops
following and shows a notice in the header. The server remembers only the items within
2 h / 20,000 of the newest, so an unlimited stream does not grow without bound. The
footer legend drops the least important keys on a narrow terminal and always keeps
`q quit`. The dev server bounds it with
`MAILROOM_REPLAY_LIVE_POLL_S` (default 1.0), `MAILROOM_REPLAY_LIVE_HEARTBEAT_S`
(default 15) and `MAILROOM_REPLAY_LIVE_MAX_FRAMES` (default 0 = unlimited).

### `jev` and gate decisions

```
> jev
provider     local
model        jevk5
base_url     http://127.0.0.1:8898/v1/systemone
gate         jev
calibrated   yes
accept       0.844
verify       0.733
temperature  0.821
ece          0.153 -> 0.152
n            60

> audit 03a9efe794321eb2
5 audit entries
chain: ok
gate classify -> verify [jev] conf 0.88 — jev confidence 0.792 in verify band
```

With Jev disabled, `jev` prints `jev off (band gate)`. `audit` lists `gate_decision`
entries (source `jev` is coloured by action), `inspect` adds a `gate` line with the
last decision, and boot adds `[ ok ] jev · <provider> calibrated` or `[ -- ] jev · off`.
API keys are never printed.

## Deep links

`/tui#replay=run:<id>` (or a bare run id) runs `replay run:<id>` once boot finishes. The `/ui`
runs table links each eval run this way. Only ids that `replay` itself accepts become a command;
anything else prints `replay: invalid deep link`. The fragment stays in the browser, and the API
token is still taken from this tab's session storage, never from the URL: when a token is
configured, type `auth <token>` first and re-run the replay command, since `/ui` does not
pass its token on. The same `/ui` row also carries outbound `grafana ↗` and `phoenix ↗`
links (built from `GET /links`), and inside the viewer `o`/`g` open the run's Phoenix /
Grafana URLs directly.

The **status bar** at the top of `/tui` carries the outbound `ui ↗` link, which opens `/ui` in the
same tab.

## Themes

`dark` (default), `light` (labelled proposed in the kit), and `hc` (high
contrast: no glows, sparks, scanlines or CRT overlay) set the base scheme;
`amber`, `green` and `cyan` set the phosphor. The choice is stored in
`localStorage`. `prefers-reduced-motion` turns every animation off and spawns
no sparks.

## Auth

With `MAILROOM_API_TOKEN` set on the server, boot prints
`[ !! ] api token required — type 'auth <token>'`. `auth <token>` is masked in
scrollback and history, kept only in `sessionStorage`, and sent only as
`Authorization: Bearer`. `auth --clear` removes it.

## Boot checks

1. `[ ok ] api /health`
2. `[ ok ] auth` (a `/v1/documents` probe; 401 prints the token line above)
3. `[ ok ] catalog · N documents`
4. `[ ok ] eval runs · N` (a failure here prints `[ !! ]` but does not close the terminal)

Then `type 'help' to begin.` An unreachable API prints `[ !! ] api unreachable`
and `mailroom closed — no api connection`, with no data.

## Local harness: `scripts/tui_dev.sh`

```bash
scripts/tui_dev.sh up       # mock LLM :8899, API + embedded watcher :8000, seeds the inbox
scripts/tui_dev.sh status
scripts/tui_dev.sh down
MAILROOM_API_TOKEN=secret scripts/tui_dev.sh up   # exercise the auth path
```

Everything binds to 127.0.0.1. State lives in `data/tui-dev/` (gitignored):
`base/` is the mailroom base dir, plus `*.pid` and `*.log`. The mock provider is
`deploy/mock_openai.py` (`MOCK_BASE_URL=http://127.0.0.1:8899/v1`). The fixtures in
`scripts/tui_seed/` are copied into the inbox once the API is healthy; the mock
classifies every one as correspondence/email and the watcher archives them. One
fixture is named `<b>hostile<b>.txt` to check that filenames render as text.
(`/` cannot appear in a filename, so a closing `</b>` is impossible.)
`up` also runs `scripts/demo_seed_eval_runs.py`, so `/ui` and the eval views list one eval run.
Open http://127.0.0.1:8000/tui.

The four showcase runs (`run:showcase-clean`, `-escalation`, `-judge-arbiter`,
`-parked-failed`) are seeded on API startup (when span capture is enabled), so the replay viewer has data straight
away: `replay run:showcase-judge-arbiter`, or open
`http://127.0.0.1:8000/tui#replay=run:showcase-judge-arbiter`. They have spans but no
ledger rows, so the ledger panel shows `no ledger entries` and `verify failed: unknown run`.
A run-scoped verify cannot tell "never had ledger rows" from "rows deleted", so for any
run that should have a chain, treat that line as a warning and run `ledger verify`.

### Browser check: `scripts/tui_replay_check.mjs`

With the harness up, drive the viewer in headless Chromium (play, pause, seek, jump,
inspector, ledger panel, quit, keyboard handed back, no injected markup, no page errors):

```bash
node scripts/tui_replay_check.mjs                       # run:showcase-judge-arbiter
node scripts/tui_replay_check.mjs run:showcase-parked-failed
MAILROOM_API_TOKEN=secret node scripts/tui_replay_check.mjs   # authenticates first
```

It needs Playwright (`PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs` if it is not
importable) and a Chromium (`CHROMIUM_PATH`). `TUI_URL` overrides `http://127.0.0.1:8000`;
`SHOT=file.png` saves a screenshot. It exits non-zero on any failed check. It is a dev
tool, not part of the pytest suite. The viewer needs a physical keyboard: the input is
read-only while it is open.

The server reads `api/ui/index.html` and the TUI shell once at startup, so
restart (`down`, `up`) after editing them.

## Browser checks and demo screenshots

- `scripts/tui_replay_check.mjs` (above) is the pass/fail browser check for the replay viewer.
- `scripts/demo_capture.mjs` writes the committed screenshots and `manifest.json` to [`docs/demo/`](demo/README.md)
  (viewport 1200x800, dark theme, under 400 KB each); `scripts/demo_seed_eval_runs.py` gives `/ui` a run to link;
  `scripts/demo_release_notes.py --sha <commit>` prints Markdown embedding them for PR bodies and release notes.
  Regenerate them when the UI changes; the rules and exact commands are in `docs/demo/README.md`.
- For #64 B1–B4 command evidence, use `docs/demo/p6b1-4/manifest.json` and PNGs
  in that directory; preserve `docs/demo/manifest.json` and
  its older release gallery. Capture one PNG per command (1200x800, dark theme,
  at most 400 KB), recording command, source commit, SHA-256 and byte size in the
  separate manifest. This is the screenshot-location decision, not evidence that
  new captures or browser checks have run.
- The latest recorded run of the checks and of the manual checklist below, with environment and known gaps, is
  [`docs/evidence/2026-10-10-tui-live-check/`](evidence/2026-10-10-tui-live-check/README.md).

## Manual checklist

1. Boot plays: banner fade, four `[ ok ]` lines with real counts, `type 'help' to begin.`; a keypress skips the animation.
2. `help`, `man ls`, and an unknown command (`flor`) which shakes and prints `flor: command not found — try help`.
3. `ls`, `inspect <id>`, `audit <id>` show live data; the hostile filename is literal text (`document.querySelectorAll('#output img').length === 0`).
4. Tab ghost completion, Up history, Ctrl+L, Ctrl+C during `watch`.
5. `theme green`, `cyan`, `light`, `hc`; reload keeps the choice; `crt off`, `skyline off`.
6. Kill the server mid-session: the next command prints the offline message; the API serves `/tui`, so a reload cannot fetch a fresh page (the browser shows its own connection error; a cached copy that loads shows `mailroom closed — no api connection` because the health, auth or catalog check fails at boot, see Boot checks).
7. Restart with `MAILROOM_API_TOKEN` set: boot says `api token required`; `auth wrong` is rejected; the right token is accepted and absent from scrollback.
8. Mobile preset (375px): only essential status items, 12px base, no horizontal scroll; `createAmbient(..., {reducedMotion: true})` spawns no spark nodes.
9. Console has no errors.

## Modules and the plugin boundary

`tui/plugins.js` is the one way a module adds to the terminal. `main.js` keeps a
`MODULES` list (`pipeline`, `ledger`, `replay`, `shell`, `links`, `ready`, `config`) and
`registerAll` registers each through `createModuleHost(registry)`:

```js
host.registerModule({
  id: 'trace',                       // 1-32 chars: lowercase letter, then [a-z0-9_-]
  commands: [spec, ...],             // or a register function (registry, deps) => void
  panels: [{ id, title, render }],   // replay panels (reached with `p`)
  onInit: async (ctx) => {},         // optional; runs after the terminal exists
});
```

- **Ids.** Reserved ids (`core`, `engine`, `terminal`, `tui`, `boot`, `plugins`)
  and malformed ids are rejected; a duplicate module id throws.
- **All or nothing.** Commands are staged first. A command without a `name`/`run`,
  or one whose name clashes with a registered command or another in the same
  module, rejects the whole module and registers none of it. A register function
  still sees the real registry for reads (`get`, `names`, `complete`), so `help`
  and `man` keep looking commands up lazily.
- **Panels are sanitised at the boundary.** Every module panel's `render` is
  wrapped: a throw yields no rows, and the result goes through the grid's
  `sanitizeRows` (control, zero-width and bidi characters become spaces; unknown
  classes are dropped; non-rows are skipped). Rows are drawn with `textContent`,
  so markup stays inert text. Panel ids still go through `registerPanel`, which
  keeps its own reserved ids (`none`, `inspector`, `ledger`, the shipped panels).
- **Isolation.** `host.registerAll(list)` logs and skips a module that fails to
  register; `host.initAll(ctx)` runs each `onInit` and marks a throwing or
  rejecting one `failed` without stopping the others. `main.js` starts
  `initAll` without awaiting it, so a bad module never blocks boot.
  `host.modules()` lists `{id, commands, panels, state}`.

Tests: `tests/tui/js/plugins.test.mjs`.

### Shared helpers (`tui/lib/`)

New commands share these instead of importing each other (sub-plan rule 1). All
are pure ES modules with injectable `fetch`, timers and `document`.

| File | Exports | Notes |
| --- | --- | --- |
| `lib/http.js` | `createHttp`, `httpFor(ctx)`, `HttpError`, `errorHint`, `HTTP_TIMEOUT_MS` | JSON client with a 2 s default timeout; same-origin paths only; the bearer token comes from `ctx.api.authHeaders()` (still only in session storage). Typed `kind`: `offline`, `timeout`, `unauthorized`, `forbidden`, `not_found`, `http`, `parse`, `aborted`. `errorHint` maps 401 to `run 'auth <token>'`. Uploads and replay keep `api.js` and its longer timeout. |
| `lib/table.js` | `renderTable(rows, columns)`, `cell` | Aligned plain-text lines (header, rule, rows); per-column `align`, `max` (ellipsis) and `format`; control/bidi characters become spaces. |
| `lib/poll.js` | `createPoller(fn, {intervalMs, signal, onError})` | Non-overlapping ticks; skips while `document.hidden` and resumes on `visibilitychange`; aborting the signal or `stop()` cancels the pending tick and aborts the in-flight one. |
| `lib/fmt.js` | `bytes`, `duration`, `usd` | `1.5 KiB`; `850 ms`, `1.2 s`, `3m 05s`, `2h 03m`, `1d 04h`; `$0.0042` under a dollar, `$12.35` above. Non-finite input prints `—`. |
| `lib/links.js` | see the file map | `GET /links` helpers (unchanged). |

Tests: `tests/tui/js/lib-http.test.mjs`, `lib-table.test.mjs`, `lib-poll.test.mjs`,
`lib-fmt.test.mjs`.

## Readiness: `GET /ready`

`GET /ready` (`src/mailroom_reloaded/api/routes/ready.py`, the first router in the
new `api/routes/` package) probes each backend concurrently with a 2 s budget and
reports `ok | degraded | down | unconfigured` with `latency_ms`:

```json
{"schema_version": 1, "status": "ok",
 "components": [{"name": "db", "status": "ok", "latency_ms": 1.2}, ...]}
```

- Components, in order: `db`, `ledger`, `span_store`, `collector`, `phoenix`,
  `prometheus`, `grafana`, `llm_provider`, `watcher`. `collector` probes
  `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (falling back to
  `OTEL_EXPORTER_OTLP_ENDPOINT`), `phoenix`/`grafana` their server-side
  `MAILROOM_PHOENIX_PROBE_URL` / `MAILROOM_GRAFANA_PROBE_URL`, and `llm_provider`
  the base URL the LLM client resolves for the sorter role (`llm.client.resolve`),
  so an unreachable backend is `down` and an unset or unresolvable one is
  `unconfigured`. `prometheus` (#66) still reports `unconfigured`; `watcher` is
  `unconfigured` without the embedded watcher.
- A probe that times out is `degraded`; one that raises is `down` with only the
  exception type in `detail`. Neither is a 500.
- The aggregate is `down` (HTTP 503) only when `db` or `ledger` is down; any other
  down or degraded component makes it `degraded` (HTTP 200).
- The status code and aggregate are public like `/health`. `components` is
  returned only when no token is configured or the request carries the bearer
  token. `Cache-Control: no-store`.
- The TUI `ready` command reads it, including the HTTP 503 response body; see
  the operator summaries above.

Tests: `tests/api/test_ready_route.py`.

## Non-goals

`mail` compose (the app has no send path), `sound`, a corpus command, Gmail
intake commands, control endpoints beyond the existing review/retention operations,
and promoting the kit's proposed
light scheme beyond a selectable theme.

## File map

| Path | Role |
| --- | --- |
| `src/mailroom_reloaded/api/tui/index.html` | Shell page. |
| `.../tui/main.js` | Wiring; the `MODULES` list. |
| `.../tui/plugins.js` | Plugin boundary: `createModuleHost`, `registerModule`, isolated `registerAll`/`initAll`. |
| `.../tui/engine.js` | Parse, registry, history, completion (pure). |
| `.../tui/terminal.js` | DOM rendering and key handling (`textContent` only). |
| `.../tui/api.js` | Fetch wrapper and token storage. |
| `.../tui/boot.js` | Boot sequence. |
| `.../tui/ambient.js` | Themes, skyline, CRT, sparks. |
| `.../tui/commands/shell.js`, `pipeline.js`, `ledger.js`, `replay.js` | Commands (each carries its man page). |
| `.../tui/replay/` (`clock.js`, `model.js`, `grid.js`, `stations.js`, `panels.js`, `live.js`) | Pure viewer core: playback clock, timeline model, character-grid renderer, station table, the pluggable panel registry and the follow-live SSE reader. |
| `.../tui/lib/links.js` | Shared `GET /links` helpers: URL re-check, Phoenix/Grafana URLs. |
| `.../tui/lib/http.js`, `table.js`, `poll.js`, `fmt.js` | Shared command helpers (see "Shared helpers"). |
| `src/mailroom_reloaded/api/routes/ready.py` | `GET /ready` aggregate readiness. |
| `.../tui/deeplink.js` | `#replay=` deep-link parsing. |
| `.../tui/tokens.css`, `tui.css`, `banner*.txt` | Brand tokens, styles, banners. |
| `scripts/tui_dev.sh`, `scripts/tui_seed/` | Local harness and fixtures. |
| `scripts/tui_replay_check.mjs` | Headless-Chromium check of the replay viewer. |
| `tests/api/test_tui_serving.py` | Serving tests. |

## Local dev with Jev

`JEV=1 scripts/tui_dev.sh up` also starts the mock Jev (`deploy/mock_jev.py`, port
`TUI_JEV_PORT`, default 8898), fits a synthetic calibration into
`data/tui-dev/base/models/jev_calibration.json`, enables the Jev gate for the API
and seeds `scripts/tui_seed_jev/*.txt` (`[confidence:0.NN]` markers drive the
mock LLM into the medium band): `jev-proceed` archives, `jev-verify` and
`jev-parks` park for review. `down` and `status` handle the extra process. Check
`GET /v1/jev` and each document's `/v1/audit/{id}` `gate_decision` entries
(`source: "jev"`) to confirm Jev was consulted. See `docs/JEV.md`.
