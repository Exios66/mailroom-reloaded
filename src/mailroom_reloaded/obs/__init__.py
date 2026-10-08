"""Observability: OpenTelemetry traces (OpenInference) and pipeline metrics."""

from mailroom_reloaded.obs.metrics import M, setup_metrics
from mailroom_reloaded.obs.tracing import setup_tracing

__all__ = ["M", "setup_metrics", "setup_tracing"]
