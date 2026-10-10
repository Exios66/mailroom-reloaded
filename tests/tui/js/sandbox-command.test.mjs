import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRegistry, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { registerSandbox } from '../../../src/mailroom_reloaded/api/tui/commands/sandbox.js';
import { registerAll } from '../../../src/mailroom_reloaded/api/tui/main.js';

const BASE = 'http://localhost:8765';
const DEFAULT_URL = `${BASE}/ui#tab=messages&mailbox=open&role=correspondent`;
const HINT = 'if the sandbox asks for a token, paste it into its API token field; it is never put in the link';
const NOT_CONFIGURED = 'inbox: sandbox URL not configured (set MAILROOM_SANDBOX_URL)';

function makeCtx({ links = { sandbox_url: BASE }, open = true } = {}) {
  const out = [];
  const opened = [];
  const calls = [];
  const ctx = {
    out: { line: (t, c) => out.push({ t, c }) },
    api: {
      get: async (path) => {
        calls.push(path);
        if (links instanceof Error) throw links;
        return links;
      },
    },
    signal: () => new AbortController().signal,
    open: open ? (url, target, features) => opened.push({ url, target, features }) : undefined,
  };
  return { ctx, out, opened, calls };
}

const registry = () => {
  const r = createRegistry();
  registerSandbox(r);
  return r;
};
const run = async (line, opts) => {
  const h = makeCtx(opts);
  await dispatch(registry(), h.ctx, line);
  return h;
};
const texts = (h) => h.out.map((o) => o.t);

test('inbox opens the Ingress inbox in a new tab, prints the URL and the token hint', async () => {
  const h = await run('inbox');
  assert.deepEqual(h.opened, [{ url: DEFAULT_URL, target: '_blank', features: 'noopener' }]);
  assert.deepEqual(h.calls, ['/links']);
  assert.deepEqual(texts(h), [DEFAULT_URL, HINT]);
});

test('--tab maps to the sandbox tab ids', async () => {
  for (const [tab, id] of [['ingress', 'messages'], ['boss', 'boss'], ['outbox', 'outbox'], ['events', 'events']]) {
    const h = await run(`inbox --tab ${tab}`);
    assert.equal(h.opened[0].url, `${BASE}/ui#tab=${id}&mailbox=open&role=correspondent`, tab);
  }
});

test('flag combinations build one hash', async () => {
  const h = await run('inbox --tab boss --scenario sc_1 --message m.2 --thread t-3 --no-mailbox');
  assert.equal(h.opened[0].url, `${BASE}/ui#tab=boss&scenario=sc_1&sel=m.2&mailbox=closed&role=correspondent&thread=t-3`);
  const e = await run('inbox --scenario=sc_1 --thread=t1');
  assert.equal(e.opened[0].url, `${BASE}/ui#tab=messages&scenario=sc_1&mailbox=open&role=correspondent&thread=t1`);
});

test('--print prints the link and never opens', async () => {
  const h = await run('inbox --print --tab events');
  assert.deepEqual(h.opened, []);
  assert.deepEqual(texts(h), [`${BASE}/ui#tab=events&mailbox=open&role=correspondent`, HINT]);
});

test('without an opener the link is still printed', async () => {
  const h = await run('inbox', { open: false });
  assert.deepEqual(texts(h), [DEFAULT_URL, HINT]);
});

test('a trailing slash on sandbox_url is trimmed', async () => {
  const h = await run('inbox', { links: { sandbox_url: `${BASE}/` } });
  assert.equal(h.opened[0].url, DEFAULT_URL);
});

test('javascript:, credential and non-http sandbox URLs are refused', async () => {
  for (const sandbox_url of ['javascript:alert(1)', 'http://u:p@x', 'https://u@x.example', 'ftp://x', 'data:text/html,x', `${BASE}?a=1`, `${BASE}#x`, 'http://a b', 42]) {
    const h = await run('inbox', { links: { sandbox_url } });
    assert.deepEqual(h.opened, [], String(sandbox_url));
    assert.equal(texts(h)[0], NOT_CONFIGURED, String(sandbox_url));
  }
});

test('a missing sandbox_url or failed /links is reported and never throws', async () => {
  for (const links of [{}, null, 'x', new Error('offline'), { sandbox_url: '' }]) {
    const h = await run('inbox', { links });
    assert.deepEqual(h.opened, []);
    assert.deepEqual(h.out.map((o) => o.t), [NOT_CONFIGURED]);
    assert.equal(h.out[0].c, 'error');
  }
});

test('malformed flags print usage and neither fetch nor open', async () => {
  const bad = [
    'inbox --tab', 'inbox --tab docs', 'inbox --tab messages', 'inbox --tab INGRESS', 'inbox --tab constructor',
    'inbox --scenario', 'inbox --scenario "a b"', 'inbox --scenario a/b', 'inbox --message x&tab=docs', 'inbox --thread #1',
    `inbox --scenario ${'a'.repeat(65)}`, 'inbox --print boss', 'inbox --no-mailbox=yes', 'inbox --print=1',
    'inbox --bogus', 'inbox extra', 'inbox --tab boss --nope 1',
  ];
  for (const line of bad) {
    const h = await run(line);
    assert.deepEqual(h.opened, [], line);
    assert.deepEqual(h.calls, [], line);
    assert.equal(h.out[0].c, 'error', line);
    assert.match(h.out[0].t, /^inbox: /, line);
    assert.match(h.out[1].t, /^inbox: usage: inbox /, line);
  }
});

test('the URL never carries a token, even if /links has one', async () => {
  const h = await run('inbox --tab boss', { links: { sandbox_url: BASE, api_token: 'sekrit', token: 'sekrit' } });
  assert.doesNotMatch(h.opened[0].url, /sekrit|token|auth|@/i);
});

test('inbox is registered once with help, usage and a man page', () => {
  const r = createRegistry();
  registerAll(r, { ambient: { state: () => ({}), setCrt() {}, setSkyline() {}, setTheme() {} } });
  assert.equal(r.names().filter((n) => n === 'inbox').length, 1);
  const spec = r.get('inbox');
  assert.ok(spec.summary);
  assert.match(spec.usage, /--tab ingress\|boss\|outbox\|events/);
  assert.match(spec.man, /MAILROOM_SANDBOX_URL|GET \/links/);
  assert.match(spec.man, /polls every 2 s/);
});

test('--tab values complete from the allow-list', () => {
  const spec = registry().get('inbox');
  assert.deepEqual(spec.complete({}, ['--tab', '']), ['ingress', 'boss', 'outbox', 'events']);
  assert.deepEqual(spec.complete({}, ['--tab', 'o']), ['outbox']);
  assert.deepEqual(spec.complete({}, ['--scenario', 'o']), []);
});
