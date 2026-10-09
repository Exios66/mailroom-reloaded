"""Score registry and emission, and the review-cause port (trace-replay Task 2)."""

from __future__ import annotations

import json
import math

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mailroom_reloaded.obs.reconsideration import (
    CAUSES,
    CLASS_MISS,
    EXTRACTION_MISS,
    JUDGE_MISS,
    JUDGE_PARTIAL,
    NEEDS_JUDGE,
    REPORTING_INCOMPLETE,
    SCHEMA_INVALID,
    SUBCLASS_MISS,
    collect_review_causes,
    should_reconsider,
)
from mailroom_reloaded.obs.scores import SCORE_PREFIX, SCORE_SPECS, emit_score, spec_for


@pytest.fixture
def span():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("t").start_as_current_span("s") as s:
        yield s
    exporter.clear()


def test_registry_is_well_formed() -> None:
    for name, spec in SCORE_SPECS.items():
        assert spec.name == name
        assert spec.data_type in {"numeric", "boolean", "categorical", "json"}
        assert spec.tier in {0, 1, 2, 3}
        assert spec.rollup in {"mean", "sum", "none"}
        assert spec.scope == "root" or spec.scope.startswith("node:")


def test_field_scores_resolve_dynamically() -> None:
    assert spec_for("extraction_field_score.party_name").tier == 2
    assert spec_for("extraction_field_score.") is None
    assert spec_for("extraction_field_score." + "x" * 100) is None
    assert spec_for("nope") is None


def test_emit_writes_attribute_and_event(span) -> None:
    assert emit_score(span, "success_rate", 1) is True
    assert emit_score(span, "schema_valid", 1) is True  # 0/1 accepted for booleans
    assert emit_score(span, "review_causes", ["class_miss"]) is True
    assert span.attributes[SCORE_PREFIX + "success_rate"] == 1.0
    assert span.attributes[SCORE_PREFIX + "schema_valid"] is True
    assert json.loads(span.attributes[SCORE_PREFIX + "review_causes"]) == ["class_miss"]
    names = [(e.name, e.attributes["name"]) for e in span.events]
    assert names == [
        ("mailroom.score", "success_rate"),
        ("mailroom.score", "schema_valid"),
        ("mailroom.score", "review_causes"),
    ]


def test_unknown_names_and_bad_types_raise_when_strict_and_log_otherwise(span) -> None:
    with pytest.raises(ValueError):
        emit_score(span, "made_up_score", 1)
    with pytest.raises(ValueError):
        emit_score(span, "success_rate", "high")
    with pytest.raises(ValueError):
        emit_score(span, "success_rate", math.nan)
    with pytest.raises(ValueError):
        emit_score(span, "schema_valid", 7)
    assert emit_score(span, "made_up_score", 1, strict=False) is False
    assert (
        emit_score(span, "success_rate", True, strict=False) is False
    )  # bool is not a number
    assert not [k for k in span.attributes if k.endswith("made_up_score")]


# --- review causes: vectors from The-Mailroom's tests/test_reconsideration.py -------------------
def test_clean_archived_run_has_no_causes() -> None:
    assert (
        collect_review_causes(doc_type="contract", scores={"schema_valid": True}) == []
    )


def test_judge_miss_and_partial() -> None:
    assert collect_review_causes(verdict="MISS") == [JUDGE_MISS]
    assert collect_review_causes(verdict="partial") == [JUDGE_PARTIAL]
    assert collect_review_causes(verdict="CORRECT") == []


def test_class_miss_against_ground_truth() -> None:
    assert collect_review_causes(
        doc_type="contract", expected_class="correspondence"
    ) == [CLASS_MISS]
    assert collect_review_causes(
        doc_type="contract", expected_class="merger_agreement"
    ) == [CLASS_MISS]
    assert (
        collect_review_causes(
            doc_type="merger_agreement", expected_class="merger_agreement"
        )
        == []
    )
    assert collect_review_causes(scores={"class_correct": False}) == [CLASS_MISS]
    assert collect_review_causes(
        doc_type="a", expected_class="b", scores={"class_correct": 0}
    ) == [CLASS_MISS]


def test_subclass_miss() -> None:
    assert collect_review_causes(doc_subclass="nda", expected_subclass="license") == [
        SUBCLASS_MISS
    ]


def test_extraction_score_below_the_floor_is_a_miss() -> None:
    assert collect_review_causes(scores={"extraction_overall_score": 0.41}) == [
        EXTRACTION_MISS
    ]
    assert collect_review_causes(scores={"expected_field_presence": 0.2}) == [
        EXTRACTION_MISS
    ]
    assert collect_review_causes(
        scores={"extraction_overall_score": 0.41, "expected_field_presence": 0.2}
    ) == [EXTRACTION_MISS]
    assert collect_review_causes(scores={"extraction_overall_score": 0.95}) == []
    assert collect_review_causes(
        scores={"extraction_overall_score": 0.9}, floor=0.95
    ) == [EXTRACTION_MISS]


def test_incomplete_reporting_schema_and_judge_flags() -> None:
    assert collect_review_causes(scores={"completeness_label": "INCOMPLETE"}) == [
        REPORTING_INCOMPLETE
    ]
    assert collect_review_causes(scores={"completeness": 0.3}) == [REPORTING_INCOMPLETE]
    assert collect_review_causes(scores={"schema_valid": "false"}) == [SCHEMA_INVALID]
    assert collect_review_causes(scores={"extraction_needs_judge_review": True}) == [
        NEEDS_JUDGE
    ]
    assert collect_review_causes(
        scores={"guardrail_triggered": 1, "parse_error": "true"}
    ) == ["guardrail", "parse_error"]


def test_causes_are_unique_and_in_the_canonical_vocabulary() -> None:
    causes = collect_review_causes(
        doc_type="a",
        expected_class="b",
        scores={"class_correct": False, "schema_valid": False},
        verdict="MISS",
    )
    assert causes == [CLASS_MISS, JUDGE_MISS, SCHEMA_INVALID]
    assert set(causes) <= set(CAUSES)


def test_should_reconsider_only_for_finished_runs() -> None:
    causes = collect_review_causes(verdict="MISS")
    assert should_reconsider("archived", causes) is True
    assert should_reconsider("failed", causes) is False
    assert should_reconsider("extract", causes) is False
    assert should_reconsider("archived", []) is False
