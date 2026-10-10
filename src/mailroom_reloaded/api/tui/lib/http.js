// Small JSON client for new /tui commands: a 2 s default timeout, the session's bearer token
// (read from the existing api client, so the token has one home), and typed errors whose
// `hint` is what a command prints. Status probes and summaries use this; uploads and the
// long-running replay calls keep the main api client (api.js) and its longer timeout.

export const HTTP_TIMEOUT_MS = 2000;

export class HttpError extends Error {
  /**
   * @param {string} message
   * @param {{kind: 'offline'|'timeout'|'unauthorized'|'forbidden'|'not_found'|'http'|'parse'|'aborted',
   *   status?: number|null}} info
   */
  constructor(message, { kind, status = null } = {}) {
    super(message);
    this.name = 'HttpError';
    this.kind = kind;
    this.status = status;
  }
}

const KIND_BY_STATUS = { 401: 'unauthorized', 403: 'forbidden', 404: 'not_found' };

/** The one line a command prints for a failure; 401 always says to run `auth`. */
export function errorHint(err) {
  const kind = err && err.kind;
  if (kind === 'unauthorized') return "401 — run 'auth <token>'";
  if (kind === 'forbidden') return '403 — not allowed by this server';
  if (kind === 'timeout') return 'timed out — is the api up?';
  if (kind === 'offline') return 'api unreachable — mailroom closed';
  if (kind === 'not_found') return 'not found';
  if (kind === 'parse') return 'bad response from the api';
  if (kind === 'aborted') return 'cancelled';
  return err && err.message ? String(err.message) : 'request failed';
}

function query(q) {
  if (!q || typeof q !== 'object') return '';
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) {
    if (v !== undefined && v !== null) params.append(k, String(v));
  }
  const s = params.toString();
  return s ? `?${s}` : '';
}

/**
 * @param {{fetchImpl?: typeof fetch, authHeaders?: () => Record<string, string>, base?: string,
 *   timeoutMs?: number}} [options]
 */
export function createHttp({
  fetchImpl = globalThis.fetch,
  authHeaders = () => ({}),
  base = '',
  timeoutMs = HTTP_TIMEOUT_MS,
} = {}) {
  async function request(method, path, { query: q, json, signal, timeoutMs: t = timeoutMs } = {}) {
    if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//')) {
      throw new HttpError('path must be same-origin and start with /', { kind: 'http' });
    }
    const ctl = new AbortController();
    let timedOut = false;
    const onAbort = () => ctl.abort();
    if (signal) {
      if (signal.aborted) ctl.abort();
      else signal.addEventListener('abort', onAbort, { once: true });
    }
    const timer = setTimeout(() => {
      timedOut = true;
      ctl.abort();
    }, t);
    const headers = { Accept: 'application/json', ...(authHeaders() || {}) };
    const init = { method, headers, signal: ctl.signal };
    if (json !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(json);
    }
    try {
      let res;
      try {
        res = await fetchImpl(`${base}${path}${query(q)}`, init);
      } catch {
        if (signal && signal.aborted) throw new HttpError('aborted', { kind: 'aborted' });
        if (timedOut) throw new HttpError(`timed out after ${t} ms`, { kind: 'timeout' });
        throw new HttpError('no api connection', { kind: 'offline' });
      }
      if (!res.ok) {
        const kind = KIND_BY_STATUS[res.status] || 'http';
        throw new HttpError(`http ${res.status}`, { kind, status: res.status });
      }
      if (res.status === 204) return null;
      try {
        return await res.json();
      } catch {
        if (timedOut) throw new HttpError(`timed out after ${t} ms`, { kind: 'timeout' });
        throw new HttpError('response is not JSON', { kind: 'parse', status: res.status });
      }
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
    }
  }

  return {
    get(path, q, opts = {}) {
      return request('GET', path, { query: q, signal: opts.signal, timeoutMs: opts.timeoutMs });
    },
    post(path, body, opts = {}) {
      return request('POST', path, { json: body, signal: opts.signal, timeoutMs: opts.timeoutMs });
    },
  };
}

/** A client for a command's ctx: the session token from ctx.api, the command's abort signal. */
export function httpFor(ctx, options = {}) {
  const api = ctx && ctx.api;
  const client = createHttp({
    ...options,
    authHeaders: () => (api && typeof api.authHeaders === 'function' ? api.authHeaders() : {}),
  });
  const sig = () => (ctx && typeof ctx.signal === 'function' ? ctx.signal() : undefined);
  return {
    get: (path, q, opts = {}) => client.get(path, q, { signal: sig(), ...opts }),
    post: (path, body, opts = {}) => client.post(path, body, { signal: sig(), ...opts }),
  };
}
