"""Field-micro precision / recall / F1 / F2 over extraction events.

Slot-filling / IE evaluation (ACE, CoNLL, SemEval) counts **(field, value)**
events, not the mean of per-field soft scores. This module is additive:
:func:`score_extraction` still returns the soft mean as ``overall_score``.

Confusion model
---------------
* Each **expected** field with a non-empty ground-truth value is one event.
* **TP**: that field's typed score is ``>= 1.0`` (exact / list F1 of 1.0).
  Partial list matches are **not** TP; they stay in ``extraction_overall_score``.
* **FN**: expected field scored ``< 1.0``.
* **FP**: predicted extra keys not in expected, **or** unmatched predicted
  items on an ``entity_list`` field (``EntityListScore.unmatched_predicted``),
  **or** a spurious fill on an empty GT field (configurable).
* Empty GT + empty/missing prediction is **correctly-empty** (credit 1.0 on
  the empty-field contract, not an FN, and not part of ``overall_score``).

Then ``P = TP/(TP+FP)``, ``R = TP/(TP+FN)``, ``F1 = 2PR/(P+R)``,
``F2 = 5PR/(4P+R)`` — the same F-beta formula as ContractEval in
:mod:`.tasks`.
"""

from __future__ import annotations

from typing import Any, Mapping

from .classification import fbeta
from .field_scoring import (
    NEVER_SCORED_FIELDS,
    RETIRED_PROMPT_KEYS,
    ExtractionScoreResult,
    score_extraction,
)
from .gt_metadata import is_empty_value

_EMPTY = (None, "", [], {})


def _is_empty(value: Any) -> bool:
    """Empty / null / stringified-empty (``"[]"``, ``"{}"``) — never an event."""
    return is_empty_value(value)


def _public_prf(
    precision: float,
    recall: float,
    *,
    tp: int,
    fp: int,
    fn: int,
    expected_events: int,
    entity_list_f1: float | None = None,
) -> dict[str, Any]:
    f1 = fbeta(precision, recall, beta=1.0)
    f2 = fbeta(precision, recall, beta=2.0)
    out: dict[str, Any] = {
        "extraction_precision": precision,
        "extraction_recall": recall,
        "extraction_f1": f1,
        "extraction_f2": f2,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "expected_events": expected_events,
        "n": expected_events,
        "entity_list_f1": entity_list_f1,
    }
    return out


def mean_entity_list_f1(result: ExtractionScoreResult) -> float | None:
    """Mean bipartite-match F1 over list fields on one document.

    Registers as ``entity_list_f1`` (the existing diagnostics bucket is
    ``entity_list_raw_f1``).
    """
    scores = [item.f1 for item in result.entity_list_scores.values()]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 4)


def extraction_binary_metrics(
    expected: Mapping[str, Any] | None,
    predicted: Mapping[str, Any] | None,
    *,
    field_map: Mapping[str, str] | None = None,
    field_types: Mapping[str, str] | None = None,
    doc_class: str = "extraction",
    result: ExtractionScoreResult | None = None,
    doc_text: str | None = None,
    penalize_spurious_empty: bool = True,
) -> dict[str, Any]:
    """Run-level (or single-doc) field-micro P/R/F1/F2.

    When ``result`` is omitted, this calls :func:`score_extraction` once.
    Empty / null expected fields are skipped for P/R events (same as
    :func:`score_extraction` for ``None`` / ``""``; empty lists are also
    skipped so they are not FN). Correctly-empty pairs credit 1.0 on
    ``empty_field_credit``; a spurious value on empty GT is an FP when
    ``penalize_spurious_empty`` is true.
    """
    expected = dict(expected or {})
    predicted = dict(predicted or {})
    types = dict(field_map or field_types or {})
    if result is None:
        result = score_extraction(
            doc_class, types, predicted, expected, doc_text=doc_text
        )

    tp = 0
    fn = 0
    fp = 0
    expected_events = 0
    n_correctly_empty = 0
    n_spurious_fill = 0

    for name, exp_val in expected.items():
        if name in NEVER_SCORED_FIELDS or (
            name in RETIRED_PROMPT_KEYS and name not in types
        ):
            continue
        if _is_empty(exp_val):
            if _is_empty(predicted.get(name)):
                n_correctly_empty += 1
            elif penalize_spurious_empty:
                n_spurious_fill += 1
                fp += 1
            continue
        expected_events += 1
        score = float(result.field_scores.get(name, 0.0))
        if score >= 1.0:
            tp += 1
        else:
            fn += 1
        list_score = result.entity_list_scores.get(name)
        if list_score is not None:
            fp += int(list_score.unmatched_predicted)

    for key, value in predicted.items():
        if key in NEVER_SCORED_FIELDS or (
            key in RETIRED_PROMPT_KEYS and key not in types
        ):
            continue
        if key not in expected and not _is_empty(value):
            fp += 1

    precision = round(tp / (tp + fp), 4) if (tp + fp) else 0.0
    recall = round(tp / (tp + fn), 4) if (tp + fn) else 0.0
    n_empty = n_correctly_empty + n_spurious_fill
    empty_credit = None
    if n_empty:
        empty_credit = round(n_correctly_empty / n_empty, 4)
    out = _public_prf(
        precision,
        recall,
        tp=tp,
        fp=fp,
        fn=fn,
        expected_events=expected_events,
        entity_list_f1=mean_entity_list_f1(result),
    )
    out.update(
        {
            "n_correctly_empty": n_correctly_empty,
            "n_spurious_fill": n_spurious_fill,
            "empty_field_credit": empty_credit,
        }
    )
    return out


def merge_extraction_counts(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Micro-average TP/FP/FN across documents."""
    tp = sum(int(row.get("tp") or 0) for row in rows)
    fp = sum(int(row.get("fp") or 0) for row in rows)
    fn = sum(int(row.get("fn") or 0) for row in rows)
    expected_events = sum(int(row.get("expected_events") or 0) for row in rows)
    list_f1s = [
        float(row["entity_list_f1"])
        for row in rows
        if row.get("entity_list_f1") is not None
    ]
    precision = round(tp / (tp + fp), 4) if (tp + fp) else 0.0
    recall = round(tp / (tp + fn), 4) if (tp + fn) else 0.0
    entity_list_f1 = (
        round(sum(list_f1s) / len(list_f1s), 4) if list_f1s else None
    )
    return _public_prf(
        precision,
        recall,
        tp=tp,
        fp=fp,
        fn=fn,
        expected_events=expected_events,
        entity_list_f1=entity_list_f1,
    )


def prf_bundle_keys(prf: Mapping[str, Any]) -> dict[str, Any]:
    """Registry names to attach on a suite extraction payload."""
    return {
        "extraction_precision": prf.get("extraction_precision"),
        "extraction_recall": prf.get("extraction_recall"),
        "extraction_f1": prf.get("extraction_f1"),
        "extraction_f2": prf.get("extraction_f2"),
        "entity_list_f1": prf.get("entity_list_f1"),
    }


__all__ = [
    "extraction_binary_metrics",
    "mean_entity_list_f1",
    "merge_extraction_counts",
    "prf_bundle_keys",
]
