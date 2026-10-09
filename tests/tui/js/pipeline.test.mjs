import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRegistry, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { ApiError } from '../../../src/mailroom_reloaded/api/tui/api.js';
import { registerPipeline } from '../../../src/mailroom_reloaded/api/tui/commands/pipeline.js';

const HOSTILE = '<img src=x onerror=alert(1)>.txt';

function makeCtx(routes = {}, extra = {}) {
  const calls = [];
  const out = [];
  const controller = new AbortController();
  const handler = (method) => async (path, arg) => {
    calls.push({ method, path, arg });
    const r = routes[`${method} ${path}`];
    if (r === undefined) throw new Error(`unrouted ${method} ${path}`);
    if (typeof r === 'function') return r(arg, calls.length);
    if (r instanceof Error) throw r;
    return r;
  };
  const tokens = [];
  const ctx = {
    out: {
      line: (t, c) => out.push({ k: 'line', t, c }),
      pre: (t) => out.push({ k: 'pre', t }),
      table: (h, rows, o) => out.push({ k: 'table', h, rows, o }),
      kv: (pairs) => out.push({ k: 'kv', pairs }),
      listing: () => {},
      divider: () => out.push({ k: 'divider' }),
      man: () => {},
      clear: () => {},
    },
    api: {
      get: handler('GET'),
      post: handler('POST'),
      upload: async (f) => {
        calls.push({ method: 'UPLOAD', file: f });
        return routes.UPLOAD;
      },
      setToken: (t) => tokens.push(['set', t]),
      clearToken: () => tokens.push(['clear']),
    },
    signal: () => controller.signal,
    ...extra,
  };
  return { ctx, calls, out, tokens, controller };
}

function setup() {
  const registry = createRegistry();
  registerPipeline(registry);
  return registry;
}

const lines = (out) => out.filter((o) => o.k === 'line').map((o) => o.t);

test('registers every pipeline command with a man page', () => {
  const r = setup();
  for (const n of ['ls', 'inspect', 'audit', 'review', 'resolve', 'runs', 'cards', 'health', 'upload', 'watch', 'auth', 'jev']) {
    const spec = r.get(n);
    assert.ok(spec, n);
    assert.match(spec.man, /^NAME\n[\s\S]*SYNOPSIS[\s\S]*DESCRIPTION/);
  }
});

test('ls renders rows from the documents envelope and passes flags as query', async () => {
  const { ctx, calls, out } = makeCtx({
    'GET /v1/documents': {
      documents: [
        { doc_id: 'aaa', filename: 'a.txt', doc_type: 'invoice', status: 'archived' },
        { doc_id: 'bbb', filename: 'b.txt', doc_type: null, status: 'parked' },
      ],
      count: 2,
    },
  });
  await dispatch(setup(), ctx, 'ls --status parked --limit 5');
  assert.deepEqual(calls[0].arg, { limit: 5, status: 'parked' });
  const t = out.find((o) => o.k === 'table');
  assert.deepEqual(t.h, ['doc_id', 'filename', 'type', 'status']);
  assert.deepEqual(t.rows[1], ['bbb', 'b.txt', '—', 'parked']);
  assert.equal(t.o.cellClass(0, 3), 'success');
  assert.equal(t.o.cellClass(1, 3), 'warn');
});

test('ls keeps a hostile filename as plain text', async () => {
  const long = 'x'.repeat(10000);
  const { ctx, out } = makeCtx({
    'GET /v1/documents': {
      documents: [
        { doc_id: 'h1', filename: HOSTILE, status: 'failed' },
        { doc_id: 'h2', filename: long, status: 'processing' },
      ],
    },
  });
  await dispatch(setup(), ctx, 'ls');
  const t = out.find((o) => o.k === 'table');
  assert.equal(t.rows[0][1], HOSTILE);
  assert.equal(t.rows[1][1].length, 10000);
});

test('ls empty prints the exact empty-state line', async () => {
  const { ctx, out } = makeCtx({ 'GET /v1/documents': { documents: [], count: 0 } });
  await dispatch(setup(), ctx, 'ls');
  assert.deepEqual(lines(out), ["no documents — drop a file in the inbox or run 'upload'"]);
  assert.equal(out.some((o) => o.k === 'table'), false);
});

test('ls rejects a bad --limit without a request', async () => {
  const { ctx, calls, out } = makeCtx();
  await dispatch(setup(), ctx, 'ls --limit 0');
  assert.equal(calls.length, 0);
  assert.match(lines(out)[0], /^ls: --limit/);
});

test('review asks for parked documents', async () => {
  const { ctx, calls } = makeCtx({ 'GET /v1/documents': { documents: [] } });
  await dispatch(setup(), ctx, 'review');
  assert.deepEqual(calls[0].arg, { status: 'parked' });
});

test('inspect prints title, kv and stage lines in order', async () => {
  const { ctx, out, calls } = makeCtx({
    'GET /v1/documents/abc': {
      doc_id: 'abc',
      status: 'archived',
      manifest: { filename: 'inv.txt' },
      report: {
        classification: { doc_type: 'invoice', confidence: 0.93 },
        route_trail: ['gate_classify', 'gate_extract', 'report_catalog_archive'],
      },
      catalog: { filename: 'inv.txt', doc_type: 'invoice' },
    },
  });
  await dispatch(setup(), ctx, 'inspect abc');
  assert.equal(calls[0].path, '/v1/documents/abc');
  assert.deepEqual(out[0], { k: 'line', t: 'inv.txt', c: 'amber' });
  const kv = out.find((o) => o.k === 'kv');
  assert.deepEqual(kv.pairs, [
    ['doc_id', 'abc'],
    ['status', 'archived'],
    ['doc_type', 'invoice'],
    ['confidence', '0.93'],
  ]);
  const stages = out.filter((o) => o.c === 'info').map((o) => o.t);
  assert.deepEqual(stages, ['▸ gate_classify', '▸ gate_extract', '▸ report_catalog_archive']);
});

test('inspect copes with a document that has no report yet', async () => {
  const { ctx, out } = makeCtx({
    'GET /v1/documents/n1': { doc_id: 'n1', status: 'processing', manifest: null, report: null, catalog: null },
  });
  await dispatch(setup(), ctx, 'inspect n1');
  assert.ok(out.find((o) => o.k === 'kv'));
  assert.ok(lines(out).includes('no route trail yet'));
});

test('audit prints count and chain ok', async () => {
  const { ctx, out } = makeCtx({
    'GET /v1/audit/abc': { doc_id: 'abc', entries: [{}, {}, {}], chain: { ok: true, broken_at: null } },
  });
  await dispatch(setup(), ctx, 'audit abc');
  assert.deepEqual(out.map((o) => [o.t, o.c]), [
    ['3 audit entries', undefined],
    ['chain: ok', 'success'],
  ]);
});

test('audit prints the broken seq', async () => {
  const { ctx, out } = makeCtx({
    'GET /v1/audit/abc': { entries: [{}, {}], chain: { ok: false, broken_at: 2 } },
  });
  await dispatch(setup(), ctx, 'audit abc');
  assert.deepEqual(out[1], { k: 'line', t: 'chain: broken at 2', c: 'error' });
});

test('resolve correct without --type makes no request', async () => {
  const { ctx, calls, out } = makeCtx();
  await dispatch(setup(), ctx, 'resolve abc correct');
  assert.equal(calls.length, 0);
  assert.deepEqual(lines(out), ['resolve: correct needs --type <doc_type>']);
});

test('resolve posts the real body shape and echoes status and trail', async () => {
  const { ctx, calls, out } = makeCtx({
    'POST /v1/review/abc/resolve': {
      doc_id: 'abc',
      action: 'correct',
      status: 'archived',
      doc_type: 'receipt',
      route_trail: ['human_review', 'report_catalog_archive'],
    },
  });
  await dispatch(setup(), ctx, 'resolve abc correct --type receipt --subclass retail --reviewer jo');
  assert.deepEqual(calls[0].arg, {
    action: 'correct',
    doc_type: 'receipt',
    doc_subclass: 'retail',
    reviewer: 'jo',
  });
  assert.deepEqual(lines(out), [
    'abc · correct — archived',
    'doc_type: receipt',
    '▸ human_review',
    '▸ report_catalog_archive',
  ]);
});

test('resolve rejects an unknown action locally', async () => {
  const { ctx, calls } = makeCtx();
  await dispatch(setup(), ctx, 'resolve abc nuke');
  assert.equal(calls.length, 0);
});

test('error messages are exact: offline, 401, 404', async () => {
  const reg = setup();
  let m = makeCtx({ 'GET /v1/documents': new ApiError('no api connection', { kind: 'offline' }) });
  await dispatch(reg, m.ctx, 'ls');
  assert.deepEqual(lines(m.out), ['ls: api unreachable — mailroom closed']);

  m = makeCtx({ 'GET /v1/documents': new ApiError('api token required', { status: 401, kind: 'unauthorized' }) });
  await dispatch(reg, m.ctx, 'ls');
  assert.deepEqual(lines(m.out), ["ls: 401 — type 'auth <token>'"]);

  m = makeCtx({ 'GET /v1/documents/zzz': new ApiError('Unknown document: zzz', { status: 404, kind: 'http' }) });
  await dispatch(reg, m.ctx, 'inspect zzz');
  assert.deepEqual(lines(m.out), ['inspect: no such document zzz']);

  m = makeCtx({ 'GET /v1/audit/zzz': new ApiError('x', { status: 404, kind: 'http' }) });
  await dispatch(reg, m.ctx, 'audit zzz');
  assert.deepEqual(lines(m.out), ['audit: no such document zzz']);

  m = makeCtx({ 'POST /v1/review/zzz/resolve': new ApiError('x', { status: 404, kind: 'http' }) });
  await dispatch(reg, m.ctx, 'resolve zzz approve');
  assert.deepEqual(lines(m.out), ['resolve: no such document zzz']);
});

test('other http errors show the server detail', async () => {
  const { ctx, out } = makeCtx({
    'GET /v1/runs/nope/cards': new ApiError('Invalid run ID', { status: 400, kind: 'http' }),
  });
  await dispatch(setup(), ctx, 'cards nope');
  assert.deepEqual(lines(out), ['cards: Invalid run ID']);
});

test('runs and cards render the real shapes', async () => {
  const { ctx, out } = makeCtx({
    'GET /v1/runs': { runs: [{ run_id: 'aabbccddeeff', documents: 4 }] },
    'GET /v1/runs/aabbccddeeff/cards': {
      run_id: 'aabbccddeeff',
      cards: [{ provider: 'mock', roles: { sorter: 1 } }, { provider: HOSTILE }],
    },
  });
  const reg = setup();
  await dispatch(reg, ctx, 'runs');
  assert.deepEqual(out[0].rows, [['aabbccddeeff', 4]]);
  await dispatch(reg, ctx, 'cards aabbccddeeff');
  const kvs = out.filter((o) => o.k === 'kv');
  assert.equal(kvs.length, 2);
  assert.deepEqual(kvs[0].pairs[1], ['roles', '{"sorter":1}']);
  assert.deepEqual(kvs[1].pairs[0], ['provider', HOSTILE]);
  assert.ok(out.some((o) => o.k === 'divider'));
});

test('runs empty is a plain message', async () => {
  const { ctx, out } = makeCtx({ 'GET /v1/runs': { runs: [] } });
  await dispatch(setup(), ctx, 'runs');
  assert.deepEqual(lines(out), ['no eval runs yet']);
});

test('health prints service and status', async () => {
  const { ctx, out } = makeCtx({ 'GET /health': { status: 'ok', service: 'mailroom' } });
  await dispatch(setup(), ctx, 'health');
  assert.deepEqual(lines(out), ['mailroom — ok']);
});

test('upload uses the injected picker and prints queued line', async () => {
  const file = { name: HOSTILE };
  let accept;
  const { ctx, out, calls } = makeCtx(
    { UPLOAD: { doc_id: 'd1', file: HOSTILE, status: 'accepted' } },
    { pickFile: async (a) => ((accept = a), file) },
  );
  await dispatch(setup(), ctx, 'upload');
  assert.equal(accept, '.txt,.md,.text,.pdf,.docx,.png,.jpg,.jpeg');
  assert.equal(calls[0].file, file);
  assert.deepEqual(lines(out), [`queued ${HOSTILE} · d1`]);
});

test('upload with a cancelled picker does not call the api', async () => {
  const { ctx, calls, out } = makeCtx({}, { pickFile: async () => null });
  await dispatch(setup(), ctx, 'upload');
  assert.equal(calls.length, 0);
  assert.deepEqual(lines(out), ['upload: no file chosen']);
});

test('upload falls back to a DOM file input', async () => {
  const made = [];
  const prev = globalThis.document;
  globalThis.document = {
    createElement(tag) {
      const listeners = {};
      const n = {
        tag,
        addEventListener: (e, f) => (listeners[e] = f),
        click() {
          this.files = [{ name: 'f.txt' }];
          listeners.change();
        },
      };
      made.push(n);
      return n;
    },
  };
  try {
    const { ctx, out } = makeCtx({ UPLOAD: { doc_id: 'd2', file: 'f.txt' } });
    await dispatch(setup(), ctx, 'upload');
    assert.equal(made[0].type, 'file');
    assert.equal(made[0].accept, '.txt,.md,.text,.pdf,.docx,.png,.jpg,.jpeg');
    assert.deepEqual(lines(out), ['queued f.txt · d2']);
  } finally {
    globalThis.document = prev;
  }
});

test('upload offline and 401 messages', async () => {
  const file = { name: 'a.txt' };
  const reg = setup();
  const m = makeCtx({}, { pickFile: async () => file });
  m.ctx.api.upload = async () => {
    throw new ApiError('x', { kind: 'offline' });
  };
  await dispatch(reg, m.ctx, 'upload');
  assert.deepEqual(lines(m.out), ['upload: api unreachable — mailroom closed']);
});

test('watch baselines, prints only changes, and stops on abort', async () => {
  const polls = [
    [{ doc_id: 'a', filename: 'a.txt', status: 'processing' }],
    [{ doc_id: 'a', filename: 'a.txt', status: 'processing' }], // no change -> silent
    [
      { doc_id: 'a', filename: 'a.txt', status: 'archived' },
      { doc_id: 'b', filename: HOSTILE, status: 'new' },
    ],
  ];
  let n = 0;
  const sleeps = [];
  const m = makeCtx({ 'GET /v1/documents': () => ({ documents: polls[Math.min(n++, polls.length - 1)] }) });
  m.ctx.sleep = async (ms) => {
    sleeps.push(ms);
    if (sleeps.length === 3) m.controller.abort();
  };
  await dispatch(setup(), m.ctx, 'watch --interval 2');
  assert.deepEqual(sleeps, [2000, 2000, 2000]);
  assert.deepEqual(m.calls[0].arg, { limit: 500 });
  assert.deepEqual(lines(m.out), [
    'watching 1 document · every 2s · ctrl+c to stop',
    'a · a.txt · processing → archived',
    `b · ${HOSTILE} · new — new`,
    'watch: stopped',
  ]);
});

test('watch stops when the tab is hidden', async () => {
  const m = makeCtx({ 'GET /v1/documents': { documents: [] } }, { isHidden: () => true, sleep: async () => {} });
  await dispatch(setup(), m.ctx, 'watch');
  assert.equal(m.calls.length, 1);
  assert.equal(lines(m.out).at(-1), 'watch: stopped — tab hidden');
});

test('watch stops on an api error with the exact message', async () => {
  const m = makeCtx({ 'GET /v1/documents': new ApiError('x', { kind: 'offline' }) }, { sleep: async () => {} });
  await dispatch(setup(), m.ctx, 'watch');
  assert.deepEqual(lines(m.out), ['watch: api unreachable — mailroom closed']);
});

test('watch rejects a bad interval', async () => {
  const m = makeCtx();
  await dispatch(setup(), m.ctx, 'watch --interval 0');
  assert.equal(m.calls.length, 0);
  assert.match(lines(m.out)[0], /^watch: --interval/);
});

test('auth ok, rejected, clear, and never echoes the token', async () => {
  const reg = setup();
  let m = makeCtx({ 'GET /v1/documents': { documents: [] } });
  await dispatch(reg, m.ctx, 'auth s3cret');
  assert.deepEqual(m.tokens, [['set', 's3cret']]);
  assert.deepEqual(m.calls[0].arg, { limit: 1 });
  assert.deepEqual(lines(m.out), ['auth: ok']);

  m = makeCtx({ 'GET /v1/documents': new ApiError('x', { status: 401, kind: 'unauthorized' }) });
  await dispatch(reg, m.ctx, 'auth bad');
  assert.deepEqual(lines(m.out), ['auth: token rejected — 401']);
  assert.ok(!JSON.stringify(m.out).includes('bad'));

  m = makeCtx();
  await dispatch(reg, m.ctx, 'auth --clear');
  assert.deepEqual(m.tokens, [['clear']]);
  assert.equal(m.calls.length, 0);
});

test('statusClass ignores prototype keys', async () => {
  const { statusClass } = await import('../../../src/mailroom_reloaded/api/tui/commands/pipeline.js');
  assert.equal(statusClass('archived'), 'success');
  assert.equal(statusClass('constructor'), '');
  assert.equal(statusClass('__proto__'), '');
  assert.equal(statusClass('toString'), '');
});

test('audit with no entries warns and never prints chain: ok', async () => {
  const m = makeCtx({ 'GET /v1/audit/abc': { entries: [], chain: { ok: true } } });
  await dispatch(setup(), m.ctx, 'audit abc');
  const l = lines(m.out);
  assert.ok(l.includes('no audit entries (unknown document?)'));
  assert.ok(!l.includes('chain: ok'));
});

test('empty, dot and dotdot ids are rejected before any request', async () => {
  for (const cmd of ['inspect .', 'inspect ..', 'inspect a/b', 'audit ..', 'resolve . approve', 'cards .', 'cards ..', 'cards ""']) {
    const m = makeCtx({});
    await dispatch(setup(), m.ctx, cmd);
    assert.equal(m.calls.length, 0, cmd);
  }
});

test('pipeline commands pass ctx.signal() to the api', async () => {
  const seen = [];
  const m = makeCtx({});
  m.ctx.api.get = async (p, q, o) => {
    seen.push(o && o.signal);
    return { documents: [], runs: [] };
  };
  await dispatch(setup(), m.ctx, 'ls');
  await dispatch(setup(), m.ctx, 'runs');
  assert.equal(seen.length, 2);
  for (const s of seen) assert.equal(s, m.controller.signal);
});

test('auth rolls back the token on 401 and rejects bad tokens', async () => {
  const m = makeCtx({ 'GET /v1/documents': new ApiError('x', { status: 401, kind: 'unauthorized' }) });
  await dispatch(setup(), m.ctx, 'auth abc');
  assert.deepEqual(m.tokens, [['set', 'abc'], ['clear']]);
  const m2 = makeCtx({});
  m2.ctx.api.setToken = () => {
    throw new Error('bad');
  };
  await dispatch(setup(), m2.ctx, 'auth "a b"');
  assert.equal(m2.calls.length, 0);
});

test('ls says showing first N when the page is full', async () => {
  const docs = [1, 2].map((i) => ({ doc_id: `d${i}`, filename: 'f', status: 'new' }));
  const m = makeCtx({ 'GET /v1/documents': { documents: docs } });
  await dispatch(setup(), m.ctx, 'ls --limit 2');
  assert.ok(lines(m.out).some((l) => l.startsWith('showing first 2')));
});

test('watch warns once when the page is full and does not print after abort', async () => {
  const full = Array.from({ length: 500 }, (_, i) => ({ doc_id: `d${i}`, filename: 'f', status: 'new' }));
  const m = makeCtx({ 'GET /v1/documents': () => ({ documents: full }) });
  m.ctx.sleep = async () => m.controller.abort();
  await dispatch(setup(), m.ctx, 'watch');
  assert.ok(lines(m.out).some((l) => l.startsWith('watch: page full')));
  // abort during the poll: nothing from that poll is printed
  const m2 = makeCtx({});
  m2.ctx.api.get = async () => {
    m2.controller.abort();
    return { documents: full };
  };
  await dispatch(setup(), m2.ctx, 'watch');
  assert.deepEqual(lines(m2.out), ['watch: stopped']);
});

test('upload picker receives the signal and abort cancels the upload', async () => {
  const m = makeCtx({});
  let pickSignal;
  m.ctx.pickFile = (accept, signal) => {
    pickSignal = signal;
    return new Promise((resolve) => signal.addEventListener('abort', () => resolve(null)));
  };
  const p = dispatch(setup(), m.ctx, 'upload');
  m.controller.abort();
  await p;
  assert.equal(pickSignal, m.controller.signal);
  assert.ok(!lines(m.out).includes('upload: no file chosen'));
});

// ---- jev ----
const JEV_ON = {
  enabled: true, provider: 'local', model: 'jevk5', base_url: 'http://x/v1', api_key: 'sk-SECRET',
  calibrated: true,
  calibration: { temperature: 0.82, accept_threshold: 0.844, verify_threshold: 0.7326, ece_before: 0.153, ece_after: 0.152, n: 60 },
  gate: 'jev',
};

test('jev prints kv with calibration and never an api key', async () => {
  const { ctx, out } = makeCtx({ 'GET /v1/jev': JEV_ON });
  await dispatch(setup(), ctx, 'jev');
  const kv = out.find((o) => o.k === 'kv');
  const keys = kv.pairs.map((p) => p[0]);
  for (const k of ['provider', 'model', 'gate', 'calibrated', 'accept', 'verify']) assert.ok(keys.includes(k), k);
  assert.ok(!JSON.stringify(out).includes('SECRET'));
});

test('jev off prints the dim band-gate line', async () => {
  const { ctx, out } = makeCtx({ 'GET /v1/jev': { enabled: false, gate: 'band' } });
  await dispatch(setup(), ctx, 'jev');
  assert.deepEqual(lines(out), ['jev off (band gate)']);
  assert.equal(out[0].c, 'dim');
});

test('jev uncalibrated skips thresholds; 401 handled', async () => {
  let m = makeCtx({ 'GET /v1/jev': { enabled: true, provider: 'p', model: 'm', calibrated: false, gate: 'band' } });
  await dispatch(setup(), m.ctx, 'jev');
  assert.ok(!m.out.find((o) => o.k === 'kv').pairs.some((p) => p[0] === 'accept'));
  m = makeCtx({ 'GET /v1/jev': new ApiError('x', { status: 401, kind: 'unauthorized' }) });
  await dispatch(setup(), m.ctx, 'jev');
  assert.match(lines(m.out)[0], /401/);
});

test('audit renders gate_decision entries distinctly and survives odd actions', async () => {
  const gd = (node, payload) => ({ node, event: 'gate_decision', payload });
  const { ctx, out } = makeCtx({
    'GET /v1/audit/abc': {
      entries: [
        { node: 'ingest', event: 'completed', payload: {} },
        gd('gate_classify', { action: 'verify', source: 'jev', confidence: 0.88, reason: 'band' }),
        gd('gate_extract', { action: 'weird', source: 'band' }),
        gd('gate_extract', null),
      ],
      chain: { ok: true },
    },
  });
  await dispatch(setup(), ctx, 'audit abc');
  const l = out.filter((o) => o.k === 'line');
  const g = l.find((o) => o.t.startsWith('gate classify -> verify [jev] conf 0.88'));
  assert.ok(g && g.t.endsWith('— band'));
  assert.equal(g.c, 'warn');
  assert.ok(l.some((o) => o.t.startsWith('gate extract -> weird [band]')));
  assert.ok(l.some((o) => o.t.startsWith('gate extract -> —')));
});

test('inspect shows a gate line from audit, non-fatal on failure', async () => {
  const doc = { doc_id: 'abc', status: 'parked', report: {}, catalog: {}, manifest: {} };
  let m = makeCtx({
    'GET /v1/documents/abc': doc,
    'GET /v1/audit/abc': { entries: [
      { node: 'gate_classify', event: 'gate_decision', payload: { action: 'proceed', source: 'band', confidence: 0.9 } },
      { node: 'gate_extract', event: 'gate_decision', payload: { action: 'verify', source: 'jev', confidence: 0.7 } },
    ] },
  });
  await dispatch(setup(), m.ctx, 'inspect abc');
  const pairs = m.out.find((o) => o.k === 'kv').pairs;
  assert.ok(pairs.some((p) => p[0] === 'gate' && p[1].includes('verify') && p[1].includes('jev')));
  m = makeCtx({ 'GET /v1/documents/abc': doc, 'GET /v1/audit/abc': new ApiError('x', { status: 500, kind: 'http' }) });
  await dispatch(setup(), m.ctx, 'inspect abc');
  assert.ok(m.out.find((o) => o.k === 'kv'));
  assert.ok(!lines(m.out).some((t) => t.startsWith('inspect:')));
});

test('runs pin and unpin post a validated run id', async () => {
  for (const [sub, word] of [['pin', 'pinned'], ['unpin', 'unpinned']]) {
    const m = makeCtx({ [`POST /v1/ledger/${sub}`]: { ok: true } });
    await dispatch(setup(), m.ctx, `runs ${sub} run-1.a`);
    assert.deepEqual(m.calls, [{ method: 'POST', path: `/v1/ledger/${sub}`, arg: { run_id: 'run-1.a' } }]);
    assert.deepEqual(m.out, [{ k: 'line', t: `${word} run-1.a`, c: 'success' }]);
  }
});

test('runs pin rejects bad ids and bad arity before any request', async () => {
  for (const cmd of ['runs pin', 'runs pin ..', 'runs unpin a/b', 'runs pin a b', 'runs bogus', 'runs keep set', 'runs keep set recent:0', 'runs keep set recent:1001', 'runs keep set none', 'runs keep set all x', 'runs keep zap']) {
    const m = makeCtx({});
    await dispatch(setup(), m.ctx, cmd);
    assert.equal(m.calls.length, 0, cmd);
    assert.equal(lines(m.out)[0] !== undefined, true, cmd);
  }
});

test('runs pin surfaces the server 400 detail, offline and 401 wording; abort is silent', async () => {
  const cases = [
    [new ApiError('Showcase runs cannot be unpinned', { status: 400, kind: 'http' }), 'runs: Showcase runs cannot be unpinned'],
    [new ApiError('x', { kind: 'offline' }), 'runs: api unreachable — mailroom closed'],
    [new ApiError('x', { status: 401, kind: 'unauthorized' }), "runs: 401 — type 'auth <token>'"],
  ];
  for (const [err, want] of cases) {
    const m = makeCtx({ 'POST /v1/ledger/unpin': err });
    await dispatch(setup(), m.ctx, 'runs unpin showcase1');
    assert.deepEqual(m.out, [{ k: 'line', t: want, c: 'error' }]);
  }
  const m = makeCtx({ 'POST /v1/ledger/pin': new ApiError('aborted', { kind: 'aborted' }) });
  await dispatch(setup(), m.ctx, 'runs pin r1');
  assert.equal(m.out.length, 0);
});

test('runs keep shows policy, source, pinned and showcase as plain text', async () => {
  const m = makeCtx({
    'GET /v1/ledger/keep': { policy: 'recent:5', source: 'ledger', pinned: ['a1', '<img onerror=x>'], showcase: [] },
  });
  await dispatch(setup(), m.ctx, 'runs keep');
  assert.deepEqual(m.out[0].pairs, [
    ['policy', 'recent:5'],
    ['source', 'ledger'],
    ['pinned', 'a1, <img onerror=x>'],
    ['showcase', '—'],
  ]);
  const e = makeCtx({ 'GET /v1/ledger/keep': new ApiError('x', { kind: 'offline' }) });
  await dispatch(setup(), e.ctx, 'runs keep');
  assert.equal(lines(e.out)[0], 'runs: api unreachable — mailroom closed');
});

test('runs keep set posts a validated policy and shows server errors', async () => {
  for (const v of ['pinned', 'all', 'recent:1', 'recent:999', 'recent:1000']) {
    const m = makeCtx({ 'POST /v1/ledger/policy': { ok: true } });
    await dispatch(setup(), m.ctx, `runs keep set ${v}`);
    assert.deepEqual(m.calls, [{ method: 'POST', path: '/v1/ledger/policy', arg: { value: v } }]);
    assert.deepEqual(m.out, [{ k: 'line', t: `keep policy ${v}`, c: 'success' }]);
  }
  const m = makeCtx({ 'POST /v1/ledger/policy': new ApiError('Invalid policy', { status: 400, kind: 'http' }) });
  await dispatch(setup(), m.ctx, 'runs keep set all');
  assert.deepEqual(lines(m.out), ['runs: Invalid policy']);
});

test('bare runs does not touch the ledger endpoints', async () => {
  const m = makeCtx({ 'GET /v1/runs': { runs: [{ run_id: 'r1', documents: 2 }] } });
  await dispatch(setup(), m.ctx, 'runs');
  assert.deepEqual(m.calls.map((c) => c.path), ['/v1/runs']);
  assert.deepEqual(m.out[0].rows, [['r1', 2]]);
});
