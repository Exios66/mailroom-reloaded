import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createRegistry } from '../../../src/mailroom_reloaded/api/tui/engine.js';
import { createModuleHost } from '../../../src/mailroom_reloaded/api/tui/plugins.js';
import { registerPipeline } from '../../../src/mailroom_reloaded/api/tui/commands/pipeline.js';
import { registerReview } from '../../../src/mailroom_reloaded/api/tui/commands/review.js';

const HOSTILE = '<img src=x onerror="globalThis.xss=true"><script>globalThis.xss=true</script>';
const PRIVATE_TEXT = 'operator-only-document-text';

function fixture() {
  return {
    doc_id: 'doc1', status: 'parked',
    manifest: {
      filename: HOSTILE, updated_at: '2026-10-22T19:08:11Z',
      state: {
        text: `${PRIVATE_TEXT}\n${HOSTILE}`,
        sort: { doc_type: 'invoice' },
        route_trail: ['ingest', 'sort', 'human_review'],
        verdict: {
          label: 'partial', score: 0,
          field_findings: [{ field: HOSTILE, verdict: 'wrong', rationale: HOSTILE }],
        },
        grade: { overall: 0.4, fields: [{ field: 'amount', verdict: 'missing', rationale: 'not on page' }] },
        usage: { api_key: 'do-not-render-this-secret' },
      },
    },
    report: { rubric_scores: { completeness: 0, correctness: 0.7 }, prompts: 'never-render-prompt' },
    catalog: { filename: 'fallback.txt', doc_type: 'fallback' },
  };
}

function setup(testContext, response = fixture(), { status = 200, fetchImpl, token = 'test-token' } = {}) {
  const calls = [];
  const output = [];
  const controller = new AbortController();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return fetchImpl ? fetchImpl(url, options) : new Response(JSON.stringify(response), { status });
  };
  testContext.after(() => { globalThis.fetch = originalFetch; });
  const ctx = {
    api: { authHeaders: () => token ? { Authorization: `Bearer ${token}` } : {} },
    signal: () => controller.signal,
    out: {
      line: (text) => output.push(text),
      pre: (text) => output.push(text),
      kv: (pairs) => output.push(pairs.flat().join('\n')),
      table: (headers, rows) => output.push([headers, ...rows].flat().join('\n')),
    },
  };
  const registry = createRegistry();
  registerReview(registry);
  return {
    calls, output, controller, ctx, registry,
    run: (args = [], flags = {}) => registry.get('review').run(ctx, args, flags),
    text: () => output.join('\n'),
  };
}

test('decorates the real pipeline review through plugin staging, idempotently without duplicate names', () => {
  const registry = createRegistry();
  registerPipeline(registry);
  const previous = registry.get('review');
  const names = registry.names();
  const host = createModuleHost(registry);
  host.registerModule({ id: 'review-details', commands: registerReview });
  registerReview(registry);
  assert.equal(registry.get('review'), previous);
  assert.deepEqual(registry.names(), names);
  assert.match(previous.usage, /review queue/);
  assert.match(previous.man, /authenticated/);
  assert.deepEqual(host.modules()[0].commands, []);
});

test('reports immutable legacy specs as an integration need without partial mutation', () => {
  const registry = createRegistry();
  const spec = Object.freeze({ name: 'review', run() {} });
  registry.register(spec);
  assert.throws(() => registerReview(registry), /integration requires a mutable command spec/);
  assert.equal(registry.get('review'), spec);
});

test('bare review and review queue preserve parked listing endpoint and authorization', async (context) => {
  const harness = setup(context, { documents: [{ doc_id: 'doc1', filename: HOSTILE, doc_type: 'invoice', status: 'parked' }] });
  await harness.run();
  await harness.run(['queue']);
  assert.equal(harness.calls.length, 2);
  for (const call of harness.calls) {
    assert.equal(call.url, '/v1/documents?status=parked');
    assert.equal(call.options.method, 'GET');
    assert.equal(call.options.headers.Authorization, 'Bearer test-token');
  }
  assert.match(harness.text(), /API page; not the total queue/);
  assert.ok(harness.text().includes(HOSTILE));
});

test('shows only real metadata, transcript, trail, verdict, findings and separately labelled scores', async (context) => {
  const harness = setup(context);
  await harness.run(['show', 'doc1']);
  assert.equal(harness.calls[0].url, '/v1/documents/doc1');
  const text = harness.text();
  for (const expected of [PRIVATE_TEXT, HOSTILE, 'invoice', 'ingest -> sort -> human_review', 'partial',
    'verdict score (recorded, not a rubric)\n0', 'grade overall (recorded, not a rubric)\n0.4',
    'amount', 'not on page', 'completeness\n0', 'correctness\n0.7']) assert.ok(text.includes(expected), expected);
  assert.doesNotMatch(text, /do-not-render-this-secret|never-render-prompt/);
});

test('catalog-only document explicitly marks absent transcript, trail, verdict, findings and rubric', async (context) => {
  const harness = setup(context, { doc_id: 'doc1', status: 'parked', manifest: null, report: null,
    catalog: { filename: 'catalog.txt', doc_type: 'invoice' } });
  await harness.run(['show', 'doc1']);
  assert.match(harness.text(), /catalog.txt/);
  assert.match(harness.text(), /absent — no document transcript recorded/);
  assert.match(harness.text(), /absent — no route trail recorded/);
  assert.match(harness.text(), /label\nabsent/);
  assert.match(harness.text(), /no field findings recorded/);
  assert.match(harness.text(), /no rubric scores recorded; no scores inferred/);
});

test('report verdict and route fallback are supported without pretending verdict score is a rubric', async (context) => {
  const harness = setup(context, { manifest: { state: {} }, report: {
    verdict: { label: 'complete', score: 1, field_findings: [{ field: 'total', verdict: 'correct' }] },
    route_trail: ['report_catalog_archive'],
  } });
  await harness.run(['show', 'doc1']);
  assert.match(harness.text(), /complete/);
  assert.match(harness.text(), /report_catalog_archive/);
  assert.match(harness.text(), /total\ncorrect/);
  assert.match(harness.text(), /no rubric scores recorded/);
});

test('rejects traversal, encoded separators, controls, excessive IDs, extra args and flags without fetching', async (context) => {
  const harness = setup(context);
  for (const id of ['..', '../secret', 'a/b', 'a\\b', '%2e%2e', 'a?token=x', 'a#x', '\u001b', '', 'a'.repeat(129), HOSTILE]) {
    await harness.run(['show', id]);
  }
  for (const args of [['show'], ['queue', 'extra'], ['show', 'doc1', 'extra'], ['unknown']]) await harness.run(args);
  await harness.run([], { token: 'secret' });
  assert.equal(harness.calls.length, 0);
  assert.doesNotMatch(harness.text(), /token=x|secret/);
});

test('handles malformed dicts and arrays without serializing nested objects', async (context) => {
  const harness = setup(context, {
    doc_id: {}, status: [], manifest: { filename: { secret: 'hidden' }, state: {
      text: { secret: 'hidden' }, route_trail: [null, {}, ['hidden']],
      verdict: { label: {}, score: '0.9', field_findings: [null, [], { field: {}, rationale: {} }] },
      grade: [],
    } }, report: { rubric_scores: { bad: {}, nested: ['hidden'], string: '0.8' } },
  });
  await harness.run(['show', 'doc1']);
  assert.doesNotMatch(harness.text(), /hidden|\[object Object\]|0\.9|0\.8/);
  assert.match(harness.text(), /absent/);
});

test('bounds huge transcript, lines, fields, trail, rubric, metadata and queue', async (context) => {
  const payload = fixture();
  const state = payload.manifest.state;
  state.text = 'x'.repeat(1000000);
  payload.manifest.filename = 'y'.repeat(1000000);
  state.route_trail = Array(1000).fill('route'.repeat(1000));
  state.verdict.field_findings = Array(1000).fill({ field: 'f'.repeat(10000), verdict: 'v'.repeat(10000), rationale: 'r'.repeat(10000) });
  payload.report.rubric_scores = Object.fromEntries(Array.from({ length: 1000 }, (_, index) => [`rubric-${index}`, index]));
  const harness = setup(context, payload);
  await harness.run(['show', 'doc1']);
  assert.ok(harness.text().length < 50000);
  assert.match(harness.text(), /truncated/);
  assert.doesNotMatch(harness.text(), /rubric-30/);
  state.text = Array(1000).fill('document line').join('\n');
  harness.output.length = 0;
  await harness.run(['show', 'doc1']);
  assert.equal((harness.text().match(/document line/g) || []).length, 200);
  globalThis.fetch = async () => new Response(JSON.stringify({ documents: Array(1000).fill({ filename: 'q'.repeat(1000) }) }));
  harness.output.length = 0;
  await harness.run(['queue']);
  assert.ok(harness.text().length < 20000);
  assert.match(harness.text(), /queue output truncated/);
});

test('removes terminal control and bidi characters while keeping transcript newlines', async (context) => {
  const payload = fixture();
  payload.manifest.filename = 'bad\u001b[31m\u202ename\nfilename';
  payload.manifest.state.text = 'first\nsecond\u0000\u202e';
  const harness = setup(context, payload);
  await harness.run(['show', 'doc1']);
  assert.doesNotMatch(harness.text(), /[\u001b\u202e\u0000]/);
  assert.match(harness.text(), /first\nsecond/);
  assert.match(harness.text(), /name filename/);
});

for (const [status, expected] of [[401, /401 — run 'auth <token>'/], [403, /403 — not allowed/],
  [404, /no such document doc1/], [500, /request failed/]]) {
  test(`HTTP ${status} prints safe failure, never document data`, async (context) => {
    const harness = setup(context, fixture(), { status, token: status === 401 ? '' : 'test-token' });
    await harness.run(['show', 'doc1']);
    assert.match(harness.text(), expected);
    assert.doesNotMatch(harness.text(), new RegExp(PRIVATE_TEXT));
    if (status === 401) assert.equal(harness.calls[0].options.headers.Authorization, undefined);
  });
}

test('offline error details never reach operator output', async (context) => {
  const harness = setup(context, null, { fetchImpl: async () => { throw new Error(PRIVATE_TEXT); } });
  await harness.run(['show', 'doc1']);
  assert.equal(harness.text(), 'review: api unreachable — mailroom closed');
});

test('invalid JSON, malformed queue, and empty queue are explicit', async (context) => {
  const harness = setup(context, null, { fetchImpl: async () => new Response('not JSON') });
  await harness.run();
  assert.match(harness.text(), /bad response from the api/);
  for (const response of [null, [], {}, { documents: {} }]) {
    globalThis.fetch = async () => new Response(JSON.stringify(response));
    harness.output.length = 0;
    await harness.run(['queue']);
    assert.match(harness.text(), /bad response from the api/);
  }
  globalThis.fetch = async () => new Response('{"documents":[]}');
  harness.output.length = 0;
  await harness.run();
  assert.equal(harness.text(), 'no parked documents');
});

test('pre-cancellation makes no request', async (context) => {
  const harness = setup(context);
  harness.controller.abort();
  await harness.run(['show', 'doc1']);
  assert.equal(harness.calls.length, 0);
  assert.equal(harness.text(), '');
});

test('in-flight cancellation aborts fetch and stays silent', async (context) => {
  const harness = setup(context, null, { fetchImpl: async (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error(PRIVATE_TEXT)), { once: true });
  }) });
  const pending = harness.run(['show', 'doc1']);
  harness.controller.abort();
  await pending;
  assert.equal(harness.calls[0].options.signal.aborted, true);
  assert.equal(harness.text(), '');
});

test('cancellation suppresses a late successful response even if fetch ignores abort', async (context) => {
  let release;
  const harness = setup(context, null, { fetchImpl: () => new Promise((resolve) => { release = resolve; }) });
  const pending = harness.run(['show', 'doc1']);
  harness.controller.abort();
  release(new Response(JSON.stringify(fixture())));
  await pending;
  assert.equal(harness.text(), '');
});

test('actual operator DOM renders seeded XSS as text and keeps document data out of console and requests', {
  skip: !process.env.PLAYWRIGHT_MODULE && 'set PLAYWRIGHT_MODULE to run actual Chromium DOM proof',
}, async (context) => {
  const { chromium } = await import(process.env.PLAYWRIGHT_MODULE);
  const sources = new Map();
  for (const path of ['commands/review.js', 'lib/http.js', 'terminal.js', 'engine.js']) {
    sources.set(`/tui/${path}`, await readFile(new URL(`../../../src/mailroom_reloaded/api/tui/${path}`, import.meta.url)));
  }
  const server = createServer((request, response) => {
    if (request.url === '/') {
      response.writeHead(200, { 'Content-Type': 'text/html' });
      response.end('<!doctype html><html><body></body></html>');
      return;
    }
    const source = sources.get(request.url);
    response.writeHead(source ? 200 : 404, { 'Content-Type': 'text/javascript' });
    response.end(source || '');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  context.after(() => new Promise((resolve) => server.close(resolve)));
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
  context.after(() => browser.close());
  const page = await browser.newPage();
  const messages = [];
  const requests = [];
  page.on('console', (message) => messages.push(message.text()));
  page.on('pageerror', (error) => messages.push(error.message));
  page.on('request', (request) => requests.push(`${request.url()} ${request.postData() || ''}`));
  await page.route('**/v1/documents/doc1', (route) => {
    const authenticated = route.request().headers().authorization === 'Bearer test-token';
    return route.fulfill({ status: authenticated ? 200 : 401, contentType: 'application/json', body: JSON.stringify(fixture()) });
  });
  await page.route('**/v1/documents?status=parked', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ documents: [{ doc_id: 'doc1', filename: HOSTILE, doc_type: HOSTILE, status: 'parked' }] }),
  }));
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  const result = await page.evaluate(async () => {
    const { createRegistry } = await import('/tui/engine.js');
    const { registerReview } = await import('/tui/commands/review.js');
    const { renderLine, renderPre, renderTable, renderKv } = await import('/tui/terminal.js');
    const registry = createRegistry();
    registerReview(registry);
    const output = document.createElement('section');
    document.body.appendChild(output);
    let token = '';
    const ctx = {
      api: { authHeaders: () => token ? { Authorization: `Bearer ${token}` } : {} },
      out: {
        line: (text, cls) => output.appendChild(renderLine(document, text, cls)),
        pre: (text) => output.appendChild(renderPre(document, text)),
        table: (headers, rows) => output.appendChild(renderTable(document, headers, rows)),
        kv: (pairs) => output.appendChild(renderKv(document, pairs)),
      },
    };
    await registry.get('review').run(ctx, ['show', 'doc1']);
    const unauthorized = output.textContent;
    output.replaceChildren();
    token = 'test-token';
    await registry.get('review').run(ctx, ['queue']);
    const queueText = output.textContent;
    const queueInjected = output.querySelectorAll('img,script,iframe,a').length;
    output.replaceChildren();
    await registry.get('review').run(ctx, ['show', 'doc1']);
    const text = output.textContent;
    const injected = output.querySelectorAll('img,script,iframe,a').length;
    const transcript = output.querySelector('pre')?.textContent;
    const controller = new AbortController();
    ctx.signal = () => controller.signal;
    output.replaceChildren();
    const cancelled = registry.get('review').run(ctx, ['show', 'doc1']);
    controller.abort();
    await cancelled;
    const cancelledText = output.textContent;
    return { unauthorized, queueText, queueInjected, cancelledText, text, injected, transcript,
      xss: globalThis.xss === true };
  });
  assert.match(result.unauthorized, /401/);
  assert.ok(!result.unauthorized.includes(PRIVATE_TEXT));
  assert.equal(result.injected, 0);
  assert.equal(result.queueInjected, 0);
  assert.ok(result.queueText.includes(HOSTILE));
  assert.equal(result.cancelledText, '');
  assert.equal(result.xss, false);
  assert.ok(result.text.includes(HOSTILE));
  assert.ok(result.text.includes(PRIVATE_TEXT));
  assert.ok(result.transcript.includes(PRIVATE_TEXT));
  assert.doesNotMatch(result.text, /do-not-render-this-secret|never-render-prompt/);
  assert.ok(!messages.join('\n').includes(PRIVATE_TEXT));
  assert.ok(!requests.join('\n').includes(PRIVATE_TEXT));
});
