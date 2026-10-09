"""Attribute vocabulary shared by spans, the ledger and the replay builder.

One place defines the ``mailroom.*`` span attribute keys, the node -> station
map (the track the replay draws), the OpenInference span kind of each node, and
the bounded failure vocabulary. Everything that is stored durably maps free text
(filenames, exception messages) onto these closed sets, so no document content
reaches the ledger or the span store through a reason string.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "FAILURE_CLASSES",
    "FAILURE_REASONS",
    "SPAN_KIND_FOR_NODE",
    "STATIONS",
    "Station",
    "failure_class_for",
    "failure_reason_for",
    "station_for",
]

# --- attribute keys (section A-B of the plan) ---------------------------------
DOC_ID = "mailroom.doc_id"
FILENAME = "mailroom.filename"
RUN_ID = "mailroom.run_id"
SESSION_ID = "session.id"
ENVIRONMENT = "mailroom.environment"
SOURCE = "mailroom.source"
NODE = "mailroom.node"
STATION = "mailroom.station"
PHASE = "mailroom.phase"
STAGE = "mailroom.stage"
STATUS = "mailroom.status"
ATTEMPT = "mailroom.attempt"
RETRY_KIND = "mailroom.retry_kind"
FAIL_REASON = "mailroom.fail_reason"
FAILURE_CLASS = "mailroom.failure_class"
SPAN_KIND = "openinference.span.kind"

__all__ += [
    "ATTEMPT",
    "DOC_ID",
    "ENVIRONMENT",
    "FAILURE_CLASS",
    "FAIL_REASON",
    "FILENAME",
    "NODE",
    "PHASE",
    "RETRY_KIND",
    "RUN_ID",
    "SESSION_ID",
    "SOURCE",
    "SPAN_KIND",
    "STAGE",
    "STATION",
    "STATUS",
]


@dataclass(frozen=True)
class Station:
    """One stop on the replay track."""

    id: str
    label: str
    phase: str
    kind: str  # main | detour | bay
    color_token: str


#: Stations in track order. The JS mirror lives in ``api/tui/replay/stations.js``.
STATIONS: tuple[Station, ...] = (
    Station("intake", "intake", "intake_sort", "main", "--term-fg-dim"),
    Station("sorter", "sorter", "intake_sort", "main", "--term-cyan"),
    Station("gate", "gate", "intake_sort", "main", "--term-phosphor"),
    Station("specialist", "specialist", "extraction", "main", "--term-amber"),
    Station("judge", "judge", "extraction", "detour", "--term-station-judge"),
    Station("boss", "boss", "extraction", "detour", "--term-red"),
    Station("review", "review", "review", "bay", "--term-station-review"),
    Station("archive", "archive", "reporting", "main", "--term-green"),
    Station("failed", "failed", "terminal", "bay", "--term-red"),
)

_STATION_BY_ID = {s.id: s for s in STATIONS}

_NODE_STATION: dict[str, str] = {
    "ingest": "intake",
    "bert_primary": "intake",
    "sort": "sorter",
    "gate_classify": "gate",
    "gate_extract": "gate",
    "extract": "specialist",
    "verify": "judge",
    "grade": "judge",
    "boss": "boss",
    "human_review": "review",
    "report_catalog_archive": "archive",
}

#: OpenInference span kind per node (llm-dojo-scoring ``NODE_OBSERVATION_TYPES``).
SPAN_KIND_FOR_NODE: dict[str, str] = {
    "ingest": "RETRIEVER",
    "bert_primary": "SPAN",
    "sort": "AGENT",
    "gate_classify": "GUARDRAIL",
    "gate_extract": "GUARDRAIL",
    "extract": "AGENT",
    "verify": "EVALUATOR",
    "grade": "EVALUATOR",
    "boss": "AGENT",
    "human_review": "SPAN",
    "report_catalog_archive": "SPAN",
}


def station_for(node: str) -> Station:
    """The station a node belongs to; an unknown node raises ``KeyError``."""
    return _STATION_BY_ID[_NODE_STATION[node]]


# --- failure vocabulary -------------------------------------------------------
#: The-Mailroom's ``failure_class`` vocabulary.
FAILURE_CLASSES: tuple[str, ...] = (
    "llm_timeout",
    "llm_auth",
    "llm_rate_limit",
    "llm_transient",
    "io_error",
    "schema_error",
    "run_budget",
    "unexpected",
)

#: Bounded ``failure_reason`` enum: the only reason strings the ledger stores.
FAILURE_REASONS: tuple[str, ...] = (
    "deadline_exceeded",
    "token_budget_exceeded",
    "ingest_failed",
    "no_text",
    "llm_error",
    "schema_invalid",
    "io_error",
    "unexpected",
)

_BUDGET_REASONS = {"deadline_exceeded", "token_budget_exceeded"}


def _status_code(exc: BaseException) -> int | None:
    """Return ``exc.status_code`` if it is an integer, otherwise ``None``."""
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def failure_class_for(failure: BaseException | str | None) -> str:
    """Map an exception or a reason string onto :data:`FAILURE_CLASSES`.

    Strings are matched only against the closed set of reasons the pipeline
    itself produces; anything else is ``unexpected``. Free text is never echoed.

    Exceptions are classified by integer status code, class name and type;
    their messages are not inspected. ``None`` and unrecognized exceptions
    map to ``unexpected``.
    """
    if failure is None:
        return "unexpected"
    if isinstance(failure, str):
        if failure in _BUDGET_REASONS:
            return "run_budget"
        if failure in {"schema_invalid", "schema_error"}:
            return "schema_error"
        if failure in {"io_error", "ingest_failed", "no_text"}:
            return "io_error"
        return "unexpected"
    code = _status_code(failure)
    name = type(failure).__name__
    if isinstance(failure, TimeoutError) or "Timeout" in name:
        return "llm_timeout"
    if code in {401, 403} or "Authentication" in name or "PermissionDenied" in name:
        return "llm_auth"
    if code == 429 or "RateLimit" in name:
        return "llm_rate_limit"
    if (code is not None and code >= 500) or "APIConnection" in name or "APIStatus" in name:
        return "llm_transient"
    if isinstance(failure, OSError):
        return "io_error"
    if "ValidationError" in name or "JSONDecode" in name or isinstance(failure, ValueError):
        return "schema_error"
    return "unexpected"


def failure_reason_for(reason: str | None) -> str:
    """Collapse a free-text failure reason onto :data:`FAILURE_REASONS`.

    Match an ``ingest`` prefix, then ``no text``/``no_text``, then ``schema``
    case-insensitively. Otherwise preserve exact vocabulary members and return
    ``unexpected`` for empty, missing or unrecognized reasons.
    """
    if reason in _BUDGET_REASONS:
        return str(reason)
    if not reason:
        return "unexpected"
    lowered = reason.lower()
    if lowered.startswith("ingest"):
        return "ingest_failed"
    if "no text" in lowered or "no_text" in lowered:
        return "no_text"
    if "schema" in lowered:
        return "schema_invalid"
    if reason in FAILURE_REASONS:
        return reason
    return "unexpected"
