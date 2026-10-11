import { test } from 'node:test';
import assert from 'node:assert/strict';
import { externalUrls, httpBase, loadLinks } from '../../../src/mailroom_reloaded/api/tui/lib/links.js';

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

test('loadLinks returns the body object or null and never throws', async () => {
  const ctx = (get) => ({ api: { get }, signal: () => undefined });
  assert.deepEqual(await loadLinks(ctx(async () => ({ a: 1 }))), { a: 1 });
  assert.equal(await loadLinks(ctx(async () => 'x')), null);
  assert.equal(await loadLinks(ctx(async () => { throw new Error('x'); })), null);
  assert.equal(await loadLinks({ api: {}, signal: () => { throw new Error('x'); } }), null);
});
