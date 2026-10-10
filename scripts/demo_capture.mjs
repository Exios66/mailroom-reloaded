#!/usr/bin/env node
// Deterministic demo screenshots for /tui, /ui and (optionally) the sandbox UI.
//
//   scripts/tui_dev.sh up
//   MAILROOM_BASE_DIR=data/tui-dev/base python3 scripts/demo_seed_eval_runs.py   # so /ui lists a run
//   node scripts/demo_capture.mjs                    # writes docs/demo/*.png and docs/demo/manifest.json
//   SANDBOX_URL=http://127.0.0.1:8100 node scripts/demo_capture.mjs   # + sandbox Boss-mailbox dock shots
//
// Environment:
//   TUI_URL            dev API base, default http://127.0.0.1:8000 (must be the unauthenticated dev stack)
//   SANDBOX_URL        optional sandbox server (`mailroom sandbox serve --content smoke`, port 8100); when set,
//                      A1 + E1 are injected if its Boss mailbox is empty, then the dock is captured
//   PLAYWRIGHT_MODULE  path or specifier of Playwright (default `playwright`)
//   CHROMIUM_PATH      Chromium executable (default: Playwright's own download)
//   OUT_DIR            output directory, default docs/demo (relative to the repo root)
//
// Determinism: viewport 1200x800, device scale 1, dark colour scheme, prefers-reduced-motion
// (boot animation, ambient sparks and replay autoplay are off), replay opened paused with
// `--at SECONDS`, and the wall clock, the random skyline and CSS animations are hidden. Ledger timestamps and hashes reflect
// the seed time, so ledger/pipeline images differ between regenerations by design.
// No token is ever typed or displayed; run the dev stack WITHOUT MAILROOM_API_TOKEN.
// Each PNG must stay under 400 KB (the script fails otherwise). It never touches src/.
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const base = (process.env.TUI_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '');
const sandbox = (process.env.SANDBOX_URL || '').replace(/\/+$/, '');
const outDir = path.resolve(root, process.env.OUT_DIR || 'docs/demo');
const RUN = 'run:7e57d0c0ffee';
const VIEWPORT = { width: 1200, height: 800 };
const BUDGET = 400 * 1024;
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

mkdirSync(outDir, { recursive: true });
const shots = [];
const errors = [];

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
const context = await browser.newContext({
  viewport: VIEWPORT,
  deviceScaleFactor: 1,
  colorScheme: 'dark',
  reducedMotion: 'reduce',
});

try {

  const shoot = async (page, file, caption, produced) => {
    const dest = path.join(outDir, file);
    await page.screenshot({ path: dest });
    const bytes = readFileSync(dest);
    if (bytes.length > BUDGET) throw new Error(`${file} is ${bytes.length} bytes, over the ${BUDGET} byte budget`);
    shots.push({
      file,
      caption,
      command: produced,
      sha256: createHash('sha256').update(bytes).digest('hex'),
      size_bytes: statSync(dest).size,
      viewport: `${VIEWPORT.width}x${VIEWPORT.height}`,
    });
    console.log(`ok   ${file} (${bytes.length} bytes)`);
  };

  const watch = (page, label) => {
    page.on('pageerror', (e) => errors.push(`${label}: ${e.message}`));
    page.on('console', (m) => m.type() === 'error' && errors.push(`${label}: ${m.text()}`));
  };

  // ---- /tui ---------------------------------------------------------------------------
  const openTui = async () => {
    const page = await context.newPage();
    watch(page, '/tui');
    await page.goto(`${base}/tui`);
    await page.getByText("type 'help' to begin.").waitFor({ timeout: 30000 });
    // The wall clock is the only time-varying status item; keep it out of the image.
    // The skyline is randomised per load and the caret blinks: hide both so images 01-12 are identical across runs (13-18 embed seed times / ids).
    await page.addStyleTag({
      content:
        '.status-group:last-child .status-item:last-child{visibility:hidden}' +
        '.ambient-skyline{display:none!important}*{animation:none!important;transition:none!important}',
    });
    return page;
  };
  const cmd = async (page, text, settle = 700) => {
    await page.keyboard.type(text);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(settle);
  };
  const openReplay = async (page, at) => {
    await cmd(page, `replay ${RUN} --at ${at}`, 300);
    await page.locator('.takeover-grid').waitFor({ timeout: 20000 });
    await page.waitForTimeout(300);
  };
  const press = async (page, keys) => {
    for (const k of keys) {
      await page.keyboard.press(k);
      await page.waitForTimeout(150);
    }
  };

  {
    const page = await openTui();
    await cmd(page, 'help');
    await shoot(page, '01-tui-help.png', 'The /tui terminal after boot, running `help`.', 'open /tui, type `help`');
    await page.close();
  }
  {
    const page = await openTui();
    await openReplay(page, 90);
    await shoot(page, '02-replay-viewer.png',
      'Replay viewer paused at 01:30: station grid with per-station counts, scrub bar and the run metrics line.',
      `/tui: \`replay ${RUN} --at 90\``);
    await page.close();
  }
  {
    const page = await openTui();
    await openReplay(page, 135);
    await press(page, ['p']);
    await shoot(page, '03-replay-panel-metrics.png', 'Replay insight panel `metrics` (first `p`).', `/tui: \`replay ${RUN} --at 135\`, key p`);
    await press(page, ['p']);
    await shoot(page, '04-replay-panel-tokens.png', 'Replay insight panel `tokens` (second `p`).', 'same session, key p x2');
    await press(page, ['p']);
    await shoot(page, '05-replay-panel-decisions.png', 'Replay insight panel `decisions` (third `p`).', 'same session, key p x3');
    await press(page, ['p']);
    await shoot(page, '06-replay-panel-latency.png', 'Replay insight panel `latency` (fourth `p`).', 'same session, key p x4');
    await page.close();
  }
  {
    // Seed order: happy, retry, failed, parked, boss, happy. `i` opens the inspector, `j` selects.
    const docs = [
      ['08-inspector-retry.png', 'Inspector on the retry document (gate_decision then retry events).', 'j x2', 2],
      ['09-inspector-failed.png', 'Inspector on the failed document (failure `run_budget`, cause `extraction_miss`).', 'j x3', 3],
      ['10-inspector-parked.png', 'Inspector on the parked document (parked in review, cause `judge_partial`).', 'j x4', 4],
      ['11-inspector-boss.png', 'Inspector on the boss-escalated document (arbiter and escalation events).', 'j x5', 5],
    ];
    const page = await openTui();
    await openReplay(page, 135);
    await press(page, ['i']);
    // Outbound links: `o` and `g` open the Phoenix project and the Grafana dashboard (noopener popups).
    // Phoenix/Grafana are not running here: stub their hosts so the popup requests are recorded, not refused.
    const opened = [];
    const stub = (route) => {
      opened.push(route.request().url());
      return route.fulfill({ status: 200, contentType: 'text/plain', body: 'stub' });
    };
    await context.route('http://localhost:6006/**', stub);
    await context.route('http://localhost:3000/**', stub);
    const popups = [];
    context.on('page', (p) => popups.push(p));
    await press(page, ['o', 'g']);
    await page.waitForTimeout(800);
    await Promise.all(popups.map((p) => p.close().catch(() => {})));
    console.log(`info outbound popups: ${JSON.stringify(opened)}`);
    await shoot(page, '07-outbound-links.png',
      'Inspector (no document selected) listing the Phoenix and Grafana links, after pressing `o` and `g` (each opens a new tab).',
      `/tui: \`replay ${RUN} --at 135\`, keys i, o, g; links come from GET /links`);
    let at = 0;
    for (const [file, caption, keys, n] of docs) {
      await press(page, Array(n - at).fill('j'));
      at = n;
      await shoot(page, file, caption, `/tui: \`replay ${RUN} --at 135\`, key i, ${keys}`);
    }
    await page.close();
  }
  {
    const page = await context.newPage();
    watch(page, '/links');
    await page.goto(`${base}/links`);
    await shoot(page, '12-links-json.png', 'Raw `GET /links`: the public base URLs the UI and viewer link out to.', `open ${base}/links`);
    await page.close();
  }
  {
    const page = await context.newPage();
    watch(page, '/ui');
    await page.goto(`${base}/ui`);
    await page.locator('#runs tbody a').first().waitFor({ timeout: 15000 }).catch(() => {
      throw new Error('/ui lists no eval run: run scripts/demo_seed_eval_runs.py first');
    });
    await page.waitForTimeout(500);
    await shoot(page, '13-ui-replay-links.png', '/ui Eval runs table with `replay`, `grafana` and `phoenix` links per run.', `open ${base}/ui (after demo_seed_eval_runs.py)`);
    await page.close();
  }
  {
    const page = await openTui();
    await cmd(page, 'ledger');
    await cmd(page, 'ledger verify');
    await shoot(page, '14-ledger.png', 'The `ledger` listing followed by `ledger verify` (hash-chain check).', 'open /tui, type `ledger` then `ledger verify`');
    await page.close();
  }
  {
    const page = await openTui();
    await cmd(page, 'ls');
    await cmd(page, 'runs');
    await shoot(page, '15-pipeline-ls-runs.png',
      'Pipeline commands `ls` (catalog documents, hostile filename shown as literal text) and `runs` (eval runs).',
      'open /tui, type `ls` then `runs`');
    await page.close();
  }

  // ---- sandbox Boss-mailbox dock (optional) ---------------------------------------------
  if (sandbox) {
    const api = `${sandbox}/api/sandbox/v1`;
    const mb = await (await fetch(`${api}/boss/mailbox?limit=1`)).json();
    if (!mb.count) {
      const r = await fetch(`${api}/inject`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ scenario_ids: ['A1_status_inquiry', 'E1_lookalike_wire_change'], wait: true }),
      });
      if (!r.ok) throw new Error(`sandbox inject failed: ${r.status}`);
    }
    const page = await context.newPage();
    watch(page, 'sandbox');
    await page.goto(`${sandbox}/ui`);
    await page.locator('#mbx-toggle').waitFor({ timeout: 15000 });
    await page.waitForTimeout(3000); // dock polls every 2 s
    await shoot(page, '16-sandbox-dock-badge.png', 'Sandbox UI with the docked Boss mailbox collapsed: unread badge on the toggle.', `${sandbox}/ui after injecting A1 + E1`);
    await page.locator('#mbx-toggle').click();
    await page.waitForTimeout(2500);
    await shoot(page, '17-sandbox-dock-open.png', 'Boss mailbox dock expanded: draft awaiting approval and the hostile_forward entry (pending Release / Quarantine).', 'same page, click the dock toggle');
    await page.getByRole('button', { name: 'Pending boss review' }).click();
    await page.waitForTimeout(800);
    await shoot(page, '18-sandbox-pending-review.png', 'Pending boss review tab: payment_fraud hostile_forward from E1 awaiting a Boss decision (nothing is decided by the capture).', 'same page, tab "Pending boss review"');
    await page.close();
  }

} finally {
  await browser.close();
}

let commit = 'unknown';
try {
  commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root }).toString().trim();
} catch {
  /* not a git checkout */
}
writeFileSync(
  path.join(outDir, 'manifest.json'),
  `${JSON.stringify({ source_commit: commit, tool: 'node scripts/demo_capture.mjs', images: shots }, null, 2)}\n`,
);
console.log(`wrote ${shots.length} images + manifest.json to ${path.relative(root, outDir) || '.'}`);
// The one by-design error: the /ui page probes an authenticated endpoint on the unauthenticated dev stack.
const unexpected = [...new Set(errors)].filter((e) => !/\b401\b|Unauthorized|Refused to apply inline style/i.test(e));
if (errors.length) {
  console.log('console/page errors seen while capturing:');
  for (const e of [...new Set(errors)]) console.log(`  ${unexpected.includes(e) ? 'UNEXPECTED' : 'expected (401 / known sandbox CSP)'} ${e}`);
}
if (unexpected.length) {
  console.error(`demo_capture: ${unexpected.length} unexpected page/console error(s); failing`);
  process.exitCode = 1;
}
