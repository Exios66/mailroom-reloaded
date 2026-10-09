"""Replay session ids and the session picker.

A session id is ``<kind>:<key>`` with kind one of ``run``, ``session``, ``doc``,
``window``; a bare id means ``run:``. :func:`list_sessions` merges what the span
store knows (exact) with what the SQLite database knows (``eval_docs`` and the
archive ledger, approximate) into the rows of ``GET /v1/replay/sessions``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from mailroom_reloaded.schemas.replay import SessionKind, SessionSummary
from mailroom_reloaded.storage.db import get_engine, ledger_table
from mailroom_reloaded.storage.span_store import (
    SpanStore,
    default_span_store_path,
    spans_table,
)

__all__ = [
    "MAX_ID_LEN",
    "MAX_LIMIT",
    "format_session_id",
    "list_sessions",
    "parse_session_id",
    "window_bounds_ns",
]

logger = structlog.get_logger(__name__)

MAX_ID_LEN = 200
MAX_LIMIT = 500
_KINDS: tuple[str, ...] = ("run", "session", "doc", "window")
#: Conservative ASCII set: letters, digits and ``. _ : / @ + = -``. ``fullmatch`` so a
#: trailing newline cannot slip through.
_KEY_RE = re.compile(r"[A-Za-z0-9._:/@+=-]+")
_WINDOW_RE = re.compile(r"([0-9]{1,20})-([0-9]{1,20})")
_EVAL_RE = re.compile(r"eval[-_]")
#: SQLite integers are signed 64-bit; larger bounds overflow when bound as a parameter.
_MAX_NS = 2**63 - 1


def parse_session_id(raw: str) -> tuple[str, str]:
    """Split ``raw`` into ``(kind, key)``.

    Accepted prefixes are ``run:``, ``session:``, ``doc:`` and ``window:``; anything
    else (including ids that merely contain a colon) is a bare run id. ``key`` uses
    ``[A-Za-z0-9._:/@+=-]``, at most 200 characters in all. A ``window:`` key is
    ``<from_ns>-<to_ns>``: two epoch-nanosecond integers with ``from < to``.

    Raises ``ValueError`` for empty, over-long or out-of-charset ids (control
    characters, whitespace, quotes and non-ASCII included).
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_ID_LEN:
        raise ValueError("invalid session id")
    kind, key = "run", raw
    head, sep, rest = raw.partition(":")
    if sep and head in _KINDS:
        kind, key = head, rest
    if not key or not _KEY_RE.fullmatch(key):
        raise ValueError("invalid session id")
    if kind == "window":
        window_bounds_ns(key)
    return kind, key


def format_session_id(kind: str, key: str) -> str:
    """The canonical ``kind:key`` form."""
    return f"{kind}:{key}"


def window_bounds_ns(key: str) -> tuple[int, int]:
    """``(from_ns, to_ns)`` of a ``window:`` key; ``ValueError`` when malformed."""
    m = _WINDOW_RE.fullmatch(key)
    if m is None:
        raise ValueError("invalid session id")
    lo, hi = int(m.group(1)), int(m.group(2))
    if lo >= hi or hi > _MAX_NS:
        raise ValueError("invalid session id")
    return lo, hi


def _environment_for(run_id: str) -> str:
    return "eval" if _EVAL_RE.match(run_id) else "live"


def _iso_from_ns(ns: int | None) -> str:
    if not ns:
        return ""
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class _Row:
    summary: SessionSummary
    end_epoch: float


def _from_ns_row(
    sid: str,
    kind: SessionKind,
    env: str,
    docs: int,
    first_ns: int | None,
    last_ns: int | None,
) -> _Row:
    first, last = first_ns or 0, last_ns or first_ns or 0
    return _Row(
        SessionSummary(
            id=sid,
            kind=kind,
            environment=env,
            documents=int(docs or 0),
            started_at=_iso_from_ns(first),
            duration_s=round(max(last - first, 0) / 1e9, 3),
            source="spans",
        ),
        last / 1e9,
    )


def _span_rows(store: SpanStore, limit: int) -> list[_Row]:
    rows: list[_Row] = []
    for r in store.list_runs(limit):
        run_id = str(r["run_id"])
        rows.append(
            _from_ns_row(
                format_session_id("run", run_id),
                "run",
                _environment_for(run_id),
                r["docs"],
                r["first_ns"],
                r["last_ns"],
            )
        )
    t = spans_table
    q = (
        select(
            t.c.session_id,
            func.count(func.distinct(t.c.doc_id)).label("docs"),
            func.min(t.c.start_ns).label("first_ns"),
            func.max(t.c.end_ns).label("last_ns"),
        )
        .where(t.c.session_id.is_not(None))
        .group_by(t.c.session_id)
        .order_by(func.max(t.c.end_ns).desc())
        .limit(limit)
    )
    with store.engine.connect() as conn:
        for r in conn.execute(q):
            sid = str(r.session_id)
            try:
                kind, key = parse_session_id(f"session:{sid}")
            except ValueError:
                continue  # a hostile stored id never reaches the picker
            rows.append(
                _from_ns_row(
                    format_session_id(kind, key),
                    "session",
                    _environment_for(key),
                    r.docs,
                    r.first_ns,
                    r.last_ns,
                )
            )
    return rows


def _audit_row(
    run_id: str, docs: int, first: str | None, last: str | None, env: str | None
) -> _Row | None:
    try:
        kind, key = parse_session_id(f"run:{run_id}")
    except ValueError:
        return None
    t0, t1 = _parse_iso(first), _parse_iso(last)
    duration = (t1 - t0).total_seconds() if t0 and t1 else 0.0
    return _Row(
        SessionSummary(
            id=format_session_id(kind, key),
            kind="run",
            environment=env or _environment_for(key),
            documents=int(docs or 0),
            started_at=t0.isoformat() if t0 else "",
            duration_s=round(max(duration, 0.0), 3),
            source="audit",
        ),
        (t1.timestamp() if t1 else 0.0),
    )


def _audit_rows(engine: Engine, limit: int) -> list[_Row]:
    rows: list[_Row] = []
    # eval_docs is created lazily by the eval runner; absent before the first run.
    eval_q = text(
        "SELECT e.run_id AS run_id, COUNT(DISTINCT e.filename) AS docs, "
        "MIN(a.ts) AS first_ts, MAX(a.ts) AS last_ts "
        "FROM eval_docs e LEFT JOIN audit_log a ON a.doc_id = e.doc_id "
        "GROUP BY e.run_id ORDER BY MAX(a.ts) DESC LIMIT :n"
    )
    ledger_q = (
        select(
            ledger_table.c.run_id,
            func.count(func.distinct(ledger_table.c.doc_id)).label("docs"),
            func.min(ledger_table.c.ts).label("first_ts"),
            func.max(ledger_table.c.ts).label("last_ts"),
        )
        .group_by(ledger_table.c.run_id)
        .order_by(func.max(ledger_table.c.ts).desc())
        .limit(limit)
    )
    with engine.connect() as conn:
        try:
            for r in conn.execute(eval_q, {"n": limit}):
                row = _audit_row(r.run_id, r.docs, r.first_ts, r.last_ts, "eval")
                if row:
                    rows.append(row)
        except SQLAlchemyError:
            logger.debug("replay_sessions_no_eval_docs")
        try:
            for r in conn.execute(ledger_q):
                row = _audit_row(r.run_id, r.docs, r.first_ts, r.last_ts, None)
                if row:
                    rows.append(row)
        except SQLAlchemyError:
            logger.debug("replay_sessions_no_ledger")
    return rows


def _open_store(store: SpanStore | None) -> SpanStore | None:
    if store is not None:
        return store
    try:
        path = default_span_store_path()
        return SpanStore(path) if path.exists() else None
    except Exception:
        logger.debug("replay_sessions_no_span_store", exc_info=True)
        return None


def list_sessions(
    limit: int = 50,
    *,
    store: SpanStore | None = None,
    engine: Engine | None = None,
) -> list[SessionSummary]:
    """Replayable sessions, newest first, at most ``limit`` (clamped to 1..500).

    Merges span-store runs and ``session.id`` values (source ``spans``) with
    ``eval_docs`` runs and archive-ledger runs (source ``audit``). Ids are canonical
    ``kind:key``; a duplicate id keeps the spans row. The environment is ``eval`` for
    ``eval-``/``eval_`` run ids, else ``live``. Each source degrades to empty on error.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    merged: dict[str, _Row] = {}

    def take(rows: list[_Row]) -> None:
        for row in rows:
            cur = merged.get(row.summary.id)
            if (
                cur is None
                or (cur.summary.source == "audit" and row.summary.source == "spans")
                or (
                    cur.summary.source == row.summary.source == "audit"
                    and row.summary.documents > cur.summary.documents
                )
            ):
                merged[row.summary.id] = row

    span_store = _open_store(store)
    if span_store is not None:
        try:
            take(_span_rows(span_store, limit))
        except Exception:
            logger.warning("replay_sessions_spans_failed", exc_info=True)
    try:
        take(_audit_rows(engine or get_engine(), limit))
    except Exception:
        logger.warning("replay_sessions_audit_failed", exc_info=True)

    ordered = sorted(merged.values(), key=lambda r: (-r.end_epoch, r.summary.id))
    return [r.summary for r in ordered[:limit]]
