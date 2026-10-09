"""Durable local span store: the replay viewer's source of truth.

A ``SpanExporter`` that writes finished spans to ``<base_dir>/traces.db`` (WAL),
separate from the hash-chained ``mailroom.db``. It never stores content:

* an **attribute allow-list** (``mailroom.*``, ``session.id``, the span kind,
  ``llm.model_name`` / ``llm.provider`` / ``llm.token_count.*`` / ``llm.cost.*``,
  ``exception.type``); everything else, notably ``llm.input_messages.*``,
  ``llm.output_messages.*`` and the ``input.value`` / ``output.value`` of LLM
  spans, is dropped whether or not ``trace_mask`` is on;
* ``input.value`` / ``output.value`` are kept only on the root and node spans,
  where they are curated summaries, and read ``<masked>`` when ``trace_mask`` is on;
* ``exception.message`` is never stored, only ``exception.type``;
* every string is bounded, and a run's span rows are capped.

Retention (which runs to keep) is decided elsewhere; this module only offers the
primitives (:meth:`SpanStore.prune`, :meth:`SpanStore.delete_runs`).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

import structlog
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from sqlalchemy import (
    BigInteger,
    Column,
    Engine,
    Index,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    event,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from mailroom_reloaded.settings import get_settings
from mailroom_reloaded.storage.db import _pragmas

__all__ = [
    "SPAN_ROWS_PER_RUN",
    "SpanStore",
    "SqliteSpanExporter",
    "default_span_store_path",
    "filter_attributes",
]

logger = structlog.get_logger(__name__)

#: Maximum span rows kept per run; later spans of that run are dropped.
SPAN_ROWS_PER_RUN = 20_000
MASKED = "<masked>"
_MAX_STR = 256
_MAX_SUMMARY = 2000
_ATTR_PREFIXES = ("mailroom.", "llm.token_count.", "llm.cost.")
_ATTR_EXACT = {
    "session.id",
    "openinference.span.kind",
    "llm.model_name",
    "llm.provider",
    "exception.type",
    "llm.invocation_parameters.max_tokens",
}
_SUMMARY_KEYS = {"input.value", "output.value"}
#: Event attribute keys the pipeline emits (plan section C). Anything else is dropped, so an
#: event can never smuggle text through an unexpected key.
_EVENT_KEYS = frozenset(
    {
        "from", "to", "reason", "kind", "attempt", "max_attempts", "confidence", "stage",
        "action", "source", "engaged", "extraction_confidence", "passes", "max_passes",
        "decision", "handoff", "fields_to_fix", "retry_count", "retry_max", "role",
        "doc_type", "error_type", "retry_in_s", "status_code", "max_tokens", "name",
        "value", "path", "outcome", "cause",
    }
)  # fmt: skip
_NODE_PREFIX = "mailroom.node."

metadata = MetaData()
spans_table = Table(
    "spans",
    metadata,
    Column("span_id", String, primary_key=True),
    Column("trace_id", String, nullable=False),
    Column("parent_id", String),
    Column("name", String, nullable=False),
    Column("kind", String),
    Column("start_ns", BigInteger, nullable=False),
    Column("end_ns", BigInteger, nullable=False),
    Column("status", String),
    Column("doc_id", String),
    Column("run_id", String),
    Column("session_id", String),
    Column("station", String),
    Column("attrs", Text, nullable=False),
    Column("events", Text, nullable=False),
)
Index("ix_spans_run", spans_table.c.run_id, spans_table.c.start_ns)
Index("ix_spans_session", spans_table.c.session_id, spans_table.c.start_ns)
Index("ix_spans_doc", spans_table.c.doc_id, spans_table.c.start_ns)
Index("ix_spans_start", spans_table.c.start_ns)

_t = spans_table
_PLAIN_COLUMNS = [c.name for c in _t.c if c.name not in ("attrs", "events")]


def default_span_store_path() -> Path:
    """``trace_store_path`` from settings, else ``<base_dir>/traces.db``."""
    settings = get_settings()
    return settings.trace_store_path or settings.base_dir / "traces.db"


def _bound(value: Any, limit: int = _MAX_STR) -> Any:
    """Bound a string, and flatten a sequence of scalars into a bounded list."""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [
            _bound(v, limit)
            for v in value[:64]
            if isinstance(v, (str, bool, int, float))
        ]
    return None


def _is_summary_span(name: str, kind: str | None) -> bool:
    return (
        kind == "CHAIN" or name == "mailroom.document" or name.startswith(_NODE_PREFIX)
    )


def filter_attributes(
    attrs: dict[str, Any], *, name: str, mask: bool = False
) -> dict[str, Any]:
    """The allow-listed, bounded attributes of one span (see the module docstring)."""
    kind = attrs.get("openinference.span.kind")
    keep_summary = _is_summary_span(name, kind if isinstance(kind, str) else None)
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if key in _SUMMARY_KEYS:
            if keep_summary and isinstance(value, str):
                out[key] = MASKED if mask else value[:_MAX_SUMMARY]
            continue
        if key in _ATTR_EXACT or key.startswith(_ATTR_PREFIXES):
            bounded = _bound(value)
            if bounded is not None:
                out[key] = bounded
    return out


def _filter_events(span: ReadableSpan) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in span.events[:200]:
        if ev.name == "exception":
            kind = (ev.attributes or {}).get("exception.type")
            out.append(
                {
                    "name": "exception",
                    "t": ev.timestamp,
                    "attrs": {"exception.type": str(kind)[:_MAX_STR]},
                }
            )
        elif ev.name.startswith("mailroom."):
            attrs = {
                k: b
                for k, v in (ev.attributes or {}).items()
                if k in _EVENT_KEYS and (b := _bound(v)) is not None
            }
            out.append({"name": ev.name[:64], "t": ev.timestamp, "attrs": attrs})
    return out


def _hex(value: int | None, width: int) -> str | None:
    return None if value is None else format(value, f"0{width}x")


class SpanStore:
    """The ``spans`` table over one SQLite file."""

    def __init__(
        self, path: str | Path, *, rows_per_run: int = SPAN_ROWS_PER_RUN
    ) -> None:
        """Open (creating if needed) the store at ``path``."""
        self.path = Path(path)
        self.rows_per_run = rows_per_run
        self._lock = threading.Lock()
        self._engine: Engine | None = None

    @property
    def engine(self) -> Engine:
        """The WAL-mode engine, created on first use."""
        if self._engine is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(
                f"sqlite:///{self.path}", connect_args={"timeout": 5}
            )
            event.listen(engine, "connect", _pragmas)
            try:
                metadata.create_all(engine)
            except (
                OperationalError
            ):  # another process created the tables between check and create
                metadata.create_all(engine)
            self._engine = engine
        return self._engine

    # ------------------------------------------------------------------ writes
    def row_for(self, span: ReadableSpan, *, mask: bool = False) -> dict[str, Any]:
        """The allow-listed row for ``span``."""
        raw = dict(span.attributes or {})
        attrs = filter_attributes(raw, name=span.name, mask=mask)
        ctx, parent = span.context, span.parent
        run_id = attrs.get("mailroom.run_id")
        return {
            "span_id": _hex(ctx.span_id, 16),
            "trace_id": _hex(ctx.trace_id, 32),
            "parent_id": _hex(parent.span_id, 16) if parent is not None else None,
            "name": span.name[:128],
            "kind": attrs.get("openinference.span.kind"),
            "start_ns": span.start_time or 0,
            "end_ns": span.end_time or span.start_time or 0,
            "status": span.status.status_code.name if span.status else None,
            "doc_id": attrs.get("mailroom.doc_id"),
            "run_id": run_id if isinstance(run_id, str) else None,
            "session_id": attrs.get("session.id"),
            "station": attrs.get("mailroom.station"),
            "attrs": json.dumps(attrs, sort_keys=True, default=str),
            "events": json.dumps(_filter_events(span), default=str),
        }

    def write(self, rows: Sequence[dict[str, Any]]) -> int:
        """Insert ``rows`` (ignoring duplicates), honouring the per-run cap. Returns rows stored.

        The cap is enforced against the database inside a ``BEGIN IMMEDIATE`` transaction, so
        separate store instances (processes) cannot exceed it, and the return value counts only
        rows that were really inserted.
        """
        if not rows:
            return 0
        with self._lock, self.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                stored = 0
                counts: dict[str | None, int] = {}
                for row in rows:
                    run = row["run_id"]
                    if run not in counts:
                        where = _t.c.run_id == run if run else _t.c.run_id.is_(None)
                        counts[run] = conn.execute(
                            select(func.count()).select_from(_t).where(where)
                        ).scalar_one()
                    if counts[run] >= self.rows_per_run:
                        continue
                    inserted = conn.execute(
                        sqlite_insert(_t).on_conflict_do_nothing(), [row]
                    ).rowcount
                    if inserted:
                        counts[run] += 1
                        stored += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return stored

    # ------------------------------------------------------------------ reads
    def _rows(self, where: Any, limit: int) -> list[dict[str, Any]]:
        q = select(_t).where(where).order_by(_t.c.start_ns, _t.c.span_id).limit(limit)
        with self.engine.connect() as conn:
            return [
                {
                    **{c: r._mapping[c] for c in _PLAIN_COLUMNS},
                    "attrs": json.loads(r.attrs),
                    "events": json.loads(r.events),
                }
                for r in conn.execute(q)
            ]

    def spans_for_run(self, run_id: str, limit: int = 100_000) -> list[dict[str, Any]]:
        """Spans of one run in start order."""
        return self._rows(_t.c.run_id == run_id, limit)

    def spans_for_session(
        self, session_id: str, limit: int = 100_000
    ) -> list[dict[str, Any]]:
        """Spans of one session in start order."""
        return self._rows(_t.c.session_id == session_id, limit)

    def spans_for_doc(self, doc_id: str, limit: int = 100_000) -> list[dict[str, Any]]:
        """Spans of one document (across runs) in start order."""
        return self._rows(_t.c.doc_id == doc_id, limit)

    def spans_between(
        self, start_ns: int, end_ns: int, limit: int = 100_000
    ) -> list[dict[str, Any]]:
        """Spans that started in ``[start_ns, end_ns)``."""
        return self._rows((_t.c.start_ns >= start_ns) & (_t.c.start_ns < end_ns), limit)

    def list_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        """One summary row per run id, newest first."""
        q = (
            select(
                _t.c.run_id,
                func.count().label("spans"),
                func.count(func.distinct(_t.c.doc_id)).label("docs"),
                func.min(_t.c.start_ns).label("first_ns"),
                func.max(_t.c.end_ns).label("last_ns"),
            )
            .where(_t.c.run_id.is_not(None))
            .group_by(_t.c.run_id)
            .order_by(func.max(_t.c.end_ns).desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    def watermark(self) -> int:
        """Newest ``end_ns`` stored (0 when empty); the timeline cache key."""
        with self.engine.connect() as conn:
            return conn.execute(select(func.max(_t.c.end_ns))).scalar() or 0

    def count(self, run_id: str | None = None) -> int:
        """Stored span rows (for one run, or all)."""
        q = select(func.count()).select_from(_t)
        if run_id is not None:
            q = q.where(_t.c.run_id == run_id)
        with self.engine.connect() as conn:
            return conn.execute(q).scalar_one()

    # ------------------------------------------------------------------ retention primitives
    def delete_runs(self, run_ids: Collection[str]) -> int:
        """Delete every span of the given runs. Returns rows removed."""
        if not run_ids:
            return 0
        with self._lock, self.engine.begin() as conn:
            return conn.execute(
                delete(_t).where(_t.c.run_id.in_(list(run_ids)))
            ).rowcount

    def prune(self, *, keep_runs: Collection[str], older_than_ns: int) -> int:
        """Delete spans that started before ``older_than_ns`` unless their run is in ``keep_runs``."""
        cond = _t.c.start_ns < older_than_ns
        if keep_runs:
            cond = cond & (_t.c.run_id.is_(None) | _t.c.run_id.not_in(list(keep_runs)))
        with self._lock, self.engine.begin() as conn:
            return conn.execute(delete(_t).where(cond)).rowcount

    def close(self) -> None:
        """Dispose the engine."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None


class SqliteSpanExporter(SpanExporter):
    """OpenTelemetry exporter that stores allow-listed spans in a :class:`SpanStore`."""

    def __init__(
        self, store: SpanStore | None = None, *, mask: bool | None = None
    ) -> None:
        """Export into ``store`` (default: the configured path); ``mask`` overrides ``trace_mask``."""
        self.store = store or SpanStore(default_span_store_path())
        self._mask = mask

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Store a batch; failures are logged and reported, never raised."""
        try:
            mask = get_settings().trace_mask if self._mask is None else self._mask
            self.store.write([self.store.row_for(s, mask=mask) for s in spans])
            return SpanExportResult.SUCCESS
        except Exception:
            logger.warning("span_store_export_failed", exc_info=True)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """Release the database handle."""
        self.store.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Nothing buffered here (the batch processor owns the queue)."""
        return True
