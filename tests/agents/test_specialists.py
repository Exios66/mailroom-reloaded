import json
import math

import pytest
from helpers import assert_no_gt

from mailroom_reloaded.agents.sorter import sort
from mailroom_reloaded.agents.specialists import (
    _coverage,
    extract,
    extraction_confidence,
    prepare_input,
)
from mailroom_reloaded.ingest.bert import Handoff, SortMode
from mailroom_reloaded.prompts.loader import load_prompt
from mailroom_reloaded.schemas.extraction import get_extraction_schema
from mailroom_reloaded.settings import RunConditions, load_taxonomy


def _dagger_cond():
    return RunConditions(54000, 6144, 0.7, 2, "dagger")


# Derived from `mailroom-dataset` @ ed7576b6 ground_truth TRAIN split:
# fields whose ground-truth presence rate is >= 0.8, mapped to extraction
# schema field names. Pins the taxonomy `required_fields` block (spec §6).
_DERIVED_REQUIRED_FIELDS = {
    "contract": ["cuad_clauses"],
    "merger_agreement": ["maud_clauses"],
    "corporate_record": ["intent", "subject_matter", "keywords"],
    "correspondence": ["intent", "subject_matter", "keywords"],
    "insurance_claim": [
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
        "intent",
        "subject_matter",
        "keywords",
    ],
}


def test_required_fields_block_present_and_schema_aligned():
    raw = load_taxonomy().raw
    rf = raw["required_fields"]
    assert set(rf) == set(_DERIVED_REQUIRED_FIELDS)
    for doc_type, fields in _DERIVED_REQUIRED_FIELDS.items():
        assert rf[doc_type] == fields
        schema_fields = get_extraction_schema(doc_type).model_fields
        for name in rf[doc_type]:
            assert name in schema_fields, f"{doc_type}.{name} not an extraction field"


def test_coverage_uses_configured_required_fields():
    fields = load_taxonomy().raw["required_fields"]["insurance_claim"]
    assert _coverage("insurance_claim", {}) == 0.0
    full = {name: "x" for name in fields}
    assert _coverage("insurance_claim", full) == pytest.approx(1.0)
    # `adjuster` is populated on only 13.6% of train rows and is excluded from
    # the block; a present-but-unrequired field must not move coverage.
    assert "adjuster" not in fields
    assert _coverage("insurance_claim", {**full, "adjuster": "A. Adjuster"}) == pytest.approx(1.0)
    # A missing required field lowers coverage by exactly one slot.
    partial = {k: v for k, v in full.items() if k != "claim_number"}
    assert _coverage("insurance_claim", partial) == pytest.approx(
        (len(fields) - 1) / len(fields)
    )


def test_coverage_falls_back_to_schema_fields_when_unconfigured(monkeypatch):
    import mailroom_reloaded.agents.specialists as specialists

    class _FakeTaxonomy:
        def __init__(self, raw):
            self.raw = raw

    raw = {k: v for k, v in load_taxonomy().raw.items() if k != "required_fields"}
    monkeypatch.setattr(specialists, "load_taxonomy", lambda: _FakeTaxonomy(raw))
    # Fallback requires every contract schema field except `reasoning`;
    # only `cuad_clauses` present -> 1/11, proving the block changes behavior.
    n = len(get_extraction_schema("contract").model_fields) - 1
    assert specialists._coverage("contract", {"cuad_clauses": ["x"]}) == pytest.approx(1 / n)


def test_system_prompt_is_frozen_bytes(mock_provider):
    tax = load_taxonomy()
    for doc_type, doc_class in tax.classes.items():
        mock_provider.reply("{}")
        extract("some text", doc_type, None, tools=False)
        req = mock_provider.requests[-1]
        assert req["messages"][0]["content"] == load_prompt(
            doc_class.specialist, prompt_set="frozen_v1"
        )


def test_response_format_sent(mock_provider):
    mock_provider.reply("{}")
    extract("some text", "correspondence", None, tools=False)
    req = mock_provider.requests[-1]
    rf = req["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["additionalProperties"] is False


def test_conditions_applied(mock_provider):
    mock_provider.reply("{}")
    extract("some text", "insurance_claim", None, tools=False)
    req = mock_provider.requests[-1]
    assert req["max_tokens"] == 8192
    assert req["temperature"] == 0.1

    mock_provider.reply("{}")
    extract("some text", "contract", None, tools=False)
    req = mock_provider.requests[-1]
    assert req["max_tokens"] == 8192
    assert req["temperature"] == 0.7


def test_frozen_merger_head_tail_30k(mock_provider):
    cond = load_taxonomy().specialist_conditions("merger_agreement")
    text = "0" * 180000 + "ZZZMIDDLEZZZ" + "1" * 201749
    assert len(text) == 381761

    windows = prepare_input(text, "merger_agreement", cond)
    assert sum(len(window) for window in windows) <= 30000
    assert windows[0] == text[:15000]
    assert windows[-1] == text[-15000:]

    mock_provider.reply("{}")
    extract(text, "merger_agreement", None, tools=False)
    assert len(mock_provider.requests) == 1
    user = mock_provider.requests[0]["messages"][1]["content"]
    assert "ZZZMIDDLEZZZ" not in user
    assert text[:15000] in user
    assert text[-15000:] in user


def test_dagger_windows(mock_provider):
    cond = _dagger_cond()
    text = ("0123456789" * 38177)[:381761]
    assert len(text) == 381761

    windows = prepare_input(text, "merger_agreement", cond)
    assert len(windows) == math.ceil((381761 - 6500) / (47000 - 6500))
    assert all(len(window) <= 54000 for window in windows)
    assert windows[0] == text[:47000]

    for _ in windows:
        mock_provider.reply("{}")
    extract(text, "merger_agreement", None, tools=False, cond=cond)

    assert len(mock_provider.requests) == len(windows)
    for req in mock_provider.requests:
        assert req["max_tokens"] == 6144
        assert req["top_p"] == 0.8
        assert req["presence_penalty"] == 1.0
        assert req["top_k"] == 20


def test_dagger_resample_once_on_length(mock_provider):
    cond = _dagger_cond()
    mock_provider.length_capped().reply("{}")
    result = extract("0" * 40000, "merger_agreement", None, tools=False, cond=cond)
    assert result.calls == 2
    assert result.error_kind is None
    assert result.data is not None
    assert result.schema_valid is True


def test_length_cap_error_kind(mock_provider):
    mock_provider.length_capped()
    result = extract("some contract text", "contract", None, tools=False)
    assert result.error_kind == "LengthFinishReasonError"
    assert result.data is None
    assert result.schema_valid is False


def test_malformed_json_one_repair(mock_provider):
    mock_provider.reply("not json at all").reply('{"parties": ["Acme"]}')
    result = extract("some contract text", "contract", None, tools=False)
    assert result.schema_valid is True
    assert result.calls == 2
    assert result.data["parties"] == ["Acme"]


def test_parity_mode_tools_off(mock_provider):
    mock_provider.reply("{}")
    extract("some text", "contract", None, prompt_set="sand37")
    assert mock_provider.requests
    assert all("tools" not in req for req in mock_provider.requests)

    mock_provider.requests.clear()
    mock_provider.reply("{}")
    extract("some text", "contract", None, tools=False)
    assert mock_provider.requests
    assert all("tools" not in req for req in mock_provider.requests)


def test_extraction_confidence_formula():
    value = extraction_confidence(True, 0.5, 0.9)
    assert value == pytest.approx(0.6 * 0.5 + 0.4 * 0.9)
    assert extraction_confidence(False, 0.5, 0.9) == 0.0


def test_no_ground_truth_in_messages(mock_provider):
    gt_row = {"claim_number": "GT-SECRET-CLAIM-9911", "insurer": "Secret Holdings LLC"}

    mock_provider.reply("thinking").reply(
        json.dumps(
            {
                "doc_type": "insurance_claim",
                "doc_subclass": "carrier",
                "confidence": 0.9,
                "doc_type_disagree": False,
                "doc_type_disagree_reason": None,
            }
        )
    )
    sort("policy document text", Handoff(SortMode.FULL, None, "", "bert_unavailable"))

    mock_provider.reply("{}")
    extract("policy document text", "insurance_claim", None, tools=False)

    assert_no_gt(mock_provider.requests, gt_row)
