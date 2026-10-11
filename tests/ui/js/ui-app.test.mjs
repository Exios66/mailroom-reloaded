// Unit tests for the /ui page's pure helpers (src/mailroom_reloaded/api/ui/app.js).
// These pin the security-relevant URL handling and the display logic that the
// DOM wiring in index.html depends on.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  auditSummary,
  isHttpBase,
  mergeLinks,
  runLinks,
  statusPill,
} from '../../../src/mailroom_reloaded/api/ui/app.js';

const DEFAULTS = {
  public_url: 'http://localhost:8000',
  phoenix_url: 'http://localhost:6006',
  grafana_url: 'http://localhost:3000',
  phoenix_project: 'mailroom-live',
};

// --------------------------------------------------------------- isHttpBase

test('isHttpBase accepts only plain http(s) bases', () => {
  assert.equal(isHttpBase('http://localhost:6006'), true);
  assert.equal(isHttpBase('https://obs.example.test/base'), true);
  assert.equal(isHttpBase('HTTPS://OBS.EXAMPLE.TEST'), true);
});

test('isHttpBase rejects hostile and non-URL values', () => {
  for (const bad of [
    'javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'ftp://host',
    '//host/path',
    'http://user:pass@host', // embedded credentials
    'http://host /path', // whitespace
    '',
    null,
    42,
  ]) {
    assert.equal(isHttpBase(bad), false, `should reject ${JSON.stringify(bad)}`);
  }
});

// --------------------------------------------------------------- mergeLinks

test('mergeLinks drops javascript:/data: and non-http values, keeping defaults', () => {
  const merged = mergeLinks(DEFAULTS, {
    public_url: 'javascript:alert(1)',
    phoenix_url: 'data:text/html,x',
    grafana_url: 'ftp://host',
    phoenix_project: 'proj-x',
  });
  assert.equal(merged.public_url, DEFAULTS.public_url);
  assert.equal(merged.phoenix_url, DEFAULTS.phoenix_url);
  assert.equal(merged.grafana_url, DEFAULTS.grafana_url);
  assert.equal(merged.phoenix_project, 'proj-x'); // a name, not a URL
});

test('mergeLinks strips trailing slashes and keeps valid http(s) bases', () => {
  const merged = mergeLinks(DEFAULTS, {
    phoenix_url: 'https://phoenix.example.test/',
    grafana_url: 'http://grafana.example.test///',
  });
  assert.equal(merged.phoenix_url, 'https://phoenix.example.test');
  assert.equal(merged.grafana_url, 'http://grafana.example.test');
});

test('mergeLinks ignores non-string values and a non-object body', () => {
  assert.deepEqual(mergeLinks(DEFAULTS, null), DEFAULTS);
  assert.deepEqual(mergeLinks(DEFAULTS, 'nope'), DEFAULTS);
  const merged = mergeLinks(DEFAULTS, { phoenix_url: 42, grafana_url: '' });
  assert.equal(merged.phoenix_url, DEFAULTS.phoenix_url);
  assert.equal(merged.grafana_url, DEFAULTS.grafana_url);
});

test('mergeLinks never mutates the defaults object', () => {
  const before = JSON.stringify(DEFAULTS);
  mergeLinks(DEFAULTS, { phoenix_url: 'https://x.test/' });
  assert.equal(JSON.stringify(DEFAULTS), before);
});

// --------------------------------------------------------------- statusPill

test('statusPill maps known statuses to the right class', () => {
  assert.deepEqual(statusPill('archived'), { status: 'archived', className: 'pill ok' });
  assert.deepEqual(statusPill('failed'), { status: 'failed', className: 'pill bad' });
  assert.deepEqual(statusPill('processing'), { status: 'processing', className: 'pill muted' });
  assert.deepEqual(statusPill('parked'), { status: 'parked', className: 'pill muted' });
});

test('statusPill shows an unknown status as "?" with a muted class', () => {
  assert.deepEqual(statusPill('bogus'), { status: '?', className: 'pill muted' });
  assert.deepEqual(statusPill(undefined), { status: '?', className: 'pill muted' });
});

// --------------------------------------------------------------- runLinks

test('runLinks builds encoded replay/grafana/phoenix hrefs', () => {
  const links = runLinks(DEFAULTS, 'run:7e57d0c0ffee');
  assert.equal(links.replay, '/tui#replay=run:run%3A7e57d0c0ffee');
  assert.equal(
    links.grafana,
    'http://localhost:3000/d/mailroom-quality?var-run_id=run%3A7e57d0c0ffee'
  );
  assert.equal(links.phoenix, DEFAULTS.phoenix_url);
});

// --------------------------------------------------------------- auditSummary

test('auditSummary reports a verified chain', () => {
  assert.deepEqual(auditSummary({ ok: true }, 3), { text: 'chain OK', className: 'ok', entries: 3 });
});

test('auditSummary reports a broken chain with its index', () => {
  assert.deepEqual(auditSummary({ ok: false, broken_at: 7 }, 9), {
    text: 'chain BROKEN at 7',
    className: 'bad',
    entries: 9,
  });
});

test('auditSummary treats a missing chain as broken, never OK', () => {
  const summary = auditSummary(null, 0);
  assert.equal(summary.className, 'bad');
  assert.match(summary.text, /^chain BROKEN/);
});
