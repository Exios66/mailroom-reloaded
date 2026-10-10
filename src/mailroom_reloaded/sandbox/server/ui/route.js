"use strict";
// URL fragment routing for the sandbox UI: parseRoute turns "#tab=messages&mailbox=open&role=correspondent"
// into a validated state object and formatRoute does the reverse. Pure functions, no DOM access.
// Only allow-listed keys with well-formed values survive; everything else is dropped, never thrown.
// The fragment must never carry the API token, so no key for it exists here.
(function (root) {
  const TABS = ["messages", "boss", "outbox", "events", "conformance", "docs", "policy"];
  const ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
  const MAX_HASH = 2048;
  const KEYS = {
    tab: (v) => TABS.includes(v),
    scenario: (v) => ID.test(v),
    sel: (v) => ID.test(v),
    mailbox: (v) => v === "open" || v === "closed",
    role: (v) => v === "correspondent" || v === "boss",
    thread: (v) => ID.test(v),
  };
  const ORDER = Object.keys(KEYS);

  /** Percent-decode one token, or return null when the escape sequence is malformed. */
  function decode(s) {
    try { return decodeURIComponent(s); } catch (e) { return null; }
  }
  /**
   * Parse a location hash (leading "#" optional) into {key: value} for the allow-listed keys.
   * The first occurrence of a key wins even when its value is rejected, so a later duplicate
   * cannot override it. Non-strings and fragments over 2048 characters yield {}.
   */
  function parseRoute(hash) {
    const out = {};
    if (typeof hash !== "string" || hash.length > MAX_HASH) return out;
    const seen = new Set();
    for (const part of (hash.charAt(0) === "#" ? hash.slice(1) : hash).split("&")) {
      const eq = part.indexOf("=");
      if (eq < 1) continue;
      const k = decode(part.slice(0, eq));
      if (k === null || !Object.prototype.hasOwnProperty.call(KEYS, k) || seen.has(k)) continue;
      seen.add(k);
      const v = decode(part.slice(eq + 1));
      if (v !== null && KEYS[k](v)) out[k] = v;
    }
    return out;
  }
  /** Build a canonical hash ("#k=v&...", or "" when empty) from a state object, skipping invalid or unknown entries. */
  function formatRoute(state) {
    const s = state || {};
    const parts = [];
    for (const k of ORDER) {
      const v = s[k];
      if (typeof v === "string" && KEYS[k](v)) parts.push(k + "=" + encodeURIComponent(v));
    }
    return parts.length ? "#" + parts.join("&") : "";
  }
  root.sbxRoute = { parseRoute, formatRoute, TABS };
})(typeof window !== "undefined" ? window : globalThis);
