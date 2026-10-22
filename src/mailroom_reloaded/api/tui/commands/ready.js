// Ready command for /tui: render GET /ready as a component table, optionally re-polled.
// `ready --watch` and `watch ready` poll with lib/poll.js.
// Every printed value comes from the response and goes through ctx.out (textContent).

import { createPoller } from '../lib/poll.js';
import { errorHint, httpFor } from '../lib/http.js';

const text = (value) => value === null || value === undefined || value === '' ? '—' : String(value);

const STATUS_CLASS = { ok: 'success', degraded: 'warn', down: 'error', unconfigured: 'dim' };
const DEFAULT_INTERVAL = 5;
const READY_TIMEOUT_MS = 3000;

const manPages = {
  ready: `NAME
    ready — show readiness of each backend component

SYNOPSIS
    ready [--watch [--interval N]]
    health --all
    watch ready [--interval N]

DESCRIPTION
    Reads GET /ready: the aggregate status (ok, degraded or down) and one row per
    component with its status and probe latency in milliseconds. Status is coloured:
    ok green, degraded amber, down red, unconfigured dim.

    The component rows are returned only when the api has no token configured or
    the session sends the right one. Without them the aggregate still prints and the
    hint says to run 'auth <token>'. A 503 (down) still shows the rows.

    --watch re-polls every N seconds (1-60, default 5) and prints the table again
    only when a component or the aggregate changes. A failed poll prints once and
    polling continues. Stops on Ctrl+C.`,
};

/** Colour class for a status word; unknown or missing words get no class. */
export function readyClass(status) {
  return typeof status === 'string' && Object.hasOwn(STATUS_CLASS, status) ? STATUS_CLASS[status] : '';
}

function componentRow(c) {
  const x = c && typeof c === 'object' && !Array.isArray(c) ? c : {};
  const latency = Number.isFinite(x.latency_ms) ? `${Math.round(x.latency_ms * 10) / 10} ms` : '—';
  return { name: text(x.name), status: text(x.status), cls: readyClass(x.status), latency };
}

/**
 * The parts of a /ready body the command shows: the aggregate status and the component rows,
 * or null when the body carries none (anonymous or wrong token). Never throws.
 */
export function readyView(res) {
  const r = res && typeof res === 'object' && !Array.isArray(res) ? res : {};
  const components = Array.isArray(r.components) ? r.components.map(componentRow) : null;
  return { status: typeof r.status === 'string' ? r.status : null, components };
}

/** A string that changes only when a status changes (latency is excluded so it does not re-print). */
function viewKey(view) {
  return JSON.stringify([view.status, view.components?.map((component) => [component.name, component.status])]);
}

async function fetchReady(ctx, deps, signal) {
  try {
    const body = await httpFor(ctx, { fetchImpl: deps.fetchImpl }).get('/ready', undefined, { signal, timeoutMs: READY_TIMEOUT_MS });
    return { res: body };
  } catch (err) {
    // /ready answers 503 with its body when the aggregate is down; that body is a result, not an error.
    if (err && err.kind === 'http' && err.status === 503 && err.body && typeof err.body === 'object') {
      return { res: err.body };
    }
    return { err };
  }
}

function printError(ctx, err) {
  if (err && err.kind === 'aborted') return;
  const hint = err && err.kind === 'not_found' ? 'not found — this api has no /ready' : errorHint(err);
  ctx.out.line(`ready: ${hint}`, 'error');
}

function printView(ctx, view, hasToken) {
  ctx.out.line(`ready — ${text(view.status)}`, readyClass(view.status) || 'dim');
  if (view.components === null) {
    const hint = hasToken
      ? "component detail hidden — the token was not accepted; run 'auth <token>'"
      : "component detail needs a token — run 'auth <token>'";
    ctx.out.line(hint, 'dim');
    return;
  }
  if (view.components.length === 0) {
    ctx.out.line('no components reported', 'dim');
    return;
  }
  const rows = view.components.map((c) => [c.name, c.status, c.latency]);
  ctx.out.table(['component', 'status', 'latency'], rows, {
    cellClass: (row, col) => (col === 1 ? view.components[row].cls : ''),
  });
}

function hasToken(ctx) {
  return Boolean(ctx.api && typeof ctx.api.hasToken === 'function' && ctx.api.hasToken());
}

async function readOnce(ctx, deps) {
  const { res, err } = await fetchReady(ctx, deps, ctx.signal());
  if (ctx.signal().aborted) return;
  if (err) return printError(ctx, err);
  return printView(ctx, readyView(res), hasToken(ctx));
}

/** Re-poll until Ctrl+C. The returned promise settles when the command's signal aborts. */
function watchReady(ctx, deps, seconds) {
  const signal = ctx.signal();
  let last = null;
  ctx.out.line(`watching ready · every ${seconds}s · ctrl+c to stop`, 'dim');

  const tick = async (pollSignal) => {
    const { res, err } = await fetchReady(ctx, deps, pollSignal);
    if (signal.aborted) return;
    if (err) {
      const key = `error:${err.kind || 'unknown'}:${err.status ?? ''}`;
      if (key !== last) {
        last = key;
        printError(ctx, err);
      }
      return;
    }
    const view = readyView(res);
    const key = viewKey(view);
    if (key === last) return;
    last = key;
    printView(ctx, view, hasToken(ctx));
  };

  return new Promise((resolve) => {
    const poller = createPoller(tick, {
      intervalMs: seconds * 1000,
      signal,
      timers: deps.timers,
      doc: deps.doc,
      onError: (err) => {
        ctx.out.line(`ready: ${text(err && err.message)}`, 'error');
      },
    });
    const done = () => {
      poller.stop();
      resolve();
    };
    if (signal.aborted) return done();
    signal.addEventListener('abort', done, { once: true });
    poller.start();
    return undefined;
  }).then(() => {
    ctx.out.line('ready: watch stopped', 'dim');
  });
}

/** Register `ready`. `deps` is injectable for tests: fetchImpl, timers and doc (for the poller). */
export function registerReady(registry, deps = {}) {
  const spec = {
    name: 'ready',
    summary: 'show backend readiness',
    usage: 'ready [--watch [--interval N]]',
    man: manPages.ready,
    async run(ctx, args, flags = {}) {
      if (Object.keys(flags).some((flag) => flag !== 'watch' && flag !== 'interval')) {
        return ctx.out.line('ready: usage: ready [--watch [--interval N]]', 'error');
      }
      if (args.length > 0) return ctx.out.line('ready: usage: ready [--watch [--interval N]]', 'error');
      if (flags.watch !== undefined && flags.watch !== true) {
        return ctx.out.line('ready: --watch takes no value', 'error');
      }
      if (flags.watch === undefined) {
        if (flags.interval !== undefined) return ctx.out.line('ready: --interval needs --watch', 'error');
        return readOnce(ctx, deps);
      }
      const raw = flags.interval;
      const interval = raw === undefined ? DEFAULT_INTERVAL : typeof raw === 'string' && /^\d+$/.test(raw) ? Number(raw) : NaN;
      if (!Number.isInteger(interval) || interval < 1 || interval > 60) {
        return ctx.out.line('ready: --interval must be a whole number from 1 to 60', 'error');
      }
      return watchReady(ctx, deps, interval);
    },
  };
  registry.register(spec);
  const health = registry.get('health');
  if (health) {
    const runHealth = health.run;
    health.usage = 'health [--all]';
    health.man += "\n    health --all shows backend readiness (see man ready).\n";
    health.run = (ctx, args, flags = {}) => {
      if (!Object.hasOwn(flags, 'all')) return runHealth(ctx, args, flags);
      if (flags.all !== true || args.length || Object.keys(flags).length !== 1) {
        return ctx.out.line('health: usage: health [--all]', 'error');
      }
      return readOnce(ctx, deps);
    };
  }
  const watch = registry.get('watch');
  if (watch) {
    const runWatch = watch.run;
    watch.usage = 'watch [ready] [--interval N]';
    watch.man += "\n    watch ready follows backend readiness (see man ready).\n";
    watch.run = (ctx, args, flags = {}) => args[0] === 'ready'
      ? spec.run(ctx, args.slice(1), { ...flags, watch: true })
      : runWatch(ctx, args, flags);
  }
}
