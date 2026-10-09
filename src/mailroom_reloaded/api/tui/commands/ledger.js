// Ledger commands for /tui: view and verify the archive ledger.
// Every printed value comes from a live response and goes through ctx.out (textContent).

import { fail, flagText, parseIntFlag, text, validRunId } from './pipeline.js';

const KIND_RE = /^[A-Za-z0-9_.-]{1,64}$/;
const HASH_CHARS = 12;

function hash12(value) {
  return typeof value === 'string' && value ? value.slice(0, HASH_CHARS) : '—';
}

function shortTs(value) {
  if (typeof value !== 'string' || value === '') return text(value);
  return value.replace('T', ' ').slice(0, 19);
}

/** One table row for a ledger entry; the payload is deliberately not printed. */
export function ledgerRow(e) {
  const x = e && typeof e === 'object' ? e : {};
  return [text(x.seq), text(x.kind), text(x.run_id), text(x.doc_id), shortTs(x.ts), hash12(x.entry_hash)];
}

/** Lines [{t, cls}] for a verify response; never throws on odd payloads. */
export function verifyLine(res) {
  const r = res && typeof res === 'object' ? res : {};
  const out = [];
  if (r.ok) {
    out.push({
      t: `chain ok — ${text(r.count)} entries, head #${text(r.head_seq)} ${hash12(r.head_hash)}`,
      cls: 'success',
    });
  } else if (r.broken_at === null || r.broken_at === undefined) {
    out.push({ t: `verify failed: ${text(r.detail)}`, cls: 'error' });
    return out;
  } else {
    out.push({ t: `chain broken at seq ${text(r.broken_at)} — ${text(r.detail)}`, cls: 'error' });
  }
  if (r.merkle_ok === false) {
    out.push({ t: 'merkle: root mismatch', cls: 'error' });
  } else if (r.merkle_ok === null || r.merkle_ok === undefined) {
    out.push({ t: 'merkle: run not closed', cls: 'dim' });
  } else {
    out.push({ t: 'merkle: ok', cls: 'success' });
  }
  return out;
}

const manPages = {
  ledger: `NAME
    ledger — view and verify the archive ledger

SYNOPSIS
    ledger [--run ID] [--kind K] [--limit N]
    ledger head
    ledger verify [run_id]

DESCRIPTION
    Bare 'ledger' reads GET /v1/ledger, newest first: seq, kind, run, doc, time
    and the first 12 characters of the entry hash. --limit is 1-500 (default 50).
    'head' reads GET /v1/ledger/head: the newest entry and the entry count.
    'verify' reads GET /v1/ledger/verify and re-checks the hash chain, for one run
    when a run_id is given; it also reports the run's Merkle root when closed.`,
};

async function ledgerList(ctx, flags) {
  const limit = parseIntFlag(flags, 'limit', 1, 500);
  if (limit.error) return ctx.out.line(`ledger: ${limit.error}`, 'error');
  const query = { limit: limit.value ?? 50 };
  if (flags.run !== undefined) {
    if (!validRunId(flags.run)) return ctx.out.line('ledger: invalid run_id', 'error');
    query.run_id = flags.run;
  }
  if (flags.kind !== undefined) {
    if (!KIND_RE.test(flagText(flags, 'kind') ?? '')) return ctx.out.line('ledger: invalid --kind', 'error');
    query.kind = flags.kind;
  }
  let res;
  try {
    res = await ctx.api.get('/v1/ledger', query, { signal: ctx.signal() });
  } catch (err) {
    return fail(ctx, 'ledger', err);
  }
  const entries = (res && res.entries) || [];
  if (entries.length === 0) return ctx.out.line('no ledger entries', 'dim');
  ctx.out.table(['seq', 'kind', 'run', 'doc', 'ts', 'hash'], entries.map(ledgerRow));
  ctx.out.line(`${entries.length} entr${entries.length === 1 ? 'y' : 'ies'}`, 'dim');
  if (entries.length === query.limit) {
    ctx.out.line(`showing newest ${entries.length} — use --limit to see more`, 'dim');
  }
  return undefined;
}

async function ledgerHead(ctx) {
  let res;
  try {
    res = await ctx.api.get('/v1/ledger/head', undefined, { signal: ctx.signal() });
  } catch (err) {
    return fail(ctx, 'ledger', err);
  }
  const head = res && res.head;
  if (!head || typeof head !== 'object') return ctx.out.line('ledger is empty', 'dim');
  return ctx.out.kv([
    ['seq', text(head.seq)],
    ['count', text(res.count)],
    ['hash', hash12(head.entry_hash)],
    ['kind', text(head.kind)],
    ['ts', shortTs(head.ts)],
  ]);
}

async function ledgerVerify(ctx, args) {
  if (args.length > 1) return ctx.out.line('ledger: usage: ledger verify [run_id]', 'error');
  const query = {};
  if (args[0] !== undefined) {
    if (!validRunId(args[0])) return ctx.out.line('ledger: invalid run_id', 'error');
    query.run_id = args[0];
  }
  let res;
  try {
    res = await ctx.api.get('/v1/ledger/verify', query, { signal: ctx.signal() });
  } catch (err) {
    return fail(ctx, 'ledger', err);
  }
  if (ctx.signal().aborted) return undefined;
  verifyLine(res).forEach((l) => ctx.out.line(l.t, l.cls));
  return undefined;
}

export function registerLedger(registry) {
  registry.register({
    name: 'ledger',
    summary: 'view and verify the archive ledger',
    usage: 'ledger [--run ID] [--kind K] [--limit N] | head | verify [run_id]',
    man: manPages.ledger,
    async run(ctx, args, flags) {
      if ((args[0] === 'head' || args[0] === 'verify') && Object.keys(flags).length > 0) {
        return ctx.out.line(`ledger: ${args[0]} takes no flags`, 'error');
      }
      if (args[0] === 'head' && args.length === 1) return ledgerHead(ctx);
      if (args[0] === 'verify') return ledgerVerify(ctx, args.slice(1));
      if (args.length > 0) {
        return ctx.out.line('ledger: usage: ledger [--run ID] [--kind K] [--limit N] | head | verify [run_id]', 'error');
      }
      return ledgerList(ctx, flags);
    },
  });
}
