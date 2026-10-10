import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderTable, cell } from '../../../src/mailroom_reloaded/api/tui/lib/table.js';

const COLS = [
  { key: 'name', label: 'component' },
  { key: 'status' },
  { key: 'latency_ms', label: 'ms', align: 'right' },
];

test('renders a header, a rule and aligned rows', () => {
  const lines = renderTable(
    [
      { name: 'db', status: 'ok', latency_ms: 3 },
      { name: 'phoenix', status: 'down', latency_ms: 2001 },
    ],
    COLS,
  );
  assert.deepEqual(lines, [
    'component  status    ms',
    '─────────  ──────  ────',
    'db         ok         3',
    'phoenix    down    2001',
  ]);
});

test('cells are clamped to max with an ellipsis', () => {
  const [, , row] = renderTable([{ name: 'abcdefghij' }], [{ key: 'name', max: 5 }]);
  assert.equal(row, 'abcd…');
});

test('control and bidi characters become spaces; missing values are dashes', () => {
  assert.equal(cell('a‮b\u0000c'), 'a b c');
  assert.equal(cell(null), '—');
  assert.equal(cell(''), '—');
  assert.equal(cell({ a: 1 }), '{"a":1}');
  const lines = renderTable([{ name: 'x​y' }], [{ key: 'name' }]);
  assert.doesNotMatch(lines.join('\n'), /​/);
});

test('format is applied and a throwing format degrades to a dash', () => {
  const lines = renderTable([{ v: 2 }], [
    { key: 'v', format: (v) => v * 10 },
    { key: 'v', label: 'bad', format: () => { throw new Error('x'); } },
  ], { header: false });
  assert.deepEqual(lines, ['20  —']);
});

test('no columns or junk rows render safely', () => {
  assert.deepEqual(renderTable([{ a: 1 }], []), []);
  assert.deepEqual(renderTable(null, null), []);
  assert.equal(renderTable([null, 3, 'x', { a: 1 }], [{ key: 'a' }]).length, 3);
});
