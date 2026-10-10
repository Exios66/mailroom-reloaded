"""FastAPI native-telemetry exclusions: probe/static routes create no spans.

FastAPI 0.142 ships native OpenTelemetry (``fastapi.telemetry``) that creates an
HTTP span (plus ``fastapi.endpoint`` / ``fastapi.dependencies`` /
``fastapi.serialization`` operation spans) per request. The container healthcheck
hits ``/health`` every few seconds and the TUI loads ``/tui/assets/*``, so without
exclusions Phoenix fills with health/asset noise instead of the mailroom pipeline
traces. ``create_app`` passes ``telemetry={"exclude": _telemetry_exclude,
"operation_spans": False}``; these tests pin that behaviour.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mailroom_reloaded.api.app import _telemetry_exclude, create_app
from mailroom_reloaded.obs.tracing import setup_tracing


def _attach_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    setup_tracing(exporter=exporter)
    return exporter


def test_exclude_matches_probes_and_static_only():
    assert _telemetry_exclude({"type": "http", "path": "/health"}) is True
    assert _telemetry_exclude({"type": "http", "path": "/ready"}) is True
    assert _telemetry_exclude({"type": "http", "path": "/metrics"}) is True
    assert _telemetry_exclude({"type": "http", "path": "/tui/assets/main.js"}) is True
    assert _telemetry_exclude({"type": "http", "path": "/favicon.ico"}) is True
    assert _telemetry_exclude({"type": "http", "path": "/v1/documents"}) is False
    assert _telemetry_exclude({"type": "lifespan"}) is False


def test_probe_route_creates_no_spans():
    exporter = _attach_exporter()
    client = TestClient(create_app())
    assert client.get("/health").status_code == 200
    assert exporter.get_finished_spans() == ()


def test_non_probe_route_is_traced_without_operation_spans():
    exporter = _attach_exporter()
    client = TestClient(create_app())
    client.get("/links")
    names = [span.name for span in exporter.get_finished_spans()]
    assert any("links" in name.lower() for name in names), names
    # operation_spans=False: no fastapi.endpoint/dependencies/serialization noise.
    assert not [name for name in names if name.startswith("fastapi.")], names
