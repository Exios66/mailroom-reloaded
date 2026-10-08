// Terminal renderer and prompt line for /tui.
// Every node is built with createElement + textContent; no method accepts HTML.
// The render* helpers take `doc` so they run under `node --test` with a tiny DOM stub.

import { dispatch, parseLine } from './engine.js';

export const MAX_SCROLLBACK = 1000;
const MASK = '••••';
const LISTING_KINDS = new Set(['dir', 'md', 'hidden']);
const MAN_MS_PER_CHAR = 1.4;

/** Keep the last `max` entries. */
export function capScrollback(lines, max = MAX_SCROLLBACK) {
  return lines.length > max ? lines.slice(lines.length - max) : lines;
}

/** `auth <token>` becomes `auth ••••`; `auth --clear` and every other line are untouched. */
export function maskCommand(line) {
  const text = String(line);
  const parsed = parseLine(text);
  if (parsed.error) {
    // An unterminated quote never runs, but the echo must not leak a token typed so far.
    return /^\s*["']?auth/.test(text) && /\s/.test(text.trim()) ? `auth ${MASK}` : line;
  }
  if (parsed.cmd !== 'auth') return line;
  const flagNames = Object.keys(parsed.flags);
  if (parsed.args.length === 0 && flagNames.length === 0) return line; // bare `auth`
  if (parsed.args.length === 0 && flagNames.length === 1 && parsed.flags.clear === true) {
    return line; // `auth --clear`
  }
  return `auth ${MASK}`;
}

function el(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

export function renderLine(doc, text, cls) {
  const extra = cls ? ` ${String(cls).trim()}` : '';
  return el(doc, 'div', `line${extra}`, text);
}

export function renderPre(doc, text) {
  return el(doc, 'pre', 'out-pre', text);
}

export function renderTable(doc, headers, rows, opts = {}) {
  const table = el(doc, 'table', 'mr');
  const thead = el(doc, 'thead');
  const headRow = el(doc, 'tr');
  for (const h of headers) headRow.appendChild(el(doc, 'th', '', h));
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el(doc, 'tbody');
  rows.forEach((row, rowIdx) => {
    const tr = el(doc, 'tr');
    row.forEach((cell, col) => {
      const cls = opts.cellClass ? opts.cellClass(rowIdx, col) : '';
      tr.appendChild(el(doc, 'td', cls || '', cell));
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

export function renderKv(doc, pairs) {
  const kv = el(doc, 'div', 'kv');
  for (const [k, v] of pairs) {
    kv.appendChild(el(doc, 'b', '', k));
    kv.appendChild(el(doc, 'span', '', v));
  }
  return kv;
}

export function renderListing(doc, items) {
  const listing = el(doc, 'div', 'listing');
  for (const item of items) {
    const kind = LISTING_KINDS.has(item.kind) ? ` ${item.kind}` : '';
    listing.appendChild(el(doc, 'span', `file${kind}`, item.name));
  }
  return listing;
}

export function renderBanner(doc, text) {
  const card = el(doc, 'div', 'title-card');
  card.setAttribute('aria-hidden', 'true');
  card.appendChild(el(doc, 'pre', 'banner', text));
  return card;
}

export function renderDivider(doc, width = 72) {
  const rule = el(doc, 'div', 'divider', '─'.repeat(width));
  rule.setAttribute('aria-hidden', 'true');
  return rule;
}

export function renderMan(doc) {
  const box = el(doc, 'div', 'man-page');
  // Typed out frame by frame: keep the live region from re-announcing every update.
  box.setAttribute('aria-live', 'off');
  const pre = el(doc, 'pre', '', '');
  box.appendChild(pre);
  return { box, pre };
}

function prefersReducedMotion() {
  try {
    return globalThis.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

const STATUS_LEFT = [
  { key: 'pwd', label: '', value: '~', essential: true },
  { key: 'theme', label: 'theme ', value: 'dark' },
  { key: 'crt', label: 'crt ', value: 'on' },
];
const STATUS_RIGHT = [
  { key: 'api', label: 'api ', value: '…' },
  { key: 'clock', label: '', value: '', essential: true },
];

/**
 * @param {{root: Document, registry: object, history: object, api: object, cwd?: string}} opts
 * `root` is the document (or any object with querySelector/createElement).
 */
export function createTerminal({ root, registry, history, api, cwd = '~' }) {
  const doc = root;
  const output = doc.querySelector('#output');
  const promptLine = doc.querySelector('.prompt-line');
  const statusBar = doc.querySelector('.status-bar');

  // ---- status bar ----
  const statusEls = new Map();
  function statusItem(spec) {
    const item = el(doc, 'span', `status-item${spec.essential ? ' essential' : ''}`);
    if (spec.label) item.appendChild(el(doc, 'span', '', spec.label));
    const b = el(doc, 'b', '', spec.value);
    item.appendChild(b);
    statusEls.set(spec.key, b);
    return item;
  }
  if (statusBar) {
    const left = el(doc, 'div', 'status-group');
    const dotItem = el(doc, 'span', 'status-item essential');
    dotItem.appendChild(el(doc, 'span', 'dot'));
    dotItem.appendChild(el(doc, 'span', '', 'mailroom@floor'));
    left.appendChild(dotItem);
    for (const spec of STATUS_LEFT) left.appendChild(statusItem(spec));
    const linkItem = el(doc, 'span', 'status-item essential');
    const link = el(doc, 'a', 'sister-link', 'ui ↗');
    link.setAttribute('href', '/ui');
    linkItem.appendChild(link);
    left.appendChild(linkItem);
    const right = el(doc, 'div', 'status-group');
    for (const spec of STATUS_RIGHT) right.appendChild(statusItem(spec));
    statusBar.appendChild(left);
    statusBar.appendChild(right);
  }
  function setStatus(key, value) {
    const node = statusEls.get(key);
    if (node) node.textContent = String(value);
  }
  function tickClock() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    setStatus('clock', `${p(d.getHours())}:${p(d.getMinutes())}`);
  }
  tickClock();
  const clockTimer = setInterval(tickClock, 30000);
  if (clockTimer && typeof clockTimer.unref === 'function') clockTimer.unref();

  // ---- scrollback ----
  function append(node) {
    output.appendChild(node);
    while (output.childElementCount > MAX_SCROLLBACK && output.firstChild) {
      output.firstChild.remove();
    }
    output.scrollTop = output.scrollHeight;
    return node;
  }

  function clearOutput() {
    while (output.firstChild) output.removeChild(output.firstChild);
    if (prefersReducedMotion()) return;
    const sweep = el(doc, 'div', 'clear-sweep');
    sweep.setAttribute('aria-hidden', 'true');
    doc.body.appendChild(sweep);
    setTimeout(() => sweep.remove(), 460);
  }

  function man(text, { instant = false } = {}) {
    const { box, pre } = renderMan(doc);
    append(box);
    const full = String(text);
    if (instant || prefersReducedMotion() || full.length === 0) {
      pre.textContent = full;
      output.scrollTop = output.scrollHeight;
      return Promise.resolve();
    }
    const sig = controller.signal;
    return new Promise((resolve) => {
      const start = performance.now();
      let shown = 0;
      function frame(now) {
        if (sig.aborted) {
          pre.textContent = full;
          output.scrollTop = output.scrollHeight;
          resolve();
          return;
        }
        const target = Math.min(full.length, Math.floor((now - start) / MAN_MS_PER_CHAR));
        if (target > shown) {
          shown = target;
          pre.textContent = full.slice(0, shown);
          output.scrollTop = output.scrollHeight;
        }
        if (shown < full.length) requestAnimationFrame(frame);
        else resolve();
      }
      requestAnimationFrame(frame);
    });
  }

  const out = {
    line: (text, cls) => append(renderLine(doc, text, cls)),
    pre: (text) => append(renderPre(doc, text)),
    banner: (text) => append(renderBanner(doc, text)),
    table: (headers, rows, opts) => append(renderTable(doc, headers, rows, opts)),
    kv: (pairs) => append(renderKv(doc, pairs)),
    listing: (items) => append(renderListing(doc, items)),
    divider: () => append(renderDivider(doc)),
    man,
    clear: clearOutput,
  };

  // ---- run / abort ----
  let controller = new AbortController();
  let busy = false;

  const ctx = {
    out,
    api,
    registry,
    history,
    signal: () => controller.signal,
    setStatus,
  };

  const ABORTED = Symbol('aborted');

  async function run(line, { warn } = {}) {
    const text = String(line);
    const masked = maskCommand(text);
    out.line(`${cwd} $ ${masked}`, 'cmd-echo');
    if (warn) out.line(warn, 'warn');
    if (masked === text) history.push(text);
    history.reset();
    const mine = new AbortController();
    controller = mine;
    busy = true;
    // Ctrl+C must free the prompt even if the command ignores its signal.
    const aborted = new Promise((resolve) => {
      mine.signal.addEventListener('abort', () => resolve(ABORTED), { once: true });
    });
    const running = dispatch(registry, ctx, text);
    running.catch(() => {});
    try {
      const result = await Promise.race([running, aborted]);
      return result === ABORTED ? 'aborted' : result;
    } finally {
      if (controller === mine) busy = false;
    }
  }

  // ---- prompt line ----
  const prompt = el(doc, 'span', 'prompt');
  prompt.appendChild(doc.createTextNode('mailroom@floor:'));
  prompt.appendChild(el(doc, 'span', 'path', cwd));
  prompt.appendChild(doc.createTextNode(' $'));
  const wrap = el(doc, 'div', 'input-wrap');
  const display = el(doc, 'div', 'input-display');
  const before = el(doc, 'span', 'typed');
  const cursor = el(doc, 'span', 'block-cursor');
  const after = el(doc, 'span', 'typed');
  const ghost = el(doc, 'span', 'ghost-text');
  display.appendChild(before);
  display.appendChild(cursor);
  display.appendChild(after);
  display.appendChild(ghost);
  const input = el(doc, 'input', 'prompt-input');
  input.type = 'text';
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('autocapitalize', 'off');
  input.setAttribute('autocorrect', 'off');
  input.setAttribute('spellcheck', 'false');
  input.setAttribute('aria-label', 'terminal input');
  display.setAttribute('aria-hidden', 'true');
  display.style.unicodeBidi = 'plaintext';
  wrap.appendChild(display);
  wrap.appendChild(input);
  promptLine.appendChild(prompt);
  promptLine.appendChild(wrap);

  let composing = false;
  let draft = '';
  let navigating = false;

  function refresh() {
    const v = input.value;
    const pos = input.selectionStart ?? v.length;
    const masked = maskCommand(v);
    if (masked !== v) {
      // Never paint a token (even while it is being typed) into the visible display.
      before.textContent = masked;
      after.textContent = '';
      ghost.textContent = '';
      return;
    }
    before.textContent = v.slice(0, pos);
    after.textContent = v.slice(pos);
    let g = '';
    if (pos === v.length && v !== '' && !composing) g = registry.complete(v).ghost;
    ghost.textContent = g;
  }

  function setValue(v) {
    input.value = v;
    try {
      input.setSelectionRange(v.length, v.length);
    } catch {
      /* some input states do not support selection */
    }
    refresh();
  }

  function focus() {
    input.focus({ preventScroll: true });
  }

  // Lines entered while a command runs are queued, not dropped.
  const queue = [];

  async function submit(extra) {
    const line = input.value;
    setValue('');
    draft = '';
    navigating = false;
    if (busy) {
      queue.push([line, extra]);
      return;
    }
    await run(line, extra);
    while (queue.length) {
      const [next, nextExtra] = queue.shift();
      await run(next, nextExtra);
    }
  }

  input.addEventListener('input', refresh);
  input.addEventListener('keyup', refresh);
  input.addEventListener('click', refresh);
  input.addEventListener('compositionstart', () => {
    composing = true;
    refresh();
  });
  input.addEventListener('compositionend', () => {
    composing = false;
    refresh();
  });

  input.addEventListener('paste', (e) => {
    const data = e.clipboardData ? e.clipboardData.getData('text') : '';
    if (!/[\r\n]/.test(data)) return;
    e.preventDefault();
    const rows = data.split(/\r\n|\r|\n/);
    while (rows.length > 1 && rows[rows.length - 1] === '') rows.pop();
    if (input.readOnly) return;
    const s = input.selectionStart ?? input.value.length;
    const t = input.selectionEnd ?? s;
    const next = input.value.slice(0, s) + rows[0] + input.value.slice(t);
    input.value = next;
    try {
      input.setSelectionRange(s + rows[0].length, s + rows[0].length);
    } catch {
      /* selection unsupported */
    }
    refresh();
    // Never auto-run pasted text: the user reviews the line and presses Enter.
    if (rows.length > 1) {
      out.line(
        `warn: pasted ${rows.length} lines — kept the first in the input, nothing was run`,
        'warn',
      );
    }
  });

  input.addEventListener('keydown', (e) => {
    if (e.isComposing || composing || e.keyCode === 229) return;
    if (e.ctrlKey && !e.metaKey && !e.altKey) {
      const k = e.key.toLowerCase();
      if (k === 'l') {
        e.preventDefault();
        out.clear();
        return;
      }
      if (k === 'c') {
        if (input.selectionStart !== input.selectionEnd) return; // let the browser copy
        e.preventDefault();
        controller.abort();
        queue.length = 0;
        out.line('^C', 'dim');
        if (!busy) {
          setValue('');
          history.reset();
        }
        return;
      }
    }
    switch (e.key) {
      case 'Enter':
        e.preventDefault();
        submit();
        break;
      case 'ArrowUp': {
        e.preventDefault();
        const prev = history.prev();
        if (prev !== undefined) {
          if (!navigating) draft = input.value;
          navigating = true;
          setValue(prev);
        }
        break;
      }
      case 'ArrowDown': {
        e.preventDefault();
        if (!navigating) break; // nothing to navigate: leave typed text alone
        const next = history.next();
        if (next === undefined) {
          navigating = false;
          setValue(draft);
        } else {
          setValue(next);
        }
        break;
      }
      case 'Tab': {
        const { matches, ghost: g } = registry.complete(input.value);
        if (g) {
          e.preventDefault();
          setValue(input.value + g);
        } else if (matches.length > 1 && input.value !== '') {
          e.preventDefault();
          out.line(matches.join('  '), 'dim');
        } // otherwise Tab moves focus as normal (no keyboard trap)
        break;
      }
      default:
    }
  });

  function refocusOnClick(e) {
    const sel = globalThis.getSelection ? globalThis.getSelection() : null;
    if (sel && String(sel).length > 0) return; // don't steal a text selection
    if (e.target && e.target.closest && e.target.closest('a')) return;
    focus();
  }
  output.addEventListener('click', refocusOnClick);
  promptLine.addEventListener('click', refocusOnClick);

  refresh();
  focus();

  function setInputEnabled(enabled) {
    input.readOnly = !enabled;
    input.setAttribute('aria-disabled', enabled ? 'false' : 'true');
  }

  return { ctx, run, focus, setInputEnabled };
}
