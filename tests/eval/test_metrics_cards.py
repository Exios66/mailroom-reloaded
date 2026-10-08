"""Task 21 tests: sorter/specialist KPIs, SAND-37 cards, vLLM telemetry and cost."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailroom_reloaded.eval.cards import build_card, build_master, render_card_md
from mailroom_reloaded.eval.cost import cell_cost, token_split
from mailroom_reloaded.eval.metrics import gate_kpis, sorter_kpis, specialist_kpis
from mailroom_reloaded.eval.vllm_telemetry import telemetry_delta

FIXTURES = Path(__file__).parent / "fixtures"


def _sorter_row(expected_type, predicted_type, expected_sub, predicted_sub, **extra):
    row = {
        "expected": expected_type,
        "doc_type": predicted_type,
        "expected_subclass": expected_sub,
        "doc_subclass": predicted_sub,
    }
    row.update(extra)
    return row


def _card_rows():
    return [
        {
            "filename": "a.txt",
            "doc_type": "correspondence",
            "status": "ok",
            "schema_valid": True,
            "latency_s": 4.0,
            "prompt_tokens": 3000,
            "completion_tokens": 500,
            "calls": 1,
            "overall_score": 0.8,
            "predicted": {"sender": "Alice", "recipient": "Bob"},
            "expected": {"sender": "Alice", "recipient": "Bob"},
        },
        {
            "filename": "b.txt",
            "doc_type": "correspondence",
            "status": "ok",
            "schema_valid": True,
            "latency_s": 6.0,
            "prompt_tokens": 4200,
            "completion_tokens": 700,
            "calls": 1,
            "overall_score": 0.6,
            "predicted": {"sender": "Alice", "recipient": "Carol"},
            "expected": {"sender": "Alice", "recipient": "Bob"},
        },
    ]


# --------------------------------------------------------------- sorter KPIs


def test_sorter_kpis_exact_vs_primary():
    rows = [
        _sorter_row("contract", "contract", "nda", "nda"),
        _sorter_row("contract", "contract", "nda", "license"),
        _sorter_row("contract", "merger_agreement", "nda", "nda"),
        _sorter_row("contract", "merger_agreement", "nda", "license"),
    ]
    kpis = sorter_kpis(rows)
    assert kpis["exact_match"] == 0.25
    assert kpis["primary_accuracy"] == 0.5
    assert kpis["subclass_accuracy"] == 0.5
    assert kpis["subclass_accuracy_given_primary"] == 0.5


def test_sorter_kpis_by_path():
    rows = [
        _sorter_row("contract", "contract", "nda", "nda", sort_mode="FULL"),
        _sorter_row(
            "merger_agreement",
            "merger_agreement",
            "all_cash",
            "all_cash",
            sort_mode="SUBCLASS_ONLY",
            route_trail=["ingest", "sort", "re_sort"],
        ),
    ]
    kpis = sorter_kpis(rows)
    assert set(kpis["by_path"]) == {"SUBCLASS_ONLY", "FULL"}
    assert kpis["by_path"]["FULL"]["primary_accuracy"] == 1.0
    assert kpis["by_path"]["SUBCLASS_ONLY"]["primary_accuracy"] == 1.0
    assert kpis["bert"]["fast_path_rate"] == 0.5
    assert kpis["resort_rate"] == 0.5


# --------------------------------------------------------------- specialist KPIs


def test_micro_vs_mean_f1():
    rows = [
        {"doc_type": "contract", "field_counts": {"tp": 9, "fp": 0, "fn": 1}},
        {"doc_type": "contract", "field_counts": {"tp": 0, "fp": 1, "fn": 0}},
    ]
    kpis = specialist_kpis(rows, "contract")
    assert kpis["micro_f1"] == pytest.approx(0.9, abs=1e-4)
    assert kpis["mean_doc_f1"] == pytest.approx((0.947 + 0.0) / 2, abs=1e-3)


def test_f2_weights_recall():
    rows = [{"doc_type": "contract", "field_counts": {"tp": 1, "fp": 1, "fn": 0}}]
    kpis = specialist_kpis(rows, "contract")
    assert kpis["precision"] == pytest.approx(0.5, abs=1e-4)
    assert kpis["recall"] == pytest.approx(1.0, abs=1e-4)
    assert kpis["f1"] == pytest.approx(2 * 0.5 * 1.0 / 1.5, abs=1e-3)
    assert kpis["f2"] == pytest.approx(5 * 0.5 * 1.0 / (4 * 0.5 + 1.0), abs=1e-3)
    assert kpis["f2"] > kpis["f1"]


def test_wrong_value_counts_fp_and_fn():
    rows = [
        {
            "doc_type": "corporate_record",
            "expected": {"filing_number": "ACME"},
            "predicted": {"filing_number": "Globex"},
        }
    ]
    kpis = specialist_kpis(rows, "corporate_record")
    assert (kpis["tp"], kpis["fp"], kpis["fn"]) == (0, 1, 1)


def test_gate_kpis_decision_mix_and_agreement():
    rows = [
        {"gate_decisions": {"classify": "retry", "extract": "proceed"}, "retry_expected": True},
        {"gate_decisions": {"classify": "proceed", "extract": "human_review"}, "review_expected": True},
    ]
    kpis = gate_kpis(rows)
    assert kpis["decisions"]["classify"] == {"retry": 1, "proceed": 1}
    assert kpis["agreement"]["retry_expected"] == 1.0
    assert kpis["agreement"]["review_expected"] == 1.0


# --------------------------------------------------------------- telemetry


def test_telemetry_delta_from_fixtures():
    before = (FIXTURES / "vllm_metrics_before.txt").read_text(encoding="utf-8")
    after = (FIXTURES / "vllm_metrics_after.txt").read_text(encoding="utf-8")
    delta = telemetry_delta(before, after)
    assert delta.prefix_cache_hit_rate == pytest.approx(0.5, abs=1e-6)
    assert delta.ttft_mean_seconds == pytest.approx(6.0, abs=1e-6)
    assert delta.requests == 43
    assert delta.length_finishes == 3
    assert delta.preemptions == 3
    assert delta.kv_cache_usage_perc == pytest.approx(0.35, abs=1e-6)


# --------------------------------------------------------------- cost


def test_per_token_cost():
    cost = cell_cost(
        None,
        gpus=1,
        usd_per_hour=0.80,
        ok=2,
        total=2,
        tokens=2_000_000,
        pricing="per_token",
        prices=(0.2, 0.6),
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert cost["busy_gpu_usd"] == pytest.approx(0.80, abs=1e-6)


def test_cost_matches_sand37_cell():
    cost = cell_cost(37.8, gpus=1, usd_per_hour=0.80, ok=20, total=20, tokens=100000)
    assert cost["busy_gpu_usd"] == pytest.approx(0.0084, abs=1e-6)
    assert cost["usd_per_ok_document"] == pytest.approx(0.00042, abs=1e-6)


def test_token_split_recovers_known_I():
    rows = []
    for i in range(20):
        chars = 2000 + i * 500
        prompt = round(2702 * 1 + chars / 4.46)
        rows.append(
            {
                "ok": True,
                "prompt_tokens": prompt,
                "calls": 1,
                "input_chars": chars,
                "completion_tokens": 100,
            }
        )
    split = token_split(rows)
    assert split["method"] == "fit"
    assert split["instruction_per_call"] == pytest.approx(2702, rel=0.01)
    assert split["chars_per_token"] == pytest.approx(4.46, rel=0.01)


# --------------------------------------------------------------- cards


def test_card_has_all_sand37_blocks():
    card = build_card("run-1", "correspondence", rows=_card_rows())
    expected = {
        "conditions",
        "cost",
        "documents",
        "engine_telemetry",
        "latency",
        "quality",
        "throughput",
        "time",
        "tokens",
        "concurrency",
    }
    assert set(card) >= expected
    assert card["schema"] == "mailroom.card/v1"
    markdown = render_card_md(card)
    assert "| Metric | Value |" in markdown
    assert "Per-document results" in markdown


def test_master_md_has_tables():
    cards = [
        build_card("run-1", "correspondence", rows=_card_rows()),
        build_card(
            "run-2",
            "contract",
            rows=[
                {
                    "filename": "c.txt",
                    "doc_type": "contract",
                    "status": "ok",
                    "schema_valid": True,
                    "latency_s": 9.0,
                    "prompt_tokens": 5000,
                    "completion_tokens": 900,
                    "calls": 1,
                    "overall_score": 0.55,
                }
            ],
        ),
    ]
    data, markdown = build_master(["run-1", "run-2"], cards=cards)
    assert data["kind"] == "master"
    assert "| --- |" in markdown
    assert "Serving efficiency" in markdown
    assert "Quality and cost by specialist" in markdown
    assert "## Cost" in markdown
