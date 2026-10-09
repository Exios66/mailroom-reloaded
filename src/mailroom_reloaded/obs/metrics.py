"""OpenTelemetry metrics (spec section 8, "Live OTel metrics").

``M`` is a lazy namespace over the spec's instruments (plus the decision counters). Instruments
are created on first access against the current meter provider, so a test that
calls :func:`setup_metrics` with an ``InMemoryMetricReader`` collects them, while
a run with no provider configured is a harmless no-op. ``setup_metrics`` clears
the cache so re-configuring rebinds every instrument.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from typing import Any

from opentelemetry import metrics

from mailroom_reloaded.obs.run_context import UNSCOPED, current_run
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
    # decision counters (the replay's ticker as time series)
    "retries": ("mailroom.retries", "counter", "{retry}"),
    "escalations": ("mailroom.escalations", "counter", "{escalation}"),
    "review_causes": ("mailroom.review.causes", "counter", "{cause}"),
}

_PROVIDER: Any = None


def run_labels() -> dict[str, str]:
    """``run_id`` / ``environment`` of the current run scope (``unscoped`` outside any scope).

    Bounded by design: eval run ids plus one daily live bucket. ``doc_id`` is never a label.
    """
    scope = current_run()
    if scope is None:
        return {"run_id": UNSCOPED, "environment": UNSCOPED}
    return {"run_id": scope.run_id, "environment": scope.environment}


class _Instrument:
    """An OTel instrument whose data points always carry the current run's labels."""

    def __init__(self, inner: Any) -> None:
        """Wrap ``inner`` (a counter, histogram or gauge)."""
        self._inner = inner

    def _merge(self, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
        merged = run_labels()
        if attributes:
            merged.update(attributes)  # an explicit label wins
        return merged

    def add(
        self,
        amount: float,
        attributes: Mapping[str, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Counter ``add`` with the run labels merged in."""
        self._inner.add(amount, self._merge(attributes), *args, **kwargs)

    def record(
        self,
        amount: float,
        attributes: Mapping[str, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Histogram ``record`` with the run labels merged in."""
        self._inner.record(amount, self._merge(attributes), *args, **kwargs)

    def set(
        self,
        amount: float,
        attributes: Mapping[str, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Gauge ``set`` with the run labels merged in."""
        self._inner.set(amount, self._merge(attributes), *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Anything else is the wrapped instrument's."""
        return getattr(self._inner, name)


class _Metrics:
    """Lazy attribute namespace of the spec section 8 instruments."""

    def __init__(self) -> None:
        """Initialize the cache of lazily created metric instruments."""
        object.__setattr__(self, "_instruments", {})

    def __getattr__(self, name: str) -> Any:
        """Return a cached instrument, rejecting names outside the metric specs."""
        if name.startswith("_"):
            raise AttributeError(name)
        spec = _SPECS.get(name)
        if spec is None:
            raise AttributeError(name)
        instruments = object.__getattribute__(self, "_instruments")
        if name not in instruments:
            instruments[name] = _Instrument(self._create(spec))
        return instruments[name]

    @staticmethod
    def _create(spec: tuple[str, str, str]) -> Any:
        """Create a counter, histogram or gauge on the current meter provider."""
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
        """Expose the supported metric attribute names for introspection."""
        return sorted(_SPECS)


M = _Metrics()


def _default_otlp_reader() -> Any:
    """A periodic OTLP metric reader, or ``None`` when unavailable."""
    if (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") is None
        and os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") is None
        and "pytest" in sys.modules
    ):
        return None
    protocol = os.environ.get(
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
        os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    )
    try:
        if protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
        elif protocol == "http/protobuf":
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
        else:
            raise ValueError(f"Unsupported OTLP metrics protocol: {protocol}")

        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        return PeriodicExportingMetricReader(OTLPMetricExporter())
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

        _metrics._internal._METER_PROVIDER = None
        _metrics._internal._METER_PROVIDER_SET_ONCE._done = False
    except Exception:  # pragma: no cover - defensive
        logger.debug("metrics_reset_failed", exc_info=True)
