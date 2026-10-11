// Outbound link helpers for /tui, shared by the replay viewer.
// GET /links is server config, but every URL is re-checked here before it is opened or shown.

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

/** GET /links as an object, or null on any failure (callers degrade; this never throws). */
export async function loadLinks(ctx) {
  try {
    const body = await ctx.api.get('/links', undefined, { signal: ctx.signal() });
    return body && typeof body === 'object' ? body : null;
  } catch {
    return null;
  }
}
