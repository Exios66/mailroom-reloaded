"""FastAPI app for the offline ingress sandbox.

Routes (all JSON routes require the bearer token when ``MAILROOM_API_TOKEN`` is
set, exactly like ``/v1`` in the main API; ``/health`` and the static ``/ui`` shell
stay public)::

    GET  /health   GET /ui   GET /ui/app.js   GET /ui/app.css
    /api/sandbox/v1/...   see the routes below
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from mailroom_reloaded import __version__
from mailroom_reloaded.api.app import require_token
from mailroom_reloaded.sandbox.server.service import FLOWS, SandboxService

__all__ = ["create_sandbox_app"]

UI_DIR = Path(__file__).resolve().parent / "ui"
CSP = "default-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"


class InjectBody(BaseModel):
    scenario_ids: list[str] | Literal["all"] = Field(
        description="scenario names, or 'all'"
    )
    flows: list[str] | None = Field(
        default=None, description=f"subset of {list(FLOWS)}; default both"
    )
    stagger_seconds: int = Field(default=0, ge=0, le=86400)
    process: bool = True
    wait: bool = False


class RunBody(BaseModel):
    flows: list[str] = Field(default_factory=lambda: list(FLOWS))
    wait: bool = False


class ReviewBody(BaseModel):
    by: str = "reviewer"


class BossDecisionBody(BaseModel):
    message_id: str
    decision: Literal["legitimate", "quarantine"]
    reason: str = ""
    category: Literal["phishing", "malware", "other"] | None = None
    by: str = "boss"


class ProfileBody(BaseModel):
    egress_profile: Literal["closed", "egress"] | None = None
    autonomy: Literal["human", "sandbox"] | None = None


class AttemptBody(BaseModel):
    to: str
    subject: str = "(manual test)"
    body: str = "Manual send attempt from the sandbox UI."


def _wrap(fn):
    """Map service exceptions to HTTP errors."""
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(404, f"not found: {exc.args[0]}") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def create_sandbox_app(service: SandboxService) -> FastAPI:
    """Build the sandbox API and static UI around the supplied service."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Start the service for the application lifetime and stop it on shutdown."""
        service.start()
        try:
            yield
        finally:
            service.stop()

    app = FastAPI(
        title="mailroom-sandbox",
        description="Offline ingress simulation sandbox",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.service = service
    api = APIRouter(prefix="/api/sandbox/v1", dependencies=[Depends(require_token)])

    @api.get("/status")
    def status() -> dict:
        """Return service configuration, counters, and component status."""
        return service.status()

    @api.get("/scenarios")
    def scenarios() -> dict:
        """List available scenarios with timeline counts and optional verdicts."""
        out = []
        for name in service.content.scenario_ids():
            sc = service.content.cs.scenarios[name]
            tl = sc.get("timeline", [])
            row = {
                "name": name,
                "title": sc.get("title"),
                "tags": sc.get("tags", []),
                "profile": sc.get("profile"),
                "emails": sum(1 for t in tl if "client" in t),
                "document_feeds": sum(1 for t in tl if "ingress" in t),
                "faults": sum(1 for t in tl if "fault" in t),
                "attachments": sum(
                    len(t["client"].get("attach", []) or [])
                    for t in tl
                    if "client" in t
                ),
            }
            if service.show_expected:
                row["expected_intent"] = sc.get("expect", {}).get("intent")
                ev = service.evaluation(name)
                row["verdict"] = ev["verdict"] if ev else None
            out.append(row)
        return {"scenarios": out, "count": len(out)}

    @api.get("/scenarios/{name}")
    def scenario(name: str) -> dict:
        """Return a scenario and its evaluation, honoring expected-outcome visibility."""
        sc = service.content.cs.scenarios.get(name)
        if sc is None:
            raise HTTPException(404, f"unknown scenario {name}")
        view = dict(sc)
        if not service.show_expected:
            view.pop("expect", None)
        return {"scenario": view, "evaluation": service.evaluation(name)}

    @api.post("/inject")
    def inject(body: InjectBody) -> dict:
        """Inject a batch of scenarios and optionally wait for processing."""
        res = _wrap(
            lambda: service.inject(
                body.scenario_ids,
                flows=body.flows,
                stagger_seconds=body.stagger_seconds,
                process=body.process,
            )
        )
        if body.wait:
            service.wait_idle()
            res["states"] = {m: service.message(m)["state"] for m in res["message_ids"]}
        return res

    @api.get("/messages")
    def messages(
        state: str | None = None, scenario: str | None = None, batch: str | None = None
    ) -> dict:
        """List messages matching the optional state, scenario, and batch filters."""
        rows = service.list_messages(state=state, scenario=scenario, batch=batch)
        return {"messages": rows, "count": len(rows)}

    @api.get("/messages/{mid}")
    def message(mid: str) -> dict:
        """Return the public message view or a not-found error."""
        return _wrap(lambda: service.message(mid))

    @api.get("/messages/{mid}/trace")
    def trace(mid: str) -> dict:
        """Return the ingress, processing, and egress trace for a message."""
        return _wrap(lambda: service.trace(mid))

    @api.post("/messages/{mid}/run")
    def run(mid: str, body: RunBody) -> dict:
        """Queue the requested flows for a message and optionally wait for completion."""
        res = _wrap(lambda: service.run_message(mid, body.flows))
        if body.wait:
            service.wait_idle()
            res = service.message(mid)
        return res

    @api.post("/messages/{mid}/release")
    def release(mid: str) -> dict:
        """Release a shed message for processing through its selected flows."""
        return _wrap(lambda: service.release_message(mid))

    @api.post("/messages/{mid}/attachments/{name}/release")
    def release_attachment(mid: str, name: str, body: ReviewBody | None = None) -> dict:
        """Release a held attachment into the pipeline with reviewer attribution."""
        return _wrap(
            lambda: service.release_attachment(
                mid, name, (body.by if body else "human")
            )
        )

    @api.get("/events")
    def events(
        since: int = 0,
        ref: str | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict:
        """Return events after a sequence number, optionally filtered by reference."""
        rows = service.events_since(since, ref, limit)
        return {"events": rows, "last_seq": rows[-1]["seq"] if rows else since}

    @api.get("/ingress")
    def ingress() -> dict:
        """Return admission-meter counters, message counts, and pending work."""
        s = service.status()
        return {
            "meter": s["ingress"],
            "messages": s["messages"],
            "queue_pending": s["queue_pending"],
        }

    @api.get("/outbox")
    def outbox() -> dict:
        """List outbound drafts and their capture or blocking summary."""
        with service._lock:
            items = sorted(
                (dict(i) for i in service.outbox.items.values()), key=lambda i: i["id"]
            )
        return {"outbox": items, "summary": service.outbox.summary()}

    @api.post("/outbox/{oid}/approve")
    def approve(oid: str, body: ReviewBody | None = None) -> dict:
        """Apply approval and send-policy checks to an outbound draft."""
        return _wrap(
            lambda: service.approve_outbound(oid, (body.by if body else "reviewer"))
        )

    @api.post("/outbox/{oid}/reject")
    def reject(oid: str, body: ReviewBody | None = None) -> dict:
        """Reject an outbound draft with optional reviewer attribution."""
        return _wrap(
            lambda: service.reject_outbound(oid, (body.by if body else "reviewer"))
        )

    @api.post("/egress/attempt")
    def attempt(body: AttemptBody) -> dict:
        """Run a manual send attempt through the virtual outbox."""
        return service.outbox.attempt(to=body.to, subject=body.subject, body=body.body)

    @api.get("/egress/probe")
    def probe(to: str) -> dict:
        """Check an address against both simulated recipient-policy profiles."""
        return service.outbox.probe(to)

    @api.post("/config")
    def config(body: ProfileBody) -> dict:
        """Update the requested egress profile or autonomy mode and return both."""
        if body.egress_profile:
            service.set_egress_profile(body.egress_profile)
        if body.autonomy:
            service.autonomy = body.autonomy
        return {"egress_profile": service.outbox.profile, "autonomy": service.autonomy}

    @api.get("/documents")
    def documents() -> dict:
        """List document summaries recorded by sandbox pipeline runs."""
        rows = service.documents()
        return {"documents": rows, "count": len(rows)}

    @api.get("/documents/{doc_id}")
    def document(doc_id: str) -> dict:
        """Return pipeline details for a document or a not-found error."""
        d = service.pipeline.document(doc_id)
        if d is None:
            raise HTTPException(404, f"unknown document {doc_id}")
        return d

    @api.get("/documents/{doc_id}/audit")
    def audit(doc_id: str) -> dict:
        """Return a document audit trail and its chain-verification result."""
        return service.pipeline.audit(doc_id)

    @api.get("/policy")
    def policy() -> dict:
        """Expose the loaded policy rules, provenance, and file hashes."""
        p = service.content.policy
        return {
            "source": p.source,
            "files": p.files,
            "ingress": p.ingress,
            "recipient": p.recipient,
            "send_schedule": p.send_schedule,
            "delegation_matrix": list(p.delegation.values()),
        }

    @api.get("/conformance")
    def conformance() -> dict:
        """Summarize scenario verdicts, refusing access when expectations are hidden."""
        if not service.show_expected:
            raise HTTPException(
                403, "expected outcomes are hidden (server started with --no-expected)"
            )
        rows = []
        for name in service.content.scenario_ids():
            ev = service.evaluation(name)
            if ev:
                rows.append(
                    {
                        "scenario": name,
                        "verdict": ev["verdict"],
                        **ev["summary"],
                        "failed_checks": [
                            c["key"] for c in ev["checks"] if c["ok"] is False
                        ],
                    }
                )
        return {
            "results": rows,
            "pass": sum(r["verdict"] == "pass" for r in rows),
            "fail": sum(r["verdict"] == "fail" for r in rows),
        }

    @api.get("/boss/mailbox")
    def boss_mailbox(
        direction: Literal["correspondent->boss", "boss->correspondent"] | None = None,
        role: Literal["correspondent", "boss"] | None = None,
        thread: str | None = None,
        message: str | None = None,
        status: Literal["new", "read", "acted", "expired"] | None = None,
        kind: str | None = None,
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict:
        """List matching entries after the exclusive sequence cursor without marking them read.

        Return the last returned sequence, or ``since`` when no entries match.
        """
        rows = service.mailbox.list(
            direction=direction,
            role=role,
            thread_id=thread,
            message_id=message,
            status=status,
            kind=kind,
            since=since,
            limit=limit,
        )
        return {
            "entries": rows,
            "count": len(rows),
            "last_seq": rows[-1]["seq"] if rows else since,
        }

    @api.get("/boss/mailbox/{entry_id}")
    def boss_mailbox_entry(entry_id: str) -> dict:
        """Return an entry and status history without marking it read; raise HTTP 404 if absent."""
        e = service.mailbox.get(entry_id)
        if e is None:
            raise HTTPException(404, f"unknown mailbox entry {entry_id}")
        return {**e, "history": service.mailbox.history(entry_id)}

    @api.get("/boss/pending")
    def boss_pending() -> dict:
        """Return pending review cases and their count without changing review state."""
        rows = service.pending_reviews()
        return {"pending": rows, "count": len(rows)}

    @api.get("/boss/decisions")
    def boss_decisions() -> dict:
        """Return all non-pending review cases and their count."""
        rows = [c for c in service.list_reviews() if c["state"] != "pending"]
        return {"decisions": rows, "count": len(rows)}

    @api.post("/boss/decisions")
    def boss_decide(body: BossDecisionBody) -> dict:
        """Record a decision and apply its release or quarantine effects immediately.

        Missing messages/reviews become HTTP 404; invalid or already-decided
        reviews become HTTP 409. Permission errors become HTTP 403.
        """
        return _wrap(
            lambda: service.boss_decide(
                body.message_id,
                body.decision,
                body.reason,
                category=body.category,
                by=body.by,
            )
        )

    @api.post("/reset")
    def reset() -> dict:
        """Clear sandbox state and return a reset acknowledgment."""
        service.reset()
        return {"reset": True}

    app.include_router(api)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Add content-security, MIME-sniffing, and cache-control response headers."""
        resp: Response = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Return public liveness information and the current timestamp."""
        return {"status": "ok", "service": "mailroom-sandbox", "ts": time.time()}

    @app.get("/")
    def root() -> RedirectResponse:
        """Redirect the root path to the sandbox UI."""
        return RedirectResponse("/ui")

    def _static(name: str, media: str) -> FileResponse:
        """Serve a packaged UI asset or raise a not-found error if absent."""
        p = UI_DIR / name
        if not p.is_file():
            raise HTTPException(404, "UI is not packaged")
        return FileResponse(p, media_type=media)

    @app.get("/ui")
    @app.get("/ui/")
    def ui() -> FileResponse:
        """Serve the sandbox HTML page."""
        return _static("index.html", "text/html")

    @app.get("/ui/app.js")
    def ui_js() -> FileResponse:
        """Serve the sandbox browser script."""
        return _static("app.js", "text/javascript")

    @app.get("/ui/mailbox.js")
    def ui_mailbox_js() -> FileResponse:
        """Serve the mailbox panel script, or raise HTTP 404 if it is not packaged."""
        return _static("mailbox.js", "text/javascript")

    @app.get("/ui/app.css")
    def ui_css() -> FileResponse:
        """Serve the sandbox stylesheet."""
        return _static("app.css", "text/css")

    return app
