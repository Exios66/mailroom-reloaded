# TUI live browser check, 2026-10-10

Evidence for plan item R-10 (`docs/superpowers/plans/2026-10-09-mailroom-core-plan.md`): the replay browser
check and the nine-point manual checklist in `docs/TUI.md`, run headless against `scripts/tui_dev.sh up`.
Screenshots are in [`docs/demo/`](../../demo/README.md).

## Environment

| Item | Value |
| --- | --- |
| Code under test | `502995aa6a98d0af62c02efe77ac7285bea0d15d` (`origin/main` at capture time, clean worktree) |
| OS | Linux 6.18 x86_64 (container) |
| Node | v22.22.0 |
| Playwright | 1.56.1 (`PLAYWRIGHT_MODULE=/opt/node-tools/node_modules/playwright/index.mjs`) |
| Chromium | 141.0.7390.37 (`CHROMIUM_PATH=/opt/pw-browsers/chromium-1194/chrome-linux/chrome`, headless) |
| Python / uv | 3.11.17 / 0.11.32 (`uv sync --extra dev --extra sandbox`) |
| Stack | `scripts/tui_dev.sh up`: mock LLM :8899, API :8000, loopback only; replay seed `run:7e57d0c0ffee` + four showcase runs |

## `scripts/tui_replay_check.mjs`

Default run (`run:showcase-judge-arbiter`, no token):

```
$ node scripts/tui_replay_check.mjs
ok   viewer opens as role=application
ok   clock advances while playing
ok   space pauses
ok   arrow keys seek
ok   digit jumps through the run
ok   inspector panel opens
ok   ledger panel settles on an entry list and a chain state
ok   no stray elements inside the viewer
ok   q ends the viewer
ok   keyboard is handed back to the prompt
ok   no page errors
all checks passed
```

Seeded run:

```
$ node scripts/tui_replay_check.mjs run:7e57d0c0ffee
ok   viewer opens as role=application
ok   clock advances while playing
ok   space pauses
ok   arrow keys seek
ok   digit jumps through the run
ok   inspector panel opens
ok   ledger panel settles on an entry list and a chain state
ok   no stray elements inside the viewer
ok   q ends the viewer
ok   keyboard is handed back to the prompt
ok   no page errors
all checks passed
```

Token variant (stack restarted with `MAILROOM_API_TOKEN=demo-token-not-secret`, a throwaway value):

```
$ MAILROOM_API_TOKEN=demo-token-not-secret node scripts/tui_replay_check.mjs
ok   viewer opens as role=application
ok   clock advances while playing
ok   space pauses
ok   arrow keys seek
ok   digit jumps through the run
ok   inspector panel opens
ok   ledger panel settles on an entry list and a chain state
ok   no stray elements inside the viewer
ok   q ends the viewer
ok   keyboard is handed back to the prompt
ok   no page errors
all checks passed
```

## Nine-point checklist (`docs/TUI.md` "Manual checklist")

Driven by [`checklist.mjs`](checklist.mjs) and [`offline_midsession.mjs`](offline_midsession.mjs) in this directory,
not by a human with a keyboard. Each result is what the script measured; "pass" means the observable stated in
the checklist was seen, not that the pixels were judged by eye (the screenshots in `docs/demo/` were looked at).

| # | Point | Result | Evidence |
| --- | --- | --- | --- |
| 1 | Boot plays: banner, four `[ ok ]` lines with real counts, `type 'help' to begin.`; a keypress skips | PASS | 4 ok lines, `catalog · 4 documents`, `eval runs · N`, banner element present; boot 3.0 s; with a keypress 0.2 s |
| 2 | `help`, `man ls`, unknown command shakes and prints `flor: command not found — try help` | PASS | message matched; `term-shake` animation applied to the error line |
| 3 | `ls`, `inspect <id>`, `audit <id>` live data; hostile filename literal, no `img` in `#output` | PASS | 0 `img` elements; `<b>hostile<b>.txt` appears as text; inspect 340 chars, audit 169 chars |
| 4 | Tab ghost completion, Up history, Ctrl+L, Ctrl+C during `watch` | PASS | ghost `p` after `hel`, Tab gives `help`; Up recalls last command; Ctrl+L clears; `watch` output stops after Ctrl+C and the prompt works |
| 5 | `theme green/cyan/light/hc`; reload keeps it; `crt off`, `skyline off` | PASS | four distinct `data-theme`/`data-phosphor` states; `hc` kept after reload (`localStorage mailroom.tui.theme`); overlay and skyline get class `off` |
| 6 | Kill the server mid-session: offline message; reload shows `mailroom closed — no api connection` | PARTIAL | 6a PASS: after `tui_dev.sh down`, `health` and `ls` print `api unreachable — mailroom closed`. 6b PASS only as a simulation: shell served, `/v1` and `/health` aborted at the network layer, boot prints `mailroom closed — no api connection` with no data lines. 6c: a reload against a dead server cannot load at all (`ERR_CONNECTION_REFUSED`), so the checklist wording "kill the server, reload" cannot be literally satisfied |
| 7 | Restart with `MAILROOM_API_TOKEN`: boot says `api token required`; `auth wrong` rejected; right token accepted and absent from scrollback | PASS | boot line `api token required`; asserted `/rejected|401/` on the wrong-token reply (`auth: token rejected — 401`) and `/auth: ok/` on the right-token reply (`auth: ok`); `ls` works afterwards; the token appears 0 times in page text and DOM, echoed as `••••`; the 401 console line from the pre-auth probes is by design |
| 8 | Mobile 375 px: essential status items only, 12 px base, no horizontal scroll; no spark nodes with reduced motion | PASS | 4 of 7 status items visible; body `12px`; `scrollWidth <= innerWidth` at boot and after `ls`; 0 spark elements under `prefers-reduced-motion` (the `createAmbient` unit path itself is covered by `tests/tui/js/ambient.test.mjs`) |
| 9 | Console has no errors | PASS | no console or page errors across points 1-5 and 8 (token run: only the by-design 401) |

Raw output, main walk (points 1-5, 8, 9 and 6b):

```
P1 PASS ok-lines=4 counts=true banner=true boot=3045ms; keypress-skip boot=201ms (faster)
P2 PASS shake animation=term-line-in, term-shake
P3 PASS doc=08268ac00d4c0d10 imgs=0 hostile-literal=true inspect-chars=340 audit-chars=169
P4 PASS ghost="p" tab->"help" up->"audit 08268ac00d4c0d10" cleared-chars=0 watch-stopped=true
P5 PASS applied={"green":"dark|green","cyan":"dark|cyan","light":"light|cyan","hc":"hc|"} after-reload=hc| crt-class="crt-overlay off" skyline-class="ambient-skyline off" storage={"mailroom.tui.theme":"{\"theme\":\"hc\",\"phosphor\":null}"}
P8 PASS visible-status=["mailroom@floor","~","ui ↗","00:48"] of 7; body font=12px; no-hscroll boot=true after-ls=true; spark-nodes(reduced-motion)=0
P9 PASS no console errors / page errors across points 1-5 and 8
P6b PASS simulated via route.abort; shows closed line; leaked data lines=false
```

Mid-session kill (6a, 6c):

```
P6a PASS lines=["[ ok ] api /health","~ $ health","health: api unreachable — mailroom closed","~ $ ls","ls: api unreachable — mailroom closed"]
P6c PASS cold reload against dead server cannot load: page.reload: net::ERR_CONNECTION_REFUSED
```

Token run (7):

```
P7 PASS boot-has-"api token required"=true; wrong-token reply="type 'help' to begin.\n~ $ auth ••••\nauth: token rejected — 401"; right-token reply="~ $ auth ••••\nauth: ok"; ls-after-auth works=true; token occurrences in scrollback text=0, in DOM html=0, in status bar=false
SCROLLBACK-AFTER-AUTH:
[ !! ] api token required — type 'auth <token>'
~ $ auth ••••
auth: token rejected — 401
~ $ auth ••••
auth: ok
PAGE-ERRORS: ["Failed to load resource: the server responded with a status of 401 (Unauthorized)"]
```

## Other previously unverified items

| Item | Command | Result |
| --- | --- | --- |
| Installed `mailroom` console script, import | `MAILROOM_BASE_DIR=<tmp> .venv/bin/mailroom replay import otlp.json` (hand-made, 2 spans) | PASS: `{"parsed": 2, "stored": 2, "skipped": 0, "runs": ["run-hand-1"], "sessions": []}`, exit 0 |
| Re-import | same command again | PASS: refused with `already has 2 spans; use --append ...`, exit 2; with `--append`: `warning: 2 of 2 spans were not stored (already present ...)` |
| Sessions | `.venv/bin/mailroom replay sessions --json` | PASS: stdout is valid JSON listing `run:run-hand-1`, 1 doc, 3.0 s; a `replay_sessions_no_eval_docs` debug line goes to stderr only |
| Export | `mailroom replay export run:run-hand-1 -o export.json`; to stdout; `run:nope` | PASS: file written, `version: replay/v1`; unknown session exits 1 with `no timeline for run:nope` |
| `scripts/sandbox_lofo.sh`, smoke tree | `bash scripts/sandbox_lofo.sh src/mailroom_reloaded/sandbox/fixtures/smoke <tmp>` (offline, 25 s) | PASS with noise: exit 0, 6/6 pass; `lofo_baseline.json` identical to the committed one; `conformance_baseline.json` differs only in the documented hand-added `positional_smoke_fixture` relabel. See bug 1 for the logged tracebacks |
| `scripts/sandbox_lofo.sh`, larger pack | same, with a scratch copy of a local `mailroom-sandbox-content` checkout at `0df2977` (116 scenarios, `tools/validate.py` run in the copy) | RAN, not comparable: exit 0, pass 66 / fail 49 / not_run 1 of 116. That checkout is newer than the pinned `v0.5.0` (`f650cfd`, 88 scenarios) and has no `dist/registry.yaml` until validated, so no committed baseline applies. The committed baselines in `tests/sandbox/` were not touched (output went to a temp dir) |
| Pinned `v0.5.0` bundle | none available offline | COULD NOT RUN |
| `bash -n` | `scripts/tui_dev.sh`, `scripts/sandbox.sh`, `scripts/sandbox_lofo.sh` | PASS (none were edited). `tui_dev.sh up/down/status` and `sandbox_lofo.sh` were also run for real |
| New files | `node --check scripts/demo_capture.mjs`, `docs/evidence/.../*.mjs`; `ruff check`; pytest | PASS |

## Sandbox UI (images 16-18)

`uv run --extra sandbox mailroom sandbox serve --host 127.0.0.1 --port 8100 --content smoke --data-dir <tmp>`
ran fully offline. After injecting A1 and E1 (`POST /api/sandbox/v1/inject`, `wait: true`) the dock shows
the unread badge `2` and, expanded, the draft awaiting approval and a pending `hostile_forward`
(`payment_fraud`, critical, `wire_instructions_updated_inert.pdf` held). `GET /boss/mailbox?latest=true` returned both
entries; `GET /boss/pending` returned the single pending case. No decision was posted, so nothing was released.
The `deciding` resume hint was not reached (see known gaps).

## Known gaps and bugs found (not fixed here; `src/` was not touched)

1. **Sandbox conformance logs `ledger_write_failed` tracebacks.** Repro: `bash scripts/sandbox_lofo.sh
   src/mailroom_reloaded/sandbox/fixtures/smoke /tmp/out`. Seven rich tracebacks `OperationalError: no such table:
   ledger` plus `ledger_metric_rows_dropped rows=19`; exit code 0 and results unaffected. The writer thread retries
   (`src/mailroom_reloaded/storage/ledger.py:291`) against a throwaway state dir whose ledger table was never created.
   `sandbox serve` did not log it.
2. **`/ui` run links are unstyled.** In `src/mailroom_reloaded/api/ui/index.html` the `replay ↗`, `grafana ↗` and
   `phoenix ↗` anchors inside the Eval runs table (built at lines ~211-229) render in the browser default blue
   (`#0000ee`) on the dark background, and wrap letter by letter at 1200 px (`docs/demo/13-ui-replay-links.png`).
3. **Sandbox UI CSP console error.** `/ui` on the sandbox server logs `Refused to apply inline style because it
   violates the following Content Security Policy directive: "default-src 'self'"` on every load; at 1200 px the
   ingress-queue table also overflows its card (`docs/demo/16-sandbox-dock-badge.png`).
4. **Dev harness shows no eval run.** `scripts/tui_dev.sh up` seeds spans only, so `/ui` says "no eval runs yet" and
   none of the replay/grafana/phoenix links can be seen. `scripts/demo_seed_eval_runs.py` inserts two `eval_docs`
   rows as a workaround.
5. **Checklist point 6 wording** ("kill the server ... reload shows `mailroom closed`") cannot happen as written;
   the closed screen needs a served page with an unreachable API. Suggest rewording `docs/TUI.md` point 6.
6. Seeded replay documents all show `file —` in the inspector (no filename attribute in the seed spans); not a viewer bug.

Not run or not reachable: the sandbox `deciding` resume hint (needs a half-finished decision; only unit tests reach
it); the pinned content bundle; the Supabase anchor (R-17, owner-only); `tui_replay_check.mjs` in a real GUI browser
(headless Chromium only). The nine-point walk was scripted, not done by hand on a physical keyboard.
