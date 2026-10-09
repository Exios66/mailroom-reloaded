"""Token and latency accounting for LLM calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["RoleUsage", "Usage", "add_role_usage", "usage_from_crew"]


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    calls: int = 0  # successful calls only

    def __add__(self, other: object) -> Usage:
        if isinstance(other, int) and other == 0:  # sum() start value
            return self
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.latency_s + other.latency_s,
            self.calls + other.calls,
        )

    __radd__ = __add__

    def __sub__(self, other: object) -> Usage:
        """Per-invocation delta; each field is clamped at zero (never negative)."""
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            max(0, self.prompt_tokens - other.prompt_tokens),
            max(0, self.completion_tokens - other.completion_tokens),
            max(0.0, self.latency_s - other.latency_s),
            max(0, self.calls - other.calls),
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


#: Usage keyed by agent role (taxonomy agent names plus ``grader``).
RoleUsage = dict[str, Usage]


def add_role_usage(by_role: RoleUsage, role: str, usage: Usage) -> None:
    """Add ``usage`` to ``by_role[role]`` in place (no-op for an empty usage)."""
    if usage == Usage():
        return
    by_role[role] = by_role.get(role, Usage()) + usage


def usage_from_crew(token_usage: Any) -> Usage:
    """Convert CrewAI token counts and ``successful_requests`` into usage.

    Missing or false-valued attributes become zero; latency remains zero because
    it is not reported here. Invalid integer conversions propagate ``TypeError``,
    ``ValueError`` or ``OverflowError``.
    """
    return Usage(
        prompt_tokens=int(getattr(token_usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(token_usage, "completion_tokens", 0) or 0),
        calls=int(getattr(token_usage, "successful_requests", 0) or 0),
    )
