"""Fit the Jev (TypeSafe System One) confidence calibration (issue #8).

Fitting is restricted to ``split="train"`` rows, reusing the leakage guard from
:mod:`mailroom_reloaded.eval.train_gate`; a missing or other split raises
``ValueError``. Rows are plain dicts with ``confidence`` (the Jev
confidence/probability) and ``correct`` (0/1).

The fit reuses the binary-NLL temperature helpers from ``train_gate`` and then
searches two operating points in *calibrated* confidence space to separate
correct from incorrect answers:

* ``accept_threshold`` — above it Jev's chosen action is trusted;
* ``verify_threshold`` — the lower edge of the uncertainty band (``<=`` accept).

The artifact is a flat, clear JSON object written to
``models/jev_calibration.json`` and read back by ``agents.jev.load_jev_gate``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np

from mailroom_reloaded.agents.gate import ece
from mailroom_reloaded.eval.train_gate import (
    _check_train,
    _fit_temperature,
    _logit,
    _sigmoid,
    _write_json,
)

__all__ = ["JevCalibration", "fit_jev_calibration", "load_jev_calibration"]

# Neutral calibration for empty input: no scaling, default operating points.
_NEUTRAL: dict[str, float] = {
    "temperature": 1.0,
    "accept_threshold": 0.8,
    "verify_threshold": 0.5,
}


@dataclass(frozen=True)
class JevCalibration:
    """Fitted temperature and operating points for the Jev gate."""

    temperature: float
    accept_threshold: float
    verify_threshold: float
    ece_before: float
    ece_after: float
    n: int


def load_jev_calibration(path: str | Path) -> JevCalibration:
    """Read a ``jev_calibration.json`` written by :func:`fit_jev_calibration`."""
    data = json.loads(Path(path).read_text("utf-8"))
    return JevCalibration(
        temperature=float(data["temperature"]),
        accept_threshold=float(data["accept_threshold"]),
        verify_threshold=float(data["verify_threshold"]),
        ece_before=float(data["ece_before"]),
        ece_after=float(data["ece_after"]),
        n=int(data["n"]),
    )


def _candidates(values: np.ndarray) -> list[float]:
    """Threshold candidates: endpoints, observed values, and their midpoints."""
    uniq = sorted({float(v) for v in values})
    cand = {0.0, 1.0}
    cand.update(uniq)
    cand.update((a + b) / 2.0 for a, b in pairwise(uniq))
    return sorted(min(max(c, 0.0), 1.0) for c in cand)


def _balanced_accuracy(q: np.ndarray, y: np.ndarray, t: float) -> float:
    """Balanced accuracy of the rule ``correct if q >= t``."""
    pred = q >= t
    pos, neg = y == 1, y == 0
    tpr = float(pred[pos].mean()) if pos.any() else 0.0
    tnr = float((~pred[neg]).mean()) if neg.any() else 0.0
    return 0.5 * (tpr + tnr)


def _search_thresholds(q: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Return ``(accept, verify)`` thresholds maximizing balanced accuracy.

    Both are points on the best-accuracy plateau; ``accept`` is its upper edge
    and ``verify`` its lower edge, so ``verify <= accept`` always holds. With a
    single label class or no rows the neutral defaults are returned.
    """
    if q.size == 0 or np.unique(y).size < 2:
        return _NEUTRAL["accept_threshold"], _NEUTRAL["verify_threshold"]
    cand = _candidates(q)
    scores = [_balanced_accuracy(q, y, t) for t in cand]
    best = max(scores)
    tied = [t for t, s in zip(cand, scores) if s >= best - 1e-12]
    return float(tied[-1]), float(tied[0])


def fit_jev_calibration(rows, out: Path) -> dict:
    """Fit Jev temperature scaling and thresholds; write ``out`` and return it.

    Every row must have ``split == 'train'``; otherwise ``ValueError`` is raised
    before anything is written. Missing ``confidence``/``correct`` keys raise
    ``KeyError``. Empty input writes a neutral calibration and returns zeroed
    ECE. The returned dict has ``temperature``, ``accept_threshold``,
    ``verify_threshold``, ``ece_before``, ``ece_after`` and ``n``.
    """
    _check_train(rows)
    n = len(rows)
    if n == 0:
        payload: dict = {**_NEUTRAL, "ece_before": 0.0, "ece_after": 0.0, "n": 0}
        _write_json(out, payload)
        return payload

    conf = np.asarray([float(row["confidence"]) for row in rows], dtype=float)
    y = np.asarray([int(bool(row["correct"])) for row in rows], dtype=int)
    logits = np.asarray([_logit(v) for v in conf], dtype=float)
    temperature = _fit_temperature(logits, y)
    calibrated = np.asarray([_sigmoid(_logit(v) / temperature) for v in conf], dtype=float)
    accept, verify = _search_thresholds(calibrated, y)

    payload = {
        "temperature": float(temperature),
        "accept_threshold": float(accept),
        "verify_threshold": float(verify),
        "ece_before": ece(conf, y),
        "ece_after": ece(calibrated, y),
        "n": n,
    }
    _write_json(out, payload)
    return payload
