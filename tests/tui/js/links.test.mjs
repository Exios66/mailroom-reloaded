import { test } from 'node:test';
import assert from 'node:assert/strict';
import { externalUrls, httpBase, inboxUrl, loadLinks, sandboxBase, validHashId } from '../../../src/mailroom_reloaded/api/tui/lib/links.js';

const BASE = 'http://localhost:8765';

test('httpBase accepts plain http(s) and trims trailing slashes', () => {
  assert.equal(httpBase('http://localhost:6006/'), 'http://localhost:6006');
  assert.equal(httpBase('https://x.example//'), 'https://x.example');
  for (const v of ['javascript:alert(1)', 'http://u:p@x', 'ftp://x', 'http://a b', '', null, 5]) assert.equal(httpBase(v), null, String(v));
});

test('externalUrls builds the grafana run link only for run ids', () => {
  const links = { phoenix_url: 'http://p/', grafana_url: 'http://g' };
  assert.deepEqual(externalUrls(links, 'run:a b'), { phoenix: 'http://p', grafana: 'http://g/d/mailroom-quality?var-run_id=a%20b' });
  assert.deepEqual(externalUrls(links, 'doc:x'), { phoenix: 'http://p', grafana: null });
  assert.deepEqual(externalUrls(null, 'run:a'), { phoenix: null, grafana: null });
});

test('inboxUrl defaults to the Ingress tab with the correspondent mailbox open', () => {
  assert.equal(inboxUrl(BASE), `${BASE}/ui#tab=messages&mailbox=open&role=correspondent`);
  assert.equal(inboxUrl(`${BASE}/`, {}), `${BASE}/ui#tab=messages&mailbox=open&role=correspondent`);
});

test('inboxUrl combines every allow-listed key in a fixed order', () => {
  assert.equal(
    inboxUrl(BASE, { tab: 'boss', scenario: 'sc_1.v2', sel: 'm-9', mailbox: 'closed', role: 'boss', thread: 't.1' }),
    `${BASE}/ui#tab=boss&scenario=sc_1.v2&sel=m-9&mailbox=closed&role=boss&thread=t.1`,
  );
});

test('inboxUrl rejects hostile bases', () => {
  for (const b of ['javascript:alert(1)', 'http://u:p@x', `${BASE}?a=1`, `${BASE}#x`, 'data:text/html,x', '', null, undefined, 42]) {
    assert.equal(inboxUrl(b), null, String(b));
  }
});

test('inboxUrl rejects values outside the allow-lists', () => {
  const bad = [
    { tab: 'nope' }, { tab: 'messages&x=1' }, { tab: 5 }, { tab: null },
    { mailbox: 'maybe' }, { role: 'admin' }, { role: 'boss&tab=docs' },
    { scenario: 'a b' }, { scenario: 'a'.repeat(65) }, { scenario: '' }, { scenario: '<x>' },
    { sel: 'a#b' }, { sel: 'a&b=c' }, { sel: 7 }, { thread: 'a/b' }, { thread: '%41' },
  ];
  for (const o of bad) assert.equal(inboxUrl(BASE, o), null, JSON.stringify(o));
  assert.equal(inboxUrl(BASE, 'tab=boss'), null);
  assert.equal(inboxUrl(BASE, null), null);
  assert.equal(validHashId('a'.repeat(64)), true);
  assert.equal(validHashId('a'.repeat(65)), false);
});

test('sandboxBase reads only a valid sandbox_url', () => {
  assert.equal(sandboxBase({ sandbox_url: `${BASE}/` }), BASE);
  assert.equal(sandboxBase({ sandbox_url: 'http://u:p@x' }), null);
  assert.equal(sandboxBase({}), null);
  assert.equal(sandboxBase(null), null);
});

test('loadLinks returns the body object or null and never throws', async () => {
  const ctx = (get) => ({ api: { get }, signal: () => undefined });
  assert.deepEqual(await loadLinks(ctx(async () => ({ a: 1 }))), { a: 1 });
  assert.equal(await loadLinks(ctx(async () => 'x')), null);
  assert.equal(await loadLinks(ctx(async () => { throw new Error('x'); })), null);
  assert.equal(await loadLinks({ api: {}, signal: () => { throw new Error('x'); } }), null);
});

test('ids must start with a letter or digit', () => {
  assert.equal(validHashId('..'), false);
  assert.equal(validHashId('.hidden'), false);
  assert.equal(validHashId('m0007'), true);
});
