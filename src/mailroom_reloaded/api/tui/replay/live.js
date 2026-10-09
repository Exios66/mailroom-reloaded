// Follow-live SSE reader for the replay viewer. Consumes GET /v1/replay/live over
// fetch streaming, parses `event:`/`data:` frames, and reconnects with exponential
// backoff until stopped. No DOM, no dependencies, so `node --test` can exercise it.

const DEFAULT_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30000;
const FRAME_SEP = '\n\n';

/** Parse one SSE frame block into `{name, data}`; null when it carries no data. */
export function parseSseFrame(raw) {
  let name = 'message';
  const data = [];
  for (const line of String(raw).split('\n')) {
    if (line.startsWith('event:')) name = line.slice(6).replace(/^ /, '');
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
    // ':' comments plus 'id:' and 'retry:' are ignored.
  }
  if (data.length === 0) return null;
  const text = data.join('\n');
  let payload = text;
  try {
    payload = JSON.parse(text);
  } catch {
    // Keep the raw string: a malformed data line must not tear down the stream.
  }
  return { name, data: payload };
}

/**
 * Follow an SSE endpoint. Calls `onFrame(name, obj)` per frame and reconnects with
 * exponential backoff (`min(30000, ms*2)`, base `backoffMs`, default 1000) until
 * `stop()` or the caller's `signal` aborts.
 *
 * @param {{fetchFn: Function, url: string, headers?: object, signal?: AbortSignal,
 *   onFrame: Function, backoffMs?: number}} opts
 * @returns {{stop: () => void, done: Promise<void>}}
 */
export function createFollowReader({
  fetchFn,
  url,
  headers,
  signal,
  onFrame,
  backoffMs = DEFAULT_BACKOFF_MS,
} = {}) {
  const base =
    typeof backoffMs === 'number' && Number.isFinite(backoffMs) && backoffMs > 0
      ? backoffMs
      : DEFAULT_BACKOFF_MS;
  let stopped = false;
  let controller = null;
  let buffer = '';
  let wake = null;
  let resolveDone;
  const done = new Promise((resolve) => {
    resolveDone = resolve;
  });

  function stop() {
    if (stopped) return;
    stopped = true;
    if (signal) signal.removeEventListener('abort', stop);
    if (controller) controller.abort();
    if (wake) wake();
  }

  if (signal) {
    if (signal.aborted) stopped = true;
    else signal.addEventListener('abort', stop, { once: true });
  }

  function wait(ms) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        wake = null;
        resolve();
      }, ms);
      wake = () => {
        clearTimeout(timer);
        wake = null;
        resolve();
      };
    });
  }

  function emitFrame(raw) {
    const frame = parseSseFrame(raw);
    if (!frame) return;
    if (typeof onFrame === 'function') onFrame(frame.name, frame.data);
  }

  async function pump(body) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    buffer = '';
    for (;;) {
      const { value, done: finished } = await reader.read();
      if (finished) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, '\n');
      let sep = buffer.indexOf(FRAME_SEP);
      while (sep >= 0) {
        emitFrame(buffer.slice(0, sep));
        buffer = buffer.slice(sep + FRAME_SEP.length);
        sep = buffer.indexOf(FRAME_SEP);
      }
    }
    buffer += decoder.decode();
    buffer = buffer.replace(/\r\n/g, '\n');
    if (buffer.trim() !== '') emitFrame(buffer);
    buffer = '';
  }

  async function run() {
    let backoff = base;
    while (!stopped) {
      try {
        controller = new AbortController();
        const res = await fetchFn(url, { headers, signal: controller.signal });
        if (!res || res.ok !== true || !res.body) {
          throw new Error(`live stream http ${res && res.status ? res.status : '?'}`);
        }
        backoff = base; // a good connection resets the climb
        await pump(res.body);
      } catch {
        // refused, dropped body or an abort: fall through to the backoff wait
      }
      if (stopped) break;
      await wait(backoff);
      backoff = Math.min(MAX_BACKOFF_MS, backoff * 2);
    }
    resolveDone();
  }

  if (stopped) resolveDone();
  else run();

  return { stop, done };
}
