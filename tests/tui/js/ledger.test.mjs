import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRegistry, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { ApiError } from '../../../src/mailroom_reloaded/api/tui/api.js';
import {
  registerLedger,
  ledgerRow,
  verifyLine,
} from '../../../src/mailroom_reloaded/api/tui/commands/ledger.js';

const HOSTILE = '<img src=x onerror=alert(1)>';
const H = 'abcdef0123456789abcdef0123456789';

function makeCtx(routes = {}) {
  const calls = [];
  const out = [];
  const controller = new AbortController();
  const handler = (method) => async (path, arg) => {
    calls.push({ method, path, arg });
    const r = routes[`${method} ${path}`];
    if (r === undefined) throw new Error(`unrouted ${method} ${path}`);
    if (r instanceof Error) throw r;
    return r;
  };
  const ctx = {
    out: {
      line: (t, c) => out.push({ k: 'line', t, c }),
      table: (h, rows, o) => out.push({ k: 'table', h, rows, o }),
      kv: (pairs) => out.push({ k: 'kv', pairs }),
      man: () => {},
    },
    api: { get: handler('GET'), post: handler('POST') },
    signal: () => controller.signal,
  };
  return { ctx, calls, out, controller };
}

function setup() {
  const r = createRegistry();
  registerLedger(r);
  return r;
}

const lines = (out) => out.filter((o) => o.k === 'line');
const entry = (over = {}) => ({
  seq: 7,
  kind: 'doc_closed',
  run_id: 'run-1',
  doc_id: 'd1',
  ts: '2026-05-01T12:34:56.789+00:00',
  payload: { secret: 'never-printed' },
  digest: 'x',
  prev_hash: 'y',
  entry_hash: H,
  ...over,
});

test('ledger is registered with a man page', () => {
  const spec = setup().get('ledger');
  assert.ok(spec);
  assert.match(spec.man, /^NAME\n[\s\S]*SYNOPSIS[\s\S]*DESCRIPTION/);
});

test('ledgerRow shortens ts and hash and omits the payload', () => {
  assert.deepEqual(ledgerRow(entry()), ['7', 'doc_closed', 'run-1', 'd1', '2026-05-01 12:34:56', H.slice(0, 12)]);
  assert.deepEqual(ledgerRow(null), ['—', '—', '—', '—', '—', '—']);
});

test('ledger renders a table with query defaults', async () => {
  const m = makeCtx({ 'GET /v1/ledger': { entries: [entry(), entry({ seq: 6, doc_id: null })], head: null } });
  await dispatch(setup(), m.ctx, 'ledger');
  assert.deepEqual(m.calls[0].arg, { limit: 50 });
  const t = m.out.find((o) => o.k === 'table');
  assert.deepEqual(t.h, ['seq', 'kind', 'run', 'doc', 'ts', 'hash']);
  assert.equal(t.rows.length, 2);
  assert.equal(t.rows[1][3], '—');
  assert.ok(!JSON.stringify(m.out).includes('never-printed'));
});

test('ledger passes --run, --kind and --limit', async () => {
  const m = makeCtx({ 'GET /v1/ledger': { entries: [], head: null } });
  await dispatch(setup(), m.ctx, 'ledger --run run-1 --kind doc_closed --limit 500');
  assert.deepEqual(m.calls[0].arg, { limit: 500, run_id: 'run-1', kind: 'doc_closed' });
  assert.deepEqual(lines(m.out).map((l) => l.t), ['no ledger entries']);
});

test('ledger rejects bad limit, run and kind before any request', async () => {
  for (const cmd of [
    'ledger --limit 0',
    'ledger --limit 501',
    'ledger --limit abc',
    'ledger --run ..',
    'ledger --run a/b',
    'ledger --run',
    'ledger --kind "a b"',
    'ledger --kind',
    'ledger head --run x',
    'ledger verify --run x',
    'ledger verify ..',
    'ledger verify a/b',
    'ledger verify a b',
    'ledger bogus',
  ]) {
    const m = makeCtx({});
    await dispatch(setup(), m.ctx, cmd);
    assert.equal(m.calls.length, 0, cmd);
    assert.equal(lines(m.out)[0].c, 'error', cmd);
  }
});

test('ledger head prints kv and handles an empty ledger', async () => {
  const m = makeCtx({
    'GET /v1/ledger/head': { head: { seq: 9, entry_hash: H, ts: '2026-05-01T00:00:00Z', kind: 'run_closed' }, count: 9 },
  });
  await dispatch(setup(), m.ctx, 'ledger head');
  assert.deepEqual(m.out[0].pairs, [
    ['seq', '9'],
    ['count', '9'],
    ['hash', H.slice(0, 12)],
    ['kind', 'run_closed'],
    ['ts', '2026-05-01 00:00:00'],
  ]);
  const e = makeCtx({ 'GET /v1/ledger/head': { head: null, count: 0 } });
  await dispatch(setup(), e.ctx, 'ledger head');
  assert.deepEqual(lines(e.out).map((l) => [l.t, l.c]), [['ledger is empty', 'dim']]);
});

test('verifyLine covers ok, broken and merkle states', () => {
  const ok = verifyLine({ ok: true, count: 12, head_seq: 12, head_hash: H, broken_at: null, merkle_ok: true, detail: '' });
  assert.deepEqual(ok[0], { t: `chain ok — 12 entries, head #12 ${H.slice(0, 12)}`, cls: 'success' });
  assert.deepEqual(ok[1], { t: 'merkle: ok', cls: 'success' });
  const nul = verifyLine({ ok: true, count: 1, head_seq: 1, head_hash: H, merkle_ok: null });
  assert.deepEqual(nul[1], { t: 'merkle: run not closed', cls: 'dim' });
  const bad = verifyLine({ ok: false, count: 5, broken_at: 3, merkle_ok: false, detail: 'hash mismatch' });
  assert.deepEqual(bad[0], { t: 'chain broken at seq 3 — hash mismatch', cls: 'error' });
  assert.equal(bad[1].cls, 'error');
  assert.ok(verifyLine(undefined).length > 0);
});

test('ledger verify sends run_id and prints the lines', async () => {
  const m = makeCtx({
    'GET /v1/ledger/verify': { ok: true, count: 3, head_seq: 3, head_hash: H, broken_at: null, merkle_ok: null, detail: '' },
  });
  await dispatch(setup(), m.ctx, 'ledger verify run-1');
  assert.deepEqual(m.calls[0].arg, { run_id: 'run-1' });
  assert.deepEqual(lines(m.out).map((l) => [l.t, l.c]), [
    [`chain ok — 3 entries, head #3 ${H.slice(0, 12)}`, 'success'],
    ['merkle: run not closed', 'dim'],
  ]);
  const g = makeCtx({ 'GET /v1/ledger/verify': { ok: true, count: 0, head_seq: null, head_hash: null, merkle_ok: null } });
  await dispatch(setup(), g.ctx, 'ledger verify');
  assert.deepEqual(g.calls[0].arg, {});
});

test('ledger verify broken is an error line', async () => {
  const m = makeCtx({
    'GET /v1/ledger/verify': { ok: false, count: 4, broken_at: 2, merkle_ok: false, detail: 'prev_hash mismatch' },
  });
  await dispatch(setup(), m.ctx, 'ledger verify');
  const l = lines(m.out);
  assert.equal(l[0].t, 'chain broken at seq 2 — prev_hash mismatch');
  assert.equal(l[0].c, 'error');
  assert.equal(l[1].c, 'error');
});

test('api failures use the shared wording and abort is silent', async () => {
  const cases = [
    [new ApiError('x', { kind: 'offline' }), 'ledger: api unreachable — mailroom closed'],
    [new ApiError('x', { status: 401, kind: 'unauthorized' }), "ledger: 401 — type 'auth <token>'"],
    [new ApiError('boom', { status: 500, kind: 'http' }), 'ledger: boom'],
  ];
  for (const cmd of ['ledger', 'ledger head', 'ledger verify']) {
    for (const [err, want] of cases) {
      const path = cmd === 'ledger' ? '/v1/ledger' : `/v1/ledger/${cmd.split(' ')[1]}`;
      const m = makeCtx({ [`GET ${path}`]: err });
      await dispatch(setup(), m.ctx, cmd);
      assert.deepEqual(lines(m.out).map((l) => l.t), [want], cmd);
    }
    const m = makeCtx({
      [`GET ${cmd === 'ledger' ? '/v1/ledger' : `/v1/ledger/${cmd.split(' ')[1]}`}`]: new ApiError('aborted', { kind: 'aborted' }),
    });
    await dispatch(setup(), m.ctx, cmd);
    assert.equal(m.out.length, 0, cmd);
  }
});

test('hostile response values stay plain text', async () => {
  const m = makeCtx({
    'GET /v1/ledger': { entries: [entry({ kind: HOSTILE, run_id: HOSTILE, doc_id: HOSTILE, ts: HOSTILE, entry_hash: HOSTILE })] },
    'GET /v1/ledger/verify': { ok: false, broken_at: HOSTILE, detail: HOSTILE },
  });
  await dispatch(setup(), m.ctx, 'ledger');
  const row = m.out.find((o) => o.k === 'table').rows[0];
  assert.equal(row[1], HOSTILE);
  assert.equal(row[2], HOSTILE);
  assert.ok(row[5].startsWith('<img src=x'));
  await dispatch(setup(), m.ctx, 'ledger verify');
  assert.equal(lines(m.out)[1].t, `chain broken at seq ${HOSTILE} — ${HOSTILE}`);
});

test('verifyLine says "verify failed" (not "broken") for an unknown run', () => {
  const lines = verifyLine({ ok: false, broken_at: null, detail: 'unknown run', merkle_ok: null });
  assert.deepEqual(lines, [{ t: 'verify failed: unknown run', cls: 'error' }]);
});
