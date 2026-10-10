"""Flow B: run attachments through the real mailroom-reloaded pipeline, isolated.

The pipeline reads its base directory, SQLite DB and LLM endpoint from process
globals (``Settings``, ``storage.db``, ``MOCK_BASE_URL``). :meth:`activate`
points those at ``<data-dir>/pipeline`` and the in-process offline mock LLM, so
the sandbox has its own Bins and catalog and never touches ``./data``. One
active runner per process; :meth:`deactivate` restores the previous globals.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from mailroom_reloaded.sandbox.server.mock_llm import MockLLM

__all__ = ["PipelineRunner"]

_ENV_KEYS = (
    "MAILROOM_BASE_DIR",
    "MOCK_BASE_URL",
    "DEFAULT_PROVIDER",
    "MAILROOM_PROVIDER",
)


class PipelineRunner:
    def __init__(self, data_dir: Path, mock: MockLLM | None = None) -> None:
        """Configure isolated pipeline storage and an optional externally owned mock."""
        self.base = Path(data_dir) / "pipeline"
        self.mock = mock or MockLLM()
        self._owns_mock = mock is None
        self._saved: dict[str, str | None] = {}
        self._lock = threading.RLock()
        self.active = False

    # ------------------------------------------------------------------ lifecycle
    def _repoint(self) -> None:
        """Clear cached settings and dispose the default database engine."""
        from mailroom_reloaded import settings as settings_mod
        from mailroom_reloaded.storage import db
        from mailroom_reloaded.storage.ledger import reset_ledger

        reset_ledger()  # flush and drop the singleton still bound to the old engine
        settings_mod.get_settings.cache_clear()
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None

    def activate(self) -> PipelineRunner:
        """Point process globals at sandbox storage and the mock, saving prior values."""
        if self.active:
            return self
        if not self.mock.base_url:
            self.mock.start()
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        self.base.mkdir(parents=True, exist_ok=True)
        os.environ["MAILROOM_BASE_DIR"] = str(self.base)
        os.environ["MOCK_BASE_URL"] = self.mock.base_url
        os.environ["DEFAULT_PROVIDER"] = "mock"
        os.environ.pop("MAILROOM_PROVIDER", None)
        self._repoint()
        self.active = True
        return self

    def deactivate(self) -> None:
        """Restore saved globals and stop the mock when this runner owns it."""
        if not self.active:
            return
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._repoint()
        if self._owns_mock:
            self.mock.stop()
        self.active = False

    def reset(self) -> None:
        """Delete the sandbox pipeline state (bins, manifests, SQLite) and re-init."""
        with self._lock:
            self._repoint()
            shutil.rmtree(self.base, ignore_errors=True)
            self.base.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ run
    def run(self, src: Path, name: str) -> dict[str, Any]:
        """Hand ``src`` to the pipeline as ``name``; idempotent on content hash."""
        from mailroom_reloaded.pipeline.flow import run_document
        from mailroom_reloaded.storage import audit_log, catalog
        from mailroom_reloaded.storage.bins import Bins, doc_id_for, load_manifest

        if not self.active:
            raise RuntimeError("PipelineRunner is not active")
        with self._lock:
            bins = Bins(self.base)
            doc_id = doc_id_for(src)
            existing = load_manifest(bins, doc_id)
            if existing is not None and existing.status in {
                "archived",
                "parked",
                "failed",
            }:
                return self._result(doc_id, name, reused=True, calls=0, seconds=0.0)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "document"
            dest = bins.inbox / safe
            n = 0
            while dest.exists():
                n += 1
                dest = bins.inbox / f"{Path(safe).stem}-{n}{Path(safe).suffix}"
            shutil.copyfile(src, dest)
            before = self.mock.structured_calls()
            t0 = time.monotonic()
            error = None
            try:
                run_document(dest, worker_id="sandbox")
            except Exception as exc:  # noqa: BLE001 - surfaced in the result, node crash is data here
                error = f"{type(exc).__name__}: {exc}"
            seconds = round(time.monotonic() - t0, 3)
            res = self._result(
                doc_id,
                name,
                reused=False,
                calls=self.mock.structured_calls() - before,
                seconds=seconds,
            )
            if error:
                res["error"] = error
            _ = (audit_log, catalog)
            return res

    def _result(
        self, doc_id: str, name: str, *, reused: bool, calls: int, seconds: float
    ) -> dict[str, Any]:
        """Summarize persisted document state, audit integrity, and run measurements."""
        from mailroom_reloaded.storage import audit_log, catalog
        from mailroom_reloaded.storage.bins import Bins, load_manifest

        manifest = load_manifest(Bins(self.base), doc_id)
        rec = catalog.get(doc_id)
        entries = audit_log.entries(doc_id)
        chain = audit_log.verify_chain(entries)
        state = (manifest.state if manifest is not None else {}) or {}
        sort = state.get("sort") or {}
        report = state.get("report")
        return {
            "doc_id": doc_id,
            "name": name,
            "status": manifest.status if manifest is not None else "unknown",
            "reused": reused,
            "route_trail": state.get("route_trail", []),
            "doc_type": (rec.doc_type if rec else None) or sort.get("doc_type"),
            "doc_subclass": (rec.doc_subclass if rec else None)
            or sort.get("doc_subclass"),
            "confidence": sort.get("confidence"),
            "catalog_status": rec.status if rec else None,
            "audit_entries": len(entries),
            "audit_chain_ok": bool(chain.ok),
            "structured_llm_calls": calls,
            "seconds": seconds,
            "text": (state.get("text") or "")[:4000],
            "has_report": bool(report),
            "audit_url": f"/api/sandbox/v1/documents/{doc_id}/audit",
            "document_url": f"/api/sandbox/v1/documents/{doc_id}",
        }

    # ------------------------------------------------------------------ read side
    def document(self, doc_id: str) -> dict | None:
        """Return manifest, report, and catalog details, or None for an unknown document."""
        from mailroom_reloaded.storage import catalog
        from mailroom_reloaded.storage.bins import Bins, load_manifest

        manifest = load_manifest(Bins(self.base), doc_id)
        rec = catalog.get(doc_id)
        if manifest is None and rec is None:
            return None
        state = (manifest.state if manifest is not None else {}) or {}
        return {
            "doc_id": doc_id,
            "status": manifest.status if manifest else rec.status,
            "manifest": {
                "filename": manifest.filename,
                "completed_nodes": manifest.completed_nodes,
            }
            if manifest
            else None,
            "report": state.get("report"),
            "catalog": rec.model_dump(mode="json") if rec else None,
        }

    def audit(self, doc_id: str) -> dict:
        """Return audit entries and hash-chain verification for a document."""
        from mailroom_reloaded.storage import audit_log

        entries = audit_log.entries(doc_id)
        chain = audit_log.verify_chain(entries)
        return {
            "doc_id": doc_id,
            "entries": [e.model_dump(mode="json") for e in entries],
            "chain": chain.model_dump(),
        }

    def stuck_documents(self) -> list[str]:
        """List documents whose manifests are unreadable or still marked processing."""
        from mailroom_reloaded.storage.bins import Bins

        mdir = Bins(self.base).manifests
        out = []
        from mailroom_reloaded.schemas.manifest import Manifest

        for p in mdir.glob("*.json"):
            try:
                if Manifest.model_validate_json(p.read_text()).status == "processing":
                    out.append(p.stem)
            except Exception:  # noqa: BLE001
                out.append(p.stem)
        return out
