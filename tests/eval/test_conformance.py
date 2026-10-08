"""Task 24: the behavioural conformance suite and its invariants.

The four required tests pin the spec section 11 invariants and the card rates.
They build :class:`RoleRun` records directly (and one drives the real sorter
through a scripted ``FakeOpenAI``) so they are deterministic and offline.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fakes.openai_server import FakeOpenAI

from mailroom_reloaded.eval import conformance as cf
from mailroom_reloaded.eval.conformance import (
    INVARIANTS,
    RoleRun,
    ToolCall,
    render_card_md,
    run_conformance,
)
from mailroom_reloaded.schemas.extraction import get_extraction_schema

CORRESPONDENCE_01 = "correspondence_01.txt"


@pytest.fixture
def fake_openai():
    """Yield a local fake OpenAI server and stop it after the test."""
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):
    """Point the mock provider at the local fake OpenAI server."""
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    """Disable retry delays and clear tool-support caches around each test."""
    from mailroom_reloaded.llm import retry, tooling

    monkeypatch.setattr(retry, "_sleep", lambda *_: None)
    tooling.reset_tool_support_cache()
    yield
    tooling.reset_tool_support_cache()


def _invariant(role: str, name: str):
    """Look up one invariant's predicate by role and name."""
    return next(i for i in INVARIANTS if i.role == role and i.name == name).check


# --------------------------------------------------------------------------- invariants


def test_sorter_subclass_only_doc_type_change_without_flag_fails_invariant():
    check = _invariant("sorter", "subclass_only_doc_type_guard")
    silent = RoleRun(
        role="sorter",
        filename=CORRESPONDENCE_01,
        doc_type="correspondence",
        mode="subclass_only",
        parsed={"doc_type": "contract", "doc_type_disagree": False},
    )
    assert check(silent) is False

    flagged = replace(
        silent, parsed={"doc_type": "contract", "doc_type_disagree": True}
    )
    assert check(flagged) is True

    same = replace(
        silent, parsed={"doc_type": "correspondence", "doc_type_disagree": False}
    )
    assert check(same) is True


def test_specialist_extra_key_fails_invariant():
    check = _invariant("specialists", "no_keys_outside_schema")
    base = get_extraction_schema("correspondence")().model_dump()
    clean = RoleRun(
        role="specialists",
        filename=CORRESPONDENCE_01,
        doc_type="correspondence",
        mode="live",
        parsed=base,
        schema_valid=True,
    )
    assert check(clean) is True

    dirty = replace(clean, parsed={**base, "legacy_key_points": ["x"]})
    assert check(dirty) is False


def test_judge_live_mode_gt_tool_call_fails_invariant():
    check = _invariant("judge", "live_never_ground_truth")
    live = RoleRun(
        role="judge",
        filename=CORRESPONDENCE_01,
        doc_type="correspondence",
        mode="live",
        parsed={"label": "complete", "score": 0.9},
        tool_calls=[ToolCall("judge", "get_ground_truth", True)],
    )
    assert check(live) is False

    clean_live = replace(live, tool_calls=[ToolCall("judge", "search_source", True)])
    assert check(clean_live) is True

    grade = replace(live, mode="grade")
    assert check(grade) is True


def test_card_rates(monkeypatch, tmp_path):
    calls = [
        ToolCall("sorter", "get_taxonomy", True),
        ToolCall("sorter", "list_subclasses", True),
        ToolCall("sorter", "get_taxonomy", True),
        ToolCall("sorter", "list_subclasses", False),
    ]
    sorter_run = RoleRun(
        role="sorter",
        filename="contract_01.txt",
        doc_type="contract",
        mode="full",
        parsed={
            "doc_type": "contract",
            "doc_subclass": None,
            "doc_type_disagree": False,
        },
        tool_calls=calls,
    )
    runs = {
        "sorter": [sorter_run],
        "specialists": [],
        "judge": [],
        "arbiter": [],
        "boss": [],
    }
    monkeypatch.setattr(cf, "_collect_runs", lambda **_: runs)

    card = run_conformance("mock", per_class=1, out_dir=tmp_path)

    assert card.roles["sorter"].tool_call_success_rate == 0.75
    assert card.roles["sorter"].invariant_pass_rate == 1.0
    assert (tmp_path / "conformance-mock.json").is_file()
    assert (tmp_path / "conformance-mock.md").is_file()
    payload = json.loads((tmp_path / "conformance-mock.json").read_text("utf-8"))
    assert payload["roles"]["sorter"]["tool_call_success_rate"] == 0.75
    assert "sorter" in render_card_md(card)


def test_record_tool_calls_marks_error_output_failed():
    """A tool returning an error string is recorded as a failed call (rate 0.0)."""
    from mailroom_reloaded.tools import NoParams, ToolContext, ToolDef

    tool = ToolDef("boom", "always errors", NoParams, lambda _ctx: "error: boom")
    calls: list[ToolCall] = []
    with cf._record_tool_calls("sorter", calls):
        outcome = tool.bind(ToolContext()).fn()

    assert outcome == "error: boom"
    assert [tc.ok for tc in calls] == [False]

    run = RoleRun(
        role="sorter",
        filename=CORRESPONDENCE_01,
        doc_type="correspondence",
        mode="full",
        parsed={
            "doc_type": "correspondence",
            "doc_subclass": None,
            "doc_type_disagree": False,
        },
        tool_calls=calls,
    )
    assert cf.build_role_stats([run]).tool_call_success_rate == 0.0


def test_card_rates_flags_invariant_violation(monkeypatch, tmp_path):
    """A real sorter run that changes doc_type without the flag lowers the rate."""
    violating = RoleRun(
        role="sorter",
        filename=CORRESPONDENCE_01,
        doc_type="correspondence",
        mode="subclass_only",
        parsed={
            "doc_type": "contract",
            "doc_subclass": None,
            "doc_type_disagree": False,
        },
        tool_calls=[ToolCall("sorter", "list_subclasses", True)],
    )
    runs = {
        "sorter": [violating],
        "specialists": [],
        "judge": [],
        "arbiter": [],
        "boss": [],
    }
    monkeypatch.setattr(cf, "_collect_runs", lambda **_: runs)

    card = run_conformance("mock", per_class=1, out_dir=tmp_path)
    stats = card.roles["sorter"]
    assert stats.invariant_pass_rate is not None
    assert stats.invariant_pass_rate < 1.0
    assert "subclass_only_doc_type_guard" in {f.split(":")[0] for f in stats.failures}

    payload = json.loads((tmp_path / "conformance-mock.json").read_text("utf-8"))
    assert payload["roles"]["sorter"]["invariant_pass_rate"] < 1.0


def test_empty_role_is_not_reported_as_passing(monkeypatch, tmp_path):
    """A role with no collected runs reports n/a, never a vacuous 1.0."""
    runs: dict[str, list[RoleRun]] = {role: [] for role in cf._ROLES}
    monkeypatch.setattr(cf, "_collect_runs", lambda **_: runs)

    card = run_conformance("mock", per_class=1, out_dir=tmp_path)
    for role in cf._ROLES:
        stats = card.roles[role]
        assert stats.tool_call_success_rate != 1.0
        assert stats.invariant_pass_rate != 1.0
        assert stats.invariant_pass_rate is None

    payload = json.loads((tmp_path / "conformance-mock.json").read_text("utf-8"))
    assert payload["roles"]["sorter"]["tool_call_success_rate"] == "n/a"
    assert payload["roles"]["sorter"]["invariant_pass_rate"] == "n/a"
    assert "n/a" in render_card_md(card)


def test_judge_live_seam_never_calls_ground_truth(mock_provider, monkeypatch):
    """The live judge path never offers/fetches ground truth and never leaks it."""
    from helpers import assert_no_gt

    from mailroom_reloaded.agents.judge import ClassificationFinding, JudgeGrade
    from mailroom_reloaded.eval.dataset import BlindDoc, GroundTruth, sha256_text
    from mailroom_reloaded.llm.usage import Usage

    text = "Dear Sir or Madam,\n\nPlease review the attached draft.\n"
    doc = BlindDoc("correspondence_01.txt", text, sha256_text(text))
    gt = GroundTruth(
        filename=doc.filename,
        expected="correspondence",
        expected_subclass="letter",
        fields={"claim_number": "GT-SECRET-CLAIM-9911"},
    )

    stub = JudgeGrade(
        doc_id=cf.doc_id_for_sha(doc.content_sha256),
        doc_type="correspondence",
        classification=ClassificationFinding(verdict="correct"),
        overall=1.0,
        usage=Usage(),
    )
    monkeypatch.setattr(cf, "judge_grade", lambda *args, **kwargs: stub)

    mock_provider.reply('{"label": "complete", "score": 0.9, "field_findings": []}')
    runs = cf._judge_runs(doc, gt, {"party_name": "Acme"})

    live = next(run for run in runs if run.mode == "live")
    assert all(tc.name != "get_ground_truth" for tc in live.tool_calls)
    for request in mock_provider.requests:
        offered = [t["function"]["name"] for t in request.get("tools", [])]
        assert "get_ground_truth" not in offered
    assert_no_gt(mock_provider.requests, gt.fields)


# --------------------------------------------------------------------------- real harness


def test_sorter_runs_record_real_tool_calls(mock_provider):
    """The recorder sees real tool traffic through the scripted sorter."""
    full_json = json.dumps(
        {
            "doc_type": "correspondence",
            "doc_subclass": "letter",
            "confidence": 0.9,
            "doc_type_disagree": False,
            "doc_type_disagree_reason": None,
        }
    )
    scope_json = json.dumps(
        {
            "doc_subclass": "letter",
            "confidence": 0.9,
            "doc_type_disagree": False,
            "doc_type_disagree_reason": None,
        }
    )
    mock_provider.tool_call("list_subclasses", {"doc_type": "correspondence"})
    mock_provider.reply("done")
    mock_provider.reply(full_json)
    mock_provider.tool_call("list_subclasses", {"doc_type": "correspondence"})
    mock_provider.reply("done")
    mock_provider.reply(scope_json)

    from mailroom_reloaded.eval.dataset import BlindDoc, sha256_text

    text = "Dear Sir or Madam,\n\nPlease review the attached draft.\n"
    doc = BlindDoc("correspondence_01.txt", text, sha256_text(text))
    runs = cf._sorter_runs(doc, None)

    assert {run.mode for run in runs} == {"full", "subclass_only"}
    assert all(run.tool_calls for run in runs)
    assert all(tc.ok for run in runs for tc in run.tool_calls)
    assert cf.build_role_stats(runs).invariant_pass_rate == 1.0
