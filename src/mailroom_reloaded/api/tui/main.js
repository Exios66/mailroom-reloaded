// Wires the engine, api client and terminal together for /tui.
import { createRegistry, createHistory } from './engine.js';
import { createApi } from './api.js';
import { createTerminal } from './terminal.js';

// TEMP (Task 4): removed once the pipeline and shell commands exist.
const echoCommand = {
  name: 'echo',
  summary: 'print arguments',
  usage: 'echo [text...]',
  man: 'NAME\n  echo - print arguments\n\nSYNOPSIS\n  echo [text...]\n',
  run(ctx, args) {
    ctx.out.line(args.join(' '));
  },
};

/** Single seam where every command group is registered. */
export function registerAll(registry) {
  registry.register(echoCommand);
  // Task 6: registerPipeline(registry);
  // Task 7: registerShell(registry, { ambient });
}

export function start() {
  const registry = createRegistry();
  const history = createHistory();
  const api = createApi();
  registerAll(registry);
  document.body.classList.add('powering-on');
  setTimeout(() => document.body.classList.remove('powering-on'), 650);
  const term = createTerminal({ root: document, registry, history, api });
  // Task 5: await boot(term);
  return term;
}

if (typeof document !== 'undefined') {
  start();
}
