"""OTLP protocol precedence and SDK endpoint resolution for each signal."""

import os
import sys

import pytest
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter as GrpcMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as GrpcSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter as HttpMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as HttpSpanExporter,
)
from opentelemetry.sdk.metrics import export as metrics_export

from mailroom_reloaded.obs.metrics import _default_otlp_reader
from mailroom_reloaded.obs.tracing import _default_otlp_exporter


@pytest.fixture(params=["metrics", "traces"])
def signal(request, monkeypatch):
    """Keep exporter construction real, but avoid metric reader background work."""
    for key in os.environ:
        if key.startswith("OTEL_EXPORTER_OTLP"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(metrics_export, "PeriodicExportingMetricReader", lambda exporter: exporter)
    return request.param


def make_exporter(signal):
    """Construct the default exporter for the selected signal."""
    return _default_otlp_reader() if signal == "metrics" else _default_otlp_exporter()


@pytest.mark.parametrize(
    "generic,specific,expected_protocol",
    [
        (None, None, "http/protobuf"),
        ("http/protobuf", None, "http/protobuf"),
        ("grpc", None, "grpc"),
        (None, "grpc", "grpc"),
        ("http/protobuf", "grpc", "grpc"),
        ("grpc", "http/protobuf", "http/protobuf"),
    ],
)
def test_protocol_precedence(signal, monkeypatch, generic, specific, expected_protocol):
    """Each signal overrides the shared protocol, with HTTP as the app default."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:1234/base/")
    if generic is not None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", generic)
    if specific is not None:
        monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_PROTOCOL", specific)
    other_signal = "TRACES" if signal == "metrics" else "METRICS"
    monkeypatch.setenv(
        f"OTEL_EXPORTER_OTLP_{other_signal}_PROTOCOL",
        "http/protobuf" if expected_protocol == "grpc" else "grpc",
    )
    exporter = make_exporter(signal)
    try:
        classes = {
            ("metrics", "grpc"): GrpcMetricExporter,
            ("metrics", "http/protobuf"): HttpMetricExporter,
            ("traces", "grpc"): GrpcSpanExporter,
            ("traces", "http/protobuf"): HttpSpanExporter,
        }
        assert isinstance(exporter, classes[signal, expected_protocol])
        expected_endpoint = (
            "collector:1234"
            if expected_protocol == "grpc"
            else f"http://collector:1234/base/v1/{signal}"
        )
        assert exporter._endpoint == expected_endpoint
    finally:
        if exporter is not None:
            exporter.shutdown()


@pytest.mark.parametrize("protocol", ["http/protobuf", "grpc"])
@pytest.mark.parametrize("generic_endpoint", [None, "http://ignored:1234"])
def test_signal_endpoint_is_used_verbatim(signal, monkeypatch, protocol, generic_endpoint):
    """Explicit signal endpoints override the base and never gain an HTTP suffix."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    if generic_endpoint is not None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", generic_endpoint)
    monkeypatch.setenv(
        f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT", "http://collector:1234/custom"
    )
    exporter = make_exporter(signal)
    try:
        expected = "collector:1234" if protocol == "grpc" else "http://collector:1234/custom"
        assert exporter is not None
        assert exporter._endpoint == expected
    finally:
        if exporter is not None:
            exporter.shutdown()


@pytest.mark.parametrize("protocol", ["http/protobuf", "grpc"])
def test_default_endpoint_matches_protocol(signal, monkeypatch, protocol):
    """Default production endpoints use port 4317 for gRPC and 4318 for HTTP."""
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_PROTOCOL", protocol)
    exporter = make_exporter(signal)
    try:
        expected = "localhost:4317" if protocol == "grpc" else f"http://localhost:4318/v1/{signal}"
        assert exporter is not None
        assert exporter._endpoint == expected
    finally:
        if exporter is not None:
            exporter.shutdown()


def test_no_collector_under_pytest(signal):
    """Unconfigured tests do not start exporters targeting localhost."""
    assert make_exporter(signal) is None


def test_unsupported_protocol(signal, monkeypatch, caplog):
    """Unsupported protocols retain the fail-soft behavior and report the cause."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:1234")
    monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_PROTOCOL", "unsupported")
    assert make_exporter(signal) is None
    assert f"Unsupported OTLP {signal} protocol" in caplog.text
