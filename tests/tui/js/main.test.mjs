// Unit tests for the /tui boot wiring in main.js. `start()` is DOM/browser
// territory (covered by the replay browser check); `registerAll`/`MODULES` are
// pinned in plugins.test.mjs. This file pins the asset-loading fail-safe.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadText } from '../../../src/mailroom_reloaded/api/tui/main.js';

test('loadText returns the asset text', async () => {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(url);
    return { ok: true, text: async () => 'BIG BANNER' };
  };
  try {
    assert.equal(await loadText('banner.txt'), 'BIG BANNER');
    assert.deepEqual(calls, ['/tui/assets/banner.txt']);
  } finally {
    delete globalThis.fetch;
  }
});

test('loadText returns "" on a non-ok response (never throws)', async () => {
  globalThis.fetch = async () => ({ ok: false, text: async () => 'nope' });
  try {
    assert.equal(await loadText('missing.txt'), '');
  } finally {
    delete globalThis.fetch;
  }
});

test('loadText returns "" when the fetch itself throws', async () => {
  globalThis.fetch = async () => {
    throw new Error('network down');
  };
  try {
    assert.equal(await loadText('banner.txt'), '');
  } finally {
    delete globalThis.fetch;
  }
});
