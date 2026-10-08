"""Fixtures for ingest tests. PDFs are generated with reportlab, not committed."""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes.openai_server import fake_openai  # noqa: F401  (re-exported fixture)

FIXTURES = Path(__file__).parent / "fixtures"

LETTER_LINES = [
    "Harbor & Finch LLP",
    "Re: Renewal of Master Services Agreement dated April 1, 2021",
    "Dear Ms. Whitfield: our client agrees to extend the term for an additional",
    "twenty-four months, beginning April 1, 2024, on the existing fee schedule.",
]


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):  # noqa: F811
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


@pytest.fixture
def text_pdf(tmp_path) -> Path:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "text.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 720
    for line in LETTER_LINES:
        c.drawString(72, y, line)
        y -= 18
    c.showPage()
    c.drawString(72, 720, "Page two: sincerely, Thomas R. Alder, Partner.")
    c.save()
    return path


@pytest.fixture
def scanned_pdf(tmp_path) -> Path:
    """One page holding only a raster image (no text layer)."""
    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    img = Image.new("RGB", (850, 1100), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(LETTER_LINES):
        draw.text((60, 80 + 30 * i), line, fill="black")
    path = tmp_path / "scanned.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.drawImage(ImageReader(img), 0, 0, width=letter[0], height=letter[1])
    c.save()
    return path


@pytest.fixture
def corrupt_pdf(tmp_path, text_pdf) -> Path:
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(text_pdf.read_bytes()[:120] + b"\x00\xff garbage not a pdf")
    return path
