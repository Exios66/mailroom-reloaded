"""Jev calibration contracts (leakage guard, thresholds, neutral empty input)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from mailroom_reloaded.eval.jev_calibration import (
    JevCalibration,
    fit_jev_calibration,
    load_jev_calibration,
)
from mailroom_reloaded.eval.train_gate import _logit, _nll


def _rows(n: int = 200) -> list[dict]:
    """Overconfident rows whose correctness is independent of confidence."""
    return [
        {
            "split": "train",
            "confidence": 0.9 + 0.0009 * (i % 100),
            "correct": i % 2,
        }
        for i in range(n)
    ]


def _nll_at(rows: list[dict], temperature: float) -> float:
    logits = np.asarray([_logit(row["confidence"]) for row in rows], dtype=float)
    y = np.asarray([int(bool(row["correct"])) for row in rows], dtype=float)
    return _nll(temperature, logits, y)


def test_fit_writes_and_round_trips(tmp_path):
    out = tmp_path / "models" / "jev_calibration.json"
    rows = _rows()

    result = fit_jev_calibration(rows, out)

    assert result["n"] == 200
    # Temperature scaling minimises NLL (not ECE); assert the actual objective.
    assert _nll_at(rows, result["temperature"]) <= _nll_at(rows, 1.0) + 1e-9
    assert 0.0 <= result["verify_threshold"] <= result["accept_threshold"] <= 1.0
    assert json.loads(out.read_text(encoding="utf-8")) == result

    loaded = load_jev_calibration(out)
    assert loaded.n == 200
    assert loaded.temperature == pytest.approx(result["temperature"])
    assert loaded.accept_threshold == pytest.approx(result["accept_threshold"])
    assert loaded.verify_threshold == pytest.approx(result["verify_threshold"])


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), float("-inf"), -0.1, 1.5],
)
def test_fit_rejects_non_finite_or_out_of_range_confidence(tmp_path, bad):
    out = tmp_path / "calibration.json"

    with pytest.raises(ValueError, match="confidence"):
        fit_jev_calibration([{"split": "train", "confidence": bad, "correct": 1}], out)

    assert not out.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"temperature": 0.0},
        {"temperature": float("nan")},
        {"accept_threshold": 0.4, "verify_threshold": 0.6},
        {"accept_threshold": 1.5},
    ],
)
def test_load_rejects_invariant_violations(tmp_path, overrides):
    payload = {
        "temperature": 1.0,
        "accept_threshold": 0.8,
        "verify_threshold": 0.5,
        "ece_before": 0.0,
        "ece_after": 0.0,
        "n": 1,
        **overrides,
    }
    out = tmp_path / "calibration.json"
    out.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_jev_calibration(out)


def test_fit_rejects_non_train_split(tmp_path):
    out = tmp_path / "calibration.json"

    with pytest.raises(ValueError, match="split='train' only"):
        fit_jev_calibration(
            [{"split": "test", "confidence": 0.9, "correct": 1}], out
        )

    assert not out.exists()


def test_fit_empty_writes_neutral(tmp_path):
    out = tmp_path / "calibration.json"

    result = fit_jev_calibration([], out)

    assert result == {
        "temperature": 1.0,
        "accept_threshold": 0.8,
        "verify_threshold": 0.5,
        "ece_before": 0.0,
        "ece_after": 0.0,
        "n": 0,
    }
    loaded = load_jev_calibration(out)
    assert loaded == JevCalibration(
        temperature=1.0,
        accept_threshold=0.8,
        verify_threshold=0.5,
        ece_before=0.0,
        ece_after=0.0,
        n=0,
    )


@pytest.mark.parametrize("missing", ["confidence", "correct"])
def test_fit_missing_required_key_raises(tmp_path, missing):
    row = {"split": "train", "confidence": 0.9, "correct": 1}
    del row[missing]
    out = tmp_path / "calibration.json"

    with pytest.raises(KeyError, match=missing):
        fit_jev_calibration([row], out)

    assert not out.exists()
