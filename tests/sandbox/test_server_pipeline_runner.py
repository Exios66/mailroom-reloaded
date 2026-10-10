"""PipelineRunner lifecycle: a reset must not leave the ledger bound to the deleted database."""

from __future__ import annotations

import time

import pytest
from structlog.testing import capture_logs

from mailroom_reloaded.sandbox.server import pipeline_runner as pr_mod
from mailroom_reloaded.sandbox.server.pipeline_runner import PipelineRunner
from mailroom_reloaded.storage import ledger as ledger_mod
from mailroom_reloaded.storage.ledger import get_ledger, reset_ledger


def test_reset_flushes_queued_ledger_writes_before_deleting_the_database(tmp_path, monkeypatch):
    """Everything queued before reset() is committed, and the writer stopped, before the rmtree."""
    real_write = ledger_mod.Ledger._write

    def slow_write(self, batch):
        time.sleep(0.3)  # keep the batch in flight long enough for an early rmtree to win
        return real_write(self, batch)

    seen: dict[str, object] = {}
    real_rmtree = pr_mod.shutil.rmtree

    def spy_rmtree(path, *args, **kwargs):
        held = seen["held"]
        seen["drained"] = held.flush(timeout=0)  # True only when nothing is still queued
        seen["writer_alive"] = held._thread is not None and held._thread.is_alive()
        return real_rmtree(path, *args, **kwargs)

    runner = PipelineRunner(tmp_path / "data")
    runner.activate()
    try:
        doc = tmp_path / "note.txt"
        doc.write_text("Hello from the sandbox. Invoice total 10.00\n")
        runner.run(doc, "first.txt")
        seen["held"] = get_ledger()
        seen["held"].flush()
        monkeypatch.setattr(ledger_mod.Ledger, "_write", slow_write)
        monkeypatch.setattr(pr_mod.shutil, "rmtree", spy_rmtree)
        with capture_logs() as logs:
            seen["held"].append("run_opened", "r-reset-test")  # queued, not yet committed
            runner.reset()
            monkeypatch.setattr(ledger_mod.Ledger, "_write", real_write)
            result = runner.run(doc, "second.txt")
            assert "error" not in result, result
            get_ledger().flush()
        assert seen["drained"] is True, "reset() deleted the database with a write still queued"
        assert seen["writer_alive"] is False, "the old ledger's writer outlived the reset"
        assert get_ledger() is not seen["held"]
        assert not [e for e in logs if e.get("event") == "ledger_write_failed"]
    finally:
        monkeypatch.setattr(ledger_mod.Ledger, "_write", real_write)
        runner.deactivate()
        reset_ledger()


def test_reset_refuses_to_delete_the_database_while_the_ledger_writer_is_alive(tmp_path, monkeypatch):
    """If the writer cannot be stopped, reset() raises and leaves the database in place."""
    runner = PipelineRunner(tmp_path / "data")
    runner.activate()
    try:
        doc = tmp_path / "note.txt"
        doc.write_text("Hello from the sandbox. Invoice total 10.00\n")
        runner.run(doc, "first.txt")
        held = get_ledger()
        monkeypatch.setattr(ledger_mod.Ledger, "close", lambda self, timeout=10.0: False)
        db = runner.base / "mailroom.db"
        assert db.exists()
        with pytest.raises(RuntimeError, match="ledger writer did not stop"):
            runner.reset()
        assert db.exists()
        assert get_ledger() is held  # still bound; nothing was dropped under the live writer
    finally:
        monkeypatch.undo()
        runner.deactivate()
        reset_ledger()
