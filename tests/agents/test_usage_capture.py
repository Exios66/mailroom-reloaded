"""CrewAI-routed roles (judge, arbiter, boss, grader) report their token usage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mailroom_reloaded.agents.arbiter import arbitrate
from mailroom_reloaded.agents.boss import escalate
from mailroom_reloaded.agents.judge import JudgeVerdict, judge_grade, judge_verify
from mailroom_reloaded.llm import client
from mailroom_reloaded.llm.usage import Usage, add_role_usage, usage_from_crew
from mailroom_reloaded.tools import ToolContext

VERDICT = '{"label": "complete", "score": 0.95, "field_findings": []}'
ARBITER = '{"action": "accept", "caveats": []}'
BOSS = '{"action": "accept", "reason": "fine"}'
GRADE = '{"fields": [], "classification": {"verdict": "correct", "rationale": ""}, "overall": 0.9}'


class _Recorder:
    """Stands in for one OTel instrument: remembers every ``add`` / ``record`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, dict]] = []

    def add(self, value, attributes=None) -> None:
        self.calls.append((value, dict(attributes or {})))

    record = add


@pytest.fixture
def reader(monkeypatch):
    """Swap the metric namespace used by ``llm.client`` (leaves the global provider alone)."""
    fake = SimpleNamespace(
        **{
            n: _Recorder()
            for n in (
                "llm_calls",
                "token_usage",
                "operation_duration",
                "cost_usd",
                "length_capped",
            )
        }
    )
    monkeypatch.setattr(client, "M", fake)
    return fake


def test_usage_sub_clamps_at_zero() -> None:
    a = Usage(10, 5, 1.0, 2)
    assert a - Usage(4, 1, 0.25, 1) == Usage(6, 4, 0.75, 1)
    assert Usage(1, 1, 0.0, 1) - Usage(5, 5, 1.0, 5) == Usage()


def test_add_role_usage_skips_empty_and_accumulates() -> None:
    by_role: dict[str, Usage] = {}
    add_role_usage(by_role, "judge", Usage())
    assert by_role == {}
    add_role_usage(by_role, "judge", Usage(1, 2, 0.0, 1))
    add_role_usage(by_role, "judge", Usage(3, 4, 0.0, 1))
    assert by_role == {"judge": Usage(4, 6, 0.0, 2)}


def test_usage_from_crew_tolerates_missing_fields() -> None:
    assert usage_from_crew(None) == Usage()
    assert usage_from_crew(SimpleNamespace(prompt_tokens=7)) == Usage(7, 0, 0.0, 0)


def test_record_crew_usage_fills_sink_and_metrics(mock_provider, reader) -> None:
    result = SimpleNamespace(
        token_usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=50, successful_requests=2
        )
    )
    sink: list = []
    usage = client.record_crew_usage("judge", result, sink)
    assert usage == Usage(100, 50, 0.0, 2)
    assert sink == [("judge", usage)]
    assert [(v, a["role"]) for v, a in reader.llm_calls.calls] == [(2, "judge")]
    assert reader.token_usage.calls  # tokens recorded
    assert reader.operation_duration.calls == []  # no latency is known
    assert len(reader.cost_usd.calls) == 1


def test_record_usage_ignores_empty_usage_and_unknown_roles(
    mock_provider, reader
) -> None:
    client.record_usage("judge", Usage())
    client.record_usage("no_such_role", Usage(1, 1, 0.0, 1))  # must not raise
    assert reader.llm_calls.calls == []


def test_cost_for_prices_the_grader_at_the_judge_model(monkeypatch) -> None:
    from mailroom_reloaded.settings import load_taxonomy

    judge_model = load_taxonomy().agent("judge").model
    raw = load_taxonomy().raw
    monkeypatch.setitem(
        raw["cost_models"],
        judge_model,
        {"input_per_million": 2, "output_per_million": 8},
    )
    usage = Usage(1_000_000, 1_000_000, 0.0, 1)
    assert client.cost_for("judge", usage) == pytest.approx(10.0)
    assert client.cost_for("grader", usage) == pytest.approx(10.0)
    assert client.cost_for("no_such_role", usage) == 0.0


def test_judge_arbiter_boss_append_to_usage_sink(mock_provider) -> None:
    ctx = ToolContext(doc_text="Acme and Beta agree.", doc_id="d1")
    mock_provider.reply(VERDICT)
    verdict = judge_verify("Acme and Beta agree.", "correspondence", {"a": 1}, ctx)
    mock_provider.reply(ARBITER)
    arbitrate(
        "Acme and Beta agree.",
        "correspondence",
        {"a": 1},
        JudgeVerdict.model_validate(verdict),
        ctx,
    )
    mock_provider.reply(BOSS)
    escalate("Acme and Beta agree.", {"doc_type": "x"}, ctx)
    roles = [role for role, _ in ctx.usage_sink]
    assert roles == ["judge", "arbiter", "boss"]
    assert all(u.prompt_tokens > 0 and u.calls > 0 for _, u in ctx.usage_sink)


def test_grader_usage_stays_on_the_grade_not_the_sink(mock_provider) -> None:
    ctx = ToolContext(
        doc_text="t",
        doc_id="d1",
        eval_mode=True,
        ground_truth=lambda _id: {"expected": "x"},
    )
    mock_provider.tool_call("get_ground_truth", {}).reply(GRADE)
    grade = judge_grade("t", "correspondence", {"a": 1}, ctx)
    assert grade.usage.prompt_tokens > 0
    assert ctx.usage_sink == []


def test_rejected_grade_still_emits_grader_metrics(
    mock_provider, monkeypatch, reader
) -> None:
    """The tokens were spent even when the grader's output fails validation."""
    from mailroom_reloaded.agents import judge

    spent = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=30,
        cached_prompt_tokens=0,
        successful_requests=1,
    )
    unparseable = SimpleNamespace(
        pydantic=None, json_dict=None, raw="not a grade", token_usage=spent
    )
    monkeypatch.setattr(judge, "_run", lambda *a, **k: unparseable)
    ctx = ToolContext(
        doc_text="t", doc_id="d1", eval_mode=True, ground_truth=lambda _id: {}
    )
    with pytest.raises(ValueError):
        judge_grade("t", "correspondence", {"a": 1}, ctx)
    assert [(v, a["role"]) for v, a in reader.llm_calls.calls] == [(1, "grader")]
    assert reader.token_usage.calls
