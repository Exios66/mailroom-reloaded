// Plugin boundary for /tui: a module contributes commands and replay panels under one id.
// registerModule({id, commands, panels, onInit}) stages everything first and commits only
// when the whole module is valid, so a bad module never leaves half its commands behind.
// One failing module (bad spec, duplicate command, throwing onInit) never stops the others.
// No DOM and no network, so it runs under `node --test` as well as in the browser.

import { registerPanel } from './replay/panels.js';
import { sanitizeRows } from './replay/grid.js';

const ID_RE = /^[a-z][a-z0-9_-]{0,31}$/;

/** Ids the core keeps for itself; a module may not claim them. */
export const RESERVED_MODULE_IDS = Object.freeze(['core', 'engine', 'terminal', 'tui', 'boot', 'plugins']);
const RESERVED = new Set(RESERVED_MODULE_IDS);

/** Wrap a panel renderer so whatever it returns is sanitised here, whoever draws it. */
function sanitisedPanel(moduleId, spec) {
  if (!spec || typeof spec !== 'object' || typeof spec.render !== 'function') {
    throw new TypeError(`module '${moduleId}': panel render must be a function`);
  }
  const render = spec.render;
  return {
    id: spec.id,
    title: spec.title,
    render(ctx) {
      let rows;
      try {
        rows = render(ctx);
      } catch {
        rows = [];
      }
      return sanitizeRows(rows, ctx && typeof ctx === 'object' ? ctx.cols : 100);
    },
  };
}

/** Collect a module's command specs without touching the real registry. */
function stageCommands(id, commands, deps, registry) {
  if (commands === undefined) return [];
  const staged = [];
  if (typeof commands === 'function') {
    // An existing `registerX(registry, deps)` function: record what it registers. Reads go
    // to the real registry, since commands such as `help` look it up lazily at run time.
    commands(
      {
        register: (spec) => staged.push(spec),
        get: (name) => registry.get(name),
        names: () => registry.names(),
        complete: (line) => registry.complete(line),
      },
      deps,
    );
  } else if (Array.isArray(commands)) {
    staged.push(...commands);
  } else {
    throw new TypeError(`module '${id}': commands must be an array or a register function`);
  }
  const names = new Set();
  for (const spec of staged) {
    if (!spec || typeof spec.name !== 'string' || spec.name === '' || typeof spec.run !== 'function') {
      throw new TypeError(`module '${id}': every command needs a name and a run function`);
    }
    if (names.has(spec.name)) throw new Error(`module '${id}': duplicate command ${spec.name}`);
    names.add(spec.name);
  }
  return staged;
}

/**
 * A module host bound to one command registry. `panels` is the panel registry
 * (defaults to the replay viewer's `registerPanel`), injectable for tests.
 */
export function createModuleHost(registry, { panels = { register: registerPanel }, log = globalThis.console } = {}) {
  const modules = new Map();

  function registerModule(spec, deps = {}) {
    const { id, commands, panels: panelSpecs, onInit } = spec || {};
    if (typeof id !== 'string' || !ID_RE.test(id)) {
      throw new TypeError('module id must be 1-32 characters: a lowercase letter, then letters, digits, _ or -');
    }
    if (RESERVED.has(id)) throw new Error(`module id '${id}' is reserved`);
    if (modules.has(id)) throw new Error(`duplicate module: ${id}`);
    if (onInit !== undefined && typeof onInit !== 'function') {
      throw new TypeError(`module '${id}': onInit must be a function`);
    }
    const staged = stageCommands(id, commands, deps, registry);
    for (const c of staged) {
      if (registry.get(c.name)) throw new Error(`module '${id}': duplicate command ${c.name}`);
    }
    if (panelSpecs !== undefined && !Array.isArray(panelSpecs)) {
      throw new TypeError(`module '${id}': panels must be an array`);
    }
    const wrapped = (panelSpecs || []).map((p) => sanitisedPanel(id, p));
    // Validated: commit commands, then panels (the panel registry checks its own ids).
    for (const c of staged) registry.register(c);
    const panelIds = [];
    for (const p of wrapped) {
      try {
        panels.register(p);
        panelIds.push(p.id);
      } catch (err) {
        log?.warn?.(`tui module '${id}': panel rejected: ${err && err.message ? err.message : err}`);
      }
    }
    const entry = { id, commands: staged.map((c) => c.name), panels: panelIds, onInit, state: 'registered' };
    modules.set(id, entry);
    return { id, commands: entry.commands.slice(), panels: panelIds.slice() };
  }

  return {
    registerModule,
    /**
     * Register every module, isolating failures: a module that throws is recorded as
     * `failed` (with its message) and the rest still register. Returns the failures.
     */
    registerAll(list, deps = {}) {
      const failures = [];
      for (const spec of Array.isArray(list) ? list : []) {
        try {
          registerModule(spec, deps);
        } catch (err) {
          const message = err && err.message ? err.message : String(err);
          failures.push({ id: spec && typeof spec.id === 'string' ? spec.id : '?', message });
          log?.warn?.(`tui module rejected: ${message}`);
        }
      }
      return failures;
    },
    /** Run every module's onInit(ctx); a throw or rejection marks that module failed only. */
    async initAll(ctx) {
      const failures = [];
      for (const entry of modules.values()) {
        if (!entry.onInit) {
          entry.state = 'ready';
          continue;
        }
        try {
          await entry.onInit(ctx);
          entry.state = 'ready';
        } catch (err) {
          entry.state = 'failed';
          const message = err && err.message ? err.message : String(err);
          failures.push({ id: entry.id, message });
          log?.warn?.(`tui module '${entry.id}' init failed: ${message}`);
        }
      }
      return failures;
    },
    /** Every registered module as {id, commands, panels, state}, in registration order. */
    modules() {
      return [...modules.values()].map(({ id, commands, panels: p, state }) => ({
        id,
        commands: commands.slice(),
        panels: p.slice(),
        state,
      }));
    },
  };
}
