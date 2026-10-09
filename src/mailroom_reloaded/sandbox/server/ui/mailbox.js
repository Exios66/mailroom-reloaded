"use strict";
// Boss mailbox panel: renders the two-way Correspondent <-> Boss conversation, grouped by thread.
// Vanilla JS, no external fetches, text via textContent only. Read-only apart from the
// Release / Quarantine buttons, which call opts.onDecide.
(function (root) {
  /** Create an element, flatten child arrays, and insert primitive children as text. */
  function node(tag, cls, ...kids) {
    const n = root.document.createElement(tag);
    if (cls) n.className = cls;
    for (const k of kids.flat(Infinity)) {
      if (k == null || k === false) continue;
      n.append(typeof k === "object" ? k : root.document.createTextNode(String(k)));
    }
    return n;
  }
  /** Summarize known entry kinds, falling back to the first 160 characters of payload JSON. */
  function summarize(e) {
    const p = e.payload || {};
    if (e.kind === "hostile_forward") {
      return `hostile mail forwarded: ${(p.attack_classes || []).join(", ")}; ${(p.attachment_lanes || []).length} attachment(s) held; ${p.message ? p.message.subject : ""}`;
    }
    if (e.kind === "decision") return `decision: ${p.decision}${p.category ? " (" + p.category + ")" : ""}${p.reason ? " - " + p.reason : ""}`;
    if (e.kind === "draft_for_approval") return `draft ${p.outbox_id} (${p.intent}) to ${p.to}`;
    if (e.kind === "approval" || e.kind === "rejection") return `${e.kind} of ${p.outbox_id} by ${p.by}`;
    return JSON.stringify(p).slice(0, 160);
  }
  /** Count new entries and hostile forwards with pending message IDs, once per entry. */
  function unread(entries, pendingIds) {
    const pend = pendingIds || new Set();
    return entries.filter((e) => e.status === "new" || (e.kind === "hostile_forward" && pend.has(e.message_id))).length;
  }
  /**
   * Return a detached mailbox view grouped by thread in first-seen order.
   * opts.pending is a set of message IDs. Pending forwards offer buttons only
   * when opts.onDecide exists; clicks pass (messageId, decision, reason) to it.
   * opts.isDeciding reflects the shared request guard, including after a rerender.
   * Rendering does not fetch data or change entry status.
   */
  function renderMailbox(entries, opts) {
    const o = opts || {};
    const pend = o.pending || new Set();
    const wrap = node("div", "mbx");
    if (!entries.length) {
      wrap.append(node("p", "muted", "The Boss mailbox is empty."));
      return wrap;
    }
    const threads = new Map();
    for (const e of entries) {
      if (!threads.has(e.thread_id)) threads.set(e.thread_id, []);
      threads.get(e.thread_id).push(e);
    }
    for (const [tid, list] of threads) {
      const box = node("div", "mbx-thread", node("h4", "", "thread " + tid, " ", node("span", "muted", list[0].message_id)));
      for (const e of list) {
        const row = node("div", "mbx-entry " + (e.direction === "boss->correspondent" ? "to-corr" : "to-boss"),
          node("div", "", node("b", "", e.sender_role), " → ", node("b", "", e.recipient_role), " ",
            node("span", "chip", e.kind), " ", node("span", "chip " + (e.status === "new" ? "warn" : e.status === "acted" ? "ok" : ""), e.status), " ",
            node("span", "muted", e.id + " " + String(e.created_at).slice(11, 23) + (e.in_reply_to ? " re " + e.in_reply_to : ""))),
          node("div", "mbx-sum", summarize(e)));
        if (e.kind === "hostile_forward" && pend.has(e.message_id) && o.onDecide) {
          const reason = node("input", "");
          reason.setAttribute("placeholder", "reason (recorded)");
          const rel = node("button", "small primary", "Release (legitimate)");
          rel.addEventListener("click", () => o.onDecide(e.message_id, "legitimate", reason.value));
          const q = node("button", "small danger", "Quarantine");
          q.addEventListener("click", () => o.onDecide(e.message_id, "quarantine", reason.value));
          for (const button of [rel, q]) {
            button.setAttribute("data-decision-message", e.message_id);
            button.disabled = !!(o.isDeciding && o.isDeciding(e.message_id));
          }
          row.append(node("div", "", reason, " ", rel, " ", q));
        }
        box.append(row);
      }
      wrap.append(box);
    }
    return wrap;
  }
  root.sbxMailbox = { renderMailbox, unread, summarize };
})(typeof window !== "undefined" ? window : globalThis);
