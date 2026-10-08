"""The mailroom HTTP API: ``/v1`` endpoints, ``/health`` and the ``/ui`` page.

Task 19 (plan section "FastAPI /v1 and the localhost runs UI"). The API is a
thin, read-mostly surface over the filesystem bins, the JSON manifests, the
SQLite catalog and the hash-chained audit log:

* ``GET /health`` — liveness.
* ``POST /v1/documents`` (multipart) — write an upload to ``inbox/``; returns
  the content-addressed ``doc_id``.
* ``GET /v1/documents`` / ``GET /v1/documents/{id}`` — catalog listing and one
  document's manifest + report.
* ``GET /v1/audit/{id}`` — the document's audit entries and chain verification.
* ``POST /v1/review/{id}/resolve`` — disposition a parked document.
* ``GET /v1/runs`` / ``GET /v1/runs/{run_id}/cards`` — eval runs (SQLite) and
  card JSONs (empty until Task 21 lands).
* ``GET /ui`` — a vanilla-JS page, no build step.

Security (spec section 9): every ``/v1`` route requires the configured bearer
token when ``MAILROOM_API_TOKEN`` is set; binding off loopback without a token
refuses to start (:func:`assert_bind_allowed`). ``/health`` and ``/ui`` stay
public so a load balancer and a browser can reach them.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text

from mailroom_reloaded.intake import gmail as gmail_intake
from mailroom_reloaded.review import resolve_review
from mailroom_reloaded.settings import get_settings
from mailroom_reloaded.storage import audit_log, catalog
from mailroom_reloaded.storage.bins import Bins, doc_id_for, load_manifest

logger = structlog.get_logger(__name__)

__all__ = ["api", "app", "assert_bind_allowed", "create_app"]

#: Upload guardrail: 50 MB max, matching the reference API (audit L-18).
MAX_UPLOAD_BYTES = int(
    os.environ.get("MAILROOM_MAX_UPLOAD_BYTES") or 50 * 1024 * 1024
)

#: Accepted upload extensions. Kept local so the API does not depend on the
#: taxonomy's current ``file_extensions`` block.
_ACCEPTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".rtf", ".html", ".htm"}

#: Hosts considered loopback; anything else is an off-loopback bind.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_UI_INDEX = Path(__file__).parent / "ui" / "index.html"


# --------------------------------------------------------------------------- config


def _bins() -> Bins:
    """Return filesystem bins rooted at the configured base directory."""
    return Bins(get_settings().base_dir)


def _configured_token() -> str:
    """Return the stripped API token, or an empty string when unset."""
    return (get_settings().api_token or "").strip()


def require_token(request: Request) -> None:
    """FastAPI dependency: enforce the bearer token when one is configured.

    With no token the API is loopback-unauthenticated (the bind guard refuses to
    expose it off loopback); with a token every ``/v1`` request must present it.
    """
    token = _configured_token()
    if not token:
        return
    auth = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix) or not hmac.compare_digest(
        auth[len(prefix) :].strip().encode("utf-8"), token.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Missing or invalid API token")


def assert_bind_allowed(host: str, token: str | None = None) -> None:
    """Refuse an off-loopback bind without a token (spec section 9).

    Raises :class:`SystemExit` when ``host`` is not loopback and no bearer token
    is configured (spec: "Off-loopback API bind refuses to start without
    ``MAILROOM_API_TOKEN``").
    """
    if host in _LOOPBACK_HOSTS:
        return
    resolved = token if token is not None else _configured_token()
    if resolved:
        return
    raise SystemExit(
        f"Refusing to bind to {host!r} without MAILROOM_API_TOKEN — an "
        "unauthenticated API must never be exposed off loopback."
    )


# --------------------------------------------------------------------------- schemas


class ReviewResolve(BaseModel):
    action: Literal["approve", "correct", "reject"]
    doc_type: str | None = None
    doc_subclass: str | None = None
    reviewer: str = "reviewer"


# --------------------------------------------------------------------------- routes


api = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


@api.post("/documents", status_code=202)
async def upload_document(file: UploadFile = File(...)) -> dict:  # noqa: B008
    """Write an upload to ``inbox/`` and return its content-addressed ``doc_id``."""
    filename = Path((file.filename or "").replace("\\", "/")).name
    if not filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid file name")
    suffix = Path(filename).suffix.lower()
    if suffix not in _ACCEPTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type {suffix!r}; accepted: "
            + ", ".join(sorted(_ACCEPTED_EXTENSIONS)),
        )

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the upload limit")
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    inbox = _bins().inbox
    stem = Path(filename).stem
    dest = inbox / filename
    counter = 0
    # Hard-link/concurrent-safe: an exclusive create avoids clobbering a
    # same-named document already queued (the watcher keys claims by name).
    while True:
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            counter += 1
            dest = inbox / f"{stem}-{counter}{suffix}"
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)

    doc_id = doc_id_for(dest)
    logger.info("document_uploaded", doc_id=doc_id, file=dest.name, size=len(content))
    return {"doc_id": doc_id, "file": dest.name, "status": "accepted"}


@api.get("/documents")
def list_documents_endpoint(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """List catalogued documents, newest first is not tracked yet — by ``doc_id``."""
    records = catalog.list(limit=limit, offset=offset, status=status)
    return {
        "documents": [r.model_dump(mode="json") for r in records],
        "count": len(records),
    }


@api.get("/documents/{doc_id}")
def get_document_endpoint(doc_id: str) -> dict:
    """One document's manifest and compiled report."""
    manifest = load_manifest(_bins(), doc_id)
    record = catalog.get(doc_id)
    if manifest is None and record is None:
        raise HTTPException(status_code=404, detail=f"Unknown document: {doc_id}")
    report = (manifest.state or {}).get("report") if manifest is not None else None
    return {
        "doc_id": doc_id,
        "status": manifest.status if manifest is not None else record.status,
        "manifest": manifest.model_dump(mode="json") if manifest is not None else None,
        "report": report,
        "catalog": record.model_dump(mode="json") if record is not None else None,
    }


@api.get("/audit/{doc_id}")
def audit_endpoint(doc_id: str) -> dict:
    """The document's audit entries plus hash-chain verification."""
    entries = audit_log.entries(doc_id)
    chain = audit_log.verify_chain(entries)
    return {
        "doc_id": doc_id,
        "entries": [e.model_dump(mode="json") for e in entries],
        "chain": chain.model_dump(),
    }


@api.post("/review/{doc_id}/resolve")
def resolve_review_endpoint(doc_id: str, payload: ReviewResolve) -> dict:
    """Disposition a parked document (approve / correct / reject)."""
    state = resolve_review(
        doc_id,
        payload.action,
        doc_type=payload.doc_type,
        doc_subclass=payload.doc_subclass,
        reviewer=payload.reviewer,
    )
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"No parked document with id {doc_id}"
        )
    return {
        "doc_id": doc_id,
        "action": payload.action,
        "status": state.status,
        "doc_type": state.sort.doc_type if state.sort is not None else payload.doc_type,
        "route_trail": state.route_trail,
    }


@api.get("/runs")
def list_runs_endpoint() -> dict:
    """Eval runs recorded in SQLite (``eval_docs``); empty before the first run."""
    return {"runs": _eval_runs()}


@api.get("/runs/{run_id}/cards")
def run_cards_endpoint(run_id: str) -> dict:
    """Card JSONs for a run; empty until Task 21 writes them."""
    if re.fullmatch(r"[0-9a-f]{12}", run_id) is None:
        raise HTTPException(status_code=400, detail="Invalid run ID")
    cards_dir = get_settings().base_dir / "runs" / run_id / "cards"
    cards: list[dict] = []
    if cards_dir.is_dir():
        for path in sorted(cards_dir.glob("*.json")):
            try:
                cards.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                logger.warning("card_unreadable", path=str(path))
    return {"run_id": run_id, "cards": cards}


def _eval_runs() -> list[dict]:
    """Distinct ``run_id``s with document counts from the ``eval_docs`` table."""
    from mailroom_reloaded.storage.db import get_engine

    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT run_id, COUNT(*) AS documents FROM eval_docs "
                    "GROUP BY run_id ORDER BY run_id"
                )
            ).all()
    except Exception:  # noqa: BLE001 - table absent until the first eval run
        return []
    return [{"run_id": r[0], "documents": int(r[1])} for r in rows]


# --------------------------------------------------------------------------- intake


def _gmail_poll_task() -> None:
    """Background fetch+ingest kicked off by a Gmail Pub/Sub push.

    Errors are swallowed and logged: a webhook must acknowledge even when the
    mailbox is temporarily unreachable, and Pub/Sub already retries.
    """
    try:
        created = gmail_intake.poll_and_ingest()
        logger.info("gmail_push_ingested", count=len(created))
    except Exception:
        logger.warning("gmail_push_ingest_failed", exc_info=True)


@api.post("/intake/gmail", status_code=204)
async def gmail_push_endpoint(
    request: Request, background: BackgroundTasks
) -> Response:
    """Accept a Gmail Pub/Sub push and ingest in the background.

    The envelope's ``message.data`` is Base64URL-encoded JSON; the route
    validates it, schedules the fetch, and returns 204/200 immediately so the
    Pub/Sub push is acknowledged. Same bearer guard as every other ``/v1`` route.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    try:
        notification = gmail_intake.decode_pubsub_push(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "gmail_push_received",
        email_address=notification.email_address,
        history_id=notification.history_id,
    )
    background.add_task(_gmail_poll_task)
    return Response(status_code=204)


@api.post("/intake/gmail/poll")
def gmail_poll_endpoint(limit: int = Query(default=25, ge=1, le=200)) -> dict:
    """On-demand demo upload: fetch new Gmail attachments, return created doc_ids."""
    try:
        doc_ids = gmail_intake.poll_and_ingest(limit=limit)
    except gmail_intake.GmailNotInstalled as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except gmail_intake.GmailAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"doc_ids": doc_ids, "count": len(doc_ids)}


# --------------------------------------------------------------------------- app


def _embed_watcher_enabled() -> bool:
    """Return whether the environment opts into the embedded watcher."""
    return (os.environ.get("MAILROOM_EMBED_WATCHER") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Create the bins and, when asked, run the watcher in-process.

    The embedded watcher is opt-in (``MAILROOM_EMBED_WATCHER=1``), because the
    ``split-watcher`` profile runs it as its own service and tests must not race
    a background drainer.
    """
    _bins().inbox.mkdir(parents=True, exist_ok=True)
    watcher = None
    watcher_thread = None
    if _embed_watcher_enabled():
        from mailroom_reloaded.watcher import Watcher

        watcher = Watcher(_bins(), f"api-{os.getpid()}", 1)
        def run_watcher() -> None:
            try:
                watcher.run_forever()
            except Exception:
                logger.exception("embedded_watcher_failed")

        watcher_thread = threading.Thread(
            target=run_watcher, name="mailroom-embedded-watcher", daemon=True
        )
        watcher_thread.start()
        application.state.watcher = watcher
        application.state.watcher_thread = watcher_thread
        logger.info("embedded_watcher_started")
    try:
        yield
    finally:
        if watcher is not None:
            watcher.stop()
        if watcher_thread is not None:
            await asyncio.to_thread(watcher_thread.join, timeout=30)


def create_app() -> FastAPI:
    """Build the API application with its lifespan, routes and UI endpoints."""
    application = FastAPI(
        title="mailroom-reloaded",
        description="Compressed Digital Mailroom API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(api)

    @application.get("/health")
    def health() -> dict:
        """Return the public liveness response."""
        return {"status": "ok", "service": "mailroom"}

    @application.get("/")
    def root() -> RedirectResponse:
        """Redirect the application root to the runs UI."""
        return RedirectResponse(url="/ui")

    @application.get("/ui")
    @application.get("/ui/")
    def ui() -> FileResponse:
        """Serve the packaged UI, raising HTTP 404 when it is absent."""
        if not _UI_INDEX.is_file():
            raise HTTPException(status_code=404, detail="UI is not packaged")
        return FileResponse(_UI_INDEX, media_type="text/html")

    return application


app = create_app()
