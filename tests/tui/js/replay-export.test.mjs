import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRegistry, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { registerReplay } from '../../../src/mailroom_reloaded/api/tui/commands/replay.js';
import { createHttp } from '../../../src/mailroom_reloaded/api/tui/lib/http.js';

const bytes = new TextEncoder().encode('{ "version":"replay/v1", "session":{"id":"run:r1"}, "events":[], "label":"café 🚀" }\n');

test('replay command does not import other command modules', () => {
  const source = readFileSync(new URL('../../../src/mailroom_reloaded/api/tui/commands/replay.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /from ['"]\.\//);
});

function harness({ status = 200, token = 'export-token', offline = false, clickFails = false, abortBody = false } = {}) {
  const calls = [];
  const messages = [];
  const downloads = [];
  const revoked = [];
  const controller = new AbortController();
  const anchor = { remove() { this.removed = true; }, click() {
    if (clickFails) throw new Error('sensitive browser detail');
    downloads.push({ href: this.href, filename: this.download });
  } };
  let blob;
  const registry = createRegistry();
  registerReplay(registry, {
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      if (offline) throw new Error('private network detail');
      const response = new Response(status === 200 ? bytes : '{"detail":"private-error"}', { status });
      if (abortBody) {
        const original = response.blob.bind(response);
        response.blob = async () => { controller.abort(); return original(); };
      }
      return response;
    },
    document: { createElement: () => anchor, body: { appendChild() {} } },
    urlApi: { createObjectURL(value) { blob = value; return 'blob:local-download'; }, revokeObjectURL: (url) => revoked.push(url) },
  });
  const ctx = {
    api: { authHeaders: () => token ? { Authorization: `Bearer ${token}` } : {} },
    signal: () => controller.signal,
    out: { line: (value) => messages.push(String(value)) },
    takeover() { assert.fail('export must not open the viewer'); },
  };
  return { calls, downloads, revoked, messages, anchor, controller, registry, blob: () => blob,
    run: (line = 'replay export run:r1') => dispatch(registry, ctx, line) };
}

test('browser download records the exact API bytes, including whitespace and Unicode', async () => {
  const app = harness();
  await app.run();
  assert.equal(app.calls[0].url, '/v1/replay/sessions/run%3Ar1/export');
  assert.equal(app.calls[0].init.headers.Authorization, 'Bearer export-token');
  assert.equal(app.calls[0].init.redirect, 'error');
  assert.deepEqual(new Uint8Array(await app.blob().arrayBuffer()), bytes);
  assert.deepEqual(app.downloads, [{ href: 'blob:local-download', filename: 'run_r1.replay.json' }]);
  assert.deepEqual(app.revoked, ['blob:local-download']);
  assert.equal(app.anchor.removed, true);
  assert.match(app.registry.get('replay').man, /replay export <session>/);
});

test('bare run ids and --out choose a local download filename, never a server path', async () => {
  const app = harness();
  await app.run('replay export r1 --out evidence.json');
  assert.equal(app.downloads[0].filename, 'evidence.json');
  assert.equal(app.calls[0].url, '/v1/replay/sessions/run%3Ar1/export');
});

for (const line of ['replay export', 'replay export ../secret', 'replay export run:..', 'replay export r1 extra',
  'replay export r1 --out', 'replay export r1 --out=../secret', 'replay export r1 --out=/tmp/file',
  'replay export r1 --out=', 'replay export r1 --out="<img>.json"', 'replay export r1 --follow']) {
  test(`invalid export never fetches: ${line}`, async () => {
    const app = harness();
    await app.run(line);
    assert.equal(app.calls.length, 0);
    assert.equal(app.downloads.length, 0);
    assert.match(app.messages.join(''), /usage/);
  });
}

for (const [status, hint] of [[401, /401.*auth/], [403, /403/], [404, /not found/], [410, /http 410/], [500, /http 500/]]) {
  test(`export handles HTTP ${status} without downloads or response-body leakage`, async () => {
    const app = harness({ status, token: status === 401 ? '' : 'wrong-token' });
    await app.run();
    assert.match(app.messages.join(''), hint);
    assert.ok(!app.messages.join('').includes('private-error'));
    assert.equal(app.downloads.length, 0);
    assert.equal(app.blob(), undefined);
  });
}

test('offline and cancellation do not cause downloads', async () => {
  const offline = harness({ offline: true });
  await offline.run();
  assert.match(offline.messages.join(''), /api unreachable/);
  const aborted = harness({ abortBody: true });
  await aborted.run();
  assert.equal(aborted.downloads.length, 0);
  assert.equal(aborted.messages.length, 0);
});

test('failed browser click cleans resources and reports a safe error', async () => {
  const app = harness({ clickFails: true });
  await app.run();
  assert.deepEqual(app.revoked, ['blob:local-download']);
  assert.equal(app.anchor.removed, true);
  assert.deepEqual(app.messages, ['replay export: download failed']);
});

test('blob HTTP client rejects cross-origin path tricks before sending the token', async () => {
  const client = createHttp({ fetchImpl: () => assert.fail('must not fetch') });
  for (const path of ['https://evil.example', '//evil.example', '/\\evil.example', '/\tevil.example']) {
    await assert.rejects(client.getBlob(path), /same-origin/);
  }
});

test('blob body timeout is classified and the caller abort is preserved', async () => {
  const client = createHttp({ timeoutMs: 5, fetchImpl: async (_url, init) => ({
    ok: true, status: 200,
    blob: () => new Promise((_resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('cancelled')))),
  }) });
  await assert.rejects(client.getBlob('/export'), (error) => error.kind === 'timeout');
});
