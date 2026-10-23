"""Filesystem watcher: drain ``inbox/`` into the pipeline, one claim per file.

Spec section 4 (durability) and section 9 (split-watcher profile):

* A worker claims a file by atomic rename (``Bins.claim``); the loser of a race
  sees ``None`` and moves on.
* A ``watcher.lock`` ``flock`` keeps a process-local observer and a second
  concurrency boundary from both draining the inbox.
* On startup every manifest still in status ``processing`` is resumed from its
  last completed node, without duplicating audit entries (the audit log dedupes
  per ``(doc_id, node, event)`` and the manifest records the completed prefix).
* Unreadable / empty documents are moved to ``failed/`` by the flow and the
  watcher keeps draining the rest.

The loop is deliberately small: ``watchdog`` wakes the poller on inbox events,
and a fixed poll interval re-drains as a fallback so a missed inotify event
cannot strand a document.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import structlog

from mailroom_reloaded.obs.metrics import M
from mailroom_reloaded.obs.run_context import ensure_run_scope, live_run_id
from mailroom_reloaded.pipeline import flow as _flow
from mailroom_reloaded.pipeline import run_ledger as _run_ledger
from mailroom_reloaded.schemas.manifest import Manifest
from mailroom_reloaded.storage.bins import Bins, doc_id_for, load_manifest

__all__ = ["Watcher", "WatcherLockHeld"]

logger = structlog.get_logger(__name__)

WATCHER_LOCK_NAME = "watcher.lock"
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
_MAX_DRAIN_WORKERS = 32


class WatcherLockHeld(RuntimeError):
    """Another process already holds ``watcher.lock``."""


#: Upload staging files older than this are abandoned by a crashed upload.
STAGING_STALE_AFTER_S = 3600
#: a claimed file younger than this is assumed live (``rename`` refreshes ctime), not orphaned
ORPHAN_GRACE_S = 30.0
#: minimum spacing between orphan sweeps in the drain loop
ORPHAN_SWEEP_INTERVAL_S = 60.0


def _is_processable(path: Path) -> bool:
    """Skip hidden files and intake sidecars; only documents are drained."""
    name = path.name
    if name.startswith(".") or name.endswith(".meta"):
        return False
    return path.is_file()


def _acquire_watcher_lock(path: Path):
    """Non-blocking exclusive ``flock`` on ``path``; ``None`` when held elsewhere."""
    try:
        import fcntl
    except ImportError:  # non-Unix: no locking available, so do not claim to have lost
        fcntl = None
        logger.warning("watcher_lock_unavailable", path=str(path))

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fh = open(path, "a+")  # noqa: SIM115 - the fd must outlive this function
    except OSError:
        logger.warning("watcher_lock_open_failed", path=str(path), exc_info=True)
        return None
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    except OSError:
        fh.close()
        return None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    return fh


def _release_lock(fh: Any) -> None:
    """Attempt to unlock and close the handle, logging cleanup failures."""
    if fh is None:
        return
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        logger.debug("watcher_unlock_failed", exc_info=True)
    try:
        fh.close()
    except Exception:
        logger.debug("watcher_lock_close_failed", exc_info=True)


class Watcher:
    """Drain ``inbox/`` with a bounded worker pool.

    ``concurrency`` bounds how many documents run at once in a single
    ``drain_once``. A separate process (the ``split-watcher`` profile) is kept
    out by ``watcher.lock``.
    """

    def __init__(self, bins: Bins, worker_id: str, concurrency: int = 1) -> None:
        """Configure bins and worker limits and initialize lifecycle state."""
        self.bins = bins if isinstance(bins, Bins) else Bins(bins)
        self.worker_id = worker_id
        self.concurrency = max(1, min(int(concurrency), _MAX_DRAIN_WORKERS))
        self.resumed = 0
        self._startup_done = False
        self._stop = threading.Event()
        self._lock = None
        #: set once this watcher holds the lock and is running its loop
        self.ready = threading.Event()
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self.orphan_grace_s = ORPHAN_GRACE_S
        self._active: set[str] = set()
        self._last_orphan_sweep = 0.0

    def _bump_inflight(self, delta: int) -> None:
        """Adjust the in-flight count and publish it, atomically."""
        with self._inflight_lock:
            self._inflight += delta
            with ensure_run_scope():  # the gauge belongs to the live run bucket, not "unscoped"
                M.inflight.set(self._inflight, {"worker": self.worker_id})

    # ------------------------------------------------------------- startup
    def _sweep_stale_staging(self) -> int:
        """Remove hidden upload staging files abandoned by a crash; return the count.

        Only files older than ``STAGING_STALE_AFTER_S`` are removed, so an upload
        still being written by a live API process is never touched.
        """
        cutoff = time.time() - STAGING_STALE_AFTER_S
        removed = 0
        for path in self.bins.inbox.glob(".upload-*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                logger.warning("staging_sweep_failed", path=str(path))
        return removed

    def resume_processing(self) -> int:
        """Recover processing manifests and orphaned claims after a crash.

        The flow reads the manifest's ``completed_nodes`` and restarts at the
        first unfinished node. Missing sources are checked for completed archives;
        pending catalog updates and abandoned upload staging files are also handled.
        Return the number of flow calls that finished without raising, including
        orphan recovery but excluding archive/catalog reconciliation, and add it
        to ``self.resumed``. Unreadable manifests and flow failures are skipped.
        """
        count = 0
        handled: set[str] = set()
        self._close_stale_ledger_runs()
        self._sweep_stale_staging()
        for manifest_path in sorted(self.bins.manifests.glob("*.json")):
            try:
                manifest = Manifest.model_validate_json(manifest_path.read_text())
            except Exception:  # noqa: BLE001 - a torn manifest must not stop startup
                logger.warning("manifest_unreadable", path=str(manifest_path))
                continue
            if manifest.status == "archived" and manifest.catalog_pending:
                _flow.reconcile_catalog(self.bins, manifest)
                continue
            if manifest.status != "processing":
                continue
            path = self._manifest_file(manifest)
            if path is not None:
                handled.add(str(path))
            if path is None:
                logger.warning("resume_source_missing", doc_id=manifest.doc_id)
                try:
                    reconciled = _flow.reconcile_archived(self.bins, manifest)
                except Exception:
                    logger.exception("reconcile_failed", doc_id=manifest.doc_id)
                    reconciled = False
                if reconciled:
                    logger.info("reconciled_archived_manifest", doc_id=manifest.doc_id)
                continue
            try:
                _flow.run_document(path, worker_id=self.worker_id)
            except Exception:
                logger.exception("resume_failed", doc_id=manifest.doc_id)
                continue
            count += 1
            logger.info("resumed_processing_manifest", doc_id=manifest.doc_id)
        count += self._recover_orphans(handled)
        self.resumed += count
        self._startup_done = True
        return count

    def _recover_orphans(self, handled: set[str] | None = None) -> int:
        """Run claimed files that never reached a manifest checkpoint; return the count.

        A crash after the watcher's ``claim`` and before the flow's first manifest
        save leaves a file under ``processing/`` that no manifest names, so the
        manifest scan cannot see it. Files younger than ``orphan_grace_s``, files
        this watcher is running, and ones a live manifest already owns are skipped.
        The grace period is in seconds since the file's ctime; ``handled`` contains
        path strings to skip from the manifest scan. Stat, hashing, manifest loading,
        and flow errors are skipped per file; only normal flow returns are counted.
        """
        self._last_orphan_sweep = time.monotonic()
        handled = handled or set()
        recovered = 0
        for path in self.bins.processing_files():
            key = str(path)
            if not _is_processable(path) or key in handled or key in self._active:
                continue
            try:
                if time.time() - path.stat().st_ctime < self.orphan_grace_s:
                    continue
                owner = load_manifest(self.bins, doc_id_for(path))
            except Exception:  # one bad file must not stop startup
                logger.warning("orphan_inspect_failed", path=key, exc_info=True)
                continue
            if owner is not None and owner.status == "processing":
                current = self._manifest_file(owner)
                if current is not None:
                    logger.warning(
                        "orphan_duplicate_skipped", path=key, doc_id=owner.doc_id
                    )
                    continue
            try:
                _flow.run_document(path, worker_id=self.worker_id)
            except Exception:
                logger.exception("orphan_recovery_failed", path=key)
                continue
            recovered += 1
            logger.info("recovered_orphan_claim", path=key)
        return recovered

    @staticmethod
    def _close_stale_ledger_runs() -> None:
        """Close live ledger runs a dead process left open (never today's bucket)."""
        try:
            ledger = _run_ledger.ledger_for(None)
            _run_ledger.close_other_live_runs(ledger, live_run_id())
        except Exception:
            logger.warning("ledger_closeout_failed", exc_info=True)

    @staticmethod
    def _manifest_file(manifest: Manifest) -> Path | None:
        """Return the checkpoint's source path when it still names a file."""
        state = manifest.state or {}
        candidate = state.get("path")
        if candidate:
            path = Path(candidate)
            if path.is_file():
                return path
        return None

    # ------------------------------------------------------------- draining
    def drain_once(self) -> int:
        """Process the inbox once; return the number claimed, even if a flow fails.

        While holding the watcher lock, run startup recovery once and sweep orphan
        claims at most once every 60 seconds. Recovery counts update ``self.resumed``
        separately from the returned inbox count.
        """
        if not self._startup_done and self._lock is not None:
            self.resume_processing()
        if (
            self._lock is not None
            and time.monotonic() - self._last_orphan_sweep >= ORPHAN_SWEEP_INTERVAL_S
        ):
            self.resumed += self._recover_orphans()
        files = [f for f in sorted(self.bins.inbox.iterdir()) if _is_processable(f)]
        with ensure_run_scope():
            M.queue_depth.set(len(files), {"bin": "inbox"})
        if not files:
            return 0
        if self.concurrency <= 1:
            return sum(1 for f in files if self._process(f))
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            # each worker gets its own copy of the caller's context so run_scope survives the hop
            futures = [
                pool.submit(contextvars.copy_context().run, self._process, f)
                for f in files
            ]
            results = [fut.result() for fut in futures]
        return sum(1 for r in results if r)

    def _process(self, path: Path) -> bool:
        """Claim and process a file; return whether claimed, even if processing fails."""
        claimed = self.bins.claim(path, self.worker_id)
        if claimed is None:
            logger.debug("claim_lost", file=str(path), worker_id=self.worker_id)
            return False
        self._bump_inflight(1)
        self._active.add(str(claimed))
        try:
            _flow.run_document(claimed, worker_id=self.worker_id)
            return True
        except Exception:
            logger.exception("watcher_document_crashed", file=str(claimed))
            return True
        finally:
            self._active.discard(str(claimed))
            self._bump_inflight(-1)

    # ------------------------------------------------------------- loop
    def run_forever(self, poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS) -> None:
        """Block, draining the inbox until :meth:`stop` is called."""
        lock = _acquire_watcher_lock(self.bins.base / WATCHER_LOCK_NAME)
        if lock is None:
            raise WatcherLockHeld(
                f"another process holds {WATCHER_LOCK_NAME} — "
                "the API-embedded watcher or the split-watcher is already draining"
            )
        self._lock = lock
        self._stop.clear()
        self.ready.set()
        event = threading.Event()
        observer = None
        try:
            self.resume_processing()
            observer = self._start_observer(event)
            logger.info(
                "watcher_running", inbox=str(self.bins.inbox), worker_id=self.worker_id
            )
            while not self._stop.is_set():
                self.drain_once()
                event.wait(poll_interval)
                event.clear()
        finally:
            self.ready.clear()
            if observer is not None:
                try:
                    observer.stop()
                    observer.join(timeout=5)
                except Exception:
                    logger.warning("observer_stop_failed", exc_info=True)
            _release_lock(self._lock)
            self._lock = None

    def _start_observer(self, event: threading.Event):
        """Watch the inbox; a created/modified/moved file wakes the drain loop."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception:
            logger.warning("watchdog_unavailable", exc_info=True)
            return None

        class _Handler(FileSystemEventHandler):
            def on_created(self, e):
                """Wake the drain loop when a file is created."""
                if not e.is_directory:
                    event.set()

            def on_modified(self, e):
                """Wake the drain loop when a file is modified."""
                if not e.is_directory:
                    event.set()

            def on_moved(self, e):
                """Wake the drain loop when a file is moved."""
                if not e.is_directory:
                    event.set()

        try:
            observer = Observer()
            observer.schedule(_Handler(), str(self.bins.inbox), recursive=False)
            observer.start()
            return observer
        except Exception:
            logger.warning("watchdog_start_failed", exc_info=True)
            return None

    def stop(self) -> None:
        """Ask :meth:`run_forever` to return after the current drain."""
        self._stop.set()
