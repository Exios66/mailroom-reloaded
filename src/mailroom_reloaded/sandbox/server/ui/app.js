"use strict";
// Vanilla JS, no external fetches. All message content is rendered with textContent only.
const $ = (id) => document.getElementById(id);
const API = "/api/sandbox/v1";
const state = { tab: "messages", sel: null, scenario: null, status: null, timer: null };

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return n;
}
const chip = (text, kind) => el("span", { class: "chip " + (kind || "") }, text);
const stateKind = (s) => ({ processed: "ok", pipeline_done: "ok", captured: "ok", pass: "ok", admitted: "info", queued: "warn",
  shed: "bad", error: "bad", blocked: "bad", quarantined: "bad", fail: "bad", held: "warn", draft: "warn", pending_human: "warn", hostile: "bad", suspicious: "warn", verified: "ok" }[s] || "");
const kv = (rows) => el("div", { class: "kv" }, rows.map(([k, v]) => [el("div", {}, k), el("div", {}, v)]));

async function api(path, opts) {
  const headers = { "Content-Type": "application/json" };
  const tok = $("token").value || sessionStorage.getItem("sbx_token") || "";
  if (tok) headers.Authorization = "Bearer " + tok;
  const r = await fetch(API + path, Object.assign({ headers }, opts || {}));
  if (r.status === 401) throw new Error("401: enter the API token (top right)");
  if (!r.ok) {
    let d = r.statusText; try { d = (await r.json()).detail || d; } catch (e) { /* not json */ }
    throw new Error(r.status + ": " + d);
  }
  return r.json();
}
const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
function flash(msg) { $("banner").dataset.msg = msg; console.log(msg); }
async function guarded(fn) { try { await fn(); } catch (e) { alert(e.message); } }

function flows() {
  const f = [];
  if ($("fl-corr").checked) f.push("correspondent");
  if ($("fl-pipe").checked) f.push("pipeline");
  return f;
}

// ---------------------------------------------------------------- header + scenarios
async function refreshStatus() {
  const s = await api("/status");
  state.status = s;
  const c = $("chips"); c.replaceChildren();
  c.append(chip(`content ${s.content.kind} ${s.content.version || ""} (${s.content.scenarios})`, s.content.valid ? "ok" : "bad"),
    chip("policy: " + s.content.policy_source),
    chip("offline mock LLM"),
    chip("network guard: " + s.network_guard.blocked_attempts + " blocked", s.network_guard.blocked_attempts ? "bad" : "ok"),
    chip("transmitted mail: " + s.egress.transmitted, "ok"),
    chip("queue " + s.queue_pending, s.queue_pending ? "warn" : ""));
  const prof = el("select", { onchange: (e) => guarded(() => post("/config", { egress_profile: e.target.value })) },
    ["closed", "egress"].map((p) => el("option", { value: p, selected: p === s.egress.profile }, "recipient profile: " + p)));
  const aut = el("select", { onchange: (e) => guarded(() => post("/config", { autonomy: e.target.value })) },
    ["human", "sandbox"].map((p) => el("option", { value: p, selected: p === s.autonomy }, "autonomy: " + p)));
  c.append(prof, aut);
}
async function renderScenarios() {
  const d = await api("/scenarios");
  const box = $("scenarios"); box.replaceChildren();
  for (const s of d.scenarios) {
    box.append(el("div", { class: "scn" },
      el("div", { class: "name" }, s.name, " ", s.verdict ? chip(s.verdict, stateKind(s.verdict)) : null),
      el("div", { class: "title" }, s.title || ""),
      el("div", { class: "row" }, chip(`${s.emails} email`), s.document_feeds ? chip(`${s.document_feeds} doc feed`) : null,
        s.attachments ? chip(`${s.attachments} att`) : null, s.expected_intent ? chip("expect: " + s.expected_intent) : null),
      el("div", { class: "row" },
        el("button", { class: "small primary", onclick: () => guarded(() => inject([s.name])) }, "Inject"),
        el("button", { class: "small", onclick: () => { state.scenario = s.name; state.tab = "messages"; renderTab(); } }, "Messages"))));
  }
}
async function inject(ids) {
  const f = flows();
  if (!f.length) throw new Error("select at least one flow");
  const r = await post("/inject", { scenario_ids: ids, flows: f, stagger_seconds: parseInt($("stagger").value || "0", 10) });
  state.scenario = ids === "all" ? null : ids[0];
  state.tab = "messages"; await tick(); state.sel = r.message_ids[r.message_ids.length - 1];
  await renderTab(); await renderTrace();
}

// ---------------------------------------------------------------- tabs
/** Refresh the active tab and its content, displaying view-loading errors in the tab. */
async function renderTab() {
  writeRoute();
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
  const body = $("tab-body");
  try {
    if (state.tab === "messages") body.replaceChildren(await messagesView());
    else if (state.tab === "boss") body.replaceChildren(await bossView());
    else if (state.tab === "outbox") body.replaceChildren(await outboxView());
    else if (state.tab === "events") body.replaceChildren(await eventsView());
    else if (state.tab === "conformance") body.replaceChildren(await conformanceView());
    else if (state.tab === "docs") body.replaceChildren(await docsView());
    else if (state.tab === "policy") body.replaceChildren(await policyView());
  } catch (e) { body.replaceChildren(el("p", { class: "err" }, e.message)); }
}
async function messagesView() {
  const q = state.scenario ? "?scenario=" + encodeURIComponent(state.scenario) : "";
  const d = await api("/messages" + q);
  const head = el("div", { class: "toolbar" }, state.scenario ? [chip("scenario " + state.scenario), el("button", { class: "small", onclick: () => { state.scenario = null; renderTab(); } }, "show all")] : chip("all scenarios"));
  const t = el("table", {}, el("tr", {}, ["id", "scenario", "sim t+", "from", "subject / file", "state", "flows"].map((h) => el("th", {}, h))));
  for (const m of d.messages) {
    t.append(el("tr", { class: "click" + (m.id === state.sel ? " sel" : ""), onclick: () => { state.sel = m.id; renderTab(); renderTrace(); } },
      el("td", {}, m.id), el("td", {}, m.scenario), el("td", {}, m.sim_offset_s + "s"), el("td", {}, m.wire.from),
      el("td", {}, m.wire.subject), el("td", {}, chip(m.state, stateKind(m.state)), " ", m.kind === "document" ? chip("doc feed") : null),
      el("td", {}, m.flows_done.join(", "))));
  }
  return el("div", {}, head, d.messages.length ? t : el("p", { class: "muted" }, "No messages yet. Inject a scenario."));
}
// Shared across review cards and the mailbox, including views rebuilt by polling.
const decidingMessages = new Set();
/** Disable decision buttons across views while their message has a decision in flight. */
function syncDecisionButtons() {
  document.querySelectorAll("button[data-decision-message]").forEach((b) => {
    b.disabled = decidingMessages.has(b.dataset.decisionMessage);
  });
}
/**
 * Submit a Boss decision and refresh views, ignoring concurrent calls for the same message.
 * Disable that message's decision buttons until completion, including on failure.
 * Request and uncaught refresh errors reject the returned promise.
 */
async function decide(messageId, decision, reason) {
  if (decidingMessages.has(messageId)) return;
  decidingMessages.add(messageId);
  syncDecisionButtons();
  try {
    await post("/boss/decisions", { message_id: messageId, decision, reason });
    await refreshAll();
    await pollMailbox();
  } finally {
    decidingMessages.delete(messageId);
    syncDecisionButtons();
  }
}
/**
 * Build a review card; withButtons enables decisions using the entered reason.
 * Decision clicks post to the API and refresh views, alerting on errors.
 */
function reviewCard(c, withButtons) {
  const reason = el("input", { placeholder: "reason (recorded)", size: 30 });
  const onDecide = (decision) => guarded(() => decide(c.message_id, decision, reason.value));
  const decisionAttrs = { "data-decision-message": c.message_id, disabled: decidingMessages.has(c.message_id) };
  return el("div", { class: "flow e" },
    el("div", {}, chip(c.state, c.state === "pending" ? "warn" : c.state === "released" ? "ok" : "bad"), " ", c.message_id, " ",
      c.attack_classes.map((a) => chip(a + "/" + c.priority, "bad")), " ", chip(c.category)),
    el("div", { class: "muted" }, "held attachments: " + (c.attachments.join(", ") || "none") + " | signal channel: possible_attack"),
    c.state === "deciding" ? el("div", { class: "muted" }, `decision ${c.decision} was recorded but not finished: press the same button to resume`) : null,
    c.decision ? el("div", {}, `decision: ${c.decision} by ${c.decided_by}: ${c.reason || ""}`) : null,
    withButtons ? el("div", {}, reason, " ", el("button", { ...decisionAttrs, class: "small primary", onclick: () => onDecide("legitimate") }, "Release (legitimate)"), " ",
      el("button", { ...decisionAttrs, class: "small danger", onclick: () => onDecide("quarantine") }, "Quarantine")) : null);
}
// ---------------------------------------------------------------- boss mailbox dock (always live)
const mbx = { entries: [], last: 0, open: false, role: null, thread: null, timer: null };
/**
 * Refresh cached entries, the unread badge, and the open dock without marking entries read.
 * Fetch the newest 5,000 entries; suppress polling errors and retain existing content on fetch failure.
 */
async function pollMailbox() {
  try {
    const p = await api("/boss/pending");
    const upd = await api("/boss/mailbox?limit=5000&latest=true");  // statuses change, so re-read the newest page
    mbx.entries = upd.entries; mbx.last = upd.last_seq;
    const pending = new Set(p.pending.map((c) => c.message_id));
    $("mbx-badge").textContent = String(window.sbxMailbox.unread(mbx.entries, pending));
    $("mbx-badge").className = "chip " + (pending.size ? "bad" : "");
    if (mbx.open) $("mbx-body").replaceChildren(window.sbxMailbox.renderMailbox(mbx.entries, {
      pending, role: mbx.role, thread: mbx.thread, onClearFilter: clearMailboxFilter,
      isDeciding: (mid) => decidingMessages.has(mid),
      onDecide: (mid, decision, reason) => guarded(() => decide(mid, decision, reason)),
    }));
  } catch (e) { /* offline or no token yet: keep the last view */ }
}
$("mbx-toggle").addEventListener("click", () => { mbx.open = !mbx.open; $("mbx-dock").classList.toggle("open", mbx.open); writeRoute(); pollMailbox(); });
/** Drop the dock's role/thread filter, update the URL fragment and redraw. */
function clearMailboxFilter() { mbx.role = mbx.thread = null; writeRoute(); pollMailbox(); }

// ---------------------------------------------------------------- URL fragment (deep links)
// The fragment mirrors tab, scenario, selection and dock state so a reload or a copied URL reproduces
// the view. It never carries the API token. history/location are guarded for non-browser harnesses.
const hasLocation = () => typeof location !== "undefined" && location !== null;
const hasRoute = () => typeof window !== "undefined" && !!window.sbxRoute;
/** Apply a fragment to the view state and dock; absent keys reset to their defaults. Does not fetch. */
function applyRoute(hash) {
  if (!hasRoute()) return;
  const r = window.sbxRoute.parseRoute(hash);
  state.tab = r.tab || "messages"; state.scenario = r.scenario || null; state.sel = r.sel || null;
  mbx.open = r.mailbox === "open"; mbx.role = r.role || null; mbx.thread = r.thread || null;
  $("mbx-dock").classList.toggle("open", mbx.open);
}
/** Mirror the current view into the fragment with replaceState (no history entry, no hashchange). */
function writeRoute() {
  if (!hasRoute() || !hasLocation() || typeof history === "undefined" || !history || !history.replaceState) return;
  const h = window.sbxRoute.formatRoute({ tab: state.tab, scenario: state.scenario, sel: state.sel,
    mailbox: mbx.open ? "open" : "closed", role: mbx.role, thread: mbx.thread });
  if (h === (location.hash || "")) return;
  try { history.replaceState(null, "", (location.pathname || "") + (location.search || "") + h); } catch (e) { /* sandboxed frame */ }
}
applyRoute(hasLocation() ? location.hash : "");
if (typeof window.addEventListener === "function") {
  window.addEventListener("hashchange", () => { applyRoute(hasLocation() ? location.hash : ""); renderTab(); renderTrace(); pollMailbox(); });
}
mbx.timer = setInterval(pollMailbox, 2000);
pollMailbox();

/** Build pending and decided review cards; API failures reject the returned promise. */
async function bossView() {
  const p = await api("/boss/pending");
  const d = await api("/boss/decisions");
  const wrap = el("div", {}, el("p", { class: "muted" }, "Hostile mail arrives here from the Correspondent's possible_attack signals. The message and attachments are held and the sender gets no reply until you decide."));
  for (const c of p.pending) wrap.append(reviewCard(c, true));
  if (!p.pending.length) wrap.append(el("p", { class: "muted" }, "Nothing is waiting for the Boss."));
  if (d.decisions.length) wrap.append(el("h3", {}, "Decided"));
  for (const c of d.decisions) wrap.append(reviewCard(c, false));
  return wrap;
}
async function outboxView() {
  const d = await api("/outbox");
  const wrap = el("div", {}, el("p", {}, chip("profile " + d.summary.profile), " ", chip("transmitted " + d.summary.transmitted, "ok"), " Nothing here is ever sent; approving captures into the sink after the recipient/cap checks."));
  wrap.append(attemptForm());
  for (const i of d.outbox) wrap.append(outboxCard(i));
  if (!d.outbox.length) wrap.append(el("p", { class: "muted" }, "Outbox is empty."));
  return wrap;
}
function attemptForm() {
  const to = el("input", { placeholder: "recipient, e.g. someone@gmail.com", size: 34 });
  return el("div", { class: "toolbar" }, to, el("button", { class: "small", onclick: () => guarded(async () => {
    const r = await post("/egress/attempt", { to: to.value });
    alert(`${r.state}: ${r.block ? r.block.reason : "captured in sink"}`); renderTab();
  }) }, "Try a send"), el("span", { class: "muted" }, "tests the recipient policy"));
}
function outboxCard(i) {
  return el("div", { class: "flow e" },
    el("div", {}, chip(i.state, stateKind(i.state)), " ", i.id, " to ", el("b", {}, i.to), " ", chip(i.intent || "manual")),
    el("div", { class: "muted" }, i.subject), el("pre", {}, i.body),
    i.block ? el("div", { class: "err" }, "blocked by " + i.block.rule + ": " + i.block.reason) : null,
    probeRow(i.recipient_probe),
    i.state === "draft" ? el("div", {}, el("button", { class: "small primary", onclick: () => guarded(async () => { await post(`/outbox/${i.id}/approve`); await refreshAll(); }) }, "Approve into sink"), " ",
      el("button", { class: "small", onclick: () => guarded(async () => { await post(`/outbox/${i.id}/reject`); await refreshAll(); }) }, "Reject")) : null);
}
function probeRow(p) {
  if (!p) return null;
  return el("div", {}, "recipient policy: ", Object.entries(p).map(([k, v]) => [chip(`${k}: ${v.allowed ? "allow" : "block"}`, v.allowed ? "ok" : "bad"), " "]),
    el("span", { class: "muted" }, " " + Object.values(p).map((v) => v.reason).join(" | ")));
}
async function eventsView() {
  const d = await api("/events?limit=400");
  const t = el("table", {}, el("tr", {}, ["#", "kind", "ref", "payload"].map((h) => el("th", {}, h))));
  for (const e of d.events.slice(-200).reverse()) t.append(el("tr", {}, el("td", {}, e.seq), el("td", {}, chip(e.kind)), el("td", {}, e.ref_id), el("td", {}, JSON.stringify(e.payload).slice(0, 200))));
  return t;
}
async function conformanceView() {
  const d = await api("/conformance");
  const t = el("table", {}, el("tr", {}, ["scenario", "verdict", "pass", "fail", "unchecked", "failed checks"].map((h) => el("th", {}, h))));
  for (const r of d.results) t.append(el("tr", {}, el("td", {}, r.scenario), el("td", {}, chip(r.verdict, stateKind(r.verdict))), el("td", {}, r.passed), el("td", {}, r.failed), el("td", {}, r.unchecked), el("td", {}, r.failed_checks.join(", "))));
  return el("div", {}, el("p", {}, `${d.pass} pass, ${d.fail} fail (latest injected batch per scenario). The stand-in is tuned heuristics, so failures on a wider pack are expected and informative.`), t);
}
async function docsView() {
  const d = await api("/documents");
  const t = el("table", {}, el("tr", {}, ["doc_id", "file", "status", "from message"].map((h) => el("th", {}, h))));
  for (const x of d.documents) t.append(el("tr", {}, el("td", {}, x.doc_id), el("td", {}, x.filename), el("td", {}, chip(x.status, stateKind(x.status === "archived" ? "processed" : x.status))), el("td", {}, x.message_id)));
  return d.documents.length ? t : el("p", { class: "muted" }, "No documents have reached the pipeline yet.");
}
async function policyView() {
  const d = await api("/policy");
  return el("div", {}, el("p", {}, "Loaded from: ", chip(d.source), " ", Object.entries(d.files).map(([k, v]) => el("div", { class: "muted" }, k + " " + v.slice(0, 12)))),
    el("h3", {}, "Ingress policy"), el("pre", {}, JSON.stringify(d.ingress.sources, null, 1)),
    el("h3", {}, "Recipient policy"), el("pre", {}, JSON.stringify(d.recipient, null, 1)),
    el("h3", {}, "Send caps"), el("pre", {}, JSON.stringify(d.send_schedule.caps, null, 1)));
}

// ---------------------------------------------------------------- trace
async function renderTrace() {
  const box = $("trace");
  if (!state.sel) {
    $("trace-id").textContent = "";
    box.replaceChildren();
    return;
  }
  try {
    const t = await api("/messages/" + encodeURIComponent(state.sel) + "/trace");
    $("trace-id").textContent = t.message_id + " / " + t.ingress.scenario;
    box.replaceChildren(...traceSections(t));
  } catch (e) { box.replaceChildren(el("p", { class: "err" }, e.message)); }
}
/** Return detached trace elements with action handlers and any Boss review or expected outcomes. */
function traceSections(t) {
  const out = [];
  const w = t.ingress.wire, adm = t.ingress.admission;
  out.push(el("h3", {}, "1. Ingress"));
  out.push(el("div", { class: "flow" }, kv([
    ["state", chip(t.ingress.state, stateKind(t.ingress.state))], ["kind", t.ingress.kind], ["sim time", t.ingress.sim_offset_s + "s into batch " + t.ingress.batch_id],
    ["from", w.from], ["auth", Object.entries(w.auth || {}).map(([k, v]) => k + "=" + v).join(" ") || "-"], ["subject", w.subject],
    ["admission", adm ? [chip(adm.status, stateKind(adm.status)), " ", adm.edges.map((e) => `${e.edge}:${e.status}${e.wait_s ? " +" + e.wait_s + "s" : ""}`).join(", "), adm.reason ? " (" + adm.reason + ")" : ""] : "-"],
  ]), t.ingress.notes.length ? el("div", { class: "muted" }, "notes: " + t.ingress.notes.join("; ")) : null,
  w.body ? el("pre", {}, w.body) : null,
  el("div", {}, (w.attachments || []).map((a) => el("div", {}, "attachment ", el("b", {}, a.name), a.resolved ? ` ${a.size} B sha ${a.sha256.slice(0, 12)} doc_id ${a.doc_id}` : " (unresolved)")))));
  if (t.ingress.state === "shed") out.push(el("button", { class: "small primary", onclick: () => guarded(async () => { await post("/messages/" + t.message_id + "/release"); await refreshAll(); }) }, "Release from comms/pending"));
  const c = t.correspondent;
  out.push(el("h3", {}, "2. Flow A: Correspondent decision ", chip("STAND-IN " + t.stand_in.correspondent, "warn")));
  if (!c) out.push(el("p", { class: "muted" }, "Not run (flow A not selected or document feed)."));
  else {
    out.push(el("div", { class: "flow a" }, kv([
      ["trust", [chip(c.trust, stateKind(c.trust)), " ", c.trust_reasons.join("; ")]], ["intent", c.intent + " (" + c.issue_class + ")"],
      ["client", c.client ? c.client.display_name + " via " + c.client.matched_by : "no registry match"],
      ["signals", c.signals.map((s) => [chip(s.kind + "/" + s.priority + (s.attack_class ? "/" + s.attack_class : ""), s.priority === "critical" ? "bad" : ""), " "])],
      ["attachment lanes", c.attachment_lanes.length ? c.attachment_lanes.map((l) => el("div", {}, chip(l.lane, l.lane === "quarantine" ? "bad" : l.lane === "hold" ? "warn" : "ok"), " ", l.name, " ", el("span", { class: "muted" }, l.reason))) : "none"],
      ["relations", c.relations.length ? c.relations.map((r) => el("div", {}, `${r.a} ${r.kind} ${r.b} `, chip(String(r.confidence)), " ", el("span", { class: "muted" }, r.evidence.join("; ")))) : "none"],
      ["callback task", c.callback ? `${c.callback.contact || "?"} ${c.callback.phone || ""} (${c.callback.source || c.callback.reason})` : "none"],
      ["summary", c.summary], ["rule trace", el("ul", {}, c.reasons.map((r) => el("li", {}, r)))],
    ])));
    out.push(el("h3", {}, "Boss Desk actions ", chip("STAND-IN", "warn")));
    out.push(el("div", {}, t.bossdesk.map((a) => el("div", {}, chip(a.state, stateKind(a.state)), " ", a.action + (a.params ? "(" + a.params + ")" : ""), " ", el("span", { class: "muted" }, a.source + (a.why ? " - " + a.why : ""))))));
  }
  if (t.boss_review) { out.push(el("h3", {}, "Boss review")); out.push(reviewCard(t.boss_review, t.boss_review.state === "pending")); }
  out.push(el("h3", {}, "3. Flow B: real pipeline (isolated data dir, mock LLM)"));
  if (!t.pipeline.length) out.push(el("p", { class: "muted" }, "No attachment reached the pipeline."));
  for (const h of t.pipeline) {
    const p = h.pipeline;
    out.push(el("div", { class: "flow b" }, el("div", {}, chip(h.status, stateKind(h.status)), " ", el("b", {}, h.name), " lane ", chip(h.lane), " ", el("span", { class: "muted" }, h.reason)),
      p ? kv([["status", chip(p.status, stateKind(p.status === "archived" ? "processed" : p.status))], ["doc_id", p.doc_id], ["route", p.route_trail.join(" > ")],
        ["class", `${p.doc_type || "?"}/${p.doc_subclass || "?"} conf ${p.confidence ?? "?"}`], ["LLM calls", p.structured_llm_calls + (p.reused ? " (reused, already processed)" : "")],
        ["audit", [chip(p.audit_chain_ok ? "chain ok" : "chain BROKEN", p.audit_chain_ok ? "ok" : "bad"), ` ${p.audit_entries} entries `, el("button", { class: "small", onclick: () => guarded(() => showAudit(p.doc_id)) }, "view audit")]]]) : null,
      h.status === "held" ? el("button", { class: "small primary", onclick: () => guarded(async () => { await post(`/messages/${t.message_id}/attachments/${encodeURIComponent(h.name)}/release`); await refreshAll(); }) }, "Human release to pipeline") : null));
  }
  out.push(el("h3", {}, "4. Egress capture (never transmitted)"));
  if (t.egress.sender_recipient_probe) out.push(el("div", {}, "if anyone replied to the sender: ", probeRow(t.egress.sender_recipient_probe)));
  if (!t.egress.outbox.length) out.push(el("p", { class: "muted" }, "No outbound draft for this message."));
  for (const i of t.egress.outbox) out.push(outboxCard(i));
  if (t.expected) {
    out.push(el("h3", {}, "5. Expected (scenario yaml) vs actual ", chip(t.expected.verdict, stateKind(t.expected.verdict))));
    const tb = el("table", {}, el("tr", {}, ["check", "expected", "actual", ""].map((h) => el("th", {}, h))));
    for (const k of t.expected.checks) tb.append(el("tr", {}, el("td", {}, k.key), el("td", {}, JSON.stringify(k.expected)), el("td", {}, JSON.stringify(k.actual).slice(0, 200)), el("td", {}, chip(k.ok === true ? "ok" : k.ok === false ? "MISMATCH" : "n/a", k.ok === true ? "ok" : k.ok === false ? "bad" : ""), k.note ? " " + k.note : "")));
    out.push(tb);
  }
  out.push(el("h3", {}, "Timeline"));
  const tl = el("table", {});
  for (const e of t.events) tl.append(el("tr", {}, el("td", {}, e.seq), el("td", {}, e.ts.slice(11, 23)), el("td", {}, chip(e.kind)), el("td", {}, JSON.stringify(e.payload).slice(0, 160))));
  out.push(tl);
  if (t.error) out.push(el("p", { class: "err" }, t.error));
  out.push(el("div", { class: "toolbar" }, ["correspondent", "pipeline"].map((f) => el("button", { class: "small", onclick: () => guarded(async () => { await post("/messages/" + t.message_id + "/run", { flows: [f] }); await refreshAll(); }) }, "re-run " + f))));
  return out;
}
async function showAudit(docId) {
  const a = await api("/documents/" + docId + "/audit");
  alert(`audit chain ok=${a.chain.ok}\n` + a.entries.map((e) => `${e.seq} ${e.node}.${e.event}`).join("\n"));
}

// ---------------------------------------------------------------- wiring
async function refreshAll() { await refreshStatus(); await renderScenarios(); await renderTab(); await renderTrace(); }
async function tick() {
  try { await refreshStatus(); if (state.status.queue_pending || state.dirty) { await renderTab(); await renderTrace(); await renderScenarios(); state.dirty = !!state.status.queue_pending; } } catch (e) { /* offline blip */ }
}
$("tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) { state.tab = e.target.dataset.tab; renderTab(); } });
$("inject-all").addEventListener("click", () => guarded(() => { state.dirty = true; return inject("all"); }));
$("reset").addEventListener("click", () => guarded(async () => { if (confirm("Delete all sandbox messages, outbox and pipeline state?")) { await post("/reset"); state.sel = null; $("trace").replaceChildren(); await refreshAll(); } }));
$("token").value = sessionStorage.getItem("sbx_token") || "";
$("token").addEventListener("change", () => { sessionStorage.setItem("sbx_token", $("token").value); refreshAll(); });
refreshAll().catch((e) => { $("tab-body").replaceChildren(el("p", { class: "err" }, e.message)); });
setInterval(tick, 2000);
