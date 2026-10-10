// Deep links into /tui: `/tui#replay=run:<id>` opens the replay viewer after boot, and
// `/tui#inbox[=<tab>]` runs `inbox`. The fragment is untrusted: only an id that `replay`
// itself accepts, or a tab from the inbox allow-list, becomes a command line.

import { normalizeSessionId } from './commands/replay.js';
import { INBOX_TABS } from './commands/sandbox.js';

/** True when the hash asks for a replay, whether or not it is valid. */
export function isReplayLink(hash) {
  return typeof hash === 'string' && hash.startsWith('#replay=');
}

/** True when the hash asks for the sandbox inbox, whether or not it is valid. */
export function isInboxLink(hash) {
  return typeof hash === 'string' && (hash === '#inbox' || hash.startsWith('#inbox='));
}

/** The command a deep-link hash names ('replay' or 'inbox'), valid or not; null otherwise. */
export function deepLinkKind(hash) {
  if (isReplayLink(hash)) return 'replay';
  return isInboxLink(hash) ? 'inbox' : null;
}

/** The command line for a location hash, or null when it is not a valid deep link. */
export function deepLinkCommand(hash) {
  if (typeof hash !== 'string') return null;
  if (isInboxLink(hash)) {
    if (hash === '#inbox') return 'inbox';
    const tab = hash.slice('#inbox='.length);
    return Object.hasOwn(INBOX_TABS, tab) ? `inbox --tab ${tab}` : null;
  }
  const m = /^#replay=([^&]*)$/.exec(hash);
  if (!m) return null;
  let raw;
  try {
    raw = decodeURIComponent(m[1]);
  } catch {
    return null;
  }
  const id = normalizeSessionId(raw);
  return id ? `replay ${id}` : null;
}
