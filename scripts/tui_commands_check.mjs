#!/usr/bin/env node
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const base = new URL(process.env.TUI_URL || 'http://127.0.0.1:8000');
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(base.hostname), 'TUI_URL must point to the local mock stack');
assert.ok(['http:', 'https:'].includes(base.protocol) && !base.username && !base.password, 'TUI_URL must be credential-free HTTP(S)');
assert.ok(!process.env.MAILROOM_API_TOKEN, 'Run without MAILROOM_API_TOKEN; this check never authenticates');
const origin = base.origin;
const shotDir = process.env.SHOT_DIR ? path.resolve(root, process.env.SHOT_DIR) : null;
assert.notEqual(shotDir, path.join(root, 'docs/demo'), 'Use a separate SHOT_DIR, such as docs/demo/p6b1-4, not the release gallery');
const sourceCommit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
const viewport = { width: 1200, height: 800 };
const budget = 400 * 1024;
const session = 'run:7e57d0c0ffee';
const exportPath = `/v1/replay/sessions/${encodeURIComponent(session)}/export`;
const images = [];
const commands = [];
const pageErrors = [];
const timestamp = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function until(predicate, label, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (!(await predicate())) {
    assert.ok(Date.now() < deadline, `Timed out: ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
let exportEvidence;
try {
  const context = await browser.newContext({
    viewport, deviceScaleFactor: 1, colorScheme: 'dark', reducedMotion: 'reduce',
    acceptDownloads: true,
  });
  await context.route('**/*', (route) => new URL(route.request().url()).origin === origin
    ? route.continue()
    : route.fulfill({ status: 200, contentType: 'text/plain', body: '' }));
  const configResponse = await context.request.get(`${origin}/v1/config`, { timeout: 10000 });
  assert.equal(configResponse.status(), 200, 'GET /v1/config needs a running no-token mock stack');
  const config = await configResponse.json();
  assert.equal(config.api?.token, 'unset', 'Screenshots require a no-token mock stack');
  const secretPattern = /key|token|secret|pass|credential|dsn/i;
  const secretFields = [];
  const visit = (value, prefix = '') => {
    if (secretPattern.test(prefix)) {
      assert.ok(value === 'set' || value === 'unset', `Config field ${prefix} must be redacted`);
      secretFields.push([prefix, value]);
    } else if (value && typeof value === 'object') {
      for (const [key, child] of Object.entries(value)) visit(child, prefix ? `${prefix}.${key}` : key);
    }
  };
  visit(config);
  const exportResponse = await context.request.get(`${origin}${exportPath}`, { timeout: 20000 });
  assert.equal(exportResponse.status(), 200, `Seed ${session} before running this check`);
  const exportBytes = await exportResponse.body();
  assert.ok(exportBytes.length > 0, 'API export must not be empty');
  JSON.parse(exportBytes.toString('utf8'));

  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  page.on('pageerror', (error) => pageErrors.push(error.message));
  let readyRequests = 0;
  let readyResponses = 0;
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/ready') readyRequests++;
  });
  page.on('response', (response) => {
    if (new URL(response.url()).pathname === '/ready') readyResponses++;
  });
  await page.goto(`${origin}/tui`);
  await page.getByText("type 'help' to begin.", { exact: true }).waitFor({ timeout: 30000 });
  const input = page.getByRole('textbox', { name: 'terminal input' });
  await page.waitForFunction(() => document.querySelector('[aria-label="terminal input"]')?.readOnly === false);
  await page.addStyleTag({ content:
    '.status-group:last-child .status-item:last-child{visibility:hidden}' +
    '.ambient-skyline{display:none!important}*{animation:none!important;transition:none!important}',
  });
  const output = page.locator('#output');
  const text = () => output.innerText();
  const enter = async (command) => {
    await input.focus();
    await page.keyboard.press('Control+l');
    await input.fill(command);
    commands.push(command);
    await input.press('Enter');
  };
  const noCommandErrors = async () => {
    const errors = (await output.locator('.line.error').allTextContents())
      .filter((line) => line !== 'ready — down');
    assert.deepEqual(errors, [], 'Command printed an error');
    assert.deepEqual(pageErrors, [], 'Browser page errors');
  };
  const capture = async (file, command) => {
    await noCommandErrors();
    if (!shotDir) return;
    mkdirSync(shotDir, { recursive: true });
    await output.evaluate((element) => { element.scrollTop = 0; });
    const bytes = await page.screenshot({ path: path.join(shotDir, file), animations: 'disabled' });
    assert.ok(bytes.length <= budget, `${file} exceeds ${budget} bytes`);
    images.push({
      file, command, captured_at: timestamp(), sha256: digest(bytes), size_bytes: bytes.length,
      viewport: `${viewport.width}x${viewport.height}`,
    });
    console.log(`ok   ${file} (${bytes.length} bytes)`);
  };

  await enter('help');
  await until(async () => (await text()).includes("type 'man <command>' for details."), 'help');
  const helpNames = await output.locator('.kv b').allTextContents();
  for (const name of ['links', 'ready', 'config', 'replay', 'health', 'watch']) assert.ok(helpNames.includes(name), `${name} missing from help`);
  await noCommandErrors();
  console.log('ok   help lists commands');

  await enter('links');
  await until(async () => (await text()).includes('phoenix_project'), 'links table');
  assert.deepEqual(await output.locator('table th').allTextContents(), ['link', 'value']);
  const linkNames = await output.locator('table tbody tr td:first-child').allTextContents();
  for (const name of ['public_url', 'phoenix_url', 'grafana_url', 'phoenix_project']) assert.ok(linkNames.includes(name));
  await capture('links.png', 'links');
  console.log('ok   links table');

  const assertReady = async () => {
    await until(async () => (await output.locator('table tbody tr').count()) > 0, 'readiness components');
    assert.match(await text(), /ready — (ok|degraded|down)/);
    assert.deepEqual(await output.locator('table th').allTextContents(), ['component', 'status', 'latency']);
    const rows = await output.locator('table tbody tr').evaluateAll((elements) => elements.map((element) =>
      [...element.querySelectorAll('td')].map((cell) => ({ text: cell.textContent, cls: cell.className }))));
    const classes = { ok: 'success', degraded: 'warn', down: 'error', unconfigured: 'dim' };
    for (const row of rows) {
      assert.ok(Object.hasOwn(classes, row[1].text), 'Unknown readiness status');
      assert.equal(row[1].cls, classes[row[1].text], 'Readiness status colour');
      assert.match(row[2].text, /^\d+(?:\.\d+)? ms$/, 'Readiness latency');
    }
    await noCommandErrors();
  };
  await enter('ready');
  await assertReady();
  await capture('ready.png', 'ready');
  console.log('ok   ready components, colours and latencies');
  await enter('health --all');
  await assertReady();
  console.log('ok   health --all alias');

  const previousResponses = readyResponses;
  await enter('watch ready --interval 1');
  await assertReady();
  await until(() => readyResponses >= previousResponses + 2, 'watch ready re-polls');
  await input.focus();
  await page.keyboard.press('Control+c');
  commands.push('Ctrl+C');
  await until(async () => (await text()).includes('ready: watch stopped'), 'watch cancellation');
  const stoppedRequests = readyRequests;
  const stoppedOutput = await text();
  await new Promise((resolve) => setTimeout(resolve, 1300));
  assert.equal(readyRequests, stoppedRequests, 'Readiness polling continues after Ctrl+C');
  assert.equal(await text(), stoppedOutput, 'Readiness output changes after Ctrl+C');
  await noCommandErrors();
  console.log('ok   watch ready re-polls and Ctrl+C cancels further requests');

  await enter('config');
  await until(async () => (await output.locator('.kv b').allTextContents()).includes('api.token'), 'config table');
  const configRows = await output.locator('.kv').evaluate((element) => [...element.querySelectorAll('b')]
    .map((key) => [key.textContent, key.nextElementSibling?.textContent]));
  for (const [key, value] of secretFields) assert.ok(configRows.some((row) => row[0] === key && row[1] === value), `Missing redacted field ${key}`);
  assert.ok(configRows.some(([key]) => key === 'provider.name'), 'Config provider missing');
  await capture('config.png', 'config');
  console.log('ok   config renders set/unset secret markers without authentication');

  const exportCommand = `replay export ${session} --out proof.json`;
  const downloadEvent = page.waitForEvent('download');
  await enter(exportCommand);
  const download = await downloadEvent;
  assert.equal(await download.failure(), null, 'Browser download failed');
  assert.equal(download.suggestedFilename(), 'proof.json');
  const downloadPath = await download.path();
  assert.ok(downloadPath, 'Browser download has no file');
  const downloaded = readFileSync(downloadPath);
  assert.deepEqual(downloaded, exportBytes, 'Browser download differs from GET export bytes');
  await until(async () => (await text()).includes('downloaded proof.json'), 'export completion');
  exportEvidence = { session, api_path: exportPath, filename: 'proof.json', sha256: digest(downloaded), size_bytes: downloaded.length };
  await capture('replay-export.png', exportCommand);
  console.log(`ok   replay export downloaded proof.json (${downloaded.length} bytes), identical to API export`);
  assert.deepEqual(pageErrors, [], 'Browser page errors');
  console.log('ok   no pageerrors');
} finally {
  await browser.close();
}

if (shotDir) {
  assert.equal(images.length, 4, 'Expected one image per command');
  const manifest = {
    source_commit: sourceCommit, captured_at: timestamp(), tool: 'node scripts/tui_commands_check.mjs',
    base_url: origin, commands, export: exportEvidence, images,
  };
  writeFileSync(path.join(shotDir, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
  console.log(`wrote 4 images + manifest.json to ${path.relative(root, shotDir)}`);
}
console.log('all command checks passed');
