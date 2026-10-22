"""``GET /v1/config``: a read-only, secret-free summary of the effective settings (#64, P6-B).

The route is token-gated like every ``/v1`` route: its dependency is the same
``app.require_token`` the ``/v1`` router uses, so with ``MAILROOM_API_TOKEN`` set an
unauthenticated request gets 401.

The body is an explicit allow-list. A field appears only because ``build_config`` names it;
a setting added later is not exposed until someone adds it there. Every secret-typed value
(the API token, provider and Jev keys, the anchor DSN and key, the SMTP and Twilio
credentials, the pager webhook secret, the OTLP endpoints) is reported only as the string
``"set"`` or ``"unset"``: never a value, prefix, length or hash. Base URLs are reduced to
``scheme://host[:port]``, so userinfo, path and query are dropped.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mailroom_reloaded import __version__
from mailroom_reloaded.settings import get_settings, jev_config, load_taxonomy

__all__ = ["SCHEMA_VERSION", "build_config", "router"]

SCHEMA_VERSION = 1
_PROVIDERS = ("mock", "openrouter", "vllm", "llamafile")
_JEV_PROVIDERS = ("off", "openrouter", "typesafe", "local")
_ANCHOR_MODES = ("none", "supabase", "postgres", "export")
_PAGER_TRANSPORTS = ("twilio", "email_gateway", "console")
_PAGER_DEFAULT_TRANSPORT = "twilio"
_OPENROUTER_DEFAULT_BASE = "https://openrouter.ai/api/v1"

router = APIRouter(prefix="/v1")


def _gate(request: Request) -> None:
    """The ``/v1`` bearer gate. Looked up lazily: ``app.py`` imports this module at load time.

    Raise HTTP 401 when a configured token is missing or incorrect; allow requests
    when no token is configured. Settings validation errors propagate.
    """
    from mailroom_reloaded.api.app import require_token

    require_token(request)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _set_unset(value: object) -> str:
    """Report secret presence as ``set``, or ``unset`` for None or blank string representations."""
    return "set" if value is not None and str(value).strip() != "" else "unset"


def _env_set_unset(name: str) -> str:
    return _set_unset(os.environ.get(name))


def _enum(value: str, allowed: tuple[str, ...]) -> str:
    """The value when it is one of ``allowed``; otherwise ``unset`` or ``invalid`` (never echoed)."""
    if value in allowed:
        return value
    return "unset" if value == "" else "invalid"


def _host_only(url: object) -> str | None:
    """Return an HTTP(S) base URL's scheme and host, plus a nonzero port if supplied.

    Drop userinfo, path, query and fragment. Return None for non-strings or blank
    strings, and ``invalid`` for parse errors, unsupported schemes or missing hosts.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return "invalid"
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        return "invalid"
    if ":" in host:  # an IPv6 literal
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def _guarded(section: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one section; a broken file is reported by exception type, never a 500 or a message."""
    try:
        return section()
    except Exception as exc:  # noqa: BLE001 - a misconfiguration is a status, not an error page
        return {"status": "unavailable", "error": type(exc).__name__}


def _taxonomy() -> dict[str, Any]:
    """Summarize sorted document classes, BERT enablement and global confidence thresholds.

    Taxonomy read, parse, key and validation errors propagate to the section guard.
    """
    tax = load_taxonomy()
    thresholds = tax.confidence_for(None)
    return {
        "status": "ok",
        "doc_classes": sorted(tax.classes),
        "bert_enabled": bool(tax.bert.enabled),
        "thresholds": {
            "low": thresholds.low,
            "high": thresholds.high,
            "judge_band_high": thresholds.judge_band_high,
            "retry_max": thresholds.retry_max,
        },
    }


def _jev() -> dict[str, Any]:
    """Summarize resolved Jev settings with host-only URLs and key-presence markers.

    Configuration errors propagate to the section guard.
    """
    cfg = jev_config()
    return {
        "status": "ok",
        "provider": _enum(cfg.provider, _JEV_PROVIDERS),
        "enabled": cfg.provider != "off",
        "model": cfg.model or None,
        "base_host": _host_only(cfg.base_url),
        "api_key": _set_unset(cfg.api_key),
    }


def _watcher(request: Request) -> dict[str, Any]:
    """Report embedded-watcher opt-in and thread liveness from the request's app state."""
    # Lazy: app.py imports this module at load time. The embedded watcher's concurrency is
    # fixed in app.py and the standalone one is a CLI flag, so neither is reported here.
    from mailroom_reloaded.api.app import _embed_watcher_enabled

    thread = getattr(request.app.state, "watcher_thread", None)
    return {
        "embedded": _embed_watcher_enabled(),
        "running": bool(thread is not None and thread.is_alive()),
    }


def _pager() -> dict[str, Any]:
    """Summarize pager transport, embed opt-in and credential-presence environment settings."""
    # The sender's on/off state lives in the pager settings file, which this route does not read.
    transport = _env("MAILROOM_PAGER_TRANSPORT").lower() or _PAGER_DEFAULT_TRANSPORT
    return {
        "embed": _env("MAILROOM_PAGER_EMBED") != "0",
        "transport": transport if transport in _PAGER_TRANSPORTS else "invalid",
        "twilio_auth_token": _env_set_unset("TWILIO_AUTH_TOKEN"),
        "smtp_password": _env_set_unset("MAILROOM_SMTP_PASSWORD"),
        "webhook_secret": _env_set_unset("MAILROOM_PAGER_WEBHOOK_SECRET"),
    }


def build_config(request: Request) -> dict[str, Any]:
    """The allow-listed, secret-free settings summary (see the module docstring).

    Read settings, environment variables, taxonomy and the request's embedded-watcher
    state without writing configuration or calling providers. Upload limits are in
    bytes; the GPU cost rate is in USD per hour. Paths report presence only.

    Jev and taxonomy failures become ``unavailable`` sections with exception type
    names. Validation errors from loading the main settings propagate.
    """
    from mailroom_reloaded.api.app import MAX_UPLOAD_BYTES

    s = get_settings()
    return {
        "schema_version": SCHEMA_VERSION,
        "service": "mailroom",
        "version": __version__,
        "api": {
            "token": _set_unset((s.api_token or "").strip()),
            "max_upload_bytes": MAX_UPLOAD_BYTES,
        },
        "provider": {
            "name": _enum(s.provider, _PROVIDERS),
            "openrouter_base_host": _host_only(_env("OPENROUTER_BASE_URL") or _OPENROUTER_DEFAULT_BASE),
            "vllm_base_host": _host_only(s.vllm_base_url),
            "llamafile_base_host": _host_only(s.llamafile_base_url),
            "openrouter_api_key": _set_unset(s.openrouter_api_key),
            "vllm_api_key": _env_set_unset("VLLM_API_KEY"),
        },
        "jev": _guarded(_jev),
        "taxonomy": _guarded(_taxonomy),
        "storage": {
            "base_dir": _set_unset(s.base_dir),
            "anchor": _enum(str(s.anchor), _ANCHOR_MODES),
            "anchor_url": _set_unset(s.anchor_url),
            "anchor_key": _set_unset(s.anchor_key),
            "anchor_key_file": _set_unset(s.anchor_key_file),
        },
        "tracing": {
            "otlp_endpoint": _env_set_unset("OTEL_EXPORTER_OTLP_ENDPOINT"),
            "otlp_traces_endpoint": _env_set_unset("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"),
            "phoenix_project": s.phoenix_project or "mailroom-live",
            "trace_mask": bool(s.trace_mask),
            "trace_keep": s.trace_keep,
            "trace_store_path": _set_unset(s.trace_store_path),
        },
        "links": {
            "public_url": s.public_url,
            "phoenix_url": s.phoenix_url,
            "grafana_url": s.grafana_url,
        },
        "watcher": _watcher(request),
        "pager": _pager(),
        "run": {"gpu_usd_per_hour": s.gpu_usd_per_hour},
    }


@router.get("/config", dependencies=[Depends(_gate)])
def config_endpoint(request: Request) -> JSONResponse:
    """Return the read-only settings summary with ``Cache-Control: no-store``.

    The route's dependency enforces the configured bearer token. Errors not converted
    to section statuses by ``build_config`` propagate.
    """
    return JSONResponse(build_config(request), headers={"Cache-Control": "no-store"})
