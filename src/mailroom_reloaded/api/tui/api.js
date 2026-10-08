// Fetch client for /tui. The bearer token lives only in session storage (never in the URL,
// never in localStorage) and is sent only as an Authorization header.

const TOKEN_KEY = 'mailroom.tui.token';

export class ApiError extends Error {
  /**
   * @param {string} message
   * @param {{status?: number|null, kind: 'offline'|'unauthorized'|'http', detail?: string}} info
   */
  constructor(message, { status = null, kind, detail } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.kind = kind;
    if (detail !== undefined) this.detail = detail;
  }
}

function readStorage(storage) {
  try {
    return storage ? storage.getItem(TOKEN_KEY) : null;
  } catch {
    return null;
  }
}

function writeStorage(storage, value) {
  try {
    if (!storage) return;
    if (value === null) storage.removeItem(TOKEN_KEY);
    else storage.setItem(TOKEN_KEY, value);
  } catch {
    // Storage can be blocked (private windows); the in-memory token still works.
  }
}

function buildQuery(query) {
  if (!query) return '';
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    params.append(key, String(value));
  }
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

async function readDetail(res) {
  try {
    const body = await res.json();
    if (body && typeof body.detail === 'string') return body.detail;
    if (body && body.detail !== undefined) return JSON.stringify(body.detail);
    return '';
  } catch {
    return '';
  }
}

/**
 * @param {{fetchImpl?: typeof fetch, storage?: Storage|null, base?: string}} [options]
 */
export function createApi({
  fetchImpl = globalThis.fetch,
  storage = globalThis.sessionStorage,
  base = '',
} = {}) {
  let token = readStorage(storage) || null;

  function authHeaders() {
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  async function request(method, path, { query, json, form } = {}) {
    const headers = { Accept: 'application/json', ...authHeaders() };
    const init = { method, headers };
    if (form !== undefined) {
      init.body = form;
    } else if (json !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(json);
    }

    let res;
    try {
      res = await fetchImpl(`${base}${path}${buildQuery(query)}`, init);
    } catch {
      throw new ApiError('no api connection', { kind: 'offline' });
    }

    if (res.status === 401) {
      throw new ApiError('api token required', { status: 401, kind: 'unauthorized' });
    }
    if (!res.ok) {
      const detail = await readDetail(res);
      throw new ApiError(detail || `http ${res.status}`, {
        status: res.status,
        kind: 'http',
        detail,
      });
    }
    if (res.status === 204) return null;
    try {
      return await res.json();
    } catch {
      return null;
    }
  }

  return {
    get(path, query) {
      return request('GET', path, { query });
    },
    post(path, body) {
      return request('POST', path, { json: body === undefined ? undefined : body });
    },
    upload(file) {
      const form = new FormData();
      form.append('file', file);
      return request('POST', '/v1/documents', { form });
    },
    setToken(value) {
      token = String(value);
      writeStorage(storage, token);
    },
    clearToken() {
      token = null;
      writeStorage(storage, null);
    },
    hasToken() {
      return token !== null;
    },
    async health() {
      try {
        const res = await fetchImpl(`${base}/health`, { headers: { Accept: 'application/json' } });
        return { ok: res.ok, status: res.status };
      } catch {
        return { ok: false, status: null };
      }
    },
  };
}
