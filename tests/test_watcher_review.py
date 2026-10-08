"""Task 17 tests: the watcher and human-review resume.

The pipeline uses the ``mock`` provider through ``FakeOpenAI`` for the two
standalone LLM calls (sorter, specialist). BERT is monkeypatched and the
specialist is stubbed where a scenario needs speed and determinism, following
the established harness style in ``tests/pipeline/test_flow.py``.
"""

from __future__ import annotations

import json
import threading

import pytest
from fakes.openai_server import FakeOpenAI

from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.review import resolve_review
from mailroom_reloaded.storage import audit_log
from mailroom_reloaded.storage.bins import Bins, doc_id_for, load_manifest
from mailroom_reloaded.watcher import Watcher, _acquire_watcher_lock, _release_lock

CORR_SUBCLASS = {
    "doc_subclass": "email",
    "confidence": 0.99,
    "doc_type_disagree": False,
    "doc_type_disagree_reason": None,
}

CORR_EXTRACT = {
    "sender": "alice@example.com",
    "recipient": "bob@example.com",
    "additional_recipients": ["carol@example.com"],
    "communication_type": "email",
    "communication_date": "2026-01-02",
    "demand_amount": 1000.0,
    "action_items": ["reply by Friday"],
    "urgency": "normal",
    "intent": "request",
    "subject_matter": "the deal",
    "keywords": ["deal"],
    "confidence": 0.9,
}

LOW_CLASS = {
    "doc_type": "correspondence",
    "doc_subclass": "email",
    "confidence": 0.5,
    "doc_type_disagree": False,
    "doc_type_disagree_reason": None,
}

CORRUPT_PDF = b"%PDF-1.4\n\x00\xff garbage not a real pdf"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def fake_openai():
    """Yield a local fake OpenAI server and stop it after the test."""
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):
    """Point the mock provider at the local fake OpenAI server."""
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate the base directory and reset settings and SQLite state per test."""
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


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    """Disable retry delays and clear tool-support caches around each test."""
    from mailroom_reloaded.llm import retry, tooling

    monkeypatch.setattr(retry, "_sleep", lambda *_: None)
    tooling.reset_tool_support_cache()
    yield
    tooling.reset_tool_support_cache()


# --------------------------------------------------------------------------- helpers


def _patch_handoff(
    monkeypatch,
    mode=SortMode.SUBCLASS_ONLY,
    doc_type="correspondence",
    route="fast_path",
):
    """Stub BERT classification and handoff for a deterministic routing scenario."""
    locked = doc_type if mode is SortMode.SUBCLASS_ONLY else None
    verdict = BertVerdict(
        available=True,
        reason="ok",
        doc_type=doc_type,
        subclass=None,
        calibrated_confidence=0.99,
        margin=0.5,
        window_agreement=1.0,
        n_windows=1,
        route=route,
    )
    handoff = Handoff(mode, locked, f"BERT predicts class {doc_type}", route)
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None, *, filename=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)
    return verdict, handoff


def _patch_bert_unavailable(monkeypatch):
    """Force full LLM sorting by simulating disabled BERT inference."""
    verdict = BertVerdict(available=False, reason="flag_off")
    handoff = Handoff(SortMode.FULL, None, "", "bert_unavailable:flag_off")
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None, *, filename=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)


def _reply(provider, payload):
    """Queue a discarded tool-round draft then the final structured reply."""
    provider.reply("thinking").reply(json.dumps(payload))


def _write_inbox(base, name="letter.txt", text="A short business letter about the deal."):
    """Write a test document to the inbox and return its bins and path."""
    bins = Bins(base)
    path = bins.inbox / name
    path.write_text(text)
    return bins, path


def _fake_extract(confidence=1.0, data=None):
    """Build an extractor stub with fixed confidence, data and token usage."""
    def run(text, doc_type, doc_subclass, **kwargs):
        return ExtractResult(
            doc_type,
            data if data is not None else {},
            True,
            None,
            confidence,
            None,
            1,
            Usage(prompt_tokens=5, completion_tokens=5, calls=1),
        )

    return run


def _park(env, mock_provider, monkeypatch):
    """Run a document to the review bin via the classify-low-confidence route."""
    _patch_bert_unavailable(monkeypatch)
    for _ in range(3):
        _reply(mock_provider, LOW_CLASS)
    bins, path = _write_inbox(env)
    state = flow_mod.run_document(path, worker_id="w1")
    assert state.status == "parked"
    assert list(bins.review.glob("*.txt"))
    return bins, state


# --------------------------------------------------------------------------- tests


def test_two_workers_one_file(env, mock_provider, monkeypatch):
    """Verify concurrent watchers claim and archive an inbox file exactly once."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    _reply(mock_provider, CORR_EXTRACT)
    bins, _path = _write_inbox(env)

    workers = [Watcher(bins, f"w{i}", 1) for i in (1, 2)]
    results: list[int] = []
    barrier = threading.Barrier(2)

    def drain(watcher):
        barrier.wait()
        results.append(watcher.drain_once())

    threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert len(results) == 2
    assert sum(results) == 1  # exactly one claim won
    assert len(list((bins.archive / "correspondence").glob("*.txt"))) == 1
    assert len(list(bins.manifests.glob("*.json"))) == 1


def test_corrupt_file_goes_failed_and_watcher_continues(env, mock_provider, monkeypatch):
    """Verify corrupt input fails with a valid audit trail while other work completes."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    _reply(mock_provider, CORR_EXTRACT)
    bins = Bins(env)
    (bins.inbox / "corrupt.pdf").write_bytes(CORRUPT_PDF)
    (bins.inbox / "good.txt").write_text("A short business letter about the deal.")

    processed = Watcher(bins, "w1", 2).drain_once()

    assert processed == 2
    failed = list(bins.failed.glob("*.pdf"))
    assert len(failed) == 1
    assert len(list((bins.archive / "correspondence").glob("*.txt"))) == 1

    doc_id = doc_id_for(failed[0])
    entries = audit_log.entries(doc_id)
    assert any(
        "ingest_failed" in str(entry.payload.get("reason", "")) for entry in entries
    )
    assert audit_log.verify_chain(entries).ok


def test_startup_resumes_processing_manifest(env, mock_provider, monkeypatch):
    """Verify startup recovers interrupted processing without repeating completed sorting."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    bins, path = _write_inbox(env)

    def boom(*_a, **_k):
        raise RuntimeError("crash mid-document")

    monkeypatch.setattr(flow_mod, "_extract", boom)
    with pytest.raises(RuntimeError):
        flow_mod.run_document(path, worker_id="w1")

    processing = list(bins.processing("w1").glob("*.txt"))
    assert len(processing) == 1
    doc_id = doc_id_for(processing[0])
    manifest = load_manifest(bins, doc_id)
    assert manifest is not None and manifest.status == "processing"

    monkeypatch.setattr(flow_mod, "_extract", _fake_extract(confidence=1.0))
    watcher = Watcher(bins, "w2", 1)
    watcher._lock = _acquire_watcher_lock(bins.base / "watcher.lock")
    assert watcher._lock is not None
    try:
        assert watcher.drain_once() == 0  # only the crashed claim resumed
    finally:
        _release_lock(watcher._lock)
        watcher._lock = None
    assert watcher.resumed == 1

    assert list((bins.archive / "correspondence").glob("*.txt"))
    entries = audit_log.entries(doc_id)
    assert sum(1 for e in entries if e.node == "sort") == 1  # no duplicate sort
    assert audit_log.verify_chain(entries).ok


def test_review_correct_resumes_at_extract(env, mock_provider, monkeypatch):
    """Verify review corrections resume extraction with the new class and no sorting."""
    bins, parked = _park(env, mock_provider, monkeypatch)
    doc_id = parked.doc_id

    sort_calls = {"n": 0}
    real_sort = flow_mod._sort

    def counting_sort(*args, **kwargs):
        sort_calls["n"] += 1
        return real_sort(*args, **kwargs)

    seen: list[str] = []

    def fake_extract(text, doc_type, doc_subclass, **kwargs):
        seen.append(doc_type)
        return ExtractResult(
            doc_type,
            {"field": "value"},
            True,
            None,
            0.99,
            None,
            1,
            Usage(prompt_tokens=5, completion_tokens=5, calls=1),
        )

    monkeypatch.setattr(flow_mod, "_sort", counting_sort)
    monkeypatch.setattr(flow_mod, "_extract", fake_extract)

    state = resolve_review(
        doc_id,
        "correct",
        doc_type="insurance_claim",
        doc_subclass="fnol",
        reviewer="alice",
    )

    assert state is not None and state.status == "archived"
    assert sort_calls["n"] == 0  # no sorter call on resume
    assert seen == ["insurance_claim"]  # extraction ran with the corrected class
    assert list((bins.archive / "insurance_claim").glob("*.txt"))
    entries = audit_log.entries(doc_id)
    assert any(e.event == "review_resolved" for e in entries)
    assert audit_log.verify_chain(entries).ok


def test_review_reject_moves_failed(env, mock_provider, monkeypatch):
    """Verify rejection moves the source to failed and updates manifest and audit state."""
    bins, parked = _park(env, mock_provider, monkeypatch)
    doc_id = parked.doc_id

    state = resolve_review(doc_id, "reject", reviewer="bob")

    assert state is not None and state.status == "failed"
    assert list(bins.failed.glob("*.txt"))
    assert not list(bins.review.glob("*.txt"))
    manifest = load_manifest(bins, doc_id)
    assert manifest is not None and manifest.status == "failed"
    entries = audit_log.entries(doc_id)
    assert any(e.event == "review_resolved" for e in entries)
    assert audit_log.verify_chain(entries).ok
