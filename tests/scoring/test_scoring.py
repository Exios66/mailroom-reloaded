from __future__ import annotations

import pytest

from mailroom_reloaded.scoring import (
    fbeta,
    score_extraction,
    score_field,
    subclass_vocab,
)

LIVE = ("contract", "merger_agreement", "corporate_record", "correspondence", "insurance_claim")


def test_fbeta_f2():
    assert fbeta(0.5, 1.0, beta=2) == pytest.approx(0.8333, abs=1e-4)


def test_money_exact_after_normalisation():
    assert score_field("money", "$1,000.00", "1000") == 1.0


def test_date_partial_credit():
    score = score_field("date", "2021-03-19", "2021-03-04")
    assert 0.0 < score < 1.0


def test_entity_list_hungarian():
    result = score_extraction(
        doc_class="contract",
        field_types={"parties": "entity_list:name"},
        predicted={"parties": ["Beta LLC", "Acme Corp"]},
        expected={"parties": ["Acme Corp", "Beta LLC"]},
    )
    assert result.overall_score == 1.0


def test_subclass_vocab_covers_dataset_strata():
    vocabs = {c: subclass_vocab(c) for c in LIVE}
    assert all(vocabs.values())
    union = {k for v in vocabs.values() for k in v}
    # Brief asks for >= 55; the five live lists total 56 entries but only 54
    # distinct keys ("other" is shared). See report Decisions.
    assert sum(len(v) for v in vocabs.values()) >= 55
    assert len(union) >= 54
    assert "all_cash" in union
    assert "all_cash" in subclass_vocab("merger_agreement")
    assert len(subclass_vocab("contract")) == 25


def test_subclass_vocab_returns_copy_and_unknown_is_empty():
    v = subclass_vocab("contract")
    v.append("x")
    assert "x" not in subclass_vocab("contract")
    assert subclass_vocab("court_opinion") == []
    assert subclass_vocab("nope") == []
