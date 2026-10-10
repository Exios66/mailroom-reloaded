"""Document catalog (one row per doc_id)."""

from __future__ import annotations

import time

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import OperationalError

from mailroom_reloaded.schemas.audit import CatalogRecord
from mailroom_reloaded.storage.db import catalog_table as t
from mailroom_reloaded.storage.db import get_engine

__all__ = ["CatalogRecord", "get", "list", "upsert"]

_list = list


def _to_record(r) -> CatalogRecord:
    """Validate a database row as a catalog record; validation errors propagate."""
    return CatalogRecord(**r._mapping)


def upsert(record: CatalogRecord, *, engine: Engine | None = None, retries: int = 3) -> None:
    """Insert a record or replace all stored fields for its ``doc_id``.

    Use the default database when ``engine`` is omitted. Retries up to ``retries``
    times on transient database errors (locked, busy) with exponential backoff.
    Permanent errors (constraint, validation) still propagate immediately.
    """
    engine = engine or get_engine()
    values = record.model_dump(mode="json")
    stmt = insert(t).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[t.c.doc_id],
        set_={k: v for k, v in values.items() if k != "doc_id"},
    )
    for attempt in range(retries):
        try:
            with engine.begin() as conn:
                conn.execute(stmt)
            return  # success
        except OperationalError as exc:
            # Transient errors: database is locked, busy, or temporarily unavailable
            if attempt < retries - 1 and any(msg in str(exc).lower() 
                    for msg in ("locked", "busy", "disk", "ioerror")):
                backoff_ms = min(2 ** attempt * 100, 1000)  # exponential backoff, capped at 1s
                time.sleep(backoff_ms / 1000.0)
                continue
            # Permanent error or last retry; let it propagate
            raise


def get(doc_id: str, *, engine: Engine | None = None) -> CatalogRecord | None:
    """Return a document's catalog record, or ``None`` if absent.

    Use the default database when ``engine`` is omitted. Database and record
    validation errors propagate.
    """
    engine = engine or get_engine()
    with engine.connect() as conn:
        r = conn.execute(select(t).where(t.c.doc_id == doc_id)).first()
    return _to_record(r) if r else None


def list(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    *,
    engine: Engine | None = None,
) -> _list[CatalogRecord]:
    """Return records ordered by document ID, with optional exact status filtering.

    ``limit`` and ``offset`` count records. Use the default database when
    ``engine`` is omitted; database and record validation errors propagate.
    """
    engine = engine or get_engine()
    q = select(t).order_by(t.c.doc_id).limit(limit).offset(offset)
    if status is not None:
        q = q.where(t.c.status == status)
    with engine.connect() as conn:
        return [_to_record(r) for r in conn.execute(q).all()]
