"""Features-mode harvest tests. Every Jev client is a stub: NO network is used."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from mailroom_reloaded.agents.gate import GateFeatures
from mailroom_reloaded.agents.jev import JevAnswer

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "jev_harvest.py"


def _load_module():
    """Load ``scripts/jev_harvest.py`` as a module (it is not an importable package)."""
    name = "jev_harvest_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


class _StubCfg:
    provider = "openrouter"
    model = "typesafe/jev-1.13"
    enabled = True


class _StubClient:
    """Duck-typed ``JevClient`` returning ``responder(state)``; records calls."""

    def __init__(self, responder):
        self.cfg = _StubCfg()
        self._responder = responder
        self.states: list[dict] = []

    def ask(self, state, questions):
        self.states.append(state)
        return self._responder(state)


def _write_rows(path: Path, rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def _read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_feature_target_defaults_and_label_mapping():
    module = _load_module()

    classify = module._feature_target(
        {"stage": "classify", "confidence": 0.7, "retry_expected": 1}, 0
    )
    assert classify is not None
    assert classify.features == GateFeatures(
        stage="classify", doc_type=None, confidence=0.7, attempts=0
    )
    assert classify.expected_escalate is True
    assert classify.split == "train"

    extract = module._feature_target(
        {
            "split": "train",
            "stage": "extract",
            "doc_type": "contract",
            "confidence": 0.4,
            "attempts": 2,
            "schema_valid": False,
            "length_capped": True,
            "review_expected": 0,
        },
        1,
    )
    assert extract is not None
    assert extract.features.attempts == 2
    assert extract.features.schema_valid is False
    assert extract.features.length_capped is True
    assert extract.doc_type == "contract"
    assert extract.expected_escalate is False


@pytest.mark.parametrize(
    "row",
    [
        {"stage": "bogus", "retry_expected": 1},
        {"stage": "classify"},  # missing retry_expected
        {"stage": "extract", "retry_expected": 1},  # wrong label for stage
        {"retry_expected": 1},  # missing stage
    ],
)
def test_feature_target_skips_unusable_rows(row):
    module = _load_module()
    assert module._feature_target(row, 0) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, True), (0, False), (True, True), (False, False), ("true", True),
     ("false", False), ("False", False), ("0", False), ("1", True)],
)
def test_feature_target_parses_hub_string_labels(value, expected):
    """The Hub serves ``retry_expected``/``review_expected`` as ``"true"``/``"false"``."""
    module = _load_module()
    target = module._feature_target(
        {"stage": "classify", "retry_expected": value}, 0
    )
    assert target is not None
    assert target.expected_escalate is expected


@pytest.mark.parametrize("extract_route", ["proceed", "human_review"])
def test_harvest_features_writes_calibration_rows(tmp_path, monkeypatch, extract_route):
    """Preserve target order and score each usable route against its label."""
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "local")
    module = _load_module()

    def responder(state):
        """Return stage-specific routes and simulate unusable classify answers."""
        if state["stage"] == "extract":
            return {"route": JevAnswer(type="choice", choice=extract_route, confidence=0.7)}
        confidence = round(float(state["confidence"]), 2)
        if confidence == 0.8:
            return {"route": JevAnswer(type="choice", choice="retry", confidence=0.9)}
        if confidence == 0.6:
            return {"route": JevAnswer(type="choice", choice="proceed", confidence=0.95)}
        if confidence == 0.4:
            return {}  # no route answer -> unusable
        raise AssertionError(f"unexpected state {state}")

    stub = _StubClient(responder)
    monkeypatch.setattr(module, "JevClient", lambda cfg: stub)

    rows = [
        {"split": "train", "stage": "classify", "confidence": 0.8, "retry_expected": 1},
        {"split": "train", "stage": "classify", "confidence": 0.6, "retry_expected": 0},
        {"split": "train", "stage": "extract", "confidence": 0.3, "review_expected": 1},
        {"split": "train", "stage": "classify", "confidence": 0.9},  # missing label
        {"split": "train", "stage": "classify", "confidence": 0.4, "retry_expected": 0},
        {"split": "train", "stage": "bogus", "retry_expected": 1},  # bad stage
    ]
    rows_path = _write_rows(tmp_path / "features.jsonl", rows)
    out_path = tmp_path / "out.jsonl"

    rc = module.main(
        ["--mode", "features", "--rows", str(rows_path), "--out", str(out_path)]
    )

    assert rc == 0
    out = _read_rows(out_path)
    assert len(out) == 3
    # Deterministic input order, not completion order; calibration-row shape.
    assert [r["correct"] for r in out] == [1, 1, int(extract_route == "human_review")]
    assert all(
        set(r) == {"split", "provider", "model", "doc_type", "confidence", "correct"}
        for r in out
    )
    assert out[0]["provider"] == "openrouter"
    assert out[0]["model"] == "typesafe/jev-1.13"
    assert out[0]["confidence"] == pytest.approx(0.9)
    assert out[2]["doc_type"] is None

    # The compact production state is what reaches Jev (never document text).
    assert all(set(state) == set(module._jev_state(GateFeatures("classify", None, 0.0, 0)))
               for state in stub.states)


def test_main_features_requires_rows(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "jev_config", lambda: _StubCfg())
    rc = module.main(["--mode", "features", "--out", str(tmp_path / "out.jsonl")])
    assert rc == 1


def test_harvest_features_refuses_single_class_labels(tmp_path, monkeypatch):
    """A single label class is degenerate; refuse before building a client."""
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "local")
    module = _load_module()
    monkeypatch.setattr(module, "JevClient", lambda cfg: pytest.fail("client built"))

    rows = [
        {"split": "train", "stage": "classify", "confidence": 0.8, "retry_expected": 0},
        {"split": "train", "stage": "extract", "confidence": 0.3, "review_expected": 0},
    ]
    out = tmp_path / "out.jsonl"
    rc = module.main(
        ["--mode", "features", "--rows", str(_write_rows(tmp_path / "f.jsonl", rows)),
         "--out", str(out)]
    )
    assert rc == 1
    assert not out.exists()


@pytest.mark.parametrize("surviving_label", [0, 1, None])
@pytest.mark.parametrize("existing_output", [False, True])
def test_harvest_features_skips_degenerate_survivors(
    tmp_path, monkeypatch, capsys, surviving_label, existing_output
):
    """Leave output untouched when usable answers lose a target label class."""
    module = _load_module()
    monkeypatch.setattr(module, "jev_config", lambda: _StubCfg())

    def responder(state):
        """Return usable routes only for the selected target label class."""
        if state["confidence"] != surviving_label:
            return {}
        # Surviving targets share an expected label but have mixed correctness.
        route = "retry" if state["stage"] == "classify" else "proceed"
        return {"route": JevAnswer(type="choice", choice=route, confidence=0.9)}

    monkeypatch.setattr(module, "JevClient", lambda cfg: _StubClient(responder))
    rows = [
        {"stage": stage, "confidence": label, label_key: label}
        for label in (0, 1)
        for stage, label_key in (
            ("classify", "retry_expected"), ("extract", "review_expected")
        )
    ]
    rows_path = _write_rows(tmp_path / "features.jsonl", rows)
    out = tmp_path / "out.jsonl"
    if existing_output:
        out.write_text("existing calibration rows\n", encoding="utf-8")

    rc = module.main(
        ["--mode", "features", "--rows", str(rows_path), "--out", str(out)]
    )

    assert rc == 0
    assert "surviving rows" in capsys.readouterr().err
    if existing_output:
        assert out.read_text(encoding="utf-8") == "existing calibration rows\n"
    else:
        assert not out.exists()


def test_feature_route_verify_is_not_an_escalation():
    """``verify`` is the caution tier, not a review; only real routes escalate."""
    module = _load_module()
    target = module._feature_target(
        {"stage": "extract", "review_expected": 1, "confidence": 0.4}, 0
    )

    class _Client:
        cfg = _StubCfg()

        def __init__(self, route):
            """Configure the route returned by this synthetic Jev client."""
            self._route = route

        def ask(self, state, questions):
            """Return the configured route with a fixed confidence."""
            return {"route": JevAnswer(type="choice", choice=self._route, confidence=0.9)}

    assert module._run_feature(_Client("verify"), target)["correct"] == 0
    assert module._run_feature(_Client("human_review"), target)["correct"] == 1
