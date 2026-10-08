import json

import pytest

from mailroom_reloaded.schemas.extraction import (
    InsuranceClaimExtraction,
    assess_payload,
    get_extraction_schema,
    response_format,
)

DOC_TYPES = ["contract", "merger_agreement", "corporate_record", "correspondence", "insurance_claim"]


def _walk_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for v in node.values():
            yield from _walk_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_objects(v)


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_response_format_is_strict(doc_type):
    rf = response_format(doc_type)
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"]
    schema = rf["json_schema"]["schema"]
    objs = list(_walk_objects(schema))
    assert objs
    for obj in objs:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])
    for prop in schema["properties"].values():
        branches = prop.get("anyOf", [prop])
        types = []
        for b in branches:
            t = b.get("type")
            types.extend(t if isinstance(t, list) else [t])
        assert "null" in types


def test_unknown_doc_type_raises():
    with pytest.raises(KeyError):
        get_extraction_schema("memo")
    with pytest.raises(KeyError):
        response_format("memo")


def test_insurance_gt_shape_validates():
    # GT-shaped row built from the model's fields (dataset unreachable).
    gt = {
        "claim_number": "CLM-001",
        "policy_number": "POL-9",
        "insurer": "Acme",
        "insured_party": "Jane Doe",
        "claim_type": "auto",
        "date_of_loss": "2023-04-01",
        "date_filed": "2023-04-05",
        "claimed_amount": "$12,500.00",
        "adjuster": None,
        "damages_description": "rear collision",
        "coverage_determination": "approved",
        "denial_reasons": [],
        "supporting_documents": ["photos"],
        "intent": None,
        "subject_matter": None,
        "keywords": ["auto"],
        "claim_checklist": [],
    }
    assert set(gt) <= set(InsuranceClaimExtraction.model_fields)
    m = InsuranceClaimExtraction.model_validate(gt)
    assert m.claimed_amount == 12500.0
    assert m.date_of_loss == "2023-04-01"
    res = assess_payload("insurance_claim", gt)
    assert res.schema_valid and "claimed_amount" in res.coerced_fields


def test_insurance_date_objects_coerced():
    import datetime

    m = InsuranceClaimExtraction.model_validate({"date_filed": datetime.date(2023, 1, 2)})
    assert m.date_filed == "2023-01-02"


def test_assess_parses_fenced_json():
    raw = "```json\n" + json.dumps({"sender": "a@b.c", "demand_amount": 5}) + "\n```"
    res = assess_payload("correspondence", raw)
    assert res.schema_valid is True
    assert res.parse_error is None
    assert res.parsed["sender"] == "a@b.c"


def test_assess_reports_parse_error():
    res = assess_payload("correspondence", "not json at all")
    assert res.schema_valid is False
    assert res.parsed is None
    assert res.parse_error


def test_assess_schema_invalid():
    res = assess_payload("correspondence", {"parties": 3, "demand_amount": "lots"})
    assert res.schema_valid is False
    assert res.parsed is not None
