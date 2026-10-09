"""Objective review causes (port of The-Mailroom ``reconsideration.collect_review_causes``).

Self-reported confidences can be overconfident, so these causes derive only from
grounded scores, ground-truth labels, judge verdicts and schema / guardrail flags,
never from a stated confidence alone. Tokens match The-Mailroom's vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["CAUSES", "collect_review_causes", "should_reconsider"]

CLASS_MISS = "class_miss"
SUBCLASS_MISS = "subclass_miss"
EXTRACTION_MISS = "extraction_miss"
JUDGE_MISS = "judge_miss"
JUDGE_PARTIAL = "judge_partial"
SCHEMA_INVALID = "schema_invalid"
GUARDRAIL = "guardrail"
PARSE_ERROR = "parse_error"
REPORTING_INCOMPLETE = "reporting_incomplete"
NEEDS_JUDGE = "needs_judge_review"

CAUSES: tuple[str, ...] = (
    CLASS_MISS,
    SUBCLASS_MISS,
    EXTRACTION_MISS,
    JUDGE_MISS,
    JUDGE_PARTIAL,
    SCHEMA_INVALID,
    GUARDRAIL,
    PARSE_ERROR,
    REPORTING_INCOMPLETE,
    NEEDS_JUDGE,
)

_FALSE = {"false", "no", "off", "0"}
_TRUE = {"true", "yes", "on", "1"}
_DONE_STAGES = frozenset({"archived", "archive", "catalog", "report", "compile_report"})


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_false(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in _FALSE
    return value is False or value == 0


def _is_true(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    return value is True or value == 1


def collect_review_causes(
    *,
    doc_type: str | None = None,
    doc_subclass: str | None = None,
    expected_class: str | None = None,
    expected_subclass: str | None = None,
    scores: Mapping[str, Any] | None = None,
    verdict: str | None = None,
    floor: float = 0.88,
) -> list[str]:
    """Canonical cause tokens in stable order; empty when no objective miss is present.

    ``verdict`` is the judge verdict token (``CORRECT`` / ``PARTIAL`` / ``MISS``);
    ``floor`` is the class's low-confidence threshold.
    """
    scores = scores or {}
    causes: list[str] = []

    expected = (expected_class or "").strip().lower() or None
    predicted = (doc_type or "").strip().lower() or None
    if expected and predicted and expected != predicted:
        causes.append(CLASS_MISS)
    if _is_false(scores.get("class_correct")) and CLASS_MISS not in causes:
        causes.append(CLASS_MISS)

    exp_sub = (expected_subclass or "").strip().lower() or None
    pred_sub = (doc_subclass or "").strip().lower() or None
    if exp_sub and pred_sub and exp_sub != pred_sub:
        causes.append(SUBCLASS_MISS)

    overall = _as_float(scores.get("extraction_overall_score"))
    if overall is not None and overall < floor:
        causes.append(EXTRACTION_MISS)
    presence = _as_float(scores.get("expected_field_presence"))
    if presence is not None and presence < floor and EXTRACTION_MISS not in causes:
        causes.append(EXTRACTION_MISS)

    token = (verdict or "").strip().upper()
    if token == "MISS":
        causes.append(JUDGE_MISS)
    elif token == "PARTIAL":
        causes.append(JUDGE_PARTIAL)

    if _is_false(scores.get("schema_valid")):
        causes.append(SCHEMA_INVALID)
    if _is_true(scores.get("guardrail_triggered")):
        causes.append(GUARDRAIL)
    if _is_true(scores.get("parse_error")):
        causes.append(PARSE_ERROR)

    completeness = _as_float(scores.get("completeness"))
    label = str(scores.get("completeness_label") or "").strip().upper()
    if (completeness is not None and completeness < floor) or label in {
        "LOW",
        "INCOMPLETE",
    }:
        causes.append(REPORTING_INCOMPLETE)

    if _is_true(scores.get("extraction_needs_judge_review")):
        causes.append(NEEDS_JUDGE)

    return list(dict.fromkeys(causes))


def should_reconsider(stage: str | None, causes: Iterable[str]) -> bool:
    """True when a run looks finished but objective misses remain."""
    return bool(list(causes)) and (stage or "").strip().lower() in _DONE_STAGES
