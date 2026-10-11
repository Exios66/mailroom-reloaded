// Pure, DOM-free helpers behind the /ui page. Kept in their own module so the
// security-relevant URL handling and the display logic can be unit-tested under
// `node --test` without a browser. The DOM wiring lives in index.html.

/** Keys of the public GET /links payload that the page consumes. */
export const LINK_KEYS = Object.freeze([
  'public_url',
  'phoenix_url',
  'grafana_url',
  'phoenix_project',
]);

/** Catalog statuses the pill renders; anything else is shown as "?". */
export const STATUSES = Object.freeze(['processing', 'parked', 'archived', 'failed']);

/**
 * True only for a plain http(s) base URL: no `javascript:`/`data:` scheme, no
 * embedded credentials, no whitespace. This is the guard that keeps a hostile
 * (or misconfigured) `/links` value out of an `href`.
 */
export function isHttpBase(value) {
  return typeof value === 'string' && /^https?:\/\/[^\s@]+$/i.test(value);
}

/**
 * Overlay a GET /links body on the static defaults. A value that is not a plain
 * http(s) base is dropped, so a hostile config can never place a `javascript:` or
 * `data:` URL into an href. http(s) bases lose their trailing slashes.
 */
export function mergeLinks(defaults, body) {
  const out = { ...defaults };
  if (!body || typeof body !== 'object') return out;
  for (const key of LINK_KEYS) {
    const value = body[key];
    if (typeof value !== 'string' || !value) continue;
    if (key === 'phoenix_project') {
      out[key] = value;
      continue;
    }
    if (!isHttpBase(value)) continue;
    out[key] = value.replace(/\/+$/, '');
  }
  return out;
}

/**
 * Normalize a catalog status for the pill: a known status keeps its name, an
 * unknown one becomes "?", and the class is ok/bad/muted for the colour.
 */
export function statusPill(status) {
  const known = STATUSES.includes(status) ? status : '?';
  const className = known === 'archived' ? 'ok' : known === 'failed' ? 'bad' : 'muted';
  return { status: known, className: 'pill ' + className };
}

/** The outbound hrefs for one eval-run row (replay, Grafana, Phoenix). */
export function runLinks(links, runId) {
  const id = encodeURIComponent(runId);
  return {
    replay: '/tui#replay=run:' + id,
    grafana: `${links.grafana_url}/d/mailroom-quality?var-run_id=${id}`,
    phoenix: links.phoenix_url,
  };
}

/**
 * The audit-verify summary line: "chain OK" when the chain verifies, otherwise
 * "chain BROKEN at <index>". `className` drives the ok/bad colour.
 */
export function auditSummary(chain, entryCount) {
  const ok = Boolean(chain && chain.ok);
  return {
    text: ok ? 'chain OK' : 'chain BROKEN at ' + (chain && chain.broken_at),
    className: ok ? 'ok' : 'bad',
    entries: entryCount,
  };
}
