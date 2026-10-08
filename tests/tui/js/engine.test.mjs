import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  parseLine,
  createRegistry,
  createHistory,
  dispatch,
} from '../../../src/mailroom_reloaded/api/tui/engine.js';

function fakeCtx() {
  const lines = [];
  return {
    lines,
    out: { line: (text, kind = 'normal') => lines.push({ text, kind }) },
  };
}

test('parseLine splits on whitespace and groups quotes', () => {
  const r = parseLine('ls --status parked "a b"');
  assert.equal(r.cmd, 'ls');
  assert.deepEqual(r.flags, { status: 'parked' });
  assert.deepEqual(r.args, ['a b']);
  assert.equal(r.error, undefined);
});

test('parseLine accepts single quotes and empty input', () => {
  assert.deepEqual(parseLine("inspect 'x y'").args, ['x y']);
  const empty = parseLine('   ');
  assert.equal(empty.cmd, '');
  assert.deepEqual(empty.args, []);
});

test('parseLine reports unbalanced quotes', () => {
  const r = parseLine('x "oops');
  assert.equal(r.error, 'unterminated quote');
});

test('parseLine supports --k=v, --k v and bare --k', () => {
  const r = parseLine('watch --every=5 --since 3 --all');
  assert.deepEqual(r.flags, { every: '5', since: '3', all: true });
});

test('history skips empties and consecutive duplicates and walks prev/next', () => {
  const h = createHistory();
  h.push('');
  h.push('ls');
  h.push('ls');
  h.push('inspect 1');
  assert.deepEqual(h.all(), ['ls', 'inspect 1']);
  assert.equal(h.prev(), 'inspect 1');
  assert.equal(h.prev(), 'ls');
  assert.equal(h.prev(), 'ls');
  assert.equal(h.next(), 'inspect 1');
  assert.equal(h.next(), undefined);
  h.reset();
  assert.equal(h.prev(), 'inspect 1');
});

test('history is capped at max', () => {
  const h = createHistory(2);
  h.push('a');
  h.push('b');
  h.push('c');
  assert.deepEqual(h.all(), ['b', 'c']);
});

test('registry completes a unique prefix with a ghost suffix', () => {
  const reg = createRegistry();
  reg.register({ name: 'help', summary: '', usage: '', man: '', run: async () => {} });
  reg.register({ name: 'ls', summary: '', usage: '', man: '', run: async () => {} });
  const c = reg.complete('he');
  assert.deepEqual(c.matches, ['help']);
  assert.equal(c.ghost, 'lp');
  assert.deepEqual(reg.complete('').matches.sort(), ['help', 'ls']);
  assert.equal(reg.complete('').ghost, '');
});

test('registry rejects duplicate names and unknown get returns undefined', () => {
  const reg = createRegistry();
  const spec = { name: 'ls', summary: '', usage: '', man: '', run: async () => {} };
  reg.register(spec);
  assert.throws(() => reg.register(spec));
  assert.equal(reg.get('nope'), undefined);
  assert.equal(reg.get('ls').name, 'ls');
  assert.deepEqual(reg.names(), ['ls']);
});

test('dispatch prints the exact unknown-command message', async () => {
  const reg = createRegistry();
  const ctx = fakeCtx();
  const status = await dispatch(reg, ctx, 'frobnicate --x');
  assert.equal(status, 'unknown');
  assert.deepEqual(ctx.lines, [
    { text: 'frobnicate: command not found — try help', kind: 'error' },
  ]);
});

test('dispatch runs a command with args and flags', async () => {
  const reg = createRegistry();
  let seen;
  reg.register({
    name: 'ls',
    summary: '',
    usage: '',
    man: '',
    run: async (ctx, args, flags) => {
      seen = { args, flags };
    },
  });
  const status = await dispatch(reg, fakeCtx(), 'ls --status parked');
  assert.equal(status, 'ok');
  assert.deepEqual(seen, { args: [], flags: { status: 'parked' } });
});

test('dispatch returns error and prints once when run throws', async () => {
  const reg = createRegistry();
  reg.register({
    name: 'boom',
    summary: '',
    usage: '',
    man: '',
    run: async () => {
      throw new Error('api down');
    },
  });
  const ctx = fakeCtx();
  const status = await dispatch(reg, ctx, 'boom');
  assert.equal(status, 'error');
  assert.deepEqual(ctx.lines, [{ text: 'boom: api down', kind: 'error' }]);
});

test('dispatch of an empty line is ok and prints nothing', async () => {
  const ctx = fakeCtx();
  assert.equal(await dispatch(createRegistry(), ctx, '  '), 'ok');
  assert.deepEqual(ctx.lines, []);
});

test('dispatch reports parse errors as error without running anything', async () => {
  const ctx = fakeCtx();
  const status = await dispatch(createRegistry(), ctx, 'x "oops');
  assert.equal(status, 'error');
  assert.equal(ctx.lines.length, 1);
  assert.equal(ctx.lines[0].kind, 'error');
});
