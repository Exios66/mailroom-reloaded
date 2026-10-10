"""Audit log entry schema and hash-chain helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class AuditLogEntry(BaseModel):
    doc_id: str
    seq: int  # 1-based per doc_id
    node: str
    event: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=_now)
    prev_hash: str = ""
    entry_hash: str = ""


class ChainResult(BaseModel):
    ok: bool
    broken_at: int | None = None  # seq of the first entry that fails verification


class CatalogRecord(BaseModel):
    doc_id: str
    filename: str
    doc_type: str | None = None
    doc_subclass: str | None = None
    status: str = "new"
    archive_path: str | None = None
    file_sha256: str | None = None
    updated_at: datetime = Field(default_factory=_now)


def compute_entry_hash(entry: AuditLogEntry) -> str:
    """sha256 over the canonical JSON of the entry without ``entry_hash``
    (``prev_hash`` is part of the hashed body)."""
    body = entry.model_dump(mode="json", exclude={"entry_hash"})
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
