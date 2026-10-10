import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { renderFrame, truncate } from '../../../src/mailroom_reloaded/api/tui/replay/grid.js';
import { registerPanel, getPanel, listPanels, resetPanels } from '../../../src/mailroom_reloaded/api/tui/replay/panels.js';
import { createModel } from '../../../src/mailroom_reloaded/api/tui/replay/model.js';
import { createRegistry, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { registerReplay } from '../../../src/mailroom_reloaded/api/tui/commands/replay.js';

const HOSTILE = 'evil\u202E\u200B<img src=x onerror=alert(1)>';
const CTRL = /[\u202a-\u202e\u2066-\u2069\u200b-\u200f\u061c\u2028]/;
const flat = (rows) => rows.map((r) => r.map((s) => s[0]).join(''));
const textOf = (rows) => flat(rows).join('\n');

function fakeModel(over = {}) {
  return {
    duration: 60,
    session: { id: 'run:r1', source: 'spans', approx: false, data_pruned: false },
    stations: [{ id: 'intake', label: 'Intake', kind: 'main', color_token: 'info' }],
    rollups: {},
    eventsBetween: () => [],
    eventsUpTo: () => [],
    scoresAt: () => [],
    generationsFor: () => [],
    docAt: () => null,
    runningTotalsAt: () => ({ tokens: 0, cost_usd: 0, llm_calls: 0 }),
    ...over,
  };
}

const st = { t: 12, docs: [{ doc_id: 'd1', filename: 'a.pdf', station: 'intake', status: 'ok' }], counts: { intake: 1 }, done: 0, failed: 0, active: 1 };
const clock = { t: 12, playing: false, speed: 1, duration: 60, ended: false };

afterEach(() => resetPanels());

test('registerPanel adds a panel, listPanels orders it, duplicates throw', () => {
  const spec = registerPanel({ id: 'custom', title: 'Custom', render: () => [] });
  assert.equal(spec.id, 'custom');
  assert.equal(getPanel('custom').title, 'Custom');
  const ids = listPanels().map((p) => p.id);
  assert.ok(ids.includes('custom'));
  assert.deepEqual(ids.slice(0, 5), ['metrics', 'tokens', 'decisions', 'latency', 'fields']);
  assert.deepEqual(listPanels().find((p) => p.id === 'custom'), { id: 'custom', title: 'Custom' });
  assert.equal(getPanel('missing'), null);
  assert.throws(() => registerPanel({ id: 'custom', render: () => [] }), /already registered/);
  assert.throws(() => registerPanel({ id: '  ', render: () => [] }), /panel id must be/);
  assert.throws(() => registerPanel({ id: 'x', render: 1 }), /render must be a function/);
  assert.throws(() => registerPanel(null), /panel id must be/);
});

test('renderFrame resolves a registered id; unknown id yields no panel rows', () => {
  registerPanel({ id: 'hello', title: 'Hello', render: () => [[[' hello panel ', 'info']]] });
  const withPanel = textOf(renderFrame({ model: {}, st: {}, clock: {}, cols: 80, rows: 30, panel: 'hello' }));
  assert.ok(withPanel.includes('hello panel'));
  const unknown = textOf(renderFrame({ model: {}, st: {}, clock: {}, cols: 80, rows: 30, panel: 'nope' }));
  assert.ok(!unknown.includes('hello panel'));
  assert.doesNotThrow(() => renderFrame({ model: {}, st: {}, clock: {}, cols: 80, rows: 30, panel: 'nope' }));
  // a throwing panel must not take the frame down
  registerPanel({ id: 'boom', render: () => { throw new Error('x'); } });
  assert.doesNotThrow(() => renderFrame({ model: {}, st: {}, clock: {}, cols: 80, rows: 30, panel: 'boom' }));
});

test('every built-in panel renders on a minimal model and a hostile timeline', () => {
  const minimalModel = fakeModel({ rollups: {}, runningTotalsAt: () => ({}), eventsUpTo: () => null, scoresAt: () => null });
  for (const { id } of listPanels()) {
    assert.doesNotThrow(() => renderFrame({ model: minimalModel, st: {}, clock: {}, cols: 100, rows: 30, panel: id }), id);
    assert.doesNotThrow(() => renderFrame({ model: {}, st: {}, clock: {}, cols: 100, rows: 20, panel: id }), `bare/${id}`);
  }

  const hostileModel = fakeModel({
    session: { id: HOSTILE, source: 'spans', approx: false, data_pruned: false },
    rollups: { per_station: { [HOSTILE]: { p50_s: 1, p95_s: 2, n: 3 } }, first_pass_rate: 0.5 },
    eventsUpTo: () => [{ kind: HOSTILE }, { kind: 'retry' }],
    scoresAt: () => [{ name: HOSTILE, value: HOSTILE }],
  });
  const hostileSt = { ...st, docs: [{ doc_id: 'd1', filename: HOSTILE, station: 'intake', status: 'ok' }] };
  for (const { id } of listPanels()) {
    assert.doesNotThrow(() => renderFrame({ model: hostileModel, st: hostileSt, clock, sel: 0, cols: 160, rows: 40, panel: id }), id);
  }
  const fieldsOut = textOf(renderFrame({ model: hostileModel, st: hostileSt, clock, sel: 0, cols: 160, rows: 40, panel: 'fields' }));
  assert.ok(fieldsOut.includes('<img src=x onerror=alert(1)>'), 'hostile string stays literal text');
  assert.ok(!fieldsOut.includes('&lt;img'), 'no HTML escaping/markup path');
  assert.ok(!CTRL.test(fieldsOut), 'control/bidi characters are neutralised');
});

test('registerPanel rejects reserved ids and bad ids, and cleans the title', () => {
  for (const id of ['none', 'inspector', 'ledger', 'metrics', 'tokens', 'decisions', 'latency', 'fields']) {
    assert.throws(() => registerPanel({ id, render: () => [] }), /reserved/, id);
  }
  for (const id of ['', 'a b', 'x'.repeat(33), 'a\u202eb', '<x>', 7]) {
    assert.throws(() => registerPanel({ id, render: () => [] }), /panel id must be/, String(id));
  }
  const spec = registerPanel({ id: 'tidy', title: `T\u202e\u0007${'z'.repeat(80)}`, render: () => [] });
  assert.ok(!CTRL.test(spec.title) && !/\u0007/.test(spec.title));
  assert.equal(Array.from(spec.title).length, 40);
  assert.equal(registerPanel({ id: 'plain', render: () => [] }).title, 'plain');
  assert.ok(!('key' in spec), 'no per-panel key in the contract');
  assert.deepEqual(listPanels().find((p) => p.id === 'tidy'), { id: 'tidy', title: spec.title });
});

test('custom panel rows are sanitised by the grid', () => {
  registerPanel({
    id: 'dirty',
    render: () => [
      [[`a\u202eb\u0000c${'y'.repeat(900)}`, 'nonsense'], [42, 'ok'], ['ok text', 'warn'], 'junk', [null]],
      'not a row',
      [[HOSTILE, 'info']],
    ],
  });
  const rows = renderFrame({ model: {}, st: {}, clock: {}, cols: 80, rows: 30, panel: 'dirty' });
  const out = textOf(rows);
  assert.ok(!CTRL.test(out) && !out.includes('\u0000'));
  assert.ok(out.includes('a b c'));
  rows.forEach((r) => assert.ok(r.map((s) => s[0]).join('').length <= 80));
  const classes = rows.flat().map((s) => s[1]);
  assert.ok(!classes.includes('nonsense'));
  assert.ok(out.includes('<img src=x onerror=alert(1)>'), 'markup stays literal text');
});

test('resetPanels restores exactly the built-in set', () => {
  const builtin = listPanels().map((p) => p.id);
  registerPanel({ id: 'temp', title: 'Temp', render: () => [] });
  assert.ok(listPanels().some((p) => p.id === 'temp'));
  resetPanels();
  assert.deepEqual(listPanels().map((p) => p.id), builtin);
  assert.equal(getPanel('temp'), null);
});

test('truncate/clean semantics still neutralise control characters in a built-in row', () => {
  const evil = 'a\u202Eb\u2066c\u200Fd\u061Ce\u200Bf\u2028g';
  assert.equal(truncate(evil, 40), 'a b c d e f g');
  const st2 = { ...st, docs: [{ doc_id: 'd1', filename: evil, station: 'intake', status: 'ok' }] };
  const out = textOf(
    renderFrame({ model: fakeModel({ scoresAt: () => [{ name: evil, value: evil }] }), st: st2, clock, sel: 0, cols: 160, rows: 30, panel: 'fields' }),
  );
  assert.ok(!CTRL.test(out));
});

// ------------------------------------------------------------------ command p key

const TIMELINE = {
  version: 'replay/v1',
  session: { id: 'run:r1', kind: 'run', source: 'spans', approx: false, duration_s: 60, data_pruned: false },
  stations: [{ id: 'intake', label: 'Intake', phase: 'a', order: 0, kind: 'main' }],
  entities: [{ doc_id: 'd1', filename: 'a.pdf', t_start: 0, t_end: 60 }],
  segments: [{ doc_id: 'd1', node: 'intake', station: 'intake', t0: 0, t1: 30, attempt: 1, status: 'ok' }],
  generations: [],
  events: [],
  scores: [],
  rollups: {},
};

function makeHarness() {
  const calls = [];
  const controller = new AbortController();
  const timers = { fns: [], setInterval(fn, ms) { this.fns.push({ fn, ms }); return this.fns.length; }, clearInterval() {} };
  const handler = async (path, arg) => {
    calls.push({ path, arg });
    if (path.endsWith('/timeline')) return TIMELINE;
    throw new Error(`unexpected ${path}`);
  };
  const view = { frames: [], released: 0 };
  let resolveDone;
  view.done = new Promise((r) => {
    resolveDone = r;
  });
  view.draw = (r) => view.frames.push(r);
  view.setLabel = () => {};
  view.release = () => {
    view.released += 1;
    resolveDone();
  };
  const tk = {};
  const ctx = {
    out: { line() {}, table() {}, kv() {}, man() {} },
    api: { get: handler, post: handler },
    signal: () => controller.signal,
    now: () => 0,
    timers,
    gridSize: () => ({ cols: 100, rows: 30 }),
    takeover: (opts) => {
      tk.opts = opts;
      return view;
    },
  };
  controller.signal.addEventListener('abort', () => resolveDone(), { once: true });
  return { ctx, calls, view, tk };
}

const key = (k, extra = {}) => ({ key: k, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, ...extra });
const nextTick = () => new Promise((r) => setImmediate(r));

test('p cycles insight panels, starts from another panel, and never fetches the ledger', async () => {
  const r = createRegistry();
  registerReplay(r);
  const h = makeHarness();
  const p = dispatch(r, h.ctx, 'replay r1');
  await nextTick();
  const last = () => textOf(h.view.frames.at(-1));
  for (const id of ['metrics', 'tokens', 'decisions', 'latency', 'fields']) {
    h.tk.opts.onKey(key('p'));
    assert.match(last(), new RegExp(`─ ${id}`), id);
  }
  h.tk.opts.onKey(key('p'));
  assert.ok(!/─ (metrics|tokens|decisions|latency|fields)/.test(last()), 'wraps back to none');
  h.tk.opts.onKey(key('i'));
  assert.match(last(), /─ inspector/);
  h.tk.opts.onKey(key('p'));
  assert.match(last(), /─ metrics/, 'p works while inspector is open');
  assert.equal(h.calls.filter((c) => c.path.startsWith('/v1/ledger')).length, 0);
  h.tk.opts.onKey(key('q'));
  await p;
});

function runModel() {
  return createModel({
    session: { id: 'run:r1', duration_s: 40 },
    entities: [
      { doc_id: 'a', filename: 'a.pdf', t_start: 1, t_end: 10, final_status: 'archived' },
      { doc_id: 'b', filename: 'b.pdf', t_start: 12, t_end: 30, final_status: 'archived' },
    ],
    segments: [
      { doc_id: 'a', node: 'sort', station: 'sorter', t0: 2, t1: 4, status: 'ok' },
      { doc_id: 'b', node: 'sort', station: 'sorter', t0: 13, t1: 19, status: 'ok' },
    ],
    generations: [
      { doc_id: 'a', span_id: 'g1', t0: 2, t1: 3, prompt_tokens: 1000, completion_tokens: 234, cost_usd: 0.5 },
      { doc_id: 'b', span_id: 'g2', t0: 14, t1: 15, prompt_tokens: 2000, completion_tokens: 500, cost_usd: 0.25 },
    ],
    rollups: {
      tokens: 3734,
      cost_usd: 0.75,
      first_pass_rate: 0.5,
      per_station: { sorter: { p50_s: 4.0, p95_s: 5.8, n: 2 } },
    },
  });
}

function panelAt(id, model, t) {
  const st = model.stateAt(t);
  return textOf(getPanel(id).render({ model, st, clock: { t }, cols: 100 }));
}

test('metrics, tokens and latency panels show nothing from after the playhead', () => {
  const m = runModel();
  const metrics = panelAt('metrics', m, 0);
  assert.match(metrics, /docs 0 of 2/);
  assert.match(metrics, /first-pass --/);
  assert.doesNotMatch(metrics, /first-pass 50%/);
  const tokens = panelAt('tokens', m, 0);
  assert.match(tokens, /tokens 0 /);
  assert.match(tokens, /\$0\.0000/);
  assert.doesNotMatch(tokens, /3\.7k|0\.75/);
  const latency = panelAt('latency', m, 0);
  assert.match(latency, /p50\/p95 —/);
  assert.doesNotMatch(latency, /sorter/);
});

test('panels at mid-run use only ended segments and finished documents', () => {
  const m = runModel();
  const latency = panelAt('latency', m, 5);
  assert.match(latency, /sorter\s+2\.00\s+2\.00 n 1/);
  assert.match(panelAt('metrics', m, 5), /docs 1 of 2/);
});

test('after the run ends the panels show the full totals', () => {
  const m = runModel();
  const metrics = panelAt('metrics', m, 40);
  assert.match(metrics, /docs 2 of 2/);
  assert.match(metrics, /first-pass 100%/);
  const tokens = panelAt('tokens', m, 40);
  assert.match(tokens, /tokens 3\.7k/);
  assert.match(tokens, /\$0\.7500/);
  assert.match(panelAt('latency', m, 40), /sorter\s+4\.00\s+5\.80 n 2/);
});
