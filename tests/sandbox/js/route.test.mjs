import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const sandbox = {};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(readFileSync(new URL("../../../src/mailroom_reloaded/sandbox/server/ui/route.js", import.meta.url), "utf8"), sandbox);
const { parseRoute, formatRoute, TABS } = sandbox.sbxRoute;
// Objects built inside the vm context have another Object.prototype; compare through JSON.
const plain = (o) => JSON.parse(JSON.stringify(o));

const INBOX = "#tab=messages&mailbox=open&role=correspondent";

test("canonical inbox link parses and formats back unchanged", () => {
  assert.deepEqual(plain(parseRoute(INBOX)), { tab: "messages", mailbox: "open", role: "correspondent" });
  assert.equal(formatRoute(parseRoute(INBOX)), INBOX);
});

test("leading hash is optional and every allow-listed key is accepted", () => {
  const h = "tab=policy&scenario=A1_status.inquiry-2&sel=m0007&mailbox=closed&role=boss&thread=thr_0123456789";
  const r = plain(parseRoute(h));
  assert.deepEqual(r, { tab: "policy", scenario: "A1_status.inquiry-2", sel: "m0007", mailbox: "closed", role: "boss", thread: "thr_0123456789" });
  assert.deepEqual(plain(parseRoute("#" + h)), r);
  for (const t of ["messages", "boss", "outbox", "events", "conformance", "docs", "policy"]) assert.ok(TABS.includes(t));
  assert.equal(TABS.length, 7);
});

test("malformed values and unknown keys are dropped without throwing", () => {
  assert.deepEqual(plain(parseRoute("#tab=nope&mailbox=maybe&role=admin&sel=a b&scenario=&thread=%00")), {});
  assert.deepEqual(plain(parseRoute("#foo=bar&token=abc&sbx_token=abc&tab=docs")), { tab: "docs" });
  assert.deepEqual(plain(parseRoute("#&&=&tab&=x&role=boss&")), { role: "boss" });
  for (const bad of [undefined, null, 5, {}, [], "", "#"]) assert.deepEqual(plain(parseRoute(bad)), {});
});

test("hostile fragments: huge input, bad escapes, prototype keys, encoded separators", () => {
  assert.deepEqual(plain(parseRoute("#tab=boss" + "&x=y".repeat(5000))), {});
  assert.deepEqual(plain(parseRoute("#sel=" + "a".repeat(65))), {});
  assert.equal(parseRoute("#sel=" + "a".repeat(64)).sel, "a".repeat(64));
  assert.deepEqual(plain(parseRoute("#tab=%E0%A4%A&role=%zz")), {});
  assert.deepEqual(plain(parseRoute("#__proto__=x&constructor=y&hasOwnProperty=z&toString=1")), {});
  assert.equal(Object.keys(parseRoute("#__proto__[tab]=boss")).length, 0);
  assert.equal(({}).tab, undefined);
  assert.deepEqual(plain(parseRoute("#sel=m1%26tab%3Dboss")), {});
  assert.deepEqual(plain(parseRoute("#sel=m1\n")), {});
  assert.deepEqual(plain(parseRoute("#sel=<script>alert(1)</script>")), {});
  assert.equal(parseRoute("#tab=%62oss").tab, "boss");
  assert.deepEqual(plain(parseRoute("#%74ab=docs")), { tab: "docs" });
});

test("duplicate keys: the first occurrence wins even when it is invalid", () => {
  assert.equal(parseRoute("#tab=docs&tab=boss").tab, "docs");
  assert.equal("tab" in parseRoute("#tab=bogus&tab=boss"), false);
});

test("formatRoute emits only valid allow-listed keys in canonical order", () => {
  assert.equal(formatRoute({ role: "boss", tab: "events", token: "x", sel: "m1", mailbox: "open" }), "#tab=events&sel=m1&mailbox=open&role=boss");
  assert.equal(formatRoute({ tab: "bad", sel: "has space", role: null, thread: 5 }), "");
  assert.equal(formatRoute(null), "");
  assert.equal(formatRoute({}), "");
  assert.ok(!formatRoute({ tab: "boss", sbx_token: "sbx-secret" }).includes("secret"));
});

test("round trip: parse(format(x)) keeps x, format(parse(h)) is canonical", () => {
  const s = { tab: "outbox", scenario: "E1_lookalike", sel: "m0012", mailbox: "closed", role: "correspondent", thread: "thr_abcdef0123" };
  assert.deepEqual(plain(parseRoute(formatRoute(s))), s);
  assert.equal(formatRoute(parseRoute("#role=boss&tab=docs&mailbox=open")), "#tab=docs&mailbox=open&role=boss");
});

test("dot-only ids are dropped so a selection cannot walk the API path", () => {
  assert.deepEqual(plain(parseRoute("#sel=..&thread=.&scenario=.hidden")), {});
});
