"""Parse mailroom-dataset ground-truth metadata into scoring-ready fields.

Dataset authority: ``Lucius-Morningstar/mailroom-dataset``, config
``ground_truth`` (the ``gt_fields`` column; ``expected`` is the doc-class
label). Each ``gt_fields`` value is a JSON string holding the union of every
live class's fields, the content differentiators, annotation stats, and a
stringified ``gt_presence`` map::

    {"adjuster": "", "cuad_clause_labels": "{}", ...,
     "gt_presence": "{\\"adjuster\\": \\"not_applicable\\", ...}"}

``gt_presence`` values are:

* ``populated`` — a real expected value for this document;
* ``not_applicable`` — the class never carries this field;
* ``schema_documented_absence`` — the field exists in the class schema but
  this document legitimately has none (e.g. ``denial_reasons`` on an
  approved claim, ``adjuster`` on CMS rows);
* ``pending_annotation`` — the label backfill has not run yet (the current
  corpus has 91 contract rows with ``cuad_clause_labels`` in this state).

Scoring must treat every absence status as **empty**: a model that correctly
emits nothing for them is not penalized, and stringified ``"[]"`` / ``"{}"``
values are empty containers, not required events. Annotation stats
(``gt_presence``, ``intent_status``, ``token_estimate``, …) are never
extraction targets.

This module is dependency-free (stdlib only) so every scorer can import it
without cycles.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Mapping, Sequence

__all__ = [
    "ABSENT_PRESENCE_STATUSES",
    "ANNOTATION_KEYS",
    "CUAD_PRESENCE_KEY",
    "GT_PRESENCE_KEY",
    "derive_presence_from_gt",
    "gt_presence_map",
    "is_empty_value",
    "normalize_field_values",
    "parse_gt_fields",
    "parse_json_container",
    "presence_expectations_from_cuad_labels",
    "scoring_gt_fields",
]

#: Stringified presence map inside the Hub metadata.
GT_PRESENCE_KEY = "gt_presence"

#: CUAD category → spans map in the Hub metadata (presence input, not an
#: extraction field).
CUAD_PRESENCE_KEY = "cuad_clause_labels"

#: ``gt_presence`` statuses that mean "no expected value for this document":
#: the field does not apply, is documented absent, or its label backfill has
#: not run yet (``pending_annotation`` — stale values must never score).
ABSENT_PRESENCE_STATUSES: frozenset[str] = frozenset(
    {"not_applicable", "schema_documented_absence", "pending_annotation"}
)

#: Metadata fields that are annotation / provenance / presence inputs — never
#: extraction targets, on either side of a comparison.
ANNOTATION_KEYS: frozenset[str] = frozenset(
    {
        GT_PRESENCE_KEY,
        CUAD_PRESENCE_KEY,
        "intent_status",
        "intent_source",
        "intent_confidence",
        "label_evidence",
        "topic_evidence",
        "sentiment_evidence",
        "sentiment_score",
        "context_window_band",
        "token_estimate",
        "clause_count",
        "maud_label_count",
        "related_document_ids",
        "relationships",
    }
)

_NULL_TOKENS: frozenset[str] = frozenset({"null", "none", "n/a", "n.a."})

#: A scorable clause text must contain at least one alphanumeric character.
_HAS_ALNUM = re.compile(r"[A-Za-z0-9]")


def parse_json_container(value: Any) -> Any:
    """Parse a stringified JSON list/object; return *value* unchanged otherwise.

    ``"[]"`` → ``[]``, ``"{}"`` → ``{}``, ``'["Bylaws"]'`` → ``["Bylaws"]``.
    Bare strings and JSON scalars (``"41"``, ``"true"``) are left as-is so a
    numeric string stays a string for the field scorers.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{" or stripped[-1] not in "]}":
        return value
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return value
    return parsed if isinstance(parsed, (list, dict)) else value


def is_empty_value(value: Any) -> bool:
    """True for ``None``, blank strings, JSON / placeholder absence tokens
    (``null``, ``none``, ``n/a``), and empty collections — including their
    stringified forms (``"[]"``, ``"{}"``). Numbers and booleans (including
    ``0`` / ``False``) are values.
    """
    if isinstance(value, str):
        parsed = parse_json_container(value)
        if parsed is not value:
            return is_empty_value(parsed)
        stripped = value.strip()
        return not stripped or stripped.lower() in _NULL_TOKENS
    if value is None:
        return True
    if isinstance(value, (list, dict, tuple, set)) and len(value) == 0:
        return True
    return False


def parse_gt_fields(raw: Any) -> dict[str, Any]:
    """Parse one Hub ``gt_fields`` value into a dict with nested JSON parsed.

    Accepts a JSON string, a Python dict-repr string (the ``default.metadata``
    shape), or a mapping; a blank string returns ``{}``. Return a new dict
    with string keys and stringified JSON containers parsed at the field
    level, without recursively normalizing their contents.

    Raise ``TypeError`` for inputs other than strings or mappings, and
    ``ValueError`` for malformed strings or values that decode to something
    other than a mapping.
    """
    if isinstance(raw, Mapping):
        obj: Any = dict(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            try:
                obj = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                raise ValueError(
                    "gt_fields is neither JSON nor a Python dict literal"
                ) from None
    else:
        raise TypeError(
            f"gt_fields must be a mapping or string, got {type(raw).__name__}"
        )
    if not isinstance(obj, Mapping):
        raise ValueError(
            f"gt_fields must decode to a mapping, got {type(obj).__name__}"
        )
    return {str(key): parse_json_container(value) for key, value in obj.items()}


def gt_presence_map(fields: Mapping[str, Any] | None) -> dict[str, str]:
    """The ``gt_presence`` map (stringified or parsed), or ``{}``."""
    raw = (fields or {}).get(GT_PRESENCE_KEY)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def normalize_field_values(record: Any) -> Any:
    """Return a new field dict with string keys and JSON containers parsed.

    Only field values are parsed, without recursively normalizing container
    contents. Non-mapping inputs are returned unchanged.
    """
    if not isinstance(record, Mapping):
        return record
    return {str(key): parse_json_container(value) for key, value in record.items()}


def scoring_gt_fields(
    fields: Mapping[str, Any],
    *,
    field_types: Mapping[str, str] | None = None,
    extra_keys: Sequence[str] = (),
    drop_unmapped: bool = True,
) -> dict[str, Any]:
    """Scope Hub metadata to one suite's scorable surface.

    * drops annotation keys and ``gt_presence`` itself;
    * when ``drop_unmapped`` is true and the union of ``field_types`` keys
      and ``extra_keys`` is nonempty, keeps only those keys; an empty union
      leaves unmapped keys intact;
    * replaces fields whose ``gt_presence`` status is
      ``not_applicable`` / ``schema_documented_absence`` /
      ``pending_annotation`` with ``""`` so they are never expected events.

    ``drop_unmapped=False`` keeps the historical behavior for plain field
    dicts (no Hub ``gt_presence``): unmapped keys still reach the heuristic
    scorer, while annotation stats and stringified values are still handled.
    Return a new dict; parsing errors from :func:`parse_gt_fields` propagate.
    """
    parsed = parse_gt_fields(fields)
    presence = gt_presence_map(parsed)
    allowed = set(field_types or ()) | set(extra_keys or ())
    out: dict[str, Any] = {}
    for key, value in parsed.items():
        if key in ANNOTATION_KEYS:
            continue
        if drop_unmapped and allowed and key not in allowed:
            continue
        if presence.get(key) in ABSENT_PRESENCE_STATUSES:
            out[key] = ""
            continue
        out[key] = value
    return out


def presence_expectations_from_cuad_labels(
    labels: Any,
    *,
    field: str = "cuad_clauses",
) -> dict[str, dict[str, Any]]:
    """Build ``score_category_presence`` expectations from CUAD label spans.

    Hub shape: ``{category: [{"start": int, "text": str}, ...]}``. A category
    with scorable spans is ``expected: True`` with the first text containing
    an ASCII letter or digit as the answer; empty categories are
    ``expected: False`` (recorded for detail, never scored). Categories whose spans are all
    placeholders (``"[*]"``, ``"____"``, ``"."`` — no alphanumeric content)
    are omitted: no model can quote an unmatchable clause, so they must not
    count toward the presence score. ``field`` names the prediction field
    searched for every category. Stringified JSON containers are accepted;
    a non-mapping label value returns ``{}``, and non-list category spans
    are treated as empty.
    """
    labels = parse_json_container(labels)
    if not isinstance(labels, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for category, spans in labels.items():
        spans = parse_json_container(spans)
        items = spans if isinstance(spans, list) else []
        answer = ""
        for span in items:
            if isinstance(span, Mapping) and span.get("text"):
                text = str(span["text"])
                if _HAS_ALNUM.search(text):
                    answer = text
                    break
        if items and not answer:
            # Placeholder-only category: present in GT but unscorable.
            continue
        out[str(category)] = {
            "expected": bool(items),
            "answer": answer,
            "field": field,
        }
    return out


def derive_presence_from_gt(
    fields: Mapping[str, Any] | None,
    *,
    field: str = "cuad_clauses",
) -> dict[str, dict[str, Any]] | None:
    """Presence expectations from a parsed Hub row, or ``None`` when the row
    has no present CUAD category (an all-absent map must not turn an empty row
    into a scored 1.0).

    A ``cuad_clause_labels`` field carrying an absent status — including
    ``pending_annotation`` — yields ``None`` even when a stale non-empty
    label value is still attached: the backfill has not run, so nothing may
    be scored against it.
    """
    if gt_presence_map(fields).get(CUAD_PRESENCE_KEY) in ABSENT_PRESENCE_STATUSES:
        return None
    expectations = presence_expectations_from_cuad_labels(
        (fields or {}).get(CUAD_PRESENCE_KEY), field=field
    )
    if any(entry["expected"] for entry in expectations.values()):
        return expectations
    return None
