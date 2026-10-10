// Plain-text table renderer for /tui commands. Returns lines for ctx.out.line (textContent),
// so cells are never markup; control, zero-width and bidi characters become spaces so a
// hostile value cannot reorder or hide columns.

const CTRL_RE = new RegExp(
  '[\\u0000-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ufeff]',
  'g',
);
const DASH = '—';

/** One printable single-line cell. */
export function cell(v) {
  if (v === null || v === undefined || v === '') return DASH;
  let s;
  try {
    s = typeof v === 'object' ? JSON.stringify(v) : String(v);
  } catch {
    s = '';
  }
  return typeof s === 'string' && s !== '' ? s.replace(CTRL_RE, ' ') : DASH;
}

function fit(text, width, align) {
  const chars = Array.from(text);
  if (chars.length > width) return width <= 1 ? chars.slice(0, width).join('') : `${chars.slice(0, width - 1).join('')}…`;
  const padding = ' '.repeat(width - chars.length);
  return align === 'right' ? padding + text : text + padding;
}

/**
 * Render rows as aligned lines: a header, a rule, then one line per row.
 * `columns` is [{key, label?, align?: 'left'|'right', max?: number, format?: (v, row) => any}].
 * A column is as wide as its widest cell, capped by `max` (default 40); longer cells end in `…`.
 * `rows` that are not objects are skipped. Returns [] when there are no columns.
 */
export function renderTable(rows, columns, { gap = 2, header = true } = {}) {
  const cols = Array.isArray(columns) ? columns.filter((c) => c && typeof c.key === 'string') : [];
  if (cols.length === 0) return [];
  const body = (Array.isArray(rows) ? rows : [])
    .filter((r) => r !== null && typeof r === 'object')
    .map((r) =>
      cols.map((c) => {
        let v = r[c.key];
        if (typeof c.format === 'function') {
          try {
            v = c.format(v, r);
          } catch {
            v = DASH;
          }
        }
        return cell(v);
      }),
    );
  const labels = cols.map((c) => cell(c.label ?? c.key));
  const widths = cols.map((c, i) => {
    const max = Number.isInteger(c.max) && c.max > 0 ? c.max : 40;
    const widest = Math.max(header ? Array.from(labels[i]).length : 0, ...body.map((r) => Array.from(r[i]).length), 1);
    return Math.min(max, widest);
  });
  const sep = ' '.repeat(Math.max(0, gap));
  const line = (cells) => cells.map((t, i) => fit(t, widths[i], cols[i].align)).join(sep).replace(/\s+$/, '');
  const out = [];
  if (header) {
    out.push(line(labels));
    out.push(widths.map((w) => '─'.repeat(w)).join(sep));
  }
  for (const r of body) out.push(line(r));
  return out;
}
