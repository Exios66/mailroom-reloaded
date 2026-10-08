"""Endpoint unit tests: synthetic uploads and isolated filesystem state."""

import hashlib
import importlib
import io
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from mailroom_reloaded.storage.bins import Bins

api = importlib.import_module("mailroom_reloaded.api.app")


@pytest.fixture
def bins(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    return Bins(tmp_path)


def upload(filename, content=b"document"):
    return UploadFile(filename=filename, file=io.BytesIO(content))


@pytest.mark.parametrize(
    "filename", [None, "", ".hidden.txt", "folder/", "letter.exe", "letter"]
)
async def test_invalid_upload_name_is_rejected_before_reading(bins, filename):
    file = upload(filename)
    file.read = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await api.upload_document(file)
    assert exc.value.status_code == 400
    file.read.assert_not_called()
    assert list(bins.inbox.iterdir()) == []


@pytest.mark.parametrize("size,status", [(0, 400), (8, None), (9, 413)])
async def test_upload_size_boundary(bins, monkeypatch, size, status):
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 8)
    content = b"x" * size
    file = upload("letter.txt", content)
    file.read = AsyncMock(wraps=file.read)
    if status is not None:
        with pytest.raises(HTTPException) as exc:
            await api.upload_document(file)
        assert exc.value.status_code == status
        assert list(bins.inbox.iterdir()) == []
    else:
        result = await api.upload_document(file)
        assert result == {
            "doc_id": hashlib.sha256(content).hexdigest()[:16],
            "file": "letter.txt",
            "status": "accepted",
        }
        assert (bins.inbox / "letter.txt").read_bytes() == content
    file.read.assert_awaited_once_with(9)


@pytest.mark.parametrize("filename", ["../../letter.TXT", r"C:\incoming\letter.TXT"])
async def test_upload_strips_directory_components(bins, filename):
    result = await api.upload_document(upload(filename))
    assert result["file"] == "letter.TXT"
    assert list(bins.inbox.iterdir()) == [bins.inbox / "letter.TXT"]
    assert (bins.inbox / "letter.TXT").read_bytes() == b"document"


async def test_upload_preserves_all_existing_collision_candidates(bins):
    for filename in ["letter.txt", "letter-1.txt"]:
        (bins.inbox / filename).write_bytes(b"original")

    result = await api.upload_document(upload("letter.txt", b"new content"))

    assert result["file"] == "letter-2.txt"
    assert result["doc_id"] == hashlib.sha256(b"new content").hexdigest()[:16]
    assert {p.name: p.read_bytes() for p in bins.inbox.iterdir()} == {
        "letter.txt": b"original",
        "letter-1.txt": b"original",
        "letter-2.txt": b"new content",
    }


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Basic local-test-token",
        "Bearer wrong",
        "Bearer local-test-token-extra",
    ],
)
def test_token_rejects_missing_or_inexact_credentials(monkeypatch, authorization):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "local-test-token")
    headers = (
        [(b"authorization", authorization.encode())]
        if authorization is not None
        else []
    )
    request = Request({"type": "http", "headers": headers})
    with pytest.raises(HTTPException) as exc:
        api.require_token(request)
    assert exc.value.status_code == 401


def test_token_accepts_configured_and_header_whitespace(monkeypatch):
    monkeypatch.setenv("MAILROOM_API_TOKEN", " local-test-token ")
    request = Request(
        {"type": "http", "headers": [(b"authorization", b"Bearer  local-test-token ")]}
    )
    assert api.require_token(request) is None


def test_review_forwards_corrections_and_reports_missing_document(monkeypatch):
    resolve = Mock(return_value=None)
    monkeypatch.setattr(api, "resolve_review", resolve)
    payload = api.ReviewResolve(
        action="correct",
        doc_type="contract",
        doc_subclass="nda",
        reviewer="Alice",
    )
    with pytest.raises(HTTPException) as exc:
        api.resolve_review_endpoint("missing", payload)
    assert exc.value.status_code == 404
    resolve.assert_called_once_with(
        "missing",
        "correct",
        doc_type="contract",
        doc_subclass="nda",
        reviewer="Alice",
    )


def test_cards_skip_malformed_files_and_return_sorted_valid_cards(bins):
    cards = bins.base / "runs" / "run-1" / "cards"
    cards.mkdir(parents=True)
    (cards / "b.json").write_text(json.dumps({"name": "second"}))
    (cards / "a.json").write_text(json.dumps({"name": "first"}))
    (cards / "broken.json").write_text("{invalid")
    (cards / "ignored.txt").write_text("{}")
    assert api.run_cards_endpoint("run-1") == {
        "run_id": "run-1",
        "cards": [{"name": "first"}, {"name": "second"}],
    }
    assert api.run_cards_endpoint("missing") == {"run_id": "missing", "cards": []}
