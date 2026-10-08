"""Jev calibration contracts (leakage guard, thresholds, neutral empty input)."""

from __future__ import annotations

import json

import pytest

from mailroom_reloaded.eval.jev_calibration import (
    JevCalibration,
    fit_jev_calibration,
    load_jev_calibration,
)


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


def test_fit_writes_and_round_trips(tmp_path):
    out = tmp_path / "models" / "jev_calibration.json"

    result = fit_jev_calibration(_rows(), out)

    assert result["n"] == 200
    assert result["ece_after"] <= result["ece_before"]
    assert 0.0 <= result["verify_threshold"] <= result["accept_threshold"] <= 1.0
    assert json.loads(out.read_text(encoding="utf-8")) == result

    loaded = load_jev_calibration(out)
    assert loaded.n == 200
    assert loaded.temperature == pytest.approx(result["temperature"])
    assert loaded.accept_threshold == pytest.approx(result["accept_threshold"])
    assert loaded.verify_threshold == pytest.approx(result["verify_threshold"])


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
