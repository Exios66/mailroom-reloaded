import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { createModuleHost, RESERVED_MODULE_IDS } from '../../../src/mailroom_reloaded/api/tui/plugins.js';
import { createRegistry, dispatch } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { getPanel, resetPanels } from '../../../src/mailroom_reloaded/api/tui/replay/panels.js';
import { registerAll, MODULES } from '../../../src/mailroom_reloaded/api/tui/main.js';

const quiet = { warn() {} };
const cmd = (name) => ({ name, summary: name, run() {} });
const ambient = { state: () => ({}), setCrt() {}, setSkyline() {}, setTheme() {} };

function fakePanels() {
  const stored = new Map();
  return {
    stored,
    register(spec) {
      if (stored.has(spec.id)) throw new Error(`panel '${spec.id}' is already registered`);
      stored.set(spec.id, spec);
      return spec;
    },
  };
}

afterEach(() => resetPanels());

test('duplicate module id throws', () => {
  const host = createModuleHost(createRegistry(), { panels: fakePanels(), log: quiet });
  host.registerModule({ id: 'alpha', commands: [cmd('a1')] });
  assert.throws(() => host.registerModule({ id: 'alpha', commands: [cmd('a2')] }), /duplicate module: alpha/);
});

test('reserved module id is rejected', () => {
  const host = createModuleHost(createRegistry(), { panels: fakePanels(), log: quiet });
  for (const id of RESERVED_MODULE_IDS) {
    assert.throws(() => host.registerModule({ id, commands: [cmd(`x_${id}`)] }), /reserved/);
  }
});

test('malformed module ids are rejected', () => {
  const host = createModuleHost(createRegistry(), { panels: fakePanels(), log: quiet });
  for (const id of [undefined, '', 'Upper', '1lead', 'has space', 'a'.repeat(33), '<b>']) {
    assert.throws(() => host.registerModule({ id, commands: [] }), /module id/);
  }
});

test('a command clash rejects the whole module and registers none of it', () => {
  const registry = createRegistry();
  const host = createModuleHost(registry, { panels: fakePanels(), log: quiet });
  host.registerModule({ id: 'first', commands: [cmd('shared')] });
  assert.throws(
    () => host.registerModule({ id: 'second', commands: [cmd('fresh'), cmd('shared')] }),
    /duplicate command shared/,
  );
  assert.equal(registry.get('fresh'), undefined);
  assert.deepEqual(host.modules().map((m) => m.id), ['first']);
});

test('a command without a run function rejects the module', () => {
  const registry = createRegistry();
  const host = createModuleHost(registry, { panels: fakePanels(), log: quiet });
  assert.throws(() => host.registerModule({ id: 'bad', commands: [{ name: 'norun' }] }), /run function/);
  assert.equal(registry.get('norun'), undefined);
});

test('panel row with markup is sanitised', () => {
  const panels = fakePanels();
  const host = createModuleHost(createRegistry(), { panels, log: quiet });
  host.registerModule({
    id: 'hostile',
    panels: [
      {
        id: 'evil',
        title: 'Evil',
        render: () => [
          [['<img src=x onerror=alert(1)>‮​spoof', 'err" onclick="x']],
          'not a row',
          [[{ toString: () => '<script>' }, 'ok'], ['fine', 'ok']],
        ],
      },
    ],
  });
  const rows = panels.stored.get('evil').render({ cols: 80 });
  const text = rows.map((r) => r.map((s) => s[0]).join('')).join('\n');
  assert.doesNotMatch(text, /[‪-‮​-‏]/);
  // Markup stays inert text (rows are drawn with textContent); unknown classes are dropped.
  assert.equal(rows[0][0][1], '');
  assert.equal(rows.length, 2); // the non-row entry is dropped
  // A non-string, non-number segment becomes empty text (and is clipped away).
  assert.doesNotMatch(text, /<script>/);
  assert.deepEqual(rows[1].at(-1), ['fine', 'ok']);
});

test('a throwing panel renderer yields no rows', () => {
  const panels = fakePanels();
  const host = createModuleHost(createRegistry(), { panels, log: quiet });
  host.registerModule({ id: 'boom', panels: [{ id: 'boom', render: () => { throw new Error('x'); } }] });
  assert.deepEqual(panels.stored.get('boom').render({ cols: 80 }), []);
});

test('module panels reach the replay panel registry, which still rejects its reserved ids', () => {
  const host = createModuleHost(createRegistry(), { log: quiet });
  const res = host.registerModule({
    id: 'trace',
    panels: [
      { id: 'trace_summary', title: 'Trace', render: () => [[['spans 3', 'info']]] },
      { id: 'inspector', title: 'Clash', render: () => [] },
    ],
  });
  assert.deepEqual(res.panels, ['trace_summary']);
  assert.deepEqual(getPanel('trace_summary').render({ cols: 80 }), [[['spans 3', 'info']]]);
});

test('onInit throw does not stop registerAll', async () => {
  const registry = createRegistry();
  const host = createModuleHost(registry, { panels: fakePanels(), log: quiet });
  const seen = [];
  const failures = host.registerAll([
    { id: 'broken', commands: [{ name: '' }] }, // rejected at registration
    { id: 'throws', commands: [cmd('t1')], onInit: () => { throw new Error('init boom'); } },
    { id: 'rejects', commands: [cmd('r1')], onInit: async () => { throw new Error('async boom'); } },
    { id: 'healthy', commands: [cmd('h1')], onInit: (ctx) => seen.push(ctx.tag) },
  ]);
  assert.equal(failures.length, 1);
  assert.equal(failures[0].id, 'broken');
  const initFailures = await host.initAll({ tag: 'ctx' });
  assert.deepEqual(initFailures.map((f) => f.id), ['throws', 'rejects']);
  assert.deepEqual(seen, ['ctx']);
  const state = Object.fromEntries(host.modules().map((m) => [m.id, m.state]));
  assert.deepEqual(state, { throws: 'failed', rejects: 'failed', healthy: 'ready' });
  for (const name of ['t1', 'r1', 'h1']) assert.ok(registry.get(name));
});

test('register functions see the real registry for lazy lookups', async () => {
  const registry = createRegistry();
  const host = createModuleHost(registry, { panels: fakePanels(), log: quiet });
  host.registerModule({ id: 'other', commands: [cmd('zeta')] });
  host.registerModule({
    id: 'lister',
    commands: (r) => r.register({ name: 'list', run: (ctx) => ctx.out.line(r.names().join(' ')) }),
  });
  const lines = [];
  await dispatch(registry, { out: { line: (t) => lines.push(t) } }, 'list');
  assert.deepEqual(lines, ['list zeta']);
});

test('built-in modules register the same commands as before, each once', () => {
  const registry = createRegistry();
  const host = registerAll(registry, { ambient });
  assert.deepEqual(host.modules().map((m) => m.id), MODULES.map((m) => m.id));
  for (const name of ['help', 'man', 'ls', 'inspect', 'review', 'resolve', 'ledger', 'replay', 'inbox', 'auth', 'theme']) {
    assert.ok(registry.get(name), `missing ${name}`);
  }
  assert.ok(host.modules().every((m) => m.state === 'registered'));
});

test('a bad module in the list does not block the built-ins (boot survives)', async () => {
  const registry = createRegistry();
  const warnings = [];
  const prev = console.warn;
  console.warn = (m) => warnings.push(m);
  try {
    const host = registerAll(registry, { ambient }, [
      ...MODULES,
      { id: 'core', commands: [cmd('hijack')] },
      { id: 'dupe', commands: [cmd('help')] },
      { id: 'late', commands: [cmd('late')], onInit: () => { throw new Error('x'); } },
    ]);
    assert.equal(registry.get('hijack'), undefined);
    assert.notEqual(registry.get('help').summary, 'help'); // still the shell's help
    assert.deepEqual(
      host.modules().map((m) => m.id),
      [...MODULES.map((m) => m.id), 'late'],
    );
    assert.equal(warnings.length, 2);
    const failed = await host.initAll({});
    assert.deepEqual(failed.map((f) => f.id), ['late']);
    assert.ok(registry.get('late'));
  } finally {
    console.warn = prev;
  }
});
