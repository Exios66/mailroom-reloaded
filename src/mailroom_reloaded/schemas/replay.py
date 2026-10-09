"""``replay/v1`` timeline models.

The wire contract between the timeline builder (``obs/replay``), the
``/v1/replay/*`` routes and the TUI viewer. Times are seconds relative to
``session.t0_iso``. The format is event-sourced: the client finds ``stateAt(t)``
by binary search over ``segments``/``events``/``scores`` sorted by time.

Nothing here carries document text, prompts or completions. Free-text fields
(``reason``, ``filename``) are bounded; the viewer renders every string with
``textContent`` only.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "VERSION",
    "Entity",
    "Generation",
    "ReplayEvent",
    "ReplayScore",
    "Rollups",
    "Segment",
    "Session",
    "SessionKind",
    "SessionSummary",
    "StationInfo",
    "StationStats",
    "Timeline",
    "Totals",
    "Window",
]

VERSION = "replay/v1"
SessionKind = Literal["run", "session", "doc", "window"]
MAX_REASON = 256


class Window(BaseModel):
    """The slice of the session this payload covers."""

    from_s: float = 0.0
    to_s: float = 0.0
    complete: bool = True


class Session(BaseModel):
    """Identity and provenance of one replayable session."""

    id: str
    kind: SessionKind = "run"
    environment: str = "live"
    t0_iso: str = ""
    duration_s: float = 0.0
    #: ``spans`` is exact; ``audit`` is the approximate fallback (``approx`` true).
    source: Literal["spans", "audit"] = "spans"
    approx: bool = False
    window: Window = Field(default_factory=Window)
    links: dict[str, str] = Field(default_factory=dict)
    #: Retention removed this run's spans; only the archive ledger (and audit) remains.
    data_pruned: bool = False


class StationInfo(BaseModel):
    """One stop on the track."""

    id: str
    label: str
    phase: str
    order: int
    kind: Literal["main", "detour", "bay"] = "main"
    color_token: str = ""


class Totals(BaseModel):
    """Per-entity usage totals."""

    tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    duration_s: float = 0.0


class Entity(BaseModel):
    """One document moving through the pipeline."""

    doc_id: str
    filename: str = ""
    trace_id: str | None = None
    session_id: str | None = None
    doc_type: str | None = None
    doc_subclass: str | None = None
    expected_doc_class: str | None = None
    expected_subclass: str | None = None
    final_stage: str | None = None
    final_status: str | None = None
    failure_class: str | None = None
    review_causes: list[str] = Field(default_factory=list)
    verdict: str | None = None
    quality: float | None = None
    t_start: float = 0.0
    t_end: float | None = None
    totals: Totals = Field(default_factory=Totals)


class Segment(BaseModel):
    """A document's time at one node (one attempt)."""

    doc_id: str
    node: str
    station: str
    t0: float
    t1: float
    attempt: int = 1
    retry_kind: str | None = None
    status: Literal["ok", "failed", "running"] = "ok"
    reason: str | None = Field(default=None, max_length=MAX_REASON)
    tokens: int | None = None
    cost_usd: float | None = None
    llm_calls: int | None = None
    span_id: str | None = None
    approx: bool = False


class Generation(BaseModel):
    """One LLM call (metadata only, never content)."""

    doc_id: str
    span_id: str
    parent_span_id: str | None = None
    role: str = ""
    model: str = ""
    served_model: str | None = None
    prompt_version: str | None = None
    t0: float
    t1: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    transport_attempts: int | None = None
    ttft_s: float | None = None
    length_capped: bool | None = None
    schema_valid: bool | None = None


class ReplayEvent(BaseModel):
    """A point-in-time decision or notice (gate, retry, escalation, park, ...)."""

    t: float
    doc_id: str
    kind: str
    station: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ReplayScore(BaseModel):
    """A score, visible from the end of the span it describes."""

    doc_id: str
    span_id: str | None = None
    name: str
    value: Any
    data_type: str = "numeric"
    t: float = 0.0


class StationStats(BaseModel):
    """Latency percentiles of one station."""

    p50_s: float = 0.0
    p95_s: float = 0.0
    n: int = 0


class Rollups(BaseModel):
    """Run-level metrics in The-Mailroom ``compute_metrics`` style."""

    per_station: dict[str, StationStats] = Field(default_factory=dict)
    verdict_counts: dict[str, int] = Field(default_factory=dict)
    review_causes: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    tokens: int = 0
    first_pass_rate: float | None = None
    #: ``avg_<score>`` -> mean over documents carrying that score.
    averages: dict[str, float] = Field(default_factory=dict)


class Timeline(BaseModel):
    """The full ``replay/v1`` payload."""

    version: Literal["replay/v1"] = VERSION
    session: Session
    stations: list[StationInfo] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    generations: list[Generation] = Field(default_factory=list)
    events: list[ReplayEvent] = Field(default_factory=list)
    scores: list[ReplayScore] = Field(default_factory=list)
    rollups: Rollups = Field(default_factory=Rollups)


class SessionSummary(BaseModel):
    """One row of ``GET /v1/replay/sessions``."""

    id: str
    kind: SessionKind = "run"
    environment: str = "live"
    documents: int = 0
    started_at: str = ""
    duration_s: float = 0.0
    source: Literal["spans", "audit"] = "spans"
    #: Retention removed this run's spans; only the archive ledger remains.
    data_pruned: bool = False
