"""Token and latency accounting for LLM calls."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    calls: int = 0  # successful calls only

    def __add__(self, other: object) -> Usage:
        """Return summed usage counters without modifying either operand.

        Integer zero returns this instance for ``sum()``; other unsupported
        operands return ``NotImplemented``.
        """
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

    @property
    def total_tokens(self) -> int:
        """Return prompt tokens plus completion tokens."""
        return self.prompt_tokens + self.completion_tokens
