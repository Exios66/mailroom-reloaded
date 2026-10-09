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

function docState(entity, segs, t) {
  const tEnd = finite(entity.t_end);
  const base = {
    doc_id: entity.doc_id,
    filename: entity.filename ?? '',
    station: null,
    status: 'waiting',
    attempt: null,
    retry_kind: null,
    finished: false,
    final_status: entity.final_status ?? null,
    final_stage: entity.final_stage ?? null,
    verdict: entity.verdict ?? null,
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
    const docs = entities.map((e) => {
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
    return { t: at, docs, counts, done, failed, active };
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

  function generationsFor(docId, t) {
    const list = gensByDoc.get(keyOf(docId)) ?? [];
    return list.slice(0, upperBound(list, finite(t, -Infinity), 't0'));
  }

  function docAt(docId) {
    return entityById.get(keyOf(docId));
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
  };
}
