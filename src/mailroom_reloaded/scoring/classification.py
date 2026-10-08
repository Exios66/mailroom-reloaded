"""Classification scorers: normalization, exact match, per-class stats,
confusion matrices, and binary metrics.

Ported from ``src/scorers.py`` (llm-entity-extraction) and generalized so the
valid-class table is configurable. All functions are deterministic pure
functions over ``(predicted, expected)`` pairs so Braintrust-scorer lookups,
local manifest scoring, and post-hoc analysis never disagree.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from .config import RETIRED_DOC_CLASS_KEYS

ERROR_PREFIX = "ERROR: "  # task output sentinel for failed rows

# Default doc-class keys + regexes for label normalization (the
# llm-entity-extraction / llm-mailroom taxonomy). Most-specific keys first
# so broad patterns (e.g. ``contract``) do not win over ``merger_agreement``.
_DEFAULT_CLASSES_RE: dict[str, re.Pattern[str]] = {
    "merger_agreement": re.compile(r"\bmerger[_ ]?agreement\b"),
    "corporate_record": re.compile(r"\bcorporate[_ ]?record\b"),
    "correspondence": re.compile(r"\bcorrespondence\b"),
    "compliance_filing": re.compile(r"\bcompliance[_ ]?filing\b"),
    "insurance_claim": re.compile(r"\binsurance[_ ]?claim\b"),
    "contract": re.compile(r"\bcontract\b"),
}


def _default_valid_classes() -> dict[str, re.Pattern[str]]:
    retired = set(RETIRED_DOC_CLASS_KEYS)
    return {k: v for k, v in _DEFAULT_CLASSES_RE.items() if k not in retired}


def normalize_label(value, valid: dict[str, re.Pattern[str]] | None = None) -> str:
    """Coerce an LLM output into a class key (best effort)."""
    valid = valid or _default_valid_classes()
    if value is None:
        return ""
    text = str(value).strip().lower()
    # Prefer a JSON object's doc_type field.
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            text = str(obj.get("doc_type") or text).lower()
        except json.JSONDecodeError:
            pass
    # Exact match.
    if text in valid:
        return text
    for cls, pattern in valid.items():
        if pattern.search(text):
            return cls
    return text.strip('"`*_ ')


def exact_match(output, expected) -> float:
    """Score 1.0 if the prediction matches the expected class, else 0.0."""
    return 1.0 if normalize_label(output) == normalize_label(expected) else 0.0


def failure(output, expected) -> float:
    """Score 1.0 for rows the model failed to classify (error sentinel)."""
    return 1.0 if str(output).startswith(ERROR_PREFIX) else 0.0


def accuracy(expected: list, predicted: list) -> float:
    """Overall exact-match accuracy over paired predictions."""
    if not expected:
        return 0.0
    pairs: list[tuple] = []
    for e, p in zip(expected, predicted):
        if str(p).startswith(ERROR_PREFIX):
            continue
        pairs.append((e, p))
    if not pairs:
        return 0.0
    hits = sum(
        1 for e, p in pairs if normalize_label(p) == normalize_label(e)
    )
    return round(hits / len(pairs), 4)


def fbeta(precision: float, recall: float, *, beta: float = 1.0) -> float:
    """van Rijsbergen F-beta. ``beta=1`` is F1; ``beta=2`` recall-weights F2."""
    if precision <= 0.0 and recall <= 0.0:
        return 0.0
    b2 = beta * beta
    denom = b2 * precision + recall
    if denom == 0.0:
        return 0.0
    return round((1.0 + b2) * precision * recall / denom, 4)


def per_class_stats(expected: list, predicted: list) -> dict[str, dict]:
    """Aggregate exact-match accuracy plus one-vs-rest P/R/F1 per expected class.

    Returns ``{class: {n, correct, accuracy, precision, recall, f1, f2}}``.
    Failed rows (``ERROR_PREFIX`` predictions) are skipped entirely.
    """
    pairs: list[tuple[str, str]] = []
    for e, p in zip(expected, predicted):
        out = str(p)
        if out.startswith(ERROR_PREFIX):
            continue
        pairs.append((normalize_label(e), normalize_label(out)))
    by_class: dict[str, dict] = {}
    labels = sorted({e for e, _ in pairs})
    for cls in labels:
        n = sum(1 for e, _ in pairs if e == cls)
        correct = sum(1 for e, p in pairs if e == cls and p == cls)
        tp = correct
        fp = sum(1 for e, p in pairs if e != cls and p == cls)
        fn = sum(1 for e, p in pairs if e == cls and p != cls)
        precision = round(tp / (tp + fp), 4) if tp + fp else 0.0
        recall = round(tp / (tp + fn), 4) if tp + fn else 0.0
        by_class[cls] = {
            "n": n,
            "correct": correct,
            "accuracy": round(correct / n, 4) if n else 0.0,
            "precision": precision,
            "recall": recall,
            "f1": fbeta(precision, recall, beta=1.0),
            "f2": fbeta(precision, recall, beta=2.0),
        }
    return by_class


def macro_accuracy(expected: list, predicted: list) -> float:
    """Unweighted mean of per-class accuracies (ignores empty classes)."""
    stats = per_class_stats(expected, predicted)
    if not stats:
        return 0.0
    return round(sum(s["accuracy"] for s in stats.values()) / len(stats), 4)


def confusion_matrix(expected: list, predicted: list,
                     labels: list[str] | None = None) -> tuple[list[list[int]], list[str]]:
    """Expected x predicted counts.

    Rows are expected (ground-truth) classes, columns predicted classes, in
    ``labels`` order (default: sorted union of observed classes). Failed
    predictions (ERROR_PREFIX) are dropped.
    """
    pairs = [
        (normalize_label(e), normalize_label(p))
        for e, p in zip(expected, predicted)
        if not str(p).startswith(ERROR_PREFIX)
    ]
    if labels is None:
        labels = sorted({e for e, _ in pairs} | {p for _, p in pairs})
    else:
        labels = list(labels)
        extra = sorted(({e for e, _ in pairs} | {p for _, p in pairs}) - set(labels))
        labels.extend(extra)
    index = {label: i for i, label in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for e, p in pairs:
        matrix[index[e]][index[p]] += 1
    return matrix, labels


def binary_metrics(expected: list, predicted: list, positive: str,
                   normalize: bool = True) -> dict:
    """Precision / recall / F1 for a one-vs-rest binary view.

    ``normalize=True`` runs labels through :func:`normalize_label` (exact
    class keys); ``normalize=False`` treats inputs as raw binary flags
    (True/False, 1/0, "yes"/"no").
    """
    def _flag(value):
        if not normalize:
            if isinstance(value, str):
                return value.strip().lower() in ("yes", "y", "true", "1", "1.0")
            return bool(value)
        return normalize_label(value) == positive

    tp = tn = fp = fn = 0
    for e, p in zip(expected, predicted):
        is_pos = _flag(e)
        pred_pos = _flag(p)
        if is_pos and pred_pos:
            tp += 1
        elif is_pos and not pred_pos:
            fn += 1
        elif not is_pos and pred_pos:
            fp += 1
        else:
            tn += 1
    precision = round(tp / (tp + fp), 4) if tp + fp else 0.0
    recall = round(tp / (tp + fn), 4) if tp + fn else 0.0
    f1 = fbeta(precision, recall, beta=1.0)
    f2 = fbeta(precision, recall, beta=2.0)
    total = tp + tn + fp + fn
    return {
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1, "f2": f2,
        "accuracy": round((tp + tn) / total, 4) if total else 0.0,
        "false_positive_rate": round(fp / (fp + tn), 4) if fp + tn else 0.0,
        "false_negative_rate": round(fn / (fn + tp), 4) if fn + tp else 0.0,
    }


def macro_prf(expected: list, predicted: list, *, normalize: bool = True) -> dict:
    """Unweighted mean of one-vs-rest P/R/F1/F2 over labels in *expected*.

    Standard multiclass macro-average for imbalanced doc-type / subclass
    catalogs (sorter T0 ``f1_macro``). Failed ``ERROR_PREFIX`` rows are skipped.
    """
    pairs: list[tuple[str, str]] = []
    for e, p in zip(expected, predicted):
        if str(p).startswith(ERROR_PREFIX):
            continue
        ev = normalize_label(e) if normalize else str(e)
        pv = normalize_label(p) if normalize else str(p)
        pairs.append((ev, pv))
    labels = sorted({e for e, _ in pairs if e})
    empty = {
        "precision_macro": 0.0,
        "recall_macro": 0.0,
        "f1_macro": 0.0,
        "f2_macro": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "f2": 0.0,
        "per_class": {},
        "n": len(pairs),
        "n_classes": 0,
    }
    if not labels:
        return empty
    exp = [e for e, _ in pairs]
    pred = [p for _, p in pairs]
    if normalize:
        per_class = per_class_stats(exp, pred)
        per_class = {k: v for k, v in per_class.items() if k in set(labels)}
    else:
        per_class = {}
        for cls in labels:
            n = sum(1 for e, _ in pairs if e == cls)
            correct = sum(1 for e, p in pairs if e == cls and p == cls)
            tp = correct
            fp = sum(1 for e, p in pairs if e != cls and p == cls)
            fn = sum(1 for e, p in pairs if e == cls and p != cls)
            precision = round(tp / (tp + fp), 4) if tp + fp else 0.0
            recall = round(tp / (tp + fn), 4) if tp + fn else 0.0
            per_class[cls] = {
                "n": n,
                "correct": correct,
                "accuracy": round(correct / n, 4) if n else 0.0,
                "precision": precision,
                "recall": recall,
                "f1": fbeta(precision, recall, beta=1.0),
                "f2": fbeta(precision, recall, beta=2.0),
            }
    n_cls = len(per_class)
    if not n_cls:
        return empty
    precision_macro = round(sum(s["precision"] for s in per_class.values()) / n_cls, 4)
    recall_macro = round(sum(s["recall"] for s in per_class.values()) / n_cls, 4)
    f1_macro = round(sum(s["f1"] for s in per_class.values()) / n_cls, 4)
    f2_macro = round(sum(s["f2"] for s in per_class.values()) / n_cls, 4)
    return {
        "precision_macro": precision_macro,
        "recall_macro": recall_macro,
        "f1_macro": f1_macro,
        "f2_macro": f2_macro,
        "precision": precision_macro,
        "recall": recall_macro,
        "f2": f2_macro,
        "per_class": per_class,
        "n": len(pairs),
        "n_classes": n_cls,
    }


def confusion_accuracy(matrix: list[list[int]]) -> float:
    """Trace-over-total accuracy from a square confusion matrix."""
    total = sum(sum(row) for row in matrix)
    if not total:
        return 0.0
    trace = sum(matrix[i][i] for i in range(len(matrix)))
    return round(trace / total, 4)


def top_confusions(matrix: list[list[int]], labels: list[str],
                   k: int = 5) -> list[dict]:
    """The k most frequent off-diagonal (expected, predicted) pairs."""
    off = []
    for i, row in enumerate(matrix):
        for j, count in enumerate(row):
            if i != j and count > 0:
                off.append({"expected": labels[i], "predicted": labels[j], "count": count})
    off.sort(key=lambda d: d["count"], reverse=True)
    return off[:k]


def class_distribution(labels: list) -> dict[str, int]:
    """Counts per normalized label (useful for support tables)."""
    return dict(Counter(normalize_label(x) for x in labels))


__all__ = [
    "ERROR_PREFIX", "normalize_label", "exact_match", "failure", "accuracy",
    "fbeta", "per_class_stats", "macro_accuracy", "macro_prf",
    "confusion_matrix",
    "binary_metrics", "confusion_accuracy", "top_confusions",
    "class_distribution",
]
