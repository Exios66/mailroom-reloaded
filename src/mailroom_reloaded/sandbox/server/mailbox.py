"""``boss_mailbox``: the named, durable, two-way channel between the Correspondent and the Boss.

A sandbox-only SQLite queue in the sandbox data dir. Entries are append-only (triggers
reject UPDATE and DELETE); status changes (``new`` -> ``read`` -> ``acted`` | ``expired``)
go to a separate append-only log and are mirrored as ``mailbox.*`` events. The two roles
use it as their only route to each other: neither reads the other's internal state.
Reading it as an operator changes nothing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "BOSS",
    "CORRESPONDENT",
    "KINDS",
    "STATUSES",
    "BossMailbox",
]

CORRESPONDENT = "correspondent"
BOSS = "boss"
STATUSES = ("new", "read", "acted", "expired")
KINDS = (
    "hostile_forward",  # correspondent -> boss
    "escalation",  # correspondent -> boss
    "question",  # correspondent -> boss
    "draft_for_approval",  # correspondent -> boss
    "decision",  # boss -> correspondent: release | quarantine
    "instruction",  # boss -> correspondent
    "approval",  # boss -> correspondent
    "rejection",  # boss -> correspondent
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  seq INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  thread_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  sender_role TEXT NOT NULL,
  recipient_role TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  in_reply_to TEXT
);
CREATE TABLE IF NOT EXISTS status_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id TEXT NOT NULL,
  status TEXT NOT NULL,
  at TEXT NOT NULL,
  by_role TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS status_log_entry_seq ON status_log(entry_id, seq DESC);
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'boss_mailbox entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'boss_mailbox entries are append-only'); END;
"""


def _now() -> str:
    """Return the current UTC timestamp in ISO format with millisecond precision."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class BossMailbox:
    def __init__(
        self,
        path: Path | str,
        emit: Callable[[str, str, dict | None], Any] | None = None,
    ) -> None:
        """Configure a queue opened lazily at ``path``.

        ``emit``, when supplied, receives entry and status events after commit;
        its exceptions propagate without undoing the committed write.
        """
        self.path = Path(path)
        self._emit = emit
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ plumbing
    def _conn(self) -> sqlite3.Connection:
        """Open and initialize the queue on first use, creating parent directories.

        Filesystem and SQLite errors propagate to the caller.
        """
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(_SCHEMA)
        return self._db

    def close(self) -> None:
        """Close the cached connection; later operations can reopen the queue."""
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def clear(self) -> None:
        """Sandbox reset only: drop the whole queue (the file, not row updates)."""
        with self._lock:
            self.close()
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)

    def _row(self, r: sqlite3.Row, status: str) -> dict:
        """Decode an entry payload and attach its current status to the returned view."""
        return {
            "id": r["id"],
            "seq": r["seq"],
            "thread_id": r["thread_id"],
            "message_id": r["message_id"],
            "direction": r["direction"],
            "sender_role": r["sender_role"],
            "recipient_role": r["recipient_role"],
            "kind": r["kind"],
            "payload": json.loads(r["payload"]),
            "created_at": r["created_at"],
            "status": status,
            "in_reply_to": r["in_reply_to"],
        }

    # ------------------------------------------------------------------ write
    def post(
        self,
        *,
        sender: str,
        recipient: str,
        kind: str,
        thread_id: str,
        message_id: str,
        payload: dict,
        in_reply_to: str | None = None,
    ) -> dict:
        """Append an entry with initial status ``new`` and return its stored view.

        ``sender`` and ``recipient`` must be opposite mailbox roles, and ``kind``
        must be in ``KINDS``; otherwise raise ``ValueError``. Unsupported JSON
        payload values are stringified. SQLite and serialization errors propagate,
        as do event callback errors after the entry has been committed.
        """
        if {sender, recipient} != {CORRESPONDENT, BOSS}:
            raise ValueError("the mailbox connects the correspondent and the boss only")
        if kind not in KINDS:
            raise ValueError(f"unknown mailbox kind {kind!r}")
        with self._lock:
            db = self._conn()
            seq = (
                db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM entries").fetchone()
            )[0]
            eid = f"bm{seq:05d}"
            db.execute(
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    eid,
                    thread_id,
                    message_id,
                    f"{sender}->{recipient}",
                    sender,
                    recipient,
                    kind,
                    json.dumps(payload, default=str),
                    _now(),
                    in_reply_to,
                ),
            )
            db.commit()
            entry = self.get(eid)
        assert entry is not None
        if self._emit:
            self._emit(
                "mailbox.entry",
                message_id,
                {
                    k: entry[k]
                    for k in ("id", "direction", "kind", "thread_id", "in_reply_to")
                },
            )
        return entry

    def set_status(self, entry_id: str, status: str, by: str, note: str = "") -> dict:
        """Append a status change and return the updated entry.

        Repeating the current status is a no-op; any other status in ``STATUSES``
        is accepted without transition ordering. Raise ``ValueError`` for an
        unknown status and ``KeyError`` for a missing entry. SQLite errors and
        event callback errors propagate; callbacks run after commit.
        """
        if status not in STATUSES:
            raise ValueError(f"status must be one of {list(STATUSES)}")
        with self._lock:
            cur = self.get(entry_id)
            if cur is None:
                raise KeyError(entry_id)
            if cur["status"] == status:
                return cur
            db = self._conn()
            db.execute(
                "INSERT INTO status_log (entry_id, status, at, by_role, note) VALUES (?,?,?,?,?)",
                (entry_id, status, _now(), by, note),
            )
            db.commit()
            out = self.get(entry_id)
        assert out is not None
        if self._emit:
            self._emit(
                "mailbox.status",
                out["message_id"],
                {"id": entry_id, "status": status, "by": by, "note": note},
            )
        return out

    # ------------------------------------------------------------------ read (no side effects)
    def _status_of(self, entry_id: str) -> str:
        """Return the latest recorded status, or ``new`` when no status exists."""
        r = (
            self._conn()
            .execute(
                "SELECT status FROM status_log WHERE entry_id=? ORDER BY seq DESC LIMIT 1",
                (entry_id,),
            )
            .fetchone()
        )
        return r["status"] if r else "new"

    def get(self, entry_id: str) -> dict | None:
        """Return an entry with its current status, or ``None``, without marking it read."""
        with self._lock:
            r = (
                self._conn()
                .execute("SELECT * FROM entries WHERE id=?", (entry_id,))
                .fetchone()
            )
            return self._row(r, self._status_of(entry_id)) if r else None

    def history(self, entry_id: str) -> list[dict]:
        """Return status changes in append order without changing entry status.

        Entries with no changes and unknown IDs both return an empty list.
        """
        with self._lock:
            return [
                dict(x)
                for x in self._conn().execute(
                    "SELECT status, at, by_role, note FROM status_log WHERE entry_id=? ORDER BY seq",
                    (entry_id,),
                )
            ]

    def list(
        self,
        *,
        direction: str | None = None,
        role: str | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        since: int = 0,
        limit: int = 500,
    ) -> list[dict]:
        """Return matching entries in sequence order without marking them read.

        ``since`` is an exclusive sequence cursor; ``role`` matches either sender
        or recipient. Nonempty filters are combined, including current ``status``.
        ``limit`` is applied last as a Python slice stop (zero returns no entries;
        a negative value omits that many entries from the end). SQLite errors
        propagate, including errors opening the queue on its first read.
        """
        where, args = ["seq > ?"], [since]
        for col, val in (
            ("direction", direction),
            ("thread_id", thread_id),
            ("message_id", message_id),
            ("kind", kind),
        ):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if role:
            where.append("(sender_role = ? OR recipient_role = ?)")
            args += [role, role]
        if status:
            where.append("status = ?")
            args.append(status)
        query = f"""
            WITH current_entries AS (
                SELECT entries.*, COALESCE((
                    SELECT status FROM status_log
                    WHERE entry_id = entries.id ORDER BY seq DESC LIMIT 1
                ), 'new') AS status
                FROM entries
            ), matching AS (
                SELECT * FROM current_entries WHERE {' AND '.join(where)}
            )
            SELECT * FROM matching ORDER BY seq
        """
        if limit < 0:
            query += " LIMIT (SELECT MAX(0, COUNT(*) + ?) FROM matching)"
        else:
            query += " LIMIT ?"
        with self._lock:
            rows = self._conn().execute(query, [*args, limit]).fetchall()
            return [self._row(r, r["status"]) for r in rows]

    def count(self) -> int:
        """Return the total number of entries across all statuses."""
        with self._lock:
            return self._conn().execute("SELECT COUNT(*) FROM entries").fetchone()[0]
