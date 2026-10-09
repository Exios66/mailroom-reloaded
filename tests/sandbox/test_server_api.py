"""Sandbox server API smoke, A1/E1 end-to-end traces and the expected-vs-actual view."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sbx_util import API, messages_of

from mailroom_reloaded.sandbox.server.app import create_sandbox_app
from mailroom_reloaded.sandbox.server.content import load_sandbox_content
from mailroom_reloaded.sandbox.server.service import SandboxService


def test_health_ui_and_static_are_offline(live):
    """Verify public health and UI assets load with a local-only content policy."""
    client, _svc, _ = live
    assert client.get("/health").json()["service"] == "mailroom-sandbox"
    ui = client.get("/ui")
    assert ui.status_code == 200 and "text/html" in ui.headers["content-type"]
    assert "default-src 'self'" in ui.headers["content-security-policy"]
    for asset in ("/ui/app.js", "/ui/app.css"):
        assert client.get(asset).status_code == 200
    page = ui.text + client.get("/ui/app.js").text + client.get("/ui/app.css").text
    assert (
        "http://" not in page and "https://" not in page
    )  # no external fetches at all


def test_status_scenarios_policy(live):
    """Verify status, scenario metadata, and vendored policies are exposed correctly."""
    client, _svc, _ = live
    st = client.get(f"{API}/status").json()
    assert st["stand_ins"]["correspondent"]["stand_in"] is True
    assert st["stand_ins"]["pipeline"]["real"] is True
    assert st["content"]["kind"] == "smoke" and st["content"]["valid"] is True
    sc = client.get(f"{API}/scenarios").json()
    assert sc["count"] == 6
    assert {s["name"] for s in sc["scenarios"]} >= {
        "A1_status_inquiry",
        "E1_lookalike_wire_change",
    }
    assert (
        client.get(f"{API}/scenarios/A1_status_inquiry").json()["scenario"]["expect"][
            "intent"
        ]
        == "status_request"
    )
    assert client.get(f"{API}/scenarios/nope").status_code == 404
    pol = client.get(f"{API}/policy").json()
    assert pol["source"] == "vendored" and "status_request" in {
        r["issue_class"] for r in pol["delegation_matrix"]
    }
    assert pol["ingress"]["sources"]["emails"]["rate"]["burst"] == 20


def _trace(client, scenario: str, index: int = 0) -> dict:
    """Fetch the trace for an indexed email in a scenario."""
    mid = [m for m in messages_of(client, scenario) if m["kind"] == "email"][index][
        "id"
    ]
    return client.get(f"{API}/messages/{mid}/trace").json()


def test_a1_trace_status_request_draft_waits_for_approval(live):
    """Verify A1 produces a catalog-backed draft that awaits human approval."""
    client, _svc, _ = live
    t = _trace(client, "A1_status_inquiry")
    c = t["correspondent"]
    assert c["agent"]["stand_in"] is True and c["llm_calls"] == 0
    assert (c["trust"], c["intent"]) == ("verified", "status_request")
    assert c["signals"][0]["kind"] == "status_request"
    assert t["pipeline"] == []  # no attachments
    out = t["egress"]["outbox"]
    assert len(out) == 1 and out[0]["state"] == "draft" and out[0]["captured"] is False
    # catalog-backed: the A3/B1 document feeds (Flow B) reached the catalog first, so the
    # draft cites that real record and its pipeline status instead of inventing one
    assert "schedule_c_v2.pdf: archived (doc_id 13163fac3b12782f)" in out[0]["body"]
    assert out[0]["evidence"] == ["13163fac3b12782f"]
    assert any(
        a["action"] == "approve_outbound" and a["state"] == "pending_human"
        for a in t["bossdesk"]
    )
    assert t["expected"]["verdict"] == "pass"
    kinds = [e["kind"] for e in t["events"]]
    assert (
        kinds[:3] == ["ingress.sent", "ingress.admitted", "message.received"]
        and "draft.created" in kinds
    )


def test_e1_trace_quarantine_no_reply_registry_callback_and_benign_companion(live):
    """Verify E1 quarantines the attack and processes its benign companion safely."""
    client, _svc, _ = live
    attack = _trace(client, "E1_lookalike_wire_change", 0)
    c = attack["correspondent"]
    assert c["trust"] == "hostile"
    assert (
        c["signals"][0]["attack_class"] == "payment_fraud"
        and c["signals"][0]["priority"] == "critical"
    )
    assert [(ln["name"], ln["lane"]) for ln in c["attachment_lanes"]] == [
        ("wire_instructions_updated_inert.pdf", "quarantine")
    ]
    assert (
        c["drafts"] == [] and attack["egress"]["outbox"] == []
    )  # no reply to the sender
    assert c["callback"]["phone"] == "+1-555-0142" and c["callback"][
        "source"
    ].startswith("registry")
    assert (
        "0142" not in attack["ingress"]["wire"]["body"]
    )  # the number never came from the message
    h = attack["pipeline"][0]
    assert (
        h["status"] == "quarantined" and h["pipeline"] is None
    )  # never reached the pipeline
    probe = attack["egress"]["sender_recipient_probe"]
    assert (
        probe["closed"]["allowed"] is True
    )  # closed profile alone cannot tell a lookalike apart
    assert (
        probe["egress"]["allowed"] is False
        and "no overlay route" in probe["egress"]["reason"]
    )
    assert {a["action"] for a in attack["bossdesk"]} >= {
        "quarantine_attachments",
        "recommend_callback",
        "request_human_review",
    }

    benign = _trace(client, "E1_lookalike_wire_change", 1)
    assert benign["correspondent"]["trust"] == "verified"
    assert not any(a["action"] == "quarantine_attachments" for a in benign["bossdesk"])
    p = benign["pipeline"][0]["pipeline"]
    assert p["status"] == "archived" and p["audit_chain_ok"] is True
    assert p["structured_llm_calls"] == 2 and p["route_trail"][0] == "ingest"
    assert benign["expected"]["verdict"] == "pass"
    assert attack["expected"]["verdict"] == "pass"


def test_flows_interoperate_on_same_messages(live):
    """A3's first doc enters via the document feed (Flow B); the email's relation cites it (Flow A)."""
    client, svc, _ = live
    t = _trace(client, "A3_supersession")
    rel = t["correspondent"]["relations"]
    assert rel and rel[0]["kind"] == "supersedes" and rel[0]["b"] == "schedule_c_v2.pdf"
    assert rel[0]["b_doc_id"] == "13163fac3b12782f"
    assert (
        svc.pipeline.document("13163fac3b12782f")["status"] == "archived"
    )  # real pipeline result
    assert (
        client.get(f"{API}/documents/13163fac3b12782f/audit").json()["chain"]["ok"]
        is True
    )
    assert t["expected"]["verdict"] == "pass"


def test_conformance_all_smoke_scenarios_pass(live):
    """Verify all six smoke scenarios match their expected outcomes."""
    client, _svc, _ = live
    d = client.get(f"{API}/conformance").json()
    assert d["fail"] == 0 and d["pass"] == 6, d


def test_egress_sink_blocks_by_profile_and_never_transmits(live):
    """Verify egress approval enforces recipient routes and only captures mail."""
    client, _svc, _ = live
    items = {i["to"]: i for i in client.get(f"{API}/outbox").json()["outbox"]}
    f1 = items["tomas.reyes.personal@mailbox.sandbox.invalid"]
    assert (
        client.post(f"{API}/config", json={"egress_profile": "egress"}).json()[
            "egress_profile"
        ]
        == "egress"
    )
    blocked = client.post(f"{API}/outbox/{f1['id']}/approve", json={}).json()
    assert (
        blocked["state"] == "blocked"
        and "no overlay route" in blocked["block"]["reason"]
    )
    a1 = items["dwhitcomb@harlowpryce.sandbox.invalid"]
    ok = client.post(f"{API}/outbox/{a1['id']}/approve", json={}).json()
    assert ok["state"] == "captured" and ok["dry_run"] is True
    out = client.post(f"{API}/egress/attempt", json={"to": "someone@gmail.com"}).json()
    assert out["state"] == "blocked" and "outside" in out["block"]["reason"]
    assert client.get(f"{API}/outbox").json()["summary"]["transmitted"] == 0
    client.post(f"{API}/config", json={"egress_profile": "closed"})


def test_held_attachment_needs_human_release_and_quarantine_never_opens(live):
    """Verify held attachments can be released while quarantine rejects release."""
    client, _svc, _ = live
    f1 = messages_of(client, "F1_personal_address_lockout")[0]
    t = client.get(f"{API}/messages/{f1['id']}/trace").json()
    assert t["pipeline"][0]["status"] == "held" and t["pipeline"][0]["pipeline"] is None
    e1 = messages_of(client, "E1_lookalike_wire_change")[0]
    r = client.post(
        f"{API}/messages/{e1['id']}/attachments/wire_instructions_updated_inert.pdf/release",
        json={},
    )
    assert r.status_code == 403
    r = client.post(
        f"{API}/messages/{f1['id']}/attachments/coi_unit4c.pdf/release",
        json={"by": "tester"},
    )
    assert r.status_code == 200
    assert r.json()["handoffs"][0]["pipeline"]["status"] == "archived"


def test_token_required_when_configured(monkeypatch, tmp_path):
    """Verify configured tokens protect JSON routes while health and UI stay public."""
    monkeypatch.setenv("MAILROOM_API_TOKEN", "s3cret")
    svc = SandboxService(load_sandbox_content(), tmp_path / "s")
    client = TestClient(create_sandbox_app(svc))  # no lifespan: nothing starts
    assert client.get(f"{API}/scenarios").status_code == 401
    ok = client.get(f"{API}/scenarios", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert (
        client.get("/health").status_code == 200
        and client.get("/ui").status_code == 200
    )
