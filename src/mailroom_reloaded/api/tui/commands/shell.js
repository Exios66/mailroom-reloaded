// Shell commands for /tui: help man clear history neofetch theme crt skyline.

const THEME_USAGE = 'theme: usage theme [dark|light|hc|amber|green|cyan]';
const THEME_NAMES = ['dark', 'light', 'hc', 'amber', 'green', 'cyan'];

function manPage(name, summary, usage, description) {
  return `NAME\n  ${name} - ${summary}\n\nSYNOPSIS\n  ${usage}\n\nDESCRIPTION\n  ${description}\n`;
}

function syncStatus(ctx, ambient) {
  if (!ctx.setStatus) return;
  const s = ambient.state();
  ctx.setStatus('theme', s.label || s.theme);
  ctx.setStatus('crt', s.crt ? 'on' : 'off');
}

function describeError(err) {
  if (err && err.kind === 'offline') return 'api unreachable';
  if (err && err.kind === 'unauthorized') return "401 — type 'auth <token>'";
  return err && err.message ? err.message : 'unavailable';
}

async function count(ctx, path, query, pick) {
  try {
    const body = await ctx.api.get(path, query);
    const n = pick(body);
    if (!Number.isFinite(n)) return 'unavailable — bad response';
    return String(n);
  } catch (err) {
    return `unavailable — ${describeError(err)}`;
  }
}

export function registerShell(registry, { ambient }) {
  function showMan(ctx, name) {
    const spec = registry.get(name);
    if (!spec) {
      ctx.out.line(`man: no manual entry for ${name}`, 'error');
      return undefined;
    }
    return ctx.out.man(spec.man || manPage(spec.name, spec.summary || '', spec.usage || spec.name, ''));
  }

  const toggleCmd = (name, setter, label) => ({
    name,
    summary: `${label} on or off`,
    usage: `${name} on|off`,
    man: manPage(name, `${label} on or off`, `${name} on|off`, `Turns the ${label} layer on or off. The hc theme keeps it off.`),
    run(ctx, args) {
      const v = args[0];
      if (v !== 'on' && v !== 'off') {
        ctx.out.line(`${name}: usage ${name} on|off`, 'error');
        return;
      }
      setter(v === 'on');
      syncStatus(ctx, ambient);
      ctx.out.line(`${name}: ${v}`, 'success');
    },
  });

  registry.register({
    name: 'help',
    summary: 'list commands',
    usage: 'help [command]',
    man: manPage('help', 'list commands', 'help [command]', "With no argument lists every command. 'help <command>' shows its manual page."),
    run(ctx, args) {
      if (args[0]) return showMan(ctx, args[0]);
      const pairs = registry.names().map((n) => [n, registry.get(n).summary || '']);
      ctx.out.kv(pairs);
      ctx.out.line("type 'man <command>' for details.", 'dim');
      return undefined;
    },
  });

  registry.register({
    name: 'man',
    summary: 'show a command manual',
    usage: 'man <command>',
    man: manPage('man', 'show a command manual', 'man <command>', 'Types out the manual page for a command.'),
    run(ctx, args) {
      if (!args[0]) {
        ctx.out.line('man: usage man <command>', 'error');
        return undefined;
      }
      return showMan(ctx, args[0]);
    },
  });

  registry.register({
    name: 'clear',
    summary: 'clear the screen',
    usage: 'clear',
    man: manPage('clear', 'clear the screen', 'clear', 'Clears the scrollback. Ctrl+L does the same.'),
    run(ctx) {
      ctx.out.clear();
    },
  });

  registry.register({
    name: 'history',
    summary: 'show entered commands',
    usage: 'history',
    man: manPage('history', 'show entered commands', 'history', 'Lists commands entered this session. Token lines are never kept.'),
    run(ctx) {
      const all = ctx.history ? ctx.history.all() : [];
      if (all.length === 0) {
        ctx.out.line('history: empty', 'dim');
        return;
      }
      const width = String(all.length).length;
      all.forEach((line, i) => ctx.out.line(`${String(i + 1).padStart(width, ' ')}  ${line}`));
    },
  });

  registry.register({
    name: 'neofetch',
    summary: 'banner, edition and live counts',
    usage: 'neofetch',
    man: manPage('neofetch', 'banner, edition and live counts', 'neofetch', 'Prints the banner with the api base and document and run counts read from the api.'),
    async run(ctx) {
      if (ctx.banner) {
        const art = String(ctx.banner).replace(/\s+$/, '');
        if (typeof ctx.out.banner === 'function') ctx.out.banner(art);
        else ctx.out.pre(art);
      }
      const [docs, runs] = await Promise.all([
        count(ctx, '/v1/documents', { limit: 500 }, (b) => (Array.isArray(b?.documents) ? b.documents.length : NaN)),
        count(ctx, '/v1/runs', undefined, (b) => (Array.isArray(b?.runs) ? b.runs.length : NaN)),
      ]);
      const base = ctx.apiBase || (globalThis.location && globalThis.location.origin) || 'same-origin';
      const docCount = docs === '500' ? '500+' : docs;
      ctx.out.kv([
        ['edition', 'terminal'],
        ['api', base],
        ['documents', docCount],
        ['runs', runs],
        ['theme', ambient.state().label],
      ]);
    },
  });

  registry.register({
    name: 'theme',
    summary: 'show or set the colour theme',
    usage: 'theme [dark|light|hc|amber|green|cyan]',
    man: manPage(
      'theme',
      'show or set the colour theme',
      'theme [dark|light|hc|amber|green|cyan]',
      'dark, light and hc set the base scheme. amber, green and cyan set the signature phosphor. hc turns the CRT and skyline off. The choice is remembered.',
    ),
    run(ctx, args) {
      if (args.length === 0) {
        ctx.out.line(`theme: ${ambient.state().label}`);
        ctx.out.line(`available: ${THEME_NAMES.join(' ')}`, 'dim');
        return;
      }
      const name = args[0];
      if (args.length > 1 || !THEME_NAMES.includes(name)) {
        ctx.out.line(THEME_USAGE, 'error');
        return;
      }
      if (name === 'hc') {
        ambient.setCrt(false);
        ambient.setSkyline(false);
      }
      ambient.setTheme(name);
      syncStatus(ctx, ambient);
      ctx.out.line(`theme: ${ambient.state().label}`, 'success');
    },
  });

  registry.register(toggleCmd('crt', (on) => ambient.setCrt(on), 'crt overlay'));
  registry.register(toggleCmd('skyline', (on) => ambient.setSkyline(on), 'skyline'));
}
