// Pure command engine for /tui: line parsing, command registry, input history.
// No DOM and no imports, so it runs under `node --test` as well as in the browser.

const UNKNOWN_SUFFIX = ' — try help';

/**
 * Split one input line into a command, positional args and flags.
 * Whitespace separates tokens; single or double quotes group them.
 * `--k v`, `--k=v` and bare `--k` (true) are flags.
 */
export function parseLine(line) {
  const tokens = [];
  let current = '';
  let inToken = false;
  let quote = null;
  let quoted = false;

  for (const ch of String(line ?? '')) {
    if (quote) {
      if (ch === quote) {
        quote = null;
      } else {
        current += ch;
      }
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      quoted = true;
      inToken = true;
      continue;
    }
    if (/\s/.test(ch)) {
      if (inToken) {
        tokens.push({ text: current, quoted });
        current = '';
        inToken = false;
        quoted = false;
      }
      continue;
    }
    current += ch;
    inToken = true;
  }

  if (quote) {
    return { cmd: '', args: [], flags: {}, error: 'unterminated quote' };
  }
  if (inToken) {
    tokens.push({ text: current, quoted });
  }

  if (tokens.length === 0) {
    return { cmd: '', args: [], flags: {} };
  }

  const cmd = tokens[0].text;
  const args = [];
  const flags = {};
  for (let i = 1; i < tokens.length; i++) {
    const tok = tokens[i];
    const isFlag = !tok.quoted && tok.text.startsWith('--') && tok.text.length > 2;
    if (!isFlag) {
      args.push(tok.text);
      continue;
    }
    const body = tok.text.slice(2);
    const eq = body.indexOf('=');
    if (eq !== -1) {
      flags[body.slice(0, eq)] = body.slice(eq + 1);
      continue;
    }
    const next = tokens[i + 1];
    if (next && !next.quoted && !next.text.startsWith('--')) {
      flags[body] = next.text;
      i++;
    } else {
      flags[body] = true;
    }
  }
  return { cmd, args, flags };
}

function commonPrefix(strings) {
  if (strings.length === 0) return '';
  let prefix = strings[0];
  for (const s of strings.slice(1)) {
    let i = 0;
    while (i < prefix.length && i < s.length && prefix[i] === s[i]) i++;
    prefix = prefix.slice(0, i);
  }
  return prefix;
}

/**
 * Registry of commands. `spec` is
 * {name, summary, usage, man, run(ctx, args, flags)}.
 */
export function createRegistry() {
  const specs = new Map();

  return {
    register(spec) {
      if (!spec || typeof spec.name !== 'string' || spec.name === '') {
        throw new Error('command spec needs a name');
      }
      if (specs.has(spec.name)) {
        throw new Error(`duplicate command: ${spec.name}`);
      }
      specs.set(spec.name, spec);
    },
    get(name) {
      return specs.get(name);
    },
    names() {
      return [...specs.keys()].sort();
    },
    /**
     * Complete the command name for a line that has no argument yet.
     * `ghost` is the suffix that would be added to the line.
     */
    complete(line) {
      const text = String(line ?? '');
      if (/\s/.test(text)) {
        return { matches: [], ghost: '' };
      }
      if (text === '') {
        return { matches: this.names(), ghost: '' };
      }
      const matches = this.names().filter((n) => n.startsWith(text));
      if (matches.length === 0) {
        return { matches, ghost: '' };
      }
      const shared = commonPrefix(matches);
      return { matches, ghost: shared.slice(text.length) };
    },
  };
}

/** Input history with prev/next navigation. Empty and consecutive duplicate lines are skipped. */
export function createHistory(max = 200) {
  const entries = [];
  let cursor = 0;

  return {
    push(line) {
      const text = String(line ?? '');
      if (text.trim() === '') return;
      if (entries.length > 0 && entries[entries.length - 1] === text) {
        cursor = entries.length;
        return;
      }
      entries.push(text);
      while (entries.length > max) entries.shift();
      cursor = entries.length;
    },
    prev() {
      if (entries.length === 0) return undefined;
      if (cursor > 0) cursor--;
      return entries[cursor];
    },
    next() {
      if (cursor >= entries.length) return undefined;
      cursor++;
      if (cursor === entries.length) return undefined;
      return entries[cursor];
    },
    reset() {
      cursor = entries.length;
    },
    all() {
      return entries.slice();
    },
  };
}

/**
 * Parse and run one line. Resolves to 'ok', 'unknown' or 'error'.
 * Output goes through `ctx.out.line(text, kind)`.
 */
export async function dispatch(registry, ctx, line) {
  const parsed = parseLine(line);
  if (parsed.error) {
    ctx.out.line(`parse error: ${parsed.error}`, 'error');
    return 'error';
  }
  if (parsed.cmd === '') {
    return 'ok';
  }
  const spec = registry.get(parsed.cmd);
  if (!spec) {
    ctx.out.line(`${parsed.cmd}: command not found${UNKNOWN_SUFFIX}`, 'error');
    return 'unknown';
  }
  try {
    await spec.run(ctx, parsed.args, parsed.flags);
    return 'ok';
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    ctx.out.line(`${parsed.cmd}: ${message}`, 'error');
    return 'error';
  }
}
