"""Task 17 tests: the watcher and human-review resume.

The pipeline uses the ``mock`` provider through ``FakeOpenAI`` for the two
standalone LLM calls (sorter, specialist). BERT is monkeypatched and the
specialist is stubbed where a scenario needs speed and determinism, following
the established harness style in ``tests/pipeline/test_flow.py``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fakes.openai_server import FakeOpenAI

from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.review import resolve_review
from mailroom_reloaded.storage import audit_log, catalog
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


@pytest.fixture
def ledger_state():
    """Start from empty process-level ledger registries and drop the ledger after the test."""
    from mailroom_reloaded.pipeline import run_ledger

    for registry in (run_ledger._open, run_ledger._invocations, run_ledger._closed, run_ledger._rolled):
        registry.clear()
    try:
        yield
    finally:
        from mailroom_reloaded.storage.ledger import reset_ledger

        reset_ledger()


def _doc_closed(doc_id):
    """The archive ledger's ``doc_closed`` entries for one document, in seq order."""
    from mailroom_reloaded.storage.ledger import get_ledger

    ledger = get_ledger()
    assert ledger.flush()
    return ledger.entries(kind="doc_closed", doc_id=doc_id, limit=100)


def test_reject_records_failed_ledger_disposition(env, mock_provider, monkeypatch, ledger_state):
    """Rejecting a parked document closes its invocation in the archive ledger as failed (#114)."""
    from mailroom_reloaded.storage.ledger import get_ledger

    bins, parked = _park(env, mock_provider, monkeypatch)
    doc_id = parked.doc_id
    assert [e.payload["outcome"] for e in _doc_closed(doc_id)] == ["parked"]

    state = resolve_review(doc_id, "reject", reviewer="bob")

    assert state is not None and state.status == "failed"
    closes = _doc_closed(doc_id)
    assert [e.payload["outcome"] for e in closes] == ["parked", "failed"]
    rejected = closes[-1].payload
    assert rejected["doc_type"] == "correspondence"
    assert "failure_reason" not in rejected  # a review decision, not a pipeline failure
    assert get_ledger().verify().ok

    manifest = load_manifest(bins, doc_id)
    assert manifest is not None and manifest.status == "failed"
    rec = catalog.get(doc_id)
    assert rec is not None and rec.status == "failed"
    entries = audit_log.entries(doc_id)
    assert any(e.event == "review_resolved" for e in entries)
    assert audit_log.verify_chain(entries).ok


def test_reject_twice_does_not_duplicate_ledger_close(env, mock_provider, monkeypatch, ledger_state):
    """A second reject finds no parked manifest and writes no second terminal ledger close."""
    _bins, parked = _park(env, mock_provider, monkeypatch)
    doc_id = parked.doc_id

    assert resolve_review(doc_id, "reject", reviewer="bob") is not None
    assert resolve_review(doc_id, "reject", reviewer="bob") is None

    assert [e.payload["outcome"] for e in _doc_closed(doc_id)] == ["parked", "failed"]
    reviews = [e for e in audit_log.entries(doc_id) if e.event == "review_resolved"]
    assert len(reviews) == 1


def test_reject_ledger_failure_never_changes_outcome(env, mock_provider, monkeypatch, ledger_state):
    """A broken ledger write leaves the rejection, manifest and catalog intact (best effort)."""
    from mailroom_reloaded.pipeline import run_ledger

    bins, parked = _park(env, mock_provider, monkeypatch)

    def broken(*_a, **_k):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(run_ledger, "record_review_rejected", broken, raising=False)
    state = resolve_review(parked.doc_id, "reject", reviewer="bob")

    assert state is not None and state.status == "failed"
    assert list(bins.failed.glob("*.txt"))
    manifest = load_manifest(bins, parked.doc_id)
    assert manifest is not None and manifest.status == "failed"
    assert catalog.get(parked.doc_id).status == "failed"
    assert audit_log.verify_chain(audit_log.entries(parked.doc_id)).ok


# ---- findings 6 and 7: reconcile at startup ----


def _run_and_get(env, mock_provider, monkeypatch):
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    monkeypatch.setattr(flow_mod, "_extract", _fake_extract(confidence=1.0))
    return _write_inbox(env)


@pytest.mark.parametrize("legacy", [False, True])
def test_crash_between_archive_and_snapshot_is_reconciled(
    env, mock_provider, monkeypatch, legacy
):
    """Finding 6: archived audit entry + archived file but manifest still processing."""
    bins, path = _run_and_get(env, mock_provider, monkeypatch)
    real_append = audit_log.append

    def append_without_intent(doc_id, node, event, payload):
        if event != "archiving":
            return real_append(doc_id, node, event, payload)
        return None

    if legacy:
        monkeypatch.setattr(audit_log, "append", append_without_intent)

    def boom(self, node_name, elapsed):
        if node_name == "report_catalog_archive":
            raise RuntimeError("crash after archive_document")
        return real_record(self, node_name, elapsed)

    real_record = flow_mod.MailroomFlow._record_node
    monkeypatch.setattr(flow_mod.MailroomFlow, "_record_node", boom)
    with pytest.raises(RuntimeError):
        flow_mod.run_document(path, worker_id="w1")
    monkeypatch.setattr(flow_mod.MailroomFlow, "_record_node", real_record)

    archived = list((bins.archive / "correspondence").glob("*.txt"))
    assert len(archived) == 1
    doc_id = next(bins.manifests.glob("*.json")).stem
    stale = load_manifest(bins, doc_id)
    assert stale.status == "processing"
    assert not Path(stale.state["path"]).exists()

    assert Watcher(bins, "w2").resume_processing() == 0

    manifest = load_manifest(bins, doc_id)
    assert manifest.status == "archived"
    assert manifest.state["status"] == "archived"
    assert manifest.state["path"] == str(archived[0])
    rec = catalog.get(doc_id)
    assert rec is not None and rec.archive_path == str(archived[0])
    assert audit_log.verify_chain(audit_log.entries(doc_id)).ok
    assert any(entry.event == "archiving" for entry in audit_log.entries(doc_id)) is not legacy


def test_missing_source_without_archive_is_left_and_warned(env, caplog):
    """Finding 6: no archived audit entry -> leave the manifest alone."""
    from mailroom_reloaded.schemas.manifest import Manifest
    from mailroom_reloaded.storage.bins import save_manifest

    bins = Bins(env)
    save_manifest(
        bins,
        Manifest(
            doc_id="gone",
            filename="x.txt",
            content_sha256="h",
            state={"path": str(env / "nope.txt")},
        ),
    )
    Watcher(bins, "w").resume_processing()
    assert load_manifest(bins, "gone").status == "processing"
    assert catalog.get("gone") is None


def test_catalog_failure_sets_pending_and_startup_reconciles(
    env, mock_provider, monkeypatch
):
    """Finding 7: upsert raises once -> marker -> startup retry -> record present."""
    bins, path = _run_and_get(env, mock_provider, monkeypatch)
    real_upsert = catalog.upsert
    calls = {"n": 0}

    def flaky(record, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("catalog down")
        return real_upsert(record, **kw)

    monkeypatch.setattr(flow_mod.catalog, "upsert", flaky)
    state = flow_mod.run_document(path, worker_id="w1")
    doc_id = state.doc_id
    assert state.status == "archived"
    assert catalog.get(doc_id) is None
    manifest = load_manifest(bins, doc_id)
    assert manifest.status == "archived"
    assert manifest.catalog_pending is not None

    Watcher(bins, "w2").resume_processing()

    rec = catalog.get(doc_id)
    assert rec is not None and rec.status == "archived"
    assert load_manifest(bins, doc_id).catalog_pending is None

def test_parked_document_is_cataloged_as_parked(env, mock_provider, monkeypatch):
    """Parked documents appear in the catalog so status listings can find them."""
    from mailroom_reloaded.storage import catalog

    _bins, parked = _park(env, mock_provider, monkeypatch)

    rec = catalog.get(parked.doc_id)
    assert rec is not None and rec.status == "parked"
    assert [r.doc_id for r in catalog.list(status="parked")] == [parked.doc_id]


def test_reject_updates_catalog_to_failed(env, mock_provider, monkeypatch):
    """Rejecting a parked document moves its catalog row out of 'parked'."""
    from mailroom_reloaded.storage import catalog

    _bins, parked = _park(env, mock_provider, monkeypatch)
    resolve_review(parked.doc_id, "reject", reviewer="bob")

    rec = catalog.get(parked.doc_id)
    assert rec is not None and rec.status == "failed"
    assert catalog.list(status="parked") == []


@pytest.mark.parametrize("action", ["approve", "correct"])
def test_approve_updates_catalog_to_archived(env, mock_provider, monkeypatch, action):
    """Real review ordering preserves the archived catalog and audit replay status."""
    from mailroom_reloaded.obs.replay.audit_source import timeline_from_audit
    from mailroom_reloaded.storage import catalog

    _bins, parked = _park(env, mock_provider, monkeypatch)

    def fake_extract(text, doc_type, doc_subclass, **kwargs):
        return ExtractResult(
            doc_type, {"field": "value"}, True, None, 0.99, None, 1,
            Usage(prompt_tokens=5, completion_tokens=5, calls=1),
        )

    monkeypatch.setattr(flow_mod, "_extract", fake_extract)
    resolve_review(
        parked.doc_id, action, doc_type="insurance_claim", doc_subclass="fnol"
    )

    rec = catalog.get(parked.doc_id)
    assert rec is not None and rec.status == "archived"
    assert catalog.list(status="parked") == []
    entries = audit_log.entries(parked.doc_id)
    assert entries[-1].event == "review_resolved"
    assert any(entry.event == "archived" for entry in entries[:-1])
    timeline = timeline_from_audit("doc", parked.doc_id)
    assert timeline is not None
    assert timeline.entities[0].final_status == "archived"


def test_flow_crash_during_resolve_reopens_parked_and_retry_succeeds(
    env, mock_provider, monkeypatch
):
    """A flow crash after it re-claimed the file leaves the doc parked, not 404."""

    bins, parked = _park(env, mock_provider, monkeypatch)
    doc_id = parked.doc_id

    def boom(*args, **kwargs):
        raise RuntimeError("extract exploded")

    monkeypatch.setattr(flow_mod, "_extract", boom)
    with pytest.raises(RuntimeError):
        resolve_review(doc_id, "approve")

    manifest = load_manifest(bins, doc_id)
    assert manifest is not None and manifest.status == "parked"
    assert list(bins.review.glob("*.txt"))

    def fake_extract(text, doc_type, doc_subclass, **kwargs):
        return ExtractResult(
            doc_type, {"field": "value"}, True, None, 0.99, None, 1,
            Usage(prompt_tokens=5, completion_tokens=5, calls=1),
        )

    monkeypatch.setattr(flow_mod, "_extract", fake_extract)
    state = resolve_review(doc_id, "approve")
    assert state is not None and state.status == "archived"


def test_review_approved_cleared_after_reextraction(env, mock_provider, monkeypatch):
    """The approval flag does not outlive the re-extraction it authorised."""

    bins, parked = _park(env, mock_provider, monkeypatch)

    def fake_extract(text, doc_type, doc_subclass, **kwargs):
        return ExtractResult(
            doc_type, {"field": "value"}, True, None, 0.99, None, 1,
            Usage(prompt_tokens=5, completion_tokens=5, calls=1),
        )

    monkeypatch.setattr(flow_mod, "_extract", fake_extract)
    state = resolve_review(parked.doc_id, "approve")
    assert state is not None and state.status == "archived"
    assert state.review_approved is False
    manifest = load_manifest(bins, parked.doc_id)
    assert manifest is not None and manifest.state["review_approved"] is False


# ---- #111 / #112: crash windows around archive and claim ----


def test_crash_between_archive_move_and_audit_is_recovered(
    env, mock_provider, monkeypatch
):
    """#111: crash right after the archive rename, before any ``archived`` audit entry."""
    from mailroom_reloaded.pipeline import archivist

    bins, path = _run_and_get(env, mock_provider, monkeypatch)
    real_move = archivist._move

    def move_then_crash(src, dest):
        real_move(src, dest)
        raise RuntimeError("crash after os.replace")

    monkeypatch.setattr(archivist, "_move", move_then_crash)
    with pytest.raises(RuntimeError):
        flow_mod.run_document(path, worker_id="w1")
    monkeypatch.setattr(archivist, "_move", real_move)

    doc_id = next(bins.manifests.glob("*.json")).stem
    assert not any(e.event == "archived" for e in audit_log.entries(doc_id))
    archived = list((bins.archive / "correspondence").glob("*.txt"))
    assert len(archived) == 1

    assert Watcher(bins, "w2").resume_processing() == 0

    manifest = load_manifest(bins, doc_id)
    assert manifest.status == "archived"
    assert manifest.state["path"] == str(archived[0])
    assert Path(f"{archived[0]}.report.json").is_file()
    rec = catalog.get(doc_id)
    assert rec is not None and rec.archive_path == str(archived[0])
    entries = audit_log.entries(doc_id)
    assert [e.event for e in entries].count("archived") == 1
    assert audit_log.verify_chain(entries).ok

    # a second startup is a no-op: no duplicate terminal records
    Watcher(bins, "w3").resume_processing()
    assert [e.event for e in audit_log.entries(doc_id)].count("archived") == 1


@pytest.mark.parametrize("checkpoint", ["archiving", "archived"])
@pytest.mark.parametrize(
    "damage",
    [
        "file_mismatch", "missing_sidecar", "invalid_json", "invalid_shape",
        "wrong_doc", "wrong_report_status",
    ],
)
def test_archive_recovery_requires_intact_evidence(
    env, mock_provider, monkeypatch, ledger_state, checkpoint, damage
):
    """Invalid archive evidence requires operator repair, without changing durable state."""
    from mailroom_reloaded.pipeline import archivist
    from mailroom_reloaded.storage.ledger import get_ledger

    bins, path = _run_and_get(env, mock_provider, monkeypatch)
    real_append = audit_log.append

    def crash_at_checkpoint(doc_id, node, event, payload):
        if node == "archive" and event == "archived":
            if checkpoint == "archived":
                real_append(doc_id, node, event, payload)
            raise RuntimeError("crash after archive move")
        return real_append(doc_id, node, event, payload)

    monkeypatch.setattr(archivist.audit_log, "append", crash_at_checkpoint)
    with pytest.raises(RuntimeError, match="crash after archive move"):
        flow_mod.run_document(path, worker_id="w1")
    monkeypatch.setattr(audit_log, "append", real_append)

    doc_id = next(bins.manifests.glob("*.json")).stem
    archive_event = next(
        entry for entry in reversed(audit_log.entries(doc_id))
        if entry.event == checkpoint
    )
    destination = Path(archive_event.payload["path"])
    sidecar = Path(archive_event.payload["sidecar"])
    if damage == "file_mismatch":
        destination.write_bytes(b"unrelated document")
    elif damage == "missing_sidecar":
        sidecar.unlink()
    elif damage == "invalid_json":
        sidecar.write_text("{torn", encoding="utf-8")
    elif damage == "invalid_shape":
        sidecar.write_text("[]", encoding="utf-8")
    else:
        report = json.loads(sidecar.read_text())
        if damage == "wrong_report_status":
            report["status"] = "processing"
        else:
            report["doc_id"] = "another-document"
        sidecar.write_text(json.dumps(report), encoding="utf-8")

    manifest_path = bins.manifests / f"{doc_id}.json"
    before_manifest = manifest_path.read_bytes()
    before_audit = audit_log.entries(doc_id)
    before_catalog = catalog.get(doc_id)
    before_file = destination.read_bytes()
    before_sidecar = sidecar.read_bytes() if sidecar.exists() else None
    ledger = get_ledger()
    assert ledger.flush()
    before_ledger = ledger.entries(doc_id=doc_id, limit=100)

    for _attempt in range(2):
        manifest = load_manifest(bins, doc_id)
        before_state = manifest.model_dump(mode="json")
        assert flow_mod.reconcile_archived(bins, manifest) is False
        assert manifest.model_dump(mode="json") == before_state
        assert manifest_path.read_bytes() == before_manifest
        assert audit_log.entries(doc_id) == before_audit
        assert catalog.get(doc_id) == before_catalog
        assert destination.read_bytes() == before_file
        assert (sidecar.read_bytes() if sidecar.exists() else None) == before_sidecar
        assert ledger.flush()
        assert ledger.entries(doc_id=doc_id, limit=100) == before_ledger
    assert audit_log.verify_chain(audit_log.entries(doc_id)).ok
    assert ledger.verify().ok


def test_crash_after_sidecar_before_move_resumes_and_archives_once(
    env, mock_provider, monkeypatch
):
    """#111: crash after the write-ahead entry but before the rename keeps the source."""
    from mailroom_reloaded.pipeline import archivist

    bins, path = _run_and_get(env, mock_provider, monkeypatch)
    real_move = archivist._move

    def crash(src, dest):
        raise RuntimeError("crash before os.replace")

    monkeypatch.setattr(archivist, "_move", crash)
    with pytest.raises(RuntimeError):
        flow_mod.run_document(path, worker_id="w1")
    monkeypatch.setattr(archivist, "_move", real_move)

    doc_id = next(bins.manifests.glob("*.json")).stem
    assert Watcher(bins, "w2").resume_processing() == 1
    assert load_manifest(bins, doc_id).status == "archived"
    assert len(list((bins.archive / "correspondence").glob("*.txt"))) == 1
    assert audit_log.verify_chain(audit_log.entries(doc_id)).ok


def test_orphaned_claim_without_manifest_is_recovered(
    env, mock_provider, monkeypatch
):
    """#112: crash after the watcher claim, before the first manifest checkpoint."""
    bins, path = _run_and_get(env, mock_provider, monkeypatch)
    claimed = bins.claim(path, "dead-worker")
    assert claimed is not None and not list(bins.manifests.glob("*.json"))

    young = Watcher(bins, "w2")
    assert young.resume_processing() == 0  # inside the grace window: assumed live
    assert claimed.exists()

    watcher = Watcher(bins, "w2")
    watcher.orphan_grace_s = 0.0
    assert watcher.resume_processing() == 1
    assert not claimed.exists()
    doc_id = next(bins.manifests.glob("*.json")).stem
    assert load_manifest(bins, doc_id).status == "archived"
    assert audit_log.verify_chain(audit_log.entries(doc_id)).ok
    assert watcher.resume_processing() == 0  # idempotent: nothing left to recover


def test_flow_does_not_reclaim_an_already_claimed_file(env, mock_provider, monkeypatch):
    """#112: the flow keeps the watcher's claim path instead of renaming it again."""
    bins, path = _run_and_get(env, mock_provider, monkeypatch)
    claimed = bins.claim(path, "w1")
    seen: list[str] = []
    real_save = flow_mod.save_manifest

    def spy(b, m):
        seen.append(str(m.state.get("path")))
        return real_save(b, m)

    monkeypatch.setattr(flow_mod, "save_manifest", spy)
    flow_mod.run_document(claimed, worker_id="w1")
    assert seen[0] == str(claimed)


def test_orphan_with_live_manifest_owner_is_not_run_twice(
    env, mock_provider, monkeypatch
):
    """#112: a duplicate copy never re-runs a document a manifest already owns."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    bins, path = _write_inbox(env)
    twin = bins.inbox / "twin.txt"
    twin.write_bytes(path.read_bytes())

    def boom(*_a, **_k):
        raise RuntimeError("crash mid-document")

    monkeypatch.setattr(flow_mod, "_extract", boom)
    with pytest.raises(RuntimeError):
        flow_mod.run_document(path, worker_id="w1")
    twin_claimed = bins.claim(twin, "w9")

    monkeypatch.setattr(flow_mod, "_extract", _fake_extract(confidence=1.0))
    watcher = Watcher(bins, "w2")
    watcher.orphan_grace_s = 0.0
    assert watcher.resume_processing() == 1  # only the manifest-owned original
    assert twin_claimed.exists()
