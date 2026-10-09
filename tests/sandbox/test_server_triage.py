"""Scored triage, synthetic attachments, LOFO report, evaluator ref resolution."""

from __future__ import annotations

from mailroom_reloaded.sandbox.server.conformance import family_of, lofo
from mailroom_reloaded.sandbox.server.correspondent import (
    AttachmentView,
    StandInCorrespondent,
    WireMessage,
)
from mailroom_reloaded.sandbox.server.evaluate import compare_scenario
from mailroom_reloaded.sandbox.server.synthetic import materialise_draw, synthetic_pdf
from mailroom_reloaded.sandbox.server.triage import (
    ABSTAIN_BELOW,
    extract_features,
    score_intents,
)

PASS = {"spf": "pass", "dkim": "pass", "dmarc": "pass"}


class _Tools:
    def registry(self):
        return {
            "acme": {
                "verified_addresses": ["pat@acme.example"],
                "verified_domains": ["acme.example"],
                "callback": {"contact": "Pat", "phone": "+1-555-0100"},
            }
        }

    def lookup_catalog(self):
        return []

    def read_attachment_text(self, att):
        return ""


def _m(body, subject="hello", atts=(), frm="pat@acme.example", auth=PASS):
    return WireMessage(
        "m",
        "t",
        frm,
        subject,
        body,
        auth,
        [AttachmentView(n, resolved=True) for n in atts],
    )


def _tri(body, subject="hello", atts=()):
    msg = _m(body, subject, atts)
    f = extract_features(msg, "verified", {"how": "address"}, set())
    return score_intents(f"{subject}\n{body}", f, subject)


def test_abstains_to_general_question_with_review_when_nothing_scores():
    t = _tri("Thanks.")
    assert t.abstained and t.intent == "general_question" and t.needs_review
    assert max(t.scores.values()) < ABSTAIN_BELOW


def test_lexicon_and_structure_pick_intents():
    assert _tri("Where is matter HP-1?").intent == "status_request"
    assert (
        _tri("Please find attached the signed copy.", atts=["a.pdf"]).intent
        == "document_submission"
    )
    assert (
        _tri("This is unacceptable, my third email, nobody answers.").intent
        == "complaint"
    )
    assert _tri("Closing is today, please expedite.").intent == "urgent_deadline"
    # a subject hit counts more than a body hit
    assert (
        _tri("see attached", subject="Amended Schedule C", atts=["a.pdf"]).intent
        == "correction_or_amendment"
    )
    # negated urgency is a mention, not a claim
    assert (
        _tri("Nothing urgent, just a routine item at your convenience.").intent
        != "urgent_deadline"
    )


def test_attack_intents_still_come_from_the_safety_screen_not_the_lexicon():
    res = StandInCorrespondent().handle(
        _m(
            "New wire instructions: do not call, email only.",
            frm="pat@acrne.example",
            auth={"spf": "fail", "dkim": "none", "dmarc": "fail"},
        ),
        _Tools(),
    )
    assert res.intent == "payment_or_identity_change" and res.drafts == []
    assert res.agent["name"] == "rule-based-standin/v2"


def test_matrix_drives_whether_a_reply_is_drafted():
    class Tools(_Tools):
        def __init__(self, rows):
            self.rows = rows

        def delegation(self):
            return self.rows

    ask = _m("Where is matter HP-1?")
    on = {
        "status_request": {
            "boss_action": "task_correspondent(status_update)",
            "notes": "",
        }
    }
    off = {"status_request": {"boss_action": "request_human_review", "notes": ""}}
    assert StandInCorrespondent().handle(ask, Tools(on)).drafts
    assert StandInCorrespondent().handle(ask, Tools(off)).drafts == []


def test_synthetic_placeholders_are_deterministic_and_marked(tmp_path):
    spec = {
        "class": "insurance_claim",
        "stratum": "property",
        "group": "cr_0577",
        "ref": "part_1",
    }
    p1, n1 = materialise_draw(spec, tmp_path / "a")
    p2, n2 = materialise_draw(spec, tmp_path / "b")
    assert p1.read_bytes() == p2.read_bytes() and n1 == n2 == "part_1.pdf"
    other, _ = materialise_draw(dict(spec, ref="part_2"), tmp_path / "a")
    assert other.read_bytes() != p1.read_bytes()
    data = p1.read_bytes()
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    assert b"CR-2026-0577" in data and b"SYNTHETIC PLACEHOLDER" in data
    assert synthetic_pdf(["a (b)"]) == synthetic_pdf(["a (b)"])


def test_lofo_report_arithmetic():
    rows = [
        {"scenario": "A1_x", "verdict": "pass", "failed_checks": []},
        {"scenario": "A2_x", "verdict": "fail", "failed_checks": ["intent"]},
        {"scenario": "B1_x", "verdict": "pass", "failed_checks": []},
    ]
    rep = lofo(rows)
    assert [f["held_out_family"] for f in rep["folds"]] == ["A", "B"]
    assert rep["folds"][0]["held_out_rate"] == 0.5 and rep["folds"][0][
        "held_out_failed_checks"
    ] == {"intent": 1}
    assert rep["macro_mean_held_out_rate"] == 0.75 and family_of("g3_x") == "G"


def test_evaluator_resolves_attachment_refs_and_picks_the_expected_email():
    scenario = {
        "name": "X",
        "timeline": [],
        "expect": {
            "intent": "document_submission",
            "relations": [
                {"a": "late", "b": "set", "kind": "completes", "min_conf": 0.8}
            ],
        },
    }

    def email(mid, intent, atts, rels):
        return {
            "id": mid,
            "kind": "email",
            "truth": {},
            "wire": {"attachments": atts},
            "correspondent": {
                "intent": intent,
                "trust": "verified",
                "signals": [],
                "attachment_lanes": [],
                "relations": rels,
                "llm_calls": 0,
                "agent": {},
            },
            "bossdesk": [],
            "outbox_ids": [],
            "handoffs": [],
        }

    first = email("m1", "status_request", [], [])
    second = email(
        "m2",
        "document_submission",
        [
            {"name": "late_exhibit.pdf", "ref": "late"},
        ],
        [
            {
                "a": "late_exhibit.pdf",
                "b": "set.pdf",
                "kind": "completes",
                "confidence": 0.9,
            }
        ],
    )
    doc = {
        "id": "m0",
        "kind": "document",
        "truth": {},
        "wire": {"attachments": [{"name": "set.pdf", "ref": "set"}]},
        "handoffs": [],
    }
    out = compare_scenario(scenario, [doc, first, second], {}, stuck=[], personas={})
    by = {c["key"]: c for c in out["checks"]}
    assert (
        by["intent"]["ok"] is True
    )  # the email carrying the related document is the subject
    assert by["relation"]["ok"] is True
