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
  createTerminal,
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

test('renderTable passes (rowIndex, colIndex) to cellClass', () => {
  const doc = makeDoc();
  const seen = [];
  const table = renderTable(doc, ['a', 'b'], [['1', 'x'], ['2', 'y']], {
    cellClass: (r, c) => {
      seen.push([r, c]);
      return c === 1 ? 'success' : '';
    },
  });
  assert.deepEqual(seen, [[0, 0], [0, 1], [1, 0], [1, 1]]);
  const tbody = table.children[1];
  assert.equal(tbody.children[1].children[1].className.includes('success'), true);
});

test('maskCommand masks quoted and obfuscated auth forms', () => {
  assert.equal(maskCommand('"auth" tok'), 'auth ••••');
  assert.equal(maskCommand('au""th tok'), 'auth ••••');
  assert.equal(maskCommand("'auth' a b c"), 'auth ••••');
  assert.equal(maskCommand('auth tok --x'), 'auth ••••');
  assert.equal(maskCommand('auth --token=abc'), 'auth ••••');
  assert.equal(maskCommand('auth "abc'), 'auth ••••');
  assert.equal(maskCommand('"auth" --clear'), '"auth" --clear');
});

// ---- richer fake DOM for createTerminal ----
function fakeEl(tag) {
  const listeners = {};
  const n = {
    tagName: tag,
    className: '',
    children: [],
    attrs: {},
    style: {},
    parent: null,
    _text: '',
    value: '',
    readOnly: false,
    selectionStart: 0,
    selectionEnd: 0,
    scrollTop: 0,
    scrollHeight: 0,
    appendChild(c) {
      c.parent = n;
      n.children.push(c);
      return c;
    },
    removeChild(c) {
      n.children = n.children.filter((x) => x !== c);
    },
    remove() {
      if (n.parent) n.parent.removeChild(n);
    },
    get firstChild() {
      return n.children[0] || null;
    },
    get childElementCount() {
      return n.children.length;
    },
    setAttribute(k, v) {
      n.attrs[k] = String(v);
    },
    setSelectionRange(a, b) {
      n.selectionStart = a;
      n.selectionEnd = b;
    },
    focus() {},
    addEventListener(t, f) {
      (listeners[t] ||= []).push(f);
    },
    fire(t, ev = {}) {
      const e = { preventDefault() { e.defaultPrevented = true; }, defaultPrevented: false, ...ev };
      for (const f of listeners[t] || []) f(e);
      return e;
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

function fakeTerminalDoc() {
  const nodes = { '#output': fakeEl('main'), '.prompt-line': fakeEl('div'), '.status-bar': null };
  return {
    nodes,
    body: fakeEl('body'),
    querySelector: (q) => nodes[q] || null,
    createElement: fakeEl,
    createTextNode: (t) => {
      const n = fakeEl('#text');
      n._text = t;
      return n;
    },
  };
}

function mkTerm({ run } = {}) {
  const doc = fakeTerminalDoc();
  const log = [];
  const history = {
    items: [],
    pos: 0,
    push(l) { this.items.push(l); this.pos = this.items.length; },
    prev() { if (!this.items.length) return undefined; if (this.pos > 0) this.pos--; return this.items[this.pos]; },
    next() { if (this.pos >= this.items.length) return undefined; this.pos++; return this.pos === this.items.length ? undefined : this.items[this.pos]; },
    reset() { this.pos = this.items.length; },
  };
  const registry = {
    get: (name) =>
      name === 'slow' ? { run: run || (() => new Promise(() => {})) } : name === 'echo' ? { run: (ctx) => ctx.out.line('ran', '') } : undefined,
    complete: (v) => (v === 'ec' ? { matches: ['echo'], ghost: 'ho' } : { matches: [], ghost: '' }),
  };
  const term = createTerminal({ root: doc, registry, history, api: {} });
  const input = doc.nodes['.prompt-line'].children[1].children[1];
  const display = doc.nodes['.prompt-line'].children[1].children[0];
  const out = doc.nodes['#output'];
  const type = (v) => {
    input.value = v;
    input.selectionStart = input.selectionEnd = v.length;
    input.fire('input');
  };
  const key = (k, extra = {}) => input.fire('keydown', { key: k, keyCode: 0, ...extra });
  const texts = () => out.children.map((c) => c.textContent);
  return { term, input, display, out, type, key, texts, history };
}

const tick = () => new Promise((r) => setTimeout(r, 5));

test('multi-line paste inserts the first line, warns, and never runs', async () => {
  const t = mkTerm();
  t.type('ls ');
  const e = t.input.fire('paste', { clipboardData: { getData: () => 'one\ntwo\nthree\n' } });
  assert.equal(e.defaultPrevented, true);
  assert.equal(t.input.value, 'ls one');
  await tick();
  const texts = t.texts();
  assert.ok(texts.some((x) => /pasted 3 lines/.test(x)));
  assert.ok(!texts.some((x) => x.includes('$')), 'nothing was echoed/run');
});

test('ArrowDown with no history navigation keeps typed text', () => {
  const t = mkTerm();
  t.history.push('ls');
  t.history.reset();
  t.type('half typed');
  t.key('ArrowDown');
  assert.equal(t.input.value, 'half typed');
  t.key('ArrowUp');
  assert.equal(t.input.value, 'ls');
  t.key('ArrowDown');
  assert.equal(t.input.value, 'half typed');
});

test('live display masks an auth line while typing', () => {
  const t = mkTerm();
  t.type('auth sekret123');
  const shown = t.display.textContent;
  assert.ok(!shown.includes('sekret123'));
  assert.ok(shown.includes('••••'));
  assert.equal(t.display.attrs['aria-hidden'], 'true');
});

test('Ctrl+C frees the prompt even when the command ignores its signal', async () => {
  const t = mkTerm();
  t.type('slow');
  t.key('Enter');
  await tick();
  t.key('c', { ctrlKey: true });
  await tick();
  t.type('echo');
  t.key('Enter');
  await tick();
  assert.ok(t.texts().includes('ran'), 'next command runs after ^C');
});

test('Tab only prevents default when there is a ghost to complete', () => {
  const t = mkTerm();
  t.type('zzz');
  assert.equal(t.key('Tab').defaultPrevented, false);
  t.type('ec');
  assert.equal(t.key('Tab').defaultPrevented, true);
  assert.equal(t.input.value, 'echo');
});

test('input can be made read-only until boot finishes', () => {
  const t = mkTerm();
  t.term.setInputEnabled(false);
  assert.equal(t.input.readOnly, true);
  t.term.setInputEnabled(true);
  assert.equal(t.input.readOnly, false);
});

test('scrollback is capped with a while loop on childElementCount', () => {
  const t = mkTerm();
  for (let i = 0; i < 1105; i++) t.term.ctx.out.line(`l${i}`);
  assert.equal(t.out.children.length, 1000);
  assert.equal(t.out.children[0].textContent, 'l105');
});

test('man animation is abortable and does not re-announce', async () => {
  const t = mkTerm();
  globalThis.requestAnimationFrame = (f) => setTimeout(() => f(performance.now()), 1);
  const p = t.term.ctx.out.man('x'.repeat(100000));
  const box = t.out.children[0];
  assert.equal(box.attrs['aria-live'], 'off');
  t.input.fire('keydown', { key: 'c', ctrlKey: true, keyCode: 0 });
  await Promise.race([p, new Promise((_, r) => setTimeout(() => r(new Error('man hung')), 500))]);
  delete globalThis.requestAnimationFrame;
});

test('banner and divider are aria-hidden', () => {
  const doc = makeDoc();
  assert.equal(renderBanner(doc, 'x').attrs['aria-hidden'], 'true');
});

// ---- ctx.takeover ----
async function startTakeover(opts = {}) {
  let view = null;
  const t = mkTerm({
    run: (ctx) => {
      view = ctx.takeover(opts);
      return new Promise(() => {});
    },
  });
  t.type('slow');
  t.key('Enter');
  await tick();
  return { t, view };
}

test('takeover draw builds spans via textContent; hostile text stays text', async () => {
  const { t, view } = await startTakeover({ label: 'demo' });
  const box = t.out.children.at(-1);
  assert.equal(box.className, 'takeover');
  assert.equal(box.attrs.role, 'application');
  assert.equal(box.attrs['aria-label'], 'demo');
  const pre = box.children[0];
  assert.equal(pre.tagName, 'pre');
  assert.equal(pre.className, 'takeover-grid');
  view.draw([HOSTILE, [[HOSTILE, 'hot'], [42, 'bad Cls'], ['x', 'g-ok']]]);
  assert.equal(pre.textContent, `${HOSTILE}\n${HOSTILE}42x`);
  const spans = pre.children.filter((c) => c.tagName === 'span');
  assert.equal(spans.length, 2);
  assert.equal(spans[0].className, 'g-hot');
  assert.equal(spans[1].className, 'g-g-ok');
  assert.equal(spans[0].children.length, 0);
  assert.ok(pre.children.every((c) => c.tagName !== 'img'));
  view.draw('nope');
  assert.equal(pre.textContent, `${HOSTILE}\n${HOSTILE}42x`, 'non-array draws nothing');
  view.setLabel('next');
  assert.equal(box.attrs['aria-label'], 'next');
});

test('takeover draw caps rows at 200 and each row at 400 chars', async () => {
  const { t, view } = await startTakeover();
  const pre = t.out.children.at(-1).children[0];
  view.draw(Array.from({ length: 300 }, () => 'a'));
  assert.equal(pre.textContent.split('\n').length, 200);
  view.draw([[['b'.repeat(300), 'ok'], ['c'.repeat(300), 'err'], ['d', 'dim']]]);
  assert.equal(pre.textContent.length, 400);
  assert.equal(pre.children.filter((c) => c.tagName === 'span').length, 2);
});

test('second takeover returns null while one is active', async () => {
  const { t, view } = await startTakeover();
  assert.ok(view);
  assert.equal(t.term.ctx.takeover({}), null);
  view.release();
  assert.ok(t.term.ctx.takeover({}));
});

test('takeover routes keys to onKey, not the input; release is idempotent and restores input', async () => {
  const keys = [];
  const { t, view } = await startTakeover({ onKey: (e) => keys.push(e) });
  t.type('half');
  const e = t.key('ArrowUp', { shiftKey: true });
  assert.equal(e.defaultPrevented, true);
  assert.deepEqual(keys[0], { key: 'ArrowUp', ctrlKey: false, shiftKey: true, altKey: false, metaKey: false });
  t.key('Enter');
  assert.equal(t.input.value, 'half', 'typed text preserved, nothing submitted');
  assert.equal(t.input.readOnly, true);
  // copy with a selection is left to the browser
  t.input.selectionStart = 0;
  t.input.selectionEnd = 2;
  const n = keys.length;
  assert.equal(t.key('c', { ctrlKey: true }).defaultPrevented, false);
  assert.equal(keys.length, n);
  // IME composition is not forwarded
  t.key('a', { isComposing: true });
  assert.equal(keys.length, n);
  view.release();
  view.release();
  await view.done;
  assert.equal(t.input.readOnly, false);
  assert.equal(t.input.value, 'half');
  assert.equal(t.out.children.at(-1).className, 'takeover ended');
  const before = keys.length;
  t.key('x');
  assert.equal(keys.length, before);
});

test('Ctrl+C releases an active takeover and frees the prompt', async () => {
  const keys = [];
  const { t, view } = await startTakeover({ onKey: (e) => keys.push(e) });
  view.draw(['frame']);
  t.input.selectionStart = t.input.selectionEnd = 0;
  t.key('c', { ctrlKey: true });
  await view.done;
  assert.equal(keys.length, 0);
  assert.equal(t.input.readOnly, false);
  const box = t.out.children.find((c) => c.className === 'takeover ended');
  assert.ok(box);
  assert.equal(box.textContent, 'frame');
  view.draw(['late']);
  assert.equal(box.textContent, 'frame', 'draw after release is ignored');
});

test('lines submitted during a takeover are queued', async () => {
  const { t, view } = await startTakeover();
  t.input.readOnly = false;
  t.input.value = 'echo';
  // Enter is swallowed by the takeover; the line stays in the input
  t.key('Enter');
  assert.equal(t.input.value, 'echo');
  assert.ok(!t.texts().includes('ran'));
  view.release();
});
