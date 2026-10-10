import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderFrame, truncate, bar, fmtTime } from '../../../src/mailroom_reloaded/api/tui/replay/grid.js';
import { createModel } from '../../../src/mailroom_reloaded/api/tui/replay/model.js';

const HOSTILE = '<img src=x onerror=alert(1)>';
const flat = (rows) => rows.map((r) => r.map((s) => s[0]).join(''));
const len = (s) => Array.from(s).length;

function fakeModel(over = {}) {
  return {
    duration: 60,
    session: { id: 'run:r1', source: 'spans', approx: false, data_pruned: false },
    stations: [
      { id: 'intake', label: 'Intake', kind: 'main', color_token: 'info' },
      { id: 'retry', label: 'Retry', kind: 'detour', color_token: 'warn' },
      { id: 'bay', label: 'Bay', kind: 'bay', color_token: 'weird' },
    ],
    // First-pass comes from the time-gated firstPassAt, not from the whole-run rollups.
    firstPassAt: () => 0.5,
    eventsBetween: () => [{ t: 10 }, { t: 50 }],
    eventsUpTo: () => [{ t: 5, doc_id: 'd1', kind: 'gate', station: 'intake' }],
    eventsForDoc: () => [{ t: 5, doc_id: 'd1', kind: 'gate', station: 'intake' }],
    runningTotalsAt: () => ({ tokens: 1500, cost_usd: 0.0123, llm_calls: 4 }),
    generationsFor: () => [{ role: 'extract', model: 'm-1', prompt_tokens: 10, completion_tokens: 5, cost_usd: 0.001 }],
    docAt: () => ({ failure_class: 'bad', review_causes: ['low_conf'] }),
    ...over,
  };
}
const st = {
  t: 12,
  docs: [
    { doc_id: 'd1', filename: 'a.pdf', station: 'intake', status: 'ok', attempt: 1, finished: false },
    { doc_id: 'd2', filename: 'b.pdf', station: 'retry', status: 'failed', attempt: 2, finished: false },
    { doc_id: 'd3', filename: 'c.pdf', station: 'intake', status: 'ok', attempt: 1, finished: true, final_status: 'archived' },
  ],
  counts: { intake: 2, retry: 1 },
  done: 1,
  failed: 1,
  active: 1,
};
const clock = { t: 12, playing: true, speed: 2, duration: 60, ended: false };

test('helpers', () => {
  assert.equal(truncate('abcdef', 4), 'abc…');
  assert.equal(truncate('abc', 4), 'abc');
  assert.equal(truncate('a\nb', 5), 'a b');
  assert.equal(truncate('abc', 0), '');
  assert.equal(bar(0.5, 10), '•••••·····');
  assert.equal(bar(5, 4), '••••');
  assert.equal(bar(NaN, 3), '···');
  assert.equal(fmtTime(75.34), '01:15.3');
  assert.equal(fmtTime(-1), '00:00.0');
  assert.equal(fmtTime(undefined), '00:00.0');
});

test('frame has header, track, scrub, metrics and footer within bounds', () => {
  const rows = flat(renderFrame({ model: fakeModel(), st, clock, sel: 1, cols: 100, rows: 30 }));
  assert.match(rows[0], /replay run:r1 .*>.*00:12\.0 \/ 01:00\.0.*x2.*spans/);
  assert.match(rows[1], /Intake/);
  assert.match(rows[2], /^ {3}Retry/);
  assert.match(rows[3], /^ {5}Bay/);
  assert.ok(rows.some((r) => r.includes('┼') && r.includes('█')));
  assert.ok(rows.some((r) => /done 1.*failed 1.*active 1.*tok 1\.5k.*\$0\.0123.*calls 4.*first-pass 50%/.test(r)));
  assert.match(rows.at(-1), /q quit/);
  assert.ok(rows.length <= 30);
  rows.forEach((r) => assert.ok(len(r) <= 100, r));
});

test('markers: failed, finished, selection class', () => {
  const rows = renderFrame({ model: fakeModel(), st, clock, sel: 1, cols: 100, rows: 30 });
  const text = flat(rows).join('\n');
  assert.ok(text.includes('◆'));
  assert.ok(text.includes('✓'));
  const selSeg = rows.flat().find((s) => s[1] === 'sel');
  assert.equal(selSeg[0], '◆');
  assert.ok(!rows.flat().some((s) => s[1] === 'sel') === false);
  const none = renderFrame({ model: fakeModel(), st, clock, sel: -1, cols: 100, rows: 30 });
  assert.ok(!none.flat().some((s) => s[1] === 'sel'));
});

test('hostile and long strings stay literal and fit', () => {
  const long = HOSTILE.repeat(20);
  const model = fakeModel({
    session: { id: long, source: 'audit', approx: true, data_pruned: true },
    stations: [{ id: 's', label: long, kind: 'bay', color_token: long }],
    docAt: () => ({ failure_class: long, review_causes: [long, long], filename: long }),
    generationsFor: () => [{ role: long, model: long, prompt_tokens: 1e9, cost_usd: 1e9 }],
    eventsUpTo: () => [{ t: 1, doc_id: 'x', kind: long, station: long }],
  });
  const s2 = { ...st, docs: [{ doc_id: 'x', filename: long, station: long, status: long, verdict: long, final_stage: long }], counts: { s: 99 } };
  for (const cols of [60, 80, 160, 500, 5]) {
    for (const panel of ['none', 'inspector', 'ledger']) {
      const rows = flat(renderFrame({ model, st: s2, clock, sel: 0, cols, rows: 40, panel, ledger: { entries: [{ seq: long, kind: long, entry_hash: long }], verify: { ok: false, broken_at: long } } }));
      const c = Math.min(160, Math.max(60, cols));
      rows.forEach((r) => assert.ok(len(r) <= c, `${cols}/${panel}: ${len(r)}`));
    }
  }
  const out = flat(renderFrame({ model, st: s2, clock, sel: 0, cols: 160, rows: 40, panel: 'inspector' })).join('\n');
  assert.ok(out.includes('<img src=x'));
  assert.ok(out.includes('data pruned'));
  assert.ok(out.includes('~approx'));
});

test('empty and malformed input never throws', () => {
  assert.doesNotThrow(() => renderFrame());
  assert.doesNotThrow(() => renderFrame({ model: {}, st: {}, clock: {}, cols: 'x', rows: null, panel: 'inspector' }));
  const rows = flat(renderFrame({ model: fakeModel({ stations: [] }), st: { t: 0, docs: [], counts: {} }, clock: { t: 0, duration: 0, speed: 1, playing: false }, cols: 80, rows: 20 }));
  assert.ok(rows.some((r) => r.includes('no stations')));
  assert.match(rows[0], /\|\|/);
  const throwing = fakeModel({ runningTotalsAt: () => { throw new Error('x'); }, eventsBetween: () => { throw new Error('x'); } });
  assert.doesNotThrow(() => renderFrame({ model: throwing, st, clock, cols: 80, rows: 20 }));
});

test('panel switch: inspector vs ledger vs none', () => {
  const m = fakeModel();
  const none = flat(renderFrame({ model: m, st, clock, sel: 0, cols: 100, rows: 30 })).join('\n');
  assert.ok(!none.includes('inspector') && !none.includes('ledger ─'));
  const insp = flat(renderFrame({ model: m, st, clock, sel: 0, cols: 100, rows: 30, panel: 'inspector' })).join('\n');
  assert.match(insp, /inspector/);
  assert.match(insp, /a\.pdf/);
  // a.pdf is still in progress at t=12: its failure and causes must not show yet.
  assert.match(insp, /final +in progress/);
  assert.doesNotMatch(insp, /failure bad/);
  assert.doesNotMatch(insp, /causes low_conf/);
  const finished = flat(renderFrame({ model: m, st, clock, sel: 2, cols: 100, rows: 30, panel: 'inspector' })).join('\n');
  assert.match(finished, /final +archived/);
  assert.match(finished, /failure bad/);
  assert.match(finished, /causes low_conf/);
  assert.match(insp, /extract\s+m-1/);
  assert.match(insp, /gate/);
  const nosel = flat(renderFrame({ model: m, st, clock, sel: -1, cols: 100, rows: 30, panel: 'inspector' })).join('\n');
  assert.match(nosel, /no document selected/);
  const entries = [{ seq: 3, kind: 'doc_closed', entry_hash: 'abcdef0123456789' }];
  const led = (ledger) => flat(renderFrame({ model: m, st, clock, cols: 100, rows: 30, panel: 'ledger', ledger })).join('\n');
  assert.match(led({ entries, verify: { ok: true, count: 3 } }), /#3 +doc_closed +abcdef012345[\s\S]*chain ok — 3 entries/);
  assert.match(led({ entries, verify: { ok: false, broken_at: 2 } }), /chain BROKEN at seq 2/);
  assert.match(led({ entries, verify: null }), /not verified/);
  const unknown = led({ entries: [], verify: { ok: false, broken_at: null, detail: 'unknown run' } });
  assert.match(unknown, /verify failed: unknown run/);
  assert.ok(!/BROKEN/.test(unknown));
  assert.match(led(null), /loading…/);
  assert.match(led({ unavailable: true }), /unavailable/);
});

test('output never exceeds the row budget; sizes are clamped', () => {
  const stations = Array.from({ length: 40 }, (_, i) => ({ id: `s${i}`, label: `S${i}`, kind: 'main' }));
  const m = fakeModel({ stations });
  assert.ok(renderFrame({ model: m, st, clock, cols: 100, rows: 30 }).length <= 30);
  assert.ok(renderFrame({ model: m, st, clock, cols: 100, rows: 30, panel: 'inspector', sel: 0 }).length <= 30);
  assert.ok(renderFrame({ model: m, st, clock, cols: 100, rows: 3 }).length <= 14);
});

test('bidi overrides and zero-width characters are neutralised', () => {
  const evil = 'a\u202Eb\u2066c\u200Fd\u061Ce\u200Bf\u2028g';
  const t = truncate(evil, 40);
  assert.equal(t, 'a b c d e f g');
  assert.ok(!/[\u202a-\u202e\u2066-\u2069\u200b-\u200f\u061c\u2028]/.test(t));
});

test('non-primitive fields never throw and fmtTime is bounded', () => {
  assert.doesNotThrow(() => truncate({ toString: 1 }, 20));
  assert.equal(truncate({ toString: 1 }, 20), '{"toString":1}');
  assert.equal(fmtTime(1e308), '99:59.9');
  assert.doesNotThrow(() =>
    renderFrame({
      model: fakeModel({ session: { id: { toString: 1 } }, stations: [{ id: 'x', label: { toString: 1 }, kind: 'main' }] }),
      st: { t: 0, docs: [{ doc_id: 'd', filename: { toString: 1 }, station: 'x', status: 'ok' }], counts: {} },
      clock,
      sel: 0,
      cols: 80,
      rows: 30,
      panel: 'inspector',
    }),
  );
});

test('scrub bar marks an event at t=0 and asks for the full range', () => {
  let args;
  const m = fakeModel({
    eventsBetween: (a, b) => {
      args = [a, b];
      return [{ t: 0 }];
    },
  });
  const rows = flat(renderFrame({ model: m, st, clock: { ...clock, t: 30 }, cols: 80, rows: 30 }));
  assert.equal(args[0], -Infinity);
  assert.ok(rows.some((r) => r.includes('┼')));
});

test('inspector shows the selected document events and its latest generations', () => {
  let asked;
  const gens = Array.from({ length: 6 }, (_, i) => ({ role: `r${i}`, model: 'm', prompt_tokens: 1, completion_tokens: 1, cost_usd: 0 }));
  const m = fakeModel({
    generationsFor: () => gens,
    eventsForDoc: (id, t, n) => {
      asked = [id, t, n];
      return [{ t: 5, doc_id: id, kind: 'only-for-me', station: 'intake' }];
    },
  });
  const text = flat(renderFrame({ model: m, st, clock, sel: 0, cols: 100, rows: 40, panel: 'inspector' })).join('\n');
  assert.deepEqual(asked, ['d1', 12, 3]);
  assert.match(text, /only-for-me/);
  assert.match(text, /r5/);
  assert.ok(!/ r0 /.test(text));
});

// A small run for the time-gating checks below: real model, real rows.
const LEAK_TL = {
  version: 'replay/v1',
  session: { id: 'run:leak', duration_s: 40 },
  stations: [{ id: 'sorter', label: 'sorter', kind: 'main' }],
  entities: [
    { doc_id: 'p', filename: 'p.pdf', t_start: 0, t_end: 5, final_status: 'archived', final_stage: 'archive', verdict: 'CORRECT' },
    { doc_id: 'q', filename: 'q.pdf', t_start: 0, t_end: 8, final_status: 'archived' },
    { doc_id: 'r', filename: 'r.pdf', t_start: 0, t_end: 20, final_status: 'archived' },
    { doc_id: 'late', filename: 'late.pdf', t_start: 30, t_end: 40, final_status: 'archived' },
  ],
  segments: [{ doc_id: 'q', node: 'extract', station: 'sorter', t0: 1, t1: 2, attempt: 2, retry_kind: 'llm', status: 'ok' }],
  generations: [],
  events: [],
  scores: [{ doc_id: 'r', name: 'success_rate', value: 0, t: 15 }],
  rollups: { first_pass_rate: 0.9 },
};

const metricsAt = (m, t) =>
  flat(renderFrame({ model: m, st: m.stateAt(t), clock: { t, duration: 40, speed: 1, playing: false }, cols: 160, rows: 30 })).find((r) => /docs \d+ of \d+/.test(r));

test('metrics count documents started by t, and first-pass waits for a finished document', () => {
  const m = createModel(LEAK_TL);
  const early = metricsAt(m, 4);
  assert.match(early, /docs 3 of 4 +done 0/);
  assert.match(early, /first-pass --/);
  assert.doesNotMatch(early, /90%/, 'the whole-run rollup must not show');
  const mid = metricsAt(m, 8);
  assert.match(mid, /done 2/);
  assert.match(mid, /first-pass 50%/);
  assert.match(metricsAt(m, 20), /docs 3 of 4.*first-pass 33%/);
  assert.match(metricsAt(m, 35), /docs 4 of 4/);
});

const INS_TL = {
  version: 'replay/v1',
  session: { id: 'run:ins', duration_s: 20 },
  stations: [{ id: 'sorter', label: 'sorter', kind: 'main' }],
  entities: [
    { doc_id: 'd', filename: 'd.pdf', t_start: 0, t_end: 12, final_status: 'archived', final_stage: 'archive', verdict: 'CORRECT', failure_class: 'bad_parse', review_causes: ['low_conf'] },
  ],
  segments: [{ doc_id: 'd', node: 'sort', station: 'sorter', t0: 0, t1: 4, status: 'ok' }],
  generations: [{ doc_id: 'd', span_id: 'g', role: 'extract', model: 'm-9', t0: 5, t1: 9, prompt_tokens: 7, completion_tokens: 3, cost_usd: 0.2 }],
  events: [],
  scores: [],
  rollups: {},
};
const CTRL_OR_BIDI = /[\u0000-\u001f\u202a-\u202e]/;
const inspectorAt = (m, t) =>
  flat(renderFrame({ model: m, st: m.stateAt(t), clock: { t, duration: 20, speed: 1, playing: false }, sel: 0, cols: 160, rows: 40, panel: 'inspector' })).join('\n');

test('inspector: outcome and in-flight calls show only from their end', () => {
  const m = createModel(INS_TL);
  const during = inspectorAt(m, 6);
  assert.match(during, /final +in progress/);
  for (const hidden of ['CORRECT', 'bad_parse', 'low_conf', 'archived', 'm-9']) {
    assert.doesNotMatch(during, new RegExp(hidden), hidden);
  }
  const after = inspectorAt(m, 12);
  assert.match(after, /final +archived\/archive/);
  assert.match(after, /verdict +CORRECT/);
  assert.match(after, /failure +bad_parse/);
  assert.match(after, /causes +low_conf/);
  assert.match(after, /m-9/);
});

test('hostile strings in the inspector stay literal text, and wait for the finish', () => {
  const m = createModel({
    ...INS_TL,
    entities: [
      {
        doc_id: 'd',
        filename: '<img src=x onerror=alert(1)>\u001b[2J.pdf',
        t_start: 0,
        t_end: 12,
        final_status: 'archived',
        verdict: '<b>x</b>\u0007',
        failure_class: '<script>\u202eevil',
        review_causes: ['<i>c</i>\u0000'],
      },
    ],
  });
  const during = inspectorAt(m, 6);
  assert.doesNotMatch(during, /<b>x|<script>|<i>c/);
  const after = inspectorAt(m, 12);
  for (const lit of ['<img src=x onerror=alert(1)>', '<b>x</b>', '<script>', '<i>c</i>']) assert.ok(after.includes(lit), lit);
  assert.doesNotMatch(after.replace(/\n/g, ' '), CTRL_OR_BIDI);
});
