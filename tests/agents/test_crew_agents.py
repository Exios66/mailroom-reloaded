import importlib
import os

import pytest
from fakes.openai_server import FakeOpenAI
from pydantic import ValidationError

from mailroom_reloaded.agents.arbiter import ArbiterDecision, arbitrate
from mailroom_reloaded.agents.boss import BossDecision, escalate
from mailroom_reloaded.agents.judge import (
    ClassificationFinding,
    FieldFinding,
    JudgeGrade,
    JudgeVerdict,
    judge_grade,
    judge_same_model,
    judge_verify,
)
from mailroom_reloaded.llm.client import resolve
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.settings import load_taxonomy
from mailroom_reloaded.tools import ToolContext

JUDGE_VERDICT_JSON = (
    '{"label": "complete", "score": 0.95, '
    '"field_findings": [{"field": "party_name", "verdict": "correct", "rationale": ""}]}'
)
JUDGE_GRADE_JSON = (
    '{"fields": [{"field": "party_name", "verdict": "gt_suspect", '
    '"rationale": "label looks noisy"}], '
    '"classification": {"verdict": "correct", "rationale": "doc_type and subclass match"}, '
    '"overall": 0.9}'
)
ARBITER_JSON = '{"action": "accept_with_caveats", "caveats": ["date format differs"]}'
BOSS_JSON = (
    '{"action": "reassign_class", "doc_type": "insurance_claim", '
    '"doc_subclass": "fnol", "reason": "coverage-denial paperwork"}'
)


@pytest.fixture
def fake_openai():
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


def _ctx(**kw):
    base = {"doc_text": "Acme and Beta agree.", "doc_id": "deadbeef", "eval_mode": False}
    base.update(kw)
    return ToolContext(**base)


def test_judge_grade_calls_ground_truth_tool(mock_provider):
    seen: dict[str, str] = {}

    def gt(doc_id: str) -> dict:
        seen["doc_id"] = doc_id
        return {
            "expected": "correspondence",
            "expected_subclass": "memo",
            "party_name": "Acme",
        }

    ctx = _ctx(eval_mode=True, ground_truth=gt)
    mock_provider.tool_call("get_ground_truth", {}).reply(JUDGE_GRADE_JSON)
    grade = judge_grade("Acme and Beta agree.", "correspondence", {"party_name": "Acme"}, ctx)

    assert seen.get("doc_id") == "deadbeef"
    assert isinstance(grade, JudgeGrade)
    assert grade.doc_id == "deadbeef"
    assert grade.doc_type == "correspondence"
    assert grade.fields[0].verdict == "gt_suspect"
    assert grade.classification.verdict == "correct"
    assert isinstance(grade.usage, Usage)
    tool_names = [t["function"]["name"] for t in mock_provider.requests[0]["tools"]]
    assert "get_ground_truth" in tool_names


def test_judge_verify_has_no_gt_tool(mock_provider):
    ctx = _ctx(eval_mode=True, ground_truth=lambda _: {"party_name": "Acme"})
    mock_provider.reply(JUDGE_VERDICT_JSON)
    verdict = judge_verify("Acme and Beta agree.", "correspondence", {"party_name": "Acme"}, ctx)

    assert isinstance(verdict, JudgeVerdict)
    assert verdict.label == "complete"
    assert mock_provider.requests, "expected at least one request"
    for req in mock_provider.requests:
        names = [t["function"]["name"] for t in req.get("tools", [])]
        assert "get_ground_truth" not in names


def test_judge_grade_requires_eval_mode():
    ctx = _ctx(eval_mode=False, ground_truth=lambda _: {})
    with pytest.raises(ValueError):
        judge_grade("text", "correspondence", {}, ctx)


def test_arbiter_returns_decision(mock_provider):
    ctx = _ctx()
    mock_provider.tool_call("get_taxonomy", {}).reply(ARBITER_JSON)
    verdict = JudgeVerdict(
        label="partial", score=0.6,
        field_findings=[FieldFinding(field="party_name", verdict="correct")],
    )
    decision = arbitrate("Acme and Beta agree.", "correspondence", {"party_name": "Acme"}, verdict, ctx)
    assert isinstance(decision, ArbiterDecision)
    assert decision.action == "accept_with_caveats"
    assert decision.caveats == ["date format differs"]


def test_boss_reassign(mock_provider):
    ctx = _ctx()
    mock_provider.tool_call("list_subclasses", {"doc_type": "correspondence"}).reply(BOSS_JSON)
    decision = escalate("Acme and Beta agree.", {"doc_type": "correspondence", "attempts": 2}, ctx)
    assert isinstance(decision, BossDecision)
    assert decision.action == "reassign_class"
    assert decision.doc_type == "insurance_claim"
    assert decision.doc_subclass == "fnol"
    assert decision.reason


def test_judge_uses_configured_model(mock_provider, monkeypatch):
    from mailroom_reloaded.llm import client

    tax = load_taxonomy()
    assert resolve("judge").model == tax.agent("judge").model
    assert judge_same_model(tax) is True  # the shipped taxonomy uses the shared model

    alt = tax.model_copy(deep=True)
    alt.agents["judge"] = alt.agents["judge"].model_copy(update={"model": "other/judge-model"})
    assert judge_same_model(alt) is False

    monkeypatch.setattr(client, "load_taxonomy", lambda: alt)
    assert resolve("judge").model == "other/judge-model"
    assert resolve("sorter").model == tax.agent("sorter").model


def test_judge_grade_gt_suspect_allowed():
    finding = FieldFinding(field="party_name", verdict="gt_suspect", rationale="noisy label")
    assert finding.verdict == "gt_suspect"
    with pytest.raises(ValidationError):
        FieldFinding(field="party_name", verdict="bogus")


def test_judge_grades_classification():
    classification = ClassificationFinding(verdict="incorrect", rationale="subclass differs")
    assert classification.verdict == "incorrect" and classification.rationale
    grade = JudgeGrade(
        doc_id="d1", doc_type="contract", classification=classification, overall=0.5, usage=Usage()
    )
    assert grade.classification.verdict == "incorrect"
    assert grade.classification.rationale == "subclass differs"


def test_crewai_telemetry_disabled(monkeypatch):
    monkeypatch.delenv("CREWAI_DISABLE_TELEMETRY", raising=False)
    import mailroom_reloaded

    importlib.reload(mailroom_reloaded)
    import mailroom_reloaded.agents as agents_pkg

    assert agents_pkg is not None
    assert os.environ["CREWAI_DISABLE_TELEMETRY"] == "true"


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("MAILROOM_LIVE") != "1", reason="set MAILROOM_LIVE=1 for live tests"
)
def test_live_agent_tool_use():
    ctx = _ctx()
    verdict = judge_verify(
        "The parties are Acme and Beta.",
        "correspondence",
        {"party_name": "Acme", "parties": ["Acme", "Beta"]},
        ctx,
    )
    assert verdict.label in {"complete", "partial", "incomplete"}
    assert 0.0 <= verdict.score <= 1.0
