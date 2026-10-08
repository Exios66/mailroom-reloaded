"""Slim vendored subset of ``llm-dojo-scoring`` v0.21.0 (see PARITY.md).

Upstream signatures are unchanged; ``subclass_vocab`` is the only addition.
"""

from __future__ import annotations

from .classification import (
    accuracy,
    confusion_matrix,
    exact_match,
    fbeta,
    macro_prf,
    normalize_label,
    per_class_stats,
)
from .corpus import DOC_TYPE_SUBCLASSES, normalize_corpus_subclass
from .extraction_metrics import extraction_binary_metrics, merge_extraction_counts
from .field_scoring import score_extraction, score_field
from .intake import INTAKE_SPAN_KEYS, looks_messy
from .maud import canonical_maud_class, maud_question_catalog


def subclass_vocab(doc_type: str) -> list[str]:
    """Canonical subtype keys for a doc type (empty list when none).

    Contract -> ``config.CONTRACT_SUBTYPE_KEYS``; merger_agreement ->
    ``config.MAUD_CONSIDERATION_TYPES``; corporate_record, correspondence and
    insurance_claim -> the per-class tuples in the dojo subclass catalogue.
    """
    return list(DOC_TYPE_SUBCLASSES.get(doc_type, ()))


__all__ = [
    "INTAKE_SPAN_KEYS",
    "accuracy",
    "canonical_maud_class",
    "confusion_matrix",
    "exact_match",
    "extraction_binary_metrics",
    "fbeta",
    "looks_messy",
    "macro_prf",
    "maud_question_catalog",
    "merge_extraction_counts",
    "normalize_corpus_subclass",
    "normalize_label",
    "per_class_stats",
    "score_extraction",
    "score_field",
    "subclass_vocab",
]
