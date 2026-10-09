// Pure frame renderer for the replay viewer: returns rows for takeover.draw.
// A row is an array of [text, cls] segments. No DOM, no clock; every untrusted
// string is only sanitised (control chars -> space) and truncated, never interpreted.

import { getPanel } from './panels.js';

const CLASSES = new Set(['dim', 'ok', 'warn', 'err', 'info', 'hot', 'sel']);
const MIN_COLS = 60;
const MAX_COLS = 160;
const MIN_ROWS = 14;
const MAX_ROWS = 60;
const MAX_TIME_S = 99 * 60 + 59.9;

// Control, zero-width and bidi-override characters would reorder or hide cells.
const CTRL_RE = new RegExp(
  '[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ufeff]',
  'g',
);
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Printable single-line string: control characters become spaces. */
function clean(v) {
  if (v === null || v === undefined || v === '') return '—';
  let s;
  try {
    s = typeof v === 'object' ? JSON.stringify(v) : String(v);
  } catch {
    s = '';
  }
  return typeof s === 'string' && s !== '' ? s.replace(CTRL_RE, ' ') : '—';
}

export function truncate(s, n) {
  const str = clean(s);
  const len = Math.max(0, Math.floor(num(n)));
  const chars = Array.from(str);
  if (chars.length <= len) return str;
  if (len === 0) return '';
  return `${chars.slice(0, len - 1).join('')}…`;
}

export function bar(frac, width) {
  const w = Math.max(0, Math.floor(num(width)));
  const n = Math.round(clamp(num(frac), 0, 1) * w);
  return '•'.repeat(n) + '·'.repeat(w - n);
}

export function fmtTime(sec) {
  const s = Math.min(MAX_TIME_S, Math.max(0, num(sec)));
  const tenths = Math.floor(s * 10 + 1e-9);
  const m = Math.floor(tenths / 600);
  const rest = tenths - m * 600;
  const ss = Math.floor(rest / 10);
  return `${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}.${rest % 10}`;
}

/** Clip a segment list to `cols` characters (code points). Shared with panels.js. */
export function clip(segs, cols) {
  const out = [];
  let left = cols;
  for (const [t, cls] of segs) {
    if (left <= 0) break;
    const chars = Array.from(String(t));
    const piece = chars.length > left ? chars.slice(0, left).join('') : chars.join('');
    left -= Math.min(chars.length, left);
    if (piece) out.push([piece, cls || '']);
  }
  return out;
}

function stationCls(token) {
  const t = typeof token === 'string' ? token.replace(/^g-/, '') : '';
  return CLASSES.has(t) && t !== 'sel' ? t : '';
}

const KIND_INDENT = { main: '', detour: '  ', bay: '    ' };
const MARK_CHARS = '0123456789abcdefghijklmnopqrstuvwxyz';

function mark(doc, i) {
  if (doc && doc.status === 'failed') return '◆';
  if (doc && doc.finished) return '✓';
  return MARK_CHARS[i % MARK_CHARS.length];
}

function money(v) {
  return `$${num(v).toFixed(4)}`;
}

function compact(n) {
  const v = num(n);
  return v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : String(Math.round(v));
}

function safe(fn, fallback) {
  try {
    const r = fn();
    return r === undefined ? fallback : r;
  } catch {
    return fallback;
  }
}

function headerRow({ model, clock, sess }) {
  const glyph = clock.ended ? '[end]' : clock.playing ? '>' : '||';
  const segs = [
    [' replay ', 'hot'],
    [truncate(sess.id, 40), 'info'],
    [`  ${glyph}  `, clock.playing ? 'ok' : 'warn'],
    [`${fmtTime(clock.t)} / ${fmtTime(clock.duration)}`, ''],
    [`  x${clock.speed}`, 'info'],
    [`  ${sess.source === 'audit' ? 'audit' : 'spans'}${sess.approx ? ' ~approx' : ''}`, sess.approx ? 'warn' : 'dim'],
  ];
  if (sess.data_pruned) segs.push(['  data pruned', 'err']);
  return segs;
}

function trackRows({ model, st, sel, cols }) {
  const stations = Array.isArray(model.stations) ? model.stations : [];
  const docs = Array.isArray(st.docs) ? st.docs : [];
  const total = Math.max(1, docs.length);
  const counts = st.counts && typeof st.counts === 'object' ? st.counts : {};
  const out = [];
  for (const s of stations) {
    const x = s && typeof s === 'object' ? s : {};
    const cls = stationCls(x.color_token);
    const indent = Object.hasOwn(KIND_INDENT, x.kind) ? KIND_INDENT[x.kind] : '';
    const n = Math.max(0, Math.floor(num(counts[x.id])));
    const label = truncate(x.label || x.id, 14 - indent.length).padEnd(14 - indent.length);
    const segs = [[` ${indent}${label} `, cls], [bar(n / total, 10), n > 0 ? cls || 'ok' : 'dim'], [` ${String(n).padStart(2)} `, 'dim']];
    docs.forEach((d, i) => {
      if (d && d.station === x.id) segs.push([mark(d, i), i === sel ? 'sel' : d.status === 'failed' ? 'err' : d.finished ? 'ok' : cls]);
    });
    out.push(clip(segs, cols));
  }
  if (out.length === 0) out.push(clip([[' (no stations)', 'dim']], cols));
  return out;
}

function scrubRow({ model, clock, cols }) {
  const w = Math.max(1, cols - 2);
  const dur = num(clock.duration);
  const frac = dur > 0 ? clamp(num(clock.t) / dur, 0, 1) : 0;
  const cells = Array(w).fill('─');
  const events = safe(() => model.eventsBetween(-Infinity, dur), []);
  if (Array.isArray(events) && dur > 0) {
    for (const e of events) {
      const i = clamp(Math.round((num(e && e.t) / dur) * (w - 1)), 0, w - 1);
      cells[i] = '┼';
    }
  }
  const head = clamp(Math.round(frac * (w - 1)), 0, w - 1);
  const left = cells.slice(0, head).join('');
  const right = cells.slice(head + 1).join('');
  return [[' ', ''], [left, 'ok'], ['█', 'hot'], [right, 'dim'], [' ', '']];
}

function metricsRow({ model, st, cols }) {
  const totals = safe(() => model.runningTotalsAt(st.t), {}) || {};
  const roll = (model.rollups && typeof model.rollups === 'object' ? model.rollups : {});
  const segs = [
    [` docs ${(st.docs || []).length}  done `, 'dim'],
    [String(num(st.done)), 'ok'],
    ['  failed ', 'dim'],
    [String(num(st.failed)), num(st.failed) > 0 ? 'err' : ''],
    ['  active ', 'dim'],
    [String(num(st.active)), 'info'],
    [`  tok ${compact(totals.tokens)}  ${money(totals.cost_usd)}  calls ${num(totals.llm_calls)}`, ''],
  ];
  if (typeof roll.first_pass_rate === 'number' && Number.isFinite(roll.first_pass_rate)) {
    segs.push([`  first-pass ${Math.round(roll.first_pass_rate * 100)}%`, 'info']);
  }
  return clip(segs, cols);
}

function inspectorRows({ model, st, clock, sel, cols }) {
  const docs = Array.isArray(st.docs) ? st.docs : [];
  const d = sel >= 0 && sel < docs.length ? docs[sel] : null;
  const out = [clip([[' ─ inspector ' + '─'.repeat(cols), 'dim']], cols)];
  if (!d) {
    out.push(clip([[' no document selected (j/k)', 'dim']], cols));
    return out;
  }
  const ent = safe(() => model.docAt(d.doc_id), null) || {};
  out.push(clip([[' file ', 'dim'], [truncate(d.filename || ent.filename, cols - 8), 'sel']], cols));
  out.push(
    clip(
      [
        [' station ', 'dim'], [truncate(d.station, 16), 'info'],
        ['  status ', 'dim'], [truncate(d.status, 10), d.status === 'failed' ? 'err' : 'ok'],
        ['  attempt ', 'dim'], [truncate(d.attempt ?? 1, 4), ''],
        ['  verdict ', 'dim'], [truncate(d.verdict ?? ent.verdict, 16), ''],
      ],
      cols,
    ),
  );
  const causes = Array.isArray(ent.review_causes) && ent.review_causes.length ? ent.review_causes.map(clean).join(',') : '—';
  out.push(
    clip(
      [
        [' final ', 'dim'], [truncate(d.final_status ?? ent.final_status, 12), ''],
        ['/', 'dim'], [truncate(d.final_stage ?? ent.final_stage, 16), ''],
        ['  failure ', 'dim'], [truncate(ent.failure_class, 16), ent.failure_class ? 'err' : 'dim'],
        ['  causes ', 'dim'], [truncate(causes, 30), ent.review_causes && ent.review_causes.length ? 'warn' : 'dim'],
      ],
      cols,
    ),
  );
  const gens = safe(() => model.generationsFor(d.doc_id, clock.t), []);
  out.push(clip([[' role            model                    tokens      cost', 'dim']], cols));
  (Array.isArray(gens) ? gens : []).slice(-4).forEach((g) => {
    const x = g && typeof g === 'object' ? g : {};
    const tok = num(x.prompt_tokens) + num(x.completion_tokens);
    out.push(
      clip(
        [[` ${truncate(x.role, 14).padEnd(14)}  ${truncate(x.model, 22).padEnd(22)} ${String(tok).padStart(8)} ${money(x.cost_usd).padStart(9)}`, '']],
        cols,
      ),
    );
  });
  const evs = safe(() => model.eventsForDoc(d.doc_id, clock.t, 3), []);
  (Array.isArray(evs) ? evs : [])
    .forEach((e) => {
      out.push(clip([[` ${fmtTime(e.t)} `, 'dim'], [truncate(e.kind, 20), 'info'], [` ${truncate(e.station, 16)}`, 'dim']], cols));
    });
  return out;
}

function ledgerRows({ ledger, cols }) {
  const out = [clip([[' ─ ledger ' + '─'.repeat(cols), 'dim']], cols)];
  if (!ledger || ledger.loading) {
    out.push(clip([[' loading…', 'dim']], cols));
    return out;
  }
  if (ledger.unavailable) {
    out.push(clip([[' unavailable', 'warn']], cols));
    return out;
  }
  const entries = Array.isArray(ledger.entries) ? ledger.entries : [];
  if (entries.length === 0) out.push(clip([[' no ledger entries', 'dim']], cols));
  for (const e of entries) {
    const x = e && typeof e === 'object' ? e : {};
    const h = typeof x.entry_hash === 'string' ? x.entry_hash.slice(0, 12) : '—';
    out.push(clip([[` #${truncate(x.seq, 6).padEnd(6)} ${truncate(x.kind, 24).padEnd(24)} `, ''], [truncate(h, 12), 'dim']], cols));
  }
  const v = ledger.verify;
  if (!v || typeof v !== 'object') out.push(clip([[' chain: not verified', 'dim']], cols));
  else if (v.ok) out.push(clip([[` chain ok — ${truncate(v.count, 8)} entries`, 'ok']], cols));
  else if (v.broken_at == null) {
    // The run has no ledger rows to verify (e.g. a seeded showcase run). A run-scoped verify
    // cannot tell that from deleted rows, so this is a warning, not a clean result.
    const why = typeof v.detail === 'string' && v.detail ? `: ${truncate(v.detail, 40)}` : '';
    out.push(clip([[` verify failed${why}`, 'warn']], cols));
  } else out.push(clip([[` chain BROKEN at seq ${truncate(v.broken_at, 8)}`, 'err']], cols));
  return out;
}

const LEGEND =
  ' spc play  </> seek  [ ] speed  0-9 jump  j/k select  i inspect  l ledger  p panels  e event  q quit';

/**
 * Resolve the `panel` selector to rows. Inspector and ledger keep their dedicated
 * renderers; any other non-'none' string is looked up in the panel registry and its
 * render(ctx) is called. An unknown id, a throwing renderer or a non-array result
 * degrades to no panel rows (the frame never throws on a bad panel).
 */
function resolvePanel(panel, ctx) {
  if (panel === 'inspector') return inspectorRows(ctx);
  if (panel === 'ledger') return ledgerRows(ctx);
  if (typeof panel !== 'string' || panel === 'none') return [];
  const spec = getPanel(panel);
  if (!spec) return [];
  const rows = safe(() => spec.render(ctx), []);
  return Array.isArray(rows) ? rows : [];
}

export function renderFrame({ model, st, clock, sel = -1, cols, rows, ledger = null, panel = 'none' } = {}) {
  const C = clamp(Math.floor(num(cols)) || 100, MIN_COLS, MAX_COLS);
  const R = clamp(Math.floor(num(rows)) || 30, MIN_ROWS, MAX_ROWS);
  const m = model && typeof model === 'object' ? model : {};
  const s = st && typeof st === 'object' ? st : { t: 0, docs: [], counts: {} };
  const ck = clock && typeof clock === 'object' ? clock : { t: 0, duration: 0, speed: 1, playing: false };
  const sess = m.session && typeof m.session === 'object' ? m.session : {};
  const selIdx = Number.isInteger(sel) ? sel : -1;
  const ctxo = { model: m, st: s, clock: ck, sel: selIdx, cols: C, ledger, sess };

  const head = clip(headerRow(ctxo), C);
  const scrub = clip(scrubRow(ctxo), C);
  const metrics = metricsRow(ctxo);
  const foot = clip([[LEGEND, 'dim']], C);
  const track = trackRows(ctxo);
  const panelRows = resolvePanel(panel, ctxo);

  const budget = R - 4; // header, scrub, metrics, footer
  let panelBudget = 0;
  if (panelRows.length) panelBudget = Math.min(panelRows.length, Math.max(4, Math.floor(budget / 2)));
  const trackBudget = Math.max(0, budget - panelBudget);
  return [head, ...track.slice(0, trackBudget), scrub, metrics, ...panelRows.slice(0, panelBudget), foot];
}
