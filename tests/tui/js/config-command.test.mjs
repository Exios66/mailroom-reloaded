import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRegistry, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { registerConfig, configRows, MAX_ROWS } from '../../../src/mailroom_reloaded/api/tui/commands/config.js';
import { renderKv } from '../../../src/mailroom_reloaded/api/tui/terminal.js';

function harness(body, { status = 200, token = 'session-token', offline = false } = {}) {
  const calls = [];
  const output = [];
  const rendered = [];
  const document = { createElement() {
    return {
      children: [], value: '',
      appendChild(child) { this.children.push(child); },
      set textContent(value) { this.value = String(value); },
      get textContent() { return this.value + this.children.map((child) => child.textContent).join(''); },
      set innerHTML(value) { assert.fail(`unsafe HTML: ${value}`); },
    };
  } };
  const controller = new AbortController();
  const registry = createRegistry();
  registerConfig(registry, { fetchImpl: async (url, init) => {
    calls.push({ url, init });
    if (offline) throw new Error('private transport details');
    return new Response(JSON.stringify(body), { status });
  } });
  const ctx = {
    api: { authHeaders: () => token ? { Authorization: `Bearer ${token}` } : {} },
    signal: () => controller.signal,
    out: {
      line: (value) => output.push(String(value)),
      kv: (rows) => {
        rendered.push(renderKv(document, rows));
        rows.forEach(([key, value]) => output.push(`${key}: ${value}`));
      },
    },
  };
  return { calls, output, rendered, controller, registry, run: (line = 'config') => dispatch(registry, ctx, line) };
}

test('config has help and no command-to-command import', () => {
  const source = readFileSync(new URL('../../../src/mailroom_reloaded/api/tui/commands/config.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /from ['"]\.\//);
  assert.match(harness({}).registry.get('config').man, /SYNOPSIS[\s\S]*auth/);
});

test('config renders dotted settings using the session token', async () => {
  const app = harness({ provider: { name: 'mock' }, api: { token: 'set' }, taxonomy: { classes: ['invoice', 'letter'] } });
  await app.run();
  assert.equal(app.calls[0].url, '/v1/config');
  assert.equal(app.calls[0].init.headers.Authorization, 'Bearer session-token');
  assert.deepEqual(app.output, ['provider.name: mock', 'api.token: set', 'taxonomy.classes: invoice, letter']);
});

test('secret scalars, arrays and nested objects never reach rendered output', async () => {
  const secret = 'env-secret-fixture-DO-NOT-RENDER';
  const app = harness({
    api: { token: secret }, provider: { api_key: [secret] },
    credentials: { [secret]: secret }, tracing: { otlp_endpoint: secret },
    storage: { anchor_url: secret }, [`${'x'.repeat(125)}_secret`]: secret,
    password: true, empty_key: null, safe_key: 'unset',
  });
  await app.run();
  const domText = app.rendered.map((node) => node.textContent).join('\n');
  assert.ok(!domText.includes(secret));
  assert.match(domText, /withheld/);
  assert.match(domText, /safe_keyunset/);
});

test('config output is text-only, bounded, and rejects malformed payloads', () => {
  assert.equal(configRows({ value: '<img src=x onerror=alert(1)>' }).rows[0][1], '<img src=x onerror=alert(1)>');
  for (const value of [null, [], 'private', 1]) assert.equal(configRows(value).invalid, true);
  const bounded = configRows(Object.fromEntries(Array.from({ length: MAX_ROWS + 1 }, (_, index) => [`field${index}`, index])));
  assert.equal(bounded.rows.length, MAX_ROWS);
  assert.equal(bounded.total, MAX_ROWS + 1);
});

for (const token of ['', 'wrong-token']) {
  test(`config 401 gives auth hint (${token || 'no token'}) without exposing error bodies`, async () => {
    const app = harness({ detail: 'secret-error' }, { status: 401, token });
    await app.run();
    assert.match(app.output.join(''), /401.*auth/);
    assert.ok(!app.output.join('').includes('secret-error'));
  });
}

test('config handles backend-down, invalid responses, bad arguments and cancellation', async () => {
  const offline = harness({}, { offline: true });
  await offline.run();
  assert.match(offline.output.join(''), /api unreachable/);
  const malformed = harness([]);
  await malformed.run();
  assert.match(malformed.output.join(''), /bad response/);
  for (const line of ['config extra', 'config --secret']) {
    const invalid = harness({});
    await invalid.run(line);
    assert.equal(invalid.calls.length, 0);
    assert.match(invalid.output.join(''), /usage/);
  }
  const cancelled = harness({ provider: 'mock' });
  cancelled.controller.abort();
  await cancelled.run();
  assert.equal(cancelled.output.length, 0);
});
