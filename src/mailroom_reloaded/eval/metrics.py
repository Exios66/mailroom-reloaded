"""Sorter, specialist and gate KPIs for the SAND-37 cards (Task 21).

All three consume ``eval_docs``-shaped rows (mappings or SQLAlchemy row
mappings) and return plain dicts. Field-level counts follow the spec section 8
table exactly, with ``match_threshold`` 0.5:

    +-------------+--------------------------+-------------+
    | ground truth| prediction               | outcome     |
    +-------------+--------------------------+-------------+
    | non-empty   | matches (score >= .5)    | TP          |
    | non-empty   | non-empty, below .5      | FP + FN     |
    | empty       | non-empty                | FP          |
    | non-empty   | empty                    | FN          |
    | empty       | empty                    | not counted |
    +-------------+--------------------------+-------------+

TP/FP/FN are pooled across documents for ``micro_f1`` (dojo's micro-average);
``mean_doc_f1`` is the unweighted mean of each document's F1. F2 is
``fbeta(P, R, beta=2)`` on the pooled counts. ``judge_scorer_agreement`` is the
share of fields where the judge's verdict is in ``{correct, partial}`` iff the
deterministic field score is at or above 0.5.

Rows may carry the predicted vs ground-truth values under ``predicted`` /
``expected`` (in-memory join) or precomputed ``field_counts``; both shapes are
accepted so the metrics can run over the stored ``eval_docs`` rows once a card
builder joins them to ground truth.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from mailroom_reloaded.scoring import confusion_matrix, fbeta, score_field
from mailroom_reloaded.settings import load_taxonomy

__all__ = ["gate_kpis", "sorter_kpis", "specialist_kpis"]

MATCH_THRESHOLD = 0.5
_EMPTY = (None, "", [], {})
_EMPTY_STRINGS = {"null", "none", "n/a", "n.a.", "na"}


# --------------------------------------------------------------------------- coercion


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, Mapping) else {}
    return {}


def _is_empty(value: Any) -> bool:
    if value in _EMPTY:
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.strip().lower() in _EMPTY_STRINGS
    return False


def _get(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _field_types(doc_type: str | None) -> dict[str, str]:
    if not doc_type:
        return {}
    try:
        klass = load_taxonomy().classes.get(doc_type)
    except Exception:  # noqa: BLE001 - a KPI must never fail on config
        return {}
    return dict(klass.field_types) if klass is not None else {}


def _score_value(field_type: str, predicted: Any, expected: Any) -> float:
    result = score_field(field_type or "free_text", predicted, expected)
    if hasattr(result, "score"):
        return float(result.score)
    return float(result)


def _counts_for_pair(
    expected: Mapping[str, Any],
    predicted: Mapping[str, Any],
    field_types: Mapping[str, str],
    threshold: float,
) -> dict[str, int]:
    tp = fp = fn = 0
    for name, exp_value in expected.items():
        if _is_empty(exp_value):
            if not _is_empty(predicted.get(name)):
                fp += 1
            continue
        pred_value = predicted.get(name)
        if _is_empty(pred_value):
            fn += 1
            continue
        field_type = field_types.get(name, "free_text")
        if _score_value(field_type, pred_value, exp_value) >= threshold:
            tp += 1
        else:
            fp += 1
            fn += 1
    for name, pred_value in predicted.items():
        if name in expected or _is_empty(pred_value):
            continue
        fp += 1
    return {"tp": tp, "fp": fp, "fn": fn}


def _row_counts(
    row: Mapping[str, Any], doc_type: str | None, threshold: float
) -> dict[str, Any]:
    explicit = _get(row, "field_counts")
    if isinstance(explicit, Mapping):
        return {
            "tp": int(explicit.get("tp") or 0),
            "fp": int(explicit.get("fp") or 0),
            "fn": int(explicit.get("fn") or 0),
        }
    if any(k in row for k in ("tp", "fp", "fn")):
        return {
            "tp": int(row.get("tp") or 0),
            "fp": int(row.get("fp") or 0),
            "fn": int(row.get("fn") or 0),
        }
    expected = _as_mapping(
        _get(row, "expected", "gt", "ground_truth", "expected_fields", "gt_fields")
    )
    predicted = _as_mapping(
        _get(row, "predicted", "prediction", "extraction", "data", "output")
    )
    return _counts_for_pair(
        expected, predicted, _field_types(doc_type or row.get("doc_type")), threshold
    )


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = round(tp / (tp + fp), 4) if tp + fp else 0.0
    recall = round(tp / (tp + fn), 4) if tp + fn else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": fbeta(precision, recall, beta=1.0),
        "f2": fbeta(precision, recall, beta=2.0),
    }


def _row_f1(counts: Mapping[str, Any]) -> float:
    return _prf(
        int(counts.get("tp") or 0),
        int(counts.get("fp") or 0),
        int(counts.get("fn") or 0),
    )["f1"]


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _sd(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return round((sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5, 4)


def _expectation(row: Mapping[str, Any], *keys: str) -> bool | None:
    value = _get(row, *keys)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


# --------------------------------------------------------------------------- sorter


def _path_of(row: Mapping[str, Any]) -> str:
    mode = _get(row, "sort_mode", "path", "mode")
    mode = getattr(mode, "value", mode)
    text = str(mode or "").upper()
    if "SUBCLASS" in text:
        return "SUBCLASS_ONLY"
    if "FULL" in text:
        return "FULL"
    bert = _as_mapping(_get(row, "bert"))
    route = str(bert.get("route") or _get(row, "bert_route") or "").lower()
    return "SUBCLASS_ONLY" if route == "fast_path" else "FULL"


def _sorter_row(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    expected = str(_get(row, "expected", "expected_doc_type", "gt_doc_type") or "")
    predicted = str(_get(row, "doc_type", "predicted_doc_type", "prediction") or "")
    expected_sub = str(_get(row, "expected_subclass", "gt_subclass") or "")
    predicted_sub = str(_get(row, "doc_subclass", "predicted_subclass") or "")
    return expected, predicted, expected_sub, predicted_sub


def _sorter_block(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    exact = primary = subclass = 0
    expected_labels: list[str] = []
    predicted_labels: list[str] = []
    confidences: list[float] = []
    correct: list[int] = []
    for row in rows:
        expected, predicted, expected_sub, predicted_sub = _sorter_row(row)
        primary_ok = bool(expected) and expected == predicted
        sub_ok = bool(expected_sub) and expected_sub == predicted_sub
        primary += int(primary_ok)
        subclass += int(sub_ok)
        exact += int(primary_ok and sub_ok)
        expected_labels.append(expected)
        predicted_labels.append(predicted)
        confidence = _get(row, "sort_confidence", "confidence")
        if confidence is not None:
            try:
                confidences.append(float(confidence))
                correct.append(int(primary_ok))
            except (TypeError, ValueError):
                pass
    n = len(rows)

    def rate(count: int) -> float:
        return round(count / n, 4) if n else 0.0

    granted = sum(
        1
        for row in rows
        if _sorter_row(row)[1] == _sorter_row(row)[0] and _sorter_row(row)[0]
    )
    granted_sub = sum(
        1
        for row in rows
        if _sorter_row(row)[1] == _sorter_row(row)[0]
        and _sorter_row(row)[0]
        and _sorter_row(row)[2]
        and _sorter_row(row)[2] == _sorter_row(row)[3]
    )
    matrix, labels = confusion_matrix(expected_labels, predicted_labels)
    return {
        "n": n,
        "exact_match": rate(exact),
        "primary_accuracy": rate(primary),
        "subclass_accuracy": rate(subclass),
        "subclass_accuracy_given_primary": (
            round(granted_sub / granted, 4) if granted else 0.0
        ),
        "confusion": {"matrix": matrix, "labels": labels},
        "confidences": confidences,
        "correct": correct,
    }


def _ece(
    confidences: Sequence[float], correct: Sequence[int], bins: int = 10
) -> float | None:
    if not confidences:
        return None
    counts = [0] * bins
    conf_sum = [0.0] * bins
    correct_sum = [0.0] * bins
    for confidence, hit in zip(confidences, correct):
        bucket = min(int(confidence * bins), bins - 1)
        counts[bucket] += 1
        conf_sum[bucket] += confidence
        correct_sum[bucket] += hit
    total = len(confidences)
    error = 0.0
    for bucket in range(bins):
        if counts[bucket]:
            error += (
                counts[bucket]
                / total
                * abs(
                    correct_sum[bucket] / counts[bucket]
                    - conf_sum[bucket] / counts[bucket]
                )
            )
    return round(error, 4)


def sorter_kpis(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Sorter classification KPIs over ``eval_docs``-shaped rows."""
    rows = [dict(row) for row in rows]
    overall = _sorter_block(rows)

    by_path: dict[str, Any] = {}
    for path in ("SUBCLASS_ONLY", "FULL"):
        subset = [row for row in rows if _path_of(row) == path]
        block = _sorter_block(subset)
        by_path[path] = {
            "n": block["n"],
            "exact_match": block["exact_match"],
            "primary_accuracy": block["primary_accuracy"],
            "subclass_accuracy": block["subclass_accuracy"],
            "subclass_accuracy_given_primary": block["subclass_accuracy_given_primary"],
            "resort_rate": _resort_rate(subset),
        }

    resort = _resort_rate(rows)
    fast = [row for row in rows if _path_of(row) == "SUBCLASS_ONLY"]
    fast_block = _sorter_block(fast)
    n = len(rows)
    return {
        "n": n,
        "exact_match": overall["exact_match"],
        "primary_accuracy": overall["primary_accuracy"],
        "subclass_accuracy": overall["subclass_accuracy"],
        "subclass_accuracy_given_primary": overall["subclass_accuracy_given_primary"],
        "confusion": overall["confusion"],
        "bert": {
            "fast_path_rate": round(len(fast) / n, 4) if n else 0.0,
            "defer_rate": round((n - len(fast)) / n, 4) if n else 0.0,
            "fast_path_primary_accuracy": fast_block["primary_accuracy"],
        },
        "by_path": by_path,
        "resort_rate": resort,
        "ece": _ece(overall["confidences"], overall["correct"]),
    }


def _resort_rate(rows: Sequence[Mapping[str, Any]]) -> float:
    if not rows:
        return 0.0
    count = 0
    for row in rows:
        flag = _get(row, "resorted")
        if flag is not None:
            count += int(bool(flag))
            continue
        trail = _get(row, "route_trail")
        if isinstance(trail, str) and trail.strip():
            try:
                trail = json.loads(trail)
            except ValueError:
                trail = [trail]
        if any(step in ("re_sort", "resort") for step in (trail or [])):
            count += 1
    return round(count / len(rows), 4)


# --------------------------------------------------------------------------- specialist


def _clause_block(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tp = sum(int(r.get("cuad_tp") or 0) for r in rows)
    fp = sum(int(r.get("cuad_fp") or 0) for r in rows)
    fn = sum(int(r.get("cuad_fn") or 0) for r in rows)
    prf = _prf(tp, fp, fn)
    docs_labeled = sum(
        1
        for r in rows
        if r.get("cuad_tp") is not None
        or r.get("cuad_fp") is not None
        or r.get("cuad_fn") is not None
    )
    return {
        "kind": "cuad",
        "precision": prf["precision"] if tp + fp else None,
        "recall": prf["recall"] if tp + fn else None,
        "f1": prf["f1"] if tp + fp or tp + fn else None,
        "docs_labeled": docs_labeled,
        "value_checked": sum(int(r.get("cuad_value_checked") or 0) for r in rows),
        "value_correct": sum(int(r.get("cuad_value_correct") or 0) for r in rows),
    }


def _maud_block(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    questions = sum(int(r.get("maud_questions") or 0) for r in rows)
    answered = sum(int(r.get("maud_answered") or 0) for r in rows)
    correct = sum(int(r.get("maud_correct") or 0) for r in rows)
    return {
        "kind": "maud",
        "questions": questions,
        "answered": answered,
        "correct": correct,
        "accuracy": round(correct / questions, 4) if questions else None,
        "coverage": round(answered / questions, 4) if questions else None,
        "precision_answered": round(correct / answered, 4) if answered else None,
    }


def _agreement(row: Mapping[str, Any]) -> tuple[int, int]:
    """(agreeing fields, compared fields) for one row's judge vs scorer."""
    findings = _get(row, "judge_fields")
    if isinstance(findings, str) and findings.strip():
        try:
            findings = json.loads(findings)
        except ValueError:
            findings = None
    scores = _as_mapping(_get(row, "field_scores", "scores"))
    verdicts = {
        str(item.get("field")): str(item.get("verdict"))
        for item in (findings or [])
        if isinstance(item, Mapping)
    }
    agree = total = 0
    for field, score in scores.items():
        verdict = verdicts.get(str(field))
        if verdict is None:
            continue
        try:
            value = float(score)
        except (TypeError, ValueError):
            continue
        judged_ok = verdict in {"correct", "partial"}
        scorer_ok = value >= MATCH_THRESHOLD
        total += 1
        agree += int(judged_ok == scorer_ok)
    return agree, total


def specialist_kpis(
    rows: Sequence[Mapping[str, Any]], doc_type: str | None = None
) -> dict[str, Any]:
    """Per-class specialist KPIs over ``eval_docs``-shaped rows."""
    all_rows = [dict(row) for row in rows]
    if doc_type is not None:
        selected = [row for row in all_rows if row.get("doc_type") == doc_type]
    else:
        selected = all_rows

    counts = [_row_counts(row, doc_type, MATCH_THRESHOLD) for row in selected]
    tp = sum(c["tp"] for c in counts)
    fp = sum(c["fp"] for c in counts)
    fn = sum(c["fn"] for c in counts)
    pooled = _prf(tp, fp, fn)

    doc_f1 = [_row_f1(c) for c in counts]
    overall_scores = [
        float(row["overall_score"])
        for row in selected
        if row.get("overall_score") is not None
    ]
    suite = overall_scores or doc_f1

    schema_values = [
        bool(row.get("schema_valid")) for row in selected if "schema_valid" in row
    ]
    error_kinds = Counter(
        str(row.get("error_kind")) for row in selected if row.get("error_kind")
    )

    agree = total_fields = 0
    for row in selected:
        a, t = _agreement(row)
        agree += a
        total_fields += t

    return {
        "doc_type": doc_type,
        "n": len(selected),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": pooled["precision"],
        "recall": pooled["recall"],
        "f1": pooled["f1"],
        "f2": pooled["f2"],
        "micro_f1": pooled["f1"],
        "mean_doc_f1": _mean(doc_f1),
        "suite_mean": _mean(suite),
        "sd": _sd(suite),
        "min": min(suite) if suite else None,
        "max": max(suite) if suite else None,
        "schema_valid_rate": (
            round(sum(schema_values) / len(schema_values), 4) if schema_values else None
        ),
        "parse_errors": sum(1 for row in selected if row.get("parse_error")),
        "error_kinds": dict(error_kinds),
        "clause": _clause_block(selected),
        "maud": _maud_block(selected),
        "judge_scorer_agreement": (
            round(agree / total_fields, 4) if total_fields else None
        ),
    }


# --------------------------------------------------------------------------- gate


def gate_kpis(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Gate decision mix per stage plus agreement with the dataset contract.

    ``retry_expected`` is agreed when the gate took any non-proceed action at
    the classify stage (retry or re_sort); ``review_expected`` when it parked
    (human_review). ``expected_stage`` is agreed when the extract stage either
    ran (``extract``) or parked (``park``/``human_review``) as the label says.
    """
    rows = [dict(row) for row in rows]
    mix: dict[str, dict[str, int]] = {
        "classify": {},
        "extract": {},
    }
    agree = {
        "retry": 0,
        "retry_n": 0,
        "review": 0,
        "review_n": 0,
        "stage": 0,
        "stage_n": 0,
    }
    for row in rows:
        decisions = _as_mapping(_get(row, "gate_decisions"))
        features = _as_mapping(_get(row, "gate_features"))
        classify = decisions.get("classify") or features.get("classify", {}).get(
            "action"
        )
        extract = decisions.get("extract") or features.get("extract", {}).get("action")
        if classify:
            mix["classify"][str(classify)] = mix["classify"].get(str(classify), 0) + 1
        if extract:
            mix["extract"][str(extract)] = mix["extract"].get(str(extract), 0) + 1

        retry_expected = _expectation(row, "retry_expected")
        if retry_expected is not None and classify is not None:
            agree["retry_n"] += 1
            agree["retry"] += int(
                retry_expected == (str(classify) in {"retry", "re_sort"})
            )
        review_expected = _expectation(row, "review_expected")
        if review_expected is not None and extract is not None:
            agree["review_n"] += 1
            agree["review"] += int(review_expected == (str(extract) == "human_review"))
        expected_stage = _get(row, "expected_stage")
        stage = _get(row, "stage", "output_stage")
        if expected_stage is not None and stage is not None:
            agree["stage_n"] += 1
            agree["stage"] += int(str(expected_stage) == str(stage))

    def rate(hits: str, count: str) -> float | None:
        return round(agree[hits] / agree[count], 4) if agree[count] else None

    return {
        "n": len(rows),
        "decisions": mix,
        "agreement": {
            "retry_expected": rate("retry", "retry_n"),
            "review_expected": rate("review", "review_n"),
            "expected_stage": rate("stage", "stage_n"),
        },
    }
