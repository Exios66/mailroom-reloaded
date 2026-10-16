from __future__ import annotations

import pytest

from mailroom_reloaded.scoring import (
    canonical_maud_class,
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


@pytest.mark.parametrize(
    "field_type,bands,score,ambiguous",
    [
        ("name", {"name": ("always",)}, 1.0, True),
        ("name", {"name": ("never",)}, 0.6, False),
        ("free_text", {"free_text": (0.2, 0.4)}, 0.2, True),
        ("free_text", {"free_text": (0.2, 0.4)}, 0.4, False),
        ("entity_list:name", {"entity_list": ("always",)}, 0.0, True),
        ("entity_list:name", {"entity_list": ("always",),
                              "entity_list:name": ("never",)}, 0.6, False),
        (None, {"name": ("always",)}, 1.0, True),
        ("name", {}, 0.5, True),
        ("name", {}, 0.85, False),
    ],
)
def test_extraction_ambiguity_uses_original_type(
    monkeypatch, field_type, bands, score, ambiguous,
):
    from mailroom_reloaded.scoring import field_scoring
    from mailroom_reloaded.scoring.config import get_settings

    settings = get_settings().field_scoring
    monkeypatch.setattr(settings, "type_bands", {"containment": ("never",), **bands})
    monkeypatch.setattr(settings, "ambiguous_band", (0.5, 0.85))
    monkeypatch.setattr(settings, "containment_fields", {"subject_matter"})
    monkeypatch.setattr(settings, "embedding_enabled", False)
    scored_types = []

    def scorer(actual_type, *args, **kwargs):
        scored_types.append(actual_type)
        return score

    monkeypatch.setattr(field_scoring, "score_field", scorer)
    result = score_extraction(
        "contract", {"subject_matter": field_type} if field_type else {},
        {"subject_matter": "services"}, {"subject_matter": "services"},
    )
    assert scored_types == [field_type if field_type and field_type.startswith("entity_list")
                            else "containment"]
    assert result.ambiguous_fields == (["subject_matter"] if ambiguous else [])
    assert result.needs_judge_review is ambiguous


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


# Behaviour that arrived with the re-vendor to llm-dojo-scoring 8c7b7f1
# (scoring/PARITY.md). Each test fails on the v0.21.0 vendored copy.


def test_cms_claim_type_folds_onto_health_one_way():
    # SAND-042: a CMS table token against GT "health" is a match; the reverse
    # direction (health predicted against a CMS token) is still a mismatch.
    fold = score_extraction(
        "insurance_claim", {"claim_type": "name"},
        {"claim_type": "pde"}, {"claim_type": "health"},
    )
    assert fold.field_scores["claim_type"] == 1.0
    reverse = score_extraction(
        "insurance_claim", {"claim_type": "name"},
        {"claim_type": "health"}, {"claim_type": "pde"},
    )
    assert reverse.field_scores["claim_type"] < 1.0


def test_expected_subclass_is_an_annotation_not_a_field():
    result = score_extraction(
        "contract", {"subject_matter": "name"},
        {"subject_matter": "services"},
        {"subject_matter": "services", "expected_subclass": "services"},
    )
    assert "expected_subclass" not in result.field_scores
    assert result.overall_score == 1.0


def test_maud_comparators_are_not_collapsed():
    # "> 5 business days" must not fold onto "5 business days"; the spelled-out
    # comparator is the same class.
    q = "Agreement provides for matching rights in connection with COR"
    assert canonical_maud_class(q, "> 5 business days") != canonical_maud_class(
        q, "5 business days"
    )
    assert canonical_maud_class(q, "Greater than 5 business days") == canonical_maud_class(
        q, "> 5 business days"
    )


def test_maud_multi_class_answers_are_order_insensitive():
    q = "Fiduciary exception to COR covenant"
    a, b = "inconsistent with fiduciary duties", "breach of fiduciary duties"
    assert canonical_maud_class(q, f"{a}, {b}") == canonical_maud_class(q, f"{b}, {a}")
