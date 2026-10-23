import { test } from 'node:test';
import assert from 'node:assert/strict';
import { registerPipeline } from '../../../src/mailroom_reloaded/api/tui/commands/pipeline.js';
import { registerAll } from '../../../src/mailroom_reloaded/api/tui/main.js';
import { renderTable } from '../../../src/mailroom_reloaded/api/tui/terminal.js';
import { createRegistry, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { registerReady, readyClass, readyView } from '../../../src/mailroom_reloaded/api/tui/commands/ready.js';

const HOSTILE = '<img src=x onerror=alert(1)>';

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const notJson = (status) => ({ ok: status >= 200 && status < 300, status, json: async () => { throw new SyntaxError('no'); } });
/** A response with an explicit status (any body, or no JSON body at all). */
const resp = (status, body, noJson = false) => ({ __resp: true, status, body, noJson });

/**
 * A ctx whose HTTP goes through a fake fetch. `reply` is a body (200), a resp(status, body)
 * response, an Error (network failure), or a function of the url returning one of those.
 * With `replies`, each fetch takes the next entry (the last one repeats).
 */
function makeCtx({ reply, replies, token = false, aborted = false } = {}) {
  const fetched = [];
  const out = [];
  const controller = new AbortController();
  if (aborted) controller.abort();
  let n = 0;
  const fetchImpl = async (url, init) => {
    fetched.push({ url, init });
    const r = replies ? replies[Math.min(n++, replies.length - 1)] : typeof reply === 'function' ? reply(url) : reply;
    if (r instanceof Error) throw r;
    if (r && r.__resp === true) return r.noJson ? notJson(r.status) : json(r.status, r.body);
    return json(200, r);
  };
  const ctx = {
    out: {
      line: (t, c) => out.push({ k: 'line', t, c }),
      table: (h, rows, o) => out.push({ k: 'table', h, rows, o }),
      kv: (pairs) => out.push({ k: 'kv', pairs }),
      man: () => {},
    },
    api: { hasToken: () => token, authHeaders: () => (token ? { Authorization: 'Bearer t' } : {}) },
    signal: () => controller.signal,
  };
  return { ctx, fetched, out, controller, fetchImpl };
}

/** Fake timers: setTimeout records the callback; `fire()` runs the next one. */
function fakeTimers() {
  const pending = [];
  return {
    pending,
    setTimeout: (fn, ms) => {
      const t = { fn, ms };
      pending.push(t);
      return t;
    },
    clearTimeout: (t) => {
      const i = pending.indexOf(t);
      if (i !== -1) pending.splice(i, 1);
    },
    async fire() {
      const t = pending.shift();
      if (!t) return false;
      await t.fn();
      await flush();
      return true;
    },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function setup(deps = {}) {
  const r = createRegistry();
  registerReady(r, deps);
  return r;
}

const lines = (out) => out.filter((o) => o.k === 'line');
const table = (out) => out.find((o) => o.k === 'table');
const COMPONENTS = [
  { name: 'db', status: 'ok', latency_ms: 1.23 },
  { name: 'phoenix', status: 'degraded', latency_ms: 2000 },
  { name: 'grafana', status: 'down', latency_ms: 5 },
  { name: 'prometheus', status: 'unconfigured', latency_ms: 0 },
];
const READY_OK = { schema_version: 1, status: 'degraded', components: COMPONENTS };

test('ready is registered with a man page', () => {
  const spec = setup().get('ready');
  assert.ok(spec);
  assert.match(spec.man, /^NAME\n[\s\S]*SYNOPSIS[\s\S]*DESCRIPTION/);
});

test('readyClass maps the four statuses and gives unknown words no class', () => {
  assert.equal(readyClass('ok'), 'success');
  assert.equal(readyClass('degraded'), 'warn');
  assert.equal(readyClass('down'), 'error');
  assert.equal(readyClass('unconfigured'), 'dim');
  assert.equal(readyClass('constructor'), '');
  assert.equal(readyClass(undefined), '');
});

test('ready renders a degraded component table after an abort-aware 2050ms response', async () => {
  const fixture = makeCtx({ token: true });
  let aborted = false;
  const fetchImpl = (_url, { signal }) => new Promise((resolve, reject) => {
    const abort = () => {
      aborted = true;
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', abort);
      resolve(json(200, READY_OK));
    }, 2050);
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort, { once: true });
  });
  await dispatch(setup({ fetchImpl }), fixture.ctx, 'ready');
  assert.equal(aborted, false, 'Readiness must allow the server probe budget plus response overhead');
  assert.deepEqual(lines(fixture.out)[0], { k: 'line', t: 'ready — degraded', c: 'warn' });
  assert.deepEqual(table(fixture.out).h, ['component', 'status', 'latency']);
  assert.deepEqual(table(fixture.out).rows[1], ['phoenix', 'degraded', '2000 ms']);
});

test('ready renders the components table with colour classes and latency', async () => {
  const m = makeCtx({ reply: READY_OK, token: true });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'ready');
  assert.equal(m.fetched[0].url, '/ready');
  assert.deepEqual(lines(m.out)[0], { k: 'line', t: 'ready — degraded', c: 'warn' });
  const t = table(m.out);
  assert.deepEqual(t.h, ['component', 'status', 'latency']);
  assert.deepEqual(t.rows, [
    ['db', 'ok', '1.2 ms'],
    ['phoenix', 'degraded', '2000 ms'],
    ['grafana', 'down', '5 ms'],
    ['prometheus', 'unconfigured', '0 ms'],
  ]);
  assert.deepEqual([0, 1, 2, 3].map((r) => t.o.cellClass(r, 1)), ['success', 'warn', 'error', 'dim']);
  assert.equal(t.o.cellClass(0, 0), '');
});

test('ready with an ok aggregate and no detail says how to get the rows', async () => {
  const m = makeCtx({ reply: { schema_version: 1, status: 'ok' } });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'ready');
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [
    ['ready — ok', 'success'],
    ['component detail needs a token — run \'auth <token>\'', 'dim'],
  ]);
  assert.equal(table(m.out), undefined);
});

test('ready with a token that was not accepted says the detail is hidden', async () => {
  const m = makeCtx({ reply: { schema_version: 1, status: 'ok' }, token: true });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'ready');
  assert.equal(lines(m.out)[1].t, "component detail hidden — the token was not accepted; run 'auth <token>'");
});

test('ready shows the rows on a 503 down response', async () => {
  const down = { schema_version: 1, status: 'down', components: [{ name: 'db', status: 'down', latency_ms: 3 }] };
  const m = makeCtx({ reply: resp(503, down), token: true });
  const result = await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'ready');
  assert.equal(result, 'ok');
  assert.deepEqual(lines(m.out)[0], { k: 'line', t: 'ready — down', c: 'error' });
  assert.deepEqual(table(m.out).rows, [['db', 'down', '3 ms']]);
});

test('ready with a 503 that has no JSON body prints the status', async () => {
  const m = makeCtx({ reply: resp(503, null, true) });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'ready');
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [['ready: http 503', 'error']]);
});

test('ready with a 401 prints the run-auth hint', async () => {
  const m = makeCtx({ reply: resp(401, {}) });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'ready');
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [["ready: 401 — run 'auth <token>'", 'error']]);
});

test('ready with a 403 or 404 prints the matching message', async () => {
  const forbidden = makeCtx({ reply: resp(403, {}) });
  await dispatch(setup({ fetchImpl: forbidden.fetchImpl }), forbidden.ctx, 'ready');
  assert.equal(lines(forbidden.out)[0].t, 'ready: 403 — not allowed by this server');
  const missing = makeCtx({ reply: resp(404, {}) });
  await dispatch(setup({ fetchImpl: missing.fetchImpl }), missing.ctx, 'ready');
  assert.equal(lines(missing.out)[0].t, 'ready: not found — this api has no /ready');
});

test('ready with the backend offline prints a message and does not throw', async () => {
  const m = makeCtx({ reply: new TypeError('fetch failed') });
  const result = await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'ready');
  assert.equal(result, 'ok');
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [['ready: api unreachable — mailroom closed', 'error']]);
});

test('ready prints nothing when the command was aborted', async () => {
  const m = makeCtx({ reply: new TypeError('fetch failed'), aborted: true });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'ready');
  assert.deepEqual(lines(m.out), []);
});

test('readyView and ready survive malformed payloads', async () => {
  const bodies = [null, undefined, 'x', 5, [], ['a'], { status: 5, components: 'no' }, { components: [null, 'x', 7] }];
  for (const body of bodies) {
    const m = makeCtx({ reply: body, token: true });
    const result = await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'ready');
    assert.equal(result, 'ok', JSON.stringify(body));
  }
  assert.deepEqual(readyView({ status: 'ok', components: [{ name: 'x', status: 'ok', latency_ms: 'NaN' }] }).components[0].latency, '—');
  assert.equal(readyView(null).status, null);
  assert.equal(readyView({ components: 'no' }).components, null);
});

test('ready prints hostile component values only as text', async () => {
  const body = { status: 'ok', components: [{ name: HOSTILE, status: HOSTILE, latency_ms: 1 }] };
  const m = makeCtx({ reply: body, token: true });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'ready');
  const t = table(m.out);
  assert.deepEqual(t.rows[0], [HOSTILE, HOSTILE, '1 ms']);
  assert.equal(t.o.cellClass(0, 1), '');
});

test('ready rejects arguments and bad flags before any request', async () => {
  for (const cmd of ['ready all', 'ready --watch 5', 'ready --interval 3', 'ready --watch --interval 0', 'ready --watch --interval abc', 'ready --bad']) {
    const m = makeCtx({ reply: READY_OK });
    await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, cmd);
    assert.equal(m.fetched.length, 0, cmd);
    assert.deepEqual(lines(m.out).map((l) => l.c), ['error'], cmd);
  }
});

test('ready --watch re-polls, prints only changes and stops on abort', async () => {
  const timers = fakeTimers();
  const statuses = [
    { status: 'ok', components: [{ name: 'db', status: 'ok', latency_ms: 1 }] },
    { status: 'ok', components: [{ name: 'db', status: 'ok', latency_ms: 9 }] },
    { status: 'down', components: [{ name: 'db', status: 'down', latency_ms: 9 }] },
  ];
  let i = 0;
  const m = makeCtx({ reply: () => statuses[Math.min(i++, statuses.length - 1)], token: true });
  const run = dispatch(setup({ fetchImpl: m.fetchImpl, timers, doc: null }), m.ctx, 'ready --watch --interval 2');
  await flush();
  assert.equal(lines(m.out)[0].t, 'watching ready · every 2s · ctrl+c to stop');
  assert.equal(timers.pending[0].ms, 0); // the first tick is immediate

  await timers.fire(); // first tick: the table prints
  assert.equal(timers.pending[0].ms, 2000);
  assert.equal(table(m.out).rows[0][2], '1 ms');
  assert.equal(table(m.out).rows.length, 1);

  await timers.fire(); // same statuses, only latency moved: nothing new is printed
  assert.equal(m.out.filter((o) => o.k === 'table').length, 1);

  await timers.fire(); // db went down: the table prints again
  assert.equal(m.out.filter((o) => o.k === 'table').length, 2);
  const tables = m.out.filter((o) => o.k === 'table');
  assert.deepEqual(tables.at(-1).rows[0].slice(0, 2), ['db', 'down']);

  m.controller.abort();
  await run;
  assert.equal(lines(m.out).at(-1).t, 'ready: watch stopped');
  assert.equal(timers.pending.length, 0);
});

test('ready --watch prints one error line per distinct failure and keeps polling', async () => {
  const timers = fakeTimers();
  const m = makeCtx({ replies: [resp(401, {}), resp(401, {}), READY_OK], token: true });
  const run = dispatch(setup({ fetchImpl: m.fetchImpl, timers, doc: null }), m.ctx, 'ready --watch');
  await flush();
  await timers.fire(); // 401: one line
  await timers.fire(); // the same 401: nothing new
  assert.equal(lines(m.out).filter((l) => l.t === "ready: 401 — run 'auth <token>'").length, 1);
  await timers.fire(); // recovered: the table prints
  assert.ok(table(m.out));
  m.controller.abort();
  await run;
});

test('ready --watch never throws on hostile payloads', async () => {
  const timers = fakeTimers();
  const m = makeCtx({ replies: [null, 'x', { status: 'ok', components: [{ name: HOSTILE }] }] });
  const run = dispatch(setup({ fetchImpl: m.fetchImpl, timers, doc: null }), m.ctx, 'ready --watch --interval 1');
  await flush();
  await timers.fire();
  await timers.fire();
  await timers.fire();
  m.controller.abort();
  await run;
  assert.ok(lines(m.out).every((l) => typeof l.t === 'string'));
  assert.equal(table(m.out).rows[0][0], HOSTILE);
});

test('ready --watch does nothing after an abort before the first tick', async () => {
  const timers = fakeTimers();
  const m = makeCtx({ reply: READY_OK, aborted: true });
  await dispatch(setup({ fetchImpl: m.fetchImpl, timers, doc: null }), m.ctx, 'ready --watch');
  assert.equal(m.fetched.length, 0);
  assert.equal(table(m.out), undefined);
});

test('main registers links, ready and config without module failures', () => {
  const registry = createRegistry();
  const host = registerAll(registry, { ambient: {} });
  for (const name of ['links', 'ready', 'config']) {
    assert.ok(host.modules().some((module) => module.id === name));
    assert.ok(registry.get(name)?.man);
  }
  assert.match(registry.get('health').usage, /--all/);
  assert.match(registry.get('watch').usage, /ready/);
});

test('health --all renders readiness while plain health preserves its original handler', async () => {
  const fixture = makeCtx({ reply: READY_OK });
  const registry = createRegistry();
  registerPipeline(registry);
  const requests = [];
  fixture.ctx.api.get = async (path) => {
    requests.push(path);
    return { service: 'mailroom', status: 'ok' };
  };
  registerReady(registry, { fetchImpl: fixture.fetchImpl });
  await dispatch(registry, fixture.ctx, 'health --all');
  assert.equal(fixture.fetched[0].url, '/ready');
  assert.ok(table(fixture.out));
  await dispatch(registry, fixture.ctx, 'health');
  assert.deepEqual(requests, ['/health']);
  assert.equal(lines(fixture.out).at(-1).t, 'mailroom — ok');
});

test('readiness aliases reject invalid arguments before requesting', async () => {
  for (const command of ['health --all no', 'health extra --all', 'health --all --interval 1', 'watch ready extra', 'watch ready --interval 0']) {
    const fixture = makeCtx({ reply: READY_OK });
    const registry = createRegistry();
    registerPipeline(registry);
    registerReady(registry, { fetchImpl: fixture.fetchImpl });
    assert.equal(await dispatch(registry, fixture.ctx, command), 'ok');
    assert.equal(fixture.fetched.length, 0, command);
    assert.equal(lines(fixture.out).at(-1).c, 'error', command);
  }
});

test('watch ready re-polls and cancels while plain watch keeps its handler', async () => {
  const fixture = makeCtx({ reply: READY_OK });
  const timers = fakeTimers();
  const registry = createRegistry();
  registerPipeline(registry);
  const originalWatch = registry.get('watch').run;
  const calls = [];
  registry.get('watch').run = (...args) => calls.push(args);
  registerReady(registry, { fetchImpl: fixture.fetchImpl, timers, doc: null });
  const running = dispatch(registry, fixture.ctx, 'watch ready --interval 2');
  await timers.fire();
  await timers.fire();
  assert.equal(fixture.fetched.length, 2);
  fixture.controller.abort();
  await running;
  assert.equal(timers.pending.length, 0);
  await dispatch(registry, fixture.ctx, 'watch --interval 4');
  assert.equal(typeof originalWatch, 'function');
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].slice(1), [[], { interval: '4' }]);
});

test('readiness watch aborts an in-flight request with no late output', async () => {
  const fixture = makeCtx();
  const timers = fakeTimers();
  let requestSignal;
  const fetchImpl = (_url, init) => new Promise((_resolve, reject) => {
    requestSignal = init.signal;
    init.signal.addEventListener('abort', () => reject(new Error('cancelled')), { once: true });
  });
  const running = dispatch(setup({ fetchImpl, timers, doc: null }), fixture.ctx, 'ready --watch');
  const ticking = timers.fire();
  assert.ok(requestSignal);
  fixture.controller.abort();
  await running;
  await ticking;
  assert.equal(requestSignal.aborted, true);
  assert.equal(timers.pending.length, 0);
  assert.deepEqual(lines(fixture.out).map((line) => line.t), [
    'watching ready · every 5s · ctrl+c to stop', 'ready: watch stopped',
  ]);
});

test('ready cancellation ignores a successful late response', async () => {
  const fixture = makeCtx();
  let respond;
  const fetchImpl = () => new Promise((resolve) => { respond = resolve; });
  const running = dispatch(setup({ fetchImpl }), fixture.ctx, 'ready');
  fixture.controller.abort();
  respond(json(200, READY_OK));
  await running;
  assert.deepEqual(fixture.out, []);
});

test('ready forwards auth and renders hostile component names as DOM text without tokens', async () => {
  const fixture = makeCtx({ reply: { status: 'ok', components: [{ name: HOSTILE, status: 'ok', latency_ms: 1 }] }, token: true });
  fixture.ctx.api.authHeaders = () => ({ Authorization: 'Bearer fixture-secret-token' });
  await dispatch(setup({ fetchImpl: fixture.fetchImpl }), fixture.ctx, 'ready');
  assert.equal(fixture.fetched[0].init.headers.Authorization, 'Bearer fixture-secret-token');
  const document = {
    createElement(tagName) {
      return {
        tagName, children: [], value: '',
        appendChild(child) { this.children.push(child); },
        set textContent(value) { this.value = String(value); },
        get textContent() { return this.value + this.children.map((child) => child.textContent).join(''); },
        set innerHTML(value) { assert.fail(`unsafe HTML: ${value}`); },
      };
    },
  };
  const rendered = table(fixture.out);
  const node = renderTable(document, rendered.h, rendered.rows, rendered.o);
  assert.ok(node.textContent.includes(HOSTILE));
  assert.ok(!node.textContent.includes('fixture-secret-token'));
});
