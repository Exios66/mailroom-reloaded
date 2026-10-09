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
* ``GET /v1/jev`` — Jev gate status (provider, calibration; never keys).
* ``POST /v1/review/{id}/resolve`` — disposition a parked document.
* ``GET /v1/runs`` / ``GET /v1/runs/{run_id}/cards`` — eval runs (SQLite) and
  card JSONs (empty until Task 21 lands).
* ``GET /ui`` — a vanilla-JS page, no build step.
* ``GET /links`` — public base URLs for the UI's Grafana/Phoenix deep links.

Security (spec section 9): every ``/v1`` route requires the configured bearer
token when ``MAILROOM_API_TOKEN`` is set; binding off loopback without a token
refuses to start (:func:`assert_bind_allowed`). ``/health``, ``/links`` and
``/ui`` stay public so a load balancer and a browser can reach them.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import threading
import time
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
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from mailroom_reloaded import __version__
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
_TUI_DIR = Path(__file__).parent / "tui"


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


def _enforce_bind_policy() -> None:
    """Apply the bind guard when the app is started without ``mailroom serve``.

    ``uvicorn mailroom_reloaded.api.app:app --host 0.0.0.0`` never runs the CLI
    check, so the lifespan re-checks ``MAILROOM_API_HOST``. Deployments whose
    published port is already loopback-only (the dev compose file) opt out with
    ``MAILROOM_ALLOW_UNAUTHENTICATED_BIND=1``.
    """
    if (os.environ.get("MAILROOM_ALLOW_UNAUTHENTICATED_BIND") or "").strip() in {
        "1",
        "true",
        "yes",
    }:
        return
    host = (os.environ.get("MAILROOM_API_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    assert_bind_allowed(host)


# --------------------------------------------------------------------------- schemas


class ReviewResolve(BaseModel):
    action: Literal["approve", "correct", "reject"]
    doc_type: str | None = None
    doc_subclass: str | None = None
    reviewer: str = "reviewer"


# --------------------------------------------------------------------------- routes


def _verify_google_oidc(token: str, audience: str) -> dict:
    """Verify a Google-signed OIDC JWT (signature, expiry, audience)."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, google_requests.Request(), audience)


def require_push_auth(request: Request) -> None:
    """Auth for the Pub/Sub push route: static bearer token or Google OIDC JWT.

    Pub/Sub cannot send a fixed bearer secret, only a signed OIDC token. When
    ``MAILROOM_GMAIL_PUSH_AUDIENCE`` and ``MAILROOM_GMAIL_PUSH_SERVICE_ACCOUNT``
    are set, a JWT for that audience from that service account is accepted; the
    static ``MAILROOM_API_TOKEN`` (a relay) is still accepted. With neither
    configured the route behaves like the other ``/v1`` routes.
    """
    settings = get_settings()
    audience = (settings.gmail_push_audience or "").strip()
    account = (settings.gmail_push_service_account or "").strip()
    if not _configured_token() and not audience:
        return
    auth = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing or invalid API token")
    bearer = auth[len(prefix) :].strip()
    token = _configured_token()
    if token and hmac.compare_digest(bearer.encode("utf-8"), token.encode("utf-8")):
        return
    if audience and account:
        try:
            claims = _verify_google_oidc(bearer, audience)
        except Exception:  # noqa: BLE001 - any verification failure is a 401
            claims = None
        if (
            claims
            and claims.get("email_verified") is True
            and hmac.compare_digest(
                str(claims.get("email", "")).encode("utf-8"), account.encode("utf-8")
            )
        ):
            return
    raise HTTPException(status_code=401, detail="Missing or invalid API token")


api = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])
push_api = APIRouter(prefix="/v1", dependencies=[Depends(require_push_auth)])


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


@api.get("/jev")
def jev_status_endpoint() -> dict:
    """Jev (TypeSafe System One) gate status. Never exposes API keys."""
    from dataclasses import asdict

    from mailroom_reloaded.eval.jev_calibration import load_jev_calibration
    from mailroom_reloaded.settings import jev_config

    cfg = jev_config()
    path = get_settings().base_dir / "models" / "jev_calibration.json"
    calibration = None
    if cfg.enabled and path.is_file():
        try:
            calibration = asdict(load_jev_calibration(path))
        except (OSError, ValueError, KeyError, TypeError):
            calibration = None
    if cfg.enabled and calibration is not None:
        gate = "jev"
    elif (get_settings().base_dir / "models" / "route_gate.json").exists():
        gate = "learned"
    else:
        gate = "band"
    return {
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "model": cfg.model or None,
        "base_url": cfg.base_url or None,
        "calibrated": calibration is not None,
        "calibration": calibration,
        "gate": gate,
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


def _read_ledger():
    """A throwaway read-only ledger: the process-wide one would install the external anchor hook."""
    from mailroom_reloaded.storage.db import get_engine
    from mailroom_reloaded.storage.ledger import Ledger

    return Ledger(get_engine())


def _pruned_run_ids() -> set[str]:
    """Run ids whose spans retention removed; empty when the ledger is unreadable."""
    from mailroom_reloaded.storage.retention import pruned_runs

    try:
        return pruned_runs(_read_ledger())
    except Exception:  # the picker still works without the marker
        logger.warning("replay_pruned_lookup_failed", exc_info=True)
        return set()


def _replay_timeline(session_id: str, from_s: float | None, to_s: float | None):
    """The timeline of ``session_id`` or an HTTP error (400 bad id, 404 none, 410 pruned)."""
    from mailroom_reloaded.obs.replay.sessions import parse_session_id
    from mailroom_reloaded.obs.replay.timeline import build_timeline
    from mailroom_reloaded.storage.db import get_engine

    try:
        kind, key = parse_session_id(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session ID") from None
    pruned = kind == "run" and key in _pruned_run_ids()
    try:
        tl = build_timeline(session_id, from_s=from_s, to_s=to_s, engine=get_engine())
    except Exception:
        logger.warning("replay_timeline_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Timeline unavailable") from None
    if tl is None:
        if pruned:
            raise HTTPException(status_code=410, detail="data pruned")
        raise HTTPException(status_code=404, detail="No such session")
    if pruned:
        tl.session.data_pruned = True
    return tl


@api.get("/replay/sessions")
def replay_sessions_endpoint(limit: int = Query(50, ge=1, le=500)) -> dict:
    """Replayable sessions, newest first; runs retention pruned carry ``data_pruned``."""
    from mailroom_reloaded.obs.replay.sessions import list_sessions
    from mailroom_reloaded.storage.db import get_engine

    try:
        sessions = list_sessions(limit, engine=get_engine())
    except Exception:
        logger.warning("replay_sessions_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Sessions unavailable") from None
    pruned = _pruned_run_ids()
    for sm in sessions:
        if sm.kind == "run" and sm.id.removeprefix("run:") in pruned:
            sm.data_pruned = True
    return {"sessions": [sm.model_dump(mode="json") for sm in sessions]}


@api.get("/replay/sessions/{session_id}/timeline")
def replay_timeline_endpoint(
    session_id: str,
    from_s: float | None = Query(None, ge=0, allow_inf_nan=False),
    to_s: float | None = Query(None, ge=0, allow_inf_nan=False),
) -> dict:
    """The ``replay/v1`` timeline of a session, optionally windowed (seconds)."""
    if from_s is not None and to_s is not None and from_s > to_s:
        raise HTTPException(status_code=422, detail="from_s must not exceed to_s")
    return _replay_timeline(session_id, from_s, to_s).model_dump(mode="json")


@api.get("/replay/sessions/{session_id}/export")
def replay_export_endpoint(session_id: str) -> Response:
    """The full timeline as a downloadable ``<session>.replay.json``."""
    tl = _replay_timeline(session_id, None, None)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    return Response(
        content=tl.model_dump_json(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}.replay.json"'},
    )


#: ``GET /v1/replay/live`` tunables (read once per request; see ``_live_*`` helpers).
_LIVE_POLL_DEFAULT_S = 1.0
_LIVE_HEARTBEAT_DEFAULT_S = 15.0
_LIVE_MAX_FRAMES_DEFAULT = 0


def _live_float(name: str, default: float) -> float:
    """A positive float env tunable, or ``default`` when unset/blank/unparseable."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _live_int(name: str, default: int) -> int:
    """A non-negative int env tunable, or ``default`` when unset/blank/unparseable."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _sse(event: str, data: dict) -> str:
    """One SSE frame, exactly ``event: <name>\\ndata: <json>\\n\\n``."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _live_items(tl):
    """``(event_name, item, seen_key)`` for one timeline snapshot, in a stable order."""
    # An ``#n`` suffix counts repeats of the same base key within one snapshot, so two
    # events that share (t, doc, kind, station) are both delivered instead of collapsed.
    counts: dict[str, int] = {}

    def keyed(base: str) -> str:
        n = counts.get(base, 0)
        counts[base] = n + 1
        return f"{base}#{n}"

    for i, seg in enumerate(tl.segments):
        yield "segment", seg, keyed(f"segment:{seg.span_id or i}")
    for gen in tl.generations:
        yield "generation", gen, keyed(f"generation:{gen.span_id}")
    for ev in tl.events:
        yield "event", ev, keyed(f"event:{ev.t}:{ev.doc_id}:{ev.kind}:{ev.station}")
    for sc in tl.scores:
        yield "score", sc, keyed(f"score:{sc.span_id}:{sc.name}:{sc.doc_id}")


def _item_time(item) -> float:
    """The item's timeline instant: ``t0`` for segments/generations, else ``t``."""
    t = getattr(item, "t0", None)
    if t is None:
        t = getattr(item, "t", None)
    return float(t) if isinstance(t, (int, float)) else 0.0


@api.get("/replay/live")
def replay_live_endpoint(
    request: Request,
    session: str | None = Query(None),
    since: float | None = Query(None, ge=0, allow_inf_nan=False),
) -> StreamingResponse:
    """Follow a session's timeline as Server-Sent Events (``text/event-stream``).

    Resolves ``session`` (a bad id is 400), else the newest session; with none it
    streams a single ``error`` frame. Otherwise: ``ready`` once, then one
    ``segment``/``generation``/``event``/``score`` frame per item not seen before,
    a ``heartbeat`` keepalive, and an ``error`` when the timeline disappears. The
    client bounds the stream with ``MAILROOM_REPLAY_LIVE_MAX_FRAMES``.
    """
    from mailroom_reloaded.obs.replay.sessions import list_sessions, parse_session_id
    from mailroom_reloaded.obs.replay.timeline import build_timeline
    from mailroom_reloaded.storage.db import get_engine
    from mailroom_reloaded.storage.span_store import SpanStore, default_span_store_path

    if session is not None:
        try:
            parse_session_id(session)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid session ID") from None
        sid: str | None = session
    else:
        try:
            sessions = list_sessions(1, engine=get_engine())
        except Exception:
            logger.warning("replay_live_sessions_failed", exc_info=True)
            sessions = []
        sid = sessions[0].id if sessions else None

    poll = _live_float("MAILROOM_REPLAY_LIVE_POLL_S", _LIVE_POLL_DEFAULT_S)
    heartbeat = _live_float(
        "MAILROOM_REPLAY_LIVE_HEARTBEAT_S", _LIVE_HEARTBEAT_DEFAULT_S
    )
    max_frames = _live_int(
        "MAILROOM_REPLAY_LIVE_MAX_FRAMES", _LIVE_MAX_FRAMES_DEFAULT
    )

    async def gen():
        if sid is None:
            yield _sse("error", {"detail": "no sessions"})
            return
        store = SpanStore(default_span_store_path())
        frames = 0
        try:
            yield _sse("ready", {"version": "replay/v1", "session": sid})
            frames += 1
            if max_frames and frames >= max_frames:
                return
            seen: set[str] = set()
            first = True
            # Seed the clock so the first pass sends a keepalive before a large
            # initial item burst, bounding a health probe with few frames.
            last_hb = time.monotonic() - heartbeat
            while True:
                now = time.monotonic()
                if now - last_hb >= heartbeat:
                    yield _sse("heartbeat", {"t": time.time()})
                    frames += 1
                    last_hb = now
                    if max_frames and frames >= max_frames:
                        return
                try:
                    # Sync sqlite reads and a deep copy: keep them off the event loop.
                    tl = await asyncio.to_thread(
                        build_timeline, sid, store=store, engine=get_engine()
                    )
                except Exception:
                    logger.warning("replay_live_timeline_failed", exc_info=True)
                    yield _sse("error", {"detail": "no timeline"})
                    return
                if tl is None:
                    yield _sse("error", {"detail": "no timeline"})
                    return
                for name, item, item_key in _live_items(tl):
                    if item_key in seen:
                        continue
                    seen.add(item_key)
                    if first and since is not None and _item_time(item) < since:
                        continue
                    yield _sse(name, item.model_dump(mode="json"))
                    frames += 1
                    if max_frames and frames >= max_frames:
                        return
                first = False
                if await request.is_disconnected():
                    return
                await asyncio.sleep(poll)
        finally:
            store.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _ledger_run_id(run_id: str | None) -> str | None:
    """``run_id`` when it is a valid run id (or absent); otherwise a 400."""
    from mailroom_reloaded.storage.retention import is_valid_run_id

    if run_id is not None and not is_valid_run_id(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    return run_id


@api.get("/ledger")
def ledger_entries_endpoint(
    run_id: str | None = Query(None),
    kind: str | None = Query(None),
    since_seq: int = Query(0, ge=0, le=2**63 - 1),
    limit: int = Query(50, ge=1, le=500),
    descending: bool = True,
) -> dict:
    """Ledger entries (newest first by default) plus the chain head."""
    from mailroom_reloaded.schemas.ledger import KINDS

    run_id = _ledger_run_id(run_id)
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=400, detail="Invalid kind")
    try:
        ledger = _read_ledger()
        entries = ledger.entries(
            run_id=run_id,
            kind=kind,
            since_seq=since_seq,
            limit=limit,
            descending=descending,
        )
        head = ledger.head()
    except Exception:
        logger.warning("ledger_entries_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Ledger unavailable") from None
    return {
        "entries": [e.model_dump(mode="json") for e in entries],
        "head": {"seq": head.seq, "entry_hash": head.entry_hash} if head else None,
    }


@api.get("/ledger/head")
def ledger_head_endpoint() -> dict:
    """The newest committed entry (or ``null``) and the chain length."""
    try:
        ledger = _read_ledger()
        head = ledger.head()
        count = ledger.total()
    except Exception:
        logger.warning("ledger_head_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Ledger unavailable") from None
    return {
        "head": (
            {
                "seq": head.seq,
                "entry_hash": head.entry_hash,
                "ts": head.ts,
                "kind": head.kind,
            }
            if head
            else None
        ),
        "count": count,
    }


@api.get("/ledger/verify")
def ledger_verify_endpoint(run_id: str | None = Query(None)) -> dict:
    """Verify the whole chain, or one run's entries. Sync: runs on the threadpool."""
    run_id = _ledger_run_id(run_id)
    try:
        return _read_ledger().verify(run_id).model_dump()
    except Exception:
        logger.warning("ledger_verify_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Ledger unavailable") from None


@api.get("/ledger/keep")
def ledger_keep_endpoint() -> dict:
    """The effective keep policy, where it came from, and the pinned and showcase runs."""
    from mailroom_reloaded.storage.retention import (
        SHOWCASE_RUN_IDS,
        effective_policy,
        pinned_runs,
        policy_source,
    )

    try:
        ledger = _read_ledger()
        policy = effective_policy(ledger)
        source = policy_source(ledger)
        pinned = sorted(pinned_runs(ledger))
    except Exception:
        logger.warning("ledger_keep_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Ledger unavailable") from None
    return {
        "policy": f"recent:{policy.n}" if policy.mode == "recent" else policy.mode,
        "source": source,
        "pinned": pinned,
        "showcase": list(SHOWCASE_RUN_IDS),
    }


class LedgerRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(max_length=120)


class LedgerPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(max_length=120)


def _ledger_write(action, *args) -> dict:
    """Run a retention write on the process-wide ledger and flush so a following read sees it."""
    from mailroom_reloaded.storage.ledger import get_ledger

    try:
        ledger = get_ledger()
        accepted = action(ledger, *args)
        flushed = ledger.flush()
    except ValueError:
        raise
    except Exception:
        logger.warning("ledger_write_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Ledger unavailable") from None
    if accepted is False or not flushed:  # None is a no-op (already in that state)
        raise HTTPException(status_code=500, detail="Ledger unavailable")
    return {"ok": True}


@api.post("/ledger/pin")
def ledger_pin_endpoint(body: LedgerRunBody) -> dict:
    """Pin a run so retention keeps its spans."""
    from mailroom_reloaded.storage.retention import pin

    _ledger_run_id(body.run_id)
    try:
        return _ledger_write(pin, body.run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run ID") from None


@api.post("/ledger/unpin")
def ledger_unpin_endpoint(body: LedgerRunBody) -> dict:
    """Unpin a run; showcase runs are always kept."""
    from mailroom_reloaded.storage.retention import SHOWCASE_RUN_IDS, unpin

    _ledger_run_id(body.run_id)
    if body.run_id in SHOWCASE_RUN_IDS:
        raise HTTPException(status_code=400, detail="Showcase runs cannot be unpinned")
    try:
        return _ledger_write(unpin, body.run_id)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Showcase runs cannot be unpinned"
        ) from None


@api.post("/ledger/policy")
def ledger_policy_endpoint(body: LedgerPolicyBody) -> dict:
    """Record a keep policy (``pinned``, ``all`` or ``recent:<N>``) overriding the env default."""
    from mailroom_reloaded.storage.retention import set_policy

    try:
        return _ledger_write(set_policy, body.value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid policy") from None


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


@push_api.post("/intake/gmail", status_code=204)
async def gmail_push_endpoint(
    request: Request, background: BackgroundTasks
) -> Response:
    """Accept a Gmail Pub/Sub push and ingest in the background.

    The envelope's ``message.data`` is Base64URL-encoded JSON; the route
    validates it, schedules the fetch, and returns 204/200 immediately so the
    Pub/Sub push is acknowledged. Guarded by :func:`require_push_auth`.
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


_RETENTION_INTERVAL_S = 86400


async def _start_retention() -> asyncio.Task | None:
    """Prune soon after startup, then daily, in a background task; never fails startup.

    Skipped under pytest (same predicate as the span store) unless a store path is set.
    """
    from mailroom_reloaded.obs.tracing import span_store_enabled

    if not span_store_enabled():
        return None
    from mailroom_reloaded.storage import retention

    async def run_once() -> None:
        try:
            await asyncio.to_thread(retention.maintain)
        except Exception:
            logger.warning("retention_maintain_failed", exc_info=True)

    async def loop() -> None:
        while True:
            await run_once()
            await asyncio.sleep(_RETENTION_INTERVAL_S)

    return asyncio.get_running_loop().create_task(loop())


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Create the bins and, when asked, run the watcher in-process.

    The embedded watcher is opt-in (``MAILROOM_EMBED_WATCHER=1``), because the
    ``split-watcher`` profile runs it as its own service and tests must not race
    a background drainer.
    """
    _enforce_bind_policy()
    _bins().inbox.mkdir(parents=True, exist_ok=True)
    retention_task = await _start_retention()
    watcher = None
    watcher_thread = None
    if _embed_watcher_enabled():
        from mailroom_reloaded.watcher import Watcher, WatcherLockHeld

        watcher = Watcher(_bins(), f"api-{os.getpid()}", 1)
        settled = threading.Event()  # set once started, or once it gave up

        def run_watcher() -> None:
            try:
                watcher.run_forever()
            except WatcherLockHeld:
                logger.warning(
                    "embedded_watcher_not_started",
                    reason="another watcher holds watcher.lock",
                )
            except Exception:
                logger.exception("embedded_watcher_failed")
            finally:
                settled.set()

        watcher_thread = threading.Thread(
            target=run_watcher, name="mailroom-embedded-watcher", daemon=True
        )
        watcher_thread.start()
        application.state.watcher = watcher
        application.state.watcher_thread = watcher_thread
        ready = getattr(watcher, "ready", None)

        def _wait_started() -> bool:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if ready is not None and ready.is_set():
                    return True
                if settled.wait(0.02):
                    return ready is not None and ready.is_set()
            return ready is not None and ready.is_set()

        if await asyncio.to_thread(_wait_started):
            logger.info("embedded_watcher_started")
    try:
        yield
    finally:
        if retention_task is not None:
            retention_task.cancel()
        if watcher is not None:
            watcher.stop()
        if watcher_thread is not None:
            await asyncio.to_thread(watcher_thread.join, timeout=30)


def create_app() -> FastAPI:
    """Build the API application with its lifespan, routes and UI endpoints."""
    application = FastAPI(
        title="mailroom-reloaded",
        description="Compressed Digital Mailroom API",
        version=__version__,
        lifespan=lifespan,
    )
    application.include_router(api)
    application.include_router(push_api)

    @application.get("/health")
    def health() -> dict:
        """Return the public liveness response."""
        return {"status": "ok", "service": "mailroom"}

    @application.get("/links")
    def links() -> dict:
        """Public observability link config for the UI; never a secret.

        The ``/ui`` header and per-run links, and the replay viewer's ``o``/``g``
        keys, build their Grafana/Phoenix URLs from these values. ``phoenix_project``
        mirrors the tracing resource attribute so a link can name the project.
        """
        settings = get_settings()
        return {
            "public_url": settings.public_url,
            "phoenix_url": settings.phoenix_url,
            "grafana_url": settings.grafana_url,
            "phoenix_project": os.environ.get("MAILROOM_PHOENIX_PROJECT", "mailroom-live"),
        }

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

    @application.get("/tui")
    @application.get("/tui/")
    def tui() -> FileResponse:
        """Serve the browser terminal shell (public, like ``/ui``)."""
        index = _TUI_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="TUI is not packaged")
        return FileResponse(index, media_type="text/html")

    if _TUI_DIR.is_dir():
        application.mount(
            "/tui/assets", StaticFiles(directory=_TUI_DIR), name="tui-assets"
        )

    return application


app = create_app()
