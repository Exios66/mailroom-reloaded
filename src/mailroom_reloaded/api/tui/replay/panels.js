// Pluggable panel registry for the replay viewer — the TUI counterpart of
// f1-race-replay's "pit wall window". A panel is a small pure renderer:
// render(ctx) -> rows, where a row is an array of [text, cls] segments using the
// grid's classes (dim|ok|warn|err|info|hot|sel|''). Strings pass through the
// grid's truncate()/clip() so control and bidi characters are neutralised and
// every row fits. Nothing here touches the DOM, the clock or the network.

import { bar, clip, fmtTime, truncate } from './grid.js';

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

function safe(fn, fallback) {
  try {
    const r = fn();
    return r === undefined ? fallback : r;
  } catch {
    return fallback;
  }
}

function money(v) {
  return `$${num(v).toFixed(4)}`;
}

function compact(n) {
  const v = num(n);
  return v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : String(Math.round(v));
}

/** A percentage, or an em dash when the rollup is absent. */
function pct(v) {
  return typeof v === 'number' && Number.isFinite(v) ? `${Math.round(v * 100)}%` : '—';
}

function header(title, cols) {
  return clip([[' ─ ' + title + ' ' + '─'.repeat(Math.max(0, num(cols))), 'dim']], cols);
}

const SPARK_BLOCKS = '▁▂▃▄▅▆▇█';

/** Map a numeric series to `▁▂▃▄▅▆▇█`, at most `width` cells. */
function sparkline(values, width) {
  const w = Math.max(1, Math.floor(num(width)));
  const vals = Array.isArray(values) ? values.filter((v) => typeof v === 'number' && Number.isFinite(v)) : [];
  if (vals.length === 0) return '';
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const span = hi - lo || 1;
  const out = [];
  for (let i = 0; i < w; i += 1) {
    const v = vals[Math.min(vals.length - 1, Math.round((i * (vals.length - 1)) / Math.max(1, w - 1)))];
    const k = Math.round(((v - lo) / span) * (SPARK_BLOCKS.length - 1));
    out.push(SPARK_BLOCKS[Math.max(0, Math.min(SPARK_BLOCKS.length - 1, k))]);
  }
  return out.join('');
}

/** Per-interval growth of a running total, sampled `n` times over [0, playhead]. */
function rateSeries(ctx, key, n) {
  const model = isObj(ctx) ? ctx.model : {};
  const t = playhead(ctx);
  const N = Math.max(2, Math.floor(num(n)));
  const cum = [];
  for (let i = 0; i < N; i += 1) {
    const tot = safe(() => model.runningTotalsAt((t * i) / (N - 1)), {}) || {};
    cum.push(num(tot[key]));
  }
  return cum.map((v, i) => (i === 0 ? 0 : Math.max(0, v - cum[i - 1])));
}

function playhead(ctx) {
  const c = isObj(ctx) ? ctx.clock : null;
  if (isObj(c) && Number.isFinite(c.t)) return c.t;
  const s = isObj(ctx) ? ctx.st : null;
  return isObj(s) && Number.isFinite(s.t) ? s.t : 0;
}

// ------------------------------------------------------------------ built-ins

function metricsPanel(ctx) {
  const model = isObj(ctx) ? ctx.model : {};
  const st = isObj(ctx) ? ctx.st : {};
  const cols = isObj(ctx) ? ctx.cols : 100;
  const totals = safe(() => model.runningTotalsAt(playhead(ctx)), {}) || {};
  const firstPass = safe(() => model.firstPassAt(playhead(ctx)), null);
  const docList = Array.isArray(st.docs) ? st.docs : [];
  // Documents started by t, out of all documents (as grid.js's metrics row).
  const started = typeof st.started === 'number' ? st.started : docList.length;
  const total = typeof st.total === 'number' ? st.total : docList.length;
  const failed = num(st.failed);
  return [
    header('metrics', cols),
    clip(
      [
        [` docs ${started} of ${total}`, 'info'],
        ['  done ', 'dim'], [String(num(st.done)), 'ok'],
        ['  failed ', 'dim'], [String(failed), failed > 0 ? 'err' : ''],
        ['  active ', 'dim'], [String(num(st.active)), 'info'],
      ],
      cols,
    ),
    clip(
      [
        [' tok ', 'dim'], [compact(totals.tokens), ''],
        ['  cost ', 'dim'], [money(totals.cost_usd), ''],
        ['  calls ', 'dim'], [String(num(totals.llm_calls)), ''],
        ['  first-pass ', 'dim'], [
          typeof firstPass === 'number' && Number.isFinite(firstPass) ? pct(firstPass) : '--',
          typeof firstPass === 'number' && Number.isFinite(firstPass) ? 'info' : 'dim',
        ],
      ],
      cols,
    ),
  ];
}

function tokensPanel(ctx) {
  const model = isObj(ctx) ? ctx.model : {};
  const cols = isObj(ctx) ? ctx.cols : 100;
  const totals = safe(() => model.runningTotalsAt(playhead(ctx)), {}) || {};
  const tokens = num(totals.tokens);
  const cost = num(totals.cost_usd);
  const w = Math.max(8, Math.min(48, Math.floor(num(cols)) - 20));
  return [
    header('tokens', cols),
    clip(
      [
        [' tokens ', 'dim'], [compact(tokens), ''],
        ['  cost ', 'dim'], [money(cost), ''],
        ['  calls ', 'dim'], [String(num(totals.llm_calls)), ''],
      ],
      cols,
    ),
    clip([[' token rate ', 'dim'], [sparkline(rateSeries(ctx, 'tokens', w), w), 'info']], cols),
    clip([[' cost  rate ', 'dim'], [sparkline(rateSeries(ctx, 'cost_usd', w), w), 'hot']], cols),
  ];
}

/** Classify one event kind into the gate/retry/escalation mix. */
function decisionFamily(kind) {
  const k = typeof kind === 'string' ? kind : '';
  if (/retry/.test(k)) return 'retry';
  if (/escalat|park|exception/.test(k)) return 'escalation';
  if (/gate|arbiter|boss|route/.test(k)) return 'gate';
  return null;
}

function decisionsPanel(ctx) {
  const model = isObj(ctx) ? ctx.model : {};
  const cols = isObj(ctx) ? ctx.cols : 100;
  const events = safe(() => model.eventsUpTo(playhead(ctx), 2000), null);
  const out = [header('decisions', cols)];
  if (!Array.isArray(events)) {
    out.push(clip([[' gate —  retry —  escalation —', 'dim']], cols));
    return out;
  }
  const fam = { gate: 0, retry: 0, escalation: 0 };
  for (const e of events) {
    const f = decisionFamily(isObj(e) ? e.kind : null);
    if (f) fam[f] += 1;
  }
  out.push(
    clip(
      [
        [' gate ', 'dim'], [String(fam.gate), fam.gate > 0 ? 'info' : 'dim'],
        ['  retry ', 'dim'], [String(fam.retry), fam.retry > 0 ? 'warn' : 'dim'],
        ['  escalation ', 'dim'], [String(fam.escalation), fam.escalation > 0 ? 'err' : 'dim'],
      ],
      cols,
    ),
  );
  return out;
}

function latencyPanel(ctx) {
  const model = isObj(ctx) ? ctx.model : {};
  const cols = isObj(ctx) ? ctx.cols : 100;
  // Only segments that have ended by the playhead; nothing from later in the run.
  const per = safe(() => model.stationLatencyAt(playhead(ctx)), null);
  const out = [header('latency', cols)];
  if (!isObj(per) || Object.keys(per).length === 0) {
    out.push(clip([[' p50/p95 —', 'dim']], cols));
    return out;
  }
  out.push(
    clip(
      [
        [' station ', 'dim'], ['     p50_s', 'dim'], ['      p95_s', 'dim'],
      ],
      cols,
    ),
  );
  for (const [id, raw] of Object.entries(per)) {
    const x = isObj(raw) ? raw : {};
    const p50 = typeof x.p50_s === 'number' && Number.isFinite(x.p50_s) ? x.p50_s.toFixed(2).padStart(8) : '       —';
    const p95 = typeof x.p95_s === 'number' && Number.isFinite(x.p95_s) ? x.p95_s.toFixed(2).padStart(9) : '        —';
    out.push(
      clip(
        [
          [` ${truncate(id, 14).padEnd(14)}`, 'info'],
          [p50, ''],
          [p95, ''],
          [` n ${num(x.n)}`, 'dim'],
        ],
        cols,
      ),
    );
  }
  return out;
}

function fieldsPanel(ctx) {
  const model = isObj(ctx) ? ctx.model : {};
  const st = isObj(ctx) ? ctx.st : {};
  const cols = isObj(ctx) ? ctx.cols : 100;
  const sel = isObj(ctx) && Number.isInteger(ctx.sel) ? ctx.sel : -1;
  const docs = Array.isArray(st.docs) ? st.docs : [];
  const d = sel >= 0 && sel < docs.length ? docs[sel] : null;
  const out = [header('fields', cols)];
  if (!d) {
    out.push(clip([[' no document selected', 'dim']], cols));
    return out;
  }
  out.push(clip([[' file ', 'dim'], [truncate(d.filename, cols - 8), 'sel']], cols));
  const scores = safe(() => model.scoresAt(playhead(ctx), d.doc_id), null);
  if (!Array.isArray(scores) || scores.length === 0) {
    out.push(clip([[' no scores', 'dim']], cols));
    return out;
  }
  for (const raw of scores) {
    const x = isObj(raw) ? raw : {};
    let value;
    try {
      value = x.value === null || x.value === undefined ? '—' : truncate(isObj(x.value) ? JSON.stringify(x.value) : x.value, 30);
    } catch {
      value = '—';
    }
    out.push(clip([[` ${truncate(x.name, 24).padEnd(24)} `, 'dim'], [value, '']], cols));
  }
  return out;
}

function leaderboardPanel(ctx) {
  const model = isObj(ctx) ? ctx.model : {};
  const st = isObj(ctx) ? ctx.st : {};
  const cols = isObj(ctx) ? ctx.cols : 100;
  const sel = isObj(ctx) && Number.isInteger(ctx.sel) ? ctx.sel : -1;
  const docs = Array.isArray(st.docs) ? st.docs : [];
  const stations = Array.isArray(model.stations) ? model.stations : [];
  const order = new Map();
  stations.forEach((s, i) => {
    if (isObj(s) && typeof s.id === 'string') order.set(s.id, i);
  });
  const span = Math.max(1, stations.length);
  const ranked = docs
    .map((d, i) => {
      const doc = isObj(d) ? d : {};
      const at = typeof doc.station === 'string' ? order.get(doc.station) : undefined;
      const progress = doc.finished === true ? 1 : at === undefined ? 0 : (at + 1) / span;
      return { doc, i, progress };
    })
    .sort((a, b) => b.progress - a.progress || a.i - b.i);

  const out = [header('leaderboard', cols)];
  if (ranked.length === 0) {
    out.push(clip([[' no documents', 'dim']], cols));
    return out;
  }
  const fileW = Math.max(8, Math.min(30, Math.floor(num(cols)) - 56));
  out.push(clip([[`  #   ${'doc'.padEnd(9)} ${'station'.padEnd(12)} ${'elapsed'.padEnd(7)} ${'tokens'.padEnd(6)} ${'progress'.padEnd(10)} file`, 'dim']], cols));
  ranked.forEach(({ doc, i, progress }, pos) => {
    const failed = doc.status === 'failed' || doc.final_status === 'failed';
    const glyph = failed ? '✕' : doc.finished === true ? '✓' : '·';
    out.push(
      clip(
        [
          [`${String(pos + 1).padStart(3)} `, 'dim'],
          [glyph, failed ? 'err' : doc.finished === true ? 'ok' : 'dim'],
          [` ${truncate(doc.doc_id, 9).padEnd(9)}`, ''],
          [` ${truncate(doc.station, 12).padEnd(12)}`, 'info'],
          [` ${`${num(doc.elapsed_s).toFixed(1)}s`.padStart(7)}`, ''],
          [` ${compact(doc.tokens).padStart(6)}`, 'dim'],
          [` ${bar(progress, 10)}`, progress >= 1 ? 'ok' : 'hot'],
          [` ${truncate(doc.filename, fileW)}`, i === sel ? 'sel' : ''],
        ],
        cols,
      ),
    );
  });
  return out;
}

function tickerPanel(ctx) {
  const model = isObj(ctx) ? ctx.model : {};
  const cols = isObj(ctx) ? ctx.cols : 100;
  const events = safe(() => model.eventsUpTo(playhead(ctx), 8), null);
  const out = [header('ticker', cols)];
  if (!Array.isArray(events) || events.length === 0) {
    out.push(clip([[' no events yet', 'dim']], cols));
    return out;
  }
  for (const raw of events) {
    const e = isObj(raw) ? raw : {};
    out.push(
      clip(
        [
          [` ${fmtTime(e.t)} `, 'dim'],
          [truncate(e.kind, 18).padEnd(18), 'info'],
          [` ${truncate(e.station, 12).padEnd(12)}`, ''],
          [` ${truncate(e.doc_id, 10)}`, 'dim'],
        ],
        cols,
      ),
    );
  }
  return out;
}

const BUILTINS = [
  { id: 'metrics', title: 'Metrics', render: metricsPanel },
  { id: 'tokens', title: 'Tokens', render: tokensPanel },
  { id: 'decisions', title: 'Decisions', render: decisionsPanel },
  { id: 'latency', title: 'Latency', render: latencyPanel },
  { id: 'fields', title: 'Fields', render: fieldsPanel },
  { id: 'leaderboard', title: 'Leaderboard', render: leaderboardPanel },
  { id: 'ticker', title: 'Ticker', render: tickerPanel },
];

// ------------------------------------------------------------------ registry

const registry = new Map();

// Ids the viewer itself handles (`panel` selectors in the command) plus the shipped panels.
const RESERVED_IDS = new Set(['none', 'inspector', 'ledger', ...BUILTINS.map((b) => b.id)]);
const ID_RE = /^[A-Za-z0-9_-]{1,32}$/;
const MAX_TITLE = 40;
// Control, zero-width and bidi characters would reorder or hide cells (as grid.js).
const CTRL_RE = new RegExp(
  '[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ufeff]',
  'g',
);

function addPanel(spec, builtin) {
  const { id, title, render } = spec || {};
  if (typeof id !== 'string' || !ID_RE.test(id)) {
    throw new TypeError('panel id must be 1-32 characters of letters, digits, _ or -');
  }
  if (!builtin && RESERVED_IDS.has(id)) {
    throw new Error(`panel id '${id}' is reserved`);
  }
  if (typeof render !== 'function') {
    throw new TypeError(`panel '${id}': render must be a function`);
  }
  if (registry.has(id)) {
    throw new Error(`panel '${id}' is already registered`);
  }
  const cleanTitle = typeof title === 'string' ? Array.from(title.replace(CTRL_RE, ' ').trim()).slice(0, MAX_TITLE).join('') : '';
  const entry = { id, title: cleanTitle || id, render };
  registry.set(id, entry);
  return entry;
}

/**
 * Register an insight panel `{ id, title, render }`. Throws on a bad spec, a reserved id
 * (`none`, `inspector`, `ledger` or a shipped panel's id) or a duplicate id, so a shipped
 * panel is never replaced. The id must be 1-32 characters of letters, digits, `_` or `-`;
 * the title is stripped of control characters and clamped to 40. Panels are reached with
 * `p`; there is no per-panel key. The rows `render` returns are sanitised by the grid
 * (control characters, classes, length). Returns the stored spec.
 */
export function registerPanel(spec = {}) {
  return addPanel(spec, false);
}

/** The registered spec, or null. */
export function getPanel(id) {
  return registry.get(id) ?? null;
}

/** Every panel as { id, title }, in registration order. */
export function listPanels() {
  return [...registry.values()].map(({ id, title }) => ({ id, title }));
}

/** Restore exactly the built-in panel set (test helper). */
export function resetPanels() {
  registry.clear();
  for (const spec of BUILTINS) addPanel(spec, true);
}

resetPanels();
