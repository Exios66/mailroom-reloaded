"""Task 24: the behavioural conformance suite and its invariants.

The four required tests pin the spec section 11 invariants and the card rates.
They build :class:`RoleRun` records directly (and one drives the real sorter
through a scripted ``FakeOpenAI``) so they are deterministic and offline.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fakes.openai_server import FakeOpenAI

from mailroom_reloaded.eval import conformance as cf
from mailroom_reloaded.eval.conformance import (
    INVARIANTS,
    RoleRun,
    ToolCall,
    render_card_md,
    run_conformance,
)
from mailroom_reloaded.schemas.extraction import get_extraction_schema

CORRESPONDENCE_01 = "correspondence_01.txt"


@pytest.fixture
def fake_openai():
    """Yield a local fake OpenAI server and stop it after the test."""
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):
    """Point the mock provider at the local fake OpenAI server."""
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    """Disable retry delays and clear tool-support caches around each test."""
    from mailroom_reloaded.llm import retry, tooling

    monkeypatch.setattr(retry, "_sleep", lambda *_: None)
    tooling.reset_tool_support_cache()
    yield
    tooling.reset_tool_support_cache()


def _invariant(role: str, name: str):
    """Look up one invariant's predicate by role and name."""
    return next(i for i in INVARIANTS if i.role == role and i.name == name).check


# --------------------------------------------------------------------------- invariants


def test_sorter_subclass_only_doc_type_change_without_flag_fails_invariant():
    check = _invariant("sorter", "subclass_only_doc_type_guard")
    silent = RoleRun(
        role="sorter",
        filename=CORRESPONDENCE_01,
        doc_type="correspondence",
        mode="subclass_only",
        parsed={"doc_type": "contract", "doc_type_disagree": False},
    )
    assert check(silent) is False

    flagged = replace(
        silent, parsed={"doc_type": "contract", "doc_type_disagree": True}
    )
    assert check(flagged) is True

    same = replace(
        silent, parsed={"doc_type": "correspondence", "doc_type_disagree": False}
    )
    assert check(same) is True


def test_specialist_extra_key_fails_invariant():
    check = _invariant("specialists", "no_keys_outside_schema")
    base = get_extraction_schema("correspondence")().model_dump()
    clean = RoleRun(
        role="specialists",
        filename=CORRESPONDENCE_01,
        doc_type="correspondence",
        mode="live",
        parsed=base,
        schema_valid=True,
    )
    assert check(clean) is True

    dirty = replace(clean, parsed={**base, "legacy_key_points": ["x"]})
    assert check(dirty) is False


def test_judge_live_mode_gt_tool_call_fails_invariant():
    check = _invariant("judge", "live_never_ground_truth")
    live = RoleRun(
        role="judge",
        filename=CORRESPONDENCE_01,
        doc_type="correspondence",
        mode="live",
        parsed={"label": "complete", "score": 0.9},
        tool_calls=[ToolCall("judge", "get_ground_truth", True)],
    )
    assert check(live) is False

    clean_live = replace(live, tool_calls=[ToolCall("judge", "search_source", True)])
    assert check(clean_live) is True

    grade = replace(live, mode="grade")
    assert check(grade) is True


def test_card_rates(monkeypatch, tmp_path):
    calls = [
        ToolCall("sorter", "get_taxonomy", True),
        ToolCall("sorter", "list_subclasses", True),
        ToolCall("sorter", "get_taxonomy", True),
        ToolCall("sorter", "list_subclasses", False),
    ]
    sorter_run = RoleRun(
        role="sorter",
        filename="contract_01.txt",
        doc_type="contract",
        mode="full",
        parsed={
            "doc_type": "contract",
            "doc_subclass": None,
            "doc_type_disagree": False,
        },
        tool_calls=calls,
    )
    runs = {
        "sorter": [sorter_run],
        "specialists": [],
        "judge": [],
        "arbiter": [],
        "boss": [],
    }
    monkeypatch.setattr(cf, "_collect_runs", lambda **_: runs)

    card = run_conformance("mock", per_class=1, out_dir=tmp_path)

    assert card.roles["sorter"].tool_call_success_rate == 0.75
    assert card.roles["sorter"].invariant_pass_rate == 1.0
    assert (tmp_path / "conformance-mock.json").is_file()
    assert (tmp_path / "conformance-mock.md").is_file()
    payload = json.loads((tmp_path / "conformance-mock.json").read_text("utf-8"))
    assert payload["roles"]["sorter"]["tool_call_success_rate"] == 0.75
    assert "sorter" in render_card_md(card)


# --------------------------------------------------------------------------- real harness


def test_sorter_runs_record_real_tool_calls(mock_provider):
    """The recorder sees real tool traffic through the scripted sorter."""
    full_json = json.dumps(
        {
            "doc_type": "correspondence",
            "doc_subclass": "letter",
            "confidence": 0.9,
            "doc_type_disagree": False,
            "doc_type_disagree_reason": None,
        }
    )
    scope_json = json.dumps(
        {
            "doc_subclass": "letter",
            "confidence": 0.9,
            "doc_type_disagree": False,
            "doc_type_disagree_reason": None,
        }
    )
    mock_provider.tool_call("list_subclasses", {"doc_type": "correspondence"})
    mock_provider.reply("done")
    mock_provider.reply(full_json)
    mock_provider.tool_call("list_subclasses", {"doc_type": "correspondence"})
    mock_provider.reply("done")
    mock_provider.reply(scope_json)

    from mailroom_reloaded.eval.dataset import BlindDoc, sha256_text

    text = "Dear Sir or Madam,\n\nPlease review the attached draft.\n"
    doc = BlindDoc("correspondence_01.txt", text, sha256_text(text))
    runs = cf._sorter_runs(doc, None)

    assert {run.mode for run in runs} == {"full", "subclass_only"}
    assert all(run.tool_calls for run in runs)
    assert all(tc.ok for run in runs for tc in run.tool_calls)
    assert cf.build_role_stats(runs).invariant_pass_rate == 1.0
