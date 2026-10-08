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
import re
from pathlib import Path
from typing import Any, Literal

import structlog

from mailroom_reloaded.pipeline import flow as _flow
from mailroom_reloaded.pipeline.state import MailroomState
from mailroom_reloaded.schemas.manifest import Manifest
from mailroom_reloaded.settings import get_settings
from mailroom_reloaded.storage import audit_log
from mailroom_reloaded.storage.bins import Bins, load_manifest, save_manifest

logger = structlog.get_logger(__name__)

__all__ = ["resolve_review"]

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
        raise ValueError(f"unknown review action: {action!r}")

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

    if action == "reject":
        dest = bins.move(path, "failed")
        state = _restore_state(manifest)
        state.status = "failed"
        state.path = str(dest)
        manifest.status = "failed"
        manifest.state = state.model_dump(mode="json")
        save_manifest(bins, manifest)
        audit_log.append(doc_id, "review", "review_resolved", payload)
        logger.info("review_rejected", doc_id=doc_id, reviewer=reviewer)
        return state

    overrides: dict[str, Any] = {"bins": bins}
    if action == "correct":
        if doc_type:
            overrides["doc_type"] = doc_type
        if doc_subclass:
            overrides["doc_subclass"] = doc_subclass

    worker_reviewer = re.sub(r"[^a-zA-Z0-9_-]+", "-", reviewer).strip("-") or "reviewer"
    state = _flow.run_document(
        path,
        worker_id=f"review-{worker_reviewer}",
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
            if candidate.is_file() and _sha256(candidate) == manifest.content_sha256:
                return candidate
        except OSError:
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
