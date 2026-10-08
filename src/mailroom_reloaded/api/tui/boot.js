// Boot sequence for /tui: banner, neofetch line, then real API checks.
// Pure of DOM and fetch: output goes through ctx.out, the API through ctx.api and the
// status bar through ctx.setStatus. A check prints [ ok ] only after it succeeded.

export const BANNER_MS = 900;
export const LINE_MS = 160;

const CLOSED = 'mailroom closed — no api connection';
const LOCKED_STATUS = 'locked — api token required';
const TOKEN_LINE = "[ !! ] api token required — type 'auth <token>'";
const UNREACHABLE_LINE = '[ !! ] api unreachable';
const HELP_HINT = "type 'help' to begin.";

function defaultSleep(ms, signal) {
  return new Promise((resolve) => {
    if (signal && signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', done);
      resolve();
    }
    if (signal) signal.addEventListener('abort', done, { once: true });
  });
}

function reasonOf(err) {
  if (err && err.kind === 'offline') return 'api unreachable';
  if (err && err.kind === 'unauthorized') return 'api token required';
  if (err && err.kind === 'http') {
    return typeof err.detail === 'string' && err.detail !== '' ? err.detail : `http ${err.status}`;
  }
  return 'failed';
}

/**
 * Run the boot sequence and report the resulting state.
 * @param {{out: object, api: object, setStatus: (key: string, value: string) => void}} ctx
 * @param {{reducedMotion?: boolean, signal?: AbortSignal, banner?: string,
 *          bannerCompact?: string, compact?: boolean,
 *          sleep?: (ms: number, signal?: AbortSignal) => Promise<void>}} [opts]
 * @returns {Promise<{state: 'live'|'locked'|'closed'}>}
 */
export async function boot(ctx, opts = {}) {
  const { reducedMotion = false, signal, banner = '', bannerCompact = '', compact = false } = opts;
  const sleep = typeof opts.sleep === 'function' ? opts.sleep : defaultSleep;
  const { out, api, setStatus } = ctx;

  // Delays are skipped under reduced motion and once the signal (key or click) has fired.
  const pause = async (ms) => {
    if (reducedMotion || (signal && signal.aborted)) return;
    await sleep(ms, signal);
  };
  const say = async (text, cls) => {
    await pause(LINE_MS);
    out.line(text, cls);
  };
  const failLine = (label, err) => {
    if (err && err.kind === 'offline') return say(UNREACHABLE_LINE, 'error');
    return say(`[ !! ] ${label} · ${reasonOf(err)}`, 'error');
  };
  const finish = async (state) => {
    await say(HELP_HINT, 'amber');
    return { state };
  };
  const closed = async () => {
    setStatus('api', CLOSED);
    await say(CLOSED, 'error');
    return finish('closed');
  };

  out.banner(compact ? bannerCompact : banner);
  await pause(BANNER_MS);
  await say('mailroom@floor — mailroom-reloaded visual engine');
  await say('boot: tty · crt on · theme amber', 'dim');

  // api /health
  let health;
  try {
    health = await api.health();
  } catch {
    health = { ok: false, status: null };
  }
  if (!health || !health.ok) {
    await say(UNREACHABLE_LINE, 'error');
    return closed();
  }
  await say('[ ok ] api /health', 'success');

  // auth: a cheap authenticated call; 401 means the token is missing or rejected.
  try {
    await api.get('/v1/documents', { limit: 1 });
  } catch (err) {
    if (err && err.kind === 'unauthorized') {
      await say(TOKEN_LINE, 'error');
      setStatus('api', LOCKED_STATUS);
      return finish('locked');
    }
    await failLine('auth', err);
    return closed();
  }
  await say('[ ok ] auth', 'success');
  setStatus('api', 'live');

  // catalog count: the limit=1 probe above returns at most one row, so count with a wider page.
  let catalog;
  try {
    catalog = await api.get('/v1/documents', { limit: 500 });
  } catch (err) {
    if (err && err.kind === 'unauthorized') {
      await say(TOKEN_LINE, 'error');
      setStatus('api', LOCKED_STATUS);
      return finish('locked');
    }
    await failLine('catalog', err);
    return closed();
  }
  const docCount =
    catalog && typeof catalog.count === 'number'
      ? catalog.count
      : catalog && Array.isArray(catalog.documents)
        ? catalog.documents.length
        : 0;
  await say(`[ ok ] catalog · ${docCount} documents`, 'success');

  // eval runs: optional detail, a failure here does not close the terminal.
  try {
    const runs = await api.get('/v1/runs');
    const runCount = runs && Array.isArray(runs.runs) ? runs.runs.length : 0;
    await say(`[ ok ] eval runs · ${runCount}`, 'success');
  } catch (err) {
    await say(`[ !! ] eval runs · ${reasonOf(err)}`, 'error');
  }

  return finish('live');
}
