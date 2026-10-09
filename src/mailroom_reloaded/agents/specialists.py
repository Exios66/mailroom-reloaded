"""Frozen-v1 specialists (spec section 7): one structured call per class.

Frozen classes cap the input and use the class prompt; the frozen merger takes a
15k head + 15k tail; the merger ``dagger`` mode windows the whole agreement
(47,000-char windows, 6,500 overlap) with SAND-37 / SAND-040 sampling
(``top_p 0.8``, ``top_k 20``, ``presence_penalty 1.0``, 6,144 output cap, one
re-sample on a length cap) and merges the windows deterministically afterwards.

Structured output is the strict ``response_format`` of the class schema; output
is assessed with ``assess_payload`` and malformed JSON gets one repair re-ask.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mailroom_reloaded.llm.client import call_structured
from mailroom_reloaded.llm.tooling import LengthFinishReasonError
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.prompts.loader import load_prompt
from mailroom_reloaded.schemas.extraction import (
    assess_payload,
    get_extraction_schema,
    response_format,
)
from mailroom_reloaded.scoring import canonical_maud_class
from mailroom_reloaded.settings import RunConditions, load_taxonomy
from mailroom_reloaded.tools import ToolContext, tools_for

__all__ = [
    "ExtractResult",
    "extract",
    "extraction_confidence",
    "merge_merger_windows",
    "prepare_input",
]

MERGER_FROZEN_HEAD = 15_000
MERGER_FROZEN_TAIL = 15_000
MERGER_FROZEN_CAP = MERGER_FROZEN_HEAD + MERGER_FROZEN_TAIL

MERGER_DAGGER_WINDOW = 47_000
MERGER_DAGGER_OVERLAP = 6_500
MERGER_DAGGER_OUTPUT_CAP = 6_144
MERGER_DAGGER_SAMPLING: dict[str, Any] = {
    "top_p": 0.8,
    "top_k": 20,
    "presence_penalty": 1.0,
}
MERGER_MAUD_PROMPT = "merger_agreement_specialist_maud_v1"

_DOC_LABELS = {
    "contract": "contract",
    "merger_agreement": "merger agreement",
    "corporate_record": "corporate record",
    "correspondence": "correspondence",
    "insurance_claim": "insurance claim documentation",
}

_REPAIR_INSTRUCTION = (
    "Your previous response was not a valid JSON object for the schema. "
    "Return ONLY the complete JSON object, with no prose and no code fences."
)

_OMITTED = "\n\n[... middle of the agreement omitted; head and tail shown ...]\n\n"


@dataclass(frozen=True)
class ExtractResult:
    """One specialist extraction: data, validity, confidence and usage."""

    doc_type: str
    data: dict | None
    schema_valid: bool
    parse_error: str | None
    confidence: float | None
    error_kind: str | None
    calls: int
    usage: Usage


def extraction_confidence(
    schema_valid: bool, coverage: float, mean_token_prob: float
) -> float:
    """Spec section 6: ``schema_valid x (0.6*coverage + 0.4*mean_token_prob)``."""
    if not schema_valid:
        return 0.0
    return 0.6 * coverage + 0.4 * mean_token_prob


# --------------------------------------------------------------------------- input


def _dagger_windows(text: str) -> list[str]:
    """Split ``text`` into 47,000-char windows overlapping by 6,500 chars."""
    if len(text) <= MERGER_DAGGER_WINDOW:
        return [text]
    step = MERGER_DAGGER_WINDOW - MERGER_DAGGER_OVERLAP
    windows: list[str] = []
    start = 0
    while start < len(text):
        windows.append(text[start : start + MERGER_DAGGER_WINDOW])
        start += step
    return windows


def prepare_input(text: str, doc_type: str, cond: RunConditions) -> list[str]:
    """Document text as it is sent: one cap, head+tail, or dagger windows."""
    if doc_type == "merger_agreement" and cond.merger_mode == "dagger":
        return _dagger_windows(text)
    if doc_type == "merger_agreement":
        if len(text) <= MERGER_FROZEN_CAP:
            return [text]
        return [text[:MERGER_FROZEN_HEAD], text[-MERGER_FROZEN_TAIL:]]
    return [text[: cond.input_cap_chars]]


# --------------------------------------------------------------------------- tools


def _tools_enabled(role: str, prompt_set: str, tools: bool | None) -> bool:
    """Resolve whether the specialist offers tools (sand37 forces off)."""
    if prompt_set == "sand37":
        return False
    if tools is not None:
        return bool(tools)
    raw = load_taxonomy().raw
    return bool(((raw.get("agents") or {}).get(role) or {}).get("tools", True))


def _sampling(cond: RunConditions, dagger: bool) -> dict[str, Any]:
    """Per-call sampling params; dagger adds SAND-37 sampling and its 6,144 cap."""
    if dagger:
        return {
            "temperature": cond.temperature,
            "max_tokens": MERGER_DAGGER_OUTPUT_CAP,
            **MERGER_DAGGER_SAMPLING,
        }
    return {"temperature": cond.temperature, "max_tokens": cond.output_cap_tokens}


# --------------------------------------------------------------------------- windows


def _messages(
    system: str,
    doc_type: str,
    doc_subclass: str | None,
    chunk: str,
    index: int,
    total: int,
) -> list[dict[str, Any]]:
    """Build the specialist messages for one chunk (adds a chunk header when split)."""
    label = _DOC_LABELS.get(doc_type, doc_type)
    header = ""
    if total > 1:
        header = (
            f"EXTRACTION CHUNK {index} OF {total} - this is one window of the "
            "document; extract every field occurrence present in THIS chunk.\n\n"
        )
    user = f"{header}Extract structured data from this {label}:\n\n{chunk}"
    if doc_subclass:
        user = f"doc_subclass: {doc_subclass}\n\n{user}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _run_chunk(
    role: str,
    system: str,
    doc_type: str,
    doc_subclass: str | None,
    chunk: str,
    index: int,
    total: int,
    rf: dict[str, Any],
    tool_defs: list[Any],
    sampling: dict[str, Any],
    dagger: bool,
) -> tuple[dict | None, bool, str | None, str | None, int, Usage]:
    """Run one chunk: one repair re-ask, plus one dagger length re-sample."""
    messages = _messages(system, doc_type, doc_subclass, chunk, index, total)
    calls = 0
    usage = Usage()
    for length_attempt in range(2):
        try:
            result = call_structured(
                role, messages, response_format=rf, tools=tool_defs, **sampling
            )
        except LengthFinishReasonError:
            calls += 1
            if dagger and length_attempt == 0:
                continue
            return None, False, None, "LengthFinishReasonError", calls, usage
        calls += 1
        usage = usage + result.usage
        assessment = assess_payload(doc_type, result.content or "")
        if assessment.schema_valid:
            return assessment.parsed, True, None, None, calls, usage
        if assessment.parse_error:
            repair = messages + [
                {"role": "assistant", "content": result.content or ""},
                {"role": "user", "content": _REPAIR_INSTRUCTION},
            ]
            repaired = call_structured(
                role, repair, response_format=rf, tools=tool_defs, **sampling
            )
            calls += 1
            usage = usage + repaired.usage
            second = assess_payload(doc_type, repaired.content or "")
            if second.schema_valid:
                return second.parsed, True, None, None, calls, usage
            return (
                second.parsed,
                False,
                second.parse_error or "schema_invalid",
                second.parse_error or "schema_invalid",
                calls,
                usage,
            )
        return assessment.parsed, False, None, "schema_invalid", calls, usage
    return None, False, None, "LengthFinishReasonError", calls, usage


def _nonempty(value: Any) -> bool:
    """True when ``value`` is present and non-blank/non-empty."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _coverage(doc_type: str, data: dict[str, Any]) -> float:
    """Share of the class's required fields that are non-empty in ``data``."""
    model = get_extraction_schema(doc_type)
    raw = load_taxonomy().raw
    required = ((raw.get("required_fields") or {}).get(doc_type)) or [
        name for name in model.model_fields if name not in ("reasoning", "confidence")
    ]
    if not required:
        return 1.0
    return sum(1 for name in required if _nonempty(data.get(name))) / len(required)


def extract(
    text: str,
    doc_type: str,
    doc_subclass: str | None,
    *,
    prompt_set: str = "frozen_v1",
    tools: bool | None = None,
    attempt: int = 0,
    cond: RunConditions | None = None,
) -> ExtractResult:
    """Extract one document. ``cond`` overrides the taxonomy condition (dagger tests)."""
    tax = load_taxonomy()
    cond = cond or tax.specialist_conditions(doc_type)
    role = tax.classes[doc_type].specialist
    dagger = doc_type == "merger_agreement" and cond.merger_mode == "dagger"

    system = (
        load_prompt(MERGER_MAUD_PROMPT)
        if dagger
        else load_prompt(role, prompt_set=prompt_set)
    )
    rf = response_format(doc_type)
    tool_defs = (
        tools_for(role, ToolContext(doc_text=text))
        if _tools_enabled(role, prompt_set, tools)
        else []
    )
    sampling = _sampling(cond, dagger)

    windows = prepare_input(text, doc_type, cond)
    if doc_type == "merger_agreement" and not dagger and len(windows) > 1:
        chunks = [_OMITTED.join(windows)]
    else:
        chunks = windows

    results: list[dict] = []
    calls = 0
    usage = Usage()
    parse_error: str | None = None
    error_kind: str | None = None
    for index, chunk in enumerate(chunks, start=1):
        data, valid, perror, ekind, chunk_calls, chunk_usage = _run_chunk(
            role,
            system,
            doc_type,
            doc_subclass,
            chunk,
            index,
            len(chunks),
            rf,
            tool_defs,
            sampling,
            dagger,
        )
        calls += chunk_calls
        usage = usage + chunk_usage
        if valid and data is not None:
            results.append(data)
        if perror and parse_error is None:
            parse_error = perror
        if ekind and error_kind is None:
            error_kind = ekind

    if results:
        data = (
            merge_merger_windows(results)
            if doc_type == "merger_agreement"
            else results[0]
        )
        confidence = extraction_confidence(True, _coverage(doc_type, data), 1.0)
        return ExtractResult(doc_type, data, True, None, confidence, None, calls, usage)
    return ExtractResult(
        doc_type, None, False, parse_error, 0.0, error_kind, calls, usage
    )


# --------------------------------------------------------------------------- merge


def _norm(value: Any) -> str:
    """Normalize a value for list-dedupe comparison (stripped, lowercased)."""
    return str(value).strip().lower()


def _maud_question_key(clause: str) -> str:
    """Canonical MAUD question key for a ``question: answer`` clause."""
    question = clause.split(":", 1)[0].strip()
    if not question:
        return ""
    return canonical_maud_class(question, question) or question.lower()


def merge_merger_windows(results: list[dict]) -> dict:
    """Union the per-window extractions into one agreement record.

    ``maud_clauses`` are keyed by the canonical MAUD question (first answer
    wins); other list fields union with normalized dedupe; scalars keep the
    first non-null value in document order; ``confidence`` keeps the max.
    """
    merged: dict[str, Any] = {}
    maud: dict[str, str] = {}
    max_confidence = 0.0
    for result in results:
        if not isinstance(result, dict):
            continue
        for key, value in result.items():
            if key == "maud_clauses":
                for clause in value or []:
                    if not isinstance(clause, str):
                        continue
                    question = _maud_question_key(clause)
                    if question and question not in maud:
                        maud[question] = clause
                continue
            if key == "confidence":
                try:
                    max_confidence = max(max_confidence, float(value or 0.0))
                except (TypeError, ValueError):
                    pass
                continue
            if isinstance(value, list):
                seen = {_norm(item) for item in merged.get(key) or []}
                for item in value:
                    if _norm(item) not in seen:
                        merged.setdefault(key, []).append(item)
                        seen.add(_norm(item))
            elif value not in (None, ""):
                if merged.get(key) in (None, ""):
                    merged[key] = value
    merged["maud_clauses"] = list(maud.values())
    merged.setdefault("confidence", max_confidence)
    return merged
