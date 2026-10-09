"""Vision transcription usage is captured per page and carried on the ingest result."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailroom_reloaded.ingest import clerk, vision
from mailroom_reloaded.ingest.clerk import ingest
from mailroom_reloaded.llm.client import LLMResult
from mailroom_reloaded.llm.usage import Usage

PAGE = Usage(prompt_tokens=1000, completion_tokens=200, latency_s=0.5, calls=1)


@pytest.fixture
def scanned3(tmp_path) -> Path:
    """Three raster-only pages (no text layer), so every page needs a vision call."""
    from PIL import Image
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    path = tmp_path / "scanned3.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    for _ in range(3):
        c.drawImage(
            ImageReader(Image.new("RGB", (400, 500), "white")),
            0,
            0,
            width=letter[0],
            height=letter[1],
        )
        c.showPage()
    c.save()
    return path


def _result(text: str, usage: Usage = PAGE) -> LLMResult:
    return LLMResult(text, None, usage, "stop", None, 0)


def test_transcribe_pages_reports_every_page(scanned3, monkeypatch) -> None:
    monkeypatch.setattr(
        vision, "call_structured", lambda role, messages, **kw: _result("page text")
    )
    usage: list[Usage] = []
    text = vision.transcribe_pages(scanned3, usage)
    assert text.count("page text") == 3
    assert sum(usage, Usage()) == Usage(3000, 600, 1.5, 3)


def test_transcribe_pages_without_collector_is_unchanged(scanned3, monkeypatch) -> None:
    monkeypatch.setattr(
        vision, "call_structured", lambda role, messages, **kw: _result("t")
    )
    assert vision.transcribe_pages(scanned3)


def test_ingest_result_carries_summed_vision_usage(scanned3, monkeypatch) -> None:
    monkeypatch.setattr(
        vision,
        "call_structured",
        lambda role, messages, **kw: _result("Dear Sir, this is a letter."),
    )
    result = ingest(scanned3)
    assert result.error is None and result.method == "vision"
    assert result.usage == Usage(3000, 600, 1.5, 3)


def test_failed_vision_keeps_spend_of_pages_already_done(scanned3, monkeypatch) -> None:
    calls = {"n": 0}

    def flaky(role, messages, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("endpoint down")
        return _result("ok")

    monkeypatch.setattr(vision, "call_structured", flaky)
    result = ingest(scanned3)
    assert result.text == "" and "endpoint down" in result.error
    assert result.usage == Usage(2000, 400, 1.0, 2)


def test_text_files_have_empty_usage(fixtures_dir) -> None:
    assert ingest(fixtures_dir / "letter.txt").usage == Usage()


def test_old_manifest_ingest_result_without_usage_still_loads() -> None:
    from mailroom_reloaded.pipeline.state import MailroomState

    state = MailroomState.model_validate(
        {
            "ingest": {
                "text": "t",
                "method": "text",
                "pages": 1,
                "stats": {},
                "clerk": {},
                "error": None,
            }
        }
    )
    assert state.ingest.usage == Usage()
    assert clerk.IngestResult("t", "text", 1).usage == Usage()
