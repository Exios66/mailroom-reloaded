// Ambient layers for /tui: skyline, conveyor sparks, CRT/grain toggles and theme switching.
// DOM access goes through `root` (a document); everything is optional so it degrades quietly.

export const THEME_KEY = 'mailroom.tui.theme';
export const THEMES = ['dark', 'light', 'hc', 'amber', 'green', 'cyan'];
const BASE_THEMES = new Set(['dark', 'light', 'hc']);
const SVG_NS = 'http://www.w3.org/2000/svg';
const SPARK_COUNT = 9;
const FLASH_MS = 350;

/** The kit's buildSkyline(): a 1440x120 ridgeline of overlapping roof peaks. */
export function buildSkylinePath(rnd) {
  const W = 1440;
  const H = 120;
  let d = `M0 ${H} L0 60`;
  let x = 0;
  while (x < W) {
    const w = 30 + rnd() * 46;
    const h = 22 + rnd() * 52;
    d += ` L${(x + w / 2).toFixed(1)} ${(60 - h).toFixed(1)} L${(x + w).toFixed(1)} 60`;
    x += w * 0.55;
  }
  return `${d} L${W} 60 L${W} ${H} Z`;
}

function readSaved(storage) {
  try {
    const raw = storage && storage.getItem(THEME_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw);
    if (!v || !BASE_THEMES.has(v.theme)) return null;
    const phosphor = v.phosphor === 'green' || v.phosphor === 'cyan' ? v.phosphor : null;
    return { theme: v.theme, phosphor };
  } catch {
    return null;
  }
}

function writeSaved(storage, value) {
  try {
    if (storage) storage.setItem(THEME_KEY, JSON.stringify(value));
  } catch {
    /* storage may be blocked; the theme still applies for this page */
  }
}

function defaultStorage() {
  try {
    return globalThis.localStorage || null;
  } catch {
    return null;
  }
}

/**
 * @param {Document} root
 * @param {{reducedMotion?: boolean, storage?: Storage|null, rnd?: () => number}} [opts]
 */
export function createAmbient(root, { reducedMotion = false, storage, rnd = Math.random } = {}) {
  const store = storage === undefined ? defaultStorage() : storage;
  const q = (sel) => (root && root.querySelector ? root.querySelector(sel) : null);
  const html = root && root.documentElement ? root.documentElement : null;
  const skylineEl = q('.ambient-skyline');
  const crtEl = q('.crt-overlay');
  const grainEl = q('.grain');

  const st = { theme: 'dark', phosphor: null, crt: true, skyline: true };

  function toggle(node, on) {
    if (!node || !node.classList) return;
    if (on) node.classList.remove('off');
    else node.classList.add('off');
  }

  // ---- skyline + sparks ----
  if (skylineEl && root.createElementNS) {
    for (const [cls, pathCls] of [
      ['back', 'sky-2'],
      ['front', 'sky-1'],
    ]) {
      const svg = root.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('class', `skyline ${cls}`);
      svg.setAttribute('viewBox', '0 0 1440 120');
      svg.setAttribute('preserveAspectRatio', 'none');
      const path = root.createElementNS(SVG_NS, 'path');
      path.setAttribute('class', pathCls);
      path.setAttribute('d', buildSkylinePath(rnd));
      svg.appendChild(path);
      skylineEl.appendChild(svg);
    }
  }
  if (skylineEl && !reducedMotion) {
    const box = root.createElement('div');
    box.className = 'ambient-sparks';
    for (let i = 0; i < SPARK_COUNT; i++) {
      const dot = root.createElement('div');
      dot.className = 'conveyor-dot';
      const set = (k, v) => dot.style.setProperty(k, v);
      dot.style.left = `${(4 + rnd() * 92).toFixed(1)}%`;
      dot.style.top = `${(8 + rnd() * 80).toFixed(1)}%`;
      dot.style.animationDelay = `${(rnd() * 4).toFixed(2)}s`;
      set('--dx', `${((rnd() * 2 - 1) * 30).toFixed(1)}px`);
      set('--dy', `${((rnd() * 2 - 1) * 20).toFixed(1)}px`);
      set('--drift', `${(4 + rnd() * 6).toFixed(2)}s`);
      set('--glow', `${(2.5 + rnd() * 4).toFixed(2)}s`);
      box.appendChild(dot);
    }
    skylineEl.appendChild(box);
  }
  if (grainEl && grainEl.setAttribute) grainEl.setAttribute('aria-hidden', 'true');

  function setCrt(on) {
    st.crt = Boolean(on);
    toggle(crtEl, st.crt);
  }
  function setSkyline(on) {
    st.skyline = Boolean(on);
    toggle(skylineEl, st.skyline);
  }

  function applyTheme(theme, phosphor) {
    if (html) {
      html.setAttribute('data-theme', theme);
      if (phosphor) html.setAttribute('data-phosphor', phosphor);
      else html.removeAttribute('data-phosphor');
    }
  }

  function flash() {
    if (reducedMotion || !html || !html.classList) return;
    html.classList.add('theme-flash');
    setTimeout(() => html.classList.remove('theme-flash'), FLASH_MS);
  }

  /** Returns false for an unknown name. hc forces CRT and skyline off. */
  function setTheme(name, { persist = true, animate = true } = {}) {
    if (!THEMES.includes(name)) return false;
    if (BASE_THEMES.has(name)) {
      st.theme = name;
      if (name === 'hc') st.phosphor = null;
    } else if (name === 'amber') {
      st.phosphor = null;
      if (st.theme === 'hc') st.theme = 'dark';
    } else {
      st.phosphor = name;
      if (st.theme === 'hc') st.theme = 'dark';
    }
    applyTheme(st.theme, st.phosphor);
    if (st.theme === 'hc') {
      setCrt(false);
      setSkyline(false);
    }
    if (animate) flash();
    if (persist) writeSaved(store, { theme: st.theme, phosphor: st.phosphor });
    return true;
  }

  function label() {
    return st.phosphor ? `${st.theme}/${st.phosphor}` : st.theme;
  }

  const saved = readSaved(store);
  if (saved) {
    st.theme = saved.theme;
    st.phosphor = saved.theme === 'hc' ? null : saved.phosphor;
    applyTheme(st.theme, st.phosphor);
    if (st.theme === 'hc') {
      setCrt(false);
      setSkyline(false);
    }
  }

  return {
    setCrt,
    setSkyline,
    setTheme,
    state: () => ({ ...st, label: label() }),
  };
}
