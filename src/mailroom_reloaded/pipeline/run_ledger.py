"""Ledger intake: turn finished documents and runs into archive-ledger entries.

Every function here is best effort. A failure is logged and swallowed, so the
ledger can never fail a document, change a document's outcome, or mask the
exception that ended it (``tests/pipeline/test_ledger_intake.py``).

* ``open_run`` / ``close_run`` write ``run_opened`` / ``run_closed``.
* ``record_document`` writes one ``doc_closed`` per document **invocation**: the
  spend is the delta against a baseline taken when the invocation started (a resumed
  document restores its earlier ``usage_total`` from the manifest), and
  ``audit_head`` pins the document's ``audit_log`` chain head so the two chains are
  linked.
* Live traffic uses one daily run (``live-<YYYYMMDD>``); the first document of a new
  run id closes the previous live run lazily, and the watcher's startup closes runs
  a dead process left open.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from mailroom_reloaded.llm.client import cost_for
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.obs.attrs import failure_class_for, failure_reason_for
from mailroom_reloaded.obs.run_context import RunScope, live_run_id
from mailroom_reloaded.schemas.ledger import canonical_json
from mailroom_reloaded.settings import get_settings, load_taxonomy
from mailroom_reloaded.storage import audit_log
from mailroom_reloaded.storage.ledger import Ledger, MetricRow, get_ledger

__all__ = [
    "Baseline",
    "close_other_live_runs",
    "close_run",
    "ensure_live_run",
    "ledger_for",
    "open_run",
    "record_aborted",
    "record_cell",
    "record_document",
    "record_reconciled",
    "snapshot",
]

logger = structlog.get_logger(__name__)

_lock = threading.Lock()
#: runs opened (or found open) by this process and not yet closed: run_id -> kind
_open: dict[str, str] = {}
#: live runs for which this process already did its rollover check
_rolled: set[str] = set()
#: runs closed (or found closed) by this process: they never receive another document
_closed: set[str] = set()
#: invocations recorded per (run_id, doc_id) by this process, seeded from the ledger
_invocations: dict[tuple[str, str], int] = {}
_OUTCOME = {"archived": "completed", "failed": "failed", "parked": "parked"}
_REASON_FOR_CLASS = {
    "llm_timeout": "llm_error",
    "llm_auth": "llm_error",
    "llm_rate_limit": "llm_error",
    "llm_transient": "llm_error",
    "io_error": "io_error",
    "schema_error": "schema_invalid",
    "run_budget": "deadline_exceeded",
}


def _reason_and_class(failure: str | BaseException) -> tuple[str, str]:
    """Bounded ``(failure_reason, failure_class)`` for a reason string or an exception."""
    if isinstance(failure, str):
        return failure_reason_for(failure), failure_class_for(failure)
    cls = failure_class_for(failure)
    return _REASON_FOR_CLASS.get(cls, "unexpected"), cls


def ledger_for(overrides: dict[str, Any] | None = None) -> Ledger:
    """The ledger passed through ``overrides`` (like ``bins``), else the process default."""
    return (overrides or {}).get("ledger") or get_ledger()


@dataclass
class Baseline:
    """What the document already carried when this invocation started."""

    usage_total: Usage = field(default_factory=Usage)
    by_role: dict[str, Usage] = field(default_factory=dict)
    partial_nodes: tuple[str, ...] = ()
    started: float = field(default_factory=time.monotonic)


def snapshot(state: Any) -> Baseline:
    """Capture the usage already on ``state`` (restored from the manifest on resume)."""
    return Baseline(
        usage_total=state.usage_total,
        by_role=dict(state.usage_by_role),
        partial_nodes=tuple(state.usage_partial_nodes),
    )


def _config_sha() -> str:
    return hashlib.sha256(
        canonical_json(load_taxonomy().raw).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- run level
def open_run(
    ledger: Ledger,
    run_id: str,
    kind: str,
    *,
    mode: str = "pipeline",
    posture_label: str = "",
    model: str = "",
    prompt_set: str = "",
    environment: str = "",
    source: str = "",
) -> bool:
    """Write ``run_opened`` once per run id. Returns whether the run is open for new documents.

    A run id has exactly one ``run_opened`` / ``run_closed`` pair: an id that was already
    closed is never reopened (``False``), and a failed append leaves the run unregistered
    so the next document retries.
    """
    try:
        with _lock:  # rare path (first document of a run per process), so the DB calls stay inside
            if run_id in _closed:
                return False
            if run_id in _open:
                return True
            if ledger.count("run_opened", run_id) > 0:
                if run_id in ledger.open_runs():
                    _open[run_id] = (
                        kind  # an earlier process opened it and nobody closed it
                    )
                    return True
                _closed.add(run_id)
                return False
            opened = ledger.append(
                "run_opened",
                run_id,
                payload={
                    "kind": kind,
                    "mode": mode,
                    "posture_label": posture_label,
                    "model": model,
                    "prompt_set": prompt_set,
                    "config_sha": _config_sha(),
                    "environment": environment,
                    "source": source,
                    "pid": os.getpid(),
                },
            )
            if opened:
                _open[run_id] = kind
            return opened
    except Exception:
        logger.warning("ledger_open_run_failed", run_id=run_id, exc_info=True)
        return False


def close_run(
    ledger: Ledger,
    run_id: str,
    closed_by: str = "completed",
    *,
    expected: int | None = None,
    counts: dict[str, int] | None = None,
) -> None:
    """Write ``run_closed`` (the ledger adds ``docs``, the Merkle root and ``dropped_rows``)."""
    try:
        with _lock:
            _open.pop(run_id, None)
            _closed.add(run_id)
        payload: dict[str, Any] = {"closed_by": closed_by, "counts": counts or {}}
        if expected is not None:
            payload["expected"] = expected
        ledger.append("run_closed", run_id, payload=payload)
    except Exception:
        logger.warning("ledger_close_run_failed", run_id=run_id, exc_info=True)


def _closed_snapshot() -> frozenset[str]:
    with _lock:
        return frozenset(_closed)


def ensure_live_run(ledger: Ledger, scope: RunScope) -> str | None:
    """Open the live run for ``scope`` and lazily close any other live run (rollover).

    Returns the run id a document finishing now should be recorded under: ``scope.run_id``
    normally, but the current live bucket when ``scope.run_id`` was already closed (a
    document that outlived its day's rollover must not append after ``run_closed``).
    Returns ``None`` if that run cannot be opened, including a closed date bucket.
    """
    try:
        if scope.environment == "eval":
            return scope.run_id  # eval runs are opened and closed by run_eval
        run_id = scope.run_id
        with _lock:
            stale = run_id in _closed
        if stale:
            run_id = live_run_id()
            if run_id in _closed_snapshot():
                run_id = f"live-{datetime.now(UTC):%Y%m%d}"  # never reopen: fall back to the date bucket
        if not open_run(
            ledger,
            run_id,
            "live",
            mode="live",
            posture_label="live",
            model=get_settings().provider,
            prompt_set="frozen_v1",
            environment=scope.environment,
            source=scope.source,
        ):
            return None
        with _lock:
            first = run_id not in _rolled
            _rolled.add(run_id)
        if (
            first
        ):  # one rollover check per run id per process, not one query per document
            close_other_live_runs(ledger, run_id)
        return run_id
    except Exception:
        logger.warning("ledger_ensure_live_run_failed", exc_info=True)
        return None


def close_other_live_runs(ledger: Ledger, keep_run_id: str) -> None:
    """Close every open live run except ``keep_run_id``.

    A run this process opened closes as ``completed`` (a normal rollover); a run
    found open in the ledger that this process never opened was left by a process
    that died or stopped, and closes as ``interrupted``.
    """
    try:
        with _lock:
            mine = {r for r, k in _open.items() if k == "live" and r != keep_run_id}
        stale = [
            r for r in ledger.open_runs("live") if r != keep_run_id and r not in mine
        ]
        for run_id in sorted(mine):
            close_run(ledger, run_id, "completed")
        for run_id in stale:
            close_run(ledger, run_id, "interrupted")
    except Exception:
        logger.warning("ledger_rollover_failed", exc_info=True)


# --------------------------------------------------------------------------- documents
def _next_invocation(ledger: Ledger, run_id: str, doc_id: str) -> int:
    key = (run_id, doc_id)
    with _lock:
        if key not in _invocations:
            try:
                _invocations[key] = ledger.count("doc_closed", run_id, doc_id)
            except Exception:  # noqa: BLE001
                _invocations[key] = 0
        _invocations[key] += 1
        return _invocations[key]


def _audit_head(doc_id: str) -> dict[str, Any] | None:
    chain = audit_log.entries(doc_id)
    return {"seq": chain[-1].seq, "entry_hash": chain[-1].entry_hash} if chain else None


def _role_deltas(state: Any, base: Baseline) -> dict[str, Usage]:
    out: dict[str, Usage] = {}
    for role, usage in state.usage_by_role.items():
        delta = usage - base.by_role.get(role, Usage())
        if delta != Usage():
            out[role] = delta
    return out


def doc_metric_rows(
    state: Any,
    *,
    duration_s: float,
    deltas: dict[str, Usage],
    total: Usage,
    cost: float,
    outcome: str,
) -> list[MetricRow]:
    """The flat metric rows of one document invocation (tiers 0-2; more join with span capture)."""
    rows = [
        MetricRow("doc.duration_s", round(duration_s, 4), 0),
        MetricRow("doc.completed", 1.0 if outcome == "completed" else 0.0, 0),
        MetricRow("usage.total_tokens", total.total_tokens, 1),
        MetricRow("usage.calls", total.calls, 1),
        MetricRow("usage.cost_usd", round(cost, 8), 1),
        MetricRow("attempts.classify", state.classify_attempts, 1),
        MetricRow("attempts.extract", state.extract_attempts, 1),
        MetricRow("route.steps", len(state.route_trail), 1),
        MetricRow("route.resorted", 1.0 if state.resorted else 0.0, 1),
    ]
    if state.sort is not None:
        rows.append(MetricRow("confidence.sort", state.sort.confidence, 1))
    if state.extract is not None and state.extract.confidence is not None:
        rows.append(MetricRow("confidence.extract", state.extract.confidence, 1))
    for role, usage in deltas.items():
        rows += [
            MetricRow(f"usage.{role}.prompt_tokens", usage.prompt_tokens, 2),
            MetricRow(f"usage.{role}.completion_tokens", usage.completion_tokens, 2),
            MetricRow(f"usage.{role}.calls", usage.calls, 2),
            MetricRow(f"usage.{role}.cost_usd", round(cost_for(role, usage), 8), 2),
        ]
    return rows


def record_document(
    ledger: Ledger,
    scope: RunScope,
    state: Any,
    base: Baseline,
    *,
    doc_type: str | None,
    failure: str | BaseException | None = None,
    aborted: bool = False,
) -> None:
    """Write the ``doc_closed`` entry of one document invocation."""
    try:
        run_id = ensure_live_run(
            ledger, scope
        )  # a rollover may have closed the run mid-document
        if run_id is None:
            return
        outcome = "aborted" if aborted else _OUTCOME.get(state.status, "aborted")
        duration = time.monotonic() - base.started
        deltas = _role_deltas(state, base)
        total = state.usage_total - base.usage_total
        cost = sum(
            cost_for(role, usage) for role, usage in deltas.items() if role != "grader"
        )
        new_partial = [
            n for n in state.usage_partial_nodes if n not in base.partial_nodes
        ]
        rows = doc_metric_rows(
            state,
            duration_s=duration,
            deltas=deltas,
            total=total,
            cost=cost,
            outcome=outcome,
        )
        payload: dict[str, Any] = {
            "invocation": _next_invocation(ledger, run_id, state.doc_id),
            "outcome": outcome,
            "doc_type": doc_type,
            "audit_head": _audit_head(state.doc_id),
            "metrics_digest": hashlib.sha256(
                canonical_json([[r.name, r.value, r.tier] for r in rows]).encode(
                    "utf-8"
                )
            ).hexdigest(),
            "rows": len(rows),
            "usage_by_role": {
                role: {
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "calls": u.calls,
                    "cost_usd": round(cost_for(role, u), 8),
                }
                for role, u in deltas.items()
            },
            "usage_complete": not new_partial,
            "usage_partial_nodes": new_partial,
            "route_trail": list(state.route_trail),
            "duration_s": round(duration, 4),
        }
        if outcome in {"failed", "aborted"} and failure is not None:
            payload["failure_reason"], payload["failure_class"] = _reason_and_class(
                failure
            )
        ledger.append(
            "doc_closed",
            run_id,
            doc_id=state.doc_id,
            payload=payload,
            metrics=rows,
        )
    except Exception:
        logger.warning("ledger_record_document_failed", exc_info=True)


def record_aborted(
    ledger: Ledger,
    run_id: str,
    doc_id: str,
    failure: BaseException,
    *,
    doc_type: str | None = None,
    started: float | None = None,
    usage_by_role: dict[str, Usage] | None = None,
    usage_complete: bool = False,
) -> None:
    """``doc_closed`` for a document that never reached (or left) the flow, e.g. an eval error row."""
    try:
        deltas = {r: u for r, u in (usage_by_role or {}).items() if u != Usage()}
        duration = time.monotonic() - started if started is not None else 0.0
        payload = {
            "invocation": _next_invocation(ledger, run_id, doc_id),
            "outcome": "aborted",
            "doc_type": doc_type,
            "audit_head": _audit_head(doc_id),
            "usage_by_role": {
                role: {
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "calls": u.calls,
                    "cost_usd": round(cost_for(role, u), 8),
                }
                for role, u in deltas.items()
            },
            "usage_complete": usage_complete,
            "duration_s": round(duration, 4),
        }
        payload["failure_reason"], payload["failure_class"] = _reason_and_class(failure)
        ledger.append("doc_closed", run_id, doc_id=doc_id, payload=payload)
    except Exception:
        logger.warning("ledger_record_aborted_failed", exc_info=True)


def record_reconciled(
    ledger: Ledger, run_id: str, doc_id: str, doc_type: str | None = None
) -> None:
    """``doc_closed`` for a document finished by startup reconciliation (its spend is unknown)."""
    try:
        payload = {
            "invocation": _next_invocation(ledger, run_id, doc_id),
            "outcome": "reconciled",
            "doc_type": doc_type,
            "audit_head": _audit_head(doc_id),
            "usage_complete": False,
        }
        ledger.append("doc_closed", run_id, doc_id=doc_id, payload=payload)
    except Exception:
        logger.warning("ledger_record_reconciled_failed", exc_info=True)


def record_cell(
    ledger: Ledger,
    run_id: str,
    doc_id: str,
    doc_type: str,
    result: Any,
    *,
    started: float | None = None,
) -> None:
    """``doc_closed`` for a ``specialist_cell`` eval row (it never touches the flow)."""
    try:
        classes = load_taxonomy().classes
        role = classes[doc_type].specialist if doc_type in classes else "specialist"
        usage: Usage = result.usage
        payload = {
            "invocation": _next_invocation(ledger, run_id, doc_id),
            "outcome": "completed" if result.schema_valid else "failed",
            "doc_type": doc_type,
            "audit_head": _audit_head(doc_id),
            "usage_by_role": {
                role: {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "calls": usage.calls,
                    "cost_usd": round(cost_for(role, usage), 8),
                }
            },
            "usage_complete": True,
            "duration_s": round(time.monotonic() - started, 4)
            if started is not None
            else 0.0,
        }
        if not result.schema_valid:
            payload["failure_reason"] = "schema_invalid"
            payload["failure_class"] = "schema_error"
        ledger.append("doc_closed", run_id, doc_id=doc_id, payload=payload)
    except Exception:
        logger.warning("ledger_record_cell_failed", exc_info=True)
