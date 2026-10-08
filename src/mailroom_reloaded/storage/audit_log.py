"""Append-only, hash-chained audit log."""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError

from mailroom_reloaded.schemas.audit import (
    AuditLogEntry,
    ChainResult,
    compute_entry_hash,
)
from mailroom_reloaded.storage.db import audit_table as t
from mailroom_reloaded.storage.db import get_engine


def _row_to_entry(r) -> AuditLogEntry:
    return AuditLogEntry(
        doc_id=r.doc_id,
        seq=r.seq,
        node=r.node,
        event=r.event,
        payload=json.loads(r.payload),
        ts=r.ts,
        prev_hash=r.prev_hash,
        entry_hash=r.entry_hash,
    )


def append(
    doc_id: str,
    node: str,
    event: str,
    payload: dict | None = None,
    *,
    engine: Engine | None = None,
) -> AuditLogEntry:
    """Append an entry. A repeat of (doc_id, node, event) with an identical payload
    returns the existing entry and stores nothing (resume-safe).

    Note: the chain detects edits, deletions in the middle and reordering, but
    truncation of the tail is undetectable without an external anchor."""
    engine = engine or get_engine()
    payload = payload or {}
    for _ in range(50):
        try:
            with engine.connect() as conn:
                # Take the write lock before reading so concurrent appenders serialize
                # (busy_timeout makes waiters block rather than fail).
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                rows = conn.execute(
                    select(t).where(t.c.doc_id == doc_id).order_by(t.c.seq)
                ).all()
                for r in rows:
                    if r.node == node and r.event == event:
                        existing = _row_to_entry(r)
                        if existing.payload == json.loads(
                            json.dumps(payload, default=str)
                        ):
                            conn.rollback()
                            return existing
                last = rows[-1] if rows else None
                entry = AuditLogEntry(
                    doc_id=doc_id,
                    seq=(last.seq + 1) if last else 1,
                    node=node,
                    event=event,
                    payload=json.loads(json.dumps(payload, default=str)),
                    prev_hash=last.entry_hash if last else "",
                )
                entry.entry_hash = compute_entry_hash(entry)
                conn.execute(
                    t.insert().values(
                        doc_id=doc_id,
                        seq=entry.seq,
                        node=node,
                        event=event,
                        payload=json.dumps(entry.payload, sort_keys=True),
                        ts=entry.ts.isoformat(),
                        prev_hash=entry.prev_hash,
                        entry_hash=entry.entry_hash,
                    )
                )
                conn.commit()
                return entry
        except IntegrityError:
            continue  # concurrent writer took this seq; retry
    raise RuntimeError(f"audit append failed for {doc_id} after retries")


def entries(doc_id: str, *, engine: Engine | None = None) -> list[AuditLogEntry]:
    engine = engine or get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(t).where(t.c.doc_id == doc_id).order_by(t.c.seq)
        ).all()
    return [_row_to_entry(r) for r in rows]


def verify_chain(chain: Sequence[AuditLogEntry]) -> ChainResult:
    """Return the seq of the first entry whose hash or link fails verification."""
    prev = ""
    expected_seq = 1
    for e in chain:
        if (
            e.seq != expected_seq
            or e.prev_hash != prev
            or compute_entry_hash(e) != e.entry_hash
        ):
            return ChainResult(ok=False, broken_at=e.seq)
        prev = e.entry_hash
        expected_seq += 1
    return ChainResult(ok=True)
