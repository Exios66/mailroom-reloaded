"""Run scope: which run a unit of work belongs to (metric labels, ledger, spans).

A :class:`RunScope` lives in a ``ContextVar``. ContextVars propagate into
``asyncio`` tasks and ``asyncio.to_thread`` but **not** into a
``ThreadPoolExecutor`` worker, so the scope is opened inside the worker
(``run_document``) rather than around the pool, and ``run_eval`` opens it before
``asyncio.run`` so every eval task inherits it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "UNSCOPED",
    "RunScope",
    "current_run",
    "ensure_run_scope",
    "environment_name",
    "live_run_id",
    "run_scope",
]

#: ``run_id`` reported for work done outside any scope.
UNSCOPED = "unscoped"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class RunScope:
    """Identity of the run the current work belongs to."""

    run_id: str
    environment: str = "live"
    source: str = "watch"
    session_id: str | None = None


_CURRENT: ContextVar[RunScope | None] = ContextVar("mailroom_run_scope", default=None)


def current_run() -> RunScope | None:
    """The active scope, or ``None`` outside any ``run_scope``."""
    return _CURRENT.get()


def _safe_run_id(run_id: str) -> str:
    """``run_id`` if it matches ``[A-Za-z0-9_-]{1,64}``, else a sanitised form."""
    if _RUN_ID_RE.fullmatch(run_id):
        return run_id
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", run_id)[:64]
    return cleaned or UNSCOPED


def live_run_id(now: datetime | None = None) -> str:
    """Run id for live traffic: ``MAILROOM_RUN_ID`` or the daily bucket ``live-<YYYYMMDD>``."""
    configured = (os.environ.get("MAILROOM_RUN_ID") or "").strip()
    if configured:
        return _safe_run_id(configured)
    return f"live-{(now or datetime.now(UTC)):%Y%m%d}"


@contextmanager
def run_scope(
    run_id: str,
    environment: str = "live",
    source: str = "watch",
    session_id: str | None = None,
) -> Iterator[RunScope]:
    """Make ``run_id`` the current run for the duration of the block (nestable)."""
    scope = RunScope(_safe_run_id(run_id), environment, source, session_id)
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


def environment_name() -> str:
    """Deployment environment label: ``MAILROOM_ENVIRONMENT`` (default ``live``)."""
    return _safe_run_id((os.environ.get("MAILROOM_ENVIRONMENT") or "live").strip() or "live")


@contextmanager
def ensure_run_scope(source: str = "watch", run_id: str | None = None) -> Iterator[RunScope]:
    """Keep the active scope, or open one when there is none.

    With ``run_id`` (an eval run) the new scope is that run in the ``eval``
    environment; otherwise it is the live daily-bucket scope.
    """
    existing = _CURRENT.get()
    if existing is not None:
        yield existing
        return
    if run_id:
        opened = run_scope(run_id, "eval", source, session_id=f"eval-{run_id}")
    else:
        opened = run_scope(live_run_id(), environment_name(), source)
    with opened as scope:
        yield scope
