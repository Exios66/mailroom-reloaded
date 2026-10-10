# `/tui` browser terminal

`/tui` is a browser terminal for mailroom-reloaded in the Mailroom brand kit's
Terminal edition (amber phosphor CRT, boot sequence, ambient skyline). It is
static ES modules served by FastAPI, with no build step and no npm dependency.
Every printed value comes from a live `/v1` call; if the API is unreachable the
terminal says so and prints nothing canned. `/tui` and its assets are public
like `/ui`; `/v1` stays token-gated when `MAILROOM_API_TOKEN` is set.

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
| `inbox` | `inbox [--tab ingress\|boss\|outbox\|events] [--scenario ID] [--message ID] [--thread ID] [--no-mailbox] [--print]` | Opens the sandbox UI's Correspondent inbox (Ingress queue plus the Boss mailbox dock filtered to `role=correspondent`) in a new tab and prints the link. `--print` only prints. |
| `cards` | `cards <run_id>` | Shows a run's cards. |
| `health` | `health` | Checks the API. |
| `upload` | `upload` | Opens a file picker and queues the file. |
| `watch` | `watch [--interval 3]` | Follows status changes; stops on Ctrl+C or when the tab is hidden. |
| `auth` | `auth <token> \| --clear` | Sets or clears the API token. |

Sources: `src/mailroom_reloaded/api/tui/commands/shell.js`,
`commands/pipeline.js`, `commands/ledger.js`, `commands/replay.js` (with `replay/`) and `commands/sandbox.js` (each command carries its man page). Keys: Tab ghost
completion, Up/Down history, Ctrl+L clear, Ctrl+C stop `watch`, any key skips
the boot animation.

### `replay` viewer

`replay <run_id>` reads `GET /v1/replay/sessions/{id}/timeline` (the `replay/v1`
payload) and takes over the output area with a character grid: a station track,
a scrub bar with event ticks, run metrics, and an inspector or ledger panel. All
text is rendered with `textContent`; bidi and control characters are replaced.
Keys: Space play/pause, Left/Right seek 5s (Shift 30s), `[` `]` speed, `0`-`9`
jump, `j`/`k` select a document, `i` inspector, `l` ledger panel (fetched on
demand, with a chain-verify line), `p` cycles insight panels (metrics, tokens,
decisions, latency, fields — pluggable via `registerPanel` in `replay/panels.js`: a panel is
`{ id, title, render }`; the id is 1-32 letters, digits, `_` or `-` and may not be `none`,
`inspector`, `ledger` or a built-in id; the title is cleaned and clamped to 40 characters;
the grid sanitises the rows `render` returns; there is no per-panel key, `p` cycles them),
`e` next event, `f` follow the live stream, `o` opens the run's Phoenix project and `g` the Grafana quality
dashboard in a new tab, `q`/Esc quit. Ctrl+C always
releases the keyboard. A pruned run answers 410 and points at `ledger --run`.
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

`/tui#inbox` runs `inbox`, and `/tui#inbox=<tab>` runs `inbox --tab <tab>`; the tab must be one of
`ingress`, `boss`, `outbox`, `events`, and anything else prints `inbox: invalid deep link`.

### `inbox` and the sandbox link

`inbox` reads `GET /links` for `sandbox_url` (server setting `MAILROOM_SANDBOX_URL`), re-checks it
client-side (plain `http(s)`, no credentials) and opens
`<sandbox_url>/ui#tab=messages&mailbox=open&role=correspondent` in a new tab, so the owner can watch
a sandbox simulation live. It is a new tab because the sandbox is a different origin with no CORS or
frame permission, so `/tui` cannot embed or call it; the sandbox UI polls its own API every 2 s.
The hash keys the sandbox UI accepts are `tab` (`messages|boss|outbox|events|conformance|docs|policy`),
`scenario`, `sel` and `thread` (ids matching `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`), `mailbox` (`open|closed`) and
`role` (`correspondent|boss`). `--tab` maps `ingress` to `messages`; `--message` sets `sel`;
`--no-mailbox` sets `mailbox=closed`. The API token is never put in the link: if the sandbox asks
for one, paste it into its API token field. Without a valid `sandbox_url` the command prints
`inbox: sandbox URL not configured (set MAILROOM_SANDBOX_URL)`. In the replay viewer's inspector a
`sandbox` row shows the same link when `/links` carries `sandbox_url` (there is no key for it).

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
- The latest recorded run of the checks and of the manual checklist below, with environment and known gaps, is
  [`docs/evidence/2026-10-10-tui-live-check/`](evidence/2026-10-10-tui-live-check/README.md).

## Manual checklist

1. Boot plays: banner fade, four `[ ok ]` lines with real counts, `type 'help' to begin.`; a keypress skips the animation.
2. `help`, `man ls`, and an unknown command (`flor`) which shakes and prints `flor: command not found — try help`.
3. `ls`, `inspect <id>`, `audit <id>` show live data; the hostile filename is literal text (`document.querySelectorAll('#output img').length === 0`).
4. Tab ghost completion, Up history, Ctrl+L, Ctrl+C during `watch`.
5. `theme green`, `cyan`, `light`, `hc`; reload keeps the choice; `crt off`, `skyline off`.
6. Kill the server mid-session: the next command prints the offline message; reload shows `mailroom closed — no api connection` with no data.
7. Restart with `MAILROOM_API_TOKEN` set: boot says `api token required`; `auth wrong` is rejected; the right token is accepted and absent from scrollback.
8. Mobile preset (375px): only essential status items, 12px base, no horizontal scroll; `createAmbient(..., {reducedMotion: true})` spawns no spark nodes.
9. Console has no errors.

## Non-goals

`mail` compose (the app has no send path), `sound`, a corpus command, Gmail
intake commands, any change to the `/v1` API, and promoting the kit's proposed
light scheme beyond a selectable theme.

## File map

| Path | Role |
| --- | --- |
| `src/mailroom_reloaded/api/tui/index.html` | Shell page. |
| `.../tui/main.js` | Wiring. |
| `.../tui/engine.js` | Parse, registry, history, completion (pure). |
| `.../tui/terminal.js` | DOM rendering and key handling (`textContent` only). |
| `.../tui/api.js` | Fetch wrapper and token storage. |
| `.../tui/boot.js` | Boot sequence. |
| `.../tui/ambient.js` | Themes, skyline, CRT, sparks. |
| `.../tui/commands/shell.js`, `pipeline.js`, `ledger.js`, `replay.js`, `sandbox.js` | Commands (each carries its man page). |
| `.../tui/replay/` (`clock.js`, `model.js`, `grid.js`, `stations.js`, `panels.js`, `live.js`) | Pure viewer core: playback clock, timeline model, character-grid renderer, station table, the pluggable panel registry and the follow-live SSE reader. |
| `.../tui/lib/links.js` | Shared `GET /links` helpers: URL re-check, Phoenix/Grafana URLs, `inboxUrl` (pure, allow-listed). |
| `.../tui/deeplink.js` | `#replay=` and `#inbox` deep-link parsing. |
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
