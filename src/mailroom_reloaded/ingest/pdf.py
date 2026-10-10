"""PDF text-layer extraction: pypdf, then pdfplumber, then pymupdf."""

from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

__all__ = ["PdfOpenError", "extract_text"]


class PdfOpenError(Exception):
    """No backend could open the file as a PDF."""


def _pypdf(path: Path) -> tuple[str, int]:
    """Return nonblank page text and total page count; pypdf errors propagate."""
    import pypdf

    reader = pypdf.PdfReader(str(path))
    pages = [(p.extract_text() or "") for p in reader.pages]
    return "\n\n".join(t for t in pages if t.strip()), len(pages)


def _pdfplumber(path: Path) -> tuple[str, int]:
    """Return nonblank page text and total page count; pdfplumber errors propagate."""
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    return "\n\n".join(t for t in pages if t.strip()), len(pages)


def _pymupdf(path: Path) -> tuple[str, int]:
    """Return nonblank page text and total page count; PyMuPDF errors propagate."""
    import fitz

    with fitz.open(str(path)) as doc:
        pages = [page.get_text() or "" for page in doc]
    return "\n\n".join(t for t in pages if t.strip()), len(pages)


def extract_text(path: Path) -> tuple[str, int]:
    """Return ``(text, page_count)``; text is empty for an image-only PDF.

    Raises ``PdfOpenError`` when every backend fails to open the file.
    """
    errors: list[str] = []
    opened: tuple[str, int] | None = None
    for name, fn in (("pypdf", _pypdf), ("pdfplumber", _pdfplumber), ("pymupdf", _pymupdf)):
        try:
            text, pages = fn(path)
        except Exception as exc:  # noqa: BLE001 - try the next backend
            logger.debug("pdf_backend_failed", backend=name, error=str(exc))
            errors.append(f"{name}: {exc}")
            continue
        if text.strip():
            return text, pages
        opened = opened or ("", pages)
    if opened is not None:
        return opened
    raise PdfOpenError("; ".join(errors) or "unreadable PDF")
