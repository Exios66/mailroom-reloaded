// Deep links into /tui: `/tui#replay=run:<id>` opens the replay viewer after boot.
// The fragment is untrusted: only an id that `replay` itself accepts becomes a command line.

import { normalizeSessionId } from './commands/replay.js';

/** True when the hash asks for a replay, whether or not it is valid. */
export function isReplayLink(hash) {
  return typeof hash === 'string' && hash.startsWith('#replay=');
}

/** The command line for a location hash, or null when it is not a valid deep link. */
export function deepLinkCommand(hash) {
  if (typeof hash !== 'string') return null;
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
