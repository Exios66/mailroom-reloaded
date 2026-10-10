// Sandbox command for /tui: `inbox` opens the sandbox UI's Correspondent inbox in a new tab.
// The sandbox is another origin (no CORS or frame permission), so the TUI only builds a link;
// GET /links supplies the base URL and the API token is never put in it.

import { flagText } from './pipeline.js';
import { inboxUrl, loadLinks, sandboxBase, validHashId } from '../lib/links.js';

// `--tab` names -> sandbox UI tab ids (ui/index.html data-tab).
export const INBOX_TABS = { ingress: 'messages', boss: 'boss', outbox: 'outbox', events: 'events' };
const USAGE =
  'inbox: usage: inbox [--tab ingress|boss|outbox|events] [--scenario ID] [--message ID] [--thread ID] [--no-mailbox] [--print]';
const KNOWN = new Set(['tab', 'scenario', 'message', 'thread', 'no-mailbox', 'print']);
const TOKEN_HINT = 'if the sandbox asks for a token, paste it into its API token field; it is never put in the link';

const manPage = `NAME
    inbox - open the sandbox Correspondent inbox in a new tab

SYNOPSIS
    inbox [--tab ingress|boss|outbox|events] [--scenario ID] [--message ID] [--thread ID] [--no-mailbox] [--print]

DESCRIPTION
    Reads GET /links for sandbox_url (MAILROOM_SANDBOX_URL on the server) and opens
    <sandbox_url>/ui#tab=messages&mailbox=open&role=correspondent in a new tab: the Ingress
    queue with the Boss mailbox dock filtered to the correspondent role, so a sandbox
    simulation can be watched live (the sandbox UI polls every 2 s). The sandbox is a
    different origin, so it cannot be framed here. The URL is printed so it can be copied.
    --tab picks ingress (default), boss, outbox or events. --scenario, --message and
    --thread select a scenario, message or thread by id (letters, digits, . _ -; starting with a letter or digit; up to 64).
    --no-mailbox starts with the mailbox dock closed. --print only prints the link.
    The link never carries a token: if the sandbox asks for one, paste it into its API
    token field.`;

function parseFlags(args, flags) {
  if (args.length) return { error: 'unexpected argument' };
  const unknown = Object.keys(flags).find((k) => !KNOWN.has(k));
  if (unknown) return { error: `unknown flag --${unknown}` };
  for (const k of ['no-mailbox', 'print']) {
    if (flags[k] !== undefined && flags[k] !== true) return { error: `--${k} takes no value` };
  }
  const opts = {};
  if (flags.tab !== undefined) {
    const raw = flagText(flags, 'tab');
    if (raw === undefined || !Object.hasOwn(INBOX_TABS, raw)) return { error: '--tab must be ingress, boss, outbox or events' };
    opts.tab = INBOX_TABS[raw];
  }
  for (const [flag, key] of [['scenario', 'scenario'], ['message', 'sel'], ['thread', 'thread']]) {
    if (flags[flag] === undefined) continue;
    const raw = flagText(flags, flag);
    if (!validHashId(raw)) return { error: `--${flag} must be 1-64 characters of letters, digits, . _ -` };
    opts[key] = raw;
  }
  if (flags['no-mailbox'] === true) opts.mailbox = 'closed';
  return { opts, print: flags.print === true };
}

export function registerSandbox(registry) {
  registry.register({
    name: 'inbox',
    summary: 'open the sandbox Correspondent inbox',
    usage: 'inbox [--tab ingress|boss|outbox|events] [--scenario ID] [--message ID] [--thread ID] [--no-mailbox] [--print]',
    man: manPage,
    complete(_ctx, args) {
      const last = args[args.length - 1];
      return args.length >= 2 && args[args.length - 2] === '--tab' ? Object.keys(INBOX_TABS).filter((t) => t.startsWith(last ?? '')) : [];
    },
    async run(ctx, args, flags) {
      const parsed = parseFlags(args, flags);
      if (parsed.error) {
        ctx.out.line(`inbox: ${parsed.error}`, 'error');
        return ctx.out.line(USAGE, 'dim');
      }
      const base = sandboxBase(await loadLinks(ctx));
      const url = base ? inboxUrl(base, parsed.opts) : null;
      if (!url) return ctx.out.line('inbox: sandbox URL not configured (set MAILROOM_SANDBOX_URL)', 'error');
      if (!parsed.print) {
        try {
          (ctx.open ?? globalThis.open)?.(url, '_blank', 'noopener');
        } catch {
          // A blocked popup still leaves the printed link.
        }
      }
      ctx.out.line(url, 'info');
      return ctx.out.line(TOKEN_HINT, 'dim');
    },
  });
}
