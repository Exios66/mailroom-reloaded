import math
import os

import pytest
from pydantic import BaseModel

from mailroom_reloaded.llm.client import (
    LengthFinishReasonError,
    ResolvedModel,
    call_structured,
    make_llm,
    resolve,
)
from mailroom_reloaded.llm.retry import with_retry
from mailroom_reloaded.llm.usage import Usage

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


def test_resolve_vllm_uses_base_url(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
    monkeypatch.setenv("VLLM_BASE_URL", "http://gpu.example:8000/v1")
    r = resolve("sorter")
    assert isinstance(r, ResolvedModel)
    assert r.provider == "vllm"
    assert r.base_url == "http://gpu.example:8000/v1"
    assert r.model == "Qwen/Qwen3-8B"  # vllm_model_map remap of qwen/qwen3.7-flash
    assert r.supports_tools is True and r.supports_logprobs is True


def test_resolve_openrouter_needs_key(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MAILROOM_OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError):
        resolve("sorter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-real")
    from mailroom_reloaded import settings

    settings.get_settings.cache_clear()
    r = resolve("sorter")
    assert r.model == "qwen/qwen3.7-flash" and r.api_key == "sk-real"
    assert r.supports_logprobs is False


def test_resolve_llamafile_remaps_and_mock_needs_url(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "llamafile")
    monkeypatch.setenv("LLAMAFILE_BASE_URL", "http://lf:8080/v1")
    r = resolve("sorter")
    assert r.model == "qwen3:7b" and r.supports_tools is None
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.delenv("MOCK_BASE_URL", raising=False)
    from mailroom_reloaded import settings

    settings.get_settings.cache_clear()
    with pytest.raises(ValueError):
        resolve("sorter")


def test_make_llm_returns_crewai_llm(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
    monkeypatch.setenv("VLLM_BASE_URL", "http://gpu.example:8000/v1")
    llm = make_llm("sorter", temperature=0.5)
    assert llm.model.endswith("Qwen/Qwen3-8B")  # crewai strips the "openai/" routing prefix
    assert llm.provider == "openai"
    assert llm.base_url == "http://gpu.example:8000/v1"
    assert llm.temperature == 0.5


def test_plain_structured_call(mock_provider):
    mock_provider.reply('{"label": "contract"}')
    res = call_structured("sorter", MSGS, schema_doc_type=None, response_format=RF)
    assert res.parsed == {"label": "contract"}
    assert res.finish_reason == "stop"
    assert res.tool_rounds == 0
    assert res.usage.calls == 1 and res.usage.prompt_tokens == 10 and res.usage.completion_tokens == 5
    req = mock_provider.requests[0]
    assert req["response_format"] == RF and "tools" not in req and "tool_choice" not in req
    assert req["messages"] == MSGS


def test_fenced_json_is_parsed(mock_provider):
    mock_provider.reply('```json\n{"label": "memo"}\n```')
    res = call_structured("sorter", MSGS, schema_doc_type=None, response_format=RF)
    assert res.parsed == {"label": "memo"}


def test_unparseable_content_gives_none(mock_provider):
    mock_provider.reply("not json at all")
    res = call_structured("sorter", MSGS, schema_doc_type=None, response_format=RF)
    assert res.parsed is None and res.content == "not json at all"


def test_length_cap_raises(mock_provider):
    mock_provider.length_capped()
    with pytest.raises(LengthFinishReasonError):
        call_structured("sorter", MSGS, schema_doc_type=None, response_format=RF)


def test_cold_start_retry_not_budgeted(mock_provider, _fast_llm):
    mock_provider.fail(503, 2).reply('{"label": "x"}')
    res = call_structured("sorter", MSGS, schema_doc_type=None, response_format=RF)
    assert res.parsed == {"label": "x"}
    assert len(mock_provider.requests) == 3
    assert res.usage.calls == 1  # successful calls only
    assert len(_fast_llm) == 2 and all(s > 40 for s in _fast_llm)  # cold-start ladder


def test_vllm_disables_thinking(monkeypatch, fake_openai):
    monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
    monkeypatch.setenv("VLLM_BASE_URL", fake_openai.base_url)
    fake_openai.reply('{"label": "x"}')
    call_structured("sorter", MSGS, schema_doc_type=None, response_format=RF)
    req = fake_openai.requests[0]
    assert req["chat_template_kwargs"]["enable_thinking"] is False
    assert req["model"] == "Qwen/Qwen3-8B"


def test_non_vllm_has_no_thinking_switch(mock_provider):
    mock_provider.reply('{"label": "x"}')
    call_structured("sorter", MSGS, schema_doc_type=None, response_format=RF)
    assert "chat_template_kwargs" not in mock_provider.requests[0]


def test_label_logprob_from_label_line(mock_provider):
    toks = [
        ("LABEL", -0.9),
        (":", -0.8),
        (" correspondence", math.log(0.9)),
        ("/memo", math.log(0.5)),
        ("\n", -0.7),
        ('{"label"', -0.6),
        (': "x"}', -0.5),
    ]
    content = "".join(t for t, _ in toks)
    mock_provider.reply(content).with_logprobs(toks)
    res = call_structured("sorter", MSGS, schema_doc_type=None, response_format=RF, logprobs=True)
    assert mock_provider.requests[0]["logprobs"] is True
    assert math.exp(res.label_logprob) == pytest.approx(0.9 * 0.5)
    assert res.parsed == {"label": "x"}  # JSON after the LABEL line is still parsed


def test_label_logprob_none_without_logprobs(mock_provider):
    mock_provider.reply("LABEL: a/b\n{}")
    res = call_structured("sorter", MSGS, schema_doc_type=None, response_format=RF, logprobs=True)
    assert res.label_logprob is None


def test_schema_doc_type_uses_extraction_response_format(mock_provider, monkeypatch):
    import sys
    import types

    mod = types.ModuleType("mailroom_reloaded.schemas.extraction")
    mod.response_format = lambda doc_type: {"type": "json_schema", "json_schema": {"name": doc_type}}
    pkg = types.ModuleType("mailroom_reloaded.schemas")
    pkg.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mailroom_reloaded.schemas", pkg)
    monkeypatch.setitem(sys.modules, "mailroom_reloaded.schemas.extraction", mod)
    mock_provider.reply("{}")
    call_structured("sorter", MSGS, schema_doc_type="contract")
    assert mock_provider.requests[0]["response_format"]["json_schema"]["name"] == "contract"


def test_with_retry_gives_up_after_max_attempts(_fast_llm):
    import httpx
    import openai

    calls = []

    def boom():
        calls.append(1)
        raise openai.APIConnectionError(request=httpx.Request("POST", "http://x"))

    with pytest.raises(openai.APIConnectionError):
        with_retry(boom, max_attempts=3)
    assert len(calls) == 3 and len(_fast_llm) == 2


def test_with_retry_does_not_retry_4xx(_fast_llm):
    import httpx
    import openai

    def bad():
        resp = httpx.Response(401, request=httpx.Request("POST", "http://x"))
        raise openai.AuthenticationError("no", response=resp, body=None)

    with pytest.raises(openai.AuthenticationError):
        with_retry(bad)
    assert _fast_llm == []


def test_usage_adds():
    a = Usage(1, 2, 0.5, 1)
    b = Usage(10, 20, 1.5, 2)
    assert a + b == Usage(11, 22, 2.0, 3)
    assert sum([a, b], Usage()) == Usage(11, 22, 2.0, 3)


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("MAILROOM_LIVE") != "1", reason="set MAILROOM_LIVE=1 for live tests")
def test_live_tool_call_and_schema():
    from dataclasses import dataclass

    class Args(BaseModel):
        doc_type: str

    @dataclass
    class T:
        name: str = "list_subclasses"
        description: str = "List subclasses of a document type."
        params_model: type[BaseModel] = Args

        def fn(self, doc_type: str) -> str:
            return "memo, letter, email"

    msgs = [
        {"role": "system", "content": "Call list_subclasses for 'correspondence' then answer."},
        {"role": "user", "content": "Pick a subclass of correspondence."},
    ]
    res = call_structured("sorter", msgs, schema_doc_type=None, response_format=RF, tools=[T()])
    assert res.parsed is not None and "label" in res.parsed
