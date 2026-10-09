"""Score registry and emission: scores live on the span they describe.

``SCORE_SPECS`` is a slim port of llm-mailroom's ``SCORE_CONFIGS`` and
llm-dojo-scoring's ``registry.py`` (units, rollups and display tiers). A score is
written as the span attribute ``mailroom.score.<name>`` plus a ``mailroom.score``
span event (so the replay can show it at the moment it existed). Tiers decide what
the viewer draws by default; nothing is dropped at capture time.

Tier: 0 headline, 1 core, 2 deep, 3 log-only (llm-dojo-scoring
``DEFAULT_DASHBOARD_TIER`` is 1).
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from typing import Any

import structlog
from opentelemetry.trace import Span

__all__ = [
    "SCORE_PREFIX",
    "SCORE_SPECS",
    "ScoreSpec",
    "emit_score",
    "spec_for",
]

logger = structlog.get_logger(__name__)

SCORE_PREFIX = "mailroom.score."
_MAX_STR = 256


@dataclass(frozen=True)
class ScoreSpec:
    """One score name's contract."""

    name: str
    data_type: str  # numeric | boolean | categorical | json
    unit: str = ""
    rollup: str = "mean"  # mean | sum | none
    scope: str = "root"  # root | node:<name> | any
    tier: int = 1
    group: str = "run"


def _s(*args: Any, **kw: Any) -> tuple[str, ScoreSpec]:
    spec = ScoreSpec(*args, **kw)
    return spec.name, spec


#: Fixed score names. ``extraction_field_score.<field>`` is handled by :func:`spec_for`.
SCORE_SPECS: dict[str, ScoreSpec] = dict(
    [
        # every run
        _s("parse_error", "boolean", rollup="mean", tier=1, group="run"),
        _s("schema_valid", "boolean", rollup="mean", tier=0, group="run"),
        _s("stage_completed", "boolean", rollup="mean", tier=0, group="run"),
        _s("guardrail_triggered", "boolean", rollup="mean", tier=1, group="run"),
        _s("success_rate", "numeric", "ratio", "mean", tier=0, group="run"),
        _s("run_aborted", "boolean", rollup="mean", tier=1, group="run"),
        _s(
            "classification_confidence", "numeric", "ratio", "mean", tier=1, group="run"
        ),
        _s("extraction_confidence", "numeric", "ratio", "mean", tier=1, group="run"),
        _s("run_duration_seconds", "numeric", "s", "mean", tier=0, group="run"),
        _s("total_tokens", "numeric", "{token}", "sum", tier=1, group="run"),
        _s("estimated_cost_usd", "numeric", "USD", "sum", tier=0, group="run"),
        _s("llm_call_count", "numeric", "{call}", "sum", tier=1, group="run"),
        _s(
            "classification_attempts",
            "numeric",
            "{attempt}",
            "mean",
            tier=1,
            group="run",
        ),
        _s("extraction_attempts", "numeric", "{attempt}", "mean", tier=1, group="run"),
        # reconsideration
        _s("review_causes", "json", rollup="none", tier=0, group="reconsideration"),
        _s(
            "needs_reconsideration",
            "boolean",
            rollup="mean",
            tier=0,
            group="reconsideration",
        ),
        # ground truth (eval)
        _s("class_correct", "boolean", rollup="mean", tier=0, group="ground_truth"),
        _s("stage_correct", "boolean", rollup="mean", tier=1, group="ground_truth"),
        _s(
            "expected_field_presence",
            "numeric",
            "ratio",
            "mean",
            tier=1,
            group="ground_truth",
        ),
        # judge (grading)
        _s(
            "judge_overall",
            "numeric",
            "ratio",
            "mean",
            "node:grade",
            tier=0,
            group="judge",
        ),
        # grounded extraction
        _s(
            "extraction_overall_score",
            "numeric",
            "ratio",
            "mean",
            "node:grade",
            tier=0,
            group="extraction",
        ),
        _s(
            "extraction_needs_judge_review",
            "boolean",
            rollup="mean",
            scope="node:grade",
            tier=2,
            group="extraction",
        ),
        _s(
            "extraction_ambiguous_fields",
            "json",
            rollup="none",
            scope="node:grade",
            tier=2,
            group="extraction",
        ),
        _s(
            "extraction_precision",
            "numeric",
            "ratio",
            "mean",
            "node:grade",
            tier=1,
            group="binary",
        ),
        _s(
            "extraction_recall",
            "numeric",
            "ratio",
            "mean",
            "node:grade",
            tier=1,
            group="binary",
        ),
        _s(
            "extraction_f1",
            "numeric",
            "ratio",
            "mean",
            "node:grade",
            tier=0,
            group="binary",
        ),
        _s(
            "extraction_f2",
            "numeric",
            "ratio",
            "mean",
            "node:grade",
            tier=2,
            group="binary",
        ),
        _s("tp", "numeric", "{field}", "sum", "node:grade", tier=2, group="binary"),
        _s("fp", "numeric", "{field}", "sum", "node:grade", tier=2, group="binary"),
        _s("fn", "numeric", "{field}", "sum", "node:grade", tier=2, group="binary"),
    ]
)

_FIELD_PREFIX = "extraction_field_score."
_FIELD_SPEC = ScoreSpec(
    "extraction_field_score.*",
    "numeric",
    "ratio",
    "mean",
    "node:grade",
    tier=2,
    group="fields",
)


def spec_for(name: str) -> ScoreSpec | None:
    """The spec of ``name``, or ``None`` for an unregistered score."""
    if name in SCORE_SPECS:
        return SCORE_SPECS[name]
    if name.startswith(_FIELD_PREFIX) and 0 < len(name) - len(_FIELD_PREFIX) <= 80:
        return _FIELD_SPEC
    return None


def _coerce(spec: ScoreSpec, value: Any) -> Any:
    """``value`` as the span-attribute form for ``spec`` (raises ``ValueError`` on a mismatch)."""
    if spec.data_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ValueError("expected a boolean")
    if spec.data_type == "numeric":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected a number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("expected a finite number")
        return number
    if spec.data_type == "json":
        return json.dumps(value, sort_keys=True, default=str)[: _MAX_STR * 4]
    return str(value)[:_MAX_STR]


def emit_score(
    span: Span, name: str, value: Any, *, strict: bool | None = None
) -> bool:
    """Record score ``name`` on ``span``. Returns whether it was written.

    An unknown name or a wrongly typed value raises ``ValueError`` when ``strict``
    (default: under pytest) and is logged and skipped otherwise.
    """
    strict = ("pytest" in sys.modules) if strict is None else strict
    spec = spec_for(name)
    try:
        if spec is None:
            raise ValueError(f"unknown score name: {name!r}")
        coerced = _coerce(spec, value)
    except ValueError:
        if strict:
            raise
        logger.warning("score_rejected", score=name)
        return False
    span.set_attribute(SCORE_PREFIX + name, coerced)
    span.add_event("mailroom.score", {"name": name, "value": coerced})
    return True
