"""mailroom-reloaded: a compressed Digital Mailroom on CrewAI Flows."""

import os

os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"

from mailroom_reloaded.obs.tracing import setup_tracing  # noqa: E402 - after the env guard

setup_tracing()

__version__ = "0.1.0"
