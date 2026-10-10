import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const UI = new URL("../../../src/mailroom_reloaded/sandbox/server/ui/", import.meta.url);
const read = (f) => readFileSync(new URL(f, UI), "utf8");

// Stub DOM: enough for app.js to boot, render a tab and the mailbox dock.
class N {
  constructor(tag) {
    this.tag = tag; this.className = ""; this.kids = []; this.attrs = {}; this.handlers = {}; this.dataset = {};
    this.nodeType = 1; this.disabled = false; this.value = ""; this.checked = false; this.textContent = "";
    const cls = new Set();
    this.classList = { toggle: (c, on) => (on === undefined ? !cls.has(c) : on) ? cls.add(c) : cls.delete(c), contains: (c) => cls.has(c) };
  }
  append(...k) { this.kids.push(...k); }
  setAttribute(k, v) { this.attrs[k] = v; }
  replaceChildren(...kids) { this.kids = kids; }
  addEventListener(t, f) { this.handlers[t] = f; }
  get text() { return this.kids.map((k) => (typeof k === "string" ? k : k.text)).join(""); }
  find(pred, out = []) { if (pred(this)) out.push(this); for (const k of this.kids) if (typeof k !== "string") k.find(pred, out); return out; }
}

const entry = (id, thread, sender, recipient, extra = {}) => ({ id, seq: 1, thread_id: thread, message_id: "m0001", direction: sender + "->" + recipient,
  sender_role: sender, recipient_role: recipient, kind: "decision", status: "read", created_at: "2026-10-09T10:00:00.000Z", in_reply_to: null,
  payload: { decision: "legitimate" }, ...extra });
const ENTRIES = [entry("bm00001", "t1", "correspondent", "boss"), entry("bm00002", "t2", "auditor", "boss"), entry("bm00003", "t1", "boss", "correspondent")];

/** Boot app.js in a stub browser; opts.hash seeds location, opts.bare removes location/history. */
async function boot({ hash = "", bare = false } = {}) {
  const els = new Map();
  const calls = [];
  const timers = [];
  const status = { content: { kind: "smoke", scenarios: 0, valid: true, policy_source: "x" }, network_guard: { blocked_attempts: 0 }, egress: { profile: "closed", transmitted: 0 }, messages: {}, queue_pending: 0 };
  const ctx = {
    console: { log() {} }, alert() {}, confirm: () => true, setInterval: (f) => timers.push(f),
    sessionStorage: { getItem: (k) => (k === "sbx_token" ? "sbx-secret-token" : null), setItem() {} },
    document: {
      getElementById: (id) => { if (!els.has(id)) els.set(id, new N("#" + id)); return els.get(id); },
      createElement: (t) => new N(t), createTextNode: (s) => String(s), querySelectorAll: () => [],
    },
    fetch: async (url) => {
      calls.push(url);
      const body = url.includes("/boss/mailbox") ? { entries: ENTRIES, last_seq: 3 } : url.includes("/boss/pending") ? { pending: [] }
        : url.includes("/messages") ? { messages: [] } : url.includes("/scenarios") ? { scenarios: [] }
        : status;
      return { ok: true, status: 200, json: async () => body };
    },
  };
  const replaced = [];
  if (!bare) {
    ctx.location = { hash, pathname: "/ui", search: "" };
    ctx.history = { replaceState: (_s, _t, url) => { replaced.push(url); ctx.location.hash = url.includes("#") ? url.slice(url.indexOf("#")) : ""; } };
  }
  const listeners = {};
  ctx.addEventListener = (t, f) => { listeners[t] = f; };
  ctx.window = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx);
  els.set("stagger", Object.assign(new N("input"), { value: "120" }));
  els.set("fl-corr", Object.assign(new N("input"), { checked: true }));
  for (const f of ["route.js", "mailbox.js", "app.js"]) vm.runInContext(read(f), ctx);
  const settle = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r)); };
  await settle();
  return { ctx, els, calls, replaced, timers, status, listeners, settle, get: (expr) => vm.runInContext(expr, ctx) };
}

test("canonical inbox link selects the tab, opens the dock and filters to the correspondent", async () => {
  const b = await boot({ hash: "#tab=messages&mailbox=open&role=correspondent" });
  assert.equal(b.get("state.tab"), "messages");
  assert.equal(b.get("mbx.open"), true);
  assert.ok(b.els.get("mbx-dock").classList.contains("open"));
  // The route is applied before the first load: the dock fetch and the tab fetch both happened.
  assert.ok(b.calls.some((u) => u.includes("/boss/mailbox")) && b.calls.some((u) => u.includes("/messages")));
  const body = b.els.get("mbx-body");
  assert.match(body.text, /filter: role=correspondent/);
  assert.equal(body.find((n) => n.className.includes("mbx-entry")).length, 2);
  assert.doesNotMatch(body.text, /auditor/);
});

test("thread filter narrows to one thread and the clear button restores the dock and the URL", async () => {
  const b = await boot({ hash: "#mailbox=open&role=correspondent&thread=t1" });
  const body = b.els.get("mbx-body");
  assert.match(body.text, /thread=t1/);
  assert.equal(body.find((n) => n.className.includes("mbx-entry")).length, 2);
  body.find((n) => n.tag === "button" && n.text === "clear filter")[0].handlers.click();
  await b.settle();
  assert.equal(b.get("mbx.role"), null);
  assert.equal(b.get("mbx.thread"), null);
  assert.equal(b.ctx.location.hash, "#tab=messages&mailbox=open");
  const after = b.els.get("mbx-body");
  assert.doesNotMatch(after.text, /filter:/);
  assert.equal(after.find((n) => n.className.includes("mbx-entry")).length, 3);
});

test("clicking a tab and toggling the dock rewrite the hash with replaceState", async () => {
  const b = await boot({ hash: "#tab=messages&mailbox=open&role=correspondent" });
  b.els.get("tabs").handlers.click({ target: { dataset: { tab: "outbox" } } });
  await b.settle();
  assert.equal(b.ctx.location.hash, "#tab=outbox&mailbox=open&role=correspondent");
  b.els.get("mbx-toggle").handlers.click();
  await b.settle();
  assert.equal(b.ctx.location.hash, "#tab=outbox&mailbox=closed&role=correspondent");
  assert.ok(b.replaced.length >= 2);
});

test("hashchange re-applies the route and an empty hash resets to defaults", async () => {
  const b = await boot({ hash: "#tab=messages&mailbox=open" });
  b.ctx.location.hash = "#tab=events&mailbox=closed";
  b.listeners.hashchange();
  await b.settle();
  assert.equal(b.get("state.tab"), "events");
  assert.equal(b.get("mbx.open"), false);
  assert.equal(b.els.get("mbx-dock").classList.contains("open"), false);
});

test("hostile hash boots into the defaults and the API token never reaches the URL", async () => {
  const b = await boot({ hash: "#tab=%00&mailbox=%E0%A4%A&role=admin&sbx_token=sbx-secret-token" });
  assert.equal(b.get("state.tab"), "messages");
  assert.equal(b.get("mbx.open"), false);
  b.els.get("tabs").handlers.click({ target: { dataset: { tab: "docs" } } });
  await b.settle();
  for (const u of [...b.replaced, b.ctx.location.hash]) assert.ok(!u.includes("secret") && !u.includes("token"));
});

test("boot works without location or history", async () => {
  const b = await boot({ bare: true });
  assert.equal(b.get("state.tab"), "messages");
  b.els.get("tabs").handlers.click({ target: { dataset: { tab: "boss" } } });
  await b.settle();
  assert.equal(b.get("state.tab"), "boss");
});

test("the poll re-renders the active tab when mail arrives from outside the page", async () => {
  const b = await boot({ hash: "#tab=messages&mailbox=open&role=correspondent" });
  const loads = () => b.calls.filter((u) => u.includes("/messages")).length;
  const tick = b.timers[b.timers.length - 1]; // app.js registers tick last
  await tick(); await b.settle();
  const before = loads();
  await tick(); await b.settle();
  assert.equal(loads(), before, "an unchanged status does not re-render");
  b.status.messages = { admitted: 1 };
  await tick(); await b.settle();
  assert.ok(loads() > before, "a changed message count re-renders without a reload or inject click");
});
