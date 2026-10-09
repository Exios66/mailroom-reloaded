"""Span attributes, decision events and scores for the pipeline (replay capture).

``MailroomFlow`` calls these helpers next to its existing audit / metric writes, so
the flow stays a few lines per node. Every helper is best effort and never raises:
tracing must not change a document's outcome. Attribute names follow
``obs/attrs.py``; nothing here records document text (summaries carry identifiers,
enums and numbers only).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import structlog
from opentelemetry import trace

from mailroom_reloaded import __version__
from mailroom_reloaded.llm.client import cost_for
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.obs import attrs as A
from mailroom_reloaded.obs.metrics import M
from mailroom_reloaded.obs.reconsideration import (
    collect_review_causes,
    should_reconsider,
)
from mailroom_reloaded.obs.scores import emit_score
from mailroom_reloaded.settings import load_taxonomy

__all__ = [
    "emit_event",
    "grade_scores",
    "node_end",
    "node_start",
    "pipeline_cost",
    "root_end",
    "root_start",
    "stage_after",
]

logger = structlog.get_logger(__name__)

#: ``Bins.claim`` / ``Bins.move`` prefix the file name with ``<uuid4 hex>_``.
_CLAIM_PREFIX = re.compile(r"^(?:[0-9a-f]{32}_)+")
_NODE_STAGE = {
    "ingest": "intake",
    "bert_primary": "intake",
    "sort": "classify",
    "extract": "extract",
    "verify": "judge_verify",
    "boss": "boss",
    "grade": "grade",
    "report_catalog_archive": "archive",
}
_RETRY_STAGE = {"sort": "retry_classify", "extract": "retry_extract"}


def _safe(fn):
    """Run a capture helper, swallowing and logging any failure."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            if (
                "pytest" in sys.modules
            ):  # surface capture bugs in tests, swallow in production
                raise
            logger.debug("trace_capture_failed", helper=fn.__name__, exc_info=True)
            return None

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@_safe
def emit_event(event: str, /, **attrs: Any) -> None:
    """Add the span event ``mailroom.<event>`` (``None`` values are dropped) to the current span."""
    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(
            f"mailroom.{event}", {k: v for k, v in attrs.items() if v is not None}
        )


def stage_after(node: str, state: Any, retry: bool = False) -> str:
    """The document's stage after ``node`` (The-Mailroom's vocabulary)."""
    if state.status == "failed":
        return "failed"
    if state.status == "parked":
        return "review"
    if retry and node in _RETRY_STAGE:
        return _RETRY_STAGE[node]
    return _NODE_STAGE.get(node, node)


# --------------------------------------------------------------------------- root span
@_safe
def root_start(span: Any, flow: Any) -> None:
    """Section A attributes known when the document starts."""
    state, manifest = flow.state, flow._manifest
    filename = _CLAIM_PREFIX.sub(
        "", Path(state.path).name if state.path else manifest.filename
    )
    span.set_attribute(A.SPAN_KIND, "CHAIN")
    span.set_attribute(A.DOC_ID, state.doc_id)
    span.set_attribute(A.FILENAME, filename[:256])
    span.set_attribute("mailroom.release", f"mailroom@{__version__}")
    span.set_attribute("mailroom.worker_id", getattr(flow, "_worker_id", "") or "")
    span.set_attribute(
        "mailroom.prompt_set", str(flow._overrides.get("prompt_set") or "frozen_v1")
    )
    resumed = bool(manifest.completed_nodes)
    span.set_attribute("mailroom.resumed", resumed)
    if getattr(flow, "_resume_from", None):
        span.set_attribute("mailroom.resume_from", flow._resume_from)
    span.set_attribute(
        "input.value", json.dumps({"filename": filename[:256], "doc_id": state.doc_id})
    )
    gt = _ground_truth(flow)
    if gt:
        span.set_attribute(
            "mailroom.gt.expected_doc_class", str(gt.get("expected") or "")
        )
        span.set_attribute(
            "mailroom.gt.expected_subclass", str(gt.get("expected_subclass") or "")
        )
        span.set_attribute(
            "mailroom.gt.expected_stage", str(gt.get("expected_stage") or "")
        )


def _ground_truth(flow: Any) -> dict[str, Any]:
    if getattr(flow, "_eval_ctx", None) is None:
        return {}
    return flow._ground_truth_fn()(flow.state.doc_id) or {}


def _failure_class(flow: Any, error: BaseException | None) -> str | None:
    if error is not None:
        return A.failure_class_for(error)
    reason = getattr(flow, "_failure_reason", None)
    return A.failure_class_for(reason) if reason else None


@_safe
def root_end(
    span: Any, flow: Any, error: BaseException | None, elapsed_s: float
) -> None:
    """Flat output attributes, the output summary and the every-run / reconsideration scores."""
    state = flow.state
    doc_type = flow._effective_doc_type()
    subclass = flow._effective_subclass()
    aborted = error is not None
    completed = state.status == "archived"
    failure_class = _failure_class(flow, error)
    if aborted:
        stage = "aborted"
    elif completed:
        stage = "archive"
    else:
        stage = stage_after("", state)
    span.set_attribute(A.STAGE, stage)
    span.set_attribute(A.STATUS, "aborted" if aborted else state.status)
    span.set_attribute("mailroom.doc_type", doc_type)
    if subclass:
        span.set_attribute("mailroom.doc_subclass", subclass)
    span.set_attribute("mailroom.route_trail", json.dumps(state.route_trail))
    if failure_class:
        span.set_attribute(A.FAILURE_CLASS, failure_class)
    usage = state.usage_total
    cost = pipeline_cost(state)
    span.set_attribute("mailroom.usage.calls", usage.calls)
    span.set_attribute("mailroom.usage.prompt_tokens", usage.prompt_tokens)
    span.set_attribute("mailroom.usage.completion_tokens", usage.completion_tokens)
    span.set_attribute("mailroom.usage.total_tokens", usage.total_tokens)
    span.set_attribute("mailroom.usage.cost_usd", round(cost, 8))
    span.set_attribute("mailroom.usage.by_role", json.dumps(_by_role(state)))
    sort_conf = state.sort.confidence if state.sort is not None else None
    ext_conf = state.extract.confidence if state.extract is not None else None
    span.set_attribute(
        "output.value",
        json.dumps(
            {
                "stage": stage,
                "status": "aborted" if aborted else state.status,
                "doc_type": doc_type,
                "doc_subclass": subclass,
                "classification_confidence": sort_conf,
                "extraction_confidence": ext_conf,
                "run_aborted": aborted,
                "failure_class": failure_class,
            }
        ),
    )
    _every_run_scores(
        span, flow, aborted, completed, elapsed_s, cost, sort_conf, ext_conf
    )
    _reconsideration(span, flow, doc_type, subclass, aborted)


def _by_role(state: Any) -> dict[str, dict[str, float]]:
    return {
        role: {
            "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens,
            "calls": u.calls,
            "cost_usd": round(cost_for(role, u), 8),
        }
        for role, u in state.usage_by_role.items()
    }


def pipeline_cost(state: Any) -> float:
    return sum(
        cost_for(role, u) for role, u in state.usage_by_role.items() if role != "grader"
    )


def _every_run_scores(
    span: Any,
    flow: Any,
    aborted: bool,
    completed: bool,
    elapsed_s: float,
    cost: float,
    sort_conf: float | None,
    ext_conf: float | None,
) -> None:
    state = flow.state
    extract = state.extract
    retried = (
        state.classify_attempts > 0 or state.extract_attempts > 0 or state.resorted
    )
    escalated = "boss" in state.route_trail
    emit_score(span, "parse_error", bool(extract is not None and extract.parse_error))
    emit_score(span, "schema_valid", bool(extract is not None and extract.schema_valid))
    emit_score(span, "stage_completed", completed)
    emit_score(span, "guardrail_triggered", state.status == "failed" and not aborted)
    emit_score(
        span,
        "success_rate",
        1.0 if completed and not retried and not escalated else 0.0,
    )
    emit_score(span, "run_aborted", aborted)
    if sort_conf is not None:
        emit_score(span, "classification_confidence", sort_conf)
    if ext_conf is not None:
        emit_score(span, "extraction_confidence", ext_conf)
    emit_score(span, "run_duration_seconds", round(elapsed_s, 4))
    emit_score(span, "total_tokens", state.usage_total.total_tokens)
    emit_score(span, "estimated_cost_usd", round(cost, 8))
    emit_score(span, "llm_call_count", state.usage_total.calls)
    emit_score(span, "classification_attempts", state.classify_attempts + 1)
    emit_score(span, "extraction_attempts", state.extract_attempts + 1)


def _ground_truth_scores(
    span: Any, flow: Any, doc_type: str, aborted: bool
) -> dict[str, Any]:
    """class_correct / stage_correct against the eval labels; returns them for the causes."""
    gt = _ground_truth(flow)
    out: dict[str, Any] = {}
    if not gt:
        return out
    expected = (gt.get("expected") or "").strip().lower()
    if expected:
        out["class_correct"] = expected == doc_type.strip().lower()
        emit_score(span, "class_correct", out["class_correct"])
    expected_stage = (gt.get("expected_stage") or "").strip().lower()
    if expected_stage and not aborted:
        final = {"archived": "archived", "parked": "review", "failed": "failed"}.get(
            flow.state.status
        )
        out["stage_correct"] = final == expected_stage or (
            final == "archived" and expected_stage == "archive"
        )
        emit_score(span, "stage_correct", out["stage_correct"])
    return out


def _reconsideration(
    span: Any, flow: Any, doc_type: str, subclass: str | None, aborted: bool
) -> None:
    state = flow.state
    extract = state.extract
    gt = _ground_truth(flow)
    scores: dict[str, Any] = {
        "schema_valid": bool(extract is not None and extract.schema_valid),
        "parse_error": bool(extract is not None and extract.parse_error),
        **_ground_truth_scores(span, flow, doc_type, aborted),
    }
    grade = getattr(state, "grade", None)
    if grade is not None:
        scores["extraction_overall_score"] = getattr(flow, "_extraction_overall", None)
    verdict = None
    if state.verdict is not None:
        verdict = {
            "complete": "CORRECT",
            "partial": "PARTIAL",
            "incomplete": "MISS",
        }.get(state.verdict.label)
        scores["completeness"] = state.verdict.score
    try:
        floor = float(load_taxonomy().confidence_for(doc_type).low)
    except Exception:  # noqa: BLE001
        floor = 0.88
    causes = collect_review_causes(
        doc_type=doc_type,
        doc_subclass=subclass,
        expected_class=gt.get("expected"),
        expected_subclass=gt.get("expected_subclass"),
        scores=scores,
        verdict=verdict,
        floor=floor,
    )
    for cause in causes:
        M.review_causes.add(1, {"cause": cause})
    emit_score(span, "review_causes", causes)
    emit_score(
        span,
        "needs_reconsideration",
        (not aborted)
        and should_reconsider(
            state.status if state.status != "archived" else "archived", causes
        ),
    )


# --------------------------------------------------------------------------- node span
@_safe
def node_start(
    span: Any, flow: Any, node: str, deadline_s: float, token_budget: int
) -> None:
    """Section B attributes known when a node starts."""
    state = flow.state
    station = A.station_for(node)
    span.set_attribute(A.SPAN_KIND, A.SPAN_KIND_FOR_NODE.get(node, "SPAN"))
    span.set_attribute(A.NODE, node)
    span.set_attribute(A.STATION, station.id)
    span.set_attribute(A.PHASE, station.phase)
    span.set_attribute(A.DOC_ID, state.doc_id)
    attempt = {
        "sort": state.classify_attempts + 1,
        "extract": state.extract_attempts + 1,
    }.get(node, 1)
    span.set_attribute(A.ATTEMPT, attempt)
    retry_kind = getattr(flow, "_retry_kind", None)
    flow._node_retry = bool(retry_kind) and node in {"sort", "extract"}
    if flow._node_retry:
        span.set_attribute(A.RETRY_KIND, retry_kind)
        flow._retry_kind = None
    span.set_attribute("mailroom.deadline_s", float(deadline_s or 0))
    span.set_attribute("mailroom.token_budget", int(token_budget or 0))


@_safe
def node_end(
    span: Any,
    flow: Any,
    node: str,
    before: Usage,
    before_cost: float,
    error: BaseException | None = None,
    fail_reason: str | None = None,
) -> None:
    """Usage, stage and (on failure) reason attributes once a node has run."""
    state = flow.state
    used = state.usage_total - before
    span.set_attribute("mailroom.tokens.used", used.total_tokens)
    span.set_attribute("mailroom.llm_calls", used.calls)
    span.set_attribute(
        "mailroom.cost_usd", round(max(0.0, pipeline_cost(state) - before_cost), 8)
    )
    stage = stage_after(node, state, retry=bool(getattr(flow, "_node_retry", False)))
    span.set_attribute(A.STAGE, stage)
    _node_extras(span, flow, node)
    if error is not None or fail_reason:
        reason = fail_reason or type(error).__name__
        span.set_attribute(A.FAIL_REASON, reason[:128])
        span.set_attribute(
            A.FAILURE_CLASS, A.failure_class_for(fail_reason if fail_reason else error)
        )
    summary = {
        "doc_id": state.doc_id,
        "doc_type": flow._effective_doc_type(),
        "stage": stage,
        "status": state.status,
    }
    span.set_attribute("output.value", json.dumps(summary))


def _node_extras(span: Any, flow: Any, node: str) -> None:
    state = flow.state
    if node == "ingest" and state.ingest is not None:
        ing = state.ingest
        span.set_attribute("mailroom.intake.method", ing.method)
        span.set_attribute("mailroom.intake.pages", ing.pages)
        span.set_attribute("mailroom.intake.chars", int(ing.stats.get("chars", 0) or 0))
        span.set_attribute(
            "mailroom.intake.raw_chars", int(ing.stats.get("raw_chars", 0) or 0)
        )
        if ing.method == "vision":
            span.set_attribute("mailroom.vision.pages", ing.pages)
    elif node == "bert_primary" and state.bert is not None:
        b = state.bert
        span.set_attribute("mailroom.bert.available", bool(b.available))
        span.set_attribute("mailroom.bert.route", str(b.route))
        if b.doc_type:
            span.set_attribute("mailroom.bert.class", str(b.doc_type))
        if b.calibrated_confidence is not None:
            span.set_attribute(
                "mailroom.bert.confidence", float(b.calibrated_confidence)
            )
    elif node == "sort" and state.sort is not None:
        s = state.sort
        span.set_attribute("mailroom.sort.doc_type", str(s.doc_type or ""))
        span.set_attribute("mailroom.sort.doc_subclass", str(s.doc_subclass or ""))
        span.set_attribute("mailroom.sort.confidence", float(s.confidence))
    elif node == "extract" and state.extract is not None:
        e = state.extract
        span.set_attribute("mailroom.extract.doc_type", e.doc_type)
        span.set_attribute("mailroom.extract.schema_valid", bool(e.schema_valid))
        if e.confidence is not None:
            span.set_attribute("mailroom.extract.confidence", float(e.confidence))
        data = e.data or {}
        span.set_attribute("mailroom.extract.n_fields", len(data))
        span.set_attribute(
            "mailroom.extract.n_empty",
            sum(1 for v in data.values() if v in (None, "", [], {})),
        )
        span.set_attribute(
            "mailroom.extract.length_capped", e.error_kind == "LengthFinishReasonError"
        )
    elif node == "verify":
        if state.verdict is not None:
            span.set_attribute("mailroom.judge.label", state.verdict.label)
            span.set_attribute("mailroom.judge.score", float(state.verdict.score))
        if state.arbiter is not None:
            span.set_attribute("mailroom.arbiter.decision", state.arbiter.action)
    elif node == "boss" and state.boss is not None:
        span.set_attribute("mailroom.boss.decision", state.boss.action)


@_safe
def grade_scores(span: Any, flow: Any) -> None:
    """Grounded extraction scores on the ``grade`` span (eval only): overall, per field, P/R/F1."""
    from mailroom_reloaded.scoring import score_extraction
    from mailroom_reloaded.scoring.extraction_metrics import extraction_binary_metrics

    state = flow.state
    if state.grade is not None:
        emit_score(span, "judge_overall", float(state.grade.overall))
    gt = _ground_truth(flow)
    expected = gt.get("fields") or {}
    extract = state.extract
    if not expected or extract is None or extract.data is None:
        return
    doc_type = flow._effective_doc_type()
    classes = load_taxonomy().classes
    types = dict(classes[doc_type].field_types) if doc_type in classes else {}
    result = score_extraction(doc_type, types, extract.data, expected)
    if result.overall_score is not None:
        flow._extraction_overall = result.overall_score
        emit_score(span, "extraction_overall_score", float(result.overall_score))
    for name, value in result.field_scores.items():
        emit_score(span, f"extraction_field_score.{name}", float(value), strict=False)
    emit_score(span, "extraction_needs_judge_review", result.needs_judge_review)
    emit_score(span, "extraction_ambiguous_fields", list(result.ambiguous_fields)[:64])
    metrics = extraction_binary_metrics(
        expected, extract.data, field_types=types, doc_class=doc_type, result=result
    )
    for name in (
        "extraction_precision",
        "extraction_recall",
        "extraction_f1",
        "extraction_f2",
        "tp",
        "fp",
        "fn",
    ):
        if isinstance(metrics.get(name), (int, float)):
            emit_score(span, name, float(metrics[name]))
