"""CrewAI boss: adjudicates escalation, reassigning the class or parking for a human."""

from __future__ import annotations

import json
from typing import Any, Literal

from crewai import Agent, Crew, Task
from pydantic import BaseModel

from mailroom_reloaded.llm.client import make_llm
from mailroom_reloaded.prompts.loader import load_prompt
from mailroom_reloaded.tools import ToolContext, crewai_tool, tools_for

ROLE = "boss"


class BossDecision(BaseModel):
    """One boss escalation action; class fields are set only when reassigning."""

    action: Literal["reassign_class", "accept", "human_review"]
    doc_type: str | None = None
    doc_subclass: str | None = None
    reason: str = ""


def _json(value: Any) -> str:
    """Render ``value`` as indented, non-ASCII-safe JSON for the task prompt."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def escalate(text: str, state_summary: dict | None, ctx: ToolContext) -> BossDecision:
    """Adjudicate an escalation from the manifest state summary."""
    tools = tools_for(ROLE, ctx)
    description = (
        f"Escalation summary:\n{_json(state_summary)}\n\n"
        f"Source text:\n{text}"
    )
    agent = Agent(
        role=ROLE,
        goal="Adjudicate the escalation and return one decision.",
        backstory=load_prompt("boss"),
        llm=make_llm(ROLE),
        tools=[crewai_tool(td, context=ctx) for td in tools],
        max_iter=4,
        allow_delegation=False,
    )
    task = Task(
        description=description,
        expected_output="A single JSON object matching BossDecision.",
        output_pydantic=BossDecision,
        agent=agent,
    )
    result = Crew(agents=[agent], tasks=[task]).kickoff()
    if isinstance(result.pydantic, BossDecision):
        return result.pydantic
    if result.json_dict:
        return BossDecision.model_validate(result.json_dict)
    return BossDecision.model_validate_json(result.raw)
