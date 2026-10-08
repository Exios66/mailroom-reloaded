"""Watcher lifecycle and review validation without running document agents."""

import hashlib
from unittest.mock import Mock

import pytest

from mailroom_reloaded import review, watcher
from mailroom_reloaded.pipeline.state import MailroomState
from mailroom_reloaded.schemas.manifest import Manifest
from mailroom_reloaded.storage.bins import Bins, load_manifest, save_manifest


@pytest.fixture
def bins(tmp_path):
    """Return filesystem bins rooted in the test's temporary directory."""
    return Bins(tmp_path)


@pytest.mark.parametrize(
    "concurrency,expected", [(-2, 1), (0, 1), (1, 1), (4, 4), (100, 32)]
)
def test_watcher_bounds_worker_count(bins, concurrency, expected):
    """Verify requested worker concurrency is clamped to the supported range."""
    assert watcher.Watcher(bins, "worker", concurrency).concurrency == expected


@pytest.mark.parametrize("concurrency", [1, 3])
def test_drain_skips_hidden_files_sidecars_and_directories(
    bins, monkeypatch, concurrency
):
    """Verify serial and concurrent draining select only processable document files."""
    for name in (".hidden", "letter.txt.meta", "letter.txt", "second.pdf"):
        (bins.inbox / name).write_text("synthetic source")
    (bins.inbox / "directory").mkdir()
    instance = watcher.Watcher(bins, "worker", concurrency)
    process = Mock(return_value=True)
    monkeypatch.setattr(instance, "_process", process)
    assert instance.drain_once() == 2
    assert {call.args[0].name for call in process.call_args_list} == {
        "letter.txt",
        "second.pdf",
    }


def test_lost_claim_does_not_run_pipeline(bins, monkeypatch):
    """Verify losing a file claim prevents pipeline execution."""
    instance = watcher.Watcher(bins, "worker")
    monkeypatch.setattr(bins, "claim", Mock(return_value=None))
    run = Mock(side_effect=AssertionError("loser must not process"))
    monkeypatch.setattr(watcher._flow, "run_document", run)
    assert instance._process(bins.inbox / "missing.txt") is False
    run.assert_not_called()


def test_claimed_document_crash_does_not_stop_drain(bins, monkeypatch):
    """Verify a processing exception does not prevent claiming the next document."""
    for name in ("a.txt", "b.txt"):
        (bins.inbox / name).write_text(name)
    run = Mock(
        side_effect=[RuntimeError("broken document"), MailroomState(status="archived")]
    )
    monkeypatch.setattr(watcher._flow, "run_document", run)
    instance = watcher.Watcher(bins, "worker")
    assert instance.drain_once() == 2
    assert run.call_count == 2
    assert not list(bins.inbox.iterdir())
    assert all(
        call.args[0].parent == bins.processing("worker") for call in run.call_args_list
    )


def test_resume_skips_corrupt_terminal_and_missing_documents(bins, monkeypatch):
    """Verify recovery attempts only existing processing files and runs once."""
    (bins.manifests / "corrupt.json").write_text("not JSON")
    for doc_id, status in [
        ("a", "processing"),
        ("b", "processing"),
        ("c", "parked"),
        ("d", "failed"),
        ("e", "archived"),
        ("missing", "processing"),
    ]:
        path = bins.processing("old") / f"{doc_id}.txt"
        if doc_id != "missing":
            path.write_text(doc_id)
        save_manifest(
            bins,
            Manifest(
                doc_id=doc_id,
                filename=path.name,
                content_sha256="hash",
                status=status,
                state={"path": str(path)},
            ),
        )
    run = Mock(
        side_effect=[RuntimeError("cannot resume a"), MailroomState(status="archived")]
    )
    monkeypatch.setattr(watcher._flow, "run_document", run)
    instance = watcher.Watcher(bins, "worker")
    assert instance.resume_processing() == 1
    assert instance.resumed == 1
    assert [call.args[0].name for call in run.call_args_list] == ["a.txt", "b.txt"]
    assert instance.drain_once() == 0
    assert run.call_count == 2  # startup recovery happens only once


@pytest.mark.parametrize("crashes", [False, True])
def test_watcher_releases_lock_and_observer_when_loop_ends(bins, monkeypatch, crashes):
    """Verify normal and exceptional loop exits stop the observer and release the lock."""
    instance = watcher.Watcher(bins, "worker")
    observer = Mock()
    monkeypatch.setattr(instance, "_start_observer", Mock(return_value=observer))

    def drain():
        if crashes:
            raise RuntimeError("drain failed")
        instance.stop()
        return 0

    monkeypatch.setattr(instance, "drain_once", drain)
    if crashes:
        with pytest.raises(RuntimeError, match="drain failed"):
            instance.run_forever(poll_interval=0)
    else:
        instance.run_forever(poll_interval=0)
    observer.stop.assert_called_once()
    observer.join.assert_called_once_with(timeout=5)
    assert instance._lock is None
    lock = watcher._acquire_watcher_lock(bins.base / watcher.WATCHER_LOCK_NAME)
    try:
        assert lock is not None
    finally:
        watcher._release_lock(lock)


def test_second_watcher_cannot_start_while_lock_is_held(bins, monkeypatch):
    """Verify an existing watcher lock prevents a second watcher's startup recovery."""
    lock = watcher._acquire_watcher_lock(bins.base / watcher.WATCHER_LOCK_NAME)
    assert lock is not None
    instance = watcher.Watcher(bins, "second")
    resume = Mock()
    monkeypatch.setattr(instance, "resume_processing", resume)
    try:
        with pytest.raises(watcher.WatcherLockHeld):
            instance.run_forever(poll_interval=0)
        resume.assert_not_called()
    finally:
        watcher._release_lock(lock)


@pytest.fixture
def parked(bins, monkeypatch):
    """Create a parked document and checkpoint with audit writes mocked."""
    path = bins.review / "letter.txt"
    path.write_text("synthetic letter")
    state = MailroomState(doc_id="doc", path=str(path), status="parked")
    manifest = Manifest(
        doc_id="doc",
        filename=path.name,
        content_sha256=hashlib.sha256(b"synthetic letter").hexdigest(),
        status="parked",
        state=state.model_dump(mode="json"),
    )
    save_manifest(bins, manifest)
    monkeypatch.setattr(review.audit_log, "append", Mock())
    return manifest, path


@pytest.mark.parametrize("action", ["", "accept", "delete"])
def test_review_rejects_unknown_action_before_reading_manifest(
    bins, monkeypatch, action
):
    """Verify unsupported review actions fail before reading document state."""
    load = Mock()
    monkeypatch.setattr(review, "load_manifest", load)
    with pytest.raises(ValueError, match="unknown review action"):
        review.resolve_review("doc", action, bins=bins)
    load.assert_not_called()


@pytest.mark.parametrize("status", [None, "processing", "archived", "failed"])
def test_review_nonparked_document_has_no_side_effects(bins, monkeypatch, status):
    """Verify missing or nonparked documents trigger neither processing nor auditing."""
    if status:
        save_manifest(
            bins,
            Manifest(
                doc_id="doc",
                filename="letter.txt",
                content_sha256="hash",
                status=status,
            ),
        )
    run = Mock()
    audit = Mock()
    monkeypatch.setattr(review._flow, "run_document", run)
    monkeypatch.setattr(review.audit_log, "append", audit)
    assert review.resolve_review("doc", "approve", bins=bins) is None
    run.assert_not_called()
    audit.assert_not_called()


@pytest.mark.parametrize(
    "action,corrections",
    [("approve", {}), ("correct", {"doc_type": "contract", "doc_subclass": "license"})],
)
def test_review_resumes_extraction_with_only_requested_corrections(
    bins, parked, monkeypatch, action, corrections
):
    """Verify approval preserves classification while correction forwards requested labels."""
    _, path = parked
    state = MailroomState(status="archived")
    run = Mock(return_value=state)
    monkeypatch.setattr(review._flow, "run_document", run)
    assert (
        review.resolve_review(
            "doc", action, "contract", "license", reviewer="alice", bins=bins
        )
        is state
    )
    run.assert_called_once_with(
        path,
        worker_id="review-alice",
        resume_from="extract",
        overrides={"bins": bins, **corrections},
    )
    review.audit_log.append.assert_called_once_with(
        "doc",
        "review",
        "review_resolved",
        {"action": action, "reviewer": "alice", **corrections},
    )


def test_review_missing_source_stays_parked_without_audit(bins, parked, monkeypatch):
    """Verify missing review sources leave the manifest parked without a success audit."""
    _, path = parked
    path.unlink()
    run = Mock()
    monkeypatch.setattr(review._flow, "run_document", run)
    assert review.resolve_review("doc", "approve", bins=bins) is None
    assert load_manifest(bins, "doc").status == "parked"
    run.assert_not_called()
    review.audit_log.append.assert_not_called()


def test_review_failed_resume_does_not_audit_success(bins, parked, monkeypatch):
    """Verify resume exceptions propagate without recording a resolved review."""
    monkeypatch.setattr(
        review._flow, "run_document", Mock(side_effect=RuntimeError("resume failed"))
    )
    with pytest.raises(RuntimeError, match="resume failed"):
        review.resolve_review("doc", "approve", bins=bins)
    review.audit_log.append.assert_not_called()


def test_reject_recovers_unreadable_state_and_stale_path(bins, parked):
    """Verify rejection locates the review file and rebuilds an invalid checkpoint."""
    manifest, path = parked
    manifest.state = {"path": "missing/source.txt", "classify_attempts": "invalid"}
    save_manifest(bins, manifest)
    state = review.resolve_review("doc", "reject", reviewer="alice", bins=bins)
    assert state.doc_id == "doc"
    assert state.status == "failed"
    assert not path.exists()
    assert len(list(bins.failed.glob("*.txt"))) == 1
    persisted = load_manifest(bins, "doc")
    assert persisted.status == "failed"
    assert persisted.state == state.model_dump(mode="json")
    review.audit_log.append.assert_called_once_with(
        "doc", "review", "review_resolved", {"action": "reject", "reviewer": "alice"}
    )


def test_drainer_does_not_recover_live_workers(bins, monkeypatch):
    instance = watcher.Watcher(bins, "drainer")
    resume = Mock()
    monkeypatch.setattr(instance, "resume_processing", resume)
    lock = watcher._acquire_watcher_lock(bins.base / watcher.WATCHER_LOCK_NAME)
    assert lock is not None
    try:
        assert instance.drain_once() == 0
        resume.assert_not_called()
        assert not instance._startup_done
        instance._lock = lock
        instance.drain_once()
        resume.assert_called_once()
    finally:
        instance._lock = None
        watcher._release_lock(lock)


@pytest.mark.parametrize("reviewer", ["../../escape", r"..\..\escape", "/", "..", "", "alice"])
def test_reviewer_cannot_escape_processing_directory(bins, parked, monkeypatch, reviewer):
    from pathlib import Path

    run = Mock(return_value=MailroomState(status="archived"))
    monkeypatch.setattr(review._flow, "run_document", run)
    monkeypatch.setattr(review.audit_log, "append", Mock())
    review.resolve_review("doc", "approve", reviewer=reviewer, bins=bins)
    worker = run.call_args.kwargs["worker_id"]
    assert worker.startswith("review-") and len(worker) > len("review-")
    assert "/" not in worker and "\\" not in worker and ".." not in worker
    assert bins.processing(worker).resolve().parent == (bins.base / "processing").resolve()
    assert Path(worker).name == worker


def test_locate_parked_ignores_other_documents_with_similar_names(bins):
    """A stale path must not resolve to another document's suffix-matching file."""
    body = b"mine"
    other = bins.review / f"{'d' * 32}_data.txt"
    other.write_bytes(b"someone else")
    manifest = Manifest(
        doc_id="doc", filename="data.txt",
        content_sha256=hashlib.sha256(body).hexdigest(), status="parked",
        state={"path": "gone/data.txt"},
    )
    assert review._locate_parked(bins, manifest) is None
    mine = bins.review / f"{'a' * 32}_data.txt"
    mine.write_bytes(body)
    assert review._locate_parked(bins, manifest) == mine


def test_locate_parked_escapes_glob_metacharacters(bins):
    body = b"x"
    manifest = Manifest(
        doc_id="doc", filename="a[1]*.txt",
        content_sha256=hashlib.sha256(body).hexdigest(), status="parked", state={},
    )
    decoy = bins.review / f"{'b' * 32}_a1zz.txt"
    decoy.write_bytes(body)
    assert review._locate_parked(bins, manifest) is None
