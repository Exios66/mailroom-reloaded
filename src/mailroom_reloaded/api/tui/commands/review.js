import { httpFor } from '../lib/http.js';

const USAGE = 'review | review queue | review show <doc_id>';
const decorated = new WeakSet();
const isRecord = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const record = (value) => isRecord(value) ? value : {};
const controls = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u00ad\u061c\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]/g;

function bounded(value, max = 300, multiline = false) {
  if (!['string', 'number', 'boolean'].includes(typeof value) || value === '') return 'absent';
  const raw = String(value);
  let result = raw.slice(0, max).replace(controls, ' ');
  if (!multiline) result = result.replace(/[\r\n\t]/g, ' ');
  else result = result.replace(/\r\n?/g, '\n').replace(/\t/g, ' ')
    .split('\n').slice(0, 200).map((line) => line.slice(0, 500)).join('\n');
  return result + (result.length < raw.length ? '\n[truncated or sanitized]' : '');
}

function score(value) {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : 'absent';
}

function show(ctx, response) {
  const manifest = record(response.manifest);
  const state = record(manifest.state);
  const catalog = record(response.catalog);
  const report = record(response.report);
  const classification = record(report.classification);
  ctx.out.kv([
    ['doc_id', bounded(response.doc_id)],
    ['status', bounded(response.status)],
    ['filename', bounded(manifest.filename ?? catalog.filename)],
    ['doc_type', bounded(record(state.sort).doc_type ?? classification.doc_type ?? catalog.doc_type)],
    ['updated_at', bounded(manifest.updated_at)],
  ]);
  ctx.out.line('Document transcript (manifest.state.text)', 'info');
  ctx.out.pre(typeof state.text === 'string' && state.text !== ''
    ? bounded(state.text, 12000, true) : 'absent — no document transcript recorded');
  ctx.out.line('Route trail', 'info');
  const trail = Array.isArray(state.route_trail) ? state.route_trail : report.route_trail;
  if (Array.isArray(trail) && trail.length) {
    ctx.out.pre(trail.slice(0, 60).map((entry) => bounded(entry, 100)).join(' -> '));
    if (trail.length > 60) ctx.out.line('[route trail truncated]', 'dim');
  } else ctx.out.line('absent — no route trail recorded', 'dim');

  const verdict = isRecord(state.verdict) ? state.verdict : record(report.verdict);
  const grade = record(state.grade);
  ctx.out.line('Verdict', 'info');
  ctx.out.kv([
    ['label', bounded(verdict.label)],
    ['verdict score (recorded, not a rubric)', score(verdict.score)],
    ['grade overall (recorded, not a rubric)', score(grade.overall)],
  ]);
  for (const [label, findings] of [['Verdict field findings', verdict.field_findings], ['Grade field findings', grade.fields]]) {
    ctx.out.line(label, 'info');
    if (!Array.isArray(findings) || findings.length === 0) {
      ctx.out.line('absent — no field findings recorded', 'dim');
      continue;
    }
    ctx.out.table(['field', 'verdict', 'rationale'], findings.slice(0, 40).map((finding) => {
      const item = record(finding);
      return [bounded(item.field), bounded(item.verdict), bounded(item.rationale)];
    }));
    if (findings.length > 40) ctx.out.line('[field findings truncated]', 'dim');
  }
  ctx.out.line('Rubric scores', 'info');
  const rubric = record(verdict.rubric_scores ?? grade.rubric_scores ?? report.rubric_scores);
  const rows = [];
  let truncated = false;
  for (const key in rubric) {
    if (!Object.hasOwn(rubric, key)) continue;
    if (rows.length === 30) { truncated = true; break; }
    rows.push([bounded(key), score(rubric[key])]);
  }
  if (rows.length) ctx.out.table(['rubric', 'score'], rows);
  else ctx.out.line('absent — no rubric scores recorded; no scores inferred', 'dim');
  if (truncated) ctx.out.line('[rubric scores truncated]', 'dim');
}

async function runReview(ctx, args = [], flags = {}) {
  const queue = args.length === 0 || (args.length === 1 && args[0] === 'queue');
  const detail = args.length === 2 && args[0] === 'show';
  if (Object.keys(flags).length || (!queue && !detail)) {
    return ctx.out.line(`review: usage: ${USAGE}`, 'error');
  }
  if (detail && (typeof args[1] !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(args[1]))) {
    return ctx.out.line('review: invalid doc_id (letters, digits, _ and -; max 128)', 'error');
  }
  const signal = typeof ctx.signal === 'function' ? ctx.signal() : undefined;
  if (signal?.aborted) return;
  try {
    const http = httpFor(ctx);
    const response = queue
      ? await http.get('/v1/documents', { status: 'parked' }, { signal })
      : await http.get(`/v1/documents/${encodeURIComponent(args[1])}`, undefined, { signal });
    if (signal?.aborted) return;
    if (!isRecord(response) || (queue && !Array.isArray(response.documents))) {
      return ctx.out.line('review: bad response from the api', 'error');
    }
    if (!queue) return show(ctx, response);
    const documents = response.documents;
    if (!documents.length) return ctx.out.line('no parked documents', 'dim');
    ctx.out.table(['doc_id', 'filename', 'type', 'status'], documents.slice(0, 50).map((entry) => {
      const doc = record(entry);
      return [bounded(doc.doc_id, 128), bounded(doc.filename), bounded(doc.doc_type), bounded(doc.status)];
    }));
    ctx.out.line(`${Math.min(documents.length, 50)} parked documents shown (API page; not the total queue)`, 'dim');
    if (documents.length > 50) ctx.out.line('[queue output truncated]', 'dim');
  } catch (error) {
    if (signal?.aborted || error?.kind === 'aborted') return;
    const messages = {
      unauthorized: "401 — run 'auth <token>'",
      forbidden: '403 — not allowed by this server',
      not_found: detail ? `no such document ${args[1]}` : 'queue not found',
      offline: 'api unreachable — mailroom closed',
      timeout: 'timed out — is the api up?',
      parse: 'bad response from the api',
    };
    ctx.out.line(`review: ${messages[error?.kind] ?? 'request failed'}`, 'error');
  }
}

export function registerReview(registry) {
  const existing = registry.get('review');
  if (existing && decorated.has(existing)) return;
  const spec = {
    name: 'review',
    summary: 'list parked documents or show a document for operator review',
    usage: USAGE,
    man: `NAME
    review — review parked documents

SYNOPSIS
    ${USAGE}

DESCRIPTION
    Bare review and review queue read the existing parked document API page.
    Show reads the authenticated document endpoint. Transcript, route trail,
    findings and recorded scores are bounded and rendered only as operator text.
    Missing data is explicitly absent; verdict/grade scores are not rubric scores.`,
    run: runReview,
  };
  if (existing) {
    if (typeof existing.run !== 'function' || !Object.isExtensible(existing)
      || ['run', 'summary', 'usage', 'man'].some((key) => {
        const descriptor = Object.getOwnPropertyDescriptor(existing, key);
        return descriptor && (!('value' in descriptor) || !descriptor.writable);
      })) {
      throw new Error('review integration requires a mutable command spec; register review after pipeline');
    }
    Object.assign(existing, spec);
    decorated.add(existing);
  } else {
    registry.register(spec);
    decorated.add(spec);
  }
}
