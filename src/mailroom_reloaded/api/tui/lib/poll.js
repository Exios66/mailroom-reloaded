// Visibility-aware, abortable poller for /tui commands. Ticks never overlap (the next one is
// scheduled after the previous resolves), a hidden tab skips ticks until it is visible again,
// and aborting the signal (or calling stop) cancels the pending tick. Errors go to onError and
// polling continues. Timers and the document are injectable for `node --test`.

/**
 * @param {(signal: AbortSignal) => any} fn  the work for one tick
 * @param {{intervalMs?: number, signal?: AbortSignal, onError?: (err: unknown) => void,
 *   doc?: {hidden?: boolean, addEventListener?: Function, removeEventListener?: Function} | null,
 *   timers?: {setTimeout: Function, clearTimeout: Function}, immediate?: boolean}} [options]
 */
export function createPoller(fn, {
  intervalMs = 5000,
  signal,
  onError,
  doc = globalThis.document ?? null,
  timers = globalThis,
  immediate = true,
} = {}) {
  if (typeof fn !== 'function') throw new TypeError('poller needs a function');
  const every = Number.isFinite(intervalMs) && intervalMs >= 100 ? intervalMs : 5000;
  let ctl = null;
  let timer = null;
  let running = false;
  let inFlight = false;
  let missed = false; // a tick came due while hidden
  let ticks = 0;

  const hidden = () => Boolean(doc && doc.hidden);

  function schedule(ms) {
    if (!running) return;
    timer = timers.setTimeout(tick, ms);
  }

  async function tick() {
    timer = null;
    if (!running) return;
    if (hidden()) {
      missed = true; // resume on visibilitychange
      return;
    }
    inFlight = true;
    try {
      ticks++;
      await fn(ctl.signal);
    } catch (err) {
      if (!ctl.signal.aborted && typeof onError === 'function') {
        try {
          onError(err);
        } catch {
          // A failing error handler must not stop the poller.
        }
      }
    } finally {
      inFlight = false;
    }
    schedule(every);
  }

  function onVisibility() {
    if (running && missed && !hidden() && !inFlight && timer === null) {
      missed = false;
      schedule(0);
    }
  }

  function stop() {
    if (!running) return;
    running = false;
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
    ctl.abort();
    doc?.removeEventListener?.('visibilitychange', onVisibility);
    signal?.removeEventListener?.('abort', stop);
  }

  return {
    start() {
      if (running || signal?.aborted) return;
      running = true;
      missed = false;
      ctl = new AbortController();
      doc?.addEventListener?.('visibilitychange', onVisibility);
      signal?.addEventListener?.('abort', stop, { once: true });
      schedule(immediate ? 0 : every);
    },
    stop,
    get running() {
      return running;
    },
    /** How many times `fn` has been called. */
    get ticks() {
      return ticks;
    },
  };
}
