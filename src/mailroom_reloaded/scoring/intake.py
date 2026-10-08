"""Intake clerk — pre-sorter text cleaning, format fixes, and prep.

Byte-compatible with llm-mailroom ``agents.intake`` (``deterministic_normalize``
/ ``looks_messy`` / ``intake_span_output``) and The-Mailroom
``mailroom_ui.intake_normalize``. Keep the three copies in sync.

Live mailroom runs the **deterministic** clerk after transcription and before
``classify-document`` (sorter), emitting span ``normalize-intake``. An LLM
normalizer is a valid alternate *method* of the same agent: it is scored
against this clerk's gold, not executed here.

Prep steps applied before the sorter sees ``doc_text``:

1. Unicode NFC
2. Newline unify (CRLF/CR / U+2028 / U+2029 → LF)
3. NBSP → space
4. Strip zero-width chars (ZWSP/ZWNJ/ZWJ/BOM/WJ)
5. C0 controls → space
6. Hyphen unwrap (``agree-\\nment`` → ``agreement``)
7. Collapse 3+ blank lines to a single paragraph break
8. Collapse horizontal runs (markdown table rows preserved)
9. Trim leading/trailing empty lines
10. ``looks_messy`` flag (OCR-ish residue the sorter should treat cautiously)
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .field_scoring import score_field

__all__ = [
    "INTAKE_SPAN",
    "INTAKE_HANDOFF_NODE",
    "INTAKE_HANDOFF_AGENT",
    "INTAKE_METHODS",
    "INTAKE_LIVE_METHOD",
    "INTAKE_SPAN_KEYS",
    "INTAKE_PREP_STEPS",
    "deterministic_normalize",
    "looks_messy",
    "intake_span_output",
    "apply_intake",
    "prep_invariants",
    "intake_prep_completeness",
    "score_intake",
]

INTAKE_SPAN = "normalize-intake"
INTAKE_HANDOFF_NODE = "classify-document"
INTAKE_HANDOFF_AGENT = "sorter"
INTAKE_METHODS: tuple[str, ...] = ("deterministic", "llm")
INTAKE_LIVE_METHOD = "deterministic"

INTAKE_SPAN_KEYS: tuple[str, ...] = (
    "messy",
    "changed",
    "collapsed_blank_runs",
    "hyphen_unwraps",
    "method",
    "chars",
    "raw_chars",
    "cleaned_chars",
)

#: Ordered prep steps the clerk runs (and that an LLM intake must still satisfy).
INTAKE_PREP_STEPS: tuple[str, ...] = (
    "nfc_normalize",
    "newline_unify",
    "nbsp_to_space",
    "strip_zero_width",
    "control_chars",
    "hyphen_unwrap",
    "collapse_blank_runs",
    "collapse_horizontal_space",
    "trim_edges",
)

_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)
_MULTI_BLANK = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
_MULTI_SPACE = re.compile(r"[^\S\n]{2,}")
_HYPHEN_WRAP = re.compile(r"(?<=[A-Za-z])-\n(?=[A-Za-z])")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\ufeff\u2060")


def deterministic_normalize(text: str) -> tuple[str, dict]:
    """Whitespace / hyphen / NBSP clerk. Returns ``(cleaned, stats)``.

    Stats keys match mailroom: ``raw_chars``, ``cleaned_chars``,
    ``collapsed_blank_runs``, ``hyphen_unwraps``, ``changed``.
    """
    raw_chars = len(text or "")
    if not text:
        return "", {
            "raw_chars": 0,
            "cleaned_chars": 0,
            "collapsed_blank_runs": 0,
            "hyphen_unwraps": 0,
            "changed": False,
        }
    original = text
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u2028", "\n").replace("\u2029", "\n")
    text = text.translate(_ZW)
    text = _CTRL.sub(" ", text)
    hyphen_unwraps = len(_HYPHEN_WRAP.findall(text))
    text = _HYPHEN_WRAP.sub("", text)
    collapsed_blank = len(_MULTI_BLANK.findall(text))
    text = _MULTI_BLANK.sub("\n\n", text)
    lines = []
    for line in text.split("\n"):
        stripped = line.rstrip()
        if stripped.lstrip().startswith("|") and stripped.rstrip().endswith("|"):
            lines.append(stripped)
        else:
            lines.append(_MULTI_SPACE.sub(" ", stripped).strip() if stripped else "")
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    cleaned = "\n".join(lines)
    return cleaned, {
        "raw_chars": raw_chars,
        "cleaned_chars": len(cleaned),
        "collapsed_blank_runs": collapsed_blank,
        "hyphen_unwraps": hyphen_unwraps,
        "changed": cleaned != original,
    }


def looks_messy(text: str, stats: dict | None = None) -> bool:
    """Heuristic: leftover OCR / wrap artifacts the sorter should treat cautiously."""
    if not text or not text.strip():
        return False
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return False
    short = sum(1 for ln in lines if len(ln.split()) <= 2)
    avg_len = sum(len(ln) for ln in lines) / len(lines)
    ctrl_ratio = sum(
        1 for ch in text
        if ch == "\ufffd" or (unicodedata.category(ch) == "Cc" and ch not in "\n\t")
    ) / max(len(text), 1)
    hyphen_left = text.count("-\n")
    if stats and stats.get("collapsed_blank_runs", 0) >= 8:
        return True
    if ctrl_ratio > 0.01:
        return True
    if len(lines) >= 20 and short / len(lines) > 0.55 and avg_len < 28:
        return True
    if hyphen_left >= 6:
        return True
    return False


def intake_span_output(stats: dict, messy: bool, *, method: str = INTAKE_LIVE_METHOD) -> dict:
    """Curated ``normalize-intake`` observation output (no document text)."""
    cleaned = int(stats.get("cleaned_chars") or 0)
    method_key = str(method or INTAKE_LIVE_METHOD).strip().lower()
    if method_key not in INTAKE_METHODS:
        method_key = INTAKE_LIVE_METHOD
    return {
        "messy": bool(messy),
        "changed": bool(stats.get("changed")),
        "collapsed_blank_runs": int(stats.get("collapsed_blank_runs") or 0),
        "hyphen_unwraps": int(stats.get("hyphen_unwraps") or 0),
        "method": method_key,
        "chars": cleaned,
        "raw_chars": int(stats.get("raw_chars") or 0),
        "cleaned_chars": cleaned,
    }


def apply_intake(
    text: str,
    *,
    filename: str | None = None,
    method: str = INTAKE_LIVE_METHOD,
) -> tuple[str, dict]:
    """Run the deterministic clerk and return ``(cleaned_text, stats)``.

    ``filename`` is accepted for mailroom API compatibility (span input);
    this package does not emit traces. ``method`` other than
    ``deterministic`` is recorded on the payload so LLM intake can be
    *scored*, not executed.
    """
    del filename  # span metadata only in the live pipeline
    cleaned, stats = deterministic_normalize(text)
    messy = looks_messy(cleaned, stats)
    payload = intake_span_output(stats, messy, method=method)
    return cleaned, {**stats, **payload}


def prep_invariants(text: str) -> dict[str, bool]:
    """Whether each prep step's post-condition holds on *text*."""
    text = text or ""
    table_ok = True
    for line in text.split("\n"):
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            continue
        if _MULTI_SPACE.search(line):
            table_ok = False
            break
    return {
        "nfc_normalize": unicodedata.normalize("NFC", text) == text,
        "newline_unify": "\r" not in text and "\u2028" not in text and "\u2029" not in text,
        "nbsp_to_space": "\u00a0" not in text,
        "strip_zero_width": not any(ch in _ZERO_WIDTH for ch in text),
        "control_chars": _CTRL.search(text) is None,
        "hyphen_unwrap": _HYPHEN_WRAP.search(text) is None,
        "collapse_blank_runs": _MULTI_BLANK.search(text) is None,
        "collapse_horizontal_space": table_ok,
        "trim_edges": not text.startswith("\n") and not text.endswith("\n"),
    }


def intake_prep_completeness(text: str) -> float:
    """Share of :data:`INTAKE_PREP_STEPS` invariants that hold on *text*."""
    checks = prep_invariants(text)
    steps = [checks[s] for s in INTAKE_PREP_STEPS if s in checks]
    return round(sum(1.0 if ok else 0.0 for ok in steps) / len(steps), 4) if steps else 1.0


def _as_text_and_payload(value: Any) -> tuple[str | None, dict]:
    """Coerce a string or span/stats dict into ``(optional_text, payload)``."""
    if value is None:
        return None, {}
    if isinstance(value, dict):
        text = value.get("text")
        if text is None:
            text = value.get("cleaned") or value.get("doc_text") or value.get("cleaned_text")
        payload = {k: value[k] for k in INTAKE_SPAN_KEYS if k in value}
        if "intake_messy" in value and "messy" not in payload:
            payload["messy"] = value["intake_messy"]
        if "intake_changed" in value and "changed" not in payload:
            payload["changed"] = value["intake_changed"]
        if "intake_method" in value and "method" not in payload:
            payload["method"] = value["intake_method"]
        return (None if text is None else str(text), payload)
    return str(value), {}


def _gold_from(expected: Any, *, raw_text: str | None = None) -> tuple[str, dict, bool, dict]:
    source = raw_text if raw_text is not None else expected
    if isinstance(source, dict):
        raw = source.get("raw") or source.get("raw_text") or source.get("text") or ""
        cleaned, stats = deterministic_normalize(str(raw))
        if source.get("text") and raw_text is None and "raw" not in source and "raw_text" not in source:
            # Explicit gold cleaned text — still run the clerk on it (idempotent).
            cleaned, stats = deterministic_normalize(str(source.get("text") or ""))
    else:
        cleaned, stats = deterministic_normalize("" if source is None else str(source))
    messy = looks_messy(cleaned, stats)
    payload = intake_span_output(stats, messy)
    return cleaned, stats, messy, payload


def _flag_match(pred: Any, gold: Any) -> float | None:
    if pred is None or gold is None:
        return None
    return 1.0 if bool(pred) == bool(gold) else 0.0


def _int_match(pred: Any, gold: Any) -> float | None:
    if pred is None or gold is None:
        return None
    try:
        return 1.0 if int(pred) == int(gold) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _score_one(expected: Any, predicted: Any, *, raw_text: str | None = None) -> dict:
    gold_text, gold_stats, gold_messy, gold_payload = _gold_from(
        expected, raw_text=raw_text
    )
    pred_text, pred_payload = _as_text_and_payload(predicted)
    method = str(pred_payload.get("method") or gold_payload["method"]).strip().lower()
    if method not in INTAKE_METHODS:
        method = INTAKE_LIVE_METHOD

    text_for_prep = pred_text if pred_text is not None else gold_text
    completeness = intake_prep_completeness(text_for_prep)
    invariants = prep_invariants(text_for_prep)

    exact = None
    token_f1 = None
    if pred_text is not None:
        exact = 1.0 if pred_text == gold_text else 0.0
        token_f1 = float(score_field("free_text", pred_text, gold_text) or 0.0)

    changed_ok = _flag_match(pred_payload.get("changed"), gold_payload["changed"])
    messy_ok = _flag_match(pred_payload.get("messy"), gold_messy)
    hyphen_ok = _int_match(
        pred_payload.get("hyphen_unwraps"), gold_stats["hyphen_unwraps"]
    )
    blank_ok = _int_match(
        pred_payload.get("collapsed_blank_runs"), gold_stats["collapsed_blank_runs"]
    )
    method_ok = 1.0 if method in INTAKE_METHODS else 0.0

    stat_scores = [s for s in (changed_ok, messy_ok, hyphen_ok, blank_ok) if s is not None]
    if exact is not None:
        accuracy = exact
    elif stat_scores:
        accuracy = _mean(stat_scores)
    else:
        accuracy = completeness

    return {
        "task": "intake",
        "kind": "intake",
        "accuracy": accuracy,
        "f1_macro": token_f1 if token_f1 is not None else accuracy,
        "intake_prep_completeness": completeness,
        "intake_changed": gold_payload["changed"],
        "intake_messy": gold_messy,
        "intake_changed_match": changed_ok,
        "intake_messy_match": messy_ok,
        "intake_hyphen_unwraps": gold_stats["hyphen_unwraps"],
        "intake_collapsed_blanks": gold_stats["collapsed_blank_runs"],
        "intake_hyphen_unwraps_match": hyphen_ok,
        "intake_collapsed_blanks_match": blank_ok,
        "intake_method": method,
        "intake_method_valid": method_ok,
        "intake_chars": gold_payload["cleaned_chars"],
        "raw_chars": gold_stats["raw_chars"],
        "cleaned_chars": gold_stats["cleaned_chars"],
        "prep_invariants": invariants,
        "gold_text": gold_text,
        "n": 1,
    }


def score_intake(
    expected: Any,
    predicted: Any,
    *,
    raw_text: str | None = None,
) -> dict:
    """Score intake output against the deterministic clerk gold.

    ``expected`` is the raw (or already-cleaned) source text — the clerk is
    idempotent, so either works. ``predicted`` is the agent's cleaned text,
    a ``normalize-intake`` span payload, or a dict with ``text`` plus stats.

    An LLM intake is scored the same way: compare its cleaned text / flags
    to ``deterministic_normalize(expected)``. ``method`` on the payload
    should be ``llm`` or ``deterministic``.
    """
    if expected is None and predicted is None and raw_text is None:
        raise TypeError(
            "intake suite.score() needs raw text and a predicted cleaned "
            "string or normalize-intake payload"
        )
    if isinstance(expected, list) and isinstance(predicted, list):
        rows = [
            _score_one(e, p, raw_text=raw_text) for e, p in zip(expected, predicted)
        ]
        keys = (
            "accuracy", "f1_macro", "intake_prep_completeness",
            "intake_method_valid",
        )
        out = {
            "task": "intake",
            "kind": "intake",
            "n": len(rows),
            "intake_changed_rate": _mean([1.0 if r["intake_changed"] else 0.0 for r in rows]),
            "intake_messy_rate": _mean([1.0 if r["intake_messy"] else 0.0 for r in rows]),
            "intake_hyphen_unwraps": round(
                sum(r["intake_hyphen_unwraps"] for r in rows) / len(rows), 4
            ) if rows else 0.0,
            "intake_collapsed_blanks": round(
                sum(r["intake_collapsed_blanks"] for r in rows) / len(rows), 4
            ) if rows else 0.0,
            "per_document": rows,
        }
        for key in keys:
            out[key] = _mean([float(r[key]) for r in rows])
        match_keys = (
            "intake_changed_match", "intake_messy_match",
            "intake_hyphen_unwraps_match", "intake_collapsed_blanks_match",
        )
        for key in match_keys:
            vals = [r[key] for r in rows if r[key] is not None]
            out[key] = _mean(vals) if vals else None
        return out
    result = _score_one(expected, predicted, raw_text=raw_text)
    result["intake_changed_rate"] = 1.0 if result["intake_changed"] else 0.0
    result["intake_messy_rate"] = 1.0 if result["intake_messy"] else 0.0
    return result
