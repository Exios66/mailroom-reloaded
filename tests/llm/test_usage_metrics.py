"""Usage and cost telemetry collected in memory; no provider HTTP requests."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from mailroom_reloaded.llm import client
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.obs import metrics


@pytest.fixture
def metric_reader(monkeypatch):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics.metrics, "get_meter", provider.get_meter)
    monkeypatch.setattr(client, "M", metrics._Metrics())
    try:
        yield reader
    finally:
        provider.shutdown()


def points(reader):
    data = reader.get_metrics_data()
    return {
        metric.name: list(metric.data.data_points)
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


@pytest.mark.parametrize(
    "prices,expected",
    [
        ({}, 0),
        ({"other-model": {"input_per_million": 99}}, 0),
        ({"test-model": {"input_per_million": "2", "output_per_million": "8"}}, 3),
        ({"test-model": {"input_per_million": None, "output_per_million": 8}}, 2),
        ({"test-model": {"input_per_million": 2}}, 1),
    ],
)
def test_usage_metrics_price_tokens_and_keep_labels(
    metric_reader, monkeypatch, prices, expected
):
    monkeypatch.setattr(
        client, "load_taxonomy", lambda: SimpleNamespace(raw={"cost_models": prices})
    )
    resolved = client.ResolvedModel(
        "mock", "test-model", "http://unused", "unused", False, False
    )
    usage = Usage(
        prompt_tokens=500_000, completion_tokens=250_000, latency_s=1.25, calls=3
    )

    client._record_usage_metrics("sorter", resolved, usage)

    emitted = points(metric_reader)
    (calls,) = emitted["mailroom.llm.calls"]
    (cost,) = emitted["mailroom.cost.usd"]
    assert calls.value == 3
    assert cost.value == pytest.approx(expected)
    assert (
        calls.attributes
        == cost.attributes
        == {"role": "sorter", "provider": "mock", "model": "test-model"}
    )
    tokens = {
        p.attributes["gen_ai.token.type"]: p
        for p in emitted["gen_ai.client.token.usage"]
    }
    assert tokens["input"].sum == 500_000
    assert tokens["output"].sum == 250_000
    for kind, point in tokens.items():
        assert point.count == 1
        assert point.attributes == {
            "gen_ai.token.type": kind,
            "gen_ai.request.model": "test-model",
            "gen_ai.provider.name": "mock",
        }
    (duration,) = emitted["gen_ai.client.operation.duration"]
    assert duration.sum == pytest.approx(1.25)
    assert duration.count == 1
    assert duration.attributes == {
        "gen_ai.request.model": "test-model",
        "gen_ai.provider.name": "mock",
        "gen_ai.operation.name": "chat",
    }


def test_zero_usage_still_counts_one_call(metric_reader, monkeypatch):
    monkeypatch.setattr(client, "load_taxonomy", lambda: SimpleNamespace(raw={}))
    resolved = client.ResolvedModel(
        "mock", "test-model", "http://unused", "unused", False, False
    )
    client._record_usage_metrics("sorter", resolved, Usage())
    emitted = points(metric_reader)
    assert emitted["mailroom.llm.calls"][0].value == 1
    assert emitted["mailroom.cost.usd"][0].value == 0
    assert all(p.sum == 0 for p in emitted["gen_ai.client.token.usage"])


@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize(
    "finish,content",
    [("stop", '{"ok": true}'), (None, "invalid json"), ("length", "truncated")],
)
def test_structured_call_records_all_usage_even_on_length_cap(
    metric_reader,
    monkeypatch,
    with_tools,
    finish,
    content,
):
    resolved = client.ResolvedModel(
        "mock", "test-model", "http://unused", "unused", False, False
    )
    monkeypatch.setattr(client, "resolve", lambda role: resolved)
    taxonomy = SimpleNamespace(
        raw={
            "cost_models": {
                "test-model": {"input_per_million": 2, "output_per_million": 8}
            }
        },
        agent=lambda role: SimpleNamespace(temperature=0, max_tokens=100),
    )
    monkeypatch.setattr(client, "load_taxonomy", lambda: taxonomy)
    monkeypatch.setattr(client.openai, "OpenAI", Mock())
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish,
                message=SimpleNamespace(content=content),
            )
        ]
    )
    final_usage = Usage(prompt_tokens=10, completion_tokens=5, calls=1, latency_s=0.25)
    monkeypatch.setattr(
        client, "chat_create", Mock(return_value=(response, final_usage))
    )
    tool_usage = Usage(prompt_tokens=20, completion_tokens=7, calls=2, latency_s=0.5)
    loop = Mock(
        return_value=SimpleNamespace(
            usage=tool_usage,
            rounds=2,
            messages=[],
            inline=False,
            reject_key=None,
        )
    )
    monkeypatch.setattr(client, "run_tool_loop", loop)
    tools = [Mock()] if with_tools else []

    if finish == "length":
        with pytest.raises(client.LengthFinishReasonError, match="sorter"):
            client.call_structured("sorter", [], tools=tools)
    else:
        result = client.call_structured("sorter", [], tools=tools)
        assert result.parsed == ({"ok": True} if finish == "stop" else None)
        assert result.finish_reason == "stop"
        assert result.usage == (final_usage + tool_usage if with_tools else final_usage)
        assert result.tool_rounds == (2 if with_tools else 0)

    assert loop.call_count == int(with_tools)
    client.chat_create.assert_called_once()
    emitted = points(metric_reader)
    assert emitted["mailroom.llm.calls"][0].value == (3 if with_tools else 1)
    assert emitted["mailroom.length_capped"][0].value == int(finish == "length")
    assert emitted["mailroom.length_capped"][0].attributes == {
        "role": "sorter",
        "provider": "mock",
        "model": "test-model",
    }
    tokens = {
        p.attributes["gen_ai.token.type"]: p.sum
        for p in emitted["gen_ai.client.token.usage"]
    }
    assert tokens == {
        "input": 30 if with_tools else 10,
        "output": 12 if with_tools else 5,
    }
    assert emitted["gen_ai.client.operation.duration"][0].sum == pytest.approx(
        0.75 if with_tools else 0.25
    )
    assert emitted["mailroom.cost.usd"][0].value == pytest.approx(
        0.000156 if with_tools else 0.00006
    )
