"""Deterministic archivist (ported from ``agents/archivist.py``).

``archive_document`` moves the working file into ``archive/<doc_type>/``, writes
a ``<name>.report.json`` sidecar next to it and appends the hash-chained audit
entry ``archived`` carrying the archived file's sha256.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from mailroom_reloaded.schemas.manifest import Manifest
from mailroom_reloaded.storage import audit_log
from mailroom_reloaded.storage.bins import Bins

__all__ = ["ArchiveResult", "archive_document"]

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_doc_type(state) -> str:
    """Archive folder name: the extraction (else sort) doc_type, filesystem-safe."""
    for source in (getattr(state, "extract", None), getattr(state, "sort", None)):
        doc_type = getattr(source, "doc_type", None)
        if doc_type:
            return _SAFE.sub("_", str(doc_type)) or "unknown"
    return "unknown"


def _sha256(path: Path) -> str:
    """Streaming sha256 hex digest of the file at ``path``."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ArchiveResult:
    """Where the file landed, its sha256, and its ``.report.json`` sidecar path."""

    path: Path
    file_sha256: str
    sidecar_path: Path


def archive_document(bins: Bins, manifest: Manifest, state) -> ArchiveResult:
    """Move ``state.path`` into ``archive/<doc_type>/`` and record the sha256."""
    src = Path(state.path)
    doc_type = _safe_doc_type(state)
    dest_dir = bins.archive / doc_type
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}_{src.name}"
    os.replace(src, dest)
    state.path = str(dest)

    file_sha256 = _sha256(dest)
    report = getattr(state, "report", None) or {}
    sidecar = dest.with_name(f"{dest.name}.report.json")
    sidecar.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    audit_log.append(
        state.doc_id,
        "archive",
        "archived",
        {
            "file_sha256": file_sha256,
            "path": str(dest),
            "sidecar": str(sidecar),
            "doc_type": doc_type,
        },
    )
    return ArchiveResult(path=dest, file_sha256=file_sha256, sidecar_path=sidecar)
