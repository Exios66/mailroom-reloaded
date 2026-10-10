// Outbound link helpers for /tui, shared by the replay viewer and the `inbox` command.
// GET /links is server config, but every URL is re-checked here before it is opened or shown.

const ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const TABS = new Set(['messages', 'boss', 'outbox', 'events', 'conformance', 'docs', 'policy']);
const MAILBOXES = new Set(['open', 'closed']);
const ROLES = new Set(['correspondent', 'boss']);

/** True when `v` is a sandbox UI id (scenario, message or thread) the hash router accepts. */
export function validHashId(v) {
  return typeof v === 'string' && ID_RE.test(v);
}

/** An http(s) base URL without credentials or whitespace, trailing slashes trimmed; else null. */
export function httpBase(v) {
  if (typeof v !== 'string' || !/^https?:\/\/[^\s@]+$/i.test(v)) return null;
  return v.replace(/\/+$/, '');
}

/**
 * The run's outbound observability URLs from the GET /links config. Both are
 * null when the config is missing or the session is not a run, so the `o`/`g`
 * keys degrade to a no-op.
 */
export function externalUrls(links, id) {
  const cfg = links && typeof links === 'object' ? links : {};
  const phoenix = httpBase(cfg.phoenix_url);
  const grafana = httpBase(cfg.grafana_url);
  const run = typeof id === 'string' && id.startsWith('run:') ? id.slice(4) : null;
  return {
    phoenix,
    grafana: grafana && run ? `${grafana}/d/mailroom-quality?var-run_id=${encodeURIComponent(run)}` : null,
  };
}

/** The validated sandbox base URL from a GET /links body, or null. */
export function sandboxBase(links) {
  const cfg = links && typeof links === 'object' ? links : {};
  return httpBase(cfg.sandbox_url);
}

/**
 * Deep link into the sandbox UI's inbox: `<base>/ui#tab=..&mailbox=..&role=..`.
 * Every value is checked against the same allow-lists the UI router applies; an
 * invalid base or option returns null and nothing unvalidated is ever emitted.
 * Options (all optional): tab, scenario, sel, mailbox, role, thread.
 */
export function inboxUrl(base, opts = {}) {
  const root = httpBase(base);
  if (!root || /[?#]/.test(root)) return null;
  const o = opts && typeof opts === 'object' ? opts : null;
  if (!o) return null;
  const { tab = 'messages', scenario, sel, mailbox = 'open', role = 'correspondent', thread } = o;
  if (typeof tab !== 'string' || !TABS.has(tab)) return null;
  if (typeof mailbox !== 'string' || !MAILBOXES.has(mailbox)) return null;
  if (typeof role !== 'string' || !ROLES.has(role)) return null;
  const parts = [`tab=${tab}`];
  for (const [key, v] of [['scenario', scenario], ['sel', sel]]) {
    if (v === undefined) continue;
    if (!validHashId(v)) return null;
    parts.push(`${key}=${v}`);
  }
  parts.push(`mailbox=${mailbox}`, `role=${role}`);
  if (thread !== undefined) {
    if (!validHashId(thread)) return null;
    parts.push(`thread=${thread}`);
  }
  return `${root}/ui#${parts.join('&')}`;
}

/** GET /links as an object, or null on any failure (callers degrade; this never throws). */
export async function loadLinks(ctx) {
  try {
    const body = await ctx.api.get('/links', undefined, { signal: ctx.signal() });
    return body && typeof body === 'object' ? body : null;
  } catch {
    return null;
  }
}
