"""Shared test helpers. ``assert_no_gt`` is created here and reused by Task 20."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

__all__ = ["assert_no_gt"]


def _gt_values(gt_row: Any) -> set[str]:
    """Every checkable ground-truth scalar (strings of 4+ chars) in ``gt_row``."""
    if hasattr(gt_row, "model_dump"):
        gt_row = gt_row.model_dump()
    elif dataclasses.is_dataclass(gt_row) and not isinstance(gt_row, type):
        gt_row = dataclasses.asdict(gt_row)
    values: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            text = value.strip()
            if len(text) >= 4:
                values.add(text)

    walk(gt_row)
    return values


def _request_text(request: dict[str, Any]) -> str:
    """The model-visible text of a recorded request (message content + tool calls)."""
    parts: list[str] = []
    for message in request.get("messages", []) or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif content is not None:
            parts.append(json.dumps(content, default=str))
        for call in message.get("tool_calls") or []:
            parts.append(json.dumps(call, default=str))
    return "\n".join(parts)


def assert_no_gt(requests: list[dict[str, Any]], gt_row: Any) -> None:
    """Assert no ground-truth value appears in any recorded request.

    ``requests`` is ``FakeOpenAI.requests``; ``gt_row`` is a ground-truth field
    dict (or a dataclass/pydantic object). Raises AssertionError with the
    leaking values and request index.
    """
    values = _gt_values(gt_row)
    assert values, "ground-truth row produced no checkable values"
    for index, request in enumerate(requests):
        blob = _request_text(request)
        leaked = sorted(v for v in values if v in blob)
        assert not leaked, f"ground truth leaked into request {index}: {leaked}"
