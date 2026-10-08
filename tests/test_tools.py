import json

from mailroom_reloaded.settings import load_taxonomy
from mailroom_reloaded.tools import (
    TOOLS,
    ToolContext,
    crewai_tool,
    openai_spec,
    tools_for,
)


def _ctx(**kw):
    base = {
        "doc_text": "Alpha beta. The purchase price is all cash. " * 50,
        "doc_id": "d1",
        "eval_mode": False,
        "ground_truth": None,
    }
    base.update(kw)
    return ToolContext(**base)


def _names(role, ctx):
    return {t.name for t in tools_for(role, ctx)}


def test_role_tool_matrix():
    ctx = _ctx(eval_mode=True, ground_truth=lambda d: {"x": d})
    assert _names("sorter", ctx) == {"get_taxonomy", "list_subclasses"}
    for agent in load_taxonomy().agents:
        if agent.endswith("_specialist"):
            assert _names(agent, ctx) == {"get_extraction_schema", "get_field_types"}
    assert _names("judge", ctx) == {
        "get_extraction_schema",
        "get_field_types",
        "search_source",
        "get_ground_truth",
    }
    assert _names("arbiter", ctx) == {
        "get_taxonomy",
        "get_extraction_schema",
        "search_source",
    }
    assert _names("boss", ctx) == {"get_taxonomy", "list_subclasses", "search_source"}


def test_ground_truth_absent_outside_eval():
    assert "get_ground_truth" not in _names(
        "judge", _ctx(eval_mode=False, ground_truth=lambda d: {})
    )
    assert "get_ground_truth" not in _names(
        "judge", _ctx(eval_mode=True, ground_truth=None)
    )


def test_ground_truth_returns_json():
    ctx = _ctx(eval_mode=True, ground_truth=lambda d: {"id": d})
    t = {t.name: t for t in tools_for("judge", ctx)}["get_ground_truth"]
    assert json.loads(t.fn(doc_id="d1")) == {"id": "d1"}


def test_crewai_tool_runs():
    out = crewai_tool(TOOLS["list_subclasses"], context=_ctx()).run(
        doc_type="merger_agreement"
    )
    assert "all_cash" in out


def test_unknown_args_return_error_string():
    t = {t.name: t for t in tools_for("sorter", _ctx())}["list_subclasses"]
    assert t.fn(bogus=1).startswith("error:")
    assert t.fn(doc_type="nope").startswith("error:")
    s = {t.name: t for t in tools_for("arbiter", _ctx())}["get_extraction_schema"]
    assert s.fn(doc_type="nope").startswith("error:")


def test_search_source_snippets():
    t = {t.name: t for t in tools_for("boss", _ctx())}["search_source"]
    res = json.loads(t.fn(query="PURCHASE PRICE"))
    assert 1 <= len(res["snippets"]) <= 3
    assert all(len(s) <= 400 for s in res["snippets"])
    assert json.loads(t.fn(query="zzzz"))["snippets"] == []


def test_openai_spec_and_other_tools():
    spec = openai_spec(TOOLS["get_field_types"])
    assert spec["type"] == "function" and spec["function"]["name"] == "get_field_types"
    assert "doc_type" in spec["function"]["parameters"]["properties"]
    ctx = _ctx()
    b = TOOLS["get_taxonomy"].bind(ctx)
    assert "contract" in b.fn()
    assert "fields" in json.loads(
        TOOLS["get_extraction_schema"].bind(ctx).fn(doc_type="contract")
    )
    assert json.loads(TOOLS["get_field_types"].bind(ctx).fn(doc_type="contract"))


def test_unknown_role_empty():
    assert tools_for("nobody", _ctx()) == []
