"""Reset queue accounting and serialization of shared outbox mutations."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor


def test_reset_discards_pending_work_and_arrivals_while_waiting(
    idle_service, monkeypatch
):
    """Verify reset discards queued work, including arrivals during its idle wait."""
    svc = idle_service
    svc.inject(["A1_status_inquiry"], flows=["correspondent"])
    assert svc._q.unfinished_tasks == 1
    processed = []
    monkeypatch.setattr(svc, "_process", lambda *args: processed.append(args))
    monkeypatch.setattr(svc.pipeline, "reset", lambda: None)

    def wait_idle(timeout):
        """Simulate a new queue entry arriving after the initial reset drain."""
        assert svc._q.unfinished_tasks == 0
        # An injection races with reset before it acquires the locks.
        svc._q.put(("stale", []))
        return True

    monkeypatch.setattr(svc, "wait_idle", wait_idle)
    svc.reset()
    assert svc._q.empty() and svc._q.unfinished_tasks == 0
    assert svc.messages == {} and svc.events == []
    assert processed == []


def test_reset_lets_dequeued_worker_finish_before_clearing(idle_service, monkeypatch):
    """Verify reset lets an active worker finish before clearing shared state."""
    svc = idle_service
    svc.inject(["A1_status_inquiry"], flows=["correspondent"])
    dequeued = threading.Event()
    proceed = threading.Event()
    original_process = svc._process
    original_wait = svc.wait_idle

    def process(mid, flows):
        """Pause a dequeued message until reset permits processing, then stop the worker."""
        dequeued.set()
        assert proceed.wait(2)
        original_process(mid, flows)
        svc._stop.set()

    def wait_idle(timeout):
        """Release the worker and verify it can acquire the work lock during reset."""
        proceed.set()
        assert original_wait(2), "reset must let the worker acquire _work_lock"
        return True

    monkeypatch.setattr(svc, "_process", process)
    monkeypatch.setattr(svc, "wait_idle", wait_idle)
    monkeypatch.setattr(svc.pipeline, "reset", lambda: None)
    svc._worker = threading.Thread(target=svc._run_worker, daemon=True)
    svc._worker.start()
    try:
        assert dequeued.wait(2)
        svc.reset()
        assert svc.messages == {} and svc.outbox.items == {}
        assert svc._q.unfinished_tasks == 0
    finally:
        proceed.set()
        svc._stop.set()
        svc._worker.join(3)


def test_outbox_mutations_exclude_concurrent_state_access(idle_service, monkeypatch):
    """Verify outbox writes hold the service lock against a competing reader."""
    svc = idle_service
    called = []

    def competing_reader():
        """Try to acquire the state lock without blocking and report success."""
        acquired = svc._lock.acquire(blocking=False)
        if acquired:
            svc._lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as readers:

        def protect(name):
            """Wrap an outbox method to assert that its caller holds the state lock."""
            original = getattr(svc.outbox, name)

            def checked(*args, **kwargs):
                """Verify the competing reader is excluded before recording and delegating a call."""
                assert not readers.submit(competing_reader).result(timeout=2), name
                called.append(name)
                return original(*args, **kwargs)

            monkeypatch.setattr(svc.outbox, name, checked)

        for name in ("add_draft", "approve", "reject", "set_profile"):
            protect(name)
        svc.autonomy = "sandbox"
        result = svc.inject(["A1_status_inquiry"], flows=["correspondent"])
        assert svc.wait_idle()
        message = svc.message(result["message_ids"][0])
        assert message["state"] == "processed", message["error"]
        oid = message["outbox_ids"][0]
        svc.approve_outbound(oid)
        svc.reject_outbound(oid)
        svc.set_egress_profile("egress")
    assert called == ["add_draft", "approve", "approve", "reject", "set_profile"]
