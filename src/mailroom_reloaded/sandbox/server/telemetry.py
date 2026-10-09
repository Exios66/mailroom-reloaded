"""Stop the package-level OTLP exporters from retrying against a collector that is not there."""

from __future__ import annotations

import logging

__all__ = ["silence_exporters"]


def silence_exporters() -> None:
    """Shut the global tracer/meter providers (they are set up at ``import mailroom_reloaded``).

    The sandbox has no collector; without this the default OTLP exporters keep
    retrying ``localhost:4318`` and spam the log. Spans become no-ops.
    """
    from opentelemetry import metrics, trace

    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        shutdown = getattr(provider, "shutdown", None)
        if shutdown is not None:
            try:
                shutdown()
            except Exception:
                logging.getLogger(__name__).debug(
                    "telemetry shutdown failed", exc_info=True
                )
