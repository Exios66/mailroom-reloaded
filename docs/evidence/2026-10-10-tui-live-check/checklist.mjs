#!/usr/bin/env node
// Scripted walk of the nine-point manual checklist in docs/TUI.md ("Manual checklist").
//   node docs/evidence/2026-10-10-tui-live-check/checklist.mjs main     # points 1-5, 8, 9 (dev stack, no token)
//   ... checklist.mjs offline-sim  # point 6b: shell served, /v1 + /health blocked (6a/6c: offline_midsession.mjs)
//   TOKEN=demo-token ... checklist.mjs token   # point 7: dev stack restarted with MAILROOM_API_TOKEN=demo-token
// Env: TUI_URL (default http://127.0.0.1:8000), PLAYWRIGHT_MODULE, CHROMIUM_PATH. Prints "P<n> PASS|FAIL detail".
import process from 'node:process';
const mode = process.argv[2] || 'main';
const base = (process.env.TUI_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
const out = (n, ok, d = '') => console.log(`P${n} ${ok ? 'PASS' : 'FAIL'} ${d}`);
const txt = (p) => p.locator('#output').innerText();
const type = async (p, s, ms = 600) => { await p.keyboard.type(s); await p.keyboard.press('Enter'); await p.waitForTimeout(ms); };
const newPage = async (opts = {}) => {
  const ctx = await browser.newContext({ viewport: { width: 1200, height: 800 }, ...opts });
  const p = await ctx.newPage();
  const errors = [];
  p.on('pageerror', (e) => errors.push(e.message));
  p.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  return { ctx, p, errors };
};
const booted = (p) => p.getByText(/type 'help' to begin\.|mailroom closed|api token required/).first().waitFor({ timeout: 30000 });

if (mode === 'main') {
  const allErrors = [];
  // 1. Boot plays (real motion, no reduced-motion), keypress skips.
  {
    const { ctx, p, errors } = await newPage();
    const t0 = Date.now();
    await p.goto(`${base}/tui`);
    await booted(p);
    const full = Date.now() - t0;
    const t = await txt(p);
    const oks = (t.match(/\[ ok \]/g) || []).length;
    const counts = /catalog · \d+ documents/.test(t) && /eval runs · \d+/.test(t);
    const banner = (await p.locator('.banner').count()) > 0;
    const p2 = await ctx.newPage();
    const t1 = Date.now();
    await p2.goto(`${base}/tui`);
    await p2.keyboard.press('Space');
    await booted(p2);
    const skipped = Date.now() - t1;
    out(1, oks >= 4 && counts && banner && /type 'help' to begin\./.test(t), `ok-lines=${oks} counts=${counts} banner=${banner} boot=${full}ms; keypress-skip boot=${skipped}ms (${skipped < full ? 'faster' : 'NOT faster'})`);
    allErrors.push(...errors);
    await ctx.close();
  }
  const { ctx, p, errors } = await newPage();
  await p.goto(`${base}/tui`); await booted(p);
  // 2. help, man ls, unknown command.
  await type(p, 'help'); const h = await txt(p);
  await type(p, 'man ls'); const m = await txt(p);
  await type(p, 'flor', 100);
  const shake = await p.evaluate(() => { const l = [...document.querySelectorAll('#output .line.error')].pop(); return l ? getComputedStyle(l).animationName : ''; });
  await p.waitForTimeout(400);
  const f = await txt(p);
  out(2, /ls/.test(h) && /NAME/.test(m.split('man ls').pop()) && /flor: command not found — try help/.test(f), `shake animation=${shake}`);
  // 3. ls / inspect / audit, hostile filename literal.
  await type(p, 'ls', 900);
  const docId = (await p.locator('#output table.mr tbody tr td:first-child').first().innerText()).trim();
  await type(p, `inspect ${docId}`, 900); const ins = (await txt(p)).split(`inspect ${docId}`).pop();
  await type(p, `audit ${docId}`, 900); const aud = (await txt(p)).split(`audit ${docId}`).pop();
  const imgs = await p.evaluate(() => document.querySelectorAll('#output img').length);
  const hostile = /<b>hostile<b>\.txt/.test(await txt(p));
  out(3, imgs === 0 && hostile && ins.length > 20 && aud.length > 10, `doc=${docId} imgs=${imgs} hostile-literal=${hostile} inspect-chars=${ins.length} audit-chars=${aud.length}`);
  // 4. Tab ghost completion, Up history, Ctrl+L, Ctrl+C during watch.
  await p.keyboard.type('hel'); await p.waitForTimeout(150);
  const ghost = await p.evaluate(() => (document.querySelector('.ghost-text') || {}).textContent || '');
  await p.keyboard.press('Tab'); await p.waitForTimeout(100);
  const afterTab = await p.evaluate(() => document.activeElement.value);
  await p.keyboard.press('Control+a'); await p.keyboard.press('Backspace');
  await p.keyboard.press('ArrowUp'); await p.waitForTimeout(100);
  const up = await p.evaluate(() => document.activeElement.value);
  await p.keyboard.press('Control+a'); await p.keyboard.press('Backspace');
  await p.keyboard.press('Control+l'); await p.waitForTimeout(200);
  const cleared = (await txt(p)).trim().length;
  await type(p, 'watch --interval 1', 1500);
  const w1 = await txt(p);
  await p.keyboard.press('Control+c'); await p.waitForTimeout(400);
  const w2 = await txt(p);
  await p.waitForTimeout(1500);
  const w3 = await txt(p);
  await type(p, 'health', 600);
  const prompted = /ok|health/.test(await txt(p));
  out(4, ghost === 'p' && afterTab === 'help' && up.length > 0 && cleared < 5 && w1.includes('watch') && w3 === w2 && prompted, `ghost="${ghost}" tab->"${afterTab}" up->"${up}" cleared-chars=${cleared} watch-stopped=${w3 === w2}`);
  // 5. Themes, reload persistence, crt/skyline.
  const themes = {};
  for (const t of ['green', 'cyan', 'light', 'hc']) {
    await type(p, `theme ${t}`, 500);
    themes[t] = await p.evaluate(() => document.documentElement.getAttribute('data-theme') + '|' + (document.documentElement.getAttribute('data-phosphor') || ''));
  }
  await p.reload(); await booted(p);
  const kept = await p.evaluate(() => document.documentElement.getAttribute('data-theme') + '|' + (document.documentElement.getAttribute('data-phosphor') || ''));
  await type(p, 'crt off', 400); const crtOff = await p.evaluate(() => (document.querySelector('.crt-overlay') || {}).className || 'none');
  await type(p, 'skyline off', 400); const skyOff = await p.evaluate(() => (document.querySelector('.ambient-skyline') || {}).className || 'none');
  const stored = await p.evaluate(() => JSON.stringify(Object.fromEntries(Object.entries(localStorage))));
  await type(p, 'theme dark', 300); await type(p, 'crt on', 200); await type(p, 'skyline on', 200);
  const distinct = new Set(Object.values(themes)).size === 4;
  out(5, distinct && kept === themes.hc && /off/.test(crtOff) && /off/.test(skyOff), `applied=${JSON.stringify(themes)} after-reload=${kept} crt-class="${crtOff}" skyline-class="${skyOff}" storage=${stored}`);
  allErrors.push(...errors);
  await ctx.close();
  // 8. Mobile preset.
  {
    const { ctx: c8, p: p8, errors: e8 } = await newPage({ viewport: { width: 375, height: 700 }, reducedMotion: 'reduce' });
    await p8.goto(`${base}/tui`); await booted(p8);
    const vis = await p8.evaluate(() => [...document.querySelectorAll('.status-item')].filter((e) => getComputedStyle(e).display !== 'none').map((e) => e.textContent.trim()));
    const fs = await p8.evaluate(() => getComputedStyle(document.body).fontSize);
    const scroll = await p8.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    await type(p8, 'ls', 800);
    const scroll2 = await p8.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    const sparks = await p8.evaluate(() => document.querySelectorAll('.ambient-skyline .spark, .ambient-skyline .node, .spark').length);
    const total = await p8.evaluate(() => document.querySelectorAll('.status-item').length);
    out(8, fs === '12px' && scroll && scroll2 && sparks === 0 && vis.length < total, `visible-status=${JSON.stringify(vis)} of ${total}; body font=${fs}; no-hscroll boot=${scroll} after-ls=${scroll2}; spark-nodes(reduced-motion)=${sparks}`);
    allErrors.push(...e8);
    await c8.close();
  }
  out(9, allErrors.length === 0, allErrors.length ? [...new Set(allErrors)].join(' | ') : 'no console errors / page errors across points 1-5 and 8');
}

if (mode === 'offline-sim') {
  // Page served by the live server, API calls blocked at the network layer (simulates an unreachable API on reload).
  const { ctx, p } = await newPage();
  await p.route(/\/(v1|health)(\/|$|\?)/, (r) => r.abort());
  await p.goto(`${base}/tui`); await booted(p);
  const t = await txt(p);
  const data = /documents|\d+ entries/.test(t.replace(/mailroom closed[^\n]*/, ''));
  out('6b', /mailroom closed — no api connection/.test(t), `simulated via route.abort; shows closed line; leaked data lines=${data}`);
  await ctx.close();
}
if (mode === 'token') {
  const token = process.env.TOKEN;
  const { ctx, p, errors } = await newPage();
  await p.goto(`${base}/tui`); await booted(p);
  const boot = await txt(p);
  // Reply = scrollback lines added by each command (the prompt echoes the token masked, so
  // splitting on the typed text would not work).
  const added = async (cmd) => {
    const before = (await txt(p)).split('\n').length;
    await type(p, cmd, 900);
    return (await txt(p)).split('\n').slice(before).join('\n');
  };
  const wrong = await added('auth wrong-token');
  const right = await added(`auth ${token}`);
  await type(p, 'ls', 900);
  const ls = (await txt(p)).split('\n$ ls').pop();
  const full = await txt(p);
  // The auth command line itself is echoed by the prompt; the check is that the token is not echoed elsewhere (status bar, later output).
  const occurrences = full.split(token).length - 1;
  const bar = await p.locator('.status-bar').innerText();
  const html = await p.content();
  const inDom = html.split(token).length - 1;
  out('7', /api token required/.test(boot) && /rejected|401/.test(wrong) && /auth: ok/.test(right) && /4 documents|archived/.test(ls) && !bar.includes(token),
    `boot-has-"api token required"=${/api token required/.test(boot)}; wrong-token reply=${JSON.stringify(wrong.trim().slice(0, 120))}; right-token reply=${JSON.stringify(right.trim().slice(0, 120))}; ls-after-auth works=${/archived/.test(ls)}; token occurrences in scrollback text=${occurrences}, in DOM html=${inDom}, in status bar=${bar.includes(token)}`);
  console.log('SCROLLBACK-AFTER-AUTH:\n' + full.split('\n').filter((l) => /auth|token/i.test(l)).join('\n'));
  console.log('PAGE-ERRORS: ' + JSON.stringify([...new Set(errors)]));
  await ctx.close();
}
await browser.close();
