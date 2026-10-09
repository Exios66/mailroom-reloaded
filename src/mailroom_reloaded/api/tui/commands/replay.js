// Replay command for /tui: list sessions or open the full-screen replay viewer.
// Reads GET /v1/replay/*; the ledger panel lazily reads GET /v1/ledger and /v1/ledger/verify.
// Every printed value goes through ctx.out / the grid (text only).

import { fail, flagText, parseIntFlag, text, validRunId } from './pipeline.js';
import { SPEEDS, createClock } from '../replay/clock.js';
import { createModel } from '../replay/model.js';
import { listPanels } from '../replay/panels.js';
import { fmtTime, renderFrame } from '../replay/grid.js';

const PREFIXES = new Set(['run', 'session', 'doc', 'window']);
const TAIL_RE = /^[A-Za-z0-9._:-]+$/;
const USAGE = 'replay: usage: replay [--limit N] | replay <run_id|session id> [--at SECONDS] [--speed N]';
const TICK_MS = 100;
const STEP_S = 5;
const STEP_BIG_S = 30;
const NUM_RE = /^\d+(\.\d+)?$/;

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
    replay <run_id|session id> [--at SECONDS] [--speed N]

DESCRIPTION
    Bare 'replay' lists recent sessions from GET /v1/replay/sessions: id, docs,
    start, duration, source and whether retention pruned the data (--limit 1-100).
    'replay <id>' reads GET /v1/replay/sessions/<id>/timeline and opens a
    full-screen text viewer. A bare run id means run:<id>. --at starts paused at
    that second; --speed is one of 0.5 1 2 4 8 16. The 'l' panel reads
    GET /v1/ledger and /v1/ledger/verify for the run, only when first opened.
    With prefers-reduced-motion the viewer starts paused.

KEYS
    space        play / pause           left/right   step -/+5s (shift: 30s)
    [ ] or - +   slower / faster        Home / End   seek to start / end
    0-9          seek to 0%..90%        up/down j/k  select document
    Enter or i   toggle inspector       l            toggle ledger panel
    p            cycle insight panels   e            jump to next event
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
  const unknown = Object.keys(flags).filter((k) => !['at', 'speed'].includes(k));
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
  return out;
}

function reducedMotion() {
  try {
    return Boolean(globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
  } catch {
    return false;
  }
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
  const clock = createClock({ duration: model.duration, speed: opts.speed ?? 1 });
  const nowFn = () => (ctx.now ? ctx.now() : performance.now());
  const timers = ctx.timers ?? { setInterval: globalThis.setInterval.bind(globalThis), clearInterval: globalThis.clearInterval.bind(globalThis) };

  let panel = 'none';
  let sel = -1;
  let ledger = null; // null until first requested
  let ledgerVersion = 0;
  let lastKey = null;
  let view = null;
  let timer = null;
  let closed = false;

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
    const key = `${ck.t.toFixed(2)}|${ck.playing}|${ck.speed}|${sel}|${panel}|${ledgerVersion}`;
    if (!force && key === lastKey) return;
    lastKey = key;
    const size = ctx.gridSize?.() ?? { cols: 100, rows: 30 };
    try {
      view.draw(renderFrame({ model, st, clock: ck, sel, cols: size.cols, rows: size.rows, ledger, panel }));
    } catch {
      // A frame that cannot be drawn ends the viewer rather than leaving it blank and wedged.
      close();
      ctx.out.line('replay: could not draw this timeline', 'error');
    }
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

  const close = () => {
    if (closed) return;
    closed = true;
    stopTimer();
    if (view) view.release();
  };

  const docCount = () => (Array.isArray(model.entities) ? model.entities.length : 0);

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
    else if (k === 'e') {
      const t = clock.state().t;
      const next = (model.eventsBetween(t, dur) || []).find((x) => x && typeof x.t === 'number' && x.t > t);
      if (next) clock.seek(next.t);
    } else if (k === 'Escape' || k === 'q') {
      close();
      return;
    } else return;
    redraw();
  }

  view = ctx.takeover({ onKey, label: `replay ${id}` });
  if (!view) return ctx.out.line('replay: another viewer is already open', 'error');

  const sig = ctx.signal();
  const onAbort = () => {
    closed = true;
    stopTimer();
  };
  try {
    redraw(true);
    if (!closed) {
      timer = timers.setInterval(() => {
        clock.tick(nowFn());
        redraw();
      }, TICK_MS);
    }
    if (sig.aborted) onAbort();
    else sig.addEventListener('abort', onAbort, { once: true });
    await view.done;
  } finally {
    // Every exit path releases the keyboard and the timer.
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
    usage: 'replay [--limit N] | replay <run_id|session id> [--at SECONDS] [--speed N]',
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
