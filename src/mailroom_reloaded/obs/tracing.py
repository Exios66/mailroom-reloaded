"""OpenTelemetry tracing setup (spec section 2, section 9).

``setup_tracing`` is called from ``mailroom_reloaded.__init__`` before anything
imports ``crewai``, so the OpenInference ``CrewAIInstrumentor`` and
``OpenAIInstrumentor`` wrap those libraries before first use. It is idempotent:
a second call returns the configured provider (and attaches any exporter the
caller passes, which the tests use to install an in-memory exporter). It is safe
when no collector is reachable: the default OTLP exporter runs behind a
``BatchSpanProcessor`` and dropped exports never raise into the pipeline.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import sys
from pathlib import Path

from opentelemetry import trace
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)

from mailroom_reloaded.settings import get_settings

__all__ = ["MaskingSpanProcessor", "RunScopeSpanProcessor", "build_resource", "setup_tracing"]

logger = logging.getLogger(__name__)

MASKED = "<masked>"

_PROVIDER: TracerProvider | None = None
_MASK_INSTALLED = False

_CGROUP_ID = re.compile(r"[0-9a-f]{64}")

#: OpenInference attribute keys that carry document/prompt text.
_EXACT_CONTENT_KEYS = {
    "input.value",
    "output.value",
    "llm.prompt_template.template",
    "llm.prompt_template.variables",
    "tool.parameters",
}
_CONTENT_SUFFIXES = (".message.content", ".prompt.text", ".embedding.text")


class RunScopeSpanProcessor(SpanProcessor):
    """Stamp every span with the run it belongs to when it starts.

    ``on_start`` runs in the thread that starts the span, where the run scope's
    ``ContextVar`` is visible (the batch exporter thread cannot see it). It covers
    spans from the instrumentors (LLM, CrewAI) as well as the pipeline's own.
    """

    def on_start(self, span, parent_context=None) -> None:
        """Set ``mailroom.run_id`` / ``mailroom.environment`` / ``session.id`` if a scope is active."""
        from mailroom_reloaded.obs.run_context import current_run

        scope = current_run()
        if scope is None:
            return
        span.set_attribute("mailroom.run_id", scope.run_id)
        span.set_attribute("mailroom.environment", scope.environment)
        if scope.session_id:
            span.set_attribute("session.id", scope.session_id)

    def on_end(self, span: ReadableSpan) -> None:
        """Nothing to do at the end of a span."""

    def shutdown(self) -> None:
        """Nothing to release."""

    def force_flush(self, timeout_millis: int | None = None) -> bool:
        """Nothing buffered."""
        return True


def _span_store_exporter() -> SpanExporter | None:
    """The local span store exporter, or ``None`` when it should not be attached.

    Under pytest the store is skipped unless ``MAILROOM_TRACE_STORE_PATH`` is set, for
    the same reason the default OTLP exporter is (tests install their own exporters).
    """
    if "pytest" in sys.modules and get_settings().trace_store_path is None:
        return None
    try:
        from mailroom_reloaded.storage.span_store import SqliteSpanExporter

        return SqliteSpanExporter()
    except Exception:  # pragma: no cover - tracing must never break runs
        logger.warning("span_store_unavailable", exc_info=True)
        return None


def _is_content_key(key: str) -> bool:
    """True when ``key`` names prompt/completion content to redact."""
    if key in _EXACT_CONTENT_KEYS:
        return True
    if key.endswith(_CONTENT_SUFFIXES):
        return True
    if key.startswith("llm.prompts"):
        return True
    return key.startswith(("llm.input_messages.", "llm.output_messages.")) and key.endswith(".content")


class MaskingSpanProcessor(SpanProcessor):
    """Replace prompt/completion content with ``"<masked>"`` (``trace_mask``).

    It rewrites the span's attribute mapping on ``on_end`` before the exporter
    processors see it. Installed before the exporter so exports are masked.
    """

    def on_start(self, span, parent_context=None) -> None:
        """No-op: masking happens once the span is complete (see ``on_end``)."""

    def on_end(self, span: ReadableSpan) -> None:
        """Mask content attributes before downstream processors export the span."""
        self._mask(span)

    def shutdown(self) -> None:
        """Nothing to release."""

    def force_flush(self, timeout_millis: int | None = None) -> bool:
        """Nothing buffered."""
        return True

    @staticmethod
    def _mask(span) -> None:
        """Replace content attributes with the mask while preserving other values."""
        attrs = getattr(span, "_attributes", None)
        if not attrs:
            return
        masked = {
            key: (MASKED if _is_content_key(key) else value) for key, value in dict(attrs).items()
        }
        span._attributes = BoundedAttributes(
            maxlen=getattr(attrs, "maxlen", None), attributes=masked
        )


def _container_id() -> str | None:
    """The 64-hex container id from ``/proc/self/cgroup`` when available."""
    try:
        text = Path("/proc/self/cgroup").read_text()
    except OSError:
        return None
    for match in _CGROUP_ID.findall(text):
        return match
    return None


def _instance_id() -> str:
    """A stable per-process instance id (env override, else host:pid)."""
    return os.environ.get("MAILROOM_INSTANCE_ID") or f"{socket.gethostname()}:{os.getpid()}"


def _gpu_replica() -> str:
    """The GPU replica index (env override, else ``0``)."""
    return (
        os.environ.get("MAILROOM_GPU_REPLICA")
        or os.environ.get("GPU_REPLICA")
        or "0"
    )


def build_resource(service_name: str = "mailroom") -> Resource:
    """Build the OTel resource with the spec section 8/9 attributes."""
    attrs = {
        "service.name": service_name,
        "service.instance.id": _instance_id(),
        "gpu.replica": _gpu_replica(),
        "openinference.project.name": os.environ.get(
            "MAILROOM_PHOENIX_PROJECT", "mailroom-live"
        ),
    }
    container = _container_id()
    if container:
        attrs["container.id"] = container
    return Resource.create(attrs)


def _default_otlp_exporter() -> SpanExporter | None:
    """An OTLP span exporter using the configured protocol and endpoint, or ``None``.

    Under pytest with no endpoint configured there is no collector to reach, so
    the default localhost exporter is skipped to keep test output clean; tests
    install an in-memory exporter through ``setup_tracing``. Production (or an
    explicit generic or trace-specific endpoint) always gets the exporter.
    """
    if (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") is None
        and os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") is None
        and "pytest" in sys.modules
    ):
        return None
    protocol = os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    )
    try:
        if protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        elif protocol == "http/protobuf":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            raise ValueError(f"Unsupported OTLP traces protocol: {protocol}")

        return OTLPSpanExporter()
    except Exception:  # pragma: no cover - exporter package always installed
        logger.warning("otlp_span_exporter_unavailable", exc_info=True)
        return None


def _install_instrumentors(provider: TracerProvider) -> None:
    """Install the OpenInference CrewAI and OpenAI instrumentors (idempotent)."""
    try:
        from openinference.instrumentation.crewai import CrewAIInstrumentor

        CrewAIInstrumentor().instrument(tracer_provider=provider)
    except Exception:  # pragma: no cover - defensive: tracing must never break runs
        logger.warning("crewai_instrumentor_failed", exc_info=True)
    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument(tracer_provider=provider)
    except Exception:  # pragma: no cover - defensive
        logger.warning("openai_instrumentor_failed", exc_info=True)


def setup_tracing(
    service_name: str = "mailroom",
    exporter: SpanExporter | None = None,
    *,
    trace_mask: bool | None = None,
) -> TracerProvider:
    """Configure the global tracer provider, instrumentors and masking.

    Idempotent: the provider is built once. A later call with ``exporter``
    attaches that exporter (used by tests); a later call with ``trace_mask=True``
    installs the masking processor before the newly attached exporter.
    """
    global _PROVIDER, _MASK_INSTALLED

    mask = get_settings().trace_mask if trace_mask is None else bool(trace_mask)

    if _PROVIDER is not None:
        if mask and not _MASK_INSTALLED:
            # Processors run in registration order: exporters already attached
            # can see unmasked attributes. Enable masking on the first setup
            # call to protect all exporters; this only protects later ones.
            _PROVIDER.add_span_processor(MaskingSpanProcessor())
            _MASK_INSTALLED = True
        if exporter is not None:
            _PROVIDER.add_span_processor(SimpleSpanProcessor(exporter))
        return _PROVIDER

    provider = TracerProvider(resource=build_resource(service_name))
    provider.add_span_processor(RunScopeSpanProcessor())
    if mask:
        provider.add_span_processor(MaskingSpanProcessor())
        _MASK_INSTALLED = True
    store = _span_store_exporter()
    if store is not None:
        # batched (the synchronous path would block the pipeline); the store applies
        # its own allow-list, so it is safe whatever the masking setting is
        provider.add_span_processor(BatchSpanProcessor(store))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        default = _default_otlp_exporter()
        if default is not None:
            provider.add_span_processor(BatchSpanProcessor(default))
    trace.set_tracer_provider(provider)
    _PROVIDER = provider
    _install_instrumentors(provider)
    return provider


def _reset_for_tests() -> None:
    """Drop the module's provider handle so a test can install a fresh one."""
    global _PROVIDER, _MASK_INSTALLED
    _PROVIDER = None
    _MASK_INSTALLED = False
