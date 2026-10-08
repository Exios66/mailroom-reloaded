"""SQLite engine, schema and the lazily created default engine."""

from __future__ import annotations

import threading
from pathlib import Path

from sqlalchemy import (
    Column,
    Engine,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
)

from mailroom_reloaded.settings import get_settings

metadata = MetaData()

audit_table = Table(
    "audit_log",
    metadata,
    Column("doc_id", String, primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("node", String, nullable=False),
    Column("event", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("ts", String, nullable=False),
    Column("prev_hash", String, nullable=False),
    Column("entry_hash", String, nullable=False),
)

catalog_table = Table(
    "catalog",
    metadata,
    Column("doc_id", String, primary_key=True),
    Column("filename", String, nullable=False),
    Column("doc_type", String),
    Column("doc_subclass", String),
    Column("status", String, nullable=False),
    Column("archive_path", String),
    Column("file_sha256", String),
    Column("updated_at", String, nullable=False),
)
Index("ix_catalog_status", catalog_table.c.status)

_default_engine: Engine | None = None
_lock = threading.Lock()


def _pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def init_db(path: str | Path) -> Engine:
    """Create (if needed) the SQLite DB at ``path`` in WAL mode and return an engine."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})
    event.listen(engine, "connect", _pragmas)
    metadata.create_all(engine)
    return engine


def get_engine() -> Engine:
    """Module-level default engine at ``<base_dir>/mailroom.db``, created lazily."""
    global _default_engine
    with _lock:
        if _default_engine is None:
            _default_engine = init_db(get_settings().base_dir / "mailroom.db")
        return _default_engine
