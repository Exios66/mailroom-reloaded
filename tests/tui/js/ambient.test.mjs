import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  buildSkylinePath,
  createAmbient,
  THEME_KEY,
} from '../../../src/mailroom_reloaded/api/tui/ambient.js';

function seeded(seed = 7) {
  let s = seed;
  return () => {
    s = (s * 16807) % 2147483647;
    return s / 2147483647;
  };
}

function makeNode(tag) {
  const classes = new Set();
  const n = {
    tagName: tag,
    children: [],
    attrs: {},
    style: { props: {}, setProperty(k, v) { n.style.props[k] = v; } },
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
    },
    appendChild(c) { n.children.push(c); return c; },
    setAttribute(k, v) { n.attrs[k] = String(v); },
    removeAttribute(k) { delete n.attrs[k]; },
  };
  Object.defineProperty(n, 'className', {
    get: () => [...classes].join(' '),
    set: (v) => { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
  });
  return n;
}

function makeDoc() {
  const parts = {
    '.ambient-skyline': makeNode('div'),
    '.crt-overlay': makeNode('div'),
    '.grain': makeNode('div'),
  };
  return {
    parts,
    documentElement: makeNode('html'),
    querySelector: (s) => parts[s] || null,
    createElement: makeNode,
    createElementNS: (_ns, tag) => makeNode(tag),
  };
}

const memory = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, v), m };
};

test('buildSkylinePath is closed, deterministic and bounded', () => {
  const a = buildSkylinePath(seeded());
  const b = buildSkylinePath(seeded());
  assert.equal(a, b);
  assert.ok(a.startsWith('M0 120'));
  assert.ok(a.endsWith('Z'));
  assert.notEqual(a, buildSkylinePath(seeded(11)));
  for (const m of a.matchAll(/L([\d.]+) (-?[\d.]+)/g)) {
    assert.ok(Number(m[2]) >= 60 - 74 - 0.1 && Number(m[2]) <= 120);
  }
});

test('createAmbient builds two skyline svgs and nine sparks', () => {
  const doc = makeDoc();
  createAmbient(doc, { storage: memory(), rnd: seeded() });
  const sky = doc.parts['.ambient-skyline'];
  const svgs = sky.children.filter((c) => c.tagName === 'svg');
  assert.deepEqual(svgs.map((s) => s.attrs.class), ['skyline back', 'skyline front']);
  assert.deepEqual(svgs.map((s) => s.children[0].attrs.class), ['sky-2', 'sky-1']);
  const sparks = sky.children.find((c) => c.className === 'ambient-sparks');
  assert.equal(sparks.children.length, 9);
});

test('reduced motion spawns no sparks and no flash', () => {
  const doc = makeDoc();
  const a = createAmbient(doc, { reducedMotion: true, storage: memory(), rnd: seeded() });
  assert.equal(doc.parts['.ambient-skyline'].children.some((c) => c.className === 'ambient-sparks'), false);
  a.setTheme('light');
  assert.equal(doc.documentElement.classList.contains('theme-flash'), false);
});

test('setCrt and setSkyline toggle .off', () => {
  const doc = makeDoc();
  const a = createAmbient(doc, { storage: memory() });
  a.setCrt(false);
  a.setSkyline(false);
  assert.ok(doc.parts['.crt-overlay'].classList.contains('off'));
  assert.ok(doc.parts['.ambient-skyline'].classList.contains('off'));
  assert.deepEqual([a.state().crt, a.state().skyline], [false, false]);
  a.setCrt(true);
  assert.ok(!doc.parts['.crt-overlay'].classList.contains('off'));
});

test('themes set data-theme/data-phosphor; amber clears phosphor; hc forces layers off', () => {
  const doc = makeDoc();
  const store = memory();
  const a = createAmbient(doc, { storage: store });
  const attrs = doc.documentElement.attrs;
  a.setTheme('green');
  assert.equal(attrs['data-phosphor'], 'green');
  a.setTheme('amber');
  assert.equal('data-phosphor' in attrs, false);
  a.setTheme('light');
  assert.equal(attrs['data-theme'], 'light');
  a.setTheme('hc');
  assert.equal(a.state().crt, false);
  assert.equal(a.state().skyline, false);
  assert.equal(a.setTheme('bogus'), false);
  assert.deepEqual(JSON.parse(store.m.get(THEME_KEY)), { theme: 'hc', phosphor: null });
});

test('saved theme is restored; throwing storage is tolerated', () => {
  const store = memory();
  store.setItem(THEME_KEY, JSON.stringify({ theme: 'light', phosphor: 'cyan' }));
  const doc = makeDoc();
  createAmbient(doc, { storage: store });
  assert.equal(doc.documentElement.attrs['data-theme'], 'light');
  assert.equal(doc.documentElement.attrs['data-phosphor'], 'cyan');

  const boom = {
    getItem() { throw new Error('blocked'); },
    setItem() { throw new Error('blocked'); },
  };
  const a = createAmbient(makeDoc(), { storage: boom });
  assert.equal(a.setTheme('light'), true);
  assert.equal(a.state().theme, 'light');
});
