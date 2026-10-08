"""MAUD merger-agreement answer-class catalogs — no guessing on GT labels.

Dataset authority: ``Lucius-Morningstar/mailroom-dataset`` config
``ground_truth``, scanned 2026-10-04 over all 152 published merger rows
(17 test + 135 train). Every ``maud_clause_labels`` record
is shaped::

    {"answer": "...", "category": "...", "excerpt_chars": 123,
     "label_idx": 0, "valid_classes": ["...", ...]}

``valid_classes`` is the answer catalog the annotators recognized for that
question on that row. Until now the scorer dropped it and
``is_valid_maud_answer`` returned ``True`` for *any* non-empty text on 21 of
the 22 questions — guessing. This module makes the class surface explicit:

* the record's own ``valid_classes`` is the authoritative per-row surface
  (preserved by ``parse_maud_labels``);
* :data:`MAUD_ANSWER_CLASSES` is the union observed across the corpus, used
  as the fallback when a prediction is scored without its GT record;
* :func:`maud_question_catalog` returns the fully populated
  ``{question: valid classes}`` dict for a document type.

Four questions legitimately vary their class set by row (R&W accuracy, MAE
definition, FTR limitations, Tail Period details) — the record-level classes
therefore win over the union. Verified against the corpus: every observed
expected answer is a member of its row's ``valid_classes`` (0 mismatches
over 152 rows). The offline mirror lives in
``tests/fixtures/maud_valid_classes.json``.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from .corpus import MAUD_QUESTION_KEYS

__all__ = [
    "MAUD_ANSWER_CLASSES",
    "MAUD_CATALOG_GENERATED",
    "MAUD_CATALOG_ROWS",
    "MAUD_CATALOG_SOURCE",
    "MAUD_QUESTION_KEYS_BY_DOC_TYPE",
    "MAUD_VARIABLE_CLASS_QUESTIONS",
    "canonical_maud_class",
    "is_maud_class",
    "maud_class_index",
    "maud_question_catalog",
]

MAUD_CATALOG_SOURCE = "Lucius-Morningstar/mailroom-dataset (ground_truth, test+train) @ bc9eab280044befb51e19dda3071d290a8677f42"
MAUD_CATALOG_GENERATED = "2026-10-04"
MAUD_CATALOG_ROWS = 152

#: Questions whose per-row ``valid_classes`` differ across rows (the union in
#: :data:`MAUD_ANSWER_CLASSES` is a superset — always prefer the row's set).
MAUD_VARIABLE_CLASS_QUESTIONS: tuple[str, ...] = (
    'Accuracy of Target R&W Closing Condition',
    'Limitations on FTR Exercise',
    'MAE Definition',
    'Tail Period & Acquisition Proposal Details',
)

#: Union of every ``valid_classes`` value observed per question.
MAUD_ANSWER_CLASSES: dict[str, tuple[str, ...]] = {
    'Absence of Litigation Closing Condition': (
        'Governmental litigation only',
        'Non-Governmental & governmental litigation',
        'Pending',
        'Pending or threatened (without "writing" requirement)',
        'Pending or threatened in writing',
    ),
    'Accuracy of Target R&W Closing Condition': (
        'Accurate at another materiality standard (e.g., hybrid standard)',
        'Accurate in all material respects',
        'Accurate in all respects',
        'Accurate in all respects with below-threshold carveout',
        'Accurate in all respects with de minimis exception',
        'All/The R&Ws accurate at MAE standard',
        'All/The R&Ws accurate in all respects (repeating R&Ws)',
        'At Closing Only',
        'At Signing & At Closing',
        'Authority, Approval, Enforceability',
        "Authority, Approval, Enforceability, No MAE, Brokers' Fee",
        "Authority, Approval, Enforceability, No MAE, Brokers' Fee, Takeover Statutes, Opinion of Financial Advisor",
        "Authority, Approval, Enforceability, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor",
        'Authority, Approval, Enforceability, No MAE, Opinion of Financial Advisor, Other',
        "Authority, Approval, Enforceability, Organization, Brokers' Fee",
        "Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes, Opinion of Financial Advisor, Other",
        "Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes, Other",
        'Capitalization-Other',
        "Capitalization-Other, Approval, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, No MAE, Brokers' Fee",
        "Capitalization-Other, Authority, Approval, Enforceability, No MAE, Brokers' Fee, Rights Agreement, Other",
        'Capitalization-Other, Authority, Approval, Enforceability, Organization',
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Brokers' Fee",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Brokers' Fee, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Brokers' Fee, Opinion of Financial Advisor, No-Conflict",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Brokers' Fee, Opinion of Financial Advisor, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Brokers' Fee, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Brokers' Fee, Takeover Statutes",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Brokers' Fee, Takeover Statutes, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Brokers' Fee, Takeover Statutes, Rights Agreement",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Brokers' Fee, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor",
        'Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE',
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Rights Agreement, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Takeover Statutes",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Takeover Statutes, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Takeover Statutes, Opinion of Financial Advisor, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Takeover Statutes, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement, No-Conflict, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor, No-Conflict",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor, Tax",
        'Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Opinion of Financial Advisor',
        'Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Other',
        'Capitalization-Other, Authority, Approval, Enforceability, Organization, No MAE, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor',
        'Capitalization-Other, Authority, Approval, Enforceability, Organization, Other',
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, Brokers' Fee",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, Brokers' Fee, No-Conflict",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, Brokers' Fee, Opinion of Financial Advisor, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, Brokers' Fee, Takeover Statutes",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, Brokers' Fee, Takeover Statutes, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, Brokers' Fee, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor",
        'Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE',
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, No-Conflict",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Opinion of Financial Advisor, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes, Opinion of Financial Advisor, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor, Other",
        "Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement, Other",
        'Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Opinion of Financial Advisor, Other',
        'Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Other',
        'Capitalization-Other, Authority, Approval, Enforceability, Organization, Subsidiaries, No MAE, Takeover Statutes, Opinion of Financial Advisor, Other',
        "Capitalization-Other, Authority, Approval, Enforceability, Subsidiaries, Brokers' Fee",
        "Capitalization-Other, Authority, Approval, Enforceability, Subsidiaries, No MAE, Brokers' Fee, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Approval, Enforceability, Subsidiaries, No MAE, Brokers' Fee, Opinion of Financial Advisor, Other",
        'Capitalization-Other, Authority, Approval, Enforceability, Subsidiaries, No MAE, Takeover Statutes, Opinion of Financial Advisor, Other',
        "Capitalization-Other, Authority, Approval, Organization, Brokers' Fee, Takeover Statutes, Rights Agreement, Opinion of Financial Advisor",
        "Capitalization-Other, Authority, Enforceability, Brokers' Fee",
        "Capitalization-Other, Authority, Enforceability, Organization, Brokers' Fee, Takeover Statutes, Opinion of Financial Advisor, Other",
        "Capitalization-Other, Authority, Enforceability, Organization, No MAE, Brokers' Fee, Opinion of Financial Advisor, No-Conflict",
        "Capitalization-Other, Authority, Organization, No MAE, Brokers' Fee, Takeover Statutes, Rights Agreement",
        'Capitalization-Other, No MAE',
        'Capitalization-Other, No MAE, Other',
        'Each R&W accurate at MAE standard',
        'fundamental/Special R&Ws',
        'General R&Ws',
        'General R&Ws, Capitalization R&Ws',
        'General R&Ws, Capitalization R&Ws, fundamental/Special R&Ws',
        'General R&Ws, Capitalization R&Ws, Fundermental/Special R&Ws',
        'General R&Ws, Capitalization R&Ws, Specified R&Ws only',
        'General R&Ws, fundamental/Special R&Ws',
        'General R&Ws, Fundermental/Special R&Ws',
        'General R&Ws, Specified R&Ws only',
        'R&Ws accurate at another materiality standard (e.g., hybrid standard)',
        'Specified R&Ws only',
        'Tax',
    ),
    'Agreement provides for matching rights in connection with COR': (
        '2 business days or less',
        '3 business days',
        '3 calendar days',
        '3 days',
        '4 business days',
        '4 calendar days',
        '5 business days',
        '> 5 business days',
        'Continuous matching right',
        'Greater than 5 business days',
        'None',
    ),
    'Agreement provides for matching rights in connection with FTR': (
        '2 business days or less',
        '3 business days',
        '3 calendar days',
        '3 days',
        '4 business days',
        '4 calendar days',
        '5 business days',
        '5 calendar days',
        '>5 business days',
        'Continuous matching right',
        'Greater than 5 business days',
        'None',
    ),
    'Breach of Meeting Covenant': (
        'No',
        'Yes',
    ),
    'Breach of No Shop': (
        'No',
        'Yes',
    ),
    'Compliance with Covenant Closing Condition': (
        'All Covenants',
        'Each Covenant',
        'Hybrid/Other Standard',
    ),
    'FTR Triggers': (
        'Superior Offer',
        'Superior Offer, Intervening Event',
    ),
    'Fiduciary exception to COR covenant': (
        '"Breach" of fiduciary duties',
        '"Inconsistent" with fiduciary duties',
        '"Reasonably likely/expected breach" of fiduciary duties',
        '"Reasonably likely/expected to be inconsistent" with fiduciary duties',
        '"Reasonably likely/expected violation" of fiduciary duties',
        '"Required to comply" with fiduciary duties',
        '"Violation" of fiduciary duties',
        'More likely than not violate fiduciary duties',
        'No',
        'None',
        'Other specified standard',
        'Yes',
    ),
    'Fiduciary exception:  Board determination (no-shop)': (
        '"Breach" of fiduciary duties',
        '"Inconsistent" with fiduciary duties',
        '"Reasonably likely/expected breach" of fiduciary duties',
        '"Reasonably likely/expected to be inconsistent" with fiduciary duties',
        '"Reasonably likely/expected violation" of fiduciary duties',
        '"Required to comply" with fiduciary duties',
        '"Violation" of fiduciary duties',
        'Acquisition Proposal only',
        'None',
        'Other specified standard',
        'Superior Offer, or Acquisition Proposal reasonably likely/expected to result in a Superior Offer',
    ),
    'General Antitrust Efforts Standard': (
        'Commercially reasonable efforts',
        'Flat standard',
        'Reasonable best efforts',
    ),
    'Intervening Event Definition': (
        'Known, but consequences unknown or not reasonably foreseeable, at signing',
        'Known, but consequences unknown, at signing',
        'May occur or arise prior to signing',
        'Must occur or arise after signing',
        'No',
        'Not known and not reasonably foreseeable at signing',
        'Not known at signing',
        'Yes',
    ),
    'Knowledge Definition': (
        'Actual knowledge',
        'Based on investigation or inquiry',
        'Based on role',
        'Constructive knowledge',
        'No',
        'Yes',
    ),
    'Limitations on FTR Exercise': (
        '(Material) breach of other provisions of agreement',
        'Any breach of no-shop',
        'Any breach of no-shop, (Material) breach of other provisions of agreement',
        'Breach of no-shop resulting in a Superior Offer',
        'Breach of no-shop resulting in a Superior Offer, (Material) breach of other provisions of agreement',
        'Material breach of no-shop',
        'Material breach of no-shop resulting in a Superior Offer',
        'Material breach of no-shop resulting in a Superior Offer, (Material) breach of other provisions of agreement',
        'Material breach of no-shop, (Material) breach of other provisions of agreement',
        'Other',
    ),
    'MAE Definition': (
        '"Arising from/out of"',
        '"Arising from/out of", "Relating to"',
        '"Arising from/out of", Other',
        '"Attributable to"',
        '"Could" (reasonably) be expected to',
        '"Relating to"',
        '"Resulting from"',
        '"Resulting from", "Arising from/out of"',
        '"Resulting from", "Arising from/out of", "Attributable to"',
        '"Resulting from", "Arising from/out of", "Relating to"',
        '"Resulting from", "Arising from/out of", "Relating to", "Attributable to"',
        '"Resulting from", "Arising from/out of", "Relating to", "Attributable to", Relational language varies among carveouts',
        '"Resulting from", "Arising from/out of", "Relating to", Other',
        '"Resulting from", "Arising from/out of", "Relating to", Relational language varies among carveouts',
        '"Resulting from", "Arising from/out of", Relational language varies among carveouts',
        '"Resulting from", "Attributable to"',
        '"Resulting from", "Relating to"',
        '"Resulting from", Other',
        '"Would"',
        '"Would" (reasonably) be expected to',
        'ability to consummate transaction',
        'All MAE carveouts',
        'announcement',
        'announcement, consummation',
        'announcement, pendency',
        'announcement, pendency, consummation',
        'Applies to Target and subsidiaries "taken as a whole"',
        'business and operation of Target',
        'business and operation of Target, ability to consummate transaction',
        'Natural disaster',
        'Natural disaster, "act of God"',
        'No',
        'Other forward-looking standard',
        'Some MAE carveouts',
        'War or terrorism',
        'War or terrorism, Natural disaster',
        'War or terrorism, Natural disaster, "act of God"',
        'War or terrorism, Natural disaster, "act of God", force majeure',
        'War or terrorism, Natural disaster, force majeure',
        'Yes',
    ),
    'Negative interim operating covenant': (
        'Applies only to specified negative covenants',
        'Applies to all negative covenants',
        'Consent may not be unreasonably withheld, conditioned or delayed',
        'Flat consent',
        'No',
        'Yes',
    ),
    'No-Shop': (
        'No',
        'Reasonable standard',
        'Strict liability',
        'Yes',
    ),
    'Ordinary course covenant': (
        'Commercially reasonable efforts',
        'Consent may not be unreasonably withheld, conditioned or delayed',
        'Flat consent',
        'Flat covenant (no efforts standard)',
        'No',
        'Reasonable best efforts',
        'Yes',
    ),
    'Specific Performance': (
        '"entitled to seek" specific performance',
        '"entitled to" specific performance',
    ),
    'Superior Offer Definition': (
        '"All or substantially all"',
        '50%',
        'Greater than 50% but not "all or substantially all"',
        'Less than 50%',
        'No',
        'Yes',
    ),
    'Tail Period & Acquisition Proposal Details': (
        '12 months or longer',
        'No',
        'Other',
        'within 12 months',
        'within 6 months',
        'within 9 months',
        'Yes',
        '“Publicly disclosed” requirement applies to Acquisition Proposal + Breach Trigger',
        '“Publicly disclosed” requirement applies to Acquisition Proposal + Breach Trigger, “Publicly disclosed” requirement applies to Acquisition Proposal + No-Vote / MTC Failure Trigger',
        '“Publicly disclosed” requirement applies to Acquisition Proposal + Breach Trigger, “Publicly disclosed” requirement applies to Acquisition Proposal + No-Vote / MTC Failure Trigger, “Publicly disclosed” requirement applies to Acquisition Proposal + Outside Date Trigger',
        '“Publicly disclosed” requirement applies to Acquisition Proposal + Breach Trigger, “Publicly disclosed” requirement applies to Acquisition Proposal + Outside Date Trigger',
        '“Publicly disclosed” requirement applies to Acquisition Proposal + No-Vote / MTC Failure Trigger',
        '“Publicly disclosed” requirement applies to Acquisition Proposal + No-Vote / MTC Failure Trigger, “Publicly disclosed” requirement applies to Acquisition Proposal + Outside Date Trigger',
        '“Publicly disclosed” requirement applies to Acquisition Proposal + Outside Date Trigger',
    ),
    'Type of Consideration': (
        'All Cash',
        'All Stock',
        'Mixed Cash/Stock',
        'Mixed Cash/Stock: Election',
    ),
}

#: Fully populated question catalogs per document type. ``merger_agreement``
#: is the MAUD class; the Hub routes merger rows through the contracts
#: specialist, and the contract suite scores MAUD differentiators when the
#: row carries them, so ``contract`` exposes the same 22-question catalog.
MAUD_QUESTION_KEYS_BY_DOC_TYPE: dict[str, tuple[str, ...]] = {
    "merger_agreement": tuple(MAUD_ANSWER_CLASSES),
    "contract": tuple(MAUD_ANSWER_CLASSES),
}

_DOC_TYPE_ALIASES = {
    "merger_agreement": "merger_agreement",
    "merger_agreement_specialist": "merger_agreement",
    "contract": "contract",
    "contracts_specialist": "contract",
}

_SMART_QUOTES = str.maketrans(
    {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"}
)
_TYPO = re.compile(r"fundermental", re.IGNORECASE)

#: Yes/No surface aliases (only valid where the question's classes include
#: Yes/No — the index decides).
_YES_NO_ALIASES = {
    "y": "yes",
    "true": "yes",
    "1": "yes",
    "n": "no",
    "false": "no",
    "0": "no",
}


def _canon(text: object) -> str:
    value = str(text or "").translate(_SMART_QUOTES)
    value = _TYPO.sub("fundamental", value)
    return " ".join(value.split())


def _fold(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _canon(text).lower()).strip()


def maud_class_index(
    question: str, valid_classes: Sequence[str] | None = None
) -> dict[str, str]:
    """``{folded class: canonical class}`` for a question.

    ``valid_classes`` (the record's own catalog) wins; otherwise the union
    catalog for the question is used. Unknown questions without an explicit
    catalog return ``{}`` — callers must fail closed, never guess.
    """
    classes: Iterable[str]
    if valid_classes:
        classes = valid_classes
    else:
        classes = MAUD_ANSWER_CLASSES.get(question, ())
    index: dict[str, str] = {}
    for cls in classes:
        folded = _fold(cls)
        if folded:
            index.setdefault(folded, _canon(cls))
    return index


def _components(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def canonical_maud_class(
    question: str, value: object, valid_classes: Sequence[str] | None = None
) -> str:
    """Canonical folded form of *value* under the question's class surface.

    Whole-string class match first (class names themselves contain commas);
    otherwise all comma components must map and the canonical components are
    rejoined. Recognized Yes/No aliases fold to ``yes`` / ``no`` only when
    the question's surface actually carries Yes/No. Unrecognized text falls
    back to the folded raw value so exact-match scoring stays deterministic.
    """
    if value is None or str(value).strip() == "":
        return ""
    index = maud_class_index(question, valid_classes)
    folded = _fold(value)
    if folded in index:
        return _fold(index[folded])
    alias = _YES_NO_ALIASES.get(folded)
    if alias and alias in index:
        return alias
    parts = _components(str(value))
    if len(parts) > 1:
        canonical = [index.get(_fold(part)) for part in parts]
        if all(part is not None for part in canonical):
            return ", ".join(_fold(part) for part in canonical)
    return folded


def is_maud_class(
    question: str, value: object, valid_classes: Sequence[str] | None = None
) -> bool:
    """True when *value* uses only classes from the question's surface."""
    if value is None or str(value).strip() == "":
        return False
    index = maud_class_index(question, valid_classes)
    if not index:
        return False
    folded = _fold(value)
    if folded in index:
        return True
    alias = _YES_NO_ALIASES.get(folded)
    if alias and alias in index:
        return True
    parts = _components(str(value))
    return len(parts) > 1 and all(_fold(part) in index for part in parts)


def maud_question_catalog(doc_type: str) -> dict[str, tuple[str, ...]]:
    """Fully populated ``{question: valid classes}`` for a document type.

    Accepts the canonical doc type or the specialist name
    (``merger_agreement_specialist`` / ``contracts_specialist``). Unknown
    types raise ``KeyError`` — never return a silently empty catalog.
    """
    key = _DOC_TYPE_ALIASES.get(str(doc_type or "").strip().lower())
    if key is None:
        raise KeyError(
            f"unknown MAUD document type {doc_type!r}; expected one of "
            f"{sorted(set(_DOC_TYPE_ALIASES.values()))}"
        )
    return {q: MAUD_ANSWER_CLASSES[q] for q in MAUD_QUESTION_KEYS_BY_DOC_TYPE[key]}
