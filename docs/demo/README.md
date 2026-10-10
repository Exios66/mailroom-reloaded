# Demo screenshots

Committed PNGs of the `/tui` terminal, the replay viewer, `/ui` and the offline sandbox UI, for pull
requests, GitHub releases and new contributors. Every image is 1200x800, device scale 1, dark theme.
`manifest.json` records, per image, the caption, how it was produced, the sha256, the size in bytes and the
viewport; its `source_commit` is the commit of the code that was running (`git rev-parse HEAD`).

| File | What it shows | Size |
| --- | --- | --- |
| [`01-tui-help.png`](01-tui-help.png) | The /tui terminal after boot, running `help`. | 201 KB |
| [`02-replay-viewer.png`](02-replay-viewer.png) | Replay viewer paused at 01:30: station grid with per-station counts, scrub bar and the run metrics line. | 237 KB |
| [`03-replay-panel-metrics.png`](03-replay-panel-metrics.png) | Replay insight panel `metrics` (first `p`). | 251 KB |
| [`04-replay-panel-tokens.png`](04-replay-panel-tokens.png) | Replay insight panel `tokens` (second `p`). | 244 KB |
| [`05-replay-panel-decisions.png`](05-replay-panel-decisions.png) | Replay insight panel `decisions` (third `p`). | 244 KB |
| [`06-replay-panel-latency.png`](06-replay-panel-latency.png) | Replay insight panel `latency` (fourth `p`). | 254 KB |
| [`07-outbound-links.png`](07-outbound-links.png) | Inspector (no document selected) listing the Phoenix and Grafana links, after pressing `o` and `g` (each opens a new tab). | 258 KB |
| [`08-inspector-retry.png`](08-inspector-retry.png) | Inspector on the retry document (gate_decision then retry events). | 277 KB |
| [`09-inspector-failed.png`](09-inspector-failed.png) | Inspector on the failed document (failure `run_budget`, cause `extraction_miss`). | 277 KB |
| [`10-inspector-parked.png`](10-inspector-parked.png) | Inspector on the parked document (parked in review, cause `judge_partial`). | 273 KB |
| [`11-inspector-boss.png`](11-inspector-boss.png) | Inspector on the boss-escalated document (arbiter and escalation events). | 278 KB |
| [`12-links-json.png`](12-links-json.png) | Raw `GET /links`: the public base URLs the UI and viewer link out to. | 14 KB |
| [`13-ui-replay-links.png`](13-ui-replay-links.png) | /ui Eval runs table with `replay`, `grafana` and `phoenix` links per run. | 104 KB |
| [`14-ledger.png`](14-ledger.png) | The `ledger` listing followed by `ledger verify` (hash-chain check). | 260 KB |
| [`15-pipeline-ls-runs.png`](15-pipeline-ls-runs.png) | Pipeline commands `ls` (catalog documents, hostile filename shown as literal text) and `runs` (eval runs). | 266 KB |
| [`16-sandbox-dock-badge.png`](16-sandbox-dock-badge.png) | Sandbox UI with the docked Boss mailbox collapsed: unread badge on the toggle. | 174 KB |
| [`17-sandbox-dock-open.png`](17-sandbox-dock-open.png) | Boss mailbox dock expanded: draft awaiting approval and the hostile_forward entry (pending Release / Quarantine). | 210 KB |
| [`18-sandbox-pending-review.png`](18-sandbox-pending-review.png) | Pending boss review tab: payment_fraud hostile_forward from E1 awaiting a Boss decision (nothing is decided by the capture). | 197 KB |

Not shown: the sandbox `deciding` resume hint ("decision ... was recorded but not finished: press the same
button to resume"). It needs a decision that failed half way and no supported way to cause that offline was
found; it is covered by `tests/sandbox/test_server_boss_review.py` only.

## Regenerate

Prerequisites: `uv sync --extra dev --extra sandbox`, Node (tested with 22.22.0), Playwright (`npm i playwright`) and a Chromium.
Run the dev stack **without** `MAILROOM_API_TOKEN` (the capture never types a token).

```bash
export PLAYWRIGHT_MODULE=/path/to/node_modules/playwright/index.mjs   # if `import 'playwright'` does not resolve
export CHROMIUM_PATH=/path/to/chrome                                   # if Playwright has no browser of its own

scripts/tui_dev.sh up
MAILROOM_BASE_DIR=data/tui-dev/base python3 scripts/demo_seed_eval_runs.py   # /ui needs one eval run to show links

# optional: the sandbox Boss-mailbox dock (images 16-18)
uv run --extra sandbox mailroom sandbox serve --host 127.0.0.1 --port 8100 --content smoke \
  --data-dir "$(mktemp -d)" &

SANDBOX_URL=http://127.0.0.1:8100 node scripts/demo_capture.mjs     # omit SANDBOX_URL to skip 16-18
# stop everything
scripts/tui_dev.sh down; kill %1
```

`OUT_DIR` (default `docs/demo`) redirects the output, for example to compare against the committed set.
The script fails if any PNG exceeds 400 KB. Without `SANDBOX_URL` it still rewrites `manifest.json`
and drops the three sandbox entries, so regenerate the full set (with the sandbox) before committing.

Stability: reduced motion, replay opened paused with `--at`, wall clock, random skyline and CSS animations
hidden. Images 01-12 embed no seed times or ids and were identical across repeated runs (spot-checked after a re-seed). Images 13-15 (`/ui`,
ledger, `ls`/`runs`) embed the stack's seed time and run ids, so they change
whenever the dev stack is re-seeded. Images 17 and 18 (sandbox) embed message timestamps and random ids and
change on every run. Image 16 depends on the sandbox state. Expect `manifest.json` hashes for 13-18 to differ
after every regeneration. `demo_capture.mjs` exits non-zero on any page or console error other than the by-design 401.

## PR bodies and release notes

```bash
python3 scripts/demo_release_notes.py --sha "$(git rev-parse HEAD)" [--repo owner/name] [--private]
```

prints Markdown embedding every image as `https://github.com/<repo>/blob/<sha>/docs/demo/<file>?raw=true`
plus a captions list; paste it into the PR description or release notes. Push the commit that contains the
PNGs first. The `blob/...?raw=true` form renders for signed-in viewers of private repositories, which
`raw.githubusercontent.com` does not; `--private` adds a note saying so.

## Rules

- Regenerate (and commit PNGs plus `manifest.json` together) whenever a change alters what `/tui`, the replay
  viewer, `/ui` or the sandbox UI look like. A stale image is a bug in the PR that changed the UI.
- Size budget: at most 400 KB per file (current set: under 300 KB). Do not raise device scale or the viewport.
- No secrets: never capture with `MAILROOM_API_TOKEN` set, never type a token, no real email, absolute home
  paths or customer data. Seed data only (the mock provider, `*.sandbox.invalid` addresses).
- Naming: `NN-kebab-case-topic.png`, two-digit prefix in reading order; do not renumber existing files, so
  links in old PRs and releases keep working. Add a caption for every new image in `scripts/demo_capture.mjs`.
- Never hand-edit PNGs or `manifest.json`; both come from `scripts/demo_capture.mjs`.
- `tests/test_demo_release_notes.py` checks that every manifest sha256 and size matches the file on disk.

## Images

### `01-tui-help.png`

The /tui terminal after boot, running `help`.

![01-tui-help.png](01-tui-help.png)

### `02-replay-viewer.png`

Replay viewer paused at 01:30: station grid with per-station counts, scrub bar and the run metrics line.

![02-replay-viewer.png](02-replay-viewer.png)

### `03-replay-panel-metrics.png`

Replay insight panel `metrics` (first `p`).

![03-replay-panel-metrics.png](03-replay-panel-metrics.png)

### `04-replay-panel-tokens.png`

Replay insight panel `tokens` (second `p`).

![04-replay-panel-tokens.png](04-replay-panel-tokens.png)

### `05-replay-panel-decisions.png`

Replay insight panel `decisions` (third `p`).

![05-replay-panel-decisions.png](05-replay-panel-decisions.png)

### `06-replay-panel-latency.png`

Replay insight panel `latency` (fourth `p`).

![06-replay-panel-latency.png](06-replay-panel-latency.png)

### `07-outbound-links.png`

Inspector (no document selected) listing the Phoenix and Grafana links, after pressing `o` and `g` (each opens a new tab).

![07-outbound-links.png](07-outbound-links.png)

### `08-inspector-retry.png`

Inspector on the retry document (gate_decision then retry events).

![08-inspector-retry.png](08-inspector-retry.png)

### `09-inspector-failed.png`

Inspector on the failed document (failure `run_budget`, cause `extraction_miss`).

![09-inspector-failed.png](09-inspector-failed.png)

### `10-inspector-parked.png`

Inspector on the parked document (parked in review, cause `judge_partial`).

![10-inspector-parked.png](10-inspector-parked.png)

### `11-inspector-boss.png`

Inspector on the boss-escalated document (arbiter and escalation events).

![11-inspector-boss.png](11-inspector-boss.png)

### `12-links-json.png`

Raw `GET /links`: the public base URLs the UI and viewer link out to.

![12-links-json.png](12-links-json.png)

### `13-ui-replay-links.png`

/ui Eval runs table with `replay`, `grafana` and `phoenix` links per run.

![13-ui-replay-links.png](13-ui-replay-links.png)

### `14-ledger.png`

The `ledger` listing followed by `ledger verify` (hash-chain check).

![14-ledger.png](14-ledger.png)

### `15-pipeline-ls-runs.png`

Pipeline commands `ls` (catalog documents, hostile filename shown as literal text) and `runs` (eval runs).

![15-pipeline-ls-runs.png](15-pipeline-ls-runs.png)

### `16-sandbox-dock-badge.png`

Sandbox UI with the docked Boss mailbox collapsed: unread badge on the toggle.

![16-sandbox-dock-badge.png](16-sandbox-dock-badge.png)

### `17-sandbox-dock-open.png`

Boss mailbox dock expanded: draft awaiting approval and the hostile_forward entry (pending Release / Quarantine).

![17-sandbox-dock-open.png](17-sandbox-dock-open.png)

### `18-sandbox-pending-review.png`

Pending boss review tab: payment_fraud hostile_forward from E1 awaiting a Boss decision (nothing is decided by the capture).

![18-sandbox-pending-review.png](18-sandbox-pending-review.png)
