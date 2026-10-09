// Read model over a replay/v1 Timeline: stateAt(t) by binary search.
// Pure and defensive: odd input is skipped, never thrown on. Strings are passed
// through untouched (the rendering layer uses textContent).

import { STATIONS } from './stations.js';

const DEFAULT_EVENT_LIMIT = 50;

function isObj(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function rows(v) {
  return Array.isArray(v) ? v.filter(isObj) : [];
}

function finite(v, fallback = null) {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
}

function byKey(list, key) {
  return list.slice().sort((a, b) => a[key] - b[key]);
}

/** Number of items whose `key` is <= t (list sorted by key). */
function upperBound(list, t, key) {
  let lo = 0;
  let hi = list.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (list[mid][key] <= t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function keyOf(v) {
  try {
    return String(v);
  } catch {
    return '';
  }
}

function groupBy(list, key) {
  const map = new Map();
  for (const item of list) {
    const k = keyOf(item[key]);
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(item);
  }
  return map;
}

function cleanSegments(raw) {
  const out = [];
  for (const s of rows(raw)) {
    const t0 = finite(s.t0);
    if (t0 === null || s.doc_id == null) continue;
    out.push({ ...s, t0, t1: finite(s.t1, t0) });
  }
  return byKey(out, 't0');
}

function cleanEvents(raw) {
  const out = [];
  for (const e of rows(raw)) {
    const t = finite(e.t);
    if (t !== null) out.push(e);
  }
  return byKey(out, 't');
}

function cleanScores(raw) {
  return byKey(rows(raw).map((s) => ({ ...s, t: finite(s.t, 0) })), 't');
}

function cleanGenerations(raw) {
  const out = [];
  for (const g of rows(raw)) {
    const t0 = finite(g.t0);
    if (t0 === null || g.doc_id == null) continue;
    out.push({ ...g, t0, t1: finite(g.t1, t0) });
  }
  return byKey(out, 't0');
}

function computeDuration(session, segments, events) {
  let d = Math.max(0, finite(session.duration_s, 0));
  for (const s of segments) d = Math.max(d, s.t1);
  for (const e of events) d = Math.max(d, e.t);
  return d;
}

// Outcome fields of a document exist for the viewer only from its end time (t_end).
const OUTCOME_KEYS = [
  'final_stage',
  'final_status',
  'failure_class',
  'review_causes',
  'verdict',
  'quality',
  'totals',
  'doc_type',
  'doc_subclass',
  'expected_doc_class',
  'expected_subclass',
];

function docState(entity, segs, t) {
  const tEnd = finite(entity.t_end);
  const tStart = finite(entity.t_start, 0);
  const base = {
    doc_id: entity.doc_id,
    filename: t >= tStart ? (entity.filename ?? '') : '',
    station: null,
    status: 'waiting',
    attempt: null,
    retry_kind: null,
    finished: false,
    final_status: null,
    final_stage: null,
    verdict: null,
  };
  const n = upperBound(segs, t, 't0');
  const last = n > 0 ? segs[n - 1] : null;

  if (tEnd !== null && t >= tEnd) {
    const seg = segs.length > 0 ? segs[segs.length - 1] : null;
    return {
      ...base,
      station: seg ? seg.station : null,
      status: entity.final_status || 'ok',
      attempt: seg ? seg.attempt ?? 1 : null,
      retry_kind: seg ? seg.retry_kind ?? null : null,
      finished: true,
      final_status: entity.final_status ?? null,
      final_stage: entity.final_stage ?? null,
      verdict: entity.verdict ?? null,
    };
  }
  if (!last) return base;
  const current = t < last.t1 || (last.status === 'running' && last.t1 <= last.t0);
  return {
    ...base,
    station: last.station,
    status: current ? last.status || 'ok' : 'idle',
    attempt: last.attempt ?? 1,
    retry_kind: last.retry_kind ?? null,
  };
}

/**
 * Build the read model for a replay/v1 Timeline.
 * @param {object} timeline
 */
export function createModel(timeline) {
  const tl = isObj(timeline) ? timeline : {};
  const session = isObj(tl.session) ? tl.session : {};
  const segments = cleanSegments(tl.segments);
  const events = cleanEvents(tl.events);
  const scores = cleanScores(tl.scores);
  const generations = cleanGenerations(tl.generations);
  const entities = rows(tl.entities).filter((e) => e.doc_id != null);
  const stationRows = rows(tl.stations).filter((s) => typeof s.id === 'string');
  const stations = stationRows.length > 0 ? stationRows : STATIONS;
  const rollups = isObj(tl.rollups) ? tl.rollups : {};
  const duration = computeDuration(session, segments, events);

  const segsByDoc = groupBy(segments, 'doc_id');
  const gensByDoc = groupBy(generations, 'doc_id');
  const eventsByDoc = groupBy(events, 'doc_id');
  const entityById = new Map();
  for (const e of entities) {
    const k = keyOf(e.doc_id);
    if (!entityById.has(k)) entityById.set(k, e);
  }

  const gensByEnd = byKey(generations, 't1');
  const prefix = [{ tokens: 0, cost_usd: 0, llm_calls: 0 }];
  for (const g of gensByEnd) {
    const p = prefix[prefix.length - 1];
    prefix.push({
      tokens: p.tokens + (finite(g.prompt_tokens, 0) + finite(g.completion_tokens, 0)),
      cost_usd: p.cost_usd + finite(g.cost_usd, 0),
      llm_calls: p.llm_calls + 1,
    });
  }

  function stateAt(t) {
    const at = finite(t, 0);
    const counts = {};
    for (const s of stations) counts[s.id] = 0;
    let done = 0;
    let failed = 0;
    let active = 0;
    let started = 0;
    const docs = entities.map((e) => {
      if (finite(e.t_start, 0) <= at) started += 1;
      const d = docState(e, segsByDoc.get(keyOf(e.doc_id)) ?? [], at);
      if (d.station !== null) counts[d.station] = (counts[d.station] ?? 0) + 1;
      if (d.finished) {
        if (d.final_status === 'failed' || d.station === 'failed') failed += 1;
        else done += 1;
      } else if (d.status !== 'waiting') {
        active += 1;
      }
      return d;
    });
    return { t: at, docs, counts, done, failed, active, started, total: entities.length };
  }

  /** Share of the documents finished by t that went through first time; null when none has finished. */
  function firstPassAt(t) {
    const at = finite(t, -Infinity);
    const flags = new Map(); // doc -> latest success_rate visible at t (scores are time-ordered)
    for (const s of scores) {
      if (s.t > at) break;
      if (s.name === 'success_rate') flags.set(keyOf(s.doc_id), s.value);
    }
    let done = 0;
    let passed = 0;
    for (const e of entities) {
      const tEnd = finite(e.t_end);
      if (tEnd === null || tEnd > at) continue;
      done += 1;
      const k = keyOf(e.doc_id);
      const flag = flags.get(k);
      if (typeof flag === 'number' && Number.isFinite(flag)) {
        if (flag >= 1) passed += 1;
        continue;
      }
      const segs = segsByDoc.get(k) ?? [];
      const retried =
        (eventsByDoc.get(k) ?? []).some((ev) => ev.kind === 'retry' && ev.t <= at) ||
        segs.some((s) => s.t0 <= at && (s.retry_kind || s.attempt > 1));
      const detoured = segs.some((s) => s.t0 <= at && (s.station === 'boss' || s.station === 'review'));
      if (e.final_status === 'archived' && !retried && !detoured) passed += 1;
    }
    return done > 0 ? passed / done : null;
  }

  function eventsBetween(a, b) {
    const lo = upperBound(events, finite(a, -Infinity), 't');
    const hi = upperBound(events, finite(b, -Infinity), 't');
    return events.slice(lo, Math.max(lo, hi));
  }

  function eventsUpTo(t, limit = DEFAULT_EVENT_LIMIT) {
    const n = upperBound(events, finite(t, -Infinity), 't');
    const lim = finite(limit, DEFAULT_EVENT_LIMIT);
    return events.slice(Math.max(0, n - Math.max(0, Math.floor(lim))), n);
  }

  function eventsForDoc(docId, t, limit = DEFAULT_EVENT_LIMIT) {
    const list = eventsByDoc.get(keyOf(docId)) ?? [];
    const n = upperBound(list, finite(t, -Infinity), 't');
    const lim = Math.max(0, Math.floor(finite(limit, DEFAULT_EVENT_LIMIT)));
    return list.slice(Math.max(0, n - lim), n);
  }

  function scoresAt(t, docId) {
    const upto = scores.slice(0, upperBound(scores, finite(t, -Infinity), 't'));
    if (docId === undefined || docId === null) return upto;
    return upto.filter((s) => keyOf(s.doc_id) === keyOf(docId));
  }

  /** Generations that have ended by t: a call in flight has no tokens or cost yet. */
  function generationsFor(docId, t) {
    const at = finite(t, -Infinity);
    return (gensByDoc.get(keyOf(docId)) ?? []).filter((g) => g.t1 <= at);
  }

  /** Identity at any t; outcome fields only once the document has finished. No t means no outcome. */
  function docAt(docId, t = -Infinity) {
    const e = entityById.get(keyOf(docId));
    if (!e) return undefined;
    const at = finite(t, -Infinity);
    const out = { doc_id: e.doc_id, filename: at >= finite(e.t_start, 0) ? (e.filename ?? '') : '' };
    const tEnd = finite(e.t_end);
    if (tEnd !== null && at >= tEnd) for (const k of OUTCOME_KEYS) out[k] = e[k];
    return out;
  }

  function segmentsFor(docId) {
    return (segsByDoc.get(keyOf(docId)) ?? []).slice();
  }

  function runningTotalsAt(t) {
    const p = prefix[upperBound(gensByEnd, finite(t, -Infinity), 't1')];
    return { tokens: p.tokens, cost_usd: p.cost_usd, llm_calls: p.llm_calls };
  }

  return {
    duration,
    stations,
    session,
    rollups,
    entities,
    stateAt,
    eventsBetween,
    eventsUpTo,
    eventsForDoc,
    scoresAt,
    generationsFor,
    docAt,
    segmentsFor,
    runningTotalsAt,
    firstPassAt,
  };
}
