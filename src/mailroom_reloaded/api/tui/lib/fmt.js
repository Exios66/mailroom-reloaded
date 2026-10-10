// Formatters shared by /tui commands: byte sizes, durations and US dollars.
// Every function returns a printable string and never throws; a value that is
// not a finite number renders as an em dash.

const DASH = '—';
const finite = (v) => typeof v === 'number' && Number.isFinite(v);
const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];

/** 0 -> "0 B", 1536 -> "1.5 KiB", 5 * 2**30 -> "5.0 GiB" (binary units). */
export function bytes(n) {
  if (!finite(n) || n < 0) return DASH;
  let v = n;
  let i = 0;
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024;
    i++;
  }
  return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(1)} ${UNITS[i]}`;
}

const pad2 = (v) => String(v).padStart(2, '0');

/** Milliseconds: "850 ms", "1.2 s", "3m 05s", "2h 03m", "1d 04h". */
export function duration(ms) {
  if (!finite(ms) || ms < 0) return DASH;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const total = Math.floor(s);
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (d > 0) return `${d}d ${pad2(h)}h`;
  if (h > 0) return `${h}h ${pad2(m)}m`;
  return `${m}m ${pad2(sec)}s`;
}

/** Dollars: four decimals under $1 so small LLM costs stay visible, else two. */
export function usd(v) {
  if (!finite(v)) return DASH;
  const sign = v < 0 ? '-' : '';
  const a = Math.abs(v);
  return `${sign}$${a < 1 ? a.toFixed(4) : a.toFixed(2)}`;
}
