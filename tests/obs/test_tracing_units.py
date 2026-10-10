"""Tracing privacy and resource contracts without a global provider or collector."""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mailroom_reloaded.obs import tracing


@pytest.mark.parametrize(
    "key",
    [
        "input.value",
        "output.value",
        "llm.prompt_template.template",
        "llm.prompt_template.variables",
        "tool.parameters",
        "llm.input_messages.0.message.content",
        "llm.output_messages.1.message.content",
        "llm.input_messages.0.content",
        "llm.output_messages.0.content",
        "llm.prompts.0",
        "custom.prompt.text",
        "custom.embedding.text",
    ],
)
def test_masking_redacts_content_before_export_and_preserves_metadata(key):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(tracing.MaskingSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        with provider.get_tracer(__name__).start_as_current_span("test") as span:
            span.set_attribute(key, "private document text")
            span.set_attribute("llm.token_count.total", 42)
            span.set_attribute("llm.input_messages.0.message.role", "user")
            span.set_attribute("gen_ai.request.model", "test-model")
        (exported,) = exporter.get_finished_spans()
        assert dict(exported.attributes) == {
            key: "<masked>",
            "llm.token_count.total": 42,
            "llm.input_messages.0.message.role": "user",
            "gen_ai.request.model": "test-model",
        }
        assert provider.force_flush()
    finally:
        provider.shutdown()


def test_masking_handles_span_without_attributes():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(tracing.MaskingSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        with provider.get_tracer(__name__).start_as_current_span("empty"):
            pass
        (exported,) = exporter.get_finished_spans()
        assert dict(exported.attributes) == {}
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    "primary,fallback,expected", [(None, None, "0"), (None, "2", "2"), ("3", "2", "3")]
)
def test_resource_identity_and_gpu_precedence(monkeypatch, primary, fallback, expected):
    monkeypatch.setenv("MAILROOM_INSTANCE_ID", "worker-1")
    monkeypatch.setenv("MAILROOM_PHOENIX_PROJECT", "test-project")
    for key, value in [("MAILROOM_GPU_REPLICA", primary), ("GPU_REPLICA", fallback)]:
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(tracing, "_container_id", lambda: "a" * 64)
    attrs = tracing.build_resource("test-service").attributes
    assert attrs["service.name"] == "test-service"
    assert attrs["service.instance.id"] == "worker-1"
    assert attrs["gpu.replica"] == expected
    assert attrs["openinference.project.name"] == "test-project"
    assert attrs["container.id"] == "a" * 64


def test_resource_falls_back_to_host_and_process_without_container(monkeypatch):
    for key in (
        "MAILROOM_INSTANCE_ID",
        "MAILROOM_PHOENIX_PROJECT",
        "OTEL_RESOURCE_ATTRIBUTES",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tracing.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(tracing.os, "getpid", lambda: 42)
    monkeypatch.setattr(tracing, "_container_id", lambda: None)
    attrs = tracing.build_resource().attributes
    assert attrs["service.instance.id"] == "host:42"
    assert attrs["openinference.project.name"] == "mailroom-live"
    assert "container.id" not in attrs
