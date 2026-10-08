import { test } from 'node:test';
import assert from 'node:assert/strict';
import { boot } from '../../../src/mailroom_reloaded/api/tui/boot.js';
import { ApiError } from '../../../src/mailroom_reloaded/api/tui/api.js';

const BANNER = 'BIG BANNER';
const BANNER_COMPACT = 'small';
const CLOSED = 'mailroom closed — no api connection';
const TOKEN = "[ !! ] api token required — type 'auth <token>'";
const HELP = "type 'help' to begin.";

// Fake ctx: records every printed line, status write and api call.
function fakeCtx({ health = { ok: true, status: 200 }, get = () => ({}) } = {}) {
  const printed = [];
  const statuses = {};
  const calls = [];
  const out = {
    banner: (text) => printed.push({ kind: 'banner', text }),
    line: (text, cls) => printed.push({ kind: 'line', text, cls }),
  };
  const api = {
    async health() {
      calls.push(['health']);
      return typeof health === 'function' ? health() : health;
    },
    async get(path, query) {
      calls.push(['get', path, query]);
      return get(path, query);
    },
  };
  const ctx = {
    out,
    api,
    setStatus(key, value) {
      statuses[key] = value;
    },
  };
  const texts = () => printed.filter((p) => p.kind === 'line').map((p) => p.text);
  return { ctx, printed, statuses, calls, texts };
}

// Injected sleep: records requested delays, never waits.
function fakeSleep() {
  const delays = [];
  const sleep = async (ms) => {
    delays.push(ms);
  };
  return { sleep, delays };
}

function healthyGet(path, query) {
  if (path === '/v1/documents' && query && query.limit === 1) {
    return { documents: [{ doc_id: 'a' }], count: 1 };
  }
  if (path === '/v1/documents' && query && query.limit === 500) {
    return { documents: [{ doc_id: 'a' }, { doc_id: 'b' }, { doc_id: 'c' }], count: 3 };
  }
  if (path === '/v1/runs') return { runs: [{ run_id: 'r1' }, { run_id: 'r2' }] };
  throw new Error(`unexpected get ${path}`);
}

test('healthy api prints real checks with counts and the motd, state live', async () => {
  const { ctx, texts, statuses } = fakeCtx({ get: healthyGet });
  const { sleep } = fakeSleep();
  const result = await boot(ctx, {
    reducedMotion: true,
    banner: BANNER,
    bannerCompact: BANNER_COMPACT,
    sleep,
  });
  assert.deepEqual(result, { state: 'live' });
  assert.deepEqual(texts(), [
    'mailroom@floor — mailroom-reloaded visual engine',
    'boot: tty · crt on · theme amber',
    '[ ok ] api /health',
    '[ ok ] auth',
    '[ ok ] catalog · 3 documents',
    '[ ok ] eval runs · 2',
    HELP,
  ]);
  assert.equal(statuses.api, 'live');
});

test('boot uses the full banner unless compact is requested', async () => {
  const full = fakeCtx({ get: healthyGet });
  await boot(full.ctx, { reducedMotion: true, banner: BANNER, bannerCompact: BANNER_COMPACT });
  assert.deepEqual(full.printed[0], { kind: 'banner', text: BANNER });

  const compact = fakeCtx({ get: healthyGet });
  await boot(compact.ctx, {
    reducedMotion: true,
    compact: true,
    banner: BANNER,
    bannerCompact: BANNER_COMPACT,
  });
  assert.deepEqual(compact.printed[0], { kind: 'banner', text: BANNER_COMPACT });
});

test('health down never prints ok, prints closed line and skips every other check', async () => {
  const { ctx, texts, statuses, calls } = fakeCtx({ health: { ok: false, status: null } });
  const result = await boot(ctx, { reducedMotion: true, banner: BANNER, bannerCompact: '' });
  assert.deepEqual(result, { state: 'closed' });
  const lines = texts();
  assert.equal(lines.some((t) => t.includes('[ ok ]')), false);
  assert.ok(lines.includes('[ !! ] api unreachable'));
  assert.ok(lines.includes(CLOSED));
  assert.equal(statuses.api, CLOSED);
  assert.deepEqual(calls, [['health']]);
  assert.equal(lines.at(-1), HELP);
});

test('health reporting a server error is also closed', async () => {
  const { ctx, statuses } = fakeCtx({ health: { ok: false, status: 503 } });
  const result = await boot(ctx, { reducedMotion: true, banner: '', bannerCompact: '' });
  assert.deepEqual(result, { state: 'closed' });
  assert.equal(statuses.api, CLOSED);
});

test('401 on the auth check prints the exact token line and state locked', async () => {
  const { ctx, texts, statuses } = fakeCtx({
    get: () => {
      throw new ApiError('api token required', { status: 401, kind: 'unauthorized' });
    },
  });
  const result = await boot(ctx, { reducedMotion: true, banner: '', bannerCompact: '' });
  assert.deepEqual(result, { state: 'locked' });
  const lines = texts();
  assert.ok(lines.includes(TOKEN));
  assert.equal(lines.some((t) => t.includes('[ ok ] auth')), false);
  assert.equal(lines.some((t) => t.includes('catalog')), false);
  assert.equal(lines.some((t) => t.includes('eval runs')), false);
  assert.equal(statuses.api, 'locked — api token required');
  assert.equal(lines.at(-1), HELP);
});

test('offline on the auth check is closed, not locked, and never prints auth ok', async () => {
  const { ctx, texts, statuses } = fakeCtx({
    get: () => {
      throw new ApiError('no api connection', { kind: 'offline' });
    },
  });
  const result = await boot(ctx, { reducedMotion: true, banner: '', bannerCompact: '' });
  assert.deepEqual(result, { state: 'closed' });
  const lines = texts();
  assert.ok(lines.includes('[ !! ] api unreachable'));
  assert.ok(lines.includes(CLOSED));
  assert.equal(lines.some((t) => t.includes('[ ok ] auth')), false);
  assert.equal(statuses.api, CLOSED);
});

test('http error on the auth check is closed and prints the failure, not ok', async () => {
  const { ctx, texts } = fakeCtx({
    get: () => {
      throw new ApiError('boom', { status: 500, kind: 'http', detail: 'boom' });
    },
  });
  const result = await boot(ctx, { reducedMotion: true, banner: '', bannerCompact: '' });
  assert.deepEqual(result, { state: 'closed' });
  const lines = texts();
  assert.ok(lines.includes('[ !! ] auth · boom'));
  assert.equal(lines.some((t) => t.includes('[ ok ]') && t.includes('auth')), false);
  assert.ok(lines.includes(CLOSED));
});

test('a failing eval runs check is printed as failed and does not stop the boot', async () => {
  const { ctx, texts } = fakeCtx({
    get: (path, query) => {
      if (path === '/v1/runs') throw new ApiError('no api connection', { kind: 'offline' });
      return healthyGet(path, query);
    },
  });
  const result = await boot(ctx, { reducedMotion: true, banner: '', bannerCompact: '' });
  assert.deepEqual(result, { state: 'live' });
  const lines = texts();
  assert.ok(lines.includes('[ !! ] eval runs · api unreachable'));
  assert.equal(lines.some((t) => t.includes('[ ok ] eval runs')), false);
  assert.equal(lines.at(-1), HELP);
});

test('an aborted signal prints every line with no delays but still runs the checks', async () => {
  const controller = new AbortController();
  controller.abort();
  const { ctx, texts, calls } = fakeCtx({ get: healthyGet });
  const { sleep, delays } = fakeSleep();
  const result = await boot(ctx, {
    reducedMotion: false,
    signal: controller.signal,
    banner: BANNER,
    bannerCompact: '',
    sleep,
  });
  assert.deepEqual(result, { state: 'live' });
  assert.equal(delays.length, 0);
  assert.equal(texts().length, 7);
  assert.ok(calls.some((c) => c[0] === 'health'));
});

test('reduced motion never delays, even without an abort', async () => {
  const { ctx } = fakeCtx({ get: healthyGet });
  const { sleep, delays } = fakeSleep();
  await boot(ctx, { reducedMotion: true, banner: BANNER, bannerCompact: '', sleep });
  assert.equal(delays.length, 0);
});

test('normal motion delays for the banner and between lines', async () => {
  const { ctx } = fakeCtx({ get: healthyGet });
  const { sleep, delays } = fakeSleep();
  await boot(ctx, { reducedMotion: false, banner: BANNER, bannerCompact: '', sleep });
  assert.ok(delays.length > 0);
  assert.equal(delays[0], 900);
  assert.ok(delays.slice(1).every((ms) => ms > 0));
});

test('the signal aborting mid-run stops later delays', async () => {
  const controller = new AbortController();
  const { ctx } = fakeCtx({ get: healthyGet });
  const delays = [];
  const sleep = async (ms) => {
    delays.push(ms);
    controller.abort();
  };
  await boot(ctx, {
    reducedMotion: false,
    signal: controller.signal,
    banner: BANNER,
    bannerCompact: '',
    sleep,
  });
  assert.deepEqual(delays, [900]);
});

test('boot line reflects the real theme and crt state', async () => {
  const f = fakeCtx();
  await boot(f.ctx, { reducedMotion: true, theme: 'hc', crt: false });
  assert.ok(f.texts().includes('boot: tty · crt off · theme hc'));
});

test('catalog prints N+ when the page is full', async () => {
  const docs = Array.from({ length: 500 }, (_, i) => ({ doc_id: String(i) }));
  const f = fakeCtx({ get: (p) => (p === '/v1/documents' ? { documents: docs } : { runs: [] }) });
  await boot(f.ctx, { reducedMotion: true });
  assert.ok(f.texts().includes('[ ok ] catalog · 500+ documents'));
});

test('boot prints a jev status line, non-fatal on failure', async () => {
  const run = async (jev) => {
    const f = fakeCtx({ get: (p, q) => (p === '/v1/jev' ? jev() : healthyGet(p, q)) });
    const r = await boot(f.ctx, { reducedMotion: true, banner: BANNER });
    return { r, t: f.texts() };
  };
  let x = await run(() => ({ enabled: true, provider: 'local', calibrated: true }));
  assert.ok(x.t.includes('[ ok ] jev · local calibrated'));
  x = await run(() => ({ enabled: false }));
  assert.ok(x.t.includes('[ -- ] jev · off'));
  x = await run(() => { throw new ApiError('no', { status: 401, kind: 'unauthorized' }); });
  assert.equal(x.r.state, 'live');
  assert.ok(!x.t.some((l) => l.includes('jev')));
});
