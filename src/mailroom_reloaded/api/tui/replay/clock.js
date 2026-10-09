// Pure playback clock for the replay viewer. The caller supplies monotonic
// millisecond timestamps; this module never reads Date or performance.

export const SPEEDS = [0.5, 1, 2, 4, 8, 16];

function cleanDuration(d) {
  return typeof d === 'number' && Number.isFinite(d) && d > 0 ? d : 0;
}

/**
 * Create a playback clock over `[0, duration]` seconds.
 * @param {{duration: number, speed?: number}} opts
 */
export function createClock({ duration, speed = 1 } = {}) {
  const total = cleanDuration(duration);
  let t = 0;
  let playing = false;
  let anchor = null; // ms of the last processed timestamp while playing
  let rate = SPEEDS.includes(speed) ? speed : 1;

  function clamp(v) {
    return Math.min(total, Math.max(0, v));
  }

  function tick(nowMs) {
    if (!playing || typeof nowMs !== 'number' || !Number.isFinite(nowMs)) return t;
    if (anchor === null) {
      anchor = nowMs;
      return t;
    }
    if (nowMs <= anchor) return t;
    t = clamp(t + ((nowMs - anchor) / 1000) * rate);
    anchor = nowMs;
    if (t >= total) {
      t = total;
      playing = false;
      anchor = null;
    }
    return t;
  }

  function play(nowMs) {
    if (playing) return;
    if (total > 0 && t >= total) t = 0;
    playing = true;
    anchor = typeof nowMs === 'number' && Number.isFinite(nowMs) ? nowMs : null;
  }

  function pause(nowMs) {
    tick(nowMs);
    playing = false;
    anchor = null;
  }

  function toggle(nowMs) {
    if (playing) pause(nowMs);
    else play(nowMs);
  }

  function seek(v) {
    if (typeof v !== 'number' || Number.isNaN(v)) return t;
    t = clamp(v);
    anchor = null;
    if (playing && t >= total) playing = false;
    return t;
  }

  function step(dt) {
    if (typeof dt !== 'number' || !Number.isFinite(dt)) return t;
    return seek(t + dt);
  }

  function setSpeed(s, nowMs) {
    if (!SPEEDS.includes(s)) return false;
    tick(nowMs);
    rate = s;
    return true;
  }

  function shift(delta, nowMs) {
    const i = SPEEDS.indexOf(rate) + delta;
    const next = SPEEDS[Math.min(SPEEDS.length - 1, Math.max(0, i))];
    return setSpeed(next, nowMs);
  }

  function state() {
    return {
      t,
      playing,
      speed: rate,
      duration: total,
      ended: !playing && total > 0 && t >= total,
    };
  }

  return {
    play,
    pause,
    toggle,
    seek,
    step,
    setSpeed,
    faster: (nowMs) => shift(1, nowMs),
    slower: (nowMs) => shift(-1, nowMs),
    tick,
    state,
  };
}
