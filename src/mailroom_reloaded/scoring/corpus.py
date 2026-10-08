"""mailroom-dataset corpus alignment — schemas, subclasses, differentiators.

Grounded in the published Hugging Face dataset
``Lucius-Morningstar/mailroom-dataset`` (default + ``ground_truth`` configs;
3,302 rows: 2,979 train / 323 test at the v9 GT-closure revision
``46a4d3c2`` — the v9 successor of the frozen v8 ``mailroom-corpus``
baseline).

This module is the single source mapping each mailroom document class to:

- the **subclass taxonomy** the sorter must emit for that class,
- the **extraction fields** the specialist suite scores,
- the **corpus differentiators** (GT columns that vary by type),
- an honest note when a native mailroom class has **zero rows** in the
  published merge.

Do not invent field names that specialists do not extract. Classification
subclasses (CMS source table, CUAD family, MAUD consideration, Enron form)
live here even when they are not extraction-schema fields.
"""

from __future__ import annotations

from typing import Any

from .config import CONTRACT_SUBTYPE_KEYS, MAUD_CONSIDERATION_TYPES, SUBTYPE_UNKNOWN
from .equivalences import (
    equivalent_doc_subclasses,
    equivalent_subtypes,
    normalize_doc_subclass,
    normalize_subtype,
)

__all__ = [
    "CORPUS_ID",
    "CORPUS_DOC_TYPES",
    "CORPUS_ABSENT_DOC_TYPES",
    "NATIVE_DOC_TYPES",
    "DOC_TYPE_SUBCLASSES",
    "CORPUS_SUBCLASS_SURFACES",
    "CORPUS_DIFFERENTIATORS",
    "CORPUS_EXTRACTION_FIELDS",
    "CUAD_CLAUSE_CATEGORIES",
    "MAUD_QUESTION_KEYS",
    "MAUD_CLAUSE_CATEGORIES",
    "CORRESPONDENCE_TOPICS",
    "CORRESPONDENCE_SENTIMENT_LABELS",
    "INSURANCE_CLAIM_TYPES",
    "normalize_corpus_subclass",
    "subclass_equivalent",
    "suite_schema",
]

#: Hugging Face dataset id this module is pinned to.
CORPUS_ID = "Lucius-Morningstar/mailroom-dataset"

#: Doc types that have at least one ground-truth row in the published merge.
CORPUS_DOC_TYPES: tuple[str, ...] = (
    "contract",
    "insurance_claim",
    "merger_agreement",
    "correspondence",
    "corporate_record",
)

#: Native mailroom classes with no rows in the published merge.
CORPUS_ABSENT_DOC_TYPES: tuple[str, ...] = (
    "due_diligence",
    "compliance_filing",
    "court_opinion",
)

#: Full native taxonomy (corpus-present ∪ corpus-absent).
NATIVE_DOC_TYPES: tuple[str, ...] = CORPUS_DOC_TYPES + CORPUS_ABSENT_DOC_TYPES

#: Canonical subclass keys per doc type. Empty tuple = no subclass dimension
#: in the published merge (and no taxonomy-level dimension yet).
DOC_TYPE_SUBCLASSES: dict[str, tuple[str, ...]] = {
    # CUAD 25-family (canonical snake_case). Corpus surfaces are folder-style
    # labels (``License_Agreements``, ``Joint Venture _ Filing``) and normalize
    # through :func:`normalize_subtype`.
    "contract": tuple(CONTRACT_SUBTYPE_KEYS),
    # MAUD Type of Consideration (expert GT). Corpus already ships canonical keys.
    "merger_agreement": tuple(MAUD_CONSIDERATION_TYPES),
    # Content-detected record type (entity-extraction taxonomy). Corpus
    # currently covers a subset; the full enum is the scoring surface.
    "corporate_record": (
        "bylaws",
        "articles_of_incorporation",
        "certificate_of_formation",
        "charter_amendment",
        "powers_of_attorney",
        "subsidiary_list",
        "rights_instrument",
        "indenture",
        "board_resolution",
        "officer_certificate",
        "other",
    ),
    # Communication form, aligned to the CURRENT dataset GT: mailroom-dataset
    # v9 ``ground_truth.expected_subclass`` (tip 46a4d3c2) carries exactly
    # these 8 tokens + the other-bucket. The v8-era `voicemail` key from the
    # Enron eval-environment labeler enum is NOT in the v9 GT vocabulary and
    # was removed here (DMR-071 adjudication 2026-09-16: the dataset pin is
    # the source of truth). The labeler keeps its voicemail key locally —
    # text-only Enron corpora make it 0% by construction — so the two enums
    # intentionally differ by that one labeler-local key.
    "correspondence": (
        "email",
        "memo",
        "letter",
        "notice",
        "demand",
        "attorney_demand",
        "press_release",
        "meeting_request",
        "other",
    ),
    # Insurance claim-document subclass: CMS DE-SynPUF *source table* tokens
    # (carrier/inpatient/outpatient/pde) PLUS the v8 synthetic LOB lines
    # (property = GNOTHEIA FNOL bundles, auto = BDR motor decision letters;
    # HUB-028/HUB-041). Mailroom ``claim_type`` also accepts these Hub tokens
    # plus legacy FNOL product lines.
    "insurance_claim": (
        "carrier",
        "inpatient",
        "outpatient",
        "pde",
        "property",
        "auto",
    ),
    "due_diligence": (),
    "compliance_filing": (
        "10-K",
        "10-Q",
        "8-K",
        "S-1",
        "DEF 14A",
        "13D",
        "13G",
        "Form 4",
        "20-F",
        "6-K",
        "other",
    ),
    "court_opinion": (),
}

#: Observed subclass *surfaces* in mailroom-dataset ``ground_truth.expected_subclass``.
#: Used by tests to pin corpus coverage; normalizers must resolve every value
#: to a key in :data:`DOC_TYPE_SUBCLASSES` (or ``other`` only when the
#: surface is the canonical other-bucket).
CORPUS_SUBCLASS_SURFACES: dict[str, tuple[str, ...]] = {
    "contract": (
        "Affiliate_Agreements",
        "Agency Agreements",
        "Co_Branding",
        "Collaboration",
        "Consulting Agreements",
        "Development",
        "Distributor",
        "Endorsement",
        "Franchise",
        "Hosting",
        "IP",
        "Joint Venture",
        "Joint Venture _ Filing",
        "License_Agreements",
        "Maintenance",
        "Manufacturing",
        "Marketing",
        "Non_Compete_Non_Solicit",
        "Outsourcing",
        "Promotion",
        "Reseller",
        "Service",
        "Sponsorship",
        "Strategic Alliance",
        "Supply",
        "Transportation",
    ),
    "merger_agreement": (
        "all_cash",
        "all_stock",
        "mixed_cash_stock",
        "mixed_cash_stock_election",
        "other",
    ),
    "corporate_record": (
        "articles_of_incorporation",
        "board_resolution",
        "bylaws",
        "charter_amendment",
        "indenture",
        "officer_certificate",
        "other",
        "powers_of_attorney",
        "rights_instrument",
        "subsidiary_list",
    ),
    "correspondence": (
        "attorney_demand",
        "demand",
        "email",
        "letter",
        "meeting_request",
        "memo",
        "notice",
        "press_release",
    ),
    "insurance_claim": (
        "auto",
        "carrier",
        "inpatient",
        "outpatient",
        "pde",
        "property",
    ),
}

#: Ground-truth columns that actually vary by document type in the merge.
#: Classification subclasses are listed even when they are not extraction fields.
CORPUS_DIFFERENTIATORS: dict[str, tuple[str, ...]] = {
    "contract": ("expected_subclass", "cuad_clause_labels"),
    "merger_agreement": ("expected_subclass", "maud_clause_labels"),
    "insurance_claim": (
        "expected_subclass",
        "claim_number",
        "policy_number",
        "insurer",
        "insured_party",
        "claim_type",
        "date_of_loss",
        "date_filed",
        "claimed_amount",
        "damages_description",
        "coverage_determination",
        "supporting_documents",
    ),
    "correspondence": (
        "expected_subclass",
        "content_topic",
        "topic_evidence",
        "sentiment_label",
        "sentiment_score",
        "sentiment_evidence",
        "label_evidence",
    ),
    "corporate_record": ("expected_subclass",),
    "due_diligence": (),
    "compliance_filing": ("expected_subclass",),
    "court_opinion": (),
}

#: Extraction-schema fields each specialist suite must score, aligned to
#: live mailroom ``EXTRACTION_SCHEMAS`` + taxonomy ``field_types``.
#: Merger is ``MergerAgreementExtraction`` (no CUAD family/clauses).
#: ``document_name`` is on the contracts / merger schema.
CORPUS_EXTRACTION_FIELDS: dict[str, tuple[str, ...]] = {
    "contract": (
        "document_name",
        "parties",
        "effective_date",
        "term_length",
        "governing_law",
        "contract_value",
        "renewal_terms",
        "cuad_family",
        "merger_consideration",
        "cuad_clauses",
        "maud_clauses",
    ),
    "merger_agreement": (
        "document_name",
        "parties",
        "effective_date",
        "effective_time",
        "governing_law",
        "merger_consideration",
        "maud_clauses",
        "intent",
        "subject_matter",
        "keywords",
    ),
    "corporate_record": (
        "entity_name",
        "record_type",
        "effective_date",
        "signatories",
        "jurisdiction",
        "filing_number",
        "intent",
        "subject_matter",
        "keywords",
    ),
    "due_diligence": (
        "target_entity",
        "diligence_type",
        "material_findings",
        "risk_flags",
        "outstanding_items",
        "document_date",
        "prepared_by",
    ),
    "correspondence": (
        "sender",
        "recipient",
        "additional_recipients",
        "communication_type",
        "communication_date",
        "demand_amount",
        "action_items",
        "urgency",
        "intent",
        "subject_matter",
        "keywords",
    ),
    "compliance_filing": (
        "filing_type",
        "regulatory_body",
        "filing_date",
        "due_date",
        "entity_name",
        "key_requirements",
        "status",
        "reference_number",
    ),
    "court_opinion": (
        "case_name",
        "court",
        "date_decided",
        "docket_number",
        "opinion_type",
        "parties",
        "holding",
        "legal_issues",
        "outcome",
        "citations",
        "authored_by",
    ),
    "insurance_claim": (
        "claim_number",
        "policy_number",
        "insurer",
        "insured_party",
        "claim_type",
        "date_of_loss",
        "date_filed",
        "claimed_amount",
        "adjuster",
        "damages_description",
        "coverage_determination",
        "denial_reasons",
        "supporting_documents",
        "intent",
        "subject_matter",
        "keywords",
        "claim_checklist",
    ),
}

#: CUAD v1 clause-category names as they appear in ``cuad_clause_labels``.
CUAD_CLAUSE_CATEGORIES: tuple[str, ...] = (
    "Affiliate License-Licensee",
    "Affiliate License-Licensor",
    "Agreement Date",
    "Anti-Assignment",
    "Audit Rights",
    "Cap On Liability",
    "Change Of Control",
    "Competitive Restriction Exception",
    "Covenant Not To Sue",
    "Document Name",
    "Effective Date",
    "Exclusivity",
    "Expiration Date",
    "Governing Law",
    "Insurance",
    "Ip Ownership Assignment",
    "Irrevocable Or Perpetual License",
    "Joint Ip Ownership",
    "License Grant",
    "Liquidated Damages",
    "Minimum Commitment",
    "Most Favored Nation",
    "No-Solicit Of Customers",
    "No-Solicit Of Employees",
    "Non-Compete",
    "Non-Disparagement",
    "Non-Transferable License",
    "Notice Period To Terminate Renewal",
    "Parties",
    "Post-Termination Services",
    "Price Restrictions",
    "Renewal Term",
    "Revenue/Profit Sharing",
    "Rofr/Rofo/Rofn",
    "Source Code Escrow",
    "Termination For Convenience",
    "Third Party Beneficiary",
    "Uncapped Liability",
    "Unlimited/All-You-Can-Eat-License",
    "Volume Restriction",
    "Warranty Duration",
)

#: MAUD question keys observed in ``maud_clause_labels`` (union across rows).
MAUD_QUESTION_KEYS: tuple[str, ...] = (
    "Absence of Litigation Closing Condition",
    "Accuracy of Target R&W Closing Condition",
    "Agreement provides for matching rights in connection with COR",
    "Agreement provides for matching rights in connection with FTR",
    "Breach of Meeting Covenant",
    "Breach of No Shop",
    "Compliance with Covenant Closing Condition",
    "FTR Triggers",
    "Fiduciary exception to COR covenant",
    "Fiduciary exception:  Board determination (no-shop)",
    "General Antitrust Efforts Standard",
    "Intervening Event Definition",
    "Knowledge Definition",
    "Limitations on FTR Exercise",
    "MAE Definition",
    "Negative interim operating covenant",
    "No-Shop",
    "Ordinary course covenant",
    "Specific Performance",
    "Superior Offer Definition",
    "Tail Period & Acquisition Proposal Details",
    "Type of Consideration",
)

MAUD_CLAUSE_CATEGORIES: tuple[str, ...] = (
    "Conditions to Closing",
    "Deal Protection and Related Provisions",
    "General Information",
    "Knowledge",
    "Material Adverse Effect",
    "Operating and Efforts Covenant",
    "Remedies",
)

CORRESPONDENCE_TOPICS: tuple[str, ...] = (
    "announcements",
    "energy_market",
    "finance_earnings",
    "general_business",
    "hr_personnel",
    "it_systems",
    "legal_contracts",
    "marketing_clients",
    "regulatory",
    "scheduling",
    "travel_logistics",
)

#: Enron ``sentiment_label`` catalog (1,000/1,000 correspondence rows populated
#: at the v9 pin ``46a4d3c2``: neutral 566 / positive 254 / negative 180).
CORRESPONDENCE_SENTIMENT_LABELS: tuple[str, ...] = (
    "negative",
    "neutral",
    "positive",
)

#: Specialist ``claim_type`` extraction enum — Hub CMS tokens first, then
#: legacy FNOL product lines. Orthogonal to the subclass dimension only in
#: the published merge (all 400 rows use CMS tables as ``expected_subclass``
#: and ``claim_type=health``); mailroom now accepts CMS tokens on
#: ``claim_type`` as well (``doc_inventories.INSURANCE_CLAIM_TYPES``).
INSURANCE_CLAIM_TYPES: tuple[str, ...] = (
    "pde",
    "inpatient",
    "outpatient",
    "carrier",
    "auto",
    "property",
    "liability",
    "health",
    "life",
    "workers_comp",
    "other",
)


def normalize_corpus_subclass(doc_type: str | None, value: Any) -> str:
    """Normalize a raw subclass into the canonical key for ``doc_type``.

    - ``contract`` → CUAD family via :func:`normalize_subtype`
    - ``merger_agreement`` → MAUD consideration via :func:`normalize_doc_subclass`
    - every other catalogued type → scoped :func:`normalize_doc_subclass`
    - unknown doc type / empty catalog → ``other``
    """
    if not doc_type:
        return SUBTYPE_UNKNOWN
    allowed = DOC_TYPE_SUBCLASSES.get(doc_type)
    if allowed is None:
        return SUBTYPE_UNKNOWN
    if doc_type == "contract":
        return normalize_subtype(value)
    if not allowed:
        return SUBTYPE_UNKNOWN
    return normalize_doc_subclass(value, allowed=set(allowed))


def subclass_equivalent(doc_type: str | None, a: Any, b: Any) -> bool:
    """Family-level equivalence scoped to the doc type's subclass dimension."""
    na = normalize_corpus_subclass(doc_type, a)
    nb = normalize_corpus_subclass(doc_type, b)
    if na == nb:
        return True
    if doc_type == "contract":
        return equivalent_subtypes(na, nb)
    if doc_type == "merger_agreement":
        return equivalent_doc_subclasses(
            na, nb, allowed=set(DOC_TYPE_SUBCLASSES["merger_agreement"])
        )
    return False


def suite_schema(doc_type: str) -> dict[str, Any]:
    """Structured alignment record for one document class."""
    present = doc_type in CORPUS_DOC_TYPES
    return {
        "doc_type": doc_type,
        "in_corpus": present,
        "subclasses": list(DOC_TYPE_SUBCLASSES.get(doc_type, ())),
        "corpus_subclass_surfaces": list(CORPUS_SUBCLASS_SURFACES.get(doc_type, ())),
        "extraction_fields": list(CORPUS_EXTRACTION_FIELDS.get(doc_type, ())),
        "differentiators": list(CORPUS_DIFFERENTIATORS.get(doc_type, ())),
        "honest_gap": (
            None
            if present
            else (
                f"HONEST GAP: {doc_type} is a native mailroom class but has "
                f"zero rows in {CORPUS_ID}; suite scores the extraction schema "
                "only (no corpus-backed subclass dimension)."
            )
        ),
    }
