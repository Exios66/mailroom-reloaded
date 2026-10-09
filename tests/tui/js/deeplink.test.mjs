import { test } from 'node:test';
import assert from 'node:assert/strict';
import { deepLinkCommand, isReplayLink } from '../../../src/mailroom_reloaded/api/tui/deeplink.js';

test('valid replay fragments become a replay command', () => {
  assert.equal(deepLinkCommand('#replay=run:r1'), 'replay run:r1');
  assert.equal(deepLinkCommand('#replay=run%3Ar1.2-x'), 'replay run:r1.2-x');
  assert.equal(deepLinkCommand('#replay=r1'), 'replay run:r1');
  assert.equal(deepLinkCommand('#replay=doc:abc123'), 'replay doc:abc123');
});

test('anything else is ignored', () => {
  for (const h of [
    '',
    '#',
    '#replay=',
    '#replay=run:',
    '#replay=run:..',
    '#replay=run:a b',
    '#replay=run:a%20--at%205',
    '#replay=run:a;ls',
    '#replay=run:a&x=1',
    '#replay=%E0%A4%A',
    '#replay=bogus:x',
    '#replay="run:a"',
    '#other=run:a',
    '#x#replay=run:a',
    null,
    undefined,
    42,
  ]) {
    assert.equal(deepLinkCommand(h), null, String(h));
  }
});

test('a flag-like id stays a positional run id, never a flag', () => {
  assert.equal(deepLinkCommand('#replay=--speed'), 'replay run:--speed');
});

test('isReplayLink flags any replay fragment, valid or not', () => {
  assert.equal(isReplayLink('#replay=run:a'), true);
  assert.equal(isReplayLink('#replay=run:a;ls'), true);
  assert.equal(isReplayLink('#other'), false);
  assert.equal(isReplayLink(undefined), false);
});
