"""CrewAI flow state for the Digital Mailroom pipeline (spec section 4).

The state is a Pydantic model so it serialises cleanly into the manifest
(``Manifest.state``) for crash-resume. CrewAI's ``Flow[State]`` wraps it in a
``StateWithId`` subclass at construction; the ``id`` field is CrewAI's, not part
of the pipeline contract.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mailroom_reloaded.agents.arbiter import ArbiterDecision
from mailroom_reloaded.agents.boss import BossDecision
from mailroom_reloaded.agents.judge import JudgeGrade, JudgeVerdict
from mailroom_reloaded.agents.sorter import SortResult
from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import BertVerdict, Handoff
from mailroom_reloaded.ingest.clerk import IngestResult
from mailroom_reloaded.llm.usage import Usage

__all__ = ["NODE_ORDER", "MailroomState"]

#: Deterministic main-path nodes, in order. ``gate_classify`` / ``gate_extract``
#: are pure route computations (they make no LLM calls and are not persisted as
#: completed work); the conditional ``verify`` / ``boss`` / ``grade`` nodes are
#: dispatched by the driver outside this tuple.
NODE_ORDER: tuple[str, ...] = (
    "ingest",
    "bert_primary",
    "sort",
    "gate_classify",
    "extract",
    "gate_extract",
    "report_catalog_archive",
)


class MailroomState(BaseModel):
    """Everything one document accumulates across the pipeline."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # identity and text
    doc_id: str = ""
    path: str = ""
    text: str = ""

    # routing
    ingest: IngestResult | None = None
    bert: BertVerdict | None = None
    handoff: Handoff | None = None
    sort: SortResult | None = None
    extract: ExtractResult | None = None
    verdict: JudgeVerdict | None = None
    arbiter: ArbiterDecision | None = None
    boss: BossDecision | None = None

    # retry counters
    classify_attempts: int = 0
    extract_attempts: int = 0
    boss_reassignments: int = 0
    resorted: bool = False

    # trail and run outputs
    route_trail: list[str] = Field(default_factory=list)
    report: dict | None = None
    status: str = "processing"
    eval_mode: bool = False
    grade: JudgeGrade | None = None
    usage_total: Usage = Field(default_factory=Usage)
    #: Spend per agent role (taxonomy names; ``pdf_transcriber`` for vision, ``grader`` for eval
    #: grading). ``usage_total`` is the pipeline sum and excludes ``grader``.
    usage_by_role: dict[str, Usage] = Field(default_factory=dict)
    #: LLM-calling nodes that raised mid-call, so part of their spend may be unrecorded.
    usage_partial_nodes: list[str] = Field(default_factory=list)
