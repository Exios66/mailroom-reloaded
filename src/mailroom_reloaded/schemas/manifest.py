from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


class Manifest(BaseModel):
    doc_id: str
    filename: str
    content_sha256: str
    status: Literal["processing", "parked", "failed", "archived"] = "processing"
    completed_nodes: list[str] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=_now)


def next_node(manifest: Manifest, node_order: Sequence[str]) -> str | None:
    """First node in ``node_order`` not yet completed, or None when all are done."""
    done = set(manifest.completed_nodes)
    for node in node_order:
        if node not in done:
            return node
    return None
