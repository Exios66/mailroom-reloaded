import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRegistry, createHistory, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { registerShell } from '../../../src/mailroom_reloaded/api/tui/commands/shell.js';

function fakeAmbient() {
  const calls = [];
  let theme = 'dark';
  return {
    calls,
    setCrt: (v) => calls.push(['crt', v]),
    setSkyline: (v) => calls.push(['skyline', v]),
    setTheme(n) { calls.push(['theme', n]); theme = n; return true; },
    state: () => ({ theme, label: theme, crt: true, skyline: true }),
  };
}

function setup({ api, ambient = fakeAmbient(), banner = 'BANNER' } = {}) {
  const registry = createRegistry();
  registry.register({ name: 'ls', summary: 'list documents', usage: 'ls', man: 'NAME\n  ls\n', run() {} });
  registerShell(registry, { ambient });
  const log = [];
  const status = {};
  const history = createHistory();
  const ctx = {
    banner,
    registry,
    history,
    api,
    setStatus: (k, v) => { status[k] = v; },
    out: {
      line: (t, c) => log.push(['line', t, c]),
      pre: (t) => log.push(['pre', t]),
      kv: (p) => log.push(['kv', p]),
      man: (t) => log.push(['man', t]),
      clear: () => log.push(['clear']),
    },
  };
  return { registry, ctx, log, ambient, status, history };
}

test('help lists every registered command exactly once', async () => {
  const { registry, ctx, log } = setup();
  await dispatch(registry, ctx, 'help');
  const kv = log.find((e) => e[0] === 'kv')[1];
  assert.deepEqual(kv.map((p) => p[0]), registry.names());
  assert.equal(new Set(kv.map((p) => p[0])).size, kv.length);
  assert.equal(kv.find((p) => p[0] === 'ls')[1], 'list documents');
});

test('help <cmd> is man <cmd>; unknown man entry message is exact', async () => {
  const { registry, ctx, log } = setup();
  await dispatch(registry, ctx, 'help ls');
  assert.deepEqual(log.at(-1), ['man', 'NAME\n  ls\n']);
  await dispatch(registry, ctx, 'man nope');
  assert.deepEqual(log.at(-1), ['line', 'man: no manual entry for nope', 'error']);
});

test('theme bogus prints usage and does not change theme', async () => {
  const { registry, ctx, log, ambient } = setup();
  await dispatch(registry, ctx, 'theme bogus');
  assert.deepEqual(log.at(-1), ['line', 'theme: usage theme [dark|light|hc|amber|green|cyan]', 'error']);
  assert.equal(ambient.calls.length, 0);
});

test('theme hc turns crt and skyline off', async () => {
  const { registry, ctx, ambient, status } = setup();
  await dispatch(registry, ctx, 'theme hc');
  assert.deepEqual(ambient.calls, [['crt', false], ['skyline', false], ['theme', 'hc']]);
  assert.equal(status.theme, 'hc');
});

test('theme with no argument lists the current theme', async () => {
  const { registry, ctx, log } = setup();
  await dispatch(registry, ctx, 'theme');
  assert.equal(log[0][1], 'theme: dark');
});

test('theme survives storage that throws (ambient owns persistence)', async () => {
  const { createAmbient } = await import('../../../src/mailroom_reloaded/api/tui/ambient.js');
  const boom = { getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); } };
  const a = createAmbient({ querySelector: () => null, documentElement: null }, { storage: boom });
  const { registry, ctx, log } = setup({ ambient: a });
  assert.equal(await dispatch(registry, ctx, 'theme light'), 'ok');
  assert.deepEqual(log.at(-1), ['line', 'theme: light', 'success']);
});

test('crt and skyline toggle and validate', async () => {
  const { registry, ctx, log, ambient } = setup();
  await dispatch(registry, ctx, 'crt off');
  await dispatch(registry, ctx, 'skyline on');
  await dispatch(registry, ctx, 'crt maybe');
  assert.deepEqual(ambient.calls, [['crt', false], ['skyline', true]]);
  assert.deepEqual(log.at(-1), ['line', 'crt: usage crt on|off', 'error']);
});

test('clear and history', async () => {
  const { registry, ctx, log, history } = setup();
  await dispatch(registry, ctx, 'clear');
  assert.deepEqual(log.at(-1), ['clear']);
  await dispatch(registry, ctx, 'history');
  assert.equal(log.at(-1)[1], 'history: empty');
  history.push('ls');
  history.push('help');
  await dispatch(registry, ctx, 'history');
  assert.deepEqual(log.slice(-2).map((e) => e[1]), ['1  ls', '2  help']);
});

test('neofetch uses the banner and live counts, never invented ones', async () => {
  const api = {
    get: async (path) => (path === '/v1/documents' ? { documents: [{}, {}, {}] } : { runs: [{}] }),
  };
  const { registry, ctx, log } = setup({ api });
  await dispatch(registry, ctx, 'neofetch');
  assert.deepEqual(log[0], ['pre', 'BANNER']);
  const kv = Object.fromEntries(log.find((e) => e[0] === 'kv')[1]);
  assert.equal(kv.documents, '3');
  assert.equal(kv.runs, '1');
  assert.equal(kv.edition, 'terminal');
});

test('neofetch when the api is down shows unavailable, not numbers', async () => {
  const api = { get: async () => { throw Object.assign(new Error('no api connection'), { kind: 'offline' }); } };
  const { registry, ctx, log } = setup({ api });
  await dispatch(registry, ctx, 'neofetch');
  const kv = Object.fromEntries(log.find((e) => e[0] === 'kv')[1]);
  assert.equal(kv.documents, 'unavailable — api unreachable');
  assert.equal(kv.runs, 'unavailable — api unreachable');
});
