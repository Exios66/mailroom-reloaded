import json

import pytest

from mailroom_reloaded.agents.gate import (
    BandGate,
    GateFeatures,
    LearnedGate,
    ece,
    load_gate,
)
from mailroom_reloaded.settings import load_taxonomy


def feat(stage="classify", doc_type=None, confidence=0.99, attempts=0, **kw):
    base = {
        "stage": stage,
        "doc_type": doc_type,
        "confidence": confidence,
        "attempts": attempts,
        "bert_confidence": 0.9,
        "bert_margin": 0.5,
        "bert_window_agreement": 1.0,
        "schema_valid": True,
        "field_coverage": 1.0,
        "length_capped": False,
        "doc_type_disagree": False,
        "resorted": False,
    }
    base.update(kw)
    return GateFeatures(**base)


CASES = [
    # classify (doc_type None -> defaults low 0.70, high 0.95, retry_max 2)
    ("classify", None, 0.95, 0, {}, "proceed"),
    ("classify", None, 0.9499, 0, {}, "retry"),
    ("classify", None, 0.70, 0, {}, "retry"),
    ("classify", None, 0.50, 1, {}, "retry"),
    ("classify", None, 0.9499, 2, {}, "human_review"),
    ("classify", None, 0.99, 0, {"doc_type_disagree": True}, "re_sort"),
    ("classify", None, 0.99, 0, {"doc_type_disagree": True, "resorted": True}, "proceed"),
    # extract, insurance_claim: low 0.90, judge_band_high 0.92
    ("extract", "insurance_claim", 0.92, 0, {}, "proceed"),
    ("extract", "insurance_claim", 0.9199, 0, {}, "verify"),
    ("extract", "insurance_claim", 0.90, 0, {}, "verify"),
    ("extract", "insurance_claim", 0.8999, 0, {}, "retry"),
    ("extract", "insurance_claim", 0.8999, 2, {}, "boss"),
    # extract, correspondence: judge_band_high 0.94
    ("extract", "correspondence", 0.94, 0, {}, "proceed"),
    ("extract", "correspondence", 0.9399, 0, {}, "verify"),
    ("extract", "correspondence", 0.99, 0, {"length_capped": True}, "retry"),
    ("extract", "correspondence", 0.99, 2, {"length_capped": True}, "human_review"),
    ("extract", "correspondence", 0.99, 1, {"schema_valid": False}, "retry"),
    ("extract", "correspondence", 0.99, 2, {"schema_valid": False}, "human_review"),
]


@pytest.mark.parametrize("stage,doc_type,conf,attempts,extra,expected", CASES)
def test_band_gate(stage, doc_type, conf, attempts, extra, expected):
    gate = BandGate(load_taxonomy())
    d = gate.decide(feat(stage, doc_type, conf, attempts, **extra))
    assert d.action == expected
    assert d.source in ("band", "rule")


def _always_positive(path, stage):
    path.write_text(
        json.dumps(
            {
                stage: {
                    "features": ["confidence"],
                    "coef": [0.0],
                    "intercept": 20.0,
                    "threshold": 0.5,
                }
            }
        )
    )


@pytest.mark.parametrize("stage,doc_type", [("classify", None), ("extract", "insurance_claim")])
def test_learned_gate_only_in_medium_band(tmp_path, stage, doc_type):
    p = tmp_path / "route_gate.json"
    _always_positive(p, stage)
    band = BandGate(load_taxonomy())
    gate = LearnedGate(band, p)
    for conf in (0.99, 0.10):
        f = feat(stage, doc_type, conf)
        assert gate.decide(f) == band.decide(f)
    mid = 0.93 if stage == "classify" else 0.91
    d = gate.decide(feat(stage, doc_type, mid))
    assert d.source == "model"
    assert d.action in ("human_review", "retry", "verify")


def test_learned_gate_can_proceed(tmp_path):
    p = tmp_path / "route_gate.json"
    p.write_text(
        json.dumps(
            {
                "classify": {
                    "features": ["confidence"],
                    "coef": [0.0],
                    "intercept": -20.0,
                    "threshold": 0.5,
                }
            }
        )
    )
    gate = LearnedGate(BandGate(load_taxonomy()), p)
    assert gate.decide(feat("classify", None, 0.8)).action == "proceed"


def test_deterministic():
    gate = BandGate(load_taxonomy())
    f = feat("classify", None, 0.8)
    assert len({gate.decide(f) for _ in range(100)}) == 1


def test_load_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    assert isinstance(load_gate(), BandGate)
    (tmp_path / "models").mkdir()
    _always_positive(tmp_path / "models" / "route_gate.json", "classify")
    assert isinstance(load_gate(), LearnedGate)


def test_ece_basic():
    assert ece([1.0, 1.0], [1, 1]) == 0.0
    assert ece([0.95] * 10, [1] * 5 + [0] * 5) == pytest.approx(0.45)
