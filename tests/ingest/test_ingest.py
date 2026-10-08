import time

from mailroom_reloaded.ingest import clerk
from mailroom_reloaded.ingest.clerk import (
    IngestResult,
    apply_intake,
    ingest,
    validate_intake,
)
from mailroom_reloaded.ingest.vision import transcribe_pages


def test_txt_ingest(fixtures_dir):
    r = ingest(fixtures_dir / "letter.txt")
    assert isinstance(r, IngestResult)
    assert r.method == "text"
    assert r.error is None
    assert "Master Services Agreement" in r.text
    assert r.pages == 1
    assert r.clerk["raw_chars"] > 0 and r.clerk["method"] == "deterministic"


def test_pdf_text_layer(text_pdf):
    r = ingest(text_pdf)
    assert r.method == "pdf_text"
    assert r.error is None
    assert r.pages == 2
    assert "twenty-four months" in r.text
    assert "Thomas R. Alder" in r.text


def test_scanned_pdf_uses_vision(scanned_pdf, monkeypatch):
    seen = []
    monkeypatch.setattr(clerk, "transcribe_pages", lambda p: seen.append(p) or "Transcribed scan text")
    r = ingest(scanned_pdf)
    assert r.method == "vision"
    assert r.text == "Transcribed scan text"
    assert r.error is None
    assert seen == [scanned_pdf]


def test_scanned_pdf_vision_failure_sets_error(scanned_pdf, monkeypatch):
    def boom(_):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(clerk, "transcribe_pages", boom)
    r = ingest(scanned_pdf)
    assert r.text == "" and "endpoint down" in r.error


def test_corrupt_pdf_sets_error(corrupt_pdf):
    r = ingest(corrupt_pdf)
    assert r.error
    assert r.text == ""


def test_missing_and_unsupported_files_set_error(tmp_path):
    assert ingest(tmp_path / "nope.txt").error
    odd = tmp_path / "x.xyz"
    odd.write_text("hi")
    assert ingest(odd).error
    empty = tmp_path / "empty.txt"
    empty.write_text("  \n")
    assert ingest(empty).error


def test_400k_chars_under_2s(tmp_path):
    para = "The parties agree to the terms set out in this agree-\nment, effective as of the date above.  \n\n"
    text = (para * (400_000 // len(para) + 1))[:400_000]
    p = tmp_path / "big.txt"
    p.write_text(text)
    t0 = time.perf_counter()
    r = ingest(p)
    assert time.perf_counter() - t0 < 2.0
    assert r.error is None and len(r.text) > 300_000


def test_apply_intake_normalizes():
    cleaned, stats = apply_intake("agree-\nment text\r\n\r\n\r\n\r\nend", "a.txt")
    assert cleaned == "agreement text\n\nend"
    assert stats["changed"] is True and "messy" in stats


def test_validate_intake_clamps():
    text = "x" * 100
    out = validate_intake(
        {
            "triage": {"primary_doc_class": "bogus", "confidence": 7},
            "cleaned_text": "  ",
            "sections": [
                {"heading": "A", "role": "term", "start_offset": 0, "end_offset": 50},
                {"heading": "overlap", "role": "term", "start_offset": 10, "end_offset": 60},
                {"heading": "oob", "role": "term", "start_offset": 90, "end_offset": 500},
                {"heading": "B", "role": "weird", "start_offset": 60, "end_offset": 80},
                "junk",
            ],
        },
        text,
    )
    assert out["triage"]["primary_doc_class"] == "unknown"
    assert out["triage"]["confidence"] == 1.0
    assert out["cleaned_text"] is None
    assert [s["heading"] for s in out["sections"]] == ["A", "B"]
    assert out["sections"][1]["role"] == "other"


def test_transcribe_pages_sends_page_images(scanned_pdf, mock_provider):
    mock_provider.reply("# Letter\nHarbor & Finch LLP")
    out = transcribe_pages(scanned_pdf)
    assert "Harbor & Finch" in out
    content = mock_provider.requests[0]["messages"][-1]["content"]
    assert any(part.get("type") == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,") for part in content)
