"""OpenTelemetry metrics (spec section 8, "Live OTel metrics").

``M`` is a lazy namespace over the twelve instruments in the spec. Instruments
are created on first access against the current meter provider, so a test that
calls :func:`setup_metrics` with an ``InMemoryMetricReader`` collects them, while
a run with no provider configured is a harmless no-op. ``setup_metrics`` clears
the cache so re-configuring rebinds every instrument.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import metrics

from mailroom_reloaded.obs.tracing import build_resource

__all__ = ["M", "setup_metrics"]

logger = logging.getLogger(__name__)

METER_NAME = "mailroom"

#: attribute name -> (OTel instrument name, kind, unit)
_SPECS: dict[str, tuple[str, str, str]] = {
    "documents": ("mailroom.documents", "counter", "{document}"),
    "node_duration": ("mailroom.node.duration", "histogram", "s"),
    "llm_calls": ("mailroom.llm.calls", "counter", "{call}"),
    "gate_decisions": ("mailroom.gate.decisions", "counter", "{decision}"),
    "bert_route": ("mailroom.bert.route", "counter", "{route}"),
    "schema_valid": ("mailroom.schema.valid", "counter", "{output}"),
    "length_capped": ("mailroom.length_capped", "counter", "{output}"),
    "cost_usd": ("mailroom.cost.usd", "counter", "USD"),
    "queue_depth": ("mailroom.queue.depth", "gauge", "{document}"),
    "inflight": ("mailroom.inflight", "gauge", "{document}"),
    "token_usage": ("gen_ai.client.token.usage", "histogram", "{token}"),
    "operation_duration": ("gen_ai.client.operation.duration", "histogram", "s"),
}

_PROVIDER: Any = None


class _Metrics:
    """Lazy attribute namespace of the spec section 8 instruments."""

    def __init__(self) -> None:
        object.__setattr__(self, "_instruments", {})

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        spec = _SPECS.get(name)
        if spec is None:
            raise AttributeError(name)
        instruments = object.__getattribute__(self, "_instruments")
        if name not in instruments:
            instruments[name] = self._create(spec)
        return instruments[name]

    @staticmethod
    def _create(spec: tuple[str, str, str]) -> Any:
        otel_name, kind, unit = spec
        meter = metrics.get_meter(METER_NAME)
        if kind == "counter":
            return meter.create_counter(otel_name, unit=unit)
        if kind == "histogram":
            return meter.create_histogram(otel_name, unit=unit)
        return meter.create_gauge(otel_name, unit=unit)

    def names(self) -> set[str]:
        """The OTel names of every instrument in the namespace."""
        return {spec[0] for spec in _SPECS.values()}

    def reset(self) -> None:
        """Drop cached instruments so the next access rebinds to the provider."""
        object.__getattribute__(self, "_instruments").clear()

    def __dir__(self) -> list[str]:
        return sorted(_SPECS)


M = _Metrics()


def _default_otlp_reader() -> Any:
    """A periodic OTLP HTTP metric reader, or ``None`` when unavailable."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
    if not endpoint.endswith("/v1/metrics"):
        endpoint = endpoint + "/v1/metrics"
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        return PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
    except Exception:  # pragma: no cover - exporter package always installed
        logger.warning("otlp_metric_reader_unavailable", exc_info=True)
        return None


def setup_metrics(reader: Any | None = None) -> Any:
    """Configure the global meter provider with ``reader`` (or OTLP by default).

    Idempotent: the first call wins. Returns the ``MeterProvider``.
    """
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    from opentelemetry.sdk.metrics import MeterProvider

    readers = []
    if reader is not None:
        readers.append(reader)
    else:
        default = _default_otlp_reader()
        if default is not None:
            readers.append(default)
    provider = MeterProvider(metric_readers=readers, resource=build_resource())
    metrics.set_meter_provider(provider)
    _PROVIDER = provider
    M.reset()
    return provider


def _reset_for_tests() -> None:
    """Clear the provider handle and the OTel set-once guard (tests only)."""
    global _PROVIDER
    M.reset()
    _PROVIDER = None
    try:
        from opentelemetry import metrics as _metrics

        _metrics._internal._METER_PROVIDER = None  # noqa: SLF001
        _metrics._internal._METER_PROVIDER_SET_ONCE._done = False  # noqa: SLF001
    except Exception:  # pragma: no cover - defensive
        logger.debug("metrics_reset_failed", exc_info=True)
