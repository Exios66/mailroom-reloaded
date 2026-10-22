// Config command for /tui: GET /v1/config as a flat key/value listing.
// Nested sections are flattened to dotted keys. A key that looks secret prints only its exact
// value "set" or "unset"; any other value under such a key is withheld. Every printed value
// goes through ctx.out.kv, which writes textContent, never HTML.

import { errorHint, httpFor } from '../lib/http.js';

export const MAX_ROWS = 500;
const MAX_DEPTH = 6;
const MAX_KEY = 120;
const MAX_VALUE = 500;
const WITHHELD = 'withheld';
// Matched against the full dotted key, so `api.token`, `jev.api_key` and `pager.webhook_secret` all match.
const SECRET_KEY_RE = /key|token|secret|pass|credential|dsn|otlp|anchor_url/i;

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function isScalar(v) {
  return v === null || v === undefined || typeof v !== 'object';
}

/** Printable text for a scalar leaf; long strings are cut, never dropped silently. */
function scalarText(v) {
  if (typeof v === 'string') return v.length > MAX_VALUE ? `${v.slice(0, MAX_VALUE)}…` : v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return '—';
}

/**
 * The value a row shows. A secret-looking key shows only the exact strings "set" and "unset";
 * anything else (including null, numbers and objects) is withheld.
 */
export function displayValue(key, value) {
  if (SECRET_KEY_RE.test(key)) {
    return value === 'set' || value === 'unset' ? value : WITHHELD;
  }
  return scalarText(value);
}

/**
 * Flatten a /v1/config JSON payload into [key, value] rows. Returns {rows, total, invalid}:
 * `rows` keeps the first MAX_ROWS; `total` counts rows before that cap, including collapsed
 * scalar arrays and withheld secret subtrees. `invalid` is true for null, arrays or scalars.
 * Keys are capped at 120 characters and values at 500, with an ellipsis for truncated values.
 * Values deeper than six levels become a marker; secret-looking keys are withheld first.
 */
export function configRows(payload) {
  if (!isPlainObject(payload)) return { rows: [], total: 0, invalid: true };
  const rows = [];
  let total = 0;
  const add = (key, value) => {
    total += 1;
    if (rows.length < MAX_ROWS) rows.push([key.slice(0, MAX_KEY), displayValue(key, value)]);
  };
  const visit = (prefix, value, depth) => {
    if (SECRET_KEY_RE.test(prefix)) return add(prefix, value);
    if (depth > MAX_DEPTH) return add(prefix, '(nested too deep)');
    if (isScalar(value)) return add(prefix, value === undefined ? null : value);
    if (Array.isArray(value)) {
      if (value.length === 0) return add(prefix, '—');
      if (value.every(isScalar)) return add(prefix, value.map(scalarText).join(', '));
      return value.forEach((item, i) => visit(`${prefix}.${i}`, item, depth + 1));
    }
    const keys = Object.keys(value);
    if (keys.length === 0) return add(prefix, '—');
    for (const k of keys) {
      visit(prefix ? `${prefix}.${k}` : k, value[k], depth + 1);
    }
    return undefined;
  };
  for (const k of Object.keys(payload)) visit(k, payload[k], 1);
  return { rows, total, invalid: false };
}

const manPages = {
  config: `NAME
    config — show the effective settings

SYNOPSIS
    config

DESCRIPTION
    Reads GET /v1/config and prints the server's effective settings as dotted
    key/value rows: provider, Jev, taxonomy thresholds, storage, tracing, links,
    watcher, pager and run. Secret-typed settings (tokens, keys, passwords, DSNs,
    OTLP endpoints) show only "set" or "unset". Any other value under a key that
    looks secret is withheld. Base URLs show scheme and host only. Needs the API
    token when one is configured ('auth <token>').`,
};

/** Register the read-only config command; deps.fetchImpl can supply its fetch implementation. */
export function registerConfig(registry, deps = {}) {
  registry.register({
    name: 'config',
    summary: 'show the effective settings (secrets as set/unset only)',
    usage: 'config',
    man: manPages.config,
    async run(ctx, args, flags) {
      if (args.length > 0 || Object.keys(flags).length > 0) {
        return ctx.out.line('config: usage: config', 'error');
      }
      let res;
      try {
        res = await httpFor(ctx, { fetchImpl: deps.fetchImpl }).get('/v1/config');
      } catch (err) {
        if (err && err.kind === 'aborted') return undefined;
        return ctx.out.line(`config: ${errorHint(err)}`, 'error');
      }
      if (ctx.signal().aborted) return undefined;
      const { rows, total, invalid } = configRows(res);
      if (invalid) return ctx.out.line('config: bad response from the api', 'error');
      if (rows.length === 0) return ctx.out.line('no settings reported', 'dim');
      ctx.out.kv(rows);
      if (total > rows.length) {
        ctx.out.line(`showing first ${rows.length} of ${total} settings`, 'dim');
      }
      return undefined;
    },
  });
}
