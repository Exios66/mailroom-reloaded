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
| `runs` | `runs` | Lists eval runs. |
| `cards` | `cards <run_id>` | Shows a run's cards. |
| `health` | `health` | Checks the API. |
| `upload` | `upload` | Opens a file picker and queues the file. |
| `watch` | `watch [--interval 3]` | Follows status changes; stops on Ctrl+C or when the tab is hidden. |
| `auth` | `auth <token> \| --clear` | Sets or clears the API token. |

Sources: `src/mailroom_reloaded/api/tui/commands/shell.js` and
`commands/pipeline.js` (each command carries its man page). Keys: Tab ghost
completion, Up/Down history, Ctrl+L clear, Ctrl+C stop `watch`, any key skips
the boot animation.

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

The server reads `api/ui/index.html` and the TUI shell once at startup, so
restart (`down`, `up`) after editing them.

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
| `.../tui/commands/shell.js`, `pipeline.js` | Commands. |
| `.../tui/tokens.css`, `tui.css`, `banner*.txt` | Brand tokens, styles, banners. |
| `scripts/tui_dev.sh`, `scripts/tui_seed/` | Local harness and fixtures. |
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
