"""CrewAI arbiter: settles judge/specialist disagreements with a bounded action."""

from __future__ import annotations

import json
from typing import Any, Literal

from crewai import Agent, Crew, Task
from pydantic import BaseModel, Field

from mailroom_reloaded.agents.judge import JudgeVerdict
from mailroom_reloaded.llm.client import make_llm, record_crew_usage
from mailroom_reloaded.prompts.loader import load_prompt
from mailroom_reloaded.tools import ToolContext, crewai_tool, tools_for

ROLE = "arbiter"


class ArbiterDecision(BaseModel):
    """One bounded arbiter action plus any acceptance caveats."""

    action: Literal["accept", "accept_with_caveats", "re_extract", "escalate"]
    caveats: list[str] = Field(default_factory=list)


def _json(value: Any) -> str:
    """Render ``value`` as indented, non-ASCII-safe JSON for the task prompt."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def arbitrate(
    text: str,
    doc_type: str,
    data: dict | None,
    verdict: JudgeVerdict,
    ctx: ToolContext,
) -> ArbiterDecision:
    """Decide the least destructive sufficient action after a judge finding.

    Append reported arbiter usage to ``ctx.usage_sink`` and emit LLM metrics
    before validating the decision. Configuration, crew execution, usage
    conversion and output validation errors propagate to the caller.
    """
    tools = tools_for(ROLE, ctx)
    description = (
        f"Document type: {doc_type}\n\n"
        f"Source text:\n{text}\n\n"
        f"Specialist extraction:\n{_json(data)}\n\n"
        f"Judge verdict:\n{verdict.model_dump_json(indent=2)}"
    )
    agent = Agent(
        role=ROLE,
        goal="Settle the judge/specialist disagreement and return one bounded decision.",
        backstory=load_prompt("arbiter"),
        llm=make_llm(ROLE),
        tools=[crewai_tool(td, context=ctx) for td in tools],
        max_iter=4,
        allow_delegation=False,
    )
    task = Task(
        description=description,
        expected_output="A single JSON object matching ArbiterDecision.",
        output_pydantic=ArbiterDecision,
        agent=agent,
    )
    result = Crew(agents=[agent], tasks=[task]).kickoff()
    record_crew_usage(ROLE, result, ctx.usage_sink)
    if isinstance(result.pydantic, ArbiterDecision):
        return result.pydantic
    if result.json_dict:
        return ArbiterDecision.model_validate(result.json_dict)
    return ArbiterDecision.model_validate_json(result.raw)
