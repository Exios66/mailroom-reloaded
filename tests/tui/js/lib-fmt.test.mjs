import { test } from 'node:test';
import assert from 'node:assert/strict';
import { bytes, duration, usd } from '../../../src/mailroom_reloaded/api/tui/lib/fmt.js';

test('bytes uses binary units', () => {
  assert.equal(bytes(0), '0 B');
  assert.equal(bytes(1023), '1023 B');
  assert.equal(bytes(1536), '1.5 KiB');
  assert.equal(bytes(5 * 2 ** 30), '5.0 GiB');
  assert.equal(bytes(2 ** 60), '1048576.0 TiB');
});

test('durations scale from ms to days', () => {
  assert.equal(duration(0), '0 ms');
  assert.equal(duration(850), '850 ms');
  assert.equal(duration(1234), '1.2 s');
  assert.equal(duration(185_000), '3m 05s');
  assert.equal(duration(2 * 3600_000 + 3 * 60_000), '2h 03m');
  assert.equal(duration(28 * 3600_000), '1d 04h');
});

test('usd keeps small costs visible', () => {
  assert.equal(usd(0), '$0.0000');
  assert.equal(usd(0.00042), '$0.0004');
  assert.equal(usd(12.345), '$12.35');
  assert.equal(usd(-1.5), '-$1.50');
});

test('non-finite or invalid input renders a dash and never throws', () => {
  for (const v of [undefined, null, NaN, Infinity, '12', {}, -1]) {
    assert.equal(bytes(v), '—');
    assert.equal(duration(v), '—');
  }
  for (const v of [undefined, null, NaN, Infinity, '12', {}]) assert.equal(usd(v), '—');
});
