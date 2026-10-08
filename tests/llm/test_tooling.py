import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from mailroom_reloaded.llm.client import call_structured
from mailroom_reloaded.llm.tooling import ToolLike

RF = {
    "type": "json_schema",
    "json_schema": {
        "name": "answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        },
    },
}
MSGS = [{"role": "system", "content": "sort it"}, {"role": "user", "content": "doc text"}]


class SubArgs(BaseModel):
    doc_type: Literal["correspondence", "contract"]


@dataclass
class FakeTool:
    name: str = "list_subclasses"
    description: str = "List subclasses."
    params_model: type[BaseModel] = SubArgs

    def fn(self, doc_type: str) -> str:
        return f"subclasses-of-{doc_type}: memo, letter"


def _run(tools=None, **kw):
    return call_structured(
        "sorter", MSGS, schema_doc_type=None, response_format=RF, tools=tools or [FakeTool()], **kw
    )


def test_tool_like_protocol():
    assert isinstance(FakeTool(), ToolLike)


def test_tool_loop_executes_and_finishes(mock_provider):
    mock_provider.tool_call("list_subclasses", {"doc_type": "correspondence"})
    mock_provider.reply("no more tools needed")  # tool phase ends on a no-tool-call turn
    mock_provider.reply('{"label": "memo"}')  # strict final turn
    res = _run()
    assert res.tool_rounds == 1
    assert res.parsed == {"label": "memo"}
    second = mock_provider.requests[1]["messages"]
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert len(tool_msgs) == 1 and "subclasses-of-correspondence" in tool_msgs[0]["content"]
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert res.usage.calls == 3


def test_tool_spec_built_from_params_model(mock_provider):
    mock_provider.reply("x").reply('{"label": "a"}')
    _run()
    spec = mock_provider.requests[0]["tools"][0]
    assert spec["type"] == "function" and spec["function"]["name"] == "list_subclasses"
    assert spec["function"]["parameters"] == SubArgs.model_json_schema()
    assert mock_provider.requests[0]["tool_choice"] == "auto"


def test_tool_loop_cap_three_rounds(mock_provider):
    for _ in range(3):
        mock_provider.tool_call("list_subclasses", {"doc_type": "contract"})
    mock_provider.reply('{"label": "memo"}')
    res = _run()
    assert res.tool_rounds == 3
    assert len(mock_provider.requests) == 4
    assert [r.get("tool_choice") for r in mock_provider.requests[:3]] == ["auto"] * 3
    assert mock_provider.requests[3]["tool_choice"] == "none"
    assert res.parsed == {"label": "memo"}


def test_two_phase_tools_then_schema(mock_provider):
    mock_provider.tool_call("list_subclasses", {"doc_type": "contract"})
    mock_provider.reply("done").reply('{"label": "memo"}')
    _run()
    for req in mock_provider.requests:
        assert not ("tools" in req and "response_format" in req)
    tool_phase = mock_provider.requests[:-1]
    assert all("response_format" not in r for r in tool_phase)
    final = mock_provider.requests[-1]
    assert final["tool_choice"] == "none" and final["response_format"] == RF


def test_unknown_tool_and_bad_args_return_error_strings(mock_provider):
    mock_provider.tool_call("nope", {})
    mock_provider.tool_call("list_subclasses", {"doc_type": "banana"})
    mock_provider.reply("done").reply('{"label": "m"}')
    res = _run()
    assert res.tool_rounds == 2
    tool_contents = [m["content"] for m in mock_provider.requests[-1]["messages"] if m["role"] == "tool"]
    assert len(tool_contents) == 2
    assert "unknown tool" in tool_contents[0].lower()
    assert "invalid arguments" in tool_contents[1].lower()


def test_inline_fallback_when_tools_rejected(mock_provider):
    mock_provider.reject_tools()
    mock_provider.reply('{"label": "memo"}')
    res = _run()
    assert res.parsed == {"label": "memo"}
    assert res.tool_rounds == 0
    assert len(mock_provider.requests) == 2
    assert "tools" in mock_provider.requests[0]
    retry = mock_provider.requests[1]
    assert "tools" not in retry and "tool_choice" not in retry
    assert retry["response_format"] == RF
    system = retry["messages"][0]
    assert system["role"] == "system" and system["content"].startswith("sort it")
    # Literal-arg tool is pre-run for every allowed value and inlined.
    assert "subclasses-of-correspondence" in system["content"]
    assert "subclasses-of-contract" in system["content"]
    assert not any(m["role"] == "tool" for m in retry["messages"])


def test_tool_rejection_is_remembered(mock_provider):
    mock_provider.reject_tools()
    mock_provider.reply('{"label": "a"}').reply('{"label": "b"}')
    _run()
    _run()
    # second call went straight to the inline path: only one extra request total
    assert len(mock_provider.requests) == 3
    assert "tools" not in mock_provider.requests[2]


def test_inline_free_text_tools_listed_as_unavailable(mock_provider):
    class Q(BaseModel):
        query: str

    @dataclass
    class Search:
        name: str = "search_source"
        description: str = "Search the source."
        params_model: type[BaseModel] = Q

        def fn(self, query: str) -> str:
            raise AssertionError("must not be pre-run")

    mock_provider.reject_tools()
    mock_provider.reply('{"label": "m"}')
    _run(tools=[Search()])
    system = mock_provider.requests[1]["messages"][0]["content"]
    assert "search_source" in system and "not available" in system.lower()


def test_tool_arguments_are_json_encoded_in_history(mock_provider):
    mock_provider.tool_call("list_subclasses", {"doc_type": "contract"})
    mock_provider.reply("d").reply('{"label": "m"}')
    _run()
    assistant = next(m for m in mock_provider.requests[1]["messages"] if m["role"] == "assistant")
    call = assistant["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"doc_type": "contract"}
