#!/usr/bin/env node
// Browser check for the /tui replay viewer, against a running API (scripts/tui_dev.sh up).
//   node scripts/tui_replay_check.mjs [session-id]      # default run:showcase-judge-arbiter
//   TUI_URL=http://127.0.0.1:8000 MAILROOM_API_TOKEN=secret node scripts/tui_replay_check.mjs
// Needs Playwright (PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs if it is not importable)
// and a Chromium (CHROMIUM_PATH=... if Playwright has not downloaded one). Exits non-zero on failure.
import process from 'node:process';

const base = (process.env.TUI_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '');
const token = process.env.MAILROOM_API_TOKEN || '';
const id = process.argv[2] || 'run:showcase-judge-arbiter';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${ok || !detail ? '' : ` — ${detail}`}`);
  if (!ok) failures.push(name);
};

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
try {
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  // Before `auth`, the boot probes answer 401 by design; that is not a page error.
  page.on('console', (m) => m.type() === 'error' && !(token && /\b401\b/.test(m.text())) && errors.push(m.text()));

  // With a token the deep link would run before auth, so authenticate and type the command.
  await page.goto(token ? `${base}/tui` : `${base}/tui#replay=${encodeURIComponent(id)}`);
  const grid = page.locator('.takeover-grid');
  if (token) {
    await page.getByText("type 'help' to begin.").waitFor({ timeout: 30000 }); // boot finished, input enabled
    await page.keyboard.type(`auth ${token}`);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(700); // let the auth command finish before the next command is typed
    await page.keyboard.type(`replay ${id}`);
    await page.keyboard.press('Enter');
  }
  await grid.waitFor({ timeout: 20000 });
  const head = async () => ((await grid.innerText()).split('\n')[0] || '');
  const text = async () => grid.innerText();
  const total = (line) => {
    const m = /\/ (\d\d):(\d\d\.\d)/.exec(line);
    return m ? Number(m[1]) * 60 + Number(m[2]) : NaN;
  };
  const secs = (line) => {
    const m = /(\d\d):(\d\d\.\d) \//.exec(line);
    return m ? Number(m[1]) * 60 + Number(m[2]) : NaN;
  };

  check('viewer opens as role=application', (await page.locator('.takeover').getAttribute('role')) === 'application');
  const t0 = secs(await head());
  await page.waitForTimeout(800);
  check('clock advances while playing', secs(await head()) > t0);
  await page.keyboard.press('Space');
  await page.waitForTimeout(200);
  const paused = await head();
  await page.waitForTimeout(500);
  check('space pauses', (await head()) === paused && /\|\|/.test(paused), paused);
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await page.waitForTimeout(150);
  check('arrow keys seek', secs(await head()) > secs(paused));
  await page.keyboard.press('5');
  await page.waitForTimeout(150);
  const jumped = await head();
  check('digit jumps through the run', secs(jumped) > 0.3 * total(jumped), jumped);
  await page.keyboard.press('j');
  await page.keyboard.press('i');
  await page.waitForTimeout(150);
  check('inspector panel opens', /─ inspector/.test(await text()));
  await page.keyboard.press('l');
  await page.waitForFunction(() => {
    const g = document.querySelector('.takeover-grid');
    return g && /─ ledger/.test(g.textContent) && !/loading…/.test(g.textContent);
  }, null, { timeout: 10000 });
  const led = await text();
  // Showcase runs have spans but no ledger rows; a run with a chain shows `chain ok` instead.
  check('ledger panel settles on an entry list and a chain state', /no ledger entries|#\d/.test(led) && /chain ok|verify failed|chain BROKEN/.test(led), led.split('\n').filter((l) => /ledger|chain|verify/.test(l)).join(' | '));
  check('no stray elements inside the viewer', (await page.locator('.takeover img, .takeover script, .takeover a').count()) === 0);
  if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT });
  await page.keyboard.press('q');
  await page.waitForTimeout(300);
  check('q ends the viewer', /ended/.test((await page.locator('.takeover').last().getAttribute('class')) || ''));
  await page.keyboard.type('help');
  const typed = await page.evaluate(() => (document.activeElement && document.activeElement.value) || '');
  check('keyboard is handed back to the prompt', typed === 'help', typed);
  check('no page errors', errors.length === 0, errors.join(' | '));
} finally {
  await browser.close();
}
if (failures.length) {
  console.error(`${failures.length} check(s) failed`);
  process.exit(1);
}
console.log('all checks passed');
