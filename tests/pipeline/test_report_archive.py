"""Task 15 tests: deterministic report writer and archivist."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mailroom_reloaded.agents.arbiter import ArbiterDecision
from mailroom_reloaded.agents.sorter import SortResult
from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import SortMode
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline import report as report_mod
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


def test_report_handles_document_without_agent_results():
    report = compile_report(MailroomState(doc_id="empty", status="failed"))
    assert report["doc_id"] == "empty"
    assert report["status"] == "failed"
    for key in ("classification", "extraction", "verdict", "arbiter", "boss"):
        assert report[key] is None
    assert report["caveats"] == report["route_trail"] == []
    assert report["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }
    assert report["cost"]["usd"] == 0.0


def test_report_retains_failed_extraction_diagnostics():
    state = _state(
        extract=ExtractResult(
            "correspondence",
            None,
            False,
            "invalid JSON",
            None,
            "LengthFinishReasonError",
            2,
            Usage(calls=2),
        )
    )
    assert compile_report(state)["extraction"] == {
        "doc_type": "correspondence",
        "data": None,
        "schema_valid": False,
        "parse_error": "invalid JSON",
        "confidence": None,
        "error_kind": "LengthFinishReasonError",
        "calls": 2,
    }


def test_report_copies_route_and_caveat_lists():
    state = _state()
    report = compile_report(state)
    report["route_trail"].clear()
    report["caveats"].append("report-only caveat")
    report["arbiter"]["caveats"].clear()
    assert state.route_trail[-1] == "verify"
    assert state.arbiter.caveats == ["date format differs", "missing PO number"]
    assert report["caveats"] == [
        "date format differs",
        "missing PO number",
        "report-only caveat",
    ]


@pytest.mark.parametrize(
    "prices,expected",
    [
        ({"input_per_million": 2, "output_per_million": 8}, 5.0),
        ({"input_per_million": "2"}, 1.0),
        ({"output_per_million": 8}, 4.0),
        ({}, 0.0),
        (None, 0.0),
        ("unavailable", 0.0),
    ],
)
def test_report_token_price_calculation(monkeypatch, prices, expected):
    taxonomy = SimpleNamespace(
        agent=lambda role: SimpleNamespace(model="test-model"),
        raw={"cost_models": {"test-model": prices}},
    )
    monkeypatch.setattr(report_mod, "load_taxonomy", lambda: taxonomy)
    state = _state(
        usage_total=Usage(prompt_tokens=500_000, completion_tokens=500_000, calls=1)
    )
    assert compile_report(state)["cost"] == {
        "usd": pytest.approx(expected),
        "pricing": "per_token_estimate",
    }


def test_report_survives_unavailable_taxonomy(monkeypatch):
    def unavailable():
        raise OSError("taxonomy unavailable")

    monkeypatch.setattr(report_mod, "load_taxonomy", unavailable)
    report = compile_report(_state())
    assert report["cost"]["usd"] == 0.0
    assert report["usage"]["total_tokens"] == 30


@pytest.mark.parametrize(
    "sort_type,extract_type,expected",
    [
        ("contract", "correspondence", "correspondence"),
        ("contract", None, "contract"),
        (None, None, "unknown"),
        (None, "folder/child", "folder_child"),
        (None, "/absolute", "_absolute"),
        (None, "claims & notices", "claims_notices"),
    ],
)
def test_archive_type_selection_and_sidecar(env, sort_type, extract_type, expected):
    bins = Bins(env)
    src = bins.processing("w1") / "letter.txt"
    src.write_bytes(b"synthetic document\x00\xff")
    state = _state(
        doc_id="archive-test", path=str(src), report={"note": "caf\u00e9", "count": 3}
    )
    if sort_type is None:
        state.sort = None
    else:
        state.sort = replace(state.sort, doc_type=sort_type)
    if extract_type is None:
        state.extract = None
    else:
        state.extract = replace(state.extract, doc_type=extract_type)
    manifest = Manifest(doc_id=state.doc_id, filename=src.name, content_sha256="unused")

    result = archive_document(bins, manifest, state)

    assert result.path.parent == bins.archive / expected
    assert result.path.is_relative_to(bins.archive)
    assert state.path == str(result.path)
    assert result.path.read_bytes() == b"synthetic document\x00\xff"
    assert json.loads(result.sidecar_path.read_text("utf-8")) == state.report
    entry = audit_log.entries(state.doc_id)[-1]
    assert entry.payload == {
        "file_sha256": result.file_sha256,
        "path": str(result.path),
        "sidecar": str(result.sidecar_path),
        "doc_type": expected,
    }


def test_archiving_same_filename_preserves_both_documents(env):
    bins = Bins(env)
    results = []
    for worker, text in [("w1", "first"), ("w2", "second")]:
        src = bins.processing(worker) / "letter.txt"
        src.write_text(text)
        state = _state(doc_id=worker, path=str(src), report={"worker": worker})
        manifest = Manifest(doc_id=worker, filename=src.name, content_sha256="unused")
        results.append(archive_document(bins, manifest, state))
    assert results[0].path != results[1].path
    assert [result.path.read_text() for result in results] == ["first", "second"]
    assert [json.loads(result.sidecar_path.read_text()) for result in results] == [
        {"worker": "w1"},
        {"worker": "w2"},
    ]


def test_missing_archive_source_does_not_record_success(env):
    bins = Bins(env)
    state = _state(doc_id="missing", path=str(env / "missing.txt"))
    manifest = Manifest(
        doc_id=state.doc_id, filename="missing.txt", content_sha256="unused"
    )
    original_path = state.path
    with pytest.raises(FileNotFoundError):
        archive_document(bins, manifest, state)
    assert state.path == original_path
    assert audit_log.entries(state.doc_id) == []
    assert not list(bins.archive.rglob("*.report.json"))
