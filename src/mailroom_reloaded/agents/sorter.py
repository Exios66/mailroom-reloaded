"""Sorter (spec sections 5 and 6): the FULL / SUBCLASS_ONLY standalone call.

The sorter is one ``call_structured`` call (not a CrewAI agent) so the pipeline
controls the JSON schema, the subclass enum scope and the logprob-derived
confidence. In ``SUBCLASS_ONLY`` mode the BERT-locked ``doc_type`` is frozen and
only the subclass vocabulary is in scope; the scope prompt's ``doc_type_disagree``
escape lets the sorter hand back to ``FULL`` once (``gate_classify`` -> re_sort).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mailroom_reloaded.ingest.bert import Handoff, SortMode
from mailroom_reloaded.llm.client import call_structured, resolve
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.prompts.loader import load_prompt
from mailroom_reloaded.scoring import subclass_vocab
from mailroom_reloaded.settings import get_settings, load_taxonomy
from mailroom_reloaded.tools import ToolContext, tools_for

__all__ = ["SortResult", "sort"]

_CONF_EPS = 1e-6


@dataclass(frozen=True)
class SortResult:
    """One sorter verdict, with raw and calibrated confidence."""

    doc_type: str | None
    doc_subclass: str | None
    confidence: float
    raw_confidence: float
    calibrated: bool
    mode: SortMode
    confidence_source: Literal["logprob", "self_report"]
    doc_type_disagree: bool
    disagree_reason: str | None
    usage: Usage


# --------------------------------------------------------------------------- confidence


def _logit(p: float) -> float:
    """Log-odds of ``p`` after clamping to ``[1e-6, 1 - 1e-6]``."""
    p = min(max(p, _CONF_EPS), 1.0 - _CONF_EPS)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    """Standard logistic function."""
    return 1.0 / (1.0 + math.exp(-x))


def calibration_path() -> Path:
    """Path of the sorter calibration file under the configured base dir."""
    return get_settings().base_dir / "models" / "calibration.json"


def _as_temperature(value: Any) -> float | None:
    """Coerce a leaf value (bare float or ``{"temperature": T}``) to a float."""
    if isinstance(value, dict):
        value = value.get("temperature")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _temperature_for(
    data: Any, provider: str, model: str, doc_type: str
) -> float | None:
    """Read the temperature-scaling factor for (provider, model, doc_type).

    Accepts a nested ``{provider: {model: {doc_type: T}}}`` file (the canonical
    layout) and flat ``provider|model|doc_type`` / ``provider/model/doc_type``
    keys; a leaf may be the bare temperature or ``{"temperature": T}``.
    """
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("temperatures"), dict):
        nested = _temperature_for(data["temperatures"], provider, model, doc_type)
        if nested is not None:
            return nested
    node = data.get(provider)
    if isinstance(node, dict):
        leaf = node.get(model)
        if isinstance(leaf, dict) and doc_type in leaf:
            return _as_temperature(leaf[doc_type])
    for key in (
        f"{provider}|{model}|{doc_type}",
        f"{provider}/{model}/{doc_type}",
        f"{provider}:{model}:{doc_type}",
    ):
        if key in data:
            return _as_temperature(data[key])
    return None


def load_calibration(provider: str, model: str, doc_type: str) -> float | None:
    """Temperature for the triple, or None when the file/entry is absent."""
    path = calibration_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return _temperature_for(data, provider, model, doc_type)


# --------------------------------------------------------------------------- request


def _system_prompt(handoff: Handoff) -> str:
    """`sorter_v14`, plus the scoped subclass block in ``SUBCLASS_ONLY`` mode."""
    system = load_prompt("sorter_v14")
    if handoff.mode is SortMode.SUBCLASS_ONLY:
        scope = load_prompt("sorter_subclass_scope")
        doc_type = handoff.locked_doc_type or ""
        subclasses = ", ".join(subclass_vocab(doc_type))
        system = f"{system}\n\n{scope.format(doc_type=doc_type, subclasses=subclasses)}"
    return system


def _sorter_response_format(handoff: Handoff) -> dict[str, Any]:
    """Strict JSON schema; the subclass enum is class-scoped in ``SUBCLASS_ONLY``."""
    if handoff.mode is SortMode.SUBCLASS_ONLY:
        properties: dict[str, Any] = {
            "doc_subclass": {
                "type": "string",
                "enum": subclass_vocab(handoff.locked_doc_type or ""),
            }
        }
    else:
        properties = {
            "doc_type": {"type": "string", "enum": list(load_taxonomy().classes)},
            "doc_subclass": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        }
    properties["confidence"] = {"type": "number"}
    properties["doc_type_disagree"] = {"type": "boolean"}
    properties["doc_type_disagree_reason"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {"name": "sorter_result", "strict": True, "schema": schema},
    }


def sort(text: str, handoff: Handoff, *, attempt: int = 0) -> SortResult:
    """Classify (FULL) or subclass (SUBCLASS_ONLY) one document."""
    tax = load_taxonomy()
    capped = text[: tax.agent("sorter").max_input_chars]

    messages = [
        {"role": "system", "content": _system_prompt(handoff)},
        {"role": "user", "content": _user_message(capped, handoff)},
    ]
    ctx = ToolContext(doc_text=capped)
    result = call_structured(
        "sorter",
        messages,
        response_format=_sorter_response_format(handoff),
        tools=tools_for("sorter", ctx),
        logprobs=True,
    )
    parsed = result.parsed or {}

    locked = handoff.locked_doc_type
    if handoff.mode is SortMode.SUBCLASS_ONLY:
        doc_type = locked
    else:
        doc_type = parsed.get("doc_type") or locked
    doc_subclass = parsed.get("doc_subclass")
    disagree = bool(parsed.get("doc_type_disagree", False))
    reason = parsed.get("doc_type_disagree_reason") or parsed.get("disagree_reason")

    if result.label_logprob is not None:
        raw = math.exp(result.label_logprob)
        source: Literal["logprob", "self_report"] = "logprob"
    else:
        value = parsed.get("confidence")
        raw = float(value) if isinstance(value, (int, float)) else 0.0
        source = "self_report"
    raw = min(max(raw, 0.0), 1.0)

    resolved = resolve("sorter")
    temperature = load_calibration(resolved.provider, resolved.model, doc_type or "")
    calibrated = temperature is not None and temperature > 0
    confidence = _sigmoid(_logit(raw) / temperature) if calibrated else raw

    return SortResult(
        doc_type=doc_type,
        doc_subclass=doc_subclass,
        confidence=confidence,
        raw_confidence=raw,
        calibrated=calibrated,
        mode=handoff.mode,
        confidence_source=source,
        doc_type_disagree=disagree,
        disagree_reason=reason,
        usage=result.usage,
    )


def _user_message(capped: str, handoff: Handoff) -> str:
    """Build the user turn; ``FULL`` mode appends the BERT handoff prior."""
    message = f"Classify this document:\n\n{capped}"
    if handoff.mode is SortMode.FULL and handoff.prior:
        message = f"{message}\n\nHandoff prior:\n{handoff.prior}"
    return message
