# Evidence: /tui status-bar ingress link (2026-10-10)

Verifies that the `/tui` status bar (the header at the top of the terminal
window) renders **both** outbound links — `ui ↗` and `inbox ↗` (the sandbox
ingress simulator) — in a real browser.

## Environment

| Item | Value |
| --- | --- |
| Harness | `scripts/tui_dev.sh up` (host: mock LLM + API + embedded watcher; no Docker) |
| API | `http://127.0.0.1:8000` |
| Browser | Google Chrome, `--headless=new` (macOS), `--virtual-time-budget=12000` |
| Source | `main` @ `ccafc84` (the `addSisterLink` change) |

## Commands

```bash
scripts/tui_dev.sh up
curl -fsS http://127.0.0.1:8000/links            # sandbox_url present
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --user-data-dir=/tmp/chrome-tui --virtual-time-budget=12000 --window-size=1200,800 \
  --dump-dom "http://127.0.0.1:8000/tui" > status-bar.html
```

## Result — `GET /links`

```json
{"public_url":"http://localhost:8000","phoenix_url":"http://localhost:6006",
 "grafana_url":"http://localhost:3000","sandbox_url":"http://localhost:8100",
 "phoenix_project":"mailroom-live"}
```

## Result — rendered status bar (after JS)

See [`status-bar.html`](status-bar.html). Both links are present:

```html
<header class="status-bar"><div class="status-group">
  <span class="status-item essential"><span class="dot"></span><span>mailroom@floor</span></span>
  <span class="status-item essential"><b>~</b></span>
  <span class="status-item"><span>theme </span><b>dark</b></span>
  <span class="status-item"><span>crt </span><b>on</b></span>
  <span class="status-item essential"><a class="sister-link" href="/ui">ui ↗</a></span>
  <span class="status-item essential"><a class="sister-link"
     href="http://localhost:8100/ui#tab=messages&amp;mailbox=open&amp;role=correspondent"
     target="_blank" rel="noopener noreferrer">inbox ↗</a></span>
</div>…</header>
```

- `ui ↗` → `/ui`, same tab.
- `inbox ↗` → `<sandbox_url>/ui#tab=messages&mailbox=open&role=correspondent`, new tab (`target="_blank"`), no token in the URL.

## Files

| File | What |
| --- | --- |
| [`status-bar-2x.png`](status-bar-2x.png) | The status bar at 2× device scale (legible). |
| [`status-bar.html`](status-bar.html) | The rendered `<header class="status-bar">` from `--dump-dom`. |
| [`../../demo/21-tui-status-links.png`](../../demo/21-tui-status-links.png) | The full 1200×800 render (also in `docs/demo/`). |

## Notes

- The `inbox ↗` target (`http://localhost:8100`) is the configured
  `MAILROOM_SANDBOX_URL` default. The link is shown even when no sandbox is
  running (the sandbox needs Docker); the page loads only when the sandbox is up.
- **Dev vs prod Docker:** `deploy/docker-compose.dev.yml` bind-mounts
  `../src:/app/src`, so the dev stack serves this change live (hard-refresh the
  browser). The production `deploy/Dockerfile` does `COPY src ./src`, so the
  production image must be **rebuilt** to pick it up.
