"""Controlled ``intent`` vocabularies for the three classes that carry one.

Ported verbatim from llm-mailroom ``langchain_agents.doc_inventories`` at
``bee7f46``; this module is now the single source of the vocabulary and
mailroom imports it from here. ``merger_agreement`` and ``contract`` have no
vocabulary: their ``intent`` stays a free-form ``name`` field.
"""

from __future__ import annotations

import re
from typing import Any

INTENT_LABELS: dict[str, tuple[str, ...]] = {
    "corporate_record": (
        "governance_rules",          # bylaws / governance instruments
        "corporate_action_approval", # board written consents / resolutions
        "entity_formation",          # articles / certificates of incorporation
        "authority_delegation",      # powers of attorney
        "investor_rights",           # rights instruments, warrants, specimen stock
        "other",
    ),
    "correspondence": (
        "payment_demand",            # demand for payment / cure
        "notice",                    # formal notice of a fact, breach, or intent
        "analysis",                  # internal memo analyzing options/remedies
        "request",                   # request for information or action
        "update",                    # informational status update
        "meeting_invite",            # meeting/calendar request
        "press_communication",       # press release / public statement
        "other",
    ),
    "insurance_claim": (
        "claim_filing",              # first notice of loss / claim submission
        "coverage_determination",    # approved / denied / partial determination
        "loss_report",               # adjuster report / appraisal / examination
        "claim_data_record",         # CMS / DE-SynPUF table row (pde, inpatient, ...)
        "other",
    ),
}

INTENT_DESCRIPTIONS: dict[str, str] = {
    "corporate_record": (
        "One controlled purpose label for THIS corporate record. "
        "governance_rules = bylaws or equivalent governance instruments; "
        "corporate_action_approval = board written consents / resolutions "
        "authorizing transactions or actions; entity_formation = articles or "
        "certificates of incorporation/formation; authority_delegation = "
        "powers of attorney; investor_rights = stockholder rights, warrants, "
        "preferred certificates, specimen stock. other = residual."
    ),
    "correspondence": (
        "One controlled purpose label for THIS message. payment_demand = "
        "demand for payment or cure; notice = formal notice of a fact, breach, "
        "or intent; analysis = internal memo analyzing options/remedies; "
        "request = request for information or action; update = informational "
        "status update; meeting_invite = meeting/calendar request; "
        "press_communication = press release / public statement. other = residual."
    ),
    "insurance_claim": (
        "One controlled purpose label for THIS claim document. claim_filing = "
        "first notice of loss / claim submission; coverage_determination = "
        "approved/denied/partial determination letter; loss_report = adjuster "
        "report / appraisal / examination; claim_data_record = CMS/DE-SynPUF "
        "table row (pde, inpatient, outpatient, carrier). other = residual."
    ),
}

INTENT_ALIASES = {
    "record_governance": "governance_rules",
    "recordgovernance": "governance_rules",
    "governance": "governance_rules",
    "governancerules": "governance_rules",
    "governingrules": "governance_rules",
    "bylaws": "governance_rules",
    "boardresolution": "corporate_action_approval",
    "corporateaction": "corporate_action_approval",
    "corporateactionapproval": "corporate_action_approval",
    "approval": "corporate_action_approval",
    "entityformation": "entity_formation",
    "formation": "entity_formation",
    "incorporation": "entity_formation",
    "articlesofincorporation": "entity_formation",
    "powerofattorney": "authority_delegation",
    "authoritydelegation": "authority_delegation",
    "investorrights": "investor_rights",
    "rightsinstrument": "investor_rights",
    "demand": "payment_demand",
    "paymentdemand": "payment_demand",
    "demandpayment": "payment_demand",
    "demandforpayment": "payment_demand",
    "demandletter": "payment_demand",
    "attorneydemand": "payment_demand",
    "noticedefault": "notice",
    "noticeofbreach": "notice",
    "noticeofnoncompliance": "notice",
    "noticeofintent": "notice",
    "analysis": "analysis",
    "remediesanalysis": "analysis",
    "recommendation": "analysis",
    "request": "request",
    "requestinformation": "request",
    "update": "update",
    "statusupdate": "update",
    "meetinginvite": "meeting_invite",
    "meetingrequest": "meeting_invite",
    "schedulemeeting": "meeting_invite",
    "pressrelease": "press_communication",
    "presscommunication": "press_communication",
    "publicstatement": "press_communication",
    "claimfiling": "claim_filing",
    "filing": "claim_filing",
    "firstnoticeofloss": "claim_filing",
    "noticeofloss": "claim_filing",
    "fnol": "claim_filing",
    "initialfnol": "claim_filing",
    "coveragedetermination": "coverage_determination",
    "determination": "coverage_determination",
    "denial": "coverage_determination",
    "denialletter": "coverage_determination",
    "denialnotice": "coverage_determination",
    "denied": "coverage_determination",
    "approved": "coverage_determination",
    "partial": "coverage_determination",
    "claimdenied": "coverage_determination",
    "claimapproved": "coverage_determination",
    "claimpartiallyapproved": "coverage_determination",
    "partialapproval": "coverage_determination",
    "coverage_approval": "coverage_determination",
    "coverage_denial": "coverage_determination",
    "coverage_partial": "coverage_determination",
    "lossreport": "loss_report",
    "adjusterreport": "loss_report",
    "claimdatarecord": "claim_data_record",
    "data record": "claim_data_record",
}


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _normalize(value: Any, keys: tuple[str, ...], aliases: dict[str, str]) -> str:
    """Map a free-text label onto a canonical inventory token.

    Longest canonical key wins so ``attorney_demand`` is not swallowed by
    ``demand``, and ``articles_of_incorporation`` is not swallowed by a
    short alias collision.
    """
    key = _compact(value)
    if not key:
        return ""
    compact_keys = {_compact(k): k for k in keys}
    if key in compact_keys:
        return compact_keys[key]
    alias_norm = {_compact(k): v for k, v in aliases.items()}
    if key in alias_norm:
        return alias_norm[key]
    ranked_aliases = sorted(aliases.items(), key=lambda kv: -len(_compact(kv[0])))
    for alias, canonical in ranked_aliases:
        ak = _compact(alias)
        if len(ak) >= 8 and (key.startswith(ak) or ak in key):
            return canonical
    ranked = sorted(keys, key=lambda item: -len(_compact(item)))
    for canonical in ranked:
        ck = _compact(canonical)
        if not ck or canonical == "other":
            continue
        if key.startswith(ck):
            return canonical
    for canonical in ranked:
        ck = _compact(canonical)
        if not ck or canonical == "other":
            continue
        if len(ck) >= 4 and ck in key:
            return canonical
        if len(ck) >= 3 and ck[:1].isdigit() and ck in key:
            return canonical
    if key == "other":
        return "other"
    return ""


def normalize_intent(doc_type: str | None, value: Any) -> str:
    """Map a free-text purpose onto the class's controlled intent label.

    Unknown/unmapped values return ``""`` (never ``other`` inventively). The
    result is always a member of ``INTENT_LABELS[doc_type]`` — aliases never
    leak a different class's token.
    """
    kind = str(doc_type or "")
    keys = INTENT_LABELS.get(kind, ())
    if not keys:
        return ""
    token = _normalize(value, keys, INTENT_ALIASES)
    return token if token in keys else ""
