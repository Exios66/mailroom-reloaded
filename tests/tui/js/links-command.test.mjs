import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderTable } from '../../../src/mailroom_reloaded/api/tui/terminal.js';
import { createRegistry, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { registerLinks, linkRows } from '../../../src/mailroom_reloaded/api/tui/commands/links.js';

const HOSTILE = '<img src=x onerror=alert(1)>';

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
/** A response with an explicit status, so a body that is itself an array is never misread. */
const resp = (status, body) => ({ __resp: true, status, body });

/**
 * A ctx whose HTTP goes through a fake fetch. `reply` is a body (200), a resp(status, body)
 * response, an Error (thrown as a network failure) or a function(url) returning one of those.
 */
function makeCtx({ reply, token = false, aborted = false } = {}) {
  const fetched = [];
  const out = [];
  const controller = new AbortController();
  if (aborted) controller.abort();
  const fetchImpl = async (url, init) => {
    fetched.push({ url, init });
    const r = typeof reply === 'function' ? reply(url) : reply;
    if (r instanceof Error) throw r;
    if (r && r.__resp === true) return json(r.status, r.body);
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

function setup(deps = {}) {
  const r = createRegistry();
  registerLinks(r, deps);
  return r;
}

const lines = (out) => out.filter((o) => o.k === 'line');
const table = (out) => out.find((o) => o.k === 'table');
const LINKS = {
  public_url: 'https://mailroom.example/',
  phoenix_url: 'http://localhost:6006/',
  grafana_url: 'http://localhost:3000',
  phoenix_project: 'mailroom-live',
};

test('links is registered with a man page', () => {
  const spec = setup().get('links');
  assert.ok(spec);
  assert.match(spec.man, /^NAME\n[\s\S]*SYNOPSIS[\s\S]*DESCRIPTION/);
});

test('links prints the validated /links table and reads GET /links', async () => {
  const m = makeCtx({ reply: LINKS });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'links');
  assert.equal(m.fetched[0].url, '/links');
  const t = table(m.out);
  assert.deepEqual(t.h, ['link', 'value']);
  assert.deepEqual(t.rows, [
    ['public_url', 'https://mailroom.example'],
    ['phoenix_url', 'http://localhost:6006'],
    ['grafana_url', 'http://localhost:3000'],
    ['phoenix_project', 'mailroom-live'],
  ]);
  assert.deepEqual(lines(m.out).map((l) => l.c), ['dim']);
});

test('links marks unset and invalid URLs without showing the invalid value', async () => {
  const body = { public_url: 'javascript:alert(1)', phoenix_url: '', grafana_url: 'http://u:p@x', phoenix_project: 'p' };
  const m = makeCtx({ reply: body });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'links');
  const t = table(m.out);
  assert.deepEqual(t.rows.map((r) => r[1]), ['invalid — not shown', '—', 'invalid — not shown', 'p']);
  assert.equal(t.o.cellClass(0, 1), 'warn');
  assert.equal(t.o.cellClass(1, 1), 'dim');
  assert.equal(t.o.cellClass(3, 1), '');
  assert.ok(!JSON.stringify(m.out).includes('javascript:'));
});

test('links with a 401 prints the run-auth hint and does not throw', async () => {
  const m = makeCtx({ reply: resp(401, {}) });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'links');
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [["links: 401 — run 'auth <token>'", 'error']]);
});

test('links with a 403 or 404 prints the matching hint', async () => {
  const forbidden = makeCtx({ reply: resp(403, {}) });
  await dispatch(setup({ fetchImpl: forbidden.fetchImpl }), forbidden.ctx, 'links');
  assert.equal(lines(forbidden.out)[0].t, 'links: 403 — not allowed by this server');
  const missing = makeCtx({ reply: resp(404, {}) });
  await dispatch(setup({ fetchImpl: missing.fetchImpl }), missing.ctx, 'links');
  assert.equal(lines(missing.out)[0].t, 'links: not found');
});

test('links with the api offline prints the unreachable message without throwing', async () => {
  const m = makeCtx({ reply: new TypeError('fetch failed') });
  const result = await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'links');
  assert.equal(result, 'ok');
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [['links: api unreachable — mailroom closed', 'error']]);
});

test('links prints nothing when the command was aborted', async () => {
  const m = makeCtx({ reply: new TypeError('fetch failed'), aborted: true });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'links');
  assert.deepEqual(lines(m.out), []);
});

test('links survives malformed payloads', async () => {
  for (const body of [null, undefined, 'x', 5, [], ['a'], { phoenix_url: { a: 1 }, phoenix_project: ['x'] }]) {
    const m = makeCtx({ reply: body });
    const result = await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'links');
    assert.equal(result, 'ok', JSON.stringify(body));
    assert.ok(table(m.out), JSON.stringify(body));
  }
});

test('links prints hostile values only as text', async () => {
  const m = makeCtx({ reply: { ...LINKS, phoenix_project: HOSTILE } });
  await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, 'links');
  const row = table(m.out).rows.find((r) => r[0] === 'phoenix_project');
  assert.equal(row[1], HOSTILE);
  assert.equal(table(m.out).o.cellClass(3, 1), '');
});

test('linkRows never throws and marks valid URLs as plain', () => {
  assert.deepEqual(linkRows(null).map((r) => r[1]), ['—', '—', '—', '—']);
  assert.deepEqual(linkRows({ grafana_url: 'http://g/' })[2], ['grafana_url', 'http://g', '']);
});

test('links open phoenix opens the validated URL with noopener and noreferrer', async () => {
  const opened = [];
  const m = makeCtx({ reply: LINKS });
  await dispatch(setup({ fetchImpl: m.fetchImpl, open: (...a) => opened.push(a) }), m.ctx, 'links open phoenix');
  assert.deepEqual(opened, [['http://localhost:6006', '_blank', 'noopener,noreferrer']]);
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [['opened phoenix: http://localhost:6006', 'success']]);
});

test('links open grafana never opens a javascript: or non-http URL', async () => {
  const opened = [];
  const m = makeCtx({ reply: { ...LINKS, grafana_url: 'javascript:alert(document.cookie)' } });
  await dispatch(setup({ fetchImpl: m.fetchImpl, open: (...a) => opened.push(a) }), m.ctx, 'links open grafana');
  assert.deepEqual(opened, []);
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [['grafana: invalid URL — not opened', 'error']]);
});

test('links open prometheus prints a dim line when the payload has no prometheus URL', async () => {
  const opened = [];
  const m = makeCtx({ reply: LINKS });
  await dispatch(setup({ fetchImpl: m.fetchImpl, open: (...a) => opened.push(a) }), m.ctx, 'links open prometheus');
  assert.deepEqual(opened, []);
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [['prometheus: not configured', 'dim']]);
});

test('links open prometheus opens a payload URL when one is published', async () => {
  const opened = [];
  const m = makeCtx({ reply: { ...LINKS, prometheus_url: 'http://localhost:9090' } });
  await dispatch(setup({ fetchImpl: m.fetchImpl, open: (...a) => opened.push(a) }), m.ctx, 'links open prometheus');
  assert.deepEqual(opened, [['http://localhost:9090', '_blank', 'noopener,noreferrer']]);
});

test('links open with an unset phoenix URL is not configured and opens nothing', async () => {
  const opened = [];
  const m = makeCtx({ reply: { ...LINKS, phoenix_url: null } });
  await dispatch(setup({ fetchImpl: m.fetchImpl, open: (...a) => opened.push(a) }), m.ctx, 'links open phoenix');
  assert.deepEqual(opened, []);
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [['phoenix: not configured', 'dim']]);
});

test('links open reports a throwing opener without throwing', async () => {
  const m = makeCtx({ reply: LINKS });
  const result = await dispatch(
    setup({ fetchImpl: m.fetchImpl, open: () => { throw new Error('blocked'); } }),
    m.ctx,
    'links open phoenix',
  );
  assert.equal(result, 'ok');
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [['links: could not open phoenix', 'error']]);
});

test('links open with a 401 or offline api prints the hint and opens nothing', async () => {
  const opened = [];
  const denied = makeCtx({ reply: resp(401, {}) });
  await dispatch(setup({ fetchImpl: denied.fetchImpl, open: (...a) => opened.push(a) }), denied.ctx, 'links open grafana');
  assert.equal(lines(denied.out)[0].t, "links: 401 — run 'auth <token>'");
  const offline = makeCtx({ reply: new TypeError('x') });
  await dispatch(setup({ fetchImpl: offline.fetchImpl, open: (...a) => opened.push(a) }), offline.ctx, 'links open grafana');
  assert.equal(lines(offline.out)[0].t, 'links: api unreachable — mailroom closed');
  assert.deepEqual(opened, []);
});

test('links rejects unknown targets and extra arguments before any request', async () => {
  for (const cmd of ['links open', 'links open javascript', 'links open phoenix extra', 'links close phoenix', 'links foo', 'links --bad', 'links open phoenix --bad']) {
    const m = makeCtx({ reply: LINKS });
    await dispatch(setup({ fetchImpl: m.fetchImpl }), m.ctx, cmd);
    assert.equal(m.fetched.length, 0, cmd);
    assert.deepEqual(lines(m.out).map((l) => l.c), ['error'], cmd);
    assert.match(lines(m.out)[0].t, /^links: usage: links \[open phoenix\|grafana\|prometheus\]$/, cmd);
  }
});

test('links open never puts a hostile value in a line', async () => {
  const m = makeCtx({ reply: { ...LINKS, phoenix_url: `http://a.example/${HOSTILE}` } });
  await dispatch(setup({ fetchImpl: m.fetchImpl, open: () => {} }), m.ctx, 'links open phoenix');
  // The hostile character set is not a valid httpBase value (whitespace), so it is refused.
  assert.deepEqual(lines(m.out).map((l) => l.c), ['error']);
});

test('links reports a missing browser opener rather than claiming success', async () => {
  const fixture = makeCtx({ reply: LINKS });
  await dispatch(setup({ fetchImpl: fixture.fetchImpl, open: false }), fixture.ctx, 'links open phoenix');
  assert.equal(lines(fixture.out).at(-1).t, 'links: cannot open phoenix here');
});

test('links forwards session auth but never prints the token', async () => {
  const fixture = makeCtx({ reply: LINKS });
  fixture.ctx.api.authHeaders = () => ({ Authorization: 'Bearer fixture-secret-token' });
  await dispatch(setup({ fetchImpl: fixture.fetchImpl }), fixture.ctx, 'links');
  assert.equal(fixture.fetched[0].init.headers.Authorization, 'Bearer fixture-secret-token');
  assert.ok(!JSON.stringify(fixture.out).includes('fixture-secret-token'));
});

test('links cancellation suppresses a late response and never opens a tab', async () => {
  for (const command of ['links', 'links open phoenix']) {
    const fixture = makeCtx();
    const opened = [];
    let respond;
    const fetchImpl = () => new Promise((resolve) => { respond = resolve; });
    const running = dispatch(setup({ fetchImpl, open: (...args) => opened.push(args) }), fixture.ctx, command);
    fixture.controller.abort();
    respond(json(200, LINKS));
    await running;
    assert.deepEqual(fixture.out, []);
    assert.deepEqual(opened, []);
  }
});

test('links table renders hostile project content using textContent only', async () => {
  const fixture = makeCtx({ reply: { ...LINKS, phoenix_project: HOSTILE } });
  await dispatch(setup({ fetchImpl: fixture.fetchImpl }), fixture.ctx, 'links');
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
