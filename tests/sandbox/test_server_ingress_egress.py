"""Metering (ingress_policy), shed/release, rendering, the egress sink and cap guards."""

from __future__ import annotations

import copy

import pytest

from mailroom_reloaded.sandbox.server.content import load_sandbox_content
from mailroom_reloaded.sandbox.server.egress import VirtualOutbox
from mailroom_reloaded.sandbox.server.ingress import IngressMeter, plan_scenario
from mailroom_reloaded.sandbox.server.service import SandboxService


def test_render_a1_and_e1_from_templates_with_attachments():
    c = load_sandbox_content()
    (a1,) = plan_scenario(c, "A1_status_inquiry")
    assert a1.wire["subject"] == "Status check on HP-2026-0417"
    assert "Schedule C indemnification review" in a1.wire["body"]
    e1 = plan_scenario(c, "E1_lookalike_wire_change")
    assert [i.offset_s for i in e1] == [2, 9]  # mm:ss offsets
    attack, _benign = e1
    att = attack.wire["attachments"][0]
    assert (
        att["name"] == "wire_instructions_updated_inert.pdf"
        and att["resolved"]
        and len(att["doc_id"]) == 16
    )
    assert (
        "wire_instructions_updated_inert.pdf" in attack.wire["body"]
    )  # {{ attachment_name }}
    assert (
        attack.truth["role"] == "impostor" and "truth" not in attack.wire
    )  # ground truth kept apart
    a3 = plan_scenario(c, "A3_supersession")
    assert [i.kind for i in a3] == ["document", "email"]


def test_token_bucket_burst_then_queue_then_shed():
    policy = copy.deepcopy(load_sandbox_content().policy.ingress)
    policy["sources"]["emails"]["rate"] = {"max_items_per_minute": 60, "burst": 2}
    policy["queues"]["pipeline_ingress"]["depth_max"] = 3
    m = IngressMeter(policy)
    got = [
        m.admit_email(0.0, f"s{i}@x.sandbox.invalid", f"t{i}")["status"]
        for i in range(8)
    ]
    assert got[:2] == ["admitted", "admitted"]
    assert got[2:5] == ["queued"] * 3  # waits for token refill, bounded queue
    assert set(got[5:]) == {"shed"}  # queue full: shed, never dropped silently


def test_per_sender_hourly_cap_sheds():
    m = IngressMeter(load_sandbox_content().policy.ingress)
    res = [
        m.admit_email(i * 30.0, "same@x.sandbox.invalid", "t")["status"]
        for i in range(14)
    ]
    assert res[:12].count("shed") == 0 and res[12:] == ["shed", "shed"]


def test_shed_goes_to_pending_and_human_release(idle_service: SandboxService):
    svc = idle_service
    svc.content.policy.ingress["correspondent_inbox"]["max_admissions_per_hour"] = 2
    svc.meter.reset()
    svc.meter = IngressMeter(svc.content.policy.ingress)
    r = svc.inject("all", process=False)
    shed = [m for m, s in r["states"].items() if s == "shed"]
    assert shed, r
    one = svc.message(shed[0])
    assert one["admission"]["reason"].startswith("max_admissions_per_hour")
    assert (svc.data_dir / "comms" / "pending" / shed[0]).is_dir()
    kinds = [e["kind"] for e in svc.events_since(0, ref_id=shed[0])]
    assert "ingress.shed" in kinds
    assert svc.release_message(shed[0])["state"] == "admitted"
    with pytest.raises(ValueError):
        svc.release_message(shed[0])


def _outbox(tmp_path, profile="closed"):
    c = load_sandbox_content()
    events = []
    routes = {
        a: "pool"
        for cl in c.registry_clients.values()
        for a in cl.get("verified_addresses", [])
    }
    ob = VirtualOutbox(
        c.policy,
        routes,
        tmp_path,
        lambda k, r, p: events.append(k),
        lambda: 1000.0,
        profile,
    )
    return ob, events


def test_recipient_policy_profiles(tmp_path):
    ob, _ = _outbox(tmp_path)
    assert ob.check_recipient("a@b.sandbox.invalid")["allowed"]
    assert not ob.check_recipient("a@gmail.com")["allowed"]
    assert not ob.check_recipient("a@sandbox.invalid")[
        "allowed"
    ]  # policy is *.sandbox.invalid
    assert ob.check_recipient("treyes@brightwaterpg.sandbox.invalid", "egress")[
        "allowed"
    ]
    assert not ob.check_recipient(
        "kalvarado@tricounty-title.sandbox.invalid", "egress"
    )["allowed"]


def test_send_guards_kill_switch_idempotency_and_thread_cap(tmp_path, monkeypatch):
    ob, events = _outbox(tmp_path)
    d = {
        "to": "x@brightwaterpg.sandbox.invalid",
        "subject": "s",
        "body": "b",
        "intent": "i",
    }
    i1 = ob.add_draft(message_id="m", thread_id="t", draft=d)
    assert ob.approve(i1["id"])["state"] == "captured" and i1["dry_run"] is True
    # same content, new outbound id: distinct send (not conflated), counts toward the thread cap
    i2 = ob.add_draft(message_id="m", thread_id="t", draft=d)
    assert ob.approve(i2["id"])["state"] == "captured"
    i3 = ob.add_draft(message_id="m", thread_id="t", draft=d)
    out = ob.approve(i3["id"])
    assert (
        out["state"] == "blocked"
        and "max_sends_per_thread_per_day" in out["block"]["reason"]
    )
    # replay of the very same outbound id/content is a duplicate and dropped
    assert (
        ob.approve(i1["id"])["state"] == "captured"
        and "send.duplicate_dropped" in events
    )
    assert (
        sum(1 for i in ob.items.values() if i["state"] == "captured") == 2
    )  # not captured twice
    # kill switch freezes the outbox
    (tmp_path / "comms").mkdir()
    (tmp_path / "comms" / "KILL_SWITCH").write_text("1")
    i4 = ob.add_draft(message_id="m", thread_id="other", draft=d)
    assert "kill switch" in ob.approve(i4["id"])["block"]["reason"]
    monkeypatch.setenv("MAILROOM_SEND_KILL_SWITCH", "1")
    assert ob._kill_switch()
