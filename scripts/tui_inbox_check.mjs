#!/usr/bin/env node
// Browser check for the /tui `inbox` command and the sandbox UI deep link it opens.
//   node scripts/tui_inbox_check.mjs
// Starts the dev stack (scripts/tui_dev.sh up, unauthenticated) and an offline sandbox, drives /tui and
// stops everything it started on exit. To reuse a stack that is already running, set both
//   TUI_URL=http://127.0.0.1:8000 SANDBOX_URL=http://127.0.0.1:8100
// (the API must have been started with MAILROOM_SANDBOX_URL pointing at that sandbox, and the sandbox
// mailbox should be empty); nothing is started or stopped then.
// Other environment: TUI_API_PORT (8000), SANDBOX_PORT (8100), PLAYWRIGHT_MODULE, CHROMIUM_PATH, SHOT=dir.
// Needs Playwright and a Chromium. Never types or sends a token. Exits non-zero on failure.
import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const apiPort = process.env.TUI_API_PORT || '8000';
const sbPort = process.env.SANDBOX_PORT || '8100';
const reuse = Boolean(process.env.TUI_URL && process.env.SANDBOX_URL);
const base = (process.env.TUI_URL || `http://127.0.0.1:${apiPort}`).replace(/\/+$/, '');
const sandbox = (process.env.SANDBOX_URL || `http://127.0.0.1:${sbPort}`).replace(/\/+$/, '');
const EXPECT = `${sandbox}/ui#tab=messages&mailbox=open&role=correspondent`;
const sbApi = `${sandbox}/api/sandbox/v1`;

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${ok || !detail ? '' : ` — ${detail}`}`);
  if (!ok) failures.push(name);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitHttp(url, label, ms = 90000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      /* not up yet */
    }
    await sleep(500);
  }
  throw new Error(`${label} did not become healthy at ${url}`);
}

let sbProc = null;
let sbDir = null;
let startedStack = false;
const cleanup = () => {
  if (sbProc && sbProc.exitCode === null) {
    try {
      process.kill(-sbProc.pid, 'SIGTERM');
    } catch {
      /* already gone */
    }
  }
  if (sbDir) rmSync(sbDir, { recursive: true, force: true });
  sbDir = null;
  if (startedStack) {
    startedStack = false;
    try {
      execFileSync('scripts/tui_dev.sh', ['down'], { cwd: root, stdio: 'ignore', env: { ...process.env, UV_OFFLINE: '1' } });
    } catch {
      /* best effort */
    }
  }
};
process.on('exit', cleanup);
for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => process.exit(130));

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
let browser = null;
try {
  if (!reuse) {
    const env = { ...process.env, UV_OFFLINE: '1', TUI_API_PORT: apiPort, MAILROOM_SANDBOX_URL: sandbox, MAILROOM_API_TOKEN: '' };
    delete env.MAILROOM_API_TOKEN;
    execFileSync('scripts/tui_dev.sh', ['up'], { cwd: root, stdio: 'ignore', env });
    startedStack = true;
    sbDir = mkdtempSync(path.join(tmpdir(), 'inbox-check-'));
    mkdirSync(sbDir, { recursive: true });
    sbProc = spawn('uv', ['run', '--extra', 'dev', '--extra', 'sandbox', 'mailroom', 'sandbox', 'serve',
      '--host', '127.0.0.1', '--port', sbPort, '--content', 'smoke', '--data-dir', sbDir], {
      cwd: root, env: { ...env, OTEL_SDK_DISABLED: 'true' }, stdio: 'ignore', detached: true,
    });
    await waitHttp(`${base}/health`, 'api');
    await waitHttp(`${sandbox}/health`, 'sandbox');
  }
  const mb0 = await (await fetch(`${sbApi}/boss/mailbox?limit=1`)).json();
  check('sandbox mailbox starts empty', mb0.count === 0, JSON.stringify(mb0).slice(0, 120));

  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
  const context = await browser.newContext({ viewport: { width: 1200, height: 800 }, colorScheme: 'dark' });
  const errors = [];
  const urls = new Set();
  const track = (p) => {
    urls.add(p.url());
    p.on('framenavigated', (f) => urls.add(f.url()));
    p.on('request', (r) => urls.add(r.url()));
    p.on('pageerror', (e) => errors.push(e.message));
    p.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  };
  context.on('page', track);
  // Record window.open calls (and let them through) so a deep link can be asserted without a gesture.
  await context.addInitScript(() => {
    const real = window.open.bind(window);
    window.__opens = [];
    window.open = (...a) => (window.__opens.push(String(a[0])), real(...a));
  });
  const boot = async (page) => page.getByText("type 'help' to begin.").waitFor({ timeout: 30000 });
  const run = async (page, text) => {
    await page.keyboard.type(text);
    await page.keyboard.press('Enter');
  };

  // ---- `inbox` opens a popup at the deep link ----------------------------------------------
  const page = await context.newPage();
  await page.goto(`${base}/tui`);
  await boot(page);
  const popupP = context.waitForEvent('page', { timeout: 15000 });
  await run(page, 'inbox');
  const popup = await popupP;
  await popup.waitForLoadState('domcontentloaded');
  await popup.locator('#mbx-toggle').waitFor({ timeout: 15000 });
  check('inbox opens the sandbox deep link', popup.url() === EXPECT, popup.url());
  check('popup is at the sandbox origin', new URL(popup.url()).origin === new URL(sandbox).origin, popup.url());
  await page.waitForTimeout(500);
  const term = await page.locator('body').innerText();
  check('terminal prints the URL', term.includes(EXPECT));
  check('terminal prints the token hint', /never put in the link/.test(term));

  const active = popup.locator('#tabs button.active');
  check('Ingress queue tab is active', ((await active.innerText()) || '').trim() === 'Ingress queue', await active.allInnerTexts().then((t) => t.join('|')));
  const body = popup.locator('#mbx-body');
  await popup.waitForTimeout(500);
  check('Boss mailbox dock is open', await body.isVisible());
  const filter = popup.locator('.mbx-filter');
  check('correspondent filter line is shown', /role=correspondent/.test((await filter.first().innerText().catch(() => '')) || ''));
  check('dock starts with no entries', (await popup.locator('.mbx-entry').count()) === 0);

  // ---- live update without a reload (2 s poll) ---------------------------------------------
  await popup.evaluate(() => { window.__noReload = true; });
  const t0 = Date.now();
  const inj = await fetch(`${sbApi}/inject`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ scenario_ids: ['A1_status_inquiry'], wait: true }),
  });
  check('scenario injected', inj.ok, String(inj.status));
  let appeared = false;
  try {
    await popup.locator('.mbx-entry').first().waitFor({ timeout: 4000 });
    appeared = true;
  } catch {
    /* reported below */
  }
  check('new mailbox row appears within 4 s', appeared, `${Date.now() - t0} ms`);
  check('without a reload', (await popup.evaluate(() => window.__noReload === true)) === true);
  // The Ingress queue table itself must follow mail injected by another client.
  let tableRows = 0;
  for (let i = 0; i < 8 && !tableRows; i++) {
    tableRows = await popup.locator('table tr.click').count();
    if (!tableRows) await popup.waitForTimeout(500);
  }
  check('Ingress table shows the injected mail without a reload', tableRows > 0, `${tableRows} rows`);
  if (process.env.SHOT) await popup.screenshot({ path: path.join(process.env.SHOT, 'inbox-popup.png') });
  await popup.close();

  // ---- `inbox --print` opens nothing --------------------------------------------------------
  const opensBefore = await page.evaluate(() => window.__opens.length);
  let extra = null;
  const onPage = (p) => { extra = p; };
  context.on('page', onPage);
  await run(page, 'inbox --print');
  await page.waitForTimeout(1500);
  context.off('page', onPage);
  check('inbox --print opens no tab', extra === null, extra ? extra.url() : '');
  check('inbox --print makes no window.open call', (await page.evaluate(() => window.__opens.length)) === opensBefore);
  check('inbox --print still prints the URL', (await page.locator('body').innerText()).split(EXPECT).length >= 3);
  await page.close();

  // ---- #inbox deep link in a fresh page -----------------------------------------------------
  const fresh = await context.newPage();
  const freshPopup = context.waitForEvent('page', { timeout: 15000 }).catch(() => null);
  await fresh.goto(`${base}/tui#inbox`);
  await boot(fresh);
  const fp = await freshPopup;
  const calls = await fresh.evaluate(() => window.__opens);
  check('/tui#inbox calls window.open with the deep link', calls.length === 1 && calls[0] === EXPECT, JSON.stringify(calls));
  check('/tui#inbox opens a popup', Boolean(fp), 'no popup');
  if (fp) {
    await fp.waitForLoadState('domcontentloaded');
    check('fresh popup is at the deep link', fp.url() === EXPECT, fp.url());
    await fp.close();
  }
  await fresh.close();

  // ---- hygiene -----------------------------------------------------------------------------
  const bad = [...urls].filter((u) => /[?&#](access_)?token=|[?&#&](api_?key|key|auth|bearer|secret|password)=|^[a-z]+:\/\/[^/]*@/i.test(u));
  check('no URL contains a token or credential', bad.length === 0, bad.join(' | '));
  const inboxUrls = [...urls].filter((u) => u.startsWith(`${sandbox}/ui`));
  check('sandbox page URLs carry only the fragment', inboxUrls.length > 0 && inboxUrls.every((u) => !u.includes('?')), inboxUrls.join(' | '));
  // The sandbox UI's inline style trips its own CSP in Chromium; demo_capture.mjs treats it as known.
  const unexpected = [...new Set(errors)].filter((e) => !/Refused to apply inline style/i.test(e));
  check('no unexpected page errors', unexpected.length === 0, unexpected.join(' | '));
} catch (e) {
  check('check script ran to completion', false, e && e.message ? e.message : String(e));
} finally {
  if (browser) await browser.close();
}
cleanup();
if (failures.length) {
  console.error(`${failures.length} check(s) failed`);
  process.exit(1);
}
console.log('all checks passed');
