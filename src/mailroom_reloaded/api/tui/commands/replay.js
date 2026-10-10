// Replay command for /tui: list sessions or open the full-screen replay viewer.
// Reads GET /v1/replay/*; the ledger panel lazily reads GET /v1/ledger and /v1/ledger/verify.
// Every printed value goes through ctx.out / the grid (text only).

import { fail, flagText, parseIntFlag, text, validRunId } from './pipeline.js';
import { SPEEDS, createClock } from '../replay/clock.js';
import { createModel } from '../replay/model.js';
import { listPanels } from '../replay/panels.js';
import { createFollowReader } from '../replay/live.js';
import { fmtTime, renderFrame } from '../replay/grid.js';

const PREFIXES = new Set(['run', 'session', 'doc', 'window']);
const TAIL_RE = /^[A-Za-z0-9._:-]+$/;
const USAGE = 'replay: usage: replay [--limit N] | replay <run_id|session id> [--at SECONDS] [--speed N] [--follow]';
const TICK_MS = 100;
const STEP_S = 5;
const STEP_BIG_S = 30;
const NUM_RE = /^\d+(\.\d+)?$/;

// Follow-live buffer bounds (Task 11): the last 2 h / 5,000 items per list.
export const LIVE_MAX_ITEMS = 5000;
export const LIVE_MAX_AGE_S = 7200;

const LIVE_LISTS = ['segments', 'generations', 'events', 'scores'];
const LIVE_TIME_KEY = { segments: 't0', generations: 't0', events: 't', scores: 't' };

function liveItemTime(list, item) {
  const v = item !== null && typeof item === 'object' ? item[LIVE_TIME_KEY[list]] : undefined;
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/**
 * Bound the follow buffer in place: keep at most LIVE_MAX_ITEMS per list and drop
 * items whose time is more than LIVE_MAX_AGE_S older than the newest item across
 * the four lists (`t0` for segment/generation, `t` for event/score). An item with
 * no finite time is kept (only the count cap can drop it). `session`, `stations`,
 * `entities` and `rollups` are never touched.
 */
export function boundLiveTimeline(tl) {
  if (tl === null || typeof tl !== 'object') return tl;
  let newest = null;
  for (const list of LIVE_LISTS) {
    const arr = tl[list];
    if (!Array.isArray(arr)) continue;
    for (const item of arr) {
      const t = liveItemTime(list, item);
      if (t !== null && (newest === null || t > newest)) newest = t;
    }
  }
  const floor = newest === null ? null : newest - LIVE_MAX_AGE_S;
  for (const list of LIVE_LISTS) {
    const arr = tl[list];
    if (!Array.isArray(arr)) continue;
    let next = arr;
    if (floor !== null) {
      next = next.filter((item) => {
        const t = liveItemTime(list, item);
        return t === null || t >= floor;
      });
    }
    if (next.length > LIVE_MAX_ITEMS) next = next.slice(next.length - LIVE_MAX_ITEMS);
    tl[list] = next;
  }
  return tl;
}

/** Session id for the API, or null when the argument is not acceptable. */
export function normalizeSessionId(arg) {
  if (typeof arg !== 'string' || arg === '') return null;
  const i = arg.indexOf(':');
  if (i < 0) return validRunId(arg) ? `run:${arg}` : null;
  const prefix = arg.slice(0, i);
  const tail = arg.slice(i + 1);
  if (!PREFIXES.has(prefix) || !TAIL_RE.test(tail) || tail === '.' || tail === '..' || tail.startsWith('..')) return null;
  return arg;
}

const manPages = {
  replay: `NAME
    replay — replay a pipeline run on a timeline

SYNOPSIS
    replay [--limit N]
    replay <run_id|session id> [--at SECONDS] [--speed N] [--follow]

DESCRIPTION
    Bare 'replay' lists recent sessions from GET /v1/replay/sessions: id, docs,
    start, duration, source and whether retention pruned the data (--limit 1-100).
    'replay <id>' reads GET /v1/replay/sessions/<id>/timeline and opens a
    full-screen text viewer. A bare run id means run:<id>. --at starts paused at
    that second; --speed is one of 0.5 1 2 4 8 16. --follow (or the 'f' key)
    starts a live SSE reader on GET /v1/replay/live, appends each new segment,
    generation, event and score as it lands, and pins the playhead to now-2s.
    A backward scrub leaves follow; 'f' re-enters. The reader pauses while the
    tab is hidden. The 'l' panel reads
    GET /v1/ledger and /v1/ledger/verify for the run, only when first opened.
    GET /links supplies the Phoenix and Grafana base URLs; 'o' and 'g' open the
    run in a new tab. With prefers-reduced-motion the viewer starts paused.

KEYS
    space        play / pause           left/right   step -/+5s (shift: 30s)
    f            follow live stream (f again, or a backward scrub, leaves it)
    [ ] or - +   slower / faster        Home / End   seek to start / end
    0-9          seek to 0%..90%        up/down j/k  select document
    Enter or i   toggle inspector       l            toggle ledger panel
    p            cycle insight panels   e            jump to next event
    o / g        open Phoenix / Grafana
    Esc or q     quit (Ctrl+C aborts)
    The view is text only; every value is shown literally.`,
};

async function listSessions(ctx, flags) {
  const limit = parseIntFlag(flags, 'limit', 1, 100);
  if (limit.error) return ctx.out.line(`replay: ${limit.error}`, 'error');
  let res;
  try {
    res = await ctx.api.get('/v1/replay/sessions', { limit: limit.value ?? 20 }, { signal: ctx.signal() });
  } catch (err) {
    return fail(ctx, 'replay', err);
  }
  const sessions = (res && Array.isArray(res.sessions) && res.sessions) || [];
  if (sessions.length === 0) return ctx.out.line('no replayable sessions', 'dim');
  ctx.out.table(
    ['id', 'docs', 'started', 'duration', 'source', 'pruned'],
    sessions.map((s) => {
      const x = s && typeof s === 'object' ? s : {};
      const started = typeof x.started_at === 'string' && x.started_at ? x.started_at.replace('T', ' ').slice(0, 19) : '—';
      return [text(x.id), text(x.documents), started, fmtTime(x.duration_s), text(x.source), x.data_pruned ? 'pruned' : '—'];
    }),
  );
  return ctx.out.line(`${sessions.length} session${sessions.length === 1 ? '' : 's'} — 'replay <id>' to open`, 'dim');
}

function parseOpenFlags(flags) {
  const unknown = Object.keys(flags).filter((k) => !['at', 'speed', 'follow'].includes(k));
  if (unknown.length) return { error: `unknown flag --${unknown[0]}` };
  const out = {};
  if (flags.at !== undefined) {
    const raw = flagText(flags, 'at');
    if (raw === undefined || !NUM_RE.test(raw)) return { error: '--at must be a number of seconds' };
    out.at = Number(raw);
  }
  if (flags.speed !== undefined) {
    const raw = flagText(flags, 'speed');
    const n = raw !== undefined && NUM_RE.test(raw) ? Number(raw) : NaN;
    if (!SPEEDS.includes(n)) return { error: `--speed must be one of ${SPEEDS.join(' ')}` };
    out.speed = n;
  }
  if (flags.follow !== undefined) {
    if (flags.follow !== true) return { error: '--follow takes no value' };
    out.follow = true;
  }
  return out;
}

function reducedMotion() {
  try {
    return Boolean(globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
  } catch {
    return false;
  }
}

/**
 * The run's outbound observability URLs from the GET /links config. Both are
 * null when the config is missing or the session is not a run, so the `o`/`g`
 * keys degrade to a no-op.
 */
function httpBase(v) {
  if (typeof v !== 'string' || !/^https?:\/\/[^\s@]+$/i.test(v)) return null;
  return v.replace(/\/+$/, '');
}

function externalUrls(links, id) {
  const cfg = links && typeof links === 'object' ? links : {};
  const phoenix = httpBase(cfg.phoenix_url);
  const grafana = httpBase(cfg.grafana_url);
  const run = typeof id === 'string' && id.startsWith('run:') ? id.slice(4) : null;
  return {
    phoenix,
    grafana: grafana && run ? `${grafana}/d/mailroom-quality?var-run_id=${encodeURIComponent(run)}` : null,
  };
}

async function openViewer(ctx, arg, flags) {
  const id = normalizeSessionId(arg);
  if (!id) return ctx.out.line('replay: invalid session id', 'error');
  const opts = parseOpenFlags(flags);
  if (opts.error) return ctx.out.line(`replay: ${opts.error}`, 'error');

  let timeline;
  try {
    timeline = await ctx.api.get(`/v1/replay/sessions/${encodeURIComponent(id)}/timeline`, undefined, { signal: ctx.signal() });
  } catch (err) {
    if (err && err.kind === 'http' && err.status === 410) {
      const run = id.startsWith('run:') ? id.slice(4) : id;
      return ctx.out.line(`replay: run ${run} — data pruned (ledger entry kept; try 'ledger --run ${run}')`, 'warn');
    }
    if (err && err.kind === 'http' && err.status === 404) return ctx.out.line('replay: no such session', 'error');
    return fail(ctx, 'replay', err);
  }
  if (ctx.signal().aborted) return undefined;

  let model;
  try {
    model = createModel(timeline);
  } catch {
    return ctx.out.line('replay: unreadable timeline', 'error');
  }
  // A mutable copy the follow reader appends to; createModel rebuilds from it (model.js is untouched).
  const liveArr = (v) => (Array.isArray(v) ? v.slice() : []);
  let liveTimeline = {
    ...(timeline && typeof timeline === 'object' ? timeline : {}),
    entities: liveArr(timeline?.entities),
    segments: liveArr(timeline?.segments),
    generations: liveArr(timeline?.generations),
    events: liveArr(timeline?.events),
    scores: liveArr(timeline?.scores),
  };
  const clock = createClock({ duration: model.duration, speed: opts.speed ?? 1 });
  const nowFn = () => (ctx.now ? ctx.now() : performance.now());
  const timers = ctx.timers ?? { setInterval: globalThis.setInterval.bind(globalThis), clearInterval: globalThis.clearInterval.bind(globalThis) };

  let panel = 'none';
  let sel = -1;
  let ledger = null; // null until first requested
  let ledgerVersion = 0;
  let links = null; // null until GET /links resolves (or fails)
  let linksVersion = 0;
  let lastKey = null;
  let view = null;
  let timer = null;
  let closed = false;
  let follow = Boolean(opts.follow);
  let reader = null;
  let notice = null; // a one-line viewer notice (e.g. the server's row-cap error), shown in the header
  const doc = ctx.document ?? (typeof globalThis.document !== 'undefined' ? globalThis.document : null);

  if (opts.at !== undefined) clock.seek(opts.at);
  if (opts.at === undefined && !reducedMotion()) clock.play(nowFn());

  const stopTimer = () => {
    if (timer !== null) {
      timers.clearInterval(timer);
      timer = null;
    }
  };

  const redraw = (force = false) => {
    if (closed || !view) return;
    const ck = clock.state();
    const st = model.stateAt(ck.t);
    const key = `${ck.t.toFixed(2)}|${ck.playing}|${ck.speed}|${sel}|${panel}|${ledgerVersion}|${linksVersion}|${notice}`;
    if (!force && key === lastKey) return;
    lastKey = key;
    const size = ctx.gridSize?.() ?? { cols: 100, rows: 30 };
    try {
      view.draw(renderFrame({ model, st, clock: ck, sel, cols: size.cols, rows: size.rows, ledger, panel, links, notice }));
    } catch {
      // A frame that cannot be drawn ends the viewer rather than leaving it blank and wedged.
      close();
      ctx.out.line('replay: could not draw this timeline', 'error');
    }
  };

  const loadLinks = () => {
    Promise.resolve()
      .then(() => ctx.api.get('/links', undefined, { signal: ctx.signal() }))
      .then((body) => {
        if (closed) return;
        links = body && typeof body === 'object' ? body : null;
        linksVersion += 1;
        redraw();
      })
      .catch(() => {
        // No config: the viewer still opens; o/g become no-ops.
      });
  };

  const loadLedger = () => {
    const run = id.startsWith('run:') ? id.slice(4) : null;
    if (!run || !validRunId(run)) {
      ledger = { unavailable: true };
      return;
    }
    ledger = { loading: true };
    const opt = { signal: ctx.signal() };
    Promise.allSettled([
      ctx.api.get('/v1/ledger', { run_id: run, limit: 20 }, opt),
      ctx.api.get('/v1/ledger/verify', { run_id: run }, opt),
    ]).then(([list, ver]) => {
      if (closed) return;
      if (list.status !== 'fulfilled') ledger = { unavailable: true };
      else {
        const entries = list.value && Array.isArray(list.value.entries) ? list.value.entries.slice(0, 20) : [];
        ledger = { entries, verify: ver.status === 'fulfilled' ? ver.value : null };
      }
      ledgerVersion += 1;
      redraw();
    });
  };

  // ---- follow-live: an SSE reader appends items, the model is rebuilt and the playhead pinned ----
  const stopFollow = () => {
    if (reader) {
      reader.stop();
      reader = null;
    }
  };

  // The server's wall clock (epoch seconds) from its latest heartbeat, with the local monotonic
  // reading at arrival; follow pins to server time so a skewed browser clock cannot move the playhead.
  let serverSync = null;

  /** Pin target in session seconds: server now minus the session start, else the newest item time. */
  const followElapsed = () => {
    const t0 = Date.parse(model.session?.t0_iso ?? '');
    if (serverSync && Number.isFinite(t0)) {
      const serverNow = serverSync.server + (nowFn() - serverSync.at) / 1000;
      return serverNow - t0 / 1000;
    }
    return model.duration;
  };

  const followSeek = () => {
    // The total may have grown since the last frame; refresh it before clamping the pin.
    clock.setDuration(model.duration);
    clock.seek(Math.max(0, followElapsed() - 2));
  };

  // The live stream replays the whole timeline on every connect (first, `f` off/on, reconnect), so
  // each item is matched against what the model already holds: `have` counts items per key ever
  // accepted, `streamSeen` counts them on the current connection, and only the surplus is appended.
  const LIVE_NAMES = { entity: 'entities', segment: 'segments', generation: 'generations', event: 'events', score: 'scores' };
  const liveKey = (name, o) => {
    // An entity is keyed by its end and outcome, so the finished version of a known document is new.
    if (name === 'entity') return `entity:${o.doc_id}:${o.t_end}:${o.final_status}`;
    if (name === 'event') return `event:${o.t}:${o.doc_id}:${o.kind}:${o.station}`;
    if (name === 'score') return `score:${o.span_id}:${o.name}:${o.doc_id}`;
    if (o.span_id) return `${name}:${o.span_id}`;
    return `${name}:${o.doc_id}:${o.node}:${o.t0}:${o.t1}:${o.attempt}`;
  };
  const have = new Map();
  for (const [name, list] of Object.entries(LIVE_NAMES)) {
    for (const o of liveTimeline[list]) {
      if (o && typeof o === 'object') {
        const k = liveKey(name, o);
        have.set(k, (have.get(k) ?? 0) + 1);
      }
    }
  }
  let streamSeen = new Map();

  const applyLiveFrame = (name, obj) => {
    if (closed) return;
    // The server ends the stream after an `error` frame; do not reconnect into the same answer.
    if (name === 'error') {
      follow = false;
      stopFollow();
      if (obj && typeof obj === 'object' && obj.code === 'row_cap') {
        notice = 'live stopped: read cap reached, timeline truncated';
        redraw(true);
      }
      return;
    }
    if (name === 'heartbeat') {
      const server = obj && typeof obj === 'object' ? obj.t : undefined;
      if (typeof server === 'number' && Number.isFinite(server)) serverSync = { server, at: nowFn() };
      return;
    }
    const list = LIVE_NAMES[name];
    if (!list || !obj || typeof obj !== 'object' || Array.isArray(obj)) return;
    const key = liveKey(name, obj);
    const nth = (streamSeen.get(key) ?? 0) + 1;
    streamSeen.set(key, nth);
    if (nth <= (have.get(key) ?? 0)) return;
    have.set(key, nth);
    if (name === 'entity') {
      // Entities are upserted by document id: a new document is appended, a finished one replaces its entry.
      const at = liveTimeline.entities.findIndex((e) => e && e.doc_id === obj.doc_id);
      if (at >= 0) liveTimeline.entities[at] = obj;
      else liveTimeline.entities.push(obj);
    } else liveTimeline[list].push(obj);
    boundLiveTimeline(liveTimeline);
    model = createModel(liveTimeline);
    clock.setDuration(model.duration);
    redraw(true);
  };

  const startFollow = () => {
    if (closed || reader || !liveTimeline) return;
    const fetchFn = ctx.fetch ?? globalThis.fetch;
    if (typeof fetchFn !== 'function') return;
    const headers = typeof ctx.api?.authHeaders === 'function' ? ctx.api.authHeaders() : {};
    streamSeen = new Map();
    reader = createFollowReader({
      fetchFn,
      url: `/v1/replay/live?session=${encodeURIComponent(id)}`,
      headers,
      signal: ctx.signal(),
      onFrame: applyLiveFrame,
    });
  };

  const setFollow = (on) => {
    if (closed) return;
    follow = Boolean(on);
    if (follow) {
      startFollow();
      followSeek();
      redraw(true);
    } else {
      stopFollow();
    }
  };

  function onVisibility() {
    if (closed) return;
    if (doc && doc.hidden) stopFollow();
    else if (follow) startFollow();
  }

  const close = () => {
    if (closed) return;
    closed = true;
    stopTimer();
    stopFollow();
    if (doc && typeof doc.removeEventListener === 'function') {
      doc.removeEventListener('visibilitychange', onVisibility);
    }
    if (view) view.release();
  };

  const docCount = () => (Array.isArray(model.entities) ? model.entities.length : 0);

  const openExternal = (url) => {
    if (!url) return;
    (ctx.open ?? globalThis.open)?.(url, '_blank', 'noopener');
  };

  // none -> metrics -> tokens -> decisions -> latency -> fields -> none.
  // From the inspector or ledger, p starts the insight cycle at its first panel.
  const cyclePanel = () => {
    const ids = listPanels().map((p) => p.id);
    const at = ids.indexOf(panel);
    panel = at < 0 ? (ids[0] ?? 'none') : at + 1 < ids.length ? ids[at + 1] : 'none';
  };

  function onKey(e) {
    if (closed || !e || typeof e.key !== 'string') return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const k = e.key;
    const big = e.shiftKey ? STEP_BIG_S : STEP_S;
    const dur = clock.state().duration;
    // A backward scrub (or a jump/seek) leaves follow; `f` re-enters.
    if (follow && (k === 'ArrowLeft' || k === 'Home' || /^[0-9]$/.test(k))) setFollow(false);
    if (k === ' ' || k === 'Spacebar') clock.toggle(nowFn());
    else if (k === 'ArrowLeft') clock.step(-big);
    else if (k === 'ArrowRight') clock.step(big);
    else if (k === '[' || k === '-') clock.slower(nowFn());
    else if (k === ']' || k === '+' || k === '=') clock.faster(nowFn());
    else if (k === 'Home') clock.seek(0);
    else if (k === 'End') clock.seek(dur);
    else if (/^[0-9]$/.test(k)) clock.seek((dur * Number(k)) / 10);
    else if (k === 'ArrowDown' || k === 'j') {
      const n = docCount();
      sel = n ? Math.min(n - 1, sel + 1) : -1;
    } else if (k === 'ArrowUp' || k === 'k') {
      const n = docCount();
      sel = n ? Math.max(0, sel - 1) : -1;
    } else if (k === 'Enter' || k === 'i') panel = panel === 'inspector' ? 'none' : 'inspector';
    else if (k === 'l') {
      panel = panel === 'ledger' ? 'none' : 'ledger';
      if (panel === 'ledger' && (ledger === null || ledger.unavailable)) loadLedger();
    } else if (k === 'p') cyclePanel();
    else if (k === 'o') openExternal(externalUrls(links, id).phoenix);
    else if (k === 'g') openExternal(externalUrls(links, id).grafana);
    else if (k === 'e') {
      const t = clock.state().t;
      const next = (model.eventsBetween(t, dur) || []).find((x) => x && typeof x.t === 'number' && x.t > t);
      if (next) clock.seek(next.t);
    } else if (k === 'f') setFollow(!follow);
    else if (k === 'Escape' || k === 'q') {
      close();
      return;
    } else return;
    redraw();
  }

  view = ctx.takeover({ onKey, label: `replay ${id}` });
  if (!view) return ctx.out.line('replay: another viewer is already open', 'error');
  loadLinks();
  if (doc && typeof doc.addEventListener === 'function') {
    doc.addEventListener('visibilitychange', onVisibility);
  }
  if (follow) startFollow();

  const sig = ctx.signal();
  const onAbort = () => {
    closed = true;
    stopTimer();
    stopFollow();
  };
  try {
    redraw(true);
    if (!closed) {
      timer = timers.setInterval(() => {
        clock.tick(nowFn());
        if (follow) followSeek();
        redraw();
      }, TICK_MS);
    }
    if (sig.aborted) onAbort();
    else sig.addEventListener('abort', onAbort, { once: true });
    await view.done;
  } finally {
    // Every exit path releases the keyboard, the timer and the live reader.
    close();
    sig.removeEventListener('abort', onAbort);
  }
  if (!sig.aborted) ctx.out.line('replay closed', 'dim');
  return undefined;
}

export function registerReplay(registry) {
  registry.register({
    name: 'replay',
    summary: 'replay a pipeline run on a timeline',
    usage: 'replay [--limit N] | replay <run_id|session id> [--at SECONDS] [--speed N] [--follow]',
    man: manPages.replay,
    async run(ctx, args, flags) {
      if (args.length === 0) {
        const extra = Object.keys(flags).filter((k) => k !== 'limit');
        if (extra.length) return ctx.out.line(USAGE, 'error');
        return listSessions(ctx, flags);
      }
      if (args.length > 1) return ctx.out.line(USAGE, 'error');
      if (flags.limit !== undefined) return ctx.out.line('replay: --limit only applies to the list', 'error');
      return openViewer(ctx, args[0], flags);
    },
  });
}
