"""Fit the route gate's calibration and learned logistic models (Task 13).

Fitting is restricted to ``split="train"`` rows; missing or other split values
raise ``ValueError`` to prevent leakage (spec section 6, "No leakage"). KPIs
are reported separately on the ``test`` split only.

Row schemas (plain dicts):

* calibration: ``{split, provider, model, doc_type, confidence, correct}`` where
  ``confidence`` is the raw sorter confidence and ``correct`` is 0/1.
* gate: ``{split, stage, doc_type?, confidence, attempts, bert_confidence,
  bert_margin, bert_window_agreement, schema_valid, field_coverage,
  length_capped, retry_expected|review_expected}``. The label is
  ``retry_expected`` for ``stage="classify"`` and ``review_expected`` for
  ``stage="extract"``.

``fit_calibration`` writes the canonical nested
``{provider: {model: {doc_type: temperature}}}`` layout that
``agents.sorter.load_calibration`` reads. ``train_gate`` writes
``{stage: {features, coef, intercept, threshold}}`` that
``agents.gate.LearnedGate`` reads.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from mailroom_reloaded.agents.gate import (
    NUMERIC_FEATURES,
    GateFeatures,
    ece,
    feature_vector,
)
from mailroom_reloaded.settings import load_taxonomy

__all__ = ["fit_calibration", "train_gate"]

_EPS = 1e-6
_LOG_T_RANGE = (-3.0, 3.0)  # temperature in ~[0.05, 20]
_GOLDEN = (math.sqrt(5.0) - 1.0) / 2.0


def _check_train(rows: list[dict]) -> None:
    """Raise ValueError for any ``split='test'`` row; allow all other splits."""
    for row in rows:
        split = row.get("split")
        if split != "train":
            raise ValueError(
                f"refusing to fit on split={split!r} rows; the gate and calibration "
                "are fitted on split='train' only"
            )


def _logit(p: float) -> float:
    """Return log-odds after clamping the probability to [1e-6, 1 - 1e-6]."""
    p = min(max(float(p), _EPS), 1.0 - _EPS)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    """Convert log-odds to a probability; propagate OverflowError from exp."""
    return 1.0 / (1.0 + math.exp(-x))


def _nll(temperature: float, logits: np.ndarray, y: np.ndarray) -> float:
    """Return mean binary negative log-likelihood for temperature-scaled logits.

    ``y`` contains the corresponding 0/1 labels. Probabilities are clipped to
    [1e-6, 1 - 1e-6] before taking natural logs; empty inputs yield NaN.
    """
    z = np.clip(logits / temperature, -500.0, 500.0)
    p = 1.0 / (1.0 + np.exp(-z))
    p = np.clip(p, _EPS, 1.0 - _EPS)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def _minimize_1d(fn, lo: float, hi: float, iters: int = 200) -> float:
    """Golden-section minimisation of a unimodal ``fn`` on ``[lo, hi]``."""
    a, b = lo, hi
    c = b - _GOLDEN * (b - a)
    d = a + _GOLDEN * (b - a)
    fc, fd = fn(c), fn(d)
    for _ in range(iters):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - _GOLDEN * (b - a)
            fc = fn(c)
        else:
            a, c, fc = c, d, fd
            d = a + _GOLDEN * (b - a)
            fd = fn(d)
    return (a + b) / 2.0


def _fit_temperature(logits: np.ndarray, y: np.ndarray) -> float:
    """Fit temperature in [exp(-3), exp(3)] by binary NLL minimization.

    ``y`` contains the 0/1 labels corresponding to ``logits``. Return 1.0
    when there are fewer than two logits or fewer than two distinct labels.
    """
    if logits.size < 2 or np.unique(y).size < 2:
        return 1.0
    u = _minimize_1d(lambda u: _nll(math.exp(u), logits, y), *_LOG_T_RANGE)
    return float(math.exp(u))


def _write_json(out: Path, data: dict) -> None:
    """Create parent directories and overwrite ``out`` with UTF-8 JSON.

    Serialization errors and filesystem OSError exceptions propagate.
    """
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")


def fit_calibration(rows, out: Path) -> dict:
    """Fit per (provider, model, doc_type) temperature scaling by NLL.

    Writes the ``{provider: {model: {doc_type: T}}}`` layout read by
    ``agents.sorter.load_calibration`` and returns ECE before and after
    calibration (global over all rows), plus the fitted temperatures.

    ``rows`` must be reusable across iterations, with raw ``confidence``
    probabilities and ``correct`` labels interpreted by truth value. All rows
    are used except that any ``split='test'`` row raises ValueError; missing
    or other splits are accepted. Groups with fewer than two rows or only
    one label class use temperature 1.0. Empty input writes an empty mapping
    and returns zero ECE values.

    Creates parent directories and overwrites ``out``. Missing confidence or
    correct keys raise KeyError; numeric conversion errors and filesystem
    OSError exceptions propagate.
    """
    _check_train(rows)

    groups: dict[tuple[str, str, str], list[tuple[float, int]]] = {}
    for row in rows:
        key = (
            str(row.get("provider", "")),
            str(row.get("model", "")),
            str(row.get("doc_type") or ""),
        )
        groups.setdefault(key, []).append(
            (float(row["confidence"]), int(bool(row["correct"])))
        )

    nested: dict[str, dict[str, dict[str, float]]] = {}
    conf_before: list[float] = []
    conf_after: list[float] = []
    correct: list[int] = []
    for (provider, model, doc_type), pairs in groups.items():
        p = np.asarray([a for a, _ in pairs], dtype=float)
        y = np.asarray([b for _, b in pairs], dtype=float)
        logits = np.asarray([_logit(v) for v in p], dtype=float)
        temperature = _fit_temperature(logits, y)
        for value, label in pairs:
            conf_before.append(value)
            conf_after.append(_sigmoid(_logit(value) / temperature))
            correct.append(label)
        nested.setdefault(provider, {}).setdefault(model, {})[doc_type] = temperature

    _write_json(out, nested)
    return {
        "ece_before": ece(conf_before, correct),
        "ece_after": ece(conf_after, correct),
        "temperatures": nested,
    }


def _gate_feature_names() -> list[str]:
    """Return numeric feature names followed by taxonomy-ordered one-hot names.

    Errors reading, parsing, or validating the taxonomy propagate.
    """
    names = list(NUMERIC_FEATURES)
    names += [f"doc_type={c}" for c in load_taxonomy().classes]
    return names


def _row_vector(names: list[str], row: dict) -> np.ndarray:
    """Encode a row in ``names`` order, with one-hot ``doc_type=...`` features.

    Missing numeric values default to zero except ``field_coverage`` (1.0);
    ``schema_valid`` defaults to True and ``length_capped`` to False. A missing
    or unmatched document type leaves its one-hot entries zero. Missing stage
    raises KeyError; conversion errors and unknown-feature AttributeError
    exceptions propagate.
    """
    f = GateFeatures(
        stage=row["stage"],
        doc_type=row.get("doc_type"),
        confidence=float(row.get("confidence", 0.0)),
        attempts=int(row.get("attempts", 0)),
        bert_confidence=float(row.get("bert_confidence", 0.0)),
        bert_margin=float(row.get("bert_margin", 0.0)),
        bert_window_agreement=float(row.get("bert_window_agreement", 0.0)),
        schema_valid=bool(row.get("schema_valid", True)),
        field_coverage=float(row.get("field_coverage", 1.0)),
        length_capped=bool(row.get("length_capped", False)),
    )
    return feature_vector(names, f)


def train_gate(rows: list[dict], out: Path) -> dict:
    """Fit one sklearn ``LogisticRegression`` per stage and write the JSON.

    Labels are ``retry_expected`` for classify and ``review_expected`` for
    extract. Writes ``{stage: {features, coef, intercept, threshold}}`` (the
    ``LearnedGate`` layout) and returns per-stage train metrics.

    Uses every supplied row, rejecting ``split='test'`` with ValueError but
    accepting missing or other splits. Labels are interpreted by truth value.
    Missing or unknown stages, missing stage labels, or fewer than two label
    classes per stage also raise ValueError. Numeric conversion, taxonomy
    loading, and sklearn fitting errors propagate; sklearn must be installed.

    Creates parent directories and overwrites ``out`` with models for the
    supplied stages, each with threshold 0.5. Filesystem OSError exceptions
    propagate. Metrics contain ``n``, ``positive_rate``, and ``train_accuracy``
    for each stage, measured on the fitting rows. Empty input writes and
    returns empty mappings.
    """
    _check_train(rows)
    from sklearn.linear_model import LogisticRegression  # dev extra

    names = _gate_feature_names()
    stages: list[str] = []
    for row in rows:
        stage = row.get("stage")
        if stage not in ("classify", "extract"):
            raise ValueError(f"unknown gate stage {stage!r}")
        if stage not in stages:
            stages.append(stage)

    models: dict[str, dict] = {}
    metrics: dict[str, dict] = {}
    for stage in stages:
        label_key = "retry_expected" if stage == "classify" else "review_expected"
        stage_rows = [r for r in rows if r["stage"] == stage]
        if any(label_key not in r for r in stage_rows):
            raise ValueError(f"rows for stage {stage!r} need a {label_key!r} label")
        x = np.vstack([_row_vector(names, r) for r in stage_rows])
        y = np.asarray([int(bool(r[label_key])) for r in stage_rows], dtype=int)
        if y.size < 2 or np.unique(y).size < 2:
            raise ValueError(f"stage {stage!r} needs at least two label classes")

        clf = LogisticRegression(max_iter=1000, C=1.0)
        clf.fit(x, y)
        threshold = 0.5
        models[stage] = {
            "features": names,
            "coef": [float(c) for c in clf.coef_[0]],
            "intercept": float(clf.intercept_[0]),
            "threshold": threshold,
        }
        metrics[stage] = {
            "n": int(y.size),
            "positive_rate": float(y.mean()),
            "train_accuracy": float((clf.predict(x) == y).mean()),
        }

    _write_json(out, models)
    return metrics
