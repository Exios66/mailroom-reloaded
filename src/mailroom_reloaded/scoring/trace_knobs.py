"""Confidence and reasoning as captured experimental knobs.

These fields are model-emitted traces. They are **never** scored into
``overall_score`` / field-micro F1 (see :data:`NEVER_SCORED_FIELDS`).
This module peels them off the prediction, stores a stable payload, and
applies adjustable gates so a run can sweep ``confidence_min``,
``confidence_band``, and whether reasoning entries route CUAD presence.
"""

from __future__ import annotations

from typing import Any, Mapping

from .config import TraceKnobSettings, get_settings

__all__ = [
    "TRACE_KNOB_KEYS",
    "CONFIDENCE_GATE_VALUES",
    "MISSING_CONFIDENCE_VALUES",
    "empty_trace_payload",
    "parse_confidence",
    "parse_reasoning",
    "confidence_gate",
    "confidence_calibration_error",
    "capture_trace_knobs",
    "get_trace_knobs",
]

TRACE_KNOB_KEYS: tuple[str, ...] = (
    "confidence",
    "reasoning",
    "confidence_gate",
    "calibration_error",
    "n_reasoning_entries",
)

CONFIDENCE_GATE_VALUES: frozenset[str] = frozenset(
    {"pass", "below_min", "in_band", "missing"}
)
MISSING_CONFIDENCE_VALUES: frozenset[str] = frozenset(
    {"absent", "assume_1", "assume_0"}
)


def get_trace_knobs() -> TraceKnobSettings:
    """The process-wide ``trace_knobs`` settings."""
    return get_settings().trace_knobs


def empty_trace_payload() -> dict[str, Any]:
    """Stable shape when capture is off or the model omitted the knobs."""
    return {
        "confidence": None,
        "reasoning": None,
        "confidence_gate": None,
        "calibration_error": None,
        "n_reasoning_entries": None,
    }


def parse_confidence(value: Any) -> float | None:
    """Coerce a model-emitted confidence to ``[0, 1]``, or ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Mapping):
        for key in ("confidence", "score", "value"):
            if key in value:
                return parse_confidence(value.get(key))
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:  # NaN
        return None
    if num > 1.0 and num <= 100.0:
        num = num / 100.0
    if num < 0.0 or num > 1.0:
        return None
    return round(num, 4)


def parse_reasoning(value: Any) -> dict[str, Any] | None:
    """Normalize reasoning to ``{summary, entries}``.

    Accepts a string, a list of entries, or the specialist object
    ``{summary, entries: [{field, evidence, section_ref}, ...]}``.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return {"summary": text, "entries": [], "text": text}
    if isinstance(value, (list, tuple)):
        entries = [_normalize_entry(item) for item in value]
        entries = [item for item in entries if item]
        if not entries:
            return None
        return {"summary": None, "entries": entries}
    if isinstance(value, Mapping):
        summary = value.get("summary")
        if summary is not None:
            summary = str(summary).strip() or None
        raw_entries = value.get("entries")
        entries: list[dict[str, Any]] = []
        if isinstance(raw_entries, (list, tuple)):
            entries = [_normalize_entry(item) for item in raw_entries]
            entries = [item for item in entries if item]
        text = value.get("text")
        if not summary and not entries and not text:
            # Bare dict that is itself one entry.
            entry = _normalize_entry(value)
            if entry and ("field" in value or "evidence" in value):
                return {"summary": None, "entries": [entry]}
            return None
        out: dict[str, Any] = {"summary": summary, "entries": entries}
        if text:
            out["text"] = str(text)
        return out
    text = str(value).strip()
    if not text:
        return None
    return {"summary": text, "entries": [], "text": text}


def _normalize_entry(item: Any) -> dict[str, Any] | None:
    """Coerce one reasoning entry to ``{field, evidence, section_ref}``, or ``None``."""
    if item is None or item == "":
        return None
    if isinstance(item, str):
        text = item.strip()
        return {"field": None, "evidence": text, "section_ref": None} if text else None
    if not isinstance(item, Mapping):
        return None
    field = item.get("field")
    evidence = item.get("evidence") or item.get("span") or item.get("text")
    section = item.get("section_ref") or item.get("section") or item.get("ref")
    if field is None and evidence is None and section is None:
        return None
    return {
        "field": None if field is None else str(field).strip() or None,
        "evidence": None if evidence is None else str(evidence),
        "section_ref": None if section is None else str(section).strip() or None,
    }


def _fill_missing_confidence(
    confidence: float | None,
    *,
    mode: str,
) -> float | None:
    """Fill a missing confidence per ``mode`` (``absent`` / ``assume_1`` / ``assume_0``)."""
    if confidence is not None:
        return confidence
    if mode == "assume_1":
        return 1.0
    if mode == "assume_0":
        return 0.0
    return None


def confidence_gate(
    confidence: float | None,
    *,
    knobs: TraceKnobSettings | None = None,
) -> str:
    """``pass`` / ``below_min`` / ``in_band`` / ``missing``."""
    settings = knobs or get_trace_knobs()
    filled = _fill_missing_confidence(
        confidence, mode=str(settings.missing_confidence or "absent")
    )
    if filled is None:
        return "missing"
    if settings.confidence_min is not None and filled < float(settings.confidence_min):
        return "below_min"
    band = settings.confidence_band
    if band is not None and len(band) == 2:
        low, high = float(band[0]), float(band[1])
        if low <= filled < high:
            return "in_band"
    return "pass"


def confidence_calibration_error(
    confidence: float | None,
    correctness: float | None,
) -> float | None:
    """``|confidence - correctness|`` when both are in ``[0, 1]``."""
    if confidence is None or correctness is None:
        return None
    try:
        conf = float(confidence)
        correct = float(correctness)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= conf <= 1.0 and 0.0 <= correct <= 1.0):
        return None
    return round(abs(conf - correct), 4)


def capture_trace_knobs(
    predicted: Mapping[str, Any] | None,
    *,
    expected: Mapping[str, Any] | None = None,
    correctness: float | None = None,
    knobs: TraceKnobSettings | None = None,
) -> dict[str, Any]:
    """Peel ``confidence`` / ``reasoning`` off a prediction for experiment logs.

    ``expected`` is accepted for callers that have a paired GT dict; those
    keys are never compared as extraction fields. ``correctness`` (typically
    ``overall_score``) drives calibration error when enabled.
    """
    settings = knobs or get_trace_knobs()
    src = dict(predicted or {})
    # Never copy knobs from GT even if the caller passed ``expected``.
    _ = expected
    payload = empty_trace_payload()
    if settings.capture_confidence:
        payload["confidence"] = parse_confidence(src.get("confidence"))
    if settings.capture_reasoning:
        payload["reasoning"] = parse_reasoning(src.get("reasoning"))
        entries = (payload["reasoning"] or {}).get("entries") or []
        payload["n_reasoning_entries"] = (
            len(entries) if payload["reasoning"] is not None else None
        )
    if settings.capture_confidence or settings.capture_reasoning:
        payload["confidence_gate"] = confidence_gate(
            payload["confidence"], knobs=settings
        )
    if settings.compute_calibration_error:
        payload["calibration_error"] = confidence_calibration_error(
            payload["confidence"], correctness
        )
    return payload
