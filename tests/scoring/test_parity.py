from __future__ import annotations

import json
from pathlib import Path

import pytest

upstream = pytest.importorskip("llm_dojo_scoring")

from mailroom_reloaded.scoring import score_extraction

PAIRS = json.loads((Path(__file__).parent / "fixtures" / "pairs.json").read_text())


def test_fixture_has_ten_pairs():
    assert len(PAIRS) == 10


@pytest.mark.parametrize("pair", PAIRS, ids=lambda p: p["name"])
def test_parity_with_upstream(pair):
    kw = {
        "doc_class": pair["doc_class"],
        "field_types": pair["field_types"],
        "predicted": pair["predicted"],
        "expected": pair["expected"],
    }
    ours = score_extraction(**kw)
    theirs = upstream.score_extraction(**kw)
    assert ours.overall_score == theirs.overall_score
    assert ours.field_scores == theirs.field_scores
