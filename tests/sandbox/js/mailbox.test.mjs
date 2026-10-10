import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// Minimal stub DOM: enough for the panel's createElement/append/addEventListener.
class N {
  constructor(tag) { this.tag = tag; this.className = ""; this.kids = []; this.attrs = {}; this.handlers = {}; this.dataset = {}; this.nodeType = 1; this.disabled = false; }
  append(...k) { this.kids.push(...k); }
  setAttribute(k, v) {
    this.attrs[k] = v;
    if (k === "data-decision-message") this.dataset.decisionMessage = v;
    if (k === "disabled") this.disabled = true;
  }
  replaceChildren(...kids) { this.kids = kids; }
  addEventListener(t, f) { this.handlers[t] = f; }
  get text() { return this.kids.map((k) => (typeof k === "string" ? k : k.text)).join(""); }
  find(pred, out = []) { if (pred(this)) out.push(this); for (const k of this.kids) if (typeof k !== "string") k.find(pred, out); return out; }
}
const sandbox = { document: { createElement: (t) => new N(t), createTextNode: (s) => String(s) } };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(readFileSync(new URL("../../../src/mailroom_reloaded/sandbox/server/ui/mailbox.js", import.meta.url), "utf8"), sandbox);
const mbx = sandbox.sbxMailbox;

const fwd = { id: "bm00001", seq: 1, thread_id: "t1", message_id: "m0001", direction: "correspondent->boss", sender_role: "correspondent", recipient_role: "boss",
  kind: "hostile_forward", status: "read", created_at: "2026-10-09T10:00:00.000Z", in_reply_to: null,
  payload: { attack_classes: ["payment_fraud"], attachment_lanes: [{ name: "w.pdf" }], message: { subject: "Wire" } } };
const dec = { id: "bm00002", seq: 2, thread_id: "t1", message_id: "m0001", direction: "boss->correspondent", sender_role: "boss", recipient_role: "correspondent",
  kind: "decision", status: "acted", created_at: "2026-10-09T10:00:01.000Z", in_reply_to: "bm00001", payload: { decision: "quarantine", reason: "lookalike", category: "phishing" } };

test("renders both directions grouped by thread with role names", () => {
  const root = mbx.renderMailbox([fwd, dec], { pending: new Set() });
  const text = root.text;
  assert.match(text, /thread t1/);
  assert.match(text, /correspondent.*boss/);
  assert.match(text, /boss.*correspondent/);
  assert.match(text, /hostile mail forwarded: payment_fraud/);
  assert.match(text, /decision: quarantine \(phishing\) - lookalike/);
  assert.equal(root.find((n) => n.className.includes("to-boss")).length, 1);
  assert.equal(root.find((n) => n.className.includes("to-corr")).length, 1);
});

test("pending forward offers Release and Quarantine; decided one does not", () => {
  const calls = [];
  const onDecide = (...a) => calls.push(a);
  const live = mbx.renderMailbox([fwd], { pending: new Set(["m0001"]), onDecide });
  const buttons = live.find((n) => n.tag === "button");
  assert.deepEqual(buttons.map((b) => b.text), ["Release (legitimate)", "Quarantine"]);
  buttons[1].handlers.click();
  assert.deepEqual(calls[0].slice(0, 2), ["m0001", "quarantine"]);
  const done = mbx.renderMailbox([fwd, dec], { pending: new Set(), onDecide });
  assert.equal(done.find((n) => n.tag === "button").length, 0);
});

test("unread badge counts new entries and pending forwards; empty state", () => {
  assert.equal(mbx.unread([{ ...dec, status: "new" }, fwd], new Set()), 1);
  assert.equal(mbx.unread([fwd], new Set(["m0001"])), 1);
  assert.match(mbx.renderMailbox([], {}).text, /empty/);
});

test("content is inserted as text, never as markup", () => {
  const evil = { ...fwd, payload: { attack_classes: ["<img src=x onerror=alert(1)>"], attachment_lanes: [], message: { subject: "<b>x</b>" } } };
  const root = mbx.renderMailbox([evil], {});
  assert.ok(root.text.includes("<img src=x onerror=alert(1)>"));
  assert.equal(root.find((n) => n.tag === "img").length, 0);
});

for (const fails of [false, true]) {
  test(`shared decisions disable both surfaces, survive rerenders and settle (${fails ? "failure" : "success"})`, async () => {
    const roots = [];
    const ids = new Map();
    const doc = {
      createElement: (t) => new N(t), createTextNode: (s) => String(s),
      getElementById: (id) => {
        if (!ids.has(id)) ids.set(id, new N("div"));
        return ids.get(id);
      },
      querySelectorAll: () => [...roots, ...ids.values()].flatMap((r) => r.find((n) => n.attrs["data-decision-message"])),
    };
    const alerts = [];
    const ctx = { document: doc, sessionStorage: { getItem: () => "" },
      setInterval: () => 0, fetch: () => new Promise(() => {}), alert: (msg) => alerts.push(msg) };
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const file of ["mailbox.js", "app.js"]) {
      vm.runInContext(readFileSync(new URL(`../../../src/mailroom_reloaded/sandbox/server/ui/${file}`, import.meta.url), "utf8"), ctx);
    }
    let settle;
    const requests = [];
    ctx.api = async (path, opts) => {
      if (path === "/boss/decisions") {
        requests.push(JSON.parse(opts.body));
        return new Promise((resolve, reject) => { settle = fails ? () => reject(new Error("failed")) : resolve; });
      }
      return path === "/boss/pending" ? { pending: [{ message_id: "m0001" }] } : { entries: [fwd], last_seq: 1 };
    };
    ctx.refreshAll = async () => {};
    vm.runInContext("mbx.open = true", ctx);
    await ctx.pollMailbox();
    const card = { message_id: "m0001", state: "pending", attack_classes: [], attachments: [], category: "other" };
    roots.push(ctx.reviewCard(card, true), ctx.reviewCard({ ...card, message_id: "other" }, true));
    const buttons = () => doc.querySelectorAll().filter((b) => b.dataset.decisionMessage === "m0001");
    const first = buttons()[fails ? 2 : 0]; // exercise mailbox and review-card entry points
    const pending = first.handlers.click();
    assert.equal(requests.length, 1);
    assert.ok(buttons().every((b) => b.disabled));
    assert.ok(doc.querySelectorAll().filter((b) => b.dataset.decisionMessage === "other").every((b) => !b.disabled));
    for (const b of buttons()) await b.handlers.click();
    assert.equal(requests.length, 1);
    await ctx.pollMailbox();
    roots[0] = ctx.reviewCard(card, true);
    assert.ok(buttons().every((b) => b.disabled));
    for (const b of buttons()) await b.handlers.click();
    assert.equal(requests.length, 1);
    settle();
    await pending;
    assert.ok(buttons().every((b) => !b.disabled));
    assert.equal(alerts.length, fails ? 1 : 0);
    const retry = buttons()[0].handlers.click();
    assert.equal(requests.length, 2);
    settle();
    await retry;
  });
}

test("role and thread filters narrow the view and show a clearable filter line", () => {
  const other = { ...dec, id: "bm00003", thread_id: "t2", sender_role: "auditor", recipient_role: "boss" };
  assert.deepEqual(mbx.filterEntries([fwd, dec, other], { role: "correspondent" }).map((e) => e.id), ["bm00001", "bm00002"]);
  assert.deepEqual(mbx.filterEntries([fwd, dec, other], { role: "boss", thread: "t2" }).map((e) => e.id), ["bm00003"]);
  assert.equal(mbx.filterEntries([fwd, dec, other], {}).length, 3);
  let cleared = 0;
  const root = mbx.renderMailbox([fwd, dec, other], { role: "correspondent", onClearFilter: () => cleared++ });
  assert.match(root.text, /filter: role=correspondent/);
  assert.doesNotMatch(root.text, /thread t2/);
  root.find((n) => n.tag === "button" && n.text === "clear filter")[0].handlers.click();
  assert.equal(cleared, 1);
  assert.match(mbx.renderMailbox([fwd], { role: "boss", thread: "zzz" }).text, /No mailbox entries match the filter/);
  assert.equal(mbx.renderMailbox([fwd, dec], {}).find((n) => n.className.includes("mbx-filter")).length, 0);
});
