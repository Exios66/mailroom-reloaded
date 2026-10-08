// Pipeline commands for /tui: thin wrappers over the /v1 API.
// Every printed value comes from a live response and goes through ctx.out (textContent).
// Shapes verified against src/mailroom_reloaded/api/app.py.

export const ACCEPT = '.txt,.md,.pdf,.docx,.rtf,.html,.htm';
const WATCH_LIMIT = 500;
export const EMPTY_LS = "no documents — drop a file in the inbox or run 'upload'";

const STATUS_CLASS = {
  archived: 'success',
  failed: 'error',
  parked: 'warn',
  review: 'warn',
  processing: 'info',
  new: 'info',
};

export function statusClass(status) {
  return typeof status === 'string' && Object.hasOwn(STATUS_CLASS, status)
    ? STATUS_CLASS[status]
    : '';
}

const DOC_ID_RE = /^[A-Za-z0-9_-]+$/;

/** Doc ids are hex-ish tokens; run ids may also contain dots but never '.' or '..'. */
export function validDocId(id) {
  return typeof id === 'string' && DOC_ID_RE.test(id);
}

export function validRunId(id) {
  return (
    typeof id === 'string' && id !== '' && id !== '.' && id !== '..' && /^[A-Za-z0-9._-]+$/.test(id)
  );
}

function text(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/**
 * Print an api failure in the plan's wording. Returns true when handled;
 * unknown errors are rethrown so the engine prints `<cmd>: <message>`.
 */
function fail(ctx, cmd, err, { notFound } = {}) {
  const kind = err && err.kind;
  if (kind === 'aborted') return;
  if (kind === 'offline') {
    ctx.out.line(`${cmd}: api unreachable — mailroom closed`, 'error');
  } else if (kind === 'unauthorized') {
    ctx.out.line(`${cmd}: 401 — type 'auth <token>'`, 'error');
  } else if (kind === 'http' && err.status === 404 && notFound) {
    ctx.out.line(`${cmd}: no such document ${notFound}`, 'error');
  } else if (kind === 'http') {
    ctx.out.line(`${cmd}: ${err.message}`, 'error');
  } else {
    throw err;
  }
}

const enc = encodeURIComponent;

const GATE_ACTION_CLASS = { proceed: 'success', verify: 'warn', park: 'warn', human_review: 'warn', reject: 'error' };

function num(v) {
  return typeof v === 'number' && Number.isFinite(v) ? String(Math.round(v * 1000) / 1000) : null;
}

/** Format one gate_decision audit entry; never throws on odd payloads. */
export function gateLine(entry) {
  const p = entry && entry.payload && typeof entry.payload === 'object' ? entry.payload : {};
  const stage = String(entry && entry.node ? entry.node : 'gate').replace(/^gate_/, 'gate ').replace(/_/g, ' ');
  const source = typeof p.source === 'string' && p.source ? p.source : '—';
  const conf = num(p.confidence);
  let t = `${stage} -> ${text(p.action)} [${source}]`;
  if (conf !== null) t += ` conf ${conf}`;
  if (p.reason) t += ` — ${text(p.reason)}`;
  const action = typeof p.action === 'string' && Object.hasOwn(GATE_ACTION_CLASS, p.action) ? GATE_ACTION_CLASS[p.action] : '';
  return { t, cls: source === 'jev' ? action || 'info' : action || 'dim', source };
}

function gateEntries(entries) {
  return (Array.isArray(entries) ? entries : []).filter((e) => e && e.event === 'gate_decision');
}

function flagText(flags, name) {
  const v = flags[name];
  return typeof v === 'string' ? v : undefined;
}

function parseIntFlag(flags, name, min, max) {
  if (flags[name] === undefined) return { value: undefined };
  const raw = flags[name];
  const n = typeof raw === 'string' && /^\d+$/.test(raw) ? Number(raw) : NaN;
  if (!Number.isInteger(n) || n < min || n > max) {
    return { error: `--${name} must be a whole number from ${min} to ${max}` };
  }
  return { value: n };
}

function docTable(ctx, docs) {
  const rows = docs.map((d) => [d.doc_id, d.filename, d.doc_type || '—', d.status]);
  // The third argument lets the renderer colour the status column; the base renderer ignores it.
  ctx.out.table(['doc_id', 'filename', 'type', 'status'], rows, {
    cellClass: (row, col) => (col === 3 ? statusClass(docs[row].status) : ''),
  });
  ctx.out.line(`${docs.length} document${docs.length === 1 ? '' : 's'}`, 'dim');
}

async function listDocs(ctx, cmd, query) {
  let res;
  try {
    res = await ctx.api.get('/v1/documents', query, { signal: ctx.signal() });
  } catch (err) {
    fail(ctx, cmd, err);
    return;
  }
  const docs = (res && res.documents) || [];
  if (docs.length === 0) {
    ctx.out.line(EMPTY_LS, 'dim');
    return;
  }
  docTable(ctx, docs);
  if (query && query.limit !== undefined && docs.length === query.limit) {
    ctx.out.line(`showing first ${docs.length} — use --limit to see more`, 'dim');
  }
}

function defaultPickFile(accept, signal) {
  const doc = globalThis.document;
  if (!doc) return Promise.resolve(null);
  return new Promise((resolve) => {
    if (signal && signal.aborted) return resolve(null);
    const input = doc.createElement('input');
    input.type = 'file';
    input.accept = accept;
    const finish = (file) => {
      if (signal) signal.removeEventListener('abort', onAbort);
      resolve(file);
    };
    const onAbort = () => finish(null);
    input.addEventListener('change', () => finish((input.files && input.files[0]) || null));
    input.addEventListener('cancel', () => finish(null));
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
    input.click();
  });
}

function defaultSleep(ms, signal) {
  return new Promise((resolve) => {
    if (signal && signal.aborted) return resolve();
    let timer = null;
    const done = () => {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', done);
      resolve();
    };
    timer = setTimeout(done, ms);
    if (signal) signal.addEventListener('abort', done, { once: true });
  });
}

function isHidden(ctx) {
  if (typeof ctx.isHidden === 'function') return Boolean(ctx.isHidden());
  return Boolean(globalThis.document && globalThis.document.hidden);
}

const manPages = {
  ls: `NAME
    ls — list catalogued documents

SYNOPSIS
    ls [--status S] [--limit N]

DESCRIPTION
    Lists documents from GET /v1/documents ordered by doc_id: doc_id, filename,
    type and status. --status filters (archived, failed, parked, processing, new);
    --limit sets the page size (1-500, default 50).`,
  inspect: `NAME
    inspect — show one document's classification and route

SYNOPSIS
    inspect <doc_id>

DESCRIPTION
    Reads GET /v1/documents/{doc_id}: status, doc_type, confidence and one line
    per stage of the route trail.`,
  audit: `NAME
    audit — verify a document's audit chain

SYNOPSIS
    audit <doc_id>

DESCRIPTION
    Reads GET /v1/audit/{doc_id}. Prints the entry count and whether the hash
    chain is intact ('chain: ok') or the first broken entry.`,
  review: `NAME
    review — list parked documents awaiting a decision

SYNOPSIS
    review

DESCRIPTION
    Same as 'ls --status parked'. Use 'resolve' to disposition one.`,
  resolve: `NAME
    resolve — disposition a parked document

SYNOPSIS
    resolve <doc_id> <approve|correct|reject> [--type T] [--subclass S] [--reviewer R]

DESCRIPTION
    POSTs to /v1/review/{doc_id}/resolve. 'correct' needs --type <doc_type>.
    Prints the resulting status and route trail.`,
  runs: `NAME
    runs — list eval runs

SYNOPSIS
    runs

DESCRIPTION
    Reads GET /v1/runs: run_id and document count. Empty before the first eval run.`,
  cards: `NAME
    cards — show a run's cards

SYNOPSIS
    cards <run_id>

DESCRIPTION
    Reads GET /v1/runs/{run_id}/cards and prints one block per card.`,
  health: `NAME
    health — check the api

SYNOPSIS
    health

DESCRIPTION
    Reads GET /health.`,
  upload: `NAME
    upload — queue a file for processing

SYNOPSIS
    upload

DESCRIPTION
    Opens a file picker (.txt .md .pdf .docx .rtf .html .htm) and posts the file
    to /v1/documents. Prints the queued file and its doc_id.`,
  watch: `NAME
    watch — follow document status changes

SYNOPSIS
    watch [--interval 3]

DESCRIPTION
    Polls GET /v1/documents?limit=500 every N seconds (1-60) and prints one line
    per new document or status change. Stops on Ctrl+C or when the tab is hidden.`,
  jev: `NAME
    jev — show the Jev gate status

SYNOPSIS
    jev

DESCRIPTION
    Reads GET /v1/jev: provider, model, which gate is active and, when calibrated,
    the accept and verify thresholds. API keys are never shown.`,
  auth: `NAME
    auth — set or clear the api token

SYNOPSIS
    auth <token>
    auth --clear

DESCRIPTION
    Stores the bearer token for this tab only (session storage), then checks it
    against the api. The token is masked in scrollback and history.`,
};

export function registerPipeline(registry) {
  registry.register({
    name: 'ls',
    summary: 'list documents',
    usage: 'ls [--status S] [--limit N]',
    man: manPages.ls,
    async run(ctx, _args, flags) {
      const limit = parseIntFlag(flags, 'limit', 1, 500);
      if (limit.error) return ctx.out.line(`ls: ${limit.error}`, 'error');
      if (flags.status === true) return ctx.out.line('ls: --status needs a value', 'error');
      const query = { limit: limit.value ?? 50 };
      if (flagText(flags, 'status')) query.status = flags.status;
      return listDocs(ctx, 'ls', query);
    },
  });

  registry.register({
    name: 'inspect',
    summary: 'show one document',
    usage: 'inspect <doc_id>',
    man: manPages.inspect,
    async run(ctx, args) {
      const id = args[0];
      if (!id) return ctx.out.line('inspect: usage: inspect <doc_id>', 'error');
      if (!validDocId(id)) return ctx.out.line('inspect: invalid doc_id', 'error');
      let res;
      try {
        res = await ctx.api.get(`/v1/documents/${enc(id)}`, undefined, { signal: ctx.signal() });
      } catch (err) {
        return fail(ctx, 'inspect', err, { notFound: id });
      }
      const report = (res && res.report) || {};
      const cls = report.classification || {};
      const cat = (res && res.catalog) || {};
      const manifest = (res && res.manifest) || {};
      const trail =
        report.route_trail || (manifest.state && manifest.state.route_trail) || [];
      let gate = '';
      try {
        let ents = gateEntries(res && res.audit && res.audit.entries);
        if (ents.length === 0) {
          const a = await ctx.api.get(`/v1/audit/${enc(id)}`, undefined, { signal: ctx.signal() });
          ents = gateEntries(a && a.entries);
        }
        if (ents.length) gate = gateLine(ents[ents.length - 1]).t;
      } catch {
        // gate info is optional
      }
      if (ctx.signal().aborted) return;
      ctx.out.line(text(manifest.filename || cat.filename || res.doc_id), 'amber');
      ctx.out.kv([
        ['doc_id', text(res.doc_id)],
        ['status', text(res.status)],
        ['doc_type', text(cls.doc_type || cat.doc_type)],
        ['confidence', text(cls.confidence)],
        ...(gate ? [['gate', gate]] : []),
      ]);
      if (trail.length === 0) {
        ctx.out.line('no route trail yet', 'dim');
      }
      trail.forEach((stage) => ctx.out.line(`▸ ${text(stage)}`, 'info'));
    },
  });

  registry.register({
    name: 'audit',
    summary: 'verify the audit chain',
    usage: 'audit <doc_id>',
    man: manPages.audit,
    async run(ctx, args) {
      const id = args[0];
      if (!id) return ctx.out.line('audit: usage: audit <doc_id>', 'error');
      if (!validDocId(id)) return ctx.out.line('audit: invalid doc_id', 'error');
      let res;
      try {
        res = await ctx.api.get(`/v1/audit/${enc(id)}`, undefined, { signal: ctx.signal() });
      } catch (err) {
        return fail(ctx, 'audit', err, { notFound: id });
      }
      const entries = (res && res.entries) || [];
      const chain = (res && res.chain) || {};
      if (entries.length === 0) {
        return ctx.out.line('no audit entries (unknown document?)', 'warn');
      }
      ctx.out.line(`${entries.length} audit entr${entries.length === 1 ? 'y' : 'ies'}`);
      if (chain.ok) {
        ctx.out.line('chain: ok', 'success');
      } else {
        ctx.out.line(`chain: broken at ${text(chain.broken_at)}`, 'error');
      }
      gateEntries(entries).forEach((e) => {
        const g = gateLine(e);
        ctx.out.line(g.t, g.cls);
      });
    },
  });

  registry.register({
    name: 'jev',
    summary: 'show the Jev gate status',
    usage: 'jev',
    man: manPages.jev,
    async run(ctx) {
      let res;
      try {
        res = await ctx.api.get('/v1/jev', undefined, { signal: ctx.signal() });
      } catch (err) {
        return fail(ctx, 'jev', err);
      }
      if (!res || !res.enabled) return ctx.out.line('jev off (band gate)', 'dim');
      const pairs = [
        ['provider', text(res.provider)],
        ['model', text(res.model)],
        ['base_url', text(res.base_url)],
        ['gate', text(res.gate)],
        ['calibrated', res.calibrated ? 'yes' : 'no'],
      ];
      const c = res.calibrated && res.calibration;
      if (c && typeof c === 'object') {
        pairs.push(
          ['accept', text(num(c.accept_threshold))],
          ['verify', text(num(c.verify_threshold))],
          ['temperature', text(num(c.temperature))],
          ['ece', `${text(num(c.ece_before))} -> ${text(num(c.ece_after))}`],
          ['n', text(c.n)],
        );
      }
      ctx.out.kv(pairs);
    },
  });

  registry.register({
    name: 'review',
    summary: 'list parked documents',
    usage: 'review',
    man: manPages.review,
    run(ctx) {
      return listDocs(ctx, 'review', { status: 'parked' });
    },
  });

  registry.register({
    name: 'resolve',
    summary: 'approve, correct or reject a parked document',
    usage: 'resolve <doc_id> <approve|correct|reject> [--type T] [--subclass S] [--reviewer R]',
    man: manPages.resolve,
    async run(ctx, args, flags) {
      const [id, action] = args;
      const usage = 'resolve: usage: resolve <doc_id> <approve|correct|reject>';
      if (!id || !action) return ctx.out.line(usage, 'error');
      if (!validDocId(id)) return ctx.out.line('resolve: invalid doc_id', 'error');
      if (!['approve', 'correct', 'reject'].includes(action)) {
        return ctx.out.line(usage, 'error');
      }
      const docType = flagText(flags, 'type');
      if (action === 'correct' && !docType) {
        return ctx.out.line('resolve: correct needs --type <doc_type>', 'error');
      }
      const body = { action };
      if (docType) body.doc_type = docType;
      if (flagText(flags, 'subclass')) body.doc_subclass = flags.subclass;
      if (flagText(flags, 'reviewer')) body.reviewer = flags.reviewer;
      let res;
      try {
        res = await ctx.api.post(`/v1/review/${enc(id)}/resolve`, body, { signal: ctx.signal() });
      } catch (err) {
        return fail(ctx, 'resolve', err, { notFound: id });
      }
      ctx.out.line(`${text(res.doc_id)} · ${text(res.action)} — ${text(res.status)}`, 'success');
      if (res.doc_type) ctx.out.line(`doc_type: ${text(res.doc_type)}`);
      (res.route_trail || []).forEach((stage) => ctx.out.line(`▸ ${text(stage)}`, 'info'));
    },
  });

  registry.register({
    name: 'runs',
    summary: 'list eval runs',
    usage: 'runs',
    man: manPages.runs,
    async run(ctx) {
      let res;
      try {
        res = await ctx.api.get('/v1/runs', undefined, { signal: ctx.signal() });
      } catch (err) {
        return fail(ctx, 'runs', err);
      }
      const runs = (res && res.runs) || [];
      if (runs.length === 0) return ctx.out.line('no eval runs yet', 'dim');
      ctx.out.table(
        ['run_id', 'documents'],
        runs.map((r) => [r.run_id, r.documents]),
      );
    },
  });

  registry.register({
    name: 'cards',
    summary: "show a run's cards",
    usage: 'cards <run_id>',
    man: manPages.cards,
    async run(ctx, args) {
      const id = args[0];
      if (!id) return ctx.out.line('cards: usage: cards <run_id>', 'error');
      if (!validRunId(id)) return ctx.out.line('cards: invalid run_id', 'error');
      let res;
      try {
        res = await ctx.api.get(`/v1/runs/${enc(id)}/cards`, undefined, { signal: ctx.signal() });
      } catch (err) {
        return fail(ctx, 'cards', err);
      }
      const cards = (res && res.cards) || [];
      if (cards.length === 0) return ctx.out.line(`no cards for run ${id}`, 'dim');
      cards.forEach((card, i) => {
        if (i > 0) ctx.out.divider();
        const entries =
          card && typeof card === 'object' && !Array.isArray(card)
            ? Object.entries(card)
            : [['card', card]];
        ctx.out.kv(entries.map(([k, v]) => [k, text(v)]));
      });
    },
  });

  registry.register({
    name: 'health',
    summary: 'check the api',
    usage: 'health',
    man: manPages.health,
    async run(ctx) {
      let res;
      try {
        res = await ctx.api.get('/health', undefined, { signal: ctx.signal() });
      } catch (err) {
        return fail(ctx, 'health', err);
      }
      ctx.out.line(`${text(res && res.service)} — ${text(res && res.status)}`, 'success');
    },
  });

  registry.register({
    name: 'upload',
    summary: 'queue a file',
    usage: 'upload',
    man: manPages.upload,
    async run(ctx) {
      const pick = typeof ctx.pickFile === 'function' ? ctx.pickFile : defaultPickFile;
      const signal = ctx.signal();
      const file = await pick(ACCEPT, signal);
      if (signal.aborted) return;
      if (!file) return ctx.out.line('upload: no file chosen', 'dim');
      let res;
      try {
        res = await ctx.api.upload(file, { signal });
      } catch (err) {
        return fail(ctx, 'upload', err);
      }
      ctx.out.line(`queued ${text(res.file || file.name)} · ${text(res.doc_id)}`, 'success');
    },
  });

  registry.register({
    name: 'watch',
    summary: 'follow status changes',
    usage: 'watch [--interval 3]',
    man: manPages.watch,
    async run(ctx, _args, flags) {
      const interval = parseIntFlag(flags, 'interval', 1, 60);
      if (interval.error) return ctx.out.line(`watch: ${interval.error}`, 'error');
      const seconds = interval.value ?? 3;
      const sleep = typeof ctx.sleep === 'function' ? ctx.sleep : defaultSleep;
      const signal = ctx.signal();
      const seen = new Map();
      let first = true;
      let warnedFull = false;
      for (;;) {
        let res;
        try {
          res = await ctx.api.get('/v1/documents', { limit: WATCH_LIMIT }, { signal });
        } catch (err) {
          return fail(ctx, 'watch', err);
        }
        if (signal.aborted) return ctx.out.line('watch: stopped', 'dim');
        const docs = (res && res.documents) || [];
        if (docs.length >= WATCH_LIMIT && !warnedFull) {
          warnedFull = true;
          ctx.out.line(`watch: page full (${WATCH_LIMIT}) — older changes may be missed`, 'warn');
        }
        if (first) {
          docs.forEach((d) => seen.set(d.doc_id, d.status));
          ctx.out.line(
            `watching ${docs.length} document${docs.length === 1 ? '' : 's'} · every ${seconds}s · ctrl+c to stop`,
            'dim',
          );
          first = false;
        } else {
          for (const d of docs) {
            const before = seen.get(d.doc_id);
            if (before === d.status) continue;
            seen.set(d.doc_id, d.status);
            const change = before === undefined ? `new — ${d.status}` : `${before} → ${d.status}`;
            ctx.out.line(`${d.doc_id} · ${d.filename} · ${change}`, statusClass(d.status));
          }
        }
        await sleep(seconds * 1000, signal);
        if (signal.aborted) return ctx.out.line('watch: stopped', 'dim');
        if (isHidden(ctx)) return ctx.out.line('watch: stopped — tab hidden', 'dim');
      }
    },
  });

  registry.register({
    name: 'auth',
    summary: 'set or clear the api token',
    usage: 'auth <token> | --clear',
    man: manPages.auth,
    async run(ctx, args, flags) {
      if (flags.clear) {
        ctx.api.clearToken();
        return ctx.out.line('auth: token cleared', 'dim');
      }
      const token = args[0];
      if (!token) return ctx.out.line('auth: usage: auth <token> | --clear', 'error');
      try {
        ctx.api.setToken(token);
      } catch {
        return ctx.out.line('auth: token must be printable ASCII with no spaces', 'error');
      }
      try {
        await ctx.api.get('/v1/documents', { limit: 1 }, { signal: ctx.signal() });
      } catch (err) {
        if (err && err.kind === 'unauthorized') {
          ctx.api.clearToken();
          return ctx.out.line('auth: token rejected — 401', 'error');
        }
        return fail(ctx, 'auth', err);
      }
      ctx.out.line('auth: ok', 'success');
    },
  });
}
