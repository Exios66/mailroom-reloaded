"""Deterministic ingest: read a file, extract text, run the intake clerk.

Ported from ``agents/intake.py`` (deterministic parts only: ``apply_intake`` and
``validate_intake``). The LLM intake agent is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog

from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.scoring.intake import apply_intake as _apply_intake
from mailroom_reloaded.settings import load_taxonomy

from . import pdf as _pdf
from .vision import transcribe_pages

logger = structlog.get_logger(__name__)

__all__ = ["INTAKE_SECTION_ROLES", "IngestResult", "apply_intake", "ingest", "validate_intake"]

#: Section roles in material-priority order.
INTAKE_SECTION_ROLES: tuple[str, ...] = (
    "governing_law",
    "term",
    "termination",
    "signatures",
    "obligations",
    "parties",
    "definitions",
    "recitals",
    "other",
)

_TEXT_SUFFIXES = {".txt", ".md", ".text"}


@dataclass
class IngestResult:
    text: str
    method: Literal["text", "pdf_text", "vision"]
    pages: int
    stats: dict = field(default_factory=dict)
    clerk: dict = field(default_factory=dict)
    error: str | None = None
    #: Vision transcription spend (role ``pdf_transcriber``); empty for text files.
    usage: Usage = field(default_factory=Usage)


def apply_intake(text: str, filename: str | None = None) -> tuple[str, dict]:
    """Normalize ``text`` with the deterministic clerk; returns ``(cleaned, stats)``."""
    return _apply_intake(text, filename=filename)


def validate_triage(raw: dict) -> dict:
    """Clamp a triage read to the live taxonomy vocabulary."""
    doc_class = str(raw.get("primary_doc_class") or "unknown").strip().lower()
    if doc_class not in load_taxonomy().classes:
        doc_class = "unknown"
    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.0
    subclass = raw.get("doc_subclass") or None
    if subclass is not None:
        subclass = str(subclass).strip()[:80] or None
    kw = raw.get("keywords")
    keywords = [str(k).strip()[:80] for k in (kw if isinstance(kw, list) else [])[:6] if str(k).strip()]
    return {
        "primary_doc_class": doc_class,
        "doc_subclass": subclass,
        "confidence": round(confidence, 3),
        "gist": str(raw.get("gist") or "").strip()[:300],
        "keywords": keywords,
    }


def validate_intake(result: dict, text: str) -> dict:
    """Clamp an intake answer to the live contracts.

    Section offsets are converted to integers and checked against ``text``
    (start inclusive, end exclusive, in characters). Keep at most 40 sections
    in start order, dropping invalid or overlapping spans and mapping unknown
    roles to ``other``.
    """
    text = text or ""
    raw_triage = result.get("triage")
    triage = validate_triage(raw_triage if isinstance(raw_triage, dict) else {})

    raw_clean = result.get("cleaned_text")
    cleaned_text = None
    if isinstance(raw_clean, str) and raw_clean.strip():
        cleaned_text = raw_clean[: max(len(text) * 3 + 2000, 2000)]

    raw_changes = result.get("changes_applied")
    if not isinstance(raw_changes, list):
        raw_changes = []
    changes = [str(c).strip()[:120] for c in raw_changes if str(c).strip()][:10]

    sections: list[dict] = []
    raw_sections = result.get("sections")
    if isinstance(raw_sections, list):
        for s in raw_sections:
            if not isinstance(s, dict):
                continue
            try:
                start, end = int(s.get("start_offset")), int(s.get("end_offset"))
            except (TypeError, ValueError):
                continue
            if start < 0 or end <= start or end > len(text):
                continue
            role = str(s.get("role") or "other").strip().lower()
            if role not in INTAKE_SECTION_ROLES:
                role = "other"
            sections.append(
                {
                    "heading": str(s.get("heading") or "").strip()[:120],
                    "role": role,
                    "start_offset": start,
                    "end_offset": end,
                }
            )
    sections.sort(key=lambda s: s["start_offset"])
    deduped: list[dict] = []
    last_end = -1
    for s in sections:
        if s["start_offset"] < last_end:
            continue
        deduped.append(s)
        last_end = s["end_offset"]

    return {
        "triage": triage,
        "cleaned_text": cleaned_text,
        "changes_applied": changes,
        "sections": deduped[:40],
    }


def _fail(
    method: str,
    error: str,
    stats: dict | None = None,
    pages: int = 0,
    usage: Usage | None = None,
) -> IngestResult:
    """Return an empty-text failure result, preserving supplied metadata and usage."""
    logger.warning("ingest_failed", error=error)
    return IngestResult("", method, pages, stats or {}, {}, error, usage or Usage())  # type: ignore[arg-type]


def ingest(path: Path) -> IngestResult:
    """Read a text file or PDF and return clerk-normalized text and ingest metadata.

    PDFs without extractable text use vision transcription. Read, PDF extraction
    and transcription failures, unsupported suffixes and empty normalized text
    produce a result with ``error`` set. Reported vision usage is retained even
    if a later page fails. Path conversion and clerk normalization errors propagate.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    page_usage: list[Usage] = []
    try:
        if suffix in _TEXT_SUFFIXES:
            raw = path.read_text(encoding="utf-8", errors="replace")
            method, pages, stats = "text", 1, {}
        elif suffix == ".docx":
            from docx import Document
            from docx.table import Table

            document = Document(path)
            blocks = []
            for block in document.iter_inner_content():
                if isinstance(block, Table):
                    blocks.extend("\t".join(cell.text for cell in row.cells) for row in block.rows)
                else:
                    blocks.append(block.text)
            raw = "\n".join(blocks)
            method, pages, stats = "text", 1, {}
        elif suffix == ".pdf":
            try:
                raw, pages = _pdf.extract_text(path)
            except _pdf.PdfOpenError as exc:
                return _fail("pdf_text", f"unreadable PDF: {exc}")
            if raw.strip():
                method, stats = "pdf_text", {}
            else:
                try:
                    raw = transcribe_pages(path, page_usage)
                except Exception as exc:  # noqa: BLE001 - ingest never raises
                    return _fail(
                        "vision",
                        f"scanned PDF, vision transcription failed: {exc}",
                        pages=pages,
                        usage=sum(page_usage, Usage()),
                    )
                method, stats = "vision", {"text_layer": False}
        else:
            return _fail("text", f"unsupported file type: {suffix or path.name}")
    except Exception as exc:  # noqa: BLE001 - ingest never raises
        return _fail("text", f"{type(exc).__name__}: {exc}", usage=sum(page_usage, Usage()))

    usage = sum(page_usage, Usage())
    cleaned, clerk_stats = apply_intake(raw, path.name)
    stats = {**stats, "raw_chars": len(raw), "chars": len(cleaned)}
    if not cleaned.strip():
        return IngestResult("", method, pages, stats, clerk_stats, "no extractable text", usage)  # type: ignore[arg-type]
    return IngestResult(cleaned, method, pages, stats, clerk_stats, None, usage)  # type: ignore[arg-type]
