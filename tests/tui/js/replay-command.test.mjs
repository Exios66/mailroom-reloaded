import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRegistry, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { ApiError } from '../../../src/mailroom_reloaded/api/tui/api.js';
import { registerReplay, normalizeSessionId, boundLiveTimeline, LIVE_MAX_ITEMS } from '../../../src/mailroom_reloaded/api/tui/commands/replay.js';
import { createModel } from '../../../src/mailroom_reloaded/api/tui/replay/model.js';

const TL = {
  version: 'replay/v1',
  session: { id: 'run:r1', kind: 'run', source: 'spans', approx: false, duration_s: 100, data_pruned: false },
  stations: [{ id: 'intake', label: 'Intake', phase: 'a', order: 0, kind: 'main' }],
  entities: [
    { doc_id: 'd1', filename: '<b>x</b>.pdf', t_start: 0, t_end: 100 },
    { doc_id: 'd2', filename: 'y.pdf', t_start: 0, t_end: 100 },
  ],
  segments: [
    { doc_id: 'd1', node: 'intake', station: 'intake', t0: 0, t1: 50, attempt: 1, status: 'ok' },
    { doc_id: 'd2', node: 'intake', station: 'intake', t0: 0, t1: 50, attempt: 1, status: 'ok' },
  ],
  generations: [],
  events: [{ t: 30, doc_id: 'd1', kind: 'gate', station: 'intake', payload: {} }],
  scores: [],
  rollups: {},
};

const LINKS = {
  public_url: 'http://localhost:8000',
  phoenix_url: 'http://localhost:6006',
  grafana_url: 'http://localhost:3000',
  phoenix_project: 'mailroom-live',
};

function fakeTimers() {
  const t = { fns: [], cleared: 0 };
  t.setInterval = (fn, ms) => {
    t.fns.push({ fn, ms });
    return t.fns.length;
  };
  t.clearInterval = () => {
    t.cleared += 1;
  };
  t.tick = () => t.fns.forEach((x) => x.fn());
  return t;
}

function makeCtx({ routes = {}, takeover = 'ok', clockMs = { v: 0 }, open = null, fetchFn = null } = {}) {
  const calls = [];
  const out = [];
  const controller = new AbortController();
  const timers = fakeTimers();
  const handler = async (path, arg) => {
    calls.push({ path, arg });
    const r = routes[path];
    if (r === undefined) throw new Error(`unrouted ${path}`);
    if (r instanceof Error) throw r;
    return typeof r === 'function' ? r(arg) : r;
  };
  const view = { frames: [], released: 0, labels: [] };
  let resolveDone;
  view.done = new Promise((r) => {
    resolveDone = r;
  });
  view.draw = (rows) => view.frames.push(rows);
  view.setLabel = (l) => view.labels.push(l);
  view.release = () => {
    view.released += 1;
    resolveDone();
  };
  const tk = { calls: 0 };
  const ctx = {
    out: {
      line: (t, c) => out.push({ k: 'line', t, c }),
      table: (h, rows) => out.push({ k: 'table', h, rows }),
      kv: () => {},
      man: () => {},
    },
    api: { get: handler, post: handler, authHeaders: () => ({}) },
    fetch: fetchFn ?? undefined,
    signal: () => controller.signal,
    now: () => clockMs.v,
    timers,
    gridSize: () => ({ cols: 100, rows: 30 }),
    open: open ?? undefined,
    takeover: (opts) => {
      tk.calls += 1;
      tk.opts = opts;
      return takeover === 'ok' ? view : null;
    },
  };
  controller.signal.addEventListener('abort', () => resolveDone(), { once: true });
  return { ctx, calls, out, controller, timers, view, tk, clockMs };
}

const setup = () => {
  const r = createRegistry();
  registerReplay(r);
  return r;
};
const lines = (out) => out.filter((o) => o.k === 'line');
const key = (k, extra = {}) => ({ key: k, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, ...extra });
const nextTick = () => new Promise((r) => setImmediate(r));
const lastText = (view) => view.frames.at(-1).map((r) => r.map((s) => s[0]).join('')).join('\n');

async function open(args, h = makeCtx({ routes: { '/links': LINKS, '/v1/replay/sessions/run%3Ar1/timeline': TL } })) {
  const p = dispatch(setup(), h.ctx, `replay ${args}`);
  await nextTick();
  return { h, p };
}

const encoder = new TextEncoder();

/** A controllable SSE body: `push(name, obj)` a frame, `close()` ends the stream. */
function sseBody() {
  let controller;
  const stream = new ReadableStream({
    start(c) {
      controller = c;
    },
  });
  return {
    stream,
    push: (name, obj) => controller.enqueue(encoder.encode(`event: ${name}\ndata: ${JSON.stringify(obj)}\n\n`)),
    close: () => controller.close(),
  };
}

/** A fake ctx.fetch that records calls and exposes each stream for the test to drive. */
async function fakeLiveFetch(url, { headers, signal } = {}) {
  const sse = sseBody();
  const rec = { url, headers, sse, aborted: false };
  fakeLiveFetch.calls.push(rec);
  if (signal) signal.addEventListener('abort', () => { rec.aborted = true; });
  return { ok: true, status: 200, body: sse.stream };
}
fakeLiveFetch.calls = [];

test('registered with man page', () => {
  const spec = setup().get('replay');
  assert.match(spec.man, /^NAME\n[\s\S]*SYNOPSIS[\s\S]*DESCRIPTION/);
});

test('normalizeSessionId', () => {
  assert.equal(normalizeSessionId('abc'), 'run:abc');
  assert.equal(normalizeSessionId('run:abc'), 'run:abc');
  assert.equal(normalizeSessionId('window:r1:10-20'), 'window:r1:10-20');
  for (const bad of ['', '..', 'a/b', 'evil:abc', 'run:', 'run:../x', 'run:a b', 'doc:<x>', '?x']) {
    assert.equal(normalizeSessionId(bad), null, bad);
  }
});

test('list renders a table', async () => {
  const h = makeCtx({
    routes: {
      '/v1/replay/sessions': {
        sessions: [
          { id: 'run:r1', documents: 3, started_at: '2026-05-01T12:00:00+00:00', duration_s: 75.2, source: 'spans', data_pruned: false },
          { id: 'run:r2', documents: 1, started_at: '', duration_s: 5, source: 'audit', data_pruned: true },
        ],
      },
    },
  });
  await dispatch(setup(), h.ctx, 'replay --limit 5');
  assert.deepEqual(h.calls[0], { path: '/v1/replay/sessions', arg: { limit: 5 } });
  const t = h.out.find((o) => o.k === 'table');
  assert.deepEqual(t.h, ['id', 'docs', 'started', 'duration', 'source', 'pruned']);
  assert.deepEqual(t.rows[0], ['run:r1', '3', '2026-05-01 12:00:00', '01:15.2', 'spans', '—']);
  assert.equal(t.rows[1][5], 'pruned');
});

test('empty list', async () => {
  const h = makeCtx({ routes: { '/v1/replay/sessions': { sessions: [] } } });
  await dispatch(setup(), h.ctx, 'replay');
  assert.equal(h.calls[0].arg.limit, 20);
  assert.match(lines(h.out)[0].t, /no replayable sessions/);
});

test('validation errors send no request', async () => {
  for (const line of [
    'replay --limit 0',
    'replay --limit 101',
    'replay --bogus',
    'replay a b',
    'replay ../x',
    'replay evil:abc',
    'replay r1 --speed 3',
    'replay r1 --at -4',
    'replay r1 --at abc',
    'replay r1 --limit 5',
    'replay r1 --nope',
    'replay r1 --follow 1',
  ]) {
    const h = makeCtx();
    await dispatch(setup(), h.ctx, line);
    assert.equal(h.calls.length, 0, line);
    assert.equal(lines(h.out)[0].c, 'error', line);
  }
});

test('410, 404 and offline wording', async () => {
  const p = '/v1/replay/sessions/run%3Ar1/timeline';
  let h = makeCtx({ routes: { [p]: new ApiError('gone', { kind: 'http', status: 410 }) } });
  await dispatch(setup(), h.ctx, 'replay r1');
  assert.equal(lines(h.out)[0].t, "replay: run r1 — data pruned (ledger entry kept; try 'ledger --run r1')");
  assert.equal(h.tk.calls, 0);
  h = makeCtx({ routes: { [p]: new ApiError('nf', { kind: 'http', status: 404 }) } });
  await dispatch(setup(), h.ctx, 'replay r1');
  assert.equal(lines(h.out)[0].t, 'replay: no such session');
  h = makeCtx({ routes: { [p]: new ApiError('x', { kind: 'offline' }) } });
  await dispatch(setup(), h.ctx, 'replay r1');
  assert.match(lines(h.out)[0].t, /^replay: api unreachable/);
});

test('opens takeover, ticks, handles keys and quits', async () => {
  const { h, p } = await open('r1');
  assert.equal(h.tk.calls, 1);
  assert.match(h.tk.opts.label, /run:r1/);
  assert.equal(h.timers.fns[0].ms, 100);
  assert.ok(h.view.frames.length >= 1);
  assert.match(lastText(h.view), />.*00:00\.0 \/ 01:40\.0/);
  // time advances by clock
  h.clockMs.v = 10000;
  h.timers.tick();
  assert.match(lastText(h.view), /00:10\.0/);
  // space pauses
  h.tk.opts.onKey(key(' '));
  assert.match(lastText(h.view), /\|\|/);
  const n = h.view.frames.length;
  h.clockMs.v = 20000;
  h.timers.tick();
  assert.equal(h.view.frames.length, n, 'no redraw when nothing changed');
  // seek
  h.tk.opts.onKey(key('5'));
  assert.match(lastText(h.view), /00:50\.0/);
  h.tk.opts.onKey(key('Home'));
  assert.match(lastText(h.view), /00:00\.0 \//);
  h.tk.opts.onKey(key('End'));
  assert.match(lastText(h.view), /01:40\.0 \//);
  h.tk.opts.onKey(key('Home'));
  h.tk.opts.onKey(key('ArrowRight'));
  assert.match(lastText(h.view), /00:05\.0/);
  h.tk.opts.onKey(key('ArrowRight', { shiftKey: true }));
  assert.match(lastText(h.view), /00:35\.0/);
  h.tk.opts.onKey(key('Home'));
  h.tk.opts.onKey(key('e'));
  assert.match(lastText(h.view), /00:30\.0/);
  // speed
  h.tk.opts.onKey(key(']'));
  assert.match(lastText(h.view), /x2/);
  // select + inspector, hostile filename literal
  h.tk.opts.onKey(key('j'));
  h.tk.opts.onKey(key('i'));
  assert.match(lastText(h.view), /inspector[\s\S]*<b>x<\/b>\.pdf/);
  // unknown / modified keys ignored
  const m = h.view.frames.length;
  h.tk.opts.onKey(key('z'));
  h.tk.opts.onKey(key('q', { ctrlKey: true }));
  assert.equal(h.view.frames.length, m);
  assert.equal(h.view.released, 0);
  h.tk.opts.onKey(key('q'));
  await p;
  assert.equal(h.view.released, 1);
  assert.ok(h.timers.cleared >= 1);
  const l = lines(h.out).at(-1);
  assert.deepEqual([l.t, l.c], ['replay closed', 'dim']);
});

test('--at starts paused at t and --speed applies', async () => {
  const { h, p } = await open('r1 --at 42.5 --speed 4');
  const t = lastText(h.view);
  assert.match(t, /\|\|/);
  assert.match(t, /00:42\.5/);
  assert.match(t, /x4/);
  h.tk.opts.onKey(key('Escape'));
  await p;
});

test('ledger is fetched lazily, once, on l', async () => {
  const routes = {
    '/links': LINKS,
    '/v1/replay/sessions/run%3Ar1/timeline': TL,
    '/v1/ledger': { entries: [{ seq: 4, kind: 'doc_closed', entry_hash: 'abcdef0123456789' }] },
    '/v1/ledger/verify': { ok: true, count: 4 },
  };
  const { h, p } = await open('r1', makeCtx({ routes }));
  assert.equal(h.calls.length, 2); // timeline + /links
  assert.equal(h.calls[0].path, '/v1/replay/sessions/run%3Ar1/timeline');
  h.tk.opts.onKey(key('l'));
  assert.match(lastText(h.view), /loading…/);
  await nextTick();
  await nextTick();
  assert.match(lastText(h.view), /doc_closed[\s\S]*chain ok — 4 entries/);
  const ledgerCalls = h.calls.filter((c) => c.path.startsWith('/v1/ledger'));
  assert.deepEqual(ledgerCalls.map((c) => c.arg), [{ run_id: 'r1', limit: 20 }, { run_id: 'r1' }]);
  h.tk.opts.onKey(key('l')); // close
  h.tk.opts.onKey(key('l')); // reopen from cache
  await nextTick();
  assert.equal(h.calls.filter((c) => c.path.startsWith('/v1/ledger')).length, 2);
  h.tk.opts.onKey(key('q'));
  await p;
});

test('ledger failure shows unavailable without blocking', async () => {
  const routes = {
    '/v1/replay/sessions/run%3Ar1/timeline': TL,
    '/v1/ledger': new ApiError('x', { kind: 'offline' }),
    '/v1/ledger/verify': new ApiError('x', { kind: 'offline' }),
  };
  const { h, p } = await open('r1', makeCtx({ routes }));
  h.tk.opts.onKey(key('l'));
  await nextTick();
  await nextTick();
  assert.match(lastText(h.view), /unavailable/);
  h.tk.opts.onKey(key('q'));
  await p;
});

test('abort releases the wait and clears the timer', async () => {
  const { h, p } = await open('r1');
  h.controller.abort();
  await p;
  assert.ok(h.timers.cleared >= 1);
  h.timers.tick(); // stray tick after abort must not draw
  const n = h.view.frames.length;
  h.timers.tick();
  assert.equal(h.view.frames.length, n);
  assert.ok(!lines(h.out).some((l) => l.t === 'replay closed'));
});

test('null takeover prints an error line', async () => {
  const h = makeCtx({ routes: { '/v1/replay/sessions/run%3Ar1/timeline': TL }, takeover: null });
  await dispatch(setup(), h.ctx, 'replay r1');
  assert.equal(lines(h.out)[0].c, 'error');
  assert.match(lines(h.out)[0].t, /^replay: /);
  assert.equal(h.timers.fns.length, 0);
});

test('reduced motion starts paused', async () => {
  globalThis.matchMedia = () => ({ matches: true });
  try {
    const { h, p } = await open('r1');
    assert.match(lastText(h.view), /\|\|/);
    h.clockMs.v = 5000;
    h.timers.tick();
    assert.match(lastText(h.view), /00:00\.0 \//);
    h.tk.opts.onKey(key('q'));
    await p;
  } finally {
    delete globalThis.matchMedia;
  }
});

test('a draw failure releases the viewer and the timer', async () => {
  const h = makeCtx({ routes: { '/v1/replay/sessions/run%3Ar1/timeline': TL } });
  let n = 0;
  const orig = h.view.draw;
  h.view.draw = (rows) => {
    n += 1;
    if (n > 1) throw new Error('boom');
    orig(rows);
  };
  const p = dispatch(setup(), h.ctx, 'replay r1');
  await nextTick();
  h.clockMs.v = 5000;
  h.timers.tick();
  await p;
  assert.equal(h.view.released, 1);
  assert.ok(h.timers.cleared >= 1);
  assert.ok(lines(h.out).some((l) => l.c === 'error' && /could not draw/.test(l.t)));
});

test('a first-frame failure still releases the viewer', async () => {
  const h = makeCtx({ routes: { '/v1/replay/sessions/run%3Ar1/timeline': TL } });
  h.view.draw = () => {
    throw new Error('boom');
  };
  await dispatch(setup(), h.ctx, 'replay r1');
  assert.equal(h.view.released, 1);
  assert.equal(h.timers.fns.length, 0);
});

test('a ledger response arriving after close is ignored', async () => {
  let resolveList;
  const routes = {
    '/v1/replay/sessions/run%3Ar1/timeline': TL,
    '/v1/ledger': () => new Promise((r) => { resolveList = r; }),
    '/v1/ledger/verify': { ok: true, count: 1 },
  };
  const { h, p } = await open('r1', makeCtx({ routes }));
  h.tk.opts.onKey(key('l'));
  h.tk.opts.onKey(key('q'));
  await p;
  const n = h.view.frames.length;
  resolveList({ entries: [] });
  await nextTick();
  await nextTick();
  assert.equal(h.view.frames.length, n);
});

test('ledger unavailable is retried on the next open', async () => {
  let fail = true;
  const routes = {
    '/v1/replay/sessions/run%3Ar1/timeline': TL,
    '/v1/ledger': () => {
      if (fail) throw new ApiError('x', { kind: 'offline' });
      return { entries: [{ seq: 1, kind: 'run_closed', entry_hash: 'abcdef0123456789' }] };
    },
    '/v1/ledger/verify': { ok: true, count: 1 },
  };
  const { h, p } = await open('r1', makeCtx({ routes }));
  h.tk.opts.onKey(key('l'));
  await nextTick();
  await nextTick();
  assert.match(lastText(h.view), /unavailable/);
  fail = false;
  h.tk.opts.onKey(key('l'));
  h.tk.opts.onKey(key('l'));
  await nextTick();
  await nextTick();
  assert.match(lastText(h.view), /run_closed/);
  h.tk.opts.onKey(key('q'));
  await p;
});

test('e seeks to the next event even with a huge event list', async () => {
  const events = Array.from({ length: 150000 }, (_, i) => ({ t: 10 + i * 0.0001, doc_id: 'd1', kind: 'k' }));
  const big = { ...TL, events, session: { ...TL.session, duration_s: 100 } };
  const { h, p } = await open('r1 --at 0', makeCtx({ routes: { '/v1/replay/sessions/run%3Ar1/timeline': big } }));
  h.tk.opts.onKey(key('e'));
  assert.match(lastText(h.view), /00:10\.0/);
  h.tk.opts.onKey(key('q'));
  await p;
});

test('the inspector shows the Phoenix and Grafana links for the run', async () => {
  const { h, p } = await open('r1');
  assert.equal(h.calls[1].path, '/links');
  h.tk.opts.onKey(key('i'));
  const t = lastText(h.view);
  assert.match(t, /phoenix\s+http:\/\/localhost:6006/);
  assert.match(t, /grafana\s+http:\/\/localhost:3000\/d\/mailroom-quality\?var-run_id=r1/);
  h.tk.opts.onKey(key('q'));
  await p;
});

test('o and g open Phoenix and Grafana with the injected opener', async () => {
  const opened = [];
  const h = makeCtx({
    routes: { '/links': LINKS, '/v1/replay/sessions/run%3Ar1/timeline': TL },
    open: (url, target, features) => opened.push({ url, target, features }),
  });
  const p = dispatch(setup(), h.ctx, 'replay r1');
  await nextTick();
  h.tk.opts.onKey(key('o'));
  h.tk.opts.onKey(key('g'));
  assert.deepEqual(opened, [
    { url: 'http://localhost:6006', target: '_blank', features: 'noopener' },
    { url: 'http://localhost:3000/d/mailroom-quality?var-run_id=r1', target: '_blank', features: 'noopener' },
  ]);
  h.tk.opts.onKey(key('q'));
  await p;
});

test('o and g refuse non-http(s) or credential-bearing link config', async () => {
  const opened = [];
  const bad = { phoenix_url: 'javascript:alert(1)', grafana_url: 'https://u:p@g.example' };
  const h = makeCtx({
    routes: { '/links': bad, '/v1/replay/sessions/run%3Ar1/timeline': TL },
    open: (url) => opened.push(url),
  });
  const p = dispatch(setup(), h.ctx, 'replay r1');
  await nextTick();
  h.tk.opts.onKey(key('o'));
  h.tk.opts.onKey(key('g'));
  assert.deepEqual(opened, []);
  h.tk.opts.onKey(key('q'));
  await p;
});

test('o and g are a no-op without an opener or link config', async () => {
  const { h, p } = await open('r1', makeCtx({ routes: { '/v1/replay/sessions/run%3Ar1/timeline': TL } }));
  assert.doesNotThrow(() => {
    h.tk.opts.onKey(key('o'));
    h.tk.opts.onKey(key('g'));
  });
  h.tk.opts.onKey(key('q'));
  await p;
});

test('a hostile run id stays literal text in the link line', async () => {
  const hostile = { ...TL, session: { ...TL.session, id: 'run:<img src=x onerror=alert(1)>' } };
  const h = makeCtx({
    routes: { '/links': LINKS, '/v1/replay/sessions/run%3Ar1/timeline': hostile },
  });
  const p = dispatch(setup(), h.ctx, 'replay r1');
  await nextTick();
  h.tk.opts.onKey(key('i'));
  const t = lastText(h.view);
  assert.ok(t.includes('<img src=x')); // rendered as text, never markup
  assert.match(t, /var-run_id=%3Cimg%20src%3Dx/); // encoded in the URL
  assert.ok(!/[\u0000-\u0009\u000b-\u001f\u007f-\u009f\u202a-\u202e]/.test(t));
  h.tk.opts.onKey(key('q'));
  await p;
});

test('the man page documents --follow and the f key', () => {
  const spec = setup().get('replay');
  assert.match(spec.man, /--follow/);
  assert.match(spec.man, /f\s+follow/);
});

test('--follow grows the model and pins the clock to now-2s', async () => {
  const realNow = Date.now;
  const F = 1_700_000_000_000;
  Date.now = () => F;
  fakeLiveFetch.calls = [];
  try {
    const tl = { ...TL, session: { ...TL.session, t0_iso: new Date(F - 5000).toISOString() } };
    const { h, p } = await open(
      'r1 --follow',
      makeCtx({
        routes: { '/links': LINKS, '/v1/replay/sessions/run%3Ar1/timeline': tl },
        fetchFn: fakeLiveFetch,
      }),
    );
    assert.equal(fakeLiveFetch.calls.length, 1);
    assert.equal(fakeLiveFetch.calls[0].url, '/v1/replay/live?session=run%3Ar1');
    // a new segment for d1 must enter the model (the inspector shows its station)
    fakeLiveFetch.calls[0].sse.push('segment', { doc_id: 'd1', node: 'gate', station: 'gate', t0: 1, t1: 2, attempt: 1, status: 'ok' });
    await nextTick();
    await nextTick();
    h.timers.tick();
    assert.match(lastText(h.view), /00:03\.0/); // pinned to now-2s
    h.tk.opts.onKey(key('j'));
    h.tk.opts.onKey(key('i'));
    assert.match(lastText(h.view), /station gate/);
    // A later segment grows the run: the clock total follows the fresh model duration.
    fakeLiveFetch.calls[0].sse.push('segment', { doc_id: 'd2', node: 'gate', station: 'gate', t0: 100, t1: 130, attempt: 1, status: 'ok' });
    await nextTick();
    await nextTick();
    h.timers.tick();
    assert.match(lastText(h.view), /00:03\.0 \/ 02:10\.0/, 'header total grows to 130s');
    // f toggles follow off (aborts the reader) and back on (a fresh reader)
    h.tk.opts.onKey(key('f'));
    assert.ok(fakeLiveFetch.calls[0].aborted, 'f off aborts the reader');
    h.tk.opts.onKey(key('f'));
    await nextTick();
    assert.equal(fakeLiveFetch.calls.length, 2);
    h.tk.opts.onKey(key('q'));
    await p;
  } finally {
    Date.now = realNow;
  }
});

test('--follow does not duplicate items the stream replays from the loaded timeline', async () => {
  fakeLiveFetch.calls = [];
  const { h, p } = await open(
    'r1 --follow',
    makeCtx({
      routes: { '/links': LINKS, '/v1/replay/sessions/run%3Ar1/timeline': TL },
      fetchFn: fakeLiveFetch,
    }),
  );
  const sse = fakeLiveFetch.calls[0].sse;
  const settle = async () => {
    await nextTick();
    await nextTick();
  };
  const ev = { t: 30, doc_id: 'd1', kind: 'gate', station: 'intake', payload: {} };
  // A replayed loaded segment and event are skipped: an accepted item forces a redraw, a skipped one does not.
  let n = h.view.frames.length;
  sse.push('segment', TL.segments[0]);
  sse.push('event', ev);
  await settle();
  assert.equal(h.view.frames.length, n, 'replayed loaded items cause no redraw');
  // A genuinely new item is accepted.
  sse.push('event', { ...ev, t: 31 });
  await settle();
  assert.equal(h.view.frames.length, n + 1, 'a new event redraws once');
  // A second identical event on the same connection is distinct (count-based), not collapsed.
  n = h.view.frames.length;
  sse.push('event', { ...ev, t: 31 });
  await settle();
  assert.equal(h.view.frames.length, n + 1, 'a repeated identical event is still accepted');
  h.tk.opts.onKey(key('q'));
  await p;
});

test('an error frame stops following instead of reconnecting', async () => {
  fakeLiveFetch.calls = [];
  const { h, p } = await open(
    'r1 --follow',
    makeCtx({
      routes: { '/links': LINKS, '/v1/replay/sessions/run%3Ar1/timeline': TL },
      fetchFn: fakeLiveFetch,
    }),
  );
  fakeLiveFetch.calls[0].sse.push('error', { detail: 'no timeline' });
  await nextTick();
  await nextTick();
  assert.ok(fakeLiveFetch.calls[0].aborted, 'the reader is stopped');
  assert.equal(fakeLiveFetch.calls.length, 1, 'no reconnect');
  h.tk.opts.onKey(key('q'));
  await p;
});

test('boundLiveTimeline caps each list at 5000 and drops items older than 2h', () => {
  assert.equal(LIVE_MAX_ITEMS, 5000);
  const seq = (n, make) => Array.from({ length: n }, (_, i) => make(i));
  const stale = { doc_id: 'd1', station: 'intake', t0: -100000, t1: -99999 };
  const tl = {
    session: { id: 'run:r1' },
    stations: [{ id: 'intake' }],
    entities: [{ doc_id: 'd1' }],
    rollups: { first_pass_rate: 0.5 },
    segments: [stale, ...seq(5001, (i) => ({ doc_id: 'd1', station: 'intake', t0: i, t1: i + 1 }))],
    generations: seq(5001, (i) => ({ doc_id: 'd1', t0: i, t1: i + 1 })),
    events: seq(5001, (i) => ({ t: i, doc_id: 'd1', kind: 'k' })),
    scores: seq(5001, (i) => ({ t: i, doc_id: 'd1' })),
  };
  const out = boundLiveTimeline(tl);
  assert.equal(out, tl, 'bounded in place');
  for (const list of ['segments', 'generations', 'events', 'scores']) {
    assert.equal(out[list].length, 5000, list);
  }
  // newest item is t0=5000, so the 2h floor is -2200 and the stale segment is gone.
  assert.ok(!out.segments.includes(stale));
  assert.equal(out.segments[0].t0, 1);
  assert.equal(out.segments.at(-1).t0, 5000);
  assert.equal(out.events.at(-1).t, 5000);
  // session/stations/entities/rollups are never trimmed.
  assert.equal(out.session, tl.session);
  assert.equal(out.stations, tl.stations);
  assert.equal(out.entities, tl.entities);
  assert.equal(out.rollups, tl.rollups);
  // The model still builds from the bounded buffer.
  assert.equal(createModel(out).duration, 5001);
});

test('a backward scrub leaves follow and f re-enters', async () => {
  fakeLiveFetch.calls = [];
  const { h, p } = await open(
    'r1 --follow',
    makeCtx({
      routes: { '/links': LINKS, '/v1/replay/sessions/run%3Ar1/timeline': TL },
      fetchFn: fakeLiveFetch,
    }),
  );
  assert.equal(fakeLiveFetch.calls.length, 1);
  h.tk.opts.onKey(key('ArrowLeft'));
  assert.ok(fakeLiveFetch.calls[0].aborted, 'a backward step stops the reader');
  h.tk.opts.onKey(key('f'));
  await nextTick();
  assert.equal(fakeLiveFetch.calls.length, 2);
  h.tk.opts.onKey(key('q'));
  await p;
});
