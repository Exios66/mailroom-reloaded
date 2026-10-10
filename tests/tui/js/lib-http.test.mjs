import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHttp, httpFor, errorHint, HttpError, HTTP_TIMEOUT_MS } from '../../../src/mailroom_reloaded/api/tui/lib/http.js';

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

test('default timeout is 2 s', () => {
  assert.equal(HTTP_TIMEOUT_MS, 2000);
});

test('sends the auth header and Accept, builds the query, returns JSON', async () => {
  const seen = [];
  const http = createHttp({
    fetchImpl: async (url, init) => { seen.push({ url, init }); return json(200, { status: 'ok' }); },
    authHeaders: () => ({ Authorization: 'Bearer t0k' }),
  });
  assert.deepEqual(await http.get('/ready', { a: 1, b: undefined }), { status: 'ok' });
  assert.equal(seen[0].url, '/ready?a=1');
  assert.equal(seen[0].init.headers.Authorization, 'Bearer t0k');
  assert.equal(seen[0].init.headers.Accept, 'application/json');
});

test('401 maps to unauthorized with a run-auth hint', async () => {
  const http = createHttp({ fetchImpl: async () => json(401, {}) });
  await assert.rejects(http.get('/v1/x'), (err) => {
    assert.ok(err instanceof HttpError);
    assert.equal(err.kind, 'unauthorized');
    assert.equal(err.status, 401);
    assert.match(errorHint(err), /run 'auth <token>'/);
    return true;
  });
});

test('status codes map to typed kinds', async () => {
  for (const [status, kind] of [[403, 'forbidden'], [404, 'not_found'], [500, 'http'], [503, 'http']]) {
    const http = createHttp({ fetchImpl: async () => json(status, {}) });
    await assert.rejects(http.get('/x'), (err) => err.kind === kind && err.status === status);
  }
});

test('a hung request times out as kind timeout', async () => {
  const http = createHttp({
    timeoutMs: 20,
    fetchImpl: (_url, init) => new Promise((_res, rej) => init.signal.addEventListener('abort', () => rej(new Error('abort')))),
  });
  await assert.rejects(http.get('/slow'), (err) => err.kind === 'timeout' && /20 ms/.test(err.message));
});

test('network failure is offline; caller abort is aborted', async () => {
  const offline = createHttp({ fetchImpl: async () => { throw new TypeError('fetch failed'); } });
  await assert.rejects(offline.get('/x'), (err) => err.kind === 'offline');
  const ctl = new AbortController();
  ctl.abort();
  const aborted = createHttp({ fetchImpl: async (_u, init) => { if (init.signal.aborted) throw new Error('a'); return json(200, {}); } });
  await assert.rejects(aborted.get('/x', undefined, { signal: ctl.signal }), (err) => err.kind === 'aborted');
});

test('non-JSON body is a parse error; 204 is null', async () => {
  const bad = createHttp({ fetchImpl: async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('x'); } }) });
  await assert.rejects(bad.get('/x'), (err) => err.kind === 'parse');
  const empty = createHttp({ fetchImpl: async () => ({ ok: true, status: 204 }) });
  assert.equal(await empty.get('/x'), null);
});

test('only same-origin absolute paths are allowed', async () => {
  const http = createHttp({ fetchImpl: async () => json(200, {}) });
  for (const p of ['http://evil.example/x', '//evil.example/x', 'relative', 42]) {
    await assert.rejects(http.get(p), (err) => err.kind === 'http');
  }
});

test('post sends a JSON body', async () => {
  let init;
  const http = createHttp({ fetchImpl: async (_u, i) => { init = i; return json(200, { ok: true }); } });
  await http.post('/v1/thing', { a: 1 });
  assert.equal(init.method, 'POST');
  assert.equal(init.headers['Content-Type'], 'application/json');
  assert.equal(init.body, '{"a":1}');
});

test('httpFor reads the token from ctx.api and the signal from ctx', async () => {
  const ctl = new AbortController();
  let init;
  const prev = globalThis.fetch;
  globalThis.fetch = async (_u, i) => { init = i; return json(200, {}); };
  try {
    const ctx = { api: { authHeaders: () => ({ Authorization: 'Bearer s' }) }, signal: () => ctl.signal };
    await httpFor(ctx).get('/ready');
    assert.equal(init.headers.Authorization, 'Bearer s');
    await httpFor({}).get('/ready');
    assert.equal(init.headers.Authorization, undefined);
  } finally {
    globalThis.fetch = prev;
  }
});

test('errorHint covers every kind without throwing', () => {
  for (const kind of ['offline', 'timeout', 'forbidden', 'not_found', 'parse', 'aborted']) {
    assert.equal(typeof errorHint(new HttpError('m', { kind })), 'string');
  }
  assert.equal(errorHint(new HttpError('http 500', { kind: 'http', status: 500 })), 'http 500');
  assert.equal(errorHint(null), 'request failed');
});
