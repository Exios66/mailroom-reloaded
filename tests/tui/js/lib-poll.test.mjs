import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createPoller } from '../../../src/mailroom_reloaded/api/tui/lib/poll.js';

/** Manual timers: run() fires the earliest pending timer. */
function fakeTimers() {
  let id = 0;
  const pending = new Map();
  return {
    pending,
    setTimeout(fn, ms) {
      pending.set(++id, { fn, ms });
      return id;
    },
    clearTimeout(t) {
      pending.delete(t);
    },
    async run() {
      const [first] = pending.keys();
      if (first === undefined) return false;
      const { fn } = pending.get(first);
      pending.delete(first);
      await fn();
      return true;
    },
  };
}

function fakeDoc() {
  const listeners = new Set();
  return {
    hidden: false,
    addEventListener: (_e, fn) => listeners.add(fn),
    removeEventListener: (_e, fn) => listeners.delete(fn),
    fire() {
      for (const fn of listeners) fn();
    },
    listeners,
  };
}

test('ticks immediately, then every interval, without overlap', async () => {
  const timers = fakeTimers();
  let calls = 0;
  const p = createPoller(() => { calls++; }, { intervalMs: 1000, timers, doc: null });
  p.start();
  assert.equal([...timers.pending.values()][0].ms, 0);
  await timers.run();
  assert.equal(calls, 1);
  assert.equal(timers.pending.size, 1);
  assert.equal([...timers.pending.values()][0].ms, 1000);
  await timers.run();
  assert.equal(p.ticks, 2);
  p.stop();
  assert.equal(timers.pending.size, 0);
});

test('a hidden tab skips ticks until visible again', async () => {
  const timers = fakeTimers();
  const doc = fakeDoc();
  let calls = 0;
  const p = createPoller(() => { calls++; }, { intervalMs: 1000, timers, doc });
  doc.hidden = true;
  p.start();
  await timers.run();
  assert.equal(calls, 0);
  assert.equal(timers.pending.size, 0); // nothing scheduled while hidden
  doc.hidden = false;
  doc.fire();
  await timers.run();
  assert.equal(calls, 1);
  p.stop();
  assert.equal(doc.listeners.size, 0);
});

test('aborting the signal stops polling and aborts the in-flight tick', async () => {
  const timers = fakeTimers();
  const outer = new AbortController();
  let tickSignal;
  const p = createPoller((s) => { tickSignal = s; }, { intervalMs: 1000, timers, doc: null, signal: outer.signal });
  p.start();
  await timers.run();
  outer.abort();
  assert.equal(p.running, false);
  assert.equal(tickSignal.aborted, true);
  assert.equal(timers.pending.size, 0);
  p.start(); // an aborted signal never restarts
  assert.equal(p.running, false);
});

test('errors go to onError and polling continues', async () => {
  const timers = fakeTimers();
  const errors = [];
  const p = createPoller(() => { throw new Error('boom'); }, {
    intervalMs: 1000, timers, doc: null, onError: (e) => errors.push(e.message),
  });
  p.start();
  await timers.run();
  await timers.run();
  assert.deepEqual(errors, ['boom', 'boom']);
  assert.equal(p.running, true);
  p.stop();
});

test('rejects a non-function and clamps a silly interval', () => {
  assert.throws(() => createPoller(null), /function/);
  const timers = fakeTimers();
  const p = createPoller(() => {}, { intervalMs: 1, timers, doc: null, immediate: false });
  p.start();
  assert.equal([...timers.pending.values()][0].ms, 5000);
  p.stop();
});
