"""CrewAI judge: live completeness verification and eval ground-truth grading.

``judge_verify`` is the live mode (no ground truth); ``judge_grade`` is the eval
``grade`` step and receives ground truth through the ``get_ground_truth`` tool
only. The judge model is configured separately (``agents.judge.model``); the
``judge_same_model`` flag reports when it collides with a specialist model.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from crewai import Agent, Crew, Task
from pydantic import BaseModel, ConfigDict, Field

from mailroom_reloaded.llm.client import make_llm
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.prompts.loader import load_prompt
from mailroom_reloaded.settings import Taxonomy, load_taxonomy
from mailroom_reloaded.tools import ToolContext, crewai_tool, tools_for

ROLE = "judge"


class FieldFinding(BaseModel):
    """One field's grading verdict and the rationale behind it."""

    field: str
    verdict: Literal[
        "correct", "partial", "wrong", "missing", "hallucinated", "gt_suspect"
    ]
    rationale: str = ""


class JudgeVerdict(BaseModel):
    """Live-mode completeness verdict (no ground truth)."""

    label: Literal["complete", "partial", "incomplete"]
    score: float
    field_findings: list[FieldFinding] = Field(default_factory=list)


class ClassificationFinding(BaseModel):
    """The grading verdict for the sorter's classification."""

    verdict: Literal["correct", "incorrect", "gt_suspect"]
    rationale: str = ""


class _GradeOutput(BaseModel):
    """The JSON shape the grading task asks the model to emit (no doc_id/usage)."""

    fields: list[FieldFinding] = Field(default_factory=list)
    classification: ClassificationFinding
    overall: float


class JudgeGrade(BaseModel):
    """Eval-mode grading result: field findings, classification and usage."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    doc_id: str
    doc_type: str
    fields: list[FieldFinding] = Field(default_factory=list)
    classification: ClassificationFinding
    overall: float
    usage: Usage


def judge_same_model(taxonomy: Taxonomy | None = None) -> bool:
    """True when ``agents.judge.model`` matches any specialist model."""
    tax = taxonomy or load_taxonomy()
    judge_model = tax.agent("judge").model
    return any(
        tax.agent(c.specialist).model == judge_model for c in tax.classes.values()
    )


def _json(value: Any) -> str:
    """Render ``value`` as indented, non-ASCII-safe JSON for the task prompt."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _usage(token_usage: Any) -> Usage:
    """Convert a CrewAI ``token_usage`` object into a ``Usage`` total."""
    return Usage(
        prompt_tokens=int(getattr(token_usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(token_usage, "completion_tokens", 0) or 0),
        calls=int(getattr(token_usage, "successful_requests", 0) or 0),
    )


def _run(
    goal: str,
    backstory: str,
    description: str,
    model: type[BaseModel],
    ctx: ToolContext,
    tools,
):
    """Build and run the single-agent judge crew for one task."""
    agent = Agent(
        role=ROLE,
        goal=goal,
        backstory=backstory,
        llm=make_llm(ROLE),
        tools=[crewai_tool(td, context=ctx) for td in tools],
        max_iter=4,
        allow_delegation=False,
    )
    task = Task(
        description=description,
        expected_output=f"A single JSON object matching {model.__name__}.",
        output_pydantic=model,
        agent=agent,
    )
    return Crew(agents=[agent], tasks=[task]).kickoff()


def _extract(result: Any, model: type[BaseModel]) -> BaseModel:
    """Coerce a CrewAI ``CrewOutput`` into ``model`` (pydantic, json_dict or raw)."""
    if isinstance(result.pydantic, model):
        return result.pydantic
    if result.json_dict:
        return model.model_validate(result.json_dict)
    return model.model_validate_json(result.raw)


def judge_verify(
    text: str, doc_type: str, data: dict | None, ctx: ToolContext
) -> JudgeVerdict:
    """Live source-grounded completeness/correctness verdict; never sees ground truth."""
    tools = [td for td in tools_for(ROLE, ctx) if td.name != "get_ground_truth"]
    description = (
        f"Document type: {doc_type}\n\n"
        f"Source text:\n{text}\n\n"
        f"Extraction to judge:\n{_json(data)}"
    )
    result = _run(
        "Judge whether the extraction is complete and correct against the source text.",
        load_prompt("judge_completeness"),
        description,
        JudgeVerdict,
        ctx,
        tools,
    )
    return _extract(result, JudgeVerdict)


def judge_grade(
    text: str, doc_type: str, data: dict | None, ctx: ToolContext
) -> JudgeGrade:
    """Eval grading against ground truth; requires ``ctx.eval_mode``."""
    if not ctx.eval_mode:
        raise ValueError(
            "judge_grade requires ctx.eval_mode (ground truth is eval-only)"
        )
    tools = tools_for(ROLE, ctx)
    if not any(td.name == "get_ground_truth" for td in tools):
        raise ValueError("judge_grade requires ctx.ground_truth to fetch the labels")
    description = (
        f"Document id: {ctx.doc_id}\n\n"
        f"Source text:\n{text}\n\n"
        f"Extraction to grade:\n{_json(data)}"
    )
    result = _run(
        "Grade every extracted field and the classification against the ground truth.",
        load_prompt("judge_grade").format(doc_type=doc_type),
        description,
        _GradeOutput,
        ctx,
        tools,
    )
    parsed = _extract(result, _GradeOutput)
    return JudgeGrade(
        doc_id=ctx.doc_id,
        doc_type=doc_type,
        fields=parsed.fields,
        classification=parsed.classification,
        overall=parsed.overall,
        usage=_usage(result.token_usage),
    )
