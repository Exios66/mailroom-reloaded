// Wires the engine, api client, terminal, commands, ambient layer and boot for /tui.
import { createRegistry, createHistory } from './engine.js';
import { createApi } from './api.js';
import { createTerminal } from './terminal.js';
import { boot } from './boot.js';
import { createAmbient } from './ambient.js';
import { registerPipeline } from './commands/pipeline.js';
import { registerLedger } from './commands/ledger.js';
import { registerReplay } from './commands/replay.js';
import { registerSandbox } from './commands/sandbox.js';
import { registerShell } from './commands/shell.js';
import { createModuleHost } from './plugins.js';
import { deepLinkCommand, deepLinkKind } from './deeplink.js';

async function loadText(name) {
  try {
    const res = await fetch(`/tui/assets/${name}`);
    return res.ok ? await res.text() : '';
  } catch {
    return '';
  }
}

/** The built-in modules, in registration order. New modules are added here (plugins.js). */
export const MODULES = [
  { id: 'pipeline', commands: registerPipeline },
  { id: 'ledger', commands: registerLedger },
  { id: 'replay', commands: registerReplay },
  { id: 'sandbox', commands: registerSandbox },
  { id: 'shell', commands: registerShell },
];

/**
 * Register every module through the plugin boundary. A module that fails to register is
 * skipped (and logged) so the TUI still boots. Returns the module host; `host.initAll(ctx)`
 * runs each module's onInit once the terminal exists.
 */
export function registerAll(registry, { ambient }, modules = MODULES) {
  const host = createModuleHost(registry);
  host.registerAll(modules, { ambient });
  return host;
}

export async function start() {
  const reducedMotion =
    typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const compact = typeof matchMedia === 'function' && matchMedia('(max-width: 640px)').matches;
  const registry = createRegistry();
  const history = createHistory();
  const api = createApi();
  const ambient = createAmbient(document, { reducedMotion });
  const host = registerAll(registry, { ambient });
  document.body.classList.add('powering-on');
  setTimeout(() => document.body.classList.remove('powering-on'), 650);
  const term = createTerminal({ root: document, registry, history, api });
  term.setInputEnabled(false); // read-only until boot resolves
  const [banner, bannerCompact] = await Promise.all([
    loadText('banner.txt'),
    loadText('banner-compact.txt'),
  ]);
  term.ctx.banner = banner;
  // Module init never blocks boot: failures are logged and isolated per module.
  host.initAll(term.ctx).catch(() => {});
  const st = ambient.state();
  term.ctx.setStatus('theme', st.label);
  term.ctx.setStatus('crt', st.crt ? 'on' : 'off');
  // Any key or click skips the boot animation (checks still run).
  const skip = new AbortController();
  const onSkip = () => skip.abort();
  document.addEventListener('keydown', onSkip, { once: true });
  document.addEventListener('pointerdown', onSkip, { once: true });
  try {
    await boot(term.ctx, {
      reducedMotion,
      signal: skip.signal,
      banner,
      bannerCompact,
      compact,
      theme: st.label,
      crt: st.crt,
    });
  } finally {
    document.removeEventListener('keydown', onSkip);
    document.removeEventListener('pointerdown', onSkip);
    term.setInputEnabled(true);
  }
  term.focus();
  const hash = globalThis.location ? globalThis.location.hash : '';
  const link = deepLinkCommand(hash);
  if (link) term.run(link).catch(() => {});
  else {
    const kind = deepLinkKind(hash);
    if (kind) term.ctx.out.line(`${kind}: invalid deep link`, 'error');
  }
  return term;
}

if (typeof document !== 'undefined') {
  start().catch((err) => {
    // A failed start must not leave the page silently dead.
    console.error('tui start failed', err);
  });
}
