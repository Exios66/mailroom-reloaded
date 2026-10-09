"""Archive ledger: one global hash chain over every pipeline run.

``audit_log`` keeps its per-document chains untouched. The ledger is a separate
pair of tables in the same ``mailroom.db``:

* ``ledger``: ``seq, kind, run_id, doc_id, ts, payload, digest, prev_hash,
  entry_hash``; one global chain (genesis ``prev_hash == ""``), tail-only append,
  no de-duplication (a repeated pin/unpin must stay two entries).
* ``ledger_metrics``: the flat metric rows captured for a run, capped per run
  (``row_cap``, default 5,000) with a single ``gap`` entry when the cap is hit.

Writes go through one writer thread that drains a queue and appends each batch in
a single ``BEGIN IMMEDIATE`` transaction, so a heavy run never contends with the
per-document audit chain. Chain entries are never dropped; metric rows are
(counted, and surfaced in ``gap`` and ``run_closed.dropped_rows``). Every public
write method swallows and logs its own failures: the ledger must never fail a
document or mask the pipeline's own exception.

``run_closed`` gets a binary Merkle root over the run's ``doc_closed`` digests
(computed by the writer inside the same transaction), so ``verify(run_id)`` can
detect a missing or altered document record.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import math
import threading
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import Engine, func, select

from mailroom_reloaded.schemas.ledger import (
    LedgerEntry,
    LedgerVerify,
    compute_ledger_hash,
    payload_digest,
    sanitize_payload,
)
from mailroom_reloaded.storage.db import get_engine, ledger_metrics_table, ledger_table

__all__ = [
    "DEFAULT_ROW_CAP",
    "Ledger",
    "MetricRow",
    "get_ledger",
    "merkle_root",
    "reset_ledger",
]

logger = structlog.get_logger(__name__)

DEFAULT_ROW_CAP = 5000
_t = ledger_table
_m = ledger_metrics_table


def merkle_root(digests: Sequence[str]) -> str:
    """Binary Merkle root over hex ``digests`` (leaf/node domain separation, odd node promoted)."""
    if not digests:
        return hashlib.sha256(b"mailroom-ledger:empty").hexdigest()
    level = [hashlib.sha256(b"\x00" + d.encode("utf-8")).digest() for d in digests]
    while len(level) > 1:
        nxt = [
            hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest()
            for i in range(0, len(level) - 1, 2)
        ]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()


@dataclass(frozen=True)
class MetricRow:
    """One flat metric sample for a run."""

    name: str
    value: float | None
    tier: int = 1
    span_id: str | None = None


@dataclass
class _Item:
    run_id: str
    doc_id: str | None
    ts: str
    kind: str | None = None  # chain entry when set
    payload: dict[str, Any] = field(default_factory=dict)
    rows: list[MetricRow] = field(default_factory=list)  # metric rows when kind is None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode(r: Any) -> LedgerEntry | None:
    """Decode a row for verification; ``None`` when it is malformed (counts as tampering)."""
    try:
        return _row_entry(r)
    except (ValueError, TypeError):
        return None


def _row_entry(r: Any) -> LedgerEntry:
    return LedgerEntry(
        seq=r.seq,
        kind=r.kind,
        run_id=r.run_id,
        doc_id=r.doc_id,
        ts=r.ts,
        payload=json.loads(r.payload),
        digest=r.digest,
        prev_hash=r.prev_hash,
        entry_hash=r.entry_hash,
    )


class Ledger:
    """The archive ledger over one SQLite engine, with its writer thread."""

    def __init__(
        self,
        engine: Engine | None = None,
        *,
        row_cap: int = DEFAULT_ROW_CAP,
        max_pending_rows: int = 20000,
        batch_window_s: float = 0.05,
        retry_base_s: float = 0.2,
    ) -> None:
        """Create the ledger; the writer thread starts on the first append."""
        self._engine = engine
        self.row_cap = row_cap
        self._max_pending_rows = max_pending_rows
        self._batch_window = batch_window_s
        self._retry_base = retry_base_s
        self._cond = threading.Condition()
        self._items: deque[_Item] = deque()
        self._pending_rows = 0
        self._enqueued = 0
        self._done = 0
        self._stop = False
        self._thread: threading.Thread | None = None
        # accessed by enqueuing threads under _cond; the writer under _cond too
        self._commit_hooks: list[Callable[[set[str]], None]] = []
        self._dropped: dict[str, int] = {}
        self._overflow_gap: set[str] = set()

    # ------------------------------------------------------------------ engine
    @property
    def engine(self) -> Engine:
        """The SQLite engine (the default ``mailroom.db`` engine unless one was given)."""
        if self._engine is None:
            self._engine = get_engine()
        return self._engine

    def add_commit_hook(self, hook: Callable[[set[str]], None]) -> None:
        """Call ``hook(kinds)`` after each committed batch (on the writer thread; failures are logged)."""
        self._commit_hooks.append(hook)

    # ------------------------------------------------------------------ writes
    def append(
        self,
        kind: str,
        run_id: str,
        doc_id: str | None = None,
        payload: dict[str, Any] | None = None,
        metrics: Iterable[MetricRow] = (),
    ) -> bool:
        """Queue a chain entry (and optional metric rows). Never raises; returns success."""
        try:
            clean = sanitize_payload(kind, payload)
            rows = [r for r in (self._clean_row(m) for m in metrics) if r is not None]
            ts = _now()
            with self._cond:
                self._items.append(_Item(run_id, doc_id, ts, kind=kind, payload=clean))
                self._enqueued += 1
                if rows:
                    self._queue_rows(run_id, doc_id, ts, rows)
                self._ensure_thread()
                self._cond.notify_all()
            return True
        except Exception:
            logger.warning("ledger_append_failed", kind=kind, exc_info=True)
            return False

    def append_metrics(
        self, run_id: str, doc_id: str | None, rows: Iterable[MetricRow]
    ) -> None:
        """Queue metric rows only (subject to the per-run cap). Never raises."""
        try:
            clean = [r for r in (self._clean_row(m) for m in rows) if r is not None]
            if not clean:
                return
            with self._cond:
                self._queue_rows(run_id, doc_id, _now(), clean)
                self._ensure_thread()
                self._cond.notify_all()
        except Exception:
            logger.warning("ledger_metrics_failed", exc_info=True)

    @staticmethod
    def _clean_row(row: MetricRow) -> MetricRow | None:
        name = str(row.name)[:80]
        if not name:
            return None
        value = row.value
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value):
                return None
        span = str(row.span_id)[:32] if row.span_id else None
        return MetricRow(name, value, min(3, max(0, int(row.tier))), span)

    def _queue_rows(
        self, run_id: str, doc_id: str | None, ts: str, rows: list[MetricRow]
    ) -> None:
        """Queue ``rows`` or, when the pending bound is hit, count them as dropped (caller holds _cond)."""
        if self._pending_rows + len(rows) > self._max_pending_rows:
            self._dropped[run_id] = self._dropped.get(run_id, 0) + len(rows)
            if run_id not in self._overflow_gap:
                self._overflow_gap.add(run_id)
                gap = sanitize_payload(
                    "gap", {"reason": "queue_overflow", "count": len(rows)}
                )
                self._items.append(_Item(run_id, None, ts, kind="gap", payload=gap))
                self._enqueued += 1
            return
        self._items.append(_Item(run_id, doc_id, ts, rows=rows))
        self._pending_rows += len(rows)
        self._enqueued += 1

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop = False
            self._thread = threading.Thread(
                target=self._run, name="ledger-writer", daemon=True
            )
            self._thread.start()

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until everything queued so far is committed. True when drained in time."""
        with self._cond:
            target = self._enqueued
            self._cond.notify_all()
            return self._cond.wait_for(lambda: self._done >= target, timeout=timeout)

    def close(self, timeout: float = 10.0) -> None:
        """Flush, then stop the writer thread."""
        self.flush(timeout)
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ------------------------------------------------------------------ writer thread
    def _run(self) -> None:
        failures = 0
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._items or self._stop)
                if not self._items and self._stop:
                    return
                if not self._stop and failures == 0:
                    self._cond.wait(
                        self._batch_window
                    )  # let a burst accumulate into one batch
                batch = list(self._items)
                self._items.clear()
            try:
                self._write(batch)
                failures = 0
            except Exception:  # keep the batch and retry; never kill the thread
                failures += 1
                logger.warning("ledger_write_failed", attempt=failures, exc_info=True)
                kept = self._shed_metrics(batch) if failures >= 5 else batch
                with self._cond:
                    self._items.extendleft(reversed(kept))
                    self._cond.wait(min(30.0, self._retry_base * 2 ** min(failures, 8)))
                continue
            with self._cond:
                self._pending_rows -= sum(len(i.rows) for i in batch)
                self._done += len(batch)
                self._cond.notify_all()
            self._run_hooks({i.kind for i in batch if i.kind})

    def _run_hooks(self, kinds: set[str]) -> None:
        for hook in list(self._commit_hooks):
            try:
                hook(kinds)
            except Exception:
                logger.warning("ledger_commit_hook_failed", exc_info=True)

    def _shed_metrics(self, batch: list[_Item]) -> list[_Item]:
        """After repeated write failures drop the batch's metric rows (never chain entries).

        The loss is counted per run and surfaced as one ``gap`` (``write_failed``) per run
        in the retained batch, so the omission is visible in the chain.
        """
        kept = [i for i in batch if i.kind is not None]
        shed = [i for i in batch if i.kind is None]
        if not shed:
            return kept
        lost: dict[str, int] = {}
        for item in shed:
            lost[item.run_id] = lost.get(item.run_id, 0) + len(item.rows)
        logger.error("ledger_metric_rows_dropped", rows=sum(lost.values()))
        with self._cond:
            self._pending_rows -= sum(len(i.rows) for i in shed)
            self._done += len(shed)
            for run, n in lost.items():
                self._dropped[run] = self._dropped.get(run, 0) + n
        ts = _now()
        for run, n in lost.items():
            gap = sanitize_payload("gap", {"reason": "write_failed", "count": n})
            kept.append(_Item(run, None, ts, kind="gap", payload=gap))
            with self._cond:
                self._enqueued += 1
        return kept

    def _write(self, batch: list[_Item]) -> None:
        with self.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                last = conn.execute(
                    select(_t).order_by(_t.c.seq.desc()).limit(1)
                ).first()
                # per-transaction state: nothing here outlives a rollback
                state: dict[str, Any] = {
                    "seq": last.seq if last else 0,
                    "prev": last.entry_hash if last else "",
                    "rows": {},  # run_id -> stored metric rows (counted in this transaction)
                    "dropped": {},  # run_id -> rows dropped by the cap in this transaction
                    "gaps": set(),  # runs whose row_cap gap exists
                }
                for item in batch:
                    if item.kind is not None:
                        self._insert_entry(conn, state, item)
                    else:
                        self._insert_rows(conn, state, item)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        with self._cond:  # committed: only now do the drops count
            for run, n in state["dropped"].items():
                self._dropped[run] = self._dropped.get(run, 0) + n

    def _insert_entry(self, conn: Any, state: dict[str, Any], item: _Item) -> None:
        payload = item.payload
        if item.kind == "run_closed":
            digests = [
                r.digest
                for r in conn.execute(
                    select(_t.c.digest)
                    .where(_t.c.run_id == item.run_id, _t.c.kind == "doc_closed")
                    .order_by(_t.c.seq)
                )
            ]
            with self._cond:
                dropped = self._dropped.get(item.run_id, 0)
            dropped += state["dropped"].get(item.run_id, 0)
            # a restart loses the in-memory count: the persisted gap counts are the floor
            gap_floor = sum(
                int(json.loads(r.payload).get("count", 0))
                for r in conn.execute(
                    select(_t.c.payload).where(
                        _t.c.run_id == item.run_id, _t.c.kind == "gap"
                    )
                )
            )
            payload = {
                **payload,
                "merkle_root": merkle_root(digests),
                "docs": len(digests),
                "dropped_rows": max(
                    int(payload.get("dropped_rows", 0)), dropped, gap_floor
                ),
            }
        entry = LedgerEntry(
            seq=state["seq"] + 1,
            kind=item.kind or "",
            run_id=item.run_id,
            doc_id=item.doc_id,
            ts=item.ts,
            payload=payload,
            digest=payload_digest(payload),
            prev_hash=state["prev"],
        )
        entry.entry_hash = compute_ledger_hash(entry)
        conn.execute(
            _t.insert().values(
                seq=entry.seq,
                kind=entry.kind,
                run_id=entry.run_id,
                doc_id=entry.doc_id,
                ts=entry.ts,
                payload=json.dumps(entry.payload, sort_keys=True),
                digest=entry.digest,
                prev_hash=entry.prev_hash,
                entry_hash=entry.entry_hash,
            )
        )
        state["seq"], state["prev"] = entry.seq, entry.entry_hash

    def _insert_rows(self, conn: Any, state: dict[str, Any], item: _Item) -> None:
        run = item.run_id
        if (
            run not in state["rows"]
        ):  # recounted per transaction: other instances may have written
            state["rows"][run] = conn.execute(
                select(func.count()).select_from(_m).where(_m.c.run_id == run)
            ).scalar_one()
        room = max(0, self.row_cap - state["rows"][run])
        keep, drop = item.rows[:room], item.rows[room:]
        if keep:
            conn.execute(
                _m.insert(),
                [
                    {
                        "run_id": run,
                        "doc_id": item.doc_id,
                        "tier": r.tier,
                        "name": r.name,
                        "value": r.value,
                        "span_id": r.span_id,
                        "ts": item.ts,
                    }
                    for r in keep
                ],
            )
            state["rows"][run] += len(keep)
        if drop:
            state["dropped"][run] = state["dropped"].get(run, 0) + len(drop)
            if run not in state["gaps"] and not self._has_gap(conn, run, "row_cap"):
                gap = _Item(
                    run,
                    None,
                    item.ts,
                    kind="gap",
                    payload=sanitize_payload(
                        "gap", {"reason": "row_cap", "count": len(drop)}
                    ),
                )
                self._insert_entry(conn, state, gap)
            state["gaps"].add(run)

    @staticmethod
    def _has_gap(conn: Any, run_id: str, reason: str) -> bool:
        rows = conn.execute(
            select(_t.c.payload).where(_t.c.run_id == run_id, _t.c.kind == "gap")
        )
        return any(json.loads(r.payload).get("reason") == reason for r in rows)

    # ------------------------------------------------------------------ reads
    def head(self) -> LedgerEntry | None:
        """The newest committed entry (queued entries are not visible until flushed)."""
        with self.engine.connect() as conn:
            row = conn.execute(select(_t).order_by(_t.c.seq.desc()).limit(1)).first()
        return _row_entry(row) if row else None

    def count(self, kind: str, run_id: str, doc_id: str | None = None) -> int:
        """Committed entries of ``kind`` for a run (and optionally one document)."""
        q = select(func.count()).select_from(_t).where(_t.c.kind == kind, _t.c.run_id == run_id)
        if doc_id is not None:
            q = q.where(_t.c.doc_id == doc_id)
        with self.engine.connect() as conn:
            return conn.execute(q).scalar_one()

    def open_runs(self, kind: str | None = None) -> list[str]:
        """Run ids that have a committed ``run_opened`` and no ``run_closed`` (oldest first)."""
        closed = select(_t.c.run_id).where(_t.c.kind == "run_closed")
        q = (
            select(_t.c.run_id, _t.c.payload)
            .where(_t.c.kind == "run_opened", _t.c.run_id.not_in(closed))
            .order_by(_t.c.seq)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        return [r.run_id for r in rows if kind is None or json.loads(r.payload).get("kind") == kind]

    def entries(
        self,
        *,
        run_id: str | None = None,
        kind: str | None = None,
        doc_id: str | None = None,
        since_seq: int = 0,
        limit: int = 200,
        offset: int = 0,
        descending: bool = False,
    ) -> list[LedgerEntry]:
        """Entries filtered by run / kind / document, in ``seq`` order."""
        q = select(_t).where(_t.c.seq > since_seq)
        if run_id is not None:
            q = q.where(_t.c.run_id == run_id)
        if kind is not None:
            q = q.where(_t.c.kind == kind)
        if doc_id is not None:
            q = q.where(_t.c.doc_id == doc_id)
        q = (
            q.order_by(_t.c.seq.desc() if descending else _t.c.seq)
            .limit(limit)
            .offset(offset)
        )
        with self.engine.connect() as conn:
            return [_row_entry(r) for r in conn.execute(q)]

    def metric_rows(
        self,
        run_id: str,
        *,
        doc_id: str | None = None,
        max_tier: int = 3,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Stored metric rows for a run, up to ``max_tier``."""
        q = select(_m).where(_m.c.run_id == run_id, _m.c.tier <= max_tier)
        if doc_id is not None:
            q = q.where(_m.c.doc_id == doc_id)
        q = q.order_by(_m.c.id).limit(limit)
        with self.engine.connect() as conn:
            return [
                {
                    "doc_id": r.doc_id,
                    "tier": r.tier,
                    "name": r.name,
                    "value": r.value,
                    "span_id": r.span_id,
                    "ts": r.ts,
                }
                for r in conn.execute(q)
            ]

    # ------------------------------------------------------------------ verification
    def verify(self, run_id: str | None = None) -> LedgerVerify:
        """Verify the whole chain, or one run's entries, hashes, links and Merkle root."""
        self.flush()
        return self._verify_run(run_id) if run_id is not None else self._verify_all()

    @staticmethod
    def _entry_ok(e: LedgerEntry) -> bool:
        return e.digest == payload_digest(
            e.payload
        ) and e.entry_hash == compute_ledger_hash(e)

    def _verify_all(self) -> LedgerVerify:
        count, prev, expected = 0, "", 1
        head_seq: int | None = None
        digests: dict[str, list[str]] = {}
        merkle_ok: bool | None = None
        with self.engine.connect() as conn:
            for r in conn.execute(select(_t).order_by(_t.c.seq)):
                e = _decode(r)
                if e is None:
                    return LedgerVerify(
                        ok=False,
                        count=count,
                        head_seq=head_seq,
                        head_hash=prev or None,
                        broken_at=r.seq,
                        detail="undecodable entry",
                    )
                if e.seq != expected or e.prev_hash != prev or not self._entry_ok(e):
                    return LedgerVerify(
                        ok=False,
                        count=count,
                        head_seq=head_seq,
                        head_hash=prev or None,
                        broken_at=e.seq,
                        detail="chain broken",
                    )
                if e.kind == "doc_closed":
                    digests.setdefault(e.run_id, []).append(e.digest)
                elif e.kind == "run_closed":
                    if not self._merkle_matches(e, digests.get(e.run_id, [])):
                        return LedgerVerify(
                            ok=False,
                            count=count + 1,
                            head_seq=e.seq,
                            head_hash=e.entry_hash,
                            broken_at=e.seq,
                            merkle_ok=False,
                            detail="merkle root mismatch",
                        )
                    merkle_ok = True if merkle_ok is None else merkle_ok
                prev, expected, count, head_seq = (
                    e.entry_hash,
                    expected + 1,
                    count + 1,
                    e.seq,
                )
        return LedgerVerify(
            ok=True,
            count=count,
            head_seq=head_seq,
            head_hash=prev or None,
            merkle_ok=merkle_ok,
        )

    @staticmethod
    def _merkle_matches(closed: LedgerEntry, digests: list[str]) -> bool:
        return closed.payload.get("merkle_root") == merkle_root(
            digests
        ) and closed.payload.get("docs") == len(digests)

    def _verify_run(self, run_id: str) -> LedgerVerify:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(_t).where(_t.c.run_id == run_id).order_by(_t.c.seq)
            ).all()
            if not rows:
                return LedgerVerify(ok=False, detail="unknown run")
            entries: list[LedgerEntry] = []
            for r in rows:
                e = _decode(r)
                if e is None:
                    return LedgerVerify(
                        ok=False,
                        count=len(rows),
                        broken_at=r.seq,
                        detail="undecodable entry",
                    )
                entries.append(e)
            digests: list[str] = []
            merkle_ok: bool | None = None
            for e in entries:
                prev_row = (
                    conn.execute(
                        select(_t.c.entry_hash).where(_t.c.seq == e.seq - 1)
                    ).first()
                    if e.seq > 1
                    else None
                )
                linked = e.prev_hash == (prev_row.entry_hash if prev_row else "") and (
                    e.seq == 1 or prev_row is not None
                )
                if not linked or not self._entry_ok(e):
                    return LedgerVerify(
                        ok=False,
                        count=len(entries),
                        broken_at=e.seq,
                        detail="chain broken",
                    )
                if e.kind == "doc_closed":
                    digests.append(e.digest)
                elif e.kind == "run_closed":
                    merkle_ok = self._merkle_matches(e, digests)
                    if not merkle_ok:
                        return LedgerVerify(
                            ok=False,
                            count=len(entries),
                            head_seq=e.seq,
                            head_hash=e.entry_hash,
                            broken_at=e.seq,
                            merkle_ok=False,
                            detail="merkle root mismatch",
                        )
        tail = entries[-1]
        return LedgerVerify(
            ok=True,
            count=len(entries),
            head_seq=tail.seq,
            head_hash=tail.entry_hash,
            merkle_ok=merkle_ok,
        )


# --------------------------------------------------------------------------- default instance
_default: Ledger | None = None
_default_lock = threading.Lock()


def get_ledger(*, anchor: bool = True) -> Ledger:
    """The process-wide ledger on the default engine (created lazily).

    If the default engine was replaced (a different ``base_dir``), the old ledger is
    flushed and closed and a new one is bound, so a late write never lands in the
    wrong database.

    ``anchor=False`` (read-only tooling such as ``mailroom audit``) skips the external
    anchor hook and its startup push, so inspecting the ledger never writes to the store.
    """
    global _default
    engine = get_engine()
    with _default_lock:
        if _default is not None and _default.engine is not engine:
            _default.close()
            _default = None
        if _default is None:
            _default = Ledger(engine)
            atexit.register(_default.close)
            if anchor:
                from mailroom_reloaded.storage import (
                    anchor as anchor_mod,  # lazy: the anchor imports the ledger types
                )

                anchor_mod.install(_default)
        return _default


def reset_ledger() -> None:
    """Flush and drop the process-wide ledger (tests, and after the default engine changes)."""
    global _default
    with _default_lock:
        if _default is not None:
            _default.close()
            _default = None
