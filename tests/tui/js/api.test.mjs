import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, createApi } from '../../../src/mailroom_reloaded/api/tui/api.js';

function memStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    _map: m,
  };
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function recorder(response) {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url, init });
    if (response instanceof Error) throw response;
    return typeof response === 'function' ? response(url, init) : response;
  };
  return { calls, fetchImpl };
}

test('no bearer header until a token is set', async () => {
  const rec = recorder(jsonResponse(200, { items: [] }));
  const storage = memStorage();
  const api = createApi({ fetchImpl: rec.fetchImpl, storage });
  await api.get('/v1/documents');
  assert.equal(rec.calls[0].init.headers?.Authorization, undefined);
  assert.equal(api.hasToken(), false);

  api.setToken('secret-token');
  await api.get('/v1/documents');
  assert.equal(rec.calls[1].init.headers.Authorization, 'Bearer secret-token');
  assert.equal(api.hasToken(), true);
});

test('token is stored in the injected storage only and never in the URL', async () => {
  const rec = recorder(jsonResponse(200, {}));
  const storage = memStorage();
  const api = createApi({ fetchImpl: rec.fetchImpl, storage });
  api.setToken('secret-token');
  assert.equal(storage.getItem('mailroom.tui.token'), 'secret-token');
  await api.get('/v1/documents', { status: 'parked' });
  assert.ok(!rec.calls[0].url.includes('secret-token'));
});

test('clearToken removes the stored key and the header', async () => {
  const rec = recorder(jsonResponse(200, {}));
  const storage = memStorage();
  const api = createApi({ fetchImpl: rec.fetchImpl, storage });
  api.setToken('t');
  api.clearToken();
  assert.equal(storage.getItem('mailroom.tui.token'), null);
  assert.equal(api.hasToken(), false);
  await api.get('/v1/documents');
  assert.equal(rec.calls[0].init.headers?.Authorization, undefined);
});

test('a token already in storage is picked up on creation', async () => {
  const storage = memStorage();
  storage.setItem('mailroom.tui.token', 'stored');
  const rec = recorder(jsonResponse(200, {}));
  const api = createApi({ fetchImpl: rec.fetchImpl, storage });
  assert.equal(api.hasToken(), true);
  await api.get('/v1/documents');
  assert.equal(rec.calls[0].init.headers.Authorization, 'Bearer stored');
});

test('query params are encoded into the URL', async () => {
  const rec = recorder(jsonResponse(200, {}));
  const api = createApi({ fetchImpl: rec.fetchImpl, storage: memStorage(), base: '' });
  await api.get('/v1/documents', { status: 'parked', limit: 5 });
  assert.equal(rec.calls[0].url, '/v1/documents?status=parked&limit=5');
});

test('network failure becomes an offline ApiError', async () => {
  const rec = recorder(new TypeError('Failed to fetch'));
  const api = createApi({ fetchImpl: rec.fetchImpl, storage: memStorage() });
  await assert.rejects(api.get('/v1/documents'), (err) => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.kind, 'offline');
    assert.equal(err.status, null);
    return true;
  });
});

test('401 becomes an unauthorized ApiError', async () => {
  const rec = recorder(jsonResponse(401, { detail: 'nope' }));
  const api = createApi({ fetchImpl: rec.fetchImpl, storage: memStorage() });
  await assert.rejects(api.get('/v1/documents'), (err) => {
    assert.equal(err.kind, 'unauthorized');
    assert.equal(err.status, 401);
    return true;
  });
});

test('other non-2xx surfaces the JSON detail as an http ApiError', async () => {
  const rec = recorder(jsonResponse(404, { detail: 'document not found' }));
  const api = createApi({ fetchImpl: rec.fetchImpl, storage: memStorage() });
  await assert.rejects(api.get('/v1/documents/9'), (err) => {
    assert.equal(err.kind, 'http');
    assert.equal(err.status, 404);
    assert.equal(err.detail, 'document not found');
    assert.equal(err.message, 'document not found');
    return true;
  });
});

test('post sends a JSON body', async () => {
  const rec = recorder(jsonResponse(200, { ok: true }));
  const api = createApi({ fetchImpl: rec.fetchImpl, storage: memStorage() });
  const out = await api.post('/v1/documents/1/review', { decision: 'accept' });
  assert.deepEqual(out, { ok: true });
  assert.equal(rec.calls[0].init.method, 'POST');
  assert.equal(rec.calls[0].init.headers['Content-Type'], 'application/json');
  assert.equal(rec.calls[0].init.body, JSON.stringify({ decision: 'accept' }));
});

test('upload posts multipart form data to /v1/documents', async () => {
  const rec = recorder(jsonResponse(201, { id: 7 }));
  const api = createApi({ fetchImpl: rec.fetchImpl, storage: memStorage() });
  const file = new File(['hello'], 'a.txt', { type: 'text/plain' });
  const out = await api.upload(file);
  assert.deepEqual(out, { id: 7 });
  assert.equal(rec.calls[0].url, '/v1/documents');
  assert.equal(rec.calls[0].init.method, 'POST');
  assert.ok(rec.calls[0].init.body instanceof FormData);
  assert.equal(rec.calls[0].init.body.get('file').name, 'a.txt');
});

test('health reports ok on 200 and status on failure, offline as null', async () => {
  const ok = createApi({
    fetchImpl: recorder(jsonResponse(200, {})).fetchImpl,
    storage: memStorage(),
  });
  assert.deepEqual(await ok.health(), { ok: true, status: 200 });

  const down = createApi({
    fetchImpl: recorder(new TypeError('x')).fetchImpl,
    storage: memStorage(),
  });
  assert.deepEqual(await down.health(), { ok: false, status: null });

  const denied = createApi({
    fetchImpl: recorder(jsonResponse(401, {})).fetchImpl,
    storage: memStorage(),
  });
  assert.deepEqual(await denied.health(), { ok: false, status: 401 });
});

test('base prefixes every request path', async () => {
  const rec = recorder(jsonResponse(200, {}));
  const api = createApi({ fetchImpl: rec.fetchImpl, storage: memStorage(), base: 'http://h:8000' });
  await api.get('/v1/documents');
  assert.equal(rec.calls[0].url, 'http://h:8000/v1/documents');
});
