"""Per-role usage flows into the state, the token guards and the report cost."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import trace

from mailroom_reloaded.agents.arbiter import ArbiterDecision
from mailroom_reloaded.agents.boss import BossDecision
from mailroom_reloaded.agents.judge import (
    ClassificationFinding,
    JudgeGrade,
    JudgeVerdict,
)
from mailroom_reloaded.agents.sorter import SortResult
from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import SortMode
from mailroom_reloaded.ingest.clerk import IngestResult
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.pipeline import report as report_mod
from mailroom_reloaded.pipeline.guards import NodeFailed
from mailroom_reloaded.pipeline.report import compile_report
from mailroom_reloaded.pipeline.state import MailroomState
from mailroom_reloaded.schemas.manifest import Manifest

U = Usage(prompt_tokens=100, completion_tokens=50, calls=1)


@pytest.fixture
def flow(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("CREWAI_TELEMETRY_DISABLED", "true")
    f = flow_mod.MailroomFlow()
    f.state.doc_id = "doc-1"
    f.state.text = "source text"
    f.state.path = "processing/letter.txt"
    f._overrides = {}
    f._resume_done = set()
    f._resume_from = None
    f._eval_ctx = None
    f._llm_calls = 0
    f._bins = Mock()
    f._bins.move.return_value = "failed/letter.txt"
    f._manifest = Manifest(doc_id="doc-1", filename="letter.txt", content_sha256="abc")
    f._tracer = trace.NoOpTracer()
    f._gate = Mock()
    monkeypatch.setattr(flow_mod, "save_manifest", Mock())
    monkeypatch.setattr(flow_mod.audit_log, "append", Mock())
    return f


def _sort(usage: Usage = U) -> SortResult:
    return SortResult(
        "correspondence",
        None,
        0.9,
        0.9,
        False,
        SortMode.FULL,
        "self_report",
        False,
        None,
        usage,
    )


def _ext(usage: Usage = U) -> ExtractResult:
    return ExtractResult("correspondence", {"a": 1}, True, None, 0.9, None, 1, usage)


def _verdict() -> JudgeVerdict:
    return JudgeVerdict(label="complete", score=0.9, field_findings=[])


def test_sort_and_specialist_usage_is_recorded_by_role(flow, monkeypatch) -> None:
    monkeypatch.setattr(flow_mod, "_sort", lambda *a, **k: _sort())
    flow._node_sort()
    monkeypatch.setattr(flow_mod, "_extract", lambda *a, **k: _ext())
    flow._node_extract()
    specialist = flow_mod.load_taxonomy().classes["correspondence"].specialist
    assert set(flow.state.usage_by_role) == {"sorter", specialist}
    assert flow.state.usage_total == U + U
    assert sum(flow.state.usage_by_role.values(), Usage()) == flow.state.usage_total


def test_verify_drains_judge_and_arbiter_into_totals(flow, monkeypatch) -> None:
    def judge(text, doc_type, data, ctx):
        ctx.usage_sink.append(("judge", U))
        return _verdict()

    def arbiter(text, doc_type, data, verdict, ctx):
        ctx.usage_sink.append(("arbiter", U))
        return ArbiterDecision(action="accept")

    monkeypatch.setattr(flow_mod, "judge_verify", judge)
    monkeypatch.setattr(flow_mod, "arbitrate", arbiter)
    flow.state.extract = _ext(Usage())
    flow._node_verify()
    assert flow.state.usage_by_role == {"judge": U, "arbiter": U}
    assert flow.state.usage_total == U + U
    assert flow.state.usage_partial_nodes == []
    assert flow._sink() == []


def test_boss_usage_is_captured(flow, monkeypatch) -> None:
    def boss(text, summary, ctx):
        ctx.usage_sink.append(("boss", U))
        return BossDecision(action="accept")

    monkeypatch.setattr(flow_mod, "escalate", boss)
    flow._node_boss()
    assert flow.state.usage_by_role == {"boss": U}
    assert flow.state.usage_total == U


def test_node_that_raises_keeps_partial_usage_and_is_flagged(flow, monkeypatch) -> None:
    def judge(text, doc_type, data, ctx):
        ctx.usage_sink.append(("judge", U))
        return _verdict()

    def arbiter(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(flow_mod, "judge_verify", judge)
    monkeypatch.setattr(flow_mod, "arbitrate", arbiter)
    flow.state.extract = _ext(Usage())
    with pytest.raises(RuntimeError):
        flow._node_verify()
    assert flow.state.usage_by_role == {"judge": U}
    assert flow.state.usage_total == U
    assert flow.state.usage_partial_nodes == ["verify"]
    saved = flow._manifest.state
    assert saved["usage_partial_nodes"] == [
        "verify"
    ]  # persisted with the completed prefix


def test_non_llm_node_failure_is_not_flagged_partial(flow) -> None:
    def work(instance):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        flow._guard_node("report_catalog_archive", 0, 0, work, (), {})
    assert flow.state.usage_partial_nodes == []


def test_failed_ingest_keeps_vision_spend(flow, monkeypatch) -> None:
    failed = IngestResult("", "vision", 3, error="vision failed", usage=U)
    monkeypatch.setattr(flow_mod, "_ingest", lambda path: failed)
    with pytest.raises(NodeFailed):
        flow._node_ingest()
    assert flow.state.usage_by_role == {"pdf_transcriber": U}
    assert flow.state.usage_total == U


def test_grader_usage_is_separate_from_pipeline_total(flow, monkeypatch) -> None:
    grade = JudgeGrade(
        doc_id="doc-1",
        doc_type="correspondence",
        fields=[],
        classification=ClassificationFinding(verdict="correct", rationale=""),
        overall=0.9,
        usage=U,
    )
    monkeypatch.setattr(flow_mod, "judge_grade", lambda *a, **k: grade)
    flow._eval_ctx = SimpleNamespace(run_id="r", ground_truth={})
    flow._ground_truth_fn = lambda: lambda doc_id: {}
    flow.state.extract = _ext(Usage())
    flow._node_grade()
    assert flow.state.usage_by_role == {"grader": U}
    assert flow.state.usage_total == Usage()


def test_failed_grade_is_flagged_but_never_fails_the_document(
    flow, monkeypatch
) -> None:
    def broken(*a, **k):
        raise RuntimeError("judge down")

    monkeypatch.setattr(flow_mod, "judge_grade", broken)
    flow._eval_ctx = SimpleNamespace(run_id="r", ground_truth={})
    flow._ground_truth_fn = lambda: lambda doc_id: {}
    flow.state.extract = _ext(Usage())
    flow._node_grade()
    assert flow.state.grade is None
    assert flow.state.usage_partial_nodes == ["grade"]


def test_token_budget_guard_sees_judge_and_arbiter_spend(flow, monkeypatch) -> None:
    def judge(text, doc_type, data, ctx):
        ctx.usage_sink.append(
            ("judge", Usage(prompt_tokens=400, completion_tokens=100, calls=1))
        )
        return _verdict()

    monkeypatch.setattr(flow_mod, "judge_verify", judge)
    monkeypatch.setattr(
        flow_mod, "arbitrate", lambda *a, **k: ArbiterDecision(action="accept")
    )
    flow._overrides = {"token_budgets": {"verify": 499}}
    flow.state.extract = _ext(Usage())
    with pytest.raises(NodeFailed) as exc:
        flow._node_verify()
    assert exc.value.reason == "token_budget_exceeded"
    assert flow.state.usage_total.total_tokens == 500


def test_old_manifest_state_without_role_usage_loads() -> None:
    state = MailroomState.model_validate(
        {"doc_id": "x", "usage_total": {"prompt_tokens": 5}}
    )
    assert state.usage_by_role == {} and state.usage_partial_nodes == []
    restored = MailroomState.model_validate(
        MailroomState(usage_by_role={"judge": U}).model_dump(mode="json")
    )
    assert restored.usage_by_role == {"judge": U}


def _taxonomy(prices: dict) -> SimpleNamespace:
    models = {
        "sorter": "m-sorter",
        "judge": "m-judge",
        "boss": "m-boss",
        "arbiter": "m-arb",
    }
    return SimpleNamespace(
        agent=lambda role: SimpleNamespace(model=models[role]),
        raw={"cost_models": prices},
    )


def test_report_cost_is_the_sum_of_per_role_cost_and_excludes_grader(
    monkeypatch,
) -> None:
    tax = _taxonomy(
        {
            "m-sorter": {"input_per_million": 1, "output_per_million": 1},
            "m-judge": {"input_per_million": 10, "output_per_million": 10},
        }
    )
    monkeypatch.setattr(report_mod, "load_taxonomy", lambda: tax)
    million = Usage(1_000_000, 1_000_000, 0.0, 1)
    state = MailroomState(
        usage_total=million + million,
        usage_by_role={"sorter": million, "judge": million, "grader": million},
    )
    assert compile_report(state)["cost"]["usd"] == pytest.approx(2.0 + 20.0)


def test_report_cost_of_legacy_state_prices_at_the_sorter(monkeypatch) -> None:
    tax = _taxonomy({"m-sorter": {"input_per_million": 1, "output_per_million": 1}})
    monkeypatch.setattr(report_mod, "load_taxonomy", lambda: tax)
    state = MailroomState(usage_total=Usage(1_000_000, 0, 0.0, 1))
    assert compile_report(state)["cost"]["usd"] == pytest.approx(1.0)


def test_report_cost_prices_legacy_residual_at_the_sorter_on_a_resumed_manifest(monkeypatch) -> None:
    tax = _taxonomy(
        {
            "m-sorter": {"input_per_million": 1, "output_per_million": 1},
            "m-judge": {"input_per_million": 10, "output_per_million": 10},
        }
    )
    monkeypatch.setattr(report_mod, "load_taxonomy", lambda: tax)
    million = Usage(1_000_000, 1_000_000, 0.0, 1)
    # one million sorter-priced tokens came from the old manifest, the judge ran after resume
    state = MailroomState(usage_total=million + million, usage_by_role={"judge": million})
    assert compile_report(state)["cost"]["usd"] == pytest.approx(2.0 + 20.0)
