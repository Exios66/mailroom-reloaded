"""Vision fallback: render PDF pages to images and transcribe them with the vision model."""

from __future__ import annotations

import base64
from pathlib import Path

import structlog

from mailroom_reloaded.llm.client import call_structured
from mailroom_reloaded.settings import load_taxonomy

logger = structlog.get_logger(__name__)

__all__ = ["render_pdf_pages", "transcribe_pages"]

SYSTEM_PROMPT = """You are a legal document transcriber. Transcribe the attached page images of a document into clean, well-structured markdown suitable for downstream legal document analysis agents.

Rules:
1. Preserve the original document structure: headings, sections, paragraphs.
2. Use markdown formatting: # for titles, ## for sections, **bold** for emphasized text.
3. Format tables as markdown tables. Preserve signature blocks.
4. Do not add, remove, or alter any facts; only format and structure.
5. Illegible spans are [illegible], never guessed words.
6. Remove clear page metadata (page numbers, repeated headers/footers).
7. Return only the transcription. No confidence score, commentary or summary.

PRODUCTION DOCTRINE (mailroom pipeline):
- Transcribe; do not summarize, classify, or extract fields."""


def _vision_cfg() -> tuple[int, int]:
    cfg = load_taxonomy().raw.get("vision") or {}
    return int(cfg.get("max_pages", 10)), int(cfg.get("dpi", 150))


def render_pdf_pages(path: Path, cap: int | None = None, dpi: int | None = None) -> list[str]:
    """Render PDF pages to PNG data URIs. ``cap`` of 0/None renders every page."""
    import fitz

    if dpi is None:
        dpi = _vision_cfg()[1]
    zoom = dpi / 72.0
    uris: list[str] = []
    with fitz.open(str(path)) as doc:
        limit = doc.page_count if not cap or cap <= 0 else min(cap, doc.page_count)
        for idx in range(limit):
            pix = doc.load_page(idx).get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB)
            uris.append("data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode("ascii"))
    return uris


def transcribe_pages(pdf_path: Path) -> str:
    """Transcribe every rendered page (up to ``vision.max_pages``; 0 = all) of a scanned PDF.

    One ``pdf_transcriber`` call per page, joined in page order. Raises on render
    failure or when no page yields text, so the caller can record an error.
    """
    pdf_path = Path(pdf_path)
    cap, dpi = _vision_cfg()
    pages = render_pdf_pages(pdf_path, cap=cap, dpi=dpi)
    if not pages:
        raise RuntimeError(f"no pages rendered from {pdf_path.name}")
    out: list[str] = []
    for n, uri in enumerate(pages, start=1):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"File: {pdf_path.name}, page {n} of {len(pages)}. Transcribe this page."},
                    {"type": "image_url", "image_url": {"url": uri}},
                ],
            },
        ]
        res = call_structured("pdf_transcriber", messages)
        if res.content.strip():
            out.append(res.content.strip())
    text = "\n\n".join(out)
    if not text.strip():
        raise RuntimeError(f"vision transcription of {pdf_path.name} returned no text")
    logger.info("pdf_vision_transcribed", file=pdf_path.name, pages=len(pages), chars=len(text))
    return text
