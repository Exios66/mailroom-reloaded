import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFollowReader, parseSseFrame } from '../../../src/mailroom_reloaded/api/tui/replay/live.js';

const URL = '/v1/replay/live?session=run%3Ar1';
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

function streamOf(chunks) {
  const enc = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    pull(c) {
      if (i >= chunks.length) {
        c.close();
        return;
      }
      c.enqueue(enc.encode(chunks[i++]));
    },
  });
}

test('parseSseFrame reads the event name and JSON data', () => {
  assert.deepEqual(parseSseFrame('event: segment\ndata: {"t0":1}'), { name: 'segment', data: { t0: 1 } });
  assert.deepEqual(parseSseFrame('data: hello'), { name: 'message', data: 'hello' });
  assert.equal(parseSseFrame(': keepalive'), null);
  assert.equal(parseSseFrame('event: heartbeat'), null);
});

test('parses frames split across chunks, then stop() ends the reader', async () => {
  const seen = [];
  let resolveTwo;
  const two = new Promise((r) => {
    resolveTwo = r;
  });
  let calls = 0;
  const reader = createFollowReader({
    fetchFn: async () => {
      calls += 1;
      return {
        ok: true,
        status: 200,
        body: streamOf([
          'event: ready\ndata: {"session":"run:r1"}\n\nevent: seg',
          'ment\ndata: {"doc_id":"d1","t0":0}\n\n',
        ]),
      };
    },
    url: URL,
    headers: { Accept: 'text/event-stream' },
    onFrame: (name, obj) => {
      seen.push([name, obj]);
      if (seen.length === 2) resolveTwo();
    },
    backoffMs: 1000,
  });
  await two;
  reader.stop();
  await reader.done;
  assert.deepEqual(seen, [
    ['ready', { session: 'run:r1' }],
    ['segment', { doc_id: 'd1', t0: 0 }],
  ]);
  assert.equal(calls, 1);
});

test('reconnects with increasing backoff until stopped', async () => {
  const times = [];
  const reader = createFollowReader({
    fetchFn: async () => {
      times.push(Date.now());
      throw new Error('offline');
    },
    url: URL,
    headers: {},
    onFrame: () => {},
    backoffMs: 20,
  });
  await delay(250);
  reader.stop();
  await reader.done;
  assert.ok(times.length >= 3, `expected reconnects, got ${times.length}`);
  const count = times.length;
  await delay(60);
  assert.equal(times.length, count, 'no reconnect after stop');
  const gaps = times.slice(1).map((t, i) => t - times[i]);
  assert.ok(gaps[gaps.length - 1] >= gaps[0], `non-increasing? ${gaps}`);
});

test('a non-ok response is retried, never thrown out of the loop', async () => {
  let calls = 0;
  const reader = createFollowReader({
    fetchFn: async () => {
      calls += 1;
      return { ok: false, status: 500, body: null };
    },
    url: URL,
    headers: {},
    onFrame: () => {},
    backoffMs: 5,
  });
  await delay(40);
  reader.stop();
  await reader.done;
  assert.ok(calls >= 2);
});

test('an aborted caller signal stops the reader', async () => {
  const ctl = new AbortController();
  let calls = 0;
  const reader = createFollowReader({
    fetchFn: async () => {
      calls += 1;
      throw new Error('offline');
    },
    url: URL,
    headers: {},
    signal: ctl.signal,
    onFrame: () => {},
    backoffMs: 1000,
  });
  await delay(5);
  ctl.abort();
  await reader.done;
  assert.ok(calls >= 1);
  await delay(10);
  assert.equal(calls, 1);
});
