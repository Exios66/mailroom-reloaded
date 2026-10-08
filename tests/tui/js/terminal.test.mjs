import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  capScrollback,
  maskCommand,
  renderTable,
  renderLine,
  renderKv,
  renderListing,
  renderPre,
  renderBanner,
} from '../../../src/mailroom_reloaded/api/tui/terminal.js';

// Tiny DOM stub: node has no document. Only createElement/textContent/appendChild.
function makeDoc() {
  function node(tagName) {
    const n = {
      tagName,
      className: '',
      children: [],
      _text: '',
      attrs: {},
      appendChild(child) {
        n.children.push(child);
        return child;
      },
      setAttribute(k, v) {
        n.attrs[k] = String(v);
      },
      get textContent() {
        return n._text + n.children.map((c) => c.textContent).join('');
      },
      set textContent(v) {
        n.children = [];
        n._text = String(v);
      },
    };
    return n;
  }
  return { createElement: node };
}

const HOSTILE = '<img src=x onerror=alert(1)>';

test('capScrollback keeps the last 1000 by default', () => {
  const lines = Array.from({ length: 1500 }, (_, i) => ({ i }));
  const kept = capScrollback(lines);
  assert.equal(kept.length, 1000);
  assert.equal(kept[0].i, 500);
  assert.equal(kept[999].i, 1499);
});

test('capScrollback leaves short lists alone and honours max', () => {
  const lines = [1, 2, 3];
  assert.deepEqual(capScrollback(lines), [1, 2, 3]);
  assert.deepEqual(capScrollback(lines, 2), [2, 3]);
  assert.deepEqual(capScrollback([], 5), []);
});

test('maskCommand masks the auth token only', () => {
  assert.equal(maskCommand('auth s3cret'), 'auth ••••');
  assert.equal(maskCommand('auth   s3cret'), 'auth ••••');
  assert.equal(maskCommand('auth "se cret"'), 'auth ••••');
  assert.equal(maskCommand('ls'), 'ls');
  assert.equal(maskCommand('auth --clear'), 'auth --clear');
  assert.equal(maskCommand('auth'), 'auth');
  assert.equal(maskCommand('authors s3cret'), 'authors s3cret');
});

test('renderTable puts hostile strings in text, never elements', () => {
  const doc = makeDoc();
  const table = renderTable(doc, [HOSTILE], [[HOSTILE, 'ok']]);
  assert.equal(table.tagName, 'table');
  assert.equal(table.className, 'mr');
  const [thead, tbody] = table.children;
  const th = thead.children[0].children[0];
  assert.equal(th.tagName, 'th');
  assert.equal(th.textContent, HOSTILE);
  assert.equal(th.children.length, 0);
  const td = tbody.children[0].children[0];
  assert.equal(td.textContent, HOSTILE);
  assert.equal(td.children.length, 0);
  assert.equal(tbody.children[0].children[1].textContent, 'ok');
});

test('renderLine, renderKv, renderListing and renderPre use text only', () => {
  const doc = makeDoc();
  const line = renderLine(doc, HOSTILE, 'error');
  assert.equal(line.className, 'line error');
  assert.equal(line.textContent, HOSTILE);
  assert.equal(line.children.length, 0);

  const kv = renderKv(doc, [[HOSTILE, HOSTILE]]);
  assert.equal(kv.className, 'kv');
  assert.equal(kv.children[0].textContent, HOSTILE);
  assert.equal(kv.children[1].textContent, HOSTILE);
  assert.equal(kv.children[0].children.length, 0);

  const listing = renderListing(doc, [
    { name: HOSTILE, kind: 'md' },
    { name: 'x', kind: 'bogus" onclick="y' },
  ]);
  assert.equal(listing.className, 'listing');
  assert.equal(listing.children[0].className, 'file md');
  assert.equal(listing.children[0].textContent, HOSTILE);
  assert.equal(listing.children[1].className, 'file');

  const pre = renderPre(doc, HOSTILE);
  assert.equal(pre.tagName, 'pre');
  assert.equal(pre.textContent, HOSTILE);
  assert.equal(pre.children.length, 0);
});

test('renderBanner wraps the art in div.title-card > pre.banner as text only', () => {
  const doc = makeDoc();
  const card = renderBanner(doc, HOSTILE);
  assert.equal(card.tagName, 'div');
  assert.equal(card.className, 'title-card');
  assert.equal(card.children.length, 1);
  const pre = card.children[0];
  assert.equal(pre.tagName, 'pre');
  assert.equal(pre.className, 'banner');
  assert.equal(pre.textContent, HOSTILE);
  assert.equal(pre.children.length, 0);
});
