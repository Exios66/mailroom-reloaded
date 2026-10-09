import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createModel } from '../../../src/mailroom_reloaded/api/tui/replay/model.js';
import { STATIONS } from '../../../src/mailroom_reloaded/api/tui/replay/stations.js';

const HOSTILE = '<img src=x onerror=alert(1)>';

function timeline(extra = {}) {
  return {
    version: 'replay/v1',
    session: { id: 's1', duration_s: 20 },
    stations: [],
    entities: [
      { doc_id: 'a', filename: HOSTILE, t_start: 1, t_end: 12, final_status: 'archived', verdict: 'pass' },
      { doc_id: 'b', filename: 'b.pdf', t_start: 3, t_end: 9, final_status: 'failed' },
      { doc_id: 'c', filename: 'c.pdf', t_start: 4, t_end: null },
    ],
    segments: [
      { doc_id: 'a', node: 'sort', station: 'sorter', t0: 2, t1: 4, status: 'ok' },
      { doc_id: 'a', node: 'extract', station: 'specialist', t0: 4, t1: 6, status: 'failed' },
      { doc_id: 'a', node: 'extract', station: 'specialist', t0: 7, t1: 10, attempt: 2, retry_kind: 'llm', status: 'ok' },
      { doc_id: 'b', node: 'sort', station: 'sorter', t0: 3, t1: 5, status: 'ok' },
      { doc_id: 'b', node: 'x', station: 'failed', t0: 5, t1: 5, status: 'ok' },
      { doc_id: 'c', node: 'extract', station: 'specialist', t0: 5, t1: 5, status: 'running' },
    ],
    events: [
      { t: 2, doc_id: 'a', kind: 'gate' },
      { t: 4, doc_id: 'a', kind: 'retry' },
      { t: 4, doc_id: 'b', kind: 'park' },
      { t: 8, doc_id: 'a', kind: 'done' },
    ],
    scores: [
      { doc_id: 'a', name: 'q', value: 1, t: 6 },
      { doc_id: 'b', name: 'q', value: 0, t: 7 },
    ],
    generations: [
      { doc_id: 'a', span_id: 'g1', t0: 2, t1: 3, prompt_tokens: 10, completion_tokens: 5, cost_usd: 0.5 },
      { doc_id: 'a', span_id: 'g2', t0: 7, t1: 9, prompt_tokens: 20, completion_tokens: 1, cost_usd: 0.25 },
      { doc_id: 'b', span_id: 'g3', t0: 3, t1: 4, prompt_tokens: 1, completion_tokens: 1, cost_usd: 0.1 },
    ],
    ...extra,
  };
}

const byId = (state, id) => state.docs.find((d) => d.doc_id === id);

test('doc lifecycle: waiting, at station, idle, finished', () => {
  const m = createModel(timeline());
  assert.equal(byId(m.stateAt(0.5), 'a').status, 'waiting');
  assert.equal(byId(m.stateAt(0.5), 'a').station, null);
  assert.equal(byId(m.stateAt(1.5), 'a').status, 'waiting');
  const s = m.stateAt(2);
  assert.equal(byId(s, 'a').station, 'sorter');
  assert.equal(byId(s, 'a').status, 'ok');
  assert.equal(s.counts.sorter, 1);
  assert.equal(byId(m.stateAt(4), 'a').station, 'specialist');
  assert.equal(byId(m.stateAt(4), 'a').status, 'failed');
  const idle = byId(m.stateAt(6.5), 'a');
  assert.equal(idle.station, 'specialist');
  assert.equal(idle.status, 'idle');
  const fin = byId(m.stateAt(12), 'a');
  assert.equal(fin.finished, true);
  assert.equal(fin.status, 'archived');
  assert.equal(fin.station, 'specialist');
});

test('retry segments expose attempt and retry_kind', () => {
  const m = createModel(timeline());
  const d = byId(m.stateAt(8), 'a');
  assert.equal(d.attempt, 2);
  assert.equal(d.retry_kind, 'llm');
  assert.equal(byId(m.stateAt(5), 'a').attempt, 1);
  assert.equal(m.segmentsFor('a').length, 3);
});

test('running segment with t1 <= t0 stays current; half-open boundaries', () => {
  const m = createModel(timeline());
  const c = byId(m.stateAt(15), 'c');
  assert.equal(c.status, 'running');
  assert.equal(c.station, 'specialist');
  assert.equal(byId(m.stateAt(4), 'a').station, 'specialist');
  assert.equal(byId(m.stateAt(3.999), 'a').station, 'sorter');
});

test('counts, done, failed, active', () => {
  const m = createModel(timeline());
  const s = m.stateAt(10);
  assert.equal(s.failed, 1);
  assert.equal(s.done, 0);
  assert.equal(s.active, 2);
  assert.equal(s.counts.failed, 1);
  const end = m.stateAt(13);
  assert.equal(end.done, 1);
  assert.equal(end.failed, 1);
  assert.equal(end.active, 1);
  assert.equal(end.counts.gate, 0);
});

test('eventsBetween is (a, b] and eventsUpTo is newest-last with limit', () => {
  const m = createModel(timeline());
  assert.deepEqual(m.eventsBetween(2, 4).map((e) => e.kind), ['retry', 'park']);
  assert.deepEqual(m.eventsBetween(4, 4), []);
  assert.deepEqual(m.eventsBetween(8, 2), []);
  assert.deepEqual(m.eventsUpTo(4).map((e) => e.kind), ['gate', 'retry', 'park']);
  assert.deepEqual(m.eventsUpTo(20, 2).map((e) => e.kind), ['park', 'done']);
  assert.deepEqual(m.eventsUpTo(20, 0), []);
});

test('scoresAt, generationsFor, docAt', () => {
  const m = createModel(timeline());
  assert.equal(m.scoresAt(6.5).length, 1);
  assert.equal(m.scoresAt(10).length, 2);
  assert.equal(m.scoresAt(10, 'b').length, 1);
  assert.deepEqual(m.generationsFor('a', 5).map((g) => g.span_id), ['g1']);
  // g2 spans t 7..9: it is in flight at 7 (no tokens or cost yet), so it shows from 9.
  assert.deepEqual(m.generationsFor('a', 7).map((g) => g.span_id), ['g1']);
  assert.deepEqual(m.generationsFor('a', 9).map((g) => g.span_id), ['g1', 'g2']);
  assert.equal(m.docAt('b', 100).filename, 'b.pdf');
  assert.equal(m.docAt('zzz', 100), undefined);
  assert.deepEqual(m.segmentsFor('zzz'), []);
});

test('runningTotalsAt sums generations that have ended', () => {
  const m = createModel(timeline());
  assert.deepEqual(m.runningTotalsAt(0), { tokens: 0, cost_usd: 0, llm_calls: 0 });
  assert.deepEqual(m.runningTotalsAt(3), { tokens: 15, cost_usd: 0.5, llm_calls: 1 });
  const all = m.runningTotalsAt(100);
  assert.equal(all.tokens, 15 + 21 + 2);
  assert.equal(all.llm_calls, 3);
  assert.ok(Math.abs(all.cost_usd - 0.85) < 1e-9);
});

test('duration is the max of session and latest segment or event', () => {
  assert.equal(createModel(timeline()).duration, 20);
  const m = createModel(timeline({ session: { id: 's', duration_s: 5 } }));
  assert.equal(m.duration, 10);
  const e = createModel(timeline({ session: {}, events: [{ t: 33, doc_id: 'a', kind: 'k' }] }));
  assert.equal(e.duration, 33);
});

test('stations fall back to STATIONS, or use the timeline list', () => {
  assert.equal(createModel(timeline()).stations, STATIONS);
  const custom = [{ id: 'x', label: 'x', order: 0 }];
  assert.deepEqual(createModel(timeline({ stations: custom })).stations, custom);
});

test('empty and garbage timelines never throw', () => {
  for (const tl of [undefined, null, 5, 'x', [], {}, { segments: 'no', events: {}, entities: 7, scores: null }]) {
    const m = createModel(tl);
    assert.equal(m.duration, 0);
    assert.deepEqual(m.stateAt(1).docs, []);
    assert.deepEqual(m.eventsBetween(0, 1), []);
    assert.deepEqual(m.eventsUpTo(1), []);
    assert.deepEqual(m.runningTotalsAt(1), { tokens: 0, cost_usd: 0, llm_calls: 0 });
  }
  const m = createModel({
    session: { duration_s: 'nope' },
    entities: [null, 3, { doc_id: 'a' }, { filename: 'no id' }],
    segments: [null, { doc_id: 'a', t0: 'x' }, { station: 's' }, { doc_id: 'a', station: 'intake', t0: 1, t1: 2 }],
    events: [{ t: 'x' }, null],
    generations: [{ doc_id: 'a', t0: NaN }, 4],
  });
  assert.equal(m.entities.length, 1);
  assert.equal(m.segmentsFor('a').length, 1);
  assert.equal(byId(m.stateAt(1.5), 'a').station, 'intake');
  assert.equal(m.stateAt(NaN).t, 0);
});

test('hostile strings pass through verbatim', () => {
  const m = createModel(timeline());
  assert.equal(byId(m.stateAt(2), 'a').filename, HOSTILE);
  assert.equal(m.docAt('a', 100).filename, HOSTILE);
  const h = createModel(timeline({ events: [{ t: 1, doc_id: 'a', kind: HOSTILE, payload: { reason: HOSTILE } }] }));
  assert.equal(h.eventsUpTo(1)[0].kind, HOSTILE);
  assert.equal(h.eventsUpTo(1)[0].payload.reason, HOSTILE);
});

test('input timeline is not mutated or reordered', () => {
  const tl = timeline({ events: [{ t: 5, doc_id: 'a', kind: 'late' }, { t: 1, doc_id: 'a', kind: 'early' }] });
  const m = createModel(tl);
  assert.equal(tl.events[0].kind, 'late');
  assert.equal(m.eventsUpTo(9)[0].kind, 'early');
});

test('half-open segment end: a doc leaves a station exactly at t1', () => {
  const m = createModel({ ...timeline(), segments: [{ doc_id: 'a', node: 'sort', station: 'sorter', t0: 2, t1: 4, status: 'ok' }], entities: [{ doc_id: 'a', filename: 'a', t_start: 1, t_end: null }] });
  assert.equal(byId(m.stateAt(3.999), 'a').status, 'ok');
  assert.equal(byId(m.stateAt(4), 'a').status, 'idle');
});

test('eventsForDoc filters by doc and keeps the newest', () => {
  const m = createModel(timeline());
  assert.deepEqual(m.eventsForDoc('a', 100, 2).map((e) => e.kind), ['retry', 'done']);
  assert.deepEqual(m.eventsForDoc('a', 5, 10).map((e) => e.kind), ['gate', 'retry']);
  assert.deepEqual(m.eventsForDoc('nope', 100), []);
});

test('non-primitive doc ids do not throw', () => {
  const tl = timeline();
  tl.entities.push({ doc_id: { toString: 1 }, filename: { toString: 1 } });
  tl.events.push({ t: 1, doc_id: { toString: 1 }, kind: 'x' });
  const m = createModel(tl);
  assert.doesNotThrow(() => m.stateAt(5));
  assert.doesNotThrow(() => m.eventsForDoc({ toString: 1 }, 5));
});

test('outcome fields appear only at the end time; identity from the start', () => {
  const m = createModel(
    timeline({
      entities: [
        { doc_id: 'a', filename: 'a.pdf', t_start: 1, t_end: 12, final_status: 'archived', final_stage: 'archive', verdict: 'CORRECT', failure_class: 'bad', review_causes: ['low_conf'], totals: { tokens: 9 } },
        { doc_id: 'c', filename: 'c.pdf', t_start: 4, t_end: null },
      ],
    }),
  );
  const before = byId(m.stateAt(10), 'a');
  assert.equal(before.finished, false);
  assert.equal(before.final_status, null);
  assert.equal(before.final_stage, null);
  assert.equal(before.verdict, null);
  assert.equal(byId(m.stateAt(3), 'c').filename, '');
  assert.equal(byId(m.stateAt(5), 'c').filename, 'c.pdf');
  const early = m.docAt('a', 10);
  assert.equal(early.filename, 'a.pdf');
  for (const k of ['final_status', 'final_stage', 'verdict', 'failure_class', 'review_causes', 'totals']) {
    assert.equal(k in early, false, k);
  }
  const after = m.docAt('a', 12);
  assert.equal(after.verdict, 'CORRECT');
  assert.equal(after.failure_class, 'bad');
  assert.deepEqual(after.review_causes, ['low_conf']);
  assert.equal(byId(m.stateAt(12), 'a').final_status, 'archived');
  assert.equal(m.docAt('a').filename, '', 'no time given: no identity before the start, no outcome');
});

test('firstPassAt counts only documents finished by t, with the rollup rule', () => {
  const m = createModel({
    session: { id: 's', duration_s: 30 },
    entities: [
      { doc_id: 'p', t_start: 0, t_end: 5, final_status: 'archived' },
      { doc_id: 'q', t_start: 0, t_end: 8, final_status: 'archived' },
      { doc_id: 'r', t_start: 0, t_end: 20, final_status: 'archived' },
    ],
    segments: [{ doc_id: 'q', node: 'extract', station: 'specialist', t0: 1, t1: 2, attempt: 2, retry_kind: 'llm', status: 'ok' }],
    scores: [{ doc_id: 'r', name: 'success_rate', value: 0, t: 15 }],
    rollups: { first_pass_rate: 0.9 },
  });
  assert.equal(m.firstPassAt(4), null);
  assert.equal(m.firstPassAt(5), 1);
  assert.equal(m.firstPassAt(8), 0.5);
  assert.equal(m.firstPassAt(14), 0.5);
  assert.equal(m.firstPassAt(20), 1 / 3);
});

test('firstPassAt ignores a score that arrives after the document finished', () => {
  const m = createModel({
    session: { id: 's', duration_s: 30 },
    entities: [{ doc_id: 's', t_start: 0, t_end: 6, final_status: 'archived' }],
    segments: [],
    scores: [{ doc_id: 's', name: 'success_rate', value: 0, t: 9 }],
  });
  assert.equal(m.firstPassAt(6), 1, 'the failing score is not visible until t=9');
  assert.equal(m.firstPassAt(9), 0);
});
