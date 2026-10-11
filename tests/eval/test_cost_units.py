"""Unit tests for eval cell-cost pricing and the prompt-token split.

These pin the error/edge branches the card renders from: a missing price must
yield ``None`` (never a fabricated ``$0``), and an unfittable token split must
fall back to the measured characters-per-token rather than inventing numbers.
"""

from __future__ import annotations

import pytest

from mailroom_reloaded.eval.cost import (
    FALLBACK_CHARS_PER_TOKEN,
    cell_cost,
    model_prices,
    token_split,
)

# ------------------------------------------------------------------ model_prices


def test_model_prices_none_for_missing_or_unknown_model():
    assert model_prices(None) is None
    assert model_prices("") is None
    assert model_prices("no-such-model") is None


def test_model_prices_exact_key_and_longest_prefix():
    assert model_prices("qwen/qwen3.7-flash") == (0.03, 0.13)
    # a suffixed deployment id resolves by the longest matching prefix
    assert model_prices("qwen/qwen3.7-flash-2026-01") == (0.03, 0.13)


def test_model_prices_returns_none_on_unreadable_config(monkeypatch):
    from mailroom_reloaded.eval import cost

    monkeypatch.setattr(
        cost, "load_taxonomy", lambda: (_ for _ in ()).throw(RuntimeError("config gone"))
    )
    assert model_prices("qwen/qwen3.7-flash") is None


# ------------------------------------------------------------------ cell_cost


def test_cell_cost_gpu_hour_and_missing_wall_time():
    cost = cell_cost(3600.0, gpus=2, usd_per_hour=0.80, ok=5, total=10, tokens=1000)
    assert cost["busy_gpu_usd"] == pytest.approx(1.6)  # 1h * 0.80 * 2
    assert cost["usd_per_document"] == pytest.approx(0.16)
    assert cost["usd_per_ok_document"] == pytest.approx(0.32)
    assert cost["usd_per_million_tokens"] == pytest.approx(1600.0)

    # a missing wall time yields None, never a fabricated $0
    missing = cell_cost(None, total=10)
    assert missing["busy_gpu_usd"] is None
    assert missing["usd_per_document"] is None
    assert missing["usd_per_ok_document"] is None
    assert missing["usd_per_million_tokens"] is None


def test_cell_cost_per_token_with_explicit_prices():
    cost = cell_cost(
        None,
        pricing="per_token",
        prompt_tokens=1_000_000,
        completion_tokens=500_000,
        prices=(0.05, 0.25),
    )
    assert cost["busy_gpu_usd"] == pytest.approx(0.05 + 0.125)


def test_cell_cost_per_token_without_a_price_is_none():
    # no explicit prices and an unresolvable model -> None, not $0
    cost = cell_cost(None, pricing="per_token", model="no-such-model", prompt_tokens=1000)
    assert cost["busy_gpu_usd"] is None
    assert cost["usd_per_million_tokens"] is None


# ------------------------------------------------------------------ token_split


def test_token_split_empty_rows_falls_back():
    out = token_split([])
    assert out["method"] == "fallback"
    assert out["documents"] == 0
    assert out["instruction_per_call"] is None
    assert out["chars_per_token"] is None


def test_token_split_skips_unusable_rows():
    rows = [
        {"ok": False, "prompt_tokens": 100, "calls": 1, "input_chars": 400},
        {"ok": True, "calls": 1, "input_chars": 400},  # no prompt_tokens
        {"ok": True, "prompt_tokens": 100, "calls": 0, "input_chars": 400},  # no calls
        {"ok": True, "prompt_tokens": 100, "calls": 1, "input_chars": 0},  # no chars
    ]
    out = token_split(rows)
    assert out["method"] == "fallback"
    assert out["documents"] == 0


def test_token_split_too_few_points_uses_fallback_ratio():
    rows = [
        {"ok": True, "prompt_tokens": 1000 + i * 100, "calls": 1, "input_chars": 4000 + i * 400}
        for i in range(3)
    ]
    out = token_split(rows)
    assert out["method"] == "fallback"
    assert out["documents"] == 3
    assert out["chars_per_token"] == FALLBACK_CHARS_PER_TOKEN


def test_token_split_recovers_a_clean_fit():
    instruction, ratio = 60.0, 4.5
    rows = []
    for i in range(10):
        calls = 1 + (i % 3)
        chars = 1200 + i * 700
        rows.append(
            {
                "ok": True,
                "prompt_tokens": instruction * calls + chars / ratio,
                "calls": calls,
                "input_chars": chars,
                "completion_tokens": 20,
            }
        )
    out = token_split(rows)
    assert out["method"] == "fit"
    assert out["instruction_per_call"] == pytest.approx(instruction, rel=1e-3)
    assert out["chars_per_token"] == pytest.approx(ratio, rel=1e-3)
    assert out["documents"] == 10
