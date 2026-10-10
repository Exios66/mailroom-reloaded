"""Human-review resolution (spec section 4).

``human_review`` parks a document in ``review/`` with manifest status
``parked``. An operator then resolves it:

* ``approve`` — re-run from ``extract`` with the classification already on the
  manifest (no sorter call).
* ``correct`` — re-run from ``extract`` with a corrected ``doc_type`` /
  ``doc_subclass`` (no sorter call).
* ``reject`` — move the document to ``failed/``.

Every disposition appends a hash-chained ``review_resolved`` audit entry. The
resume uses :func:`mailroom_reloaded.pipeline.flow.run_document` with
``resume_from="extract"`` and the correction carried as overrides, so the flow's
manifest checkpointing and audit dedupe apply unchanged.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Literal

import structlog

from mailroom_reloaded.pipeline import flow as _flow
from mailroom_reloaded.pipeline.state import MailroomState
from mailroom_reloaded.schemas.audit import CatalogRecord
from mailroom_reloaded.schemas.manifest import Manifest
from mailroom_reloaded.settings import get_settings, load_taxonomy
from mailroom_reloaded.storage import audit_log, catalog
from mailroom_reloaded.storage.bins import Bins, load_manifest, save_manifest

logger = structlog.get_logger(__name__)

__all__ = ["ReviewRequestError", "resolve_review"]


class ReviewRequestError(ValueError):
    """A review request the caller can correct: an unknown action or an invalid class."""

ReviewAction = Literal["approve", "correct", "reject"]


def resolve_review(
    doc_id: str,
    action: ReviewAction,
    doc_type: str | None = None,
    doc_subclass: str | None = None,
    reviewer: str = "reviewer",
    *,
    bins: Bins | None = None,
) -> MailroomState | None:
    """Resolve a parked document; ``None`` when no parked manifest matches."""
    if action not in ("approve", "correct", "reject"):
        raise ReviewRequestError(f"unknown review action: {action!r}")
    if action == "correct":
        taxonomy = load_taxonomy()
        if doc_type not in taxonomy.classes:
            raise ReviewRequestError("correct requires a valid doc_type")
        # Validate doc_subclass if provided (must be non-empty string, no validation table exists)
        if doc_subclass is not None and not isinstance(doc_subclass, str):
            raise ReviewRequestError(f"doc_subclass must be a string, not {type(doc_subclass).__name__}")
        if doc_subclass is not None and not doc_subclass.strip():
            raise ReviewRequestError("doc_subclass cannot be empty or whitespace-only")

    bins = bins if bins is not None else Bins(get_settings().base_dir)
    manifest = load_manifest(bins, doc_id)
    if manifest is None or manifest.status != "parked":
        return None
    path = _locate_parked(bins, manifest)
    if path is None:
        logger.warning("review_source_missing", doc_id=doc_id)
        return None

    payload: dict[str, Any] = {"action": action, "reviewer": reviewer}
    if action == "correct":
        if doc_type:
            payload["doc_type"] = doc_type
        if doc_subclass:
            payload["doc_subclass"] = doc_subclass

    worker_reviewer = re.sub(r"[^a-zA-Z0-9_-]+", "-", reviewer).strip("-") or "reviewer"
    worker_id = f"review-{worker_reviewer}"
    # Claim the parked file by atomic rename so only one concurrent resolver wins.
    claimed = bins.claim(path, worker_id)
    if claimed is None:
        logger.info("review_claim_lost", doc_id=doc_id)
        return None

    try:
        return _resolve_claimed(
            bins, manifest, claimed, action, payload, doc_type, doc_subclass,
            reviewer, worker_id,
        )
    except BaseException:
        # Keep the doc parked: put the file back and restore the parked manifest,
        # also when the flow had re-claimed the file and died mid-run.
        try:
            if claimed.is_file():
                os.replace(claimed, path)
                save_manifest(bins, manifest)
            else:
                _reopen_after_flow_failure(bins, manifest, worker_id, path)
        except Exception:  # never mask the flow's own error
            logger.exception("review_restore_failed", doc_id=doc_id)
        raise


def _reopen_after_flow_failure(
    bins: Bins, manifest: Manifest, worker_id: str, parked_path: Path
) -> bool:
    """Put a document back in ``review/`` when the resumed flow died mid-run.

    The flow re-claims the file into ``processing/<worker_id>/`` and flips the
    manifest to ``processing``; if it then raises, a retried resolve would find no
    parked manifest and 404. When the on-disk manifest is still ``processing`` and
    the content-verified file sits in the worker directory, move it back to
    ``parked_path`` and rewrite ``manifest`` (the prior parked snapshot, which
    carries no run error). Return whether the document was reopened; a manifest
    the flow already finished (archived, failed, parked again) is left alone.
    """
    current = load_manifest(bins, manifest.doc_id)
    if current is None or current.status != "processing":
        return False
    for candidate in sorted(bins.processing(worker_id).glob(f"*_{glob.escape(manifest.filename)}")):
        try:
            if candidate.is_file() and _sha256(candidate) == manifest.content_sha256:
                os.replace(candidate, parked_path)
                save_manifest(bins, manifest)
                return True
        except OSError:
            continue
    return False


def _resolve_claimed(
    bins: Bins,
    manifest: Manifest,
    path: Path,
    action: ReviewAction,
    payload: dict[str, Any],
    doc_type: str | None,
    doc_subclass: str | None,
    reviewer: str,
    worker_id: str,
) -> MailroomState:
    doc_id = manifest.doc_id
    if action == "reject":
        dest = bins.move(path, "failed")
        state = _restore_state(manifest)
        state.status = "failed"
        state.path = str(dest)
        manifest.status = "failed"
        manifest.state = state.model_dump(mode="json")
        save_manifest(bins, manifest)
        audit_log.append(doc_id, "review", "review_resolved", payload)
        try:
            catalog.upsert(
                CatalogRecord(
                    doc_id=doc_id,
                    filename=manifest.filename,
                    doc_type=state.sort.doc_type if state.sort else None,
                    doc_subclass=state.sort.doc_subclass if state.sort else None,
                    status="failed",
                )
            )
        except Exception as exc:  # noqa: BLE001 - catalog is best-effort durability
            logger.warning("catalog_upsert_failed", doc_id=doc_id, error=str(exc))
        logger.info("review_rejected", doc_id=doc_id, reviewer=reviewer)
        return state

    overrides: dict[str, Any] = {"bins": bins}
    if action == "correct":
        if doc_type:
            overrides["doc_type"] = doc_type
        if doc_subclass:
            overrides["doc_subclass"] = doc_subclass

    state = _flow.run_document(
        path,
        worker_id=worker_id,
        resume_from="extract",
        overrides=overrides,
    )
    audit_log.append(doc_id, "review", "review_resolved", payload)
    logger.info(
        "review_resumed",
        doc_id=doc_id,
        action=action,
        reviewer=reviewer,
        status=state.status,
    )
    return state


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _locate_parked(bins: Bins, manifest: Manifest) -> Path | None:
    """Find the parked source: the saved path, else a content-verified match.

    The saved ``state.path`` is trusted. When it is stale, review-bin files named
    ``<uuid>_<filename>`` are candidates, but only one whose sha256 equals the
    manifest's ``content_sha256`` is accepted, so another document with the same
    or a similar name is never picked up.
    """
    state_path = (manifest.state or {}).get("path")
    if state_path:
        try:
            if Path(state_path).is_file():
                return Path(state_path)
        except OSError:
            pass
    if not manifest.filename:
        return None
    candidates = [bins.review / manifest.filename]
    candidates.extend(
        sorted(bins.review.glob(f"{'[0-9a-f]' * 32}_{glob.escape(manifest.filename)}"))
    )
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            # Verify file exists before hashing, recheck after (race-safe)
            digest = _sha256(candidate)
            if candidate.is_file() and digest == manifest.content_sha256:
                return candidate
        except FileNotFoundError:
            # File was deleted between is_file() and _sha256() or after; skip
            continue
        except OSError:
            # Other I/O errors (permission, etc.); skip this candidate
            continue
    return None


def _restore_state(manifest: Manifest) -> MailroomState:
    """Restore checkpointed state, falling back to the manifest identity."""
    if manifest.state:
        try:
            return MailroomState.model_validate(manifest.state)
        except Exception:  # noqa: BLE001 - fall back to a minimal state
            logger.warning("review_state_unreadable", doc_id=manifest.doc_id)
    return MailroomState(doc_id=manifest.doc_id, path=manifest.filename)
