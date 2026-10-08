"""Training contracts, leakage prevention, and compatibility with inference."""

import json
import math

import pytest

from mailroom_reloaded.agents.gate import BandGate, GateFeatures, LearnedGate
from mailroom_reloaded.agents.sorter import load_calibration
from mailroom_reloaded.eval.train_gate import fit_calibration, train_gate


@pytest.mark.parametrize("fit", [fit_calibration, train_gate])
def test_empty_training_overwrites_stale_model(tmp_path, fit):
    """Verify empty training replaces stale gate or calibration data with an empty model."""
    out = tmp_path / "models" / "model.json"
    out.parent.mkdir()
    out.write_text('{"stale": true}')

    result = fit([], out)

    assert json.loads(out.read_text()) == {}
    if fit is fit_calibration:
        assert result == {"ece_before": 0.0, "ece_after": 0.0, "temperatures": {}}
    else:
        assert result == {}


@pytest.mark.parametrize("fit", [fit_calibration, train_gate])
def test_rejected_training_preserves_existing_model(tmp_path, fit):
    """Verify non-training rows are rejected without replacing the model file."""
    out = tmp_path / "model.json"
    original = '{"previous": "model"}\n'
    out.write_text(original)
    with pytest.raises(ValueError, match="split='train' only"):
        fit([{"split": "test"}], out)
    assert out.read_text() == original


@pytest.mark.parametrize("stage", [None, "", "verify", "CLASSIFY"])
def test_unknown_training_stage_does_not_write(tmp_path, stage):
    """Verify missing or unsupported gate stages cannot create a model file."""
    out = tmp_path / "model.json"
    row = {"split": "train"}
    if stage is not None:
        row["stage"] = stage
    with pytest.raises(ValueError, match="unknown gate stage"):
        train_gate([row], out)
    assert not out.exists()


@pytest.mark.parametrize(
    "stage,label",
    [
        ("classify", "retry_expected"),
        ("extract", "review_expected"),
    ],
)
def test_training_requires_the_label_for_its_stage(tmp_path, stage, label):
    """Verify each gate stage requires its own target label."""
    wrong_label = "review_expected" if stage == "classify" else "retry_expected"
    rows = [{"split": "train", "stage": stage, wrong_label: value} for value in (0, 1)]
    out = tmp_path / "model.json"
    with pytest.raises(ValueError, match=label):
        train_gate(rows, out)
    assert not out.exists()


@pytest.mark.parametrize(
    "stage,label",
    [
        ("classify", "retry_expected"),
        ("extract", "review_expected"),
    ],
)
@pytest.mark.parametrize("labels", [[0], [1], [0, 0], [1, 1]])
def test_training_requires_two_label_classes(tmp_path, stage, label, labels):
    """Verify gate fitting rejects targets containing only one label class."""
    rows = [{"split": "train", "stage": stage, label: value} for value in labels]
    out = tmp_path / "model.json"
    with pytest.raises(ValueError, match="at least two label classes"):
        train_gate(rows, out)
    assert not out.exists()


def test_each_stage_learns_its_own_label_and_loads_for_inference(tmp_path):
    # Opposite labels for identical inputs detect accidental cross-stage pooling.
    """Verify opposite stage targets train independently and reload for routing."""
    rows = []
    for _ in range(10):
        for attempts in (0, 2):
            for stage in ("classify", "extract"):
                rows.append(
                    {
                        "split": "train",
                        "stage": stage,
                        "confidence": 0.91,
                        "doc_type": "correspondence",
                        "attempts": attempts,
                        "retry_expected": int(attempts == 2),
                        "review_expected": int(attempts == 0),
                    }
                )
    out = tmp_path / "nested" / "route_gate.json"

    metrics = train_gate(rows, out)
    gate = LearnedGate(BandGate(), out)

    for stage in ("classify", "extract"):
        assert metrics[stage] == {
            "n": 20,
            "positive_rate": 0.5,
            "train_accuracy": 1.0,
        }
    for stage, attempts, action in [
        ("classify", 0, "proceed"),
        ("classify", 2, "human_review"),
        ("extract", 0, "verify"),
        ("extract", 2, "proceed"),
    ]:
        decision = gate.decide(GateFeatures(stage, "correspondence", 0.91, attempts))
        assert decision.source == "model"
        assert decision.action == action


def test_invalid_later_stage_preserves_existing_model(tmp_path):
    """Verify a later stage's validation failure leaves the existing model intact."""
    out = tmp_path / "model.json"
    out.write_text('{"previous": true}')
    rows = [
        {"split": "train", "stage": "classify", "retry_expected": label}
        for label in (0, 1)
    ] + [{"split": "train", "stage": "extract", "review_expected": 1}]
    with pytest.raises(ValueError, match="at least two label classes"):
        train_gate(rows, out)
    assert json.loads(out.read_text()) == {"previous": True}


def test_calibration_groups_are_independent_and_loadable(tmp_path, monkeypatch):
    """Verify calibration stays separate by provider, model and document type."""
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    rows = [
        {
            "split": "train",
            "provider": provider,
            "model": model,
            "doc_type": doc_type,
            "confidence": 0.95,
            "correct": correct,
        }
        for provider, model, doc_type, labels in [
            ("mock", "model-a", "contract", [0, 1] * 10),
            ("mock", "model-a", "correspondence", [1, 1]),
            ("mock", "model-b", "contract", [0]),
            ("other", "model-a", "contract", [1]),
        ]
        for correct in labels
    ]
    out = tmp_path / "models" / "calibration.json"
    result = fit_calibration(rows, out)
    temperatures = result["temperatures"]
    assert json.loads(out.read_text()) == temperatures
    assert temperatures["mock"]["model-a"]["contract"] > 1.0
    assert temperatures["mock"]["model-a"]["correspondence"] == 1.0
    assert temperatures["mock"]["model-b"]["contract"] == 1.0
    assert temperatures["other"]["model-a"]["contract"] == 1.0
    for provider, models in temperatures.items():
        for model, classes in models.items():
            for doc_type, temperature in classes.items():
                assert load_calibration(provider, model, doc_type) == temperature
    assert load_calibration("missing", "model-a", "contract") is None


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_calibration_probability_endpoints_stay_finite(tmp_path, confidence):
    """Verify confidence endpoints produce finite, bounded calibration results."""
    rows = [
        {"split": "train", "confidence": confidence, "correct": correct}
        for correct in (0, 1)
    ]
    result = fit_calibration(rows, tmp_path / "calibration.json")
    temperature = result["temperatures"][""][""][""]
    assert math.exp(-3) <= temperature <= math.exp(3)
    assert math.isfinite(result["ece_before"])
    assert math.isfinite(result["ece_after"])
    assert 0 <= result["ece_after"] <= 1


@pytest.mark.parametrize("missing", ["confidence", "correct"])
def test_calibration_missing_required_value_does_not_write(tmp_path, missing):
    """Verify incomplete calibration rows fail before creating an output file."""
    row = {"split": "train", "confidence": 0.9, "correct": 1}
    del row[missing]
    out = tmp_path / "calibration.json"
    with pytest.raises(KeyError, match=missing):
        fit_calibration([row], out)
    assert not out.exists()
