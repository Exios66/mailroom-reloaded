"""Node guards: manifest checkpointing, audit entries, spans and budgets.

``guarded(node_name, deadline_s, token_budget)`` wraps a node implementation so
that, around each call, the flow:

* records the node in ``manifest.completed_nodes`` and snapshots the serialised
  state into the manifest (crash-resume),
* appends a hash-chained audit entry,
* emits the OpenTelemetry span ``mailroom.node.<node_name>``,
* skips nodes already completed before this run started, and
* fails the document (moving it to ``failed/``) when the wall-clock or token
  budget is exceeded.

The wrapper delegates to ``MailroomFlow._guard_node``; the flow owns the bins,
manifest and state. A cooperative deadline is checked after the node returns
(a synchronous node cannot be preempted); see the implementation report.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = ["NODE_DEADLINES", "NODE_TOKEN_BUDGETS", "NodeFailed", "guarded"]

F = TypeVar("F", bound=Callable[..., Any])

#: Default per-node wall-clock deadline (seconds); 0 or negative means "no limit".
NODE_DEADLINES: dict[str, float] = {
    "ingest": 30.0,
    "bert_primary": 30.0,
    "sort": 120.0,
    "extract": 600.0,
    "verify": 180.0,
    "boss": 120.0,
    "report_catalog_archive": 30.0,
    "grade": 180.0,
}

#: Default per-node token budget (successful-call tokens); 0 means "no limit".
NODE_TOKEN_BUDGETS: dict[str, int] = {}


class NodeFailed(Exception):
    """A node exceeded its deadline/token budget or the document is terminal."""

    def __init__(self, node: str, reason: str) -> None:
        """Record the failing ``node`` and human-readable ``reason``."""
        super().__init__(f"{node}: {reason}")
        self.node = node
        self.reason = reason


def guarded(
    node_name: str, deadline_s: float = 0.0, token_budget: int = 0
) -> Callable[[F], F]:
    """Decorator factory described in the module docstring."""

    def decorator(fn: F) -> F:
        """Wrap ``fn`` so each call goes through the flow's ``_guard_node``."""

        @functools.wraps(fn)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            """Delegate one node call to ``self._guard_node`` with the bound config."""
            return self._guard_node(
                node_name, deadline_s, token_budget, fn, args, kwargs
            )

        wrapper.__mailroom_node__ = node_name  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator
