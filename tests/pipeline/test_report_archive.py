"""Task 15 tests: deterministic report writer and archivist."""

from __future__ import annotations

import hashlib

import pytest

from mailroom_reloaded.agents.arbiter import ArbiterDecision
from mailroom_reloaded.agents.sorter import SortResult
from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import SortMode
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline.archivist import archive_document
from mailroom_reloaded.pipeline.report import compile_report
from mailroom_reloaded.pipeline.state import MailroomState
from mailroom_reloaded.schemas.manifest import Manifest
from mailroom_reloaded.storage import audit_log
from mailroom_reloaded.storage.bins import Bins, doc_id_for


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate settings.base_dir and the module-level SQLite engine per test."""
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    from mailroom_reloaded import settings

    settings.get_settings.cache_clear()
    from mailroom_reloaded.storage import db

    monkeypatch.setattr(db, "_default_engine", None)
    try:
        yield tmp_path
    finally:
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()


def _state(**overrides) -> MailroomState:
    state = MailroomState(
        doc_id="d1",
        path="",
        text="correspondence body",
        sort=SortResult(
            "correspondence",
            "email",
            0.99,
            0.9,
            False,
            SortMode.FULL,
            "self_report",
            False,
            None,
            Usage(prompt_tokens=10, completion_tokens=5, calls=2),
        ),
        extract=ExtractResult(
            "correspondence",
            {"sender": "alice@example.com"},
            True,
            None,
            0.99,
            None,
            1,
            Usage(prompt_tokens=10, completion_tokens=5, calls=1),
        ),
        arbiter=ArbiterDecision(
            action="accept_with_caveats",
            caveats=["date format differs", "missing PO number"],
        ),
        route_trail=[
            "ingest",
            "bert_primary",
            "sort",
            "gate_classify",
            "extract",
            "gate_extract",
            "verify",
        ],
        usage_total=Usage(prompt_tokens=20, completion_tokens=10, calls=3),
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_report_includes_caveats():
    state = _state()
    report = compile_report(state)

    assert report["caveats"] == ["date format differs", "missing PO number"]
    assert report["arbiter"] == {
        "action": "accept_with_caveats",
        "caveats": ["date format differs", "missing PO number"],
    }
    assert report["classification"]["doc_type"] == "correspondence"
    assert report["extraction"]["confidence"] == 0.99
    assert report["route_trail"] == state.route_trail
    assert report["usage"] == {
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "total_tokens": 30,
        "calls": 3,
    }
    assert "usd" in report["cost"]


def test_report_no_llm(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("compile_report must not call the LLM")

    from mailroom_reloaded.llm import client

    monkeypatch.setattr(client, "call_structured", _boom)
    report = compile_report(_state())
    assert report["doc_id"] == "d1"


def test_archive_sha_matches_file(env):
    bins = Bins(env)
    src = bins.processing("w1") / "letter.txt"
    src.write_text("hello world")
    doc_id = doc_id_for(src)
    manifest = Manifest(doc_id=doc_id, filename="letter.txt", content_sha256="x")
    state = _state(doc_id=doc_id, path=str(src))

    result = archive_document(bins, manifest, state)

    assert result.path.parent == bins.archive / "correspondence"
    assert result.path.read_text() == "hello world"
    assert result.file_sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert result.sidecar_path.exists()
    assert result.sidecar_path.name == result.path.name + ".report.json"
    assert not src.exists()


def test_archive_audit_chain_verifies(env):
    bins = Bins(env)
    src = bins.processing("w1") / "letter.txt"
    src.write_text("hello world")
    doc_id = doc_id_for(src)
    manifest = Manifest(doc_id=doc_id, filename="letter.txt", content_sha256="x")
    state = _state(doc_id=doc_id, path=str(src))

    result = archive_document(bins, manifest, state)

    entries = audit_log.entries(doc_id)
    assert entries[-1].event == "archived"
    assert entries[-1].node == "archive"
    assert entries[-1].payload["file_sha256"] == result.file_sha256
    assert audit_log.verify_chain(entries).ok
