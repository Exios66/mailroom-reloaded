"""Per-class extraction schemas, strict JSON schema builder and compliance checks.

Models are ported from llm-mailroom ``schemas/documents.py``; the assessment
helpers from local-mailroom-sandbox ``eval/schema_adherence.py``.
"""

from __future__ import annotations

import ast
import copy
import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

_MONEY_RE = re.compile(r"^\(?\s*[-+]?\s*(?:USD|US\$|\$)?\s*[-+]?\s*([\d,]*\.?\d+)\s*(?:USD)?\s*\)?$", re.IGNORECASE)


def _coerce_money(v: Any) -> Any:
    """Accept '$12,500.00', '12,500', '1500 USD' etc. as floats; leave the rest to pydantic."""
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        m = _MONEY_RE.match(s)
        if m:
            try:
                n = float(m.group(1).replace(",", ""))
            except ValueError:
                return v
            return -n if ("-" in s or s.startswith("(")) else n
    return v


def _coerce_date(v: Any) -> Any:
    """Serves direct Python callers (date objects); model JSON only ever carries strings.

    Accept date/datetime objects (and ints are left alone) as ISO strings."""
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    return v


class ReasoningEntry(BaseModel):
    field: str
    evidence: str | None = None
    section_ref: str | None = None


class Reasoning(BaseModel):
    """Per-field reasoning trace the frozen prompts ask for: {summary, entries[]}."""

    summary: str | None = None
    entries: list[ReasoningEntry] | None = None


def _coerce_reasoning(v: Any) -> Any:
    """Accept legacy free-form dict traces by folding them into {summary, entries}."""
    if not isinstance(v, dict) or not v or set(v) <= {"summary", "entries"}:
        return v
    entries = []
    for k, val in v.items():
        if k in ("summary", "entries"):
            continue
        entries.append({"field": str(k), "evidence": None if val is None else str(val)})
    out: dict[str, Any] = {"entries": entries}
    if isinstance(v.get("summary"), str):
        out["summary"] = v["summary"]
    if isinstance(v.get("entries"), list):
        out["entries"] = v["entries"] + entries
    return out


class ContractExtraction(BaseModel):
    reasoning: Reasoning | None = None  # first: prompt says produce it before the values
    document_name: str | None = None
    parties: list[str] = Field(default_factory=list)
    effective_date: str | None = None
    term_length: str | None = None
    governing_law: str | None = None
    contract_value: str | None = None
    renewal_terms: str | None = None
    cuad_family: str | None = None
    merger_consideration: str | None = None
    cuad_clauses: list[str] = Field(default_factory=list)
    maud_clauses: list[str] = Field(default_factory=list)

    _reasoning = field_validator("reasoning", mode="before")(_coerce_reasoning)
    _dates = field_validator("effective_date", mode="before")(_coerce_date)


class MergerAgreementExtraction(BaseModel):
    reasoning: Reasoning | None = None  # first: prompt says produce it before the values
    document_name: str | None = None
    parties: list[str] = Field(default_factory=list)
    effective_date: str | None = None
    effective_time: str | None = None
    governing_law: str | None = None
    merger_consideration: str | None = None
    maud_clauses: list[str] = Field(default_factory=list)
    intent: str | None = None
    subject_matter: str | None = None
    keywords: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    _reasoning = field_validator("reasoning", mode="before")(_coerce_reasoning)
    _dates = field_validator("effective_date", mode="before")(_coerce_date)


class CorporateRecordExtraction(BaseModel):
    entity_name: str = ""
    record_type: str = ""  # articles_of_incorporation, bylaws, powers_of_attorney, rights_instrument, other
    effective_date: str | None = None
    signatories: list[str] = Field(default_factory=list)
    jurisdiction: str | None = None
    filing_number: str | None = None
    intent: str | None = None
    subject_matter: str | None = None
    keywords: list[str] = Field(default_factory=list)

    _dates = field_validator("effective_date", mode="before")(_coerce_date)


class CorrespondenceExtraction(BaseModel):
    sender: str | None = None
    recipient: str | None = None
    additional_recipients: list[str] = Field(default_factory=list)
    communication_type: str = ""  # email, letter, memo, notice, demand, attorney_demand, press_release, meeting_request
    communication_date: str | None = None
    demand_amount: float | None = None
    action_items: list[str] = Field(default_factory=list)  # capped <=3
    urgency: str = ""
    intent: str | None = None
    subject_matter: str | None = None
    keywords: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    _dates = field_validator("communication_date", mode="before")(_coerce_date)
    _money = field_validator("demand_amount", mode="before")(_coerce_money)


class InsuranceClaimExtraction(BaseModel):
    claim_number: str | None = None
    policy_number: str | None = None
    insurer: str = ""
    insured_party: str = ""
    claim_type: str = ""  # pde, inpatient, outpatient, carrier, auto, property, liability, health, life, workers_comp, other
    date_of_loss: str | None = None
    date_filed: str | None = None
    claimed_amount: float | None = None
    adjuster: str | None = None  # CMS / DE-SynPUF rows often have no adjuster
    damages_description: str = ""
    coverage_determination: str = ""  # approved, denied, partial, pending
    denial_reasons: list[str] = Field(default_factory=list)
    supporting_documents: list[str] = Field(default_factory=list)
    intent: str | None = None
    subject_matter: str | None = None
    keywords: list[str] = Field(default_factory=list)
    claim_checklist: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    _dates = field_validator("date_of_loss", "date_filed", mode="before")(_coerce_date)
    _money = field_validator("claimed_amount", mode="before")(_coerce_money)


EXTRACTION_SCHEMAS: dict[str, type[BaseModel]] = {
    "contract": ContractExtraction,
    "merger_agreement": MergerAgreementExtraction,
    "corporate_record": CorporateRecordExtraction,
    "correspondence": CorrespondenceExtraction,
    "insurance_claim": InsuranceClaimExtraction,
}


def get_extraction_schema(doc_type: str) -> type[BaseModel]:
    try:
        return EXTRACTION_SCHEMAS[doc_type]
    except KeyError:
        raise KeyError(f"unknown doc_type {doc_type!r}; expected one of {sorted(EXTRACTION_SCHEMAS)}") from None


# --------------------------------------------------------------------------- strict schema

def _make_nullable(prop: dict[str, Any]) -> dict[str, Any]:
    if "anyOf" in prop:
        if any(b.get("type") == "null" for b in prop["anyOf"]):
            return prop
        return {**prop, "anyOf": [*prop["anyOf"], {"type": "null"}]}
    if prop.get("type") == "null":
        return prop
    t = prop.get("type")
    if "$ref" in prop or t is None:
        rest = {k: v for k, v in prop.items() if k in ("title", "description")}
        core = {k: v for k, v in prop.items() if k not in rest}
        return {**rest, "anyOf": [core, {"type": "null"}]}
    if isinstance(t, list):
        return prop if "null" in t else {**prop, "type": [*t, "null"]}
    return {**prop, "type": [t, "null"]} if t in ("string", "number", "integer", "boolean") else {
        "anyOf": [prop, {"type": "null"}]
    }


def _strictify(node: Any) -> None:
    if isinstance(node, dict):
        node.pop("default", None)
        if node.get("type") == "object" or "properties" in node:
            props = node.setdefault("properties", {})
            for k in props:
                props[k] = _make_nullable(props[k])
            node["required"] = list(props)
            node["additionalProperties"] = False
        for v in list(node.values()):
            _strictify(v)
    elif isinstance(node, list):
        for v in node:
            _strictify(v)


def response_format(doc_type: str) -> dict[str, Any]:
    model = get_extraction_schema(doc_type)
    schema = copy.deepcopy(model.model_json_schema())
    _strictify(schema)
    return {
        "type": "json_schema",
        "json_schema": {"name": f"{doc_type}_extraction", "strict": True, "schema": schema},
    }


# --------------------------------------------------------------------------- compliance

@dataclass
class SchemaAssessment:
    parsed: dict | None
    schema_valid: bool
    parse_error: str | None = None
    coerced_fields: list[str] = field(default_factory=list)


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_object_text(text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj, "json"
    except json.JSONDecodeError:
        pass
    try:
        obj = ast.literal_eval(text)
        if isinstance(obj, dict):
            return obj, "python_repr"
    except (SyntaxError, ValueError):
        pass
    match = _JSON_OBJECT_RE.search(text)
    if match and (text[: match.start()].strip() or text[match.end():].strip()):
        for loader, how in ((json.loads, "wrapped_json"), (ast.literal_eval, "wrapped_python_repr")):
            try:
                obj = loader(match.group(0))
            except (SyntaxError, ValueError):  # JSONDecodeError is a ValueError
                continue
            if isinstance(obj, dict):
                return obj, how
    return None, None


def coerce_predicted_payload(predicted: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Turn a prediction into a dict, or return (None, reason)."""
    if predicted is None:
        return None, "null_payload"
    if isinstance(predicted, dict):
        if predicted.get("_parse_error"):
            raw = predicted.get("_raw")
            if isinstance(raw, str) and raw.strip():
                recovered, _ = coerce_predicted_payload(raw)
                if recovered is not None:
                    return recovered, "flagged_parse_error"
            return predicted, "flagged_parse_error"
        return predicted, None
    if isinstance(predicted, (bytes, bytearray)):
        try:
            predicted = predicted.decode("utf-8")
        except UnicodeDecodeError:
            return None, "undecodable_bytes"
    if not isinstance(predicted, str):
        return None, f"unsupported_type:{type(predicted).__name__}"
    text = predicted.strip()
    if not text:
        return None, "empty_string"
    parsed, _ = _parse_object_text(text)
    if parsed is None:
        return None, "unparseable"
    return parsed, None


def _pydantic_valid(doc_type: str, payload: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate against the model; return (valid, fields whose value was coerced)."""
    model = get_extraction_schema(doc_type)
    try:
        validated = model.model_validate(payload)
    except ValidationError:
        return False, []
    def _changed(k: str, v: Any) -> bool:
        got = getattr(validated, k)
        if isinstance(got, BaseModel):
            return got.model_dump(exclude_unset=True) != v
        return got != v

    coerced = [k for k, v in payload.items() if k in model.model_fields and _changed(k, v)]
    return True, sorted(coerced)


def assess_payload(doc_type: str, raw: str | dict) -> SchemaAssessment:
    get_extraction_schema(doc_type)  # KeyError on unknown class
    payload, reason = coerce_predicted_payload(raw)
    if payload is None or reason is not None:
        return SchemaAssessment(parsed=payload, schema_valid=False, parse_error=reason or "unparseable")
    valid, coerced = _pydantic_valid(doc_type, payload)
    return SchemaAssessment(parsed=payload, schema_valid=valid, parse_error=None, coerced_fields=coerced)
