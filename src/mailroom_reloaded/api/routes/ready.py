"""``GET /ready``: aggregate readiness of the app's backends (Phase 6 P6-A, item A4).

Each component is probed concurrently with a timeout (``PROBE_TIMEOUT_S``) and reports
``ok | degraded | down | unconfigured`` plus ``latency_ms``. A probe that times out is
``degraded``; one that raises is ``down``; neither is ever a 500. The aggregate is
``down`` (HTTP 503) only when a critical component (the app database or the ledger) is
down; any other down or degraded component makes it ``degraded`` (HTTP 200).

The status code and aggregate status are public, like ``/health``. Per-component rows
are returned only when no token is configured or the request carries the bearer token,
so an anonymous caller learns nothing about the deployment's backends.

Probes run server-side and read only local state or public status URLs; none sends a
credential. Components whose backend wiring lands with a later issue report
``unconfigured`` with the issue named in ``detail``.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)

__all__ = ["PROBES", "PROBE_TIMEOUT_S", "Probe", "aggregate", "router", "run_probes"]

STATUSES = ("ok", "degraded", "down", "unconfigured")
#: Per-probe budget in seconds (sub-plan section 2, rule 5).
PROBE_TIMEOUT_S = 2.0
_MAX_DETAIL = 200

#: A check returns ``(status, detail)``; it runs in a worker thread.
Check = Callable[[Request], "tuple[str, str | None]"]


@dataclass(frozen=True)
class Probe:
    """One named component check; ``critical`` components can make the aggregate ``down``."""

    name: str
    check: Check
    critical: bool = False


# --------------------------------------------------------------------------- probes


def _probe_db(_request: Request) -> tuple[str, str | None]:
    from sqlalchemy import text

    from mailroom_reloaded.storage.db import get_engine

    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return "ok", None


def _probe_ledger(_request: Request) -> tuple[str, str | None]:
    from mailroom_reloaded.storage.db import get_engine
    from mailroom_reloaded.storage.ledger import Ledger

    # A throwaway reader: no writer thread starts until an append, and no anchor hook.
    head = Ledger(get_engine()).head()
    return "ok", f"head seq {head.seq}" if head else "empty"


def _probe_span_store(_request: Request) -> tuple[str, str | None]:
    from mailroom_reloaded.obs.tracing import span_store_enabled
    from mailroom_reloaded.storage.span_store import SpanStore, default_span_store_path

    if not span_store_enabled():
        return "unconfigured", "span store disabled"
    path = default_span_store_path()
    if not path.is_file():
        return "ok", "no spans recorded yet"  # do not create the file from a probe
    store = SpanStore(path)
    try:
        store.watermark()
    finally:
        store.close()
    return "ok", None


def _probe_watcher(request: Request) -> tuple[str, str | None]:
    thread = getattr(request.app.state, "watcher_thread", None)
    if thread is None:
        return "unconfigured", "no embedded watcher (split-watcher or none)"
    return ("ok", "embedded") if thread.is_alive() else ("down", "embedded watcher stopped")


def _pending(issue: str) -> Check:
    def check(_request: Request) -> tuple[str, str | None]:
        return "unconfigured", f"probe not wired yet ({issue})"

    return check


#: How long a socket/HTTP probe waits before it is called unreachable.
_REACH_TIMEOUT_S = 1.5


def _split_host_port(target: str) -> tuple[str, int] | None:
    """``host:port`` from a URL or a bare ``host:port``; ``None`` when it cannot be parsed."""
    raw = target.strip()
    if "://" not in raw:
        raw = f"//{raw}"
    parts = urlsplit(raw)
    host = parts.hostname
    if not host:
        return None
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return host, port


def _tcp_probe(target: str) -> tuple[str, str | None]:
    """Reachable when a TCP connection opens (works for a gRPC or HTTP OTLP endpoint)."""
    hp = _split_host_port(target)
    if hp is None:
        return "unconfigured", "endpoint is not a host:port or URL"
    host, port = hp
    try:
        with socket.create_connection((host, port), timeout=_REACH_TIMEOUT_S):
            return "ok", None
    except OSError as exc:
        return "down", f"{host}:{port} unreachable ({type(exc).__name__})"


def _http_probe(url: str) -> tuple[str, str | None]:
    """Reachable when the URL answers; a 5xx is ``degraded``, a 4xx is still ``ok``."""
    try:
        with urlopen(url, timeout=_REACH_TIMEOUT_S) as resp:  # operator-configured status URL
            code = int(getattr(resp, "status", 200))
    except HTTPError as exc:
        return ("degraded", f"http {exc.code}") if exc.code >= 500 else ("ok", f"http {exc.code}")
    except Exception as exc:  # noqa: BLE001 - any failure is a down status, never a 500
        return "down", f"unreachable ({type(exc).__name__})"
    return ("ok", None) if code < 500 else ("degraded", f"http {code}")


#: Server-side probe URLs. The public ``MAILROOM_PHOENIX_URL`` / ``MAILROOM_GRAFANA_URL``
#: are browser-facing (e.g. ``http://localhost:6006``); inside a container the backend is at
#: a service name, so reachability uses its own variable. Unset means "no backend wired".
_PHOENIX_PROBE_ENV = "MAILROOM_PHOENIX_PROBE_URL"
_GRAFANA_PROBE_ENV = "MAILROOM_GRAFANA_PROBE_URL"


def _probe_collector(_request: Request) -> tuple[str, str | None]:
    """The OTLP trace endpoint the exporter will use (with or without Docker)."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not endpoint:
        return "unconfigured", "set OTEL_EXPORTER_OTLP_ENDPOINT to export spans"
    return _tcp_probe(endpoint)


def _probe_phoenix(_request: Request) -> tuple[str, str | None]:
    from mailroom_reloaded.settings import get_settings

    target = os.environ.get(_PHOENIX_PROBE_ENV)
    if not target:
        return "unconfigured", f"set {_PHOENIX_PROBE_ENV} (public URL {get_settings().phoenix_url})"
    return _http_probe(target)


def _probe_grafana(_request: Request) -> tuple[str, str | None]:
    from mailroom_reloaded.settings import get_settings

    target = os.environ.get(_GRAFANA_PROBE_ENV)
    if not target:
        return "unconfigured", f"set {_GRAFANA_PROBE_ENV} (public URL {get_settings().grafana_url})"
    return _http_probe(target)


#: Role whose endpoint the provider probe resolves; the sorter is the first model call.
_LLM_PROBE_ROLE = "sorter"


def _probe_llm_provider(_request: Request) -> tuple[str, str | None]:
    """Reachability of the configured provider's base URL; no resolvable endpoint is ``unconfigured``."""
    from mailroom_reloaded.llm.client import resolve
    from mailroom_reloaded.settings import get_settings

    provider = get_settings().provider
    try:
        base_url = resolve(_LLM_PROBE_ROLE).base_url
    except Exception as exc:  # noqa: BLE001 - a config error is a status, never a 500
        # Only the exception type: the resolver's message can name a path or key variable.
        return "unconfigured", f"provider {provider} has no endpoint ({type(exc).__name__})"
    return _http_probe(base_url)  # no credential is sent: reachability only


#: Component order is the response order (sub-plan section 5).
PROBES: list[Probe] = [
    Probe("db", _probe_db, critical=True),
    Probe("ledger", _probe_ledger, critical=True),
    Probe("span_store", _probe_span_store),
    Probe("collector", _probe_collector),
    Probe("phoenix", _probe_phoenix),
    Probe("prometheus", _pending("#66")),
    Probe("grafana", _probe_grafana),
    Probe("llm_provider", _probe_llm_provider),
    Probe("watcher", _probe_watcher),
]


# --------------------------------------------------------------------------- runner


def _clean_detail(detail: Any) -> str | None:
    if detail is None:
        return None
    text = "".join(ch if ch.isprintable() else " " for ch in str(detail))
    return text[:_MAX_DETAIL]


async def _run_one(probe: Probe, request: Request, timeout: float) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        status, detail = await asyncio.wait_for(
            asyncio.to_thread(probe.check, request), timeout
        )
        if status not in STATUSES:
            status, detail = "degraded", "probe returned an unknown status"
    except TimeoutError:
        # The worker thread cannot be cancelled; it finishes in the background.
        status, detail = "degraded", f"timed out after {timeout:g}s"
    except Exception as exc:
        # Only the exception type: messages can carry paths or DSNs.
        logger.warning("ready_probe_failed", component=probe.name, exc_info=True)
        status, detail = "down", f"probe failed: {type(exc).__name__}"
    row: dict[str, Any] = {
        "name": probe.name,
        "status": status,
        "latency_ms": round((time.perf_counter() - start) * 1000, 1),
    }
    cleaned = _clean_detail(detail)
    if cleaned:
        row["detail"] = cleaned
    return row


async def run_probes(
    request: Request, probes: list[Probe], timeout: float
) -> list[dict[str, Any]]:
    """Run every probe concurrently; a failing probe is a status, never an exception."""
    return list(await asyncio.gather(*(_run_one(p, request, timeout) for p in probes)))


def aggregate(rows: list[dict[str, Any]], probes: list[Probe]) -> str:
    """``down`` when a critical component is down; ``degraded`` for any other trouble."""
    critical = {p.name for p in probes if p.critical}
    statuses = [(r["name"], r["status"]) for r in rows]
    if any(s == "down" and n in critical for n, s in statuses):
        return "down"
    if any(s in ("down", "degraded") for _, s in statuses):
        return "degraded"
    return "ok"


def _authorized(request: Request) -> bool:
    """True when no token is configured or the request carries it (as ``/v1``)."""
    # Lazy: app.py imports this module at load time.
    from mailroom_reloaded.api.app import require_token

    try:
        require_token(request)
    except HTTPException:
        return False
    return True


# --------------------------------------------------------------------------- route

router = APIRouter()


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Aggregate readiness; 200 for ok/degraded, 503 for down; detail behind the token."""
    probes = list(PROBES)
    rows = await run_probes(request, probes, PROBE_TIMEOUT_S)
    status = aggregate(rows, probes)
    body: dict[str, Any] = {"schema_version": 1, "status": status}
    if _authorized(request):
        body["components"] = rows
    return JSONResponse(
        body,
        status_code=503 if status == "down" else 200,
        headers={"Cache-Control": "no-store"},
    )
