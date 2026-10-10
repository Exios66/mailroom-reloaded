#!/usr/bin/env node
// Checklist point 6a: kill the server mid-session (scripts/tui_dev.sh down) and run a command.
// Needs the dev stack up (no token). Prints "P6a PASS|FAIL ...", then P6c for a cold reload.
import { execFileSync } from 'node:child_process';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
const devScript = fileURLToPath(new URL('../../../scripts/tui_dev.sh', import.meta.url));
const base = process.env.TUI_URL || 'http://127.0.0.1:8000';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const b = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
try {
  const p = await (await b.newContext({ viewport: { width: 1200, height: 800 } })).newPage();
  await p.goto(`${base}/tui`);
  await p.getByText("type 'help' to begin.").waitFor({ timeout: 30000 });
  execFileSync(devScript, ['down'], { stdio: 'inherit' });
  for (const c of ['health', 'ls']) { await p.keyboard.type(c); await p.keyboard.press('Enter'); await p.waitForTimeout(1500); }
  const t = await p.locator('#output').innerText();
  const lines = t.split('\n').filter((l) => /health|ls|unreachable|offline|no api|connection/i.test(l)).slice(-6);
  console.log(`P6a ${/unreachable|no api connection|offline/i.test(t.split("$ health").pop()) ? 'PASS' : 'FAIL'} lines=${JSON.stringify(lines)}`);
  try { await p.reload({ timeout: 5000 }); console.log('P6c FAIL page loaded from dead server'); }
  catch (e) { console.log(`P6c PASS cold reload against dead server cannot load: ${String(e.message).split('\n')[0]}`); }
} finally {
  await b.close();
}
