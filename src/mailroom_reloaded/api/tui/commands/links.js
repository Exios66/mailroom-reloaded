// Links command for /tui: show the public observability URLs from GET /links, and open one in
// a new tab. Every URL goes through lib/links.js httpBase() before it is shown or opened, so a
// javascript: or otherwise non-http value is never opened. Every printed value is textContent.

import { errorHint, httpFor } from '../lib/http.js';
import { httpBase } from '../lib/links.js';

const OPEN_TARGETS = {
  phoenix: 'phoenix_url',
  grafana: 'grafana_url',
  prometheus: 'prometheus_url',
};
const TABLE_KEYS = ['public_url', 'phoenix_url', 'grafana_url'];
const USAGE = 'links: usage: links [open phoenix|grafana|prometheus]';

const manPages = {
  links: `NAME
    links — show or open the observability links

SYNOPSIS
    links
    links open phoenix|grafana|prometheus

DESCRIPTION
    Bare 'links' reads GET /links and prints the public URL, the Phoenix and
    Grafana URLs and the Phoenix project. A URL is shown only when it is a plain
    http(s) address; anything else is marked invalid and never opened.

    'links open <target>' opens that URL in a new tab with noopener and noreferrer.
    prometheus opens only when the server publishes a prometheus_url; otherwise it
    prints 'prometheus: not configured'.`,
};

function failLine(ctx, cmd, err) {
  if (err && err.kind === 'aborted') return;
  ctx.out.line(`${cmd}: ${errorHint(err)}`, 'error');
}

/** The validated link rows for the table: [label, shown text, css class]. Never throws. */
export function linkRows(body) {
  const b = body && typeof body === 'object' && !Array.isArray(body) ? body : {};
  const rows = TABLE_KEYS.map((key) => {
    const raw = b[key];
    const url = httpBase(raw);
    if (url !== null) return [key, url, ''];
    if (raw === undefined || raw === null || raw === '') return [key, '—', 'dim'];
    return [key, 'invalid — not shown', 'warn'];
  });
  rows.push(['phoenix_project', typeof b.phoenix_project === 'string' && b.phoenix_project ? b.phoenix_project : '—', '']);
  return rows;
}

async function fetchLinks(ctx, deps) {
  return httpFor(ctx, { fetchImpl: deps.fetchImpl }).get('/links');
}

async function showLinks(ctx, deps) {
  let body;
  try {
    body = await fetchLinks(ctx, deps);
  } catch (err) {
    return failLine(ctx, 'links', err);
  }
  if (ctx.signal().aborted) return;
  const rows = linkRows(body);
  ctx.out.table(
    ['link', 'value'],
    rows.map(([label, value]) => [label, value]),
    { cellClass: (row, col) => (col === 1 ? rows[row][2] : '') },
  );
  ctx.out.line("type 'links open phoenix|grafana|prometheus' to open one in a new tab", 'dim');
  return undefined;
}

/**
 * Open a validated phoenix, grafana or prometheus target from /links in a new tab.
 * Request and opener failures print hints; aborted requests return silently and late
 * successful replies are ignored after cancellation.
 * Missing or invalid URLs print a status without invoking the opener.
 */
async function openLink(ctx, deps, target) {
  let body;
  try {
    body = await fetchLinks(ctx, deps);
  } catch (err) {
    return failLine(ctx, 'links', err);
  }
  if (ctx.signal().aborted) return;
  const raw = body && typeof body === 'object' ? body[OPEN_TARGETS[target]] : undefined;
  const url = httpBase(raw);
  if (url === null) {
    if (raw === undefined || raw === null || raw === '') {
      ctx.out.line(`${target}: not configured`, 'dim');
    } else {
      ctx.out.line(`${target}: invalid URL — not opened`, 'error');
    }
    return undefined;
  }
  const open = deps.open ?? globalThis.open?.bind(globalThis);
  if (typeof open !== 'function') {
    ctx.out.line(`links: cannot open ${target} here`, 'error');
    return undefined;
  }
  try {
    open(url, '_blank', 'noopener,noreferrer');
  } catch {
    ctx.out.line(`links: could not open ${target}`, 'error');
    return undefined;
  }
  ctx.out.line(`opened ${target}: ${url}`, 'success');
  return undefined;
}

/**
 * Register `links`. `deps` is injectable for tests: `fetchImpl` (the HTTP client) and `open`
 * (the new-tab opener, called as open(url, '_blank', 'noopener,noreferrer')).
 */
export function registerLinks(registry, deps = {}) {
  registry.register({
    name: 'links',
    summary: 'show or open the observability links',
    usage: 'links [open phoenix|grafana|prometheus]',
    man: manPages.links,
    run(ctx, args, flags = {}) {
      if (Object.keys(flags).length) return ctx.out.line(USAGE, 'error');
      if (args.length === 0) return showLinks(ctx, deps);
      if (args[0] !== 'open' || args.length !== 2 || !Object.hasOwn(OPEN_TARGETS, args[1])) {
        return ctx.out.line(USAGE, 'error');
      }
      return openLink(ctx, deps, args[1]);
    },
  });
}
