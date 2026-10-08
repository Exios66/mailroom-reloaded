"""Document catalog (one row per doc_id)."""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert

from mailroom_reloaded.schemas.audit import CatalogRecord
from mailroom_reloaded.storage.db import catalog_table as t
from mailroom_reloaded.storage.db import get_engine

__all__ = ["CatalogRecord", "get", "list", "upsert"]

_list = list


def _to_record(r) -> CatalogRecord:
    return CatalogRecord(**r._mapping)


def upsert(record: CatalogRecord, *, engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    values = record.model_dump(mode="json")
    stmt = insert(t).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[t.c.doc_id],
        set_={k: v for k, v in values.items() if k != "doc_id"},
    )
    with engine.begin() as conn:
        conn.execute(stmt)


def get(doc_id: str, *, engine: Engine | None = None) -> CatalogRecord | None:
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
    engine = engine or get_engine()
    q = select(t).order_by(t.c.doc_id).limit(limit).offset(offset)
    if status is not None:
        q = q.where(t.c.status == status)
    with engine.connect() as conn:
        return [_to_record(r) for r in conn.execute(q).all()]
