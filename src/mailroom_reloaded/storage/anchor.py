"""External anchor for the archive ledger head.

A hash chain on one disk detects edits but not truncation or a restored backup. The anchor
pushes ``(seq, entry_hash)`` of the **ledger head** (nothing else) to a store the pipeline
host cannot rewrite, so ``audit verify --external`` can tell *truncated* and *rewritten*
history from a healthy one.

Backends (``MAILROOM_ANCHOR``): ``none`` (default), ``export`` (print the head for off-host
pinning by an operator), ``supabase`` (PostgREST over HTTPS with ``httpx``, no extra
dependency, the recommended path) and ``postgres`` (SQLAlchemy + the optional ``anchor``
extra's ``psycopg``). The remote table is insert-only: the writer credential is a dedicated
database role with ``INSERT`` and ``SELECT`` on ``mailroom_anchor`` and nothing else (see
``deploy/anchor/mailroom_anchor.sql``); the service key is never used.

Threat model, honestly: this protects against truncation, rollback (including a restored
backup) and rewrite of entries at or before the last anchor pushed before the compromise,
against an attacker **without** the writer credential. It does not protect the unanchored
tail, runs still open, the correctness of what was recorded, or a holder of the writer
credential (who can append anchors over a rewritten tail; only an off-host ``export`` copy
helps). The trigger, grants and Supabase key header can only be tested against a live
project; see the SQL file.

Pushes never block or fail the pipeline: a daemon thread makes up to three attempts (1 s and 4 s apart)
with 5 s timeouts, and the key is redacted from every logged error.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

import httpx
import structlog

from mailroom_reloaded.settings import get_settings

if TYPE_CHECKING:
    from mailroom_reloaded.schemas.ledger import LedgerEntry
    from mailroom_reloaded.storage.ledger import Ledger

__all__ = [
    "BACKENDS",
    "EXIT_NOT_CONFIGURED",
    "EXIT_OK",
    "EXIT_STALE",
    "EXIT_TAMPER",
    "EXIT_UNREACHABLE",
    "TRIGGER_KINDS",
    "AnchorConfig",
    "AnchorConflict",
    "AnchorError",
    "AnchorNotConfigured",
    "AnchorUnreachable",
    "ExternalVerify",
    "PushResult",
    "SqlBackend",
    "SupabaseBackend",
    "export_head",
    "get_config",
    "install",
    "push_head",
    "schedule_push",
    "verify_external",
]

logger = structlog.get_logger(__name__)

BACKENDS = ("none", "export", "postgres", "supabase")
#: Entry kinds that make a new head worth anchoring.
TRIGGER_KINDS = frozenset({"run_closed", "pinned", "unpinned", "policy", "pruned"})
STALE_AFTER = timedelta(hours=24)
KEY_FILE_MAX_BYTES = 8192
TIMEOUT_S = 5.0
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
RETRY_DELAYS_S = (1.0, 4.0, 16.0)

# exit codes of ``mailroom audit verify`` (2 is Click's usage error, so it is skipped)
EXIT_OK, EXIT_TAMPER, EXIT_UNREACHABLE, EXIT_NOT_CONFIGURED, EXIT_STALE = 0, 1, 3, 4, 5


class AnchorError(Exception):
    """Base class; messages never contain the key."""


class AnchorNotConfigured(AnchorError):
    """No usable backend / URL / key."""


class AnchorUnreachable(AnchorError):
    """The store could not be reached or answered with an error."""


class AnchorConflict(AnchorError):
    """The store holds a different hash for a sequence number (or a later head)."""


def _url_password(url: str | None) -> str | None:
    """The password embedded in a DSN-style ``url``, if any."""
    if not url:
        return None
    try:
        from sqlalchemy.engine import make_url

        return make_url(url).password
    except Exception:  # noqa: BLE001 - an unparsable URL has no password we can find
        return None


def _scrub_password(text: str, password: str | None) -> str:
    """``text`` with ``:<password>@`` (raw or percent-encoded) masked; other text is untouched."""
    if not password:
        return text
    for variant in {password, quote(password, safe="")}:
        text = text.replace(f":{variant}@", ":***@")
    return text


def _hide_password(url: str | None) -> str | None:
    """``url`` with an embedded password masked."""
    return _scrub_password(url, _url_password(url)) if url else url


@dataclass(frozen=True)
class AnchorConfig:
    """Resolved anchor settings. ``key`` is excluded from ``repr``."""

    backend: str
    url: str | None = None
    key: str | None = None
    key_file_world_readable: bool = False

    def __repr__(self) -> str:
        """No secrets in logs or tracebacks."""
        return f"AnchorConfig(backend={self.backend!r}, url={_hide_password(self.url)!r}, key={'***' if self.key else None})"

    def redact(self, text: str) -> str:
        """``text`` with the key (and a DSN password) replaced by ``***``."""
        out = text
        if self.key:
            out = out.replace(self.key, "***")
        return _scrub_password(out, _url_password(self.url))


@dataclass(frozen=True)
class Head:
    """An anchored (or local) head."""

    seq: int
    entry_hash: str


class Backend(Protocol):
    """What the anchor needs from a remote store."""

    def head(self) -> Head | None:
        """The highest anchored sequence number, or ``None`` when empty."""

    def get(self, seq: int) -> str | None:
        """The hash anchored at ``seq``, or ``None``."""

    def push(self, seq: int, entry_hash: str) -> None:
        """Insert one anchor (insert-only)."""


# --------------------------------------------------------------------------- configuration
def _read_key_file(path: Path) -> tuple[str, bool]:
    """The stripped key in ``path`` and whether the file is world-readable (fails closed).

    Opens non-blocking and checks the *opened* descriptor is a regular file, so a FIFO or
    device cannot hang or flood the read. Symlinks are followed (Kubernetes secret mounts).
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise AnchorNotConfigured("anchor key file is not a regular file")
            raw = os.read(fd, KEY_FILE_MAX_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise AnchorNotConfigured(
            f"anchor key file is unreadable ({type(exc).__name__})"
        ) from None
    if len(raw) > KEY_FILE_MAX_BYTES:
        raise AnchorNotConfigured("anchor key file is too large")
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise AnchorNotConfigured("anchor key file is not text") from None
    if not text:
        raise AnchorNotConfigured("anchor key file is empty")
    return text, bool(info.st_mode & stat.S_IROTH)


def get_config() -> AnchorConfig:
    """Resolve the anchor settings. Raises :class:`AnchorNotConfigured` for ``none`` or a bad setup.

    The key comes from ``MAILROOM_ANCHOR_KEY`` (wins) or the file named by
    ``MAILROOM_ANCHOR_KEY_FILE`` (Docker / Kubernetes secrets style).
    """
    settings = get_settings()
    backend = settings.anchor
    if backend == "none":
        raise AnchorNotConfigured("MAILROOM_ANCHOR is none")
    if backend not in BACKENDS:
        raise AnchorNotConfigured(f"unknown MAILROOM_ANCHOR value {backend!r}")
    if backend == "export":
        return AnchorConfig("export")
    key, world_readable = settings.anchor_key, False
    if not key and settings.anchor_key_file is not None:
        key, world_readable = _read_key_file(Path(settings.anchor_key_file))
    url = settings.anchor_url
    if not url:
        raise AnchorNotConfigured("MAILROOM_ANCHOR_URL is not set")
    if backend == "supabase":
        if not key:
            raise AnchorNotConfigured("MAILROOM_ANCHOR_KEY (or _KEY_FILE) is not set")
        _require_https(url)
    return AnchorConfig(backend, url, key, world_readable)


def _require_https(url: str) -> None:
    """Refuse a non-HTTPS URL unless it is loopback (development)."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise AnchorNotConfigured("MAILROOM_ANCHOR_URL is not a valid URL") from None
    if parsed.scheme != "https" and parsed.host not in LOOPBACK:
        raise AnchorNotConfigured("MAILROOM_ANCHOR_URL must use https")


# --------------------------------------------------------------------------- backends
class SupabaseBackend:
    """PostgREST over HTTPS (``/rest/v1/mailroom_anchor``) with a dedicated insert-only role key."""

    TABLE = "/rest/v1/mailroom_anchor"

    def __init__(
        self, url: str, key: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        """Talk to ``url`` as the role whose JWT is ``key``; ``transport`` is for tests."""
        self._key = key
        self._client = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=TIMEOUT_S,
            transport=transport,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )

    def _request(self, method: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, self.TABLE, **kwargs)
        except httpx.HTTPError as exc:
            raise AnchorUnreachable(
                f"anchor store unreachable ({type(exc).__name__})"
            ) from None

    def head(self) -> Head | None:
        """Highest anchor, via ``order=seq.desc&limit=1``."""
        resp = self._request(
            "GET",
            params={"select": "seq,entry_hash", "order": "seq.desc", "limit": "1"},
        )
        rows = self._json(resp)
        try:
            return Head(int(rows[0]["seq"]), str(rows[0]["entry_hash"])) if rows else None
        except (KeyError, TypeError, ValueError, IndexError):
            raise AnchorUnreachable("anchor store returned an unexpected row") from None

    def get(self, seq: int) -> str | None:
        """The hash at ``seq``."""
        resp = self._request(
            "GET", params={"select": "entry_hash", "seq": f"eq.{int(seq)}"}
        )
        rows = self._json(resp)
        try:
            return str(rows[0]["entry_hash"]) if rows else None
        except (KeyError, TypeError, IndexError):
            raise AnchorUnreachable("anchor store returned an unexpected row") from None

    def push(self, seq: int, entry_hash: str) -> None:
        """Insert ``(seq, entry_hash)``; duplicates are resolved by read-back, not by status code."""
        resp = self._request(
            "POST",
            json={"seq": int(seq), "entry_hash": entry_hash},
            headers={"Content-Type": "application/json", "Prefer": "return=minimal"},
        )
        if resp.is_success:
            return
        existing = self.get(
            seq
        )  # a conflict (409), trigger rejection (4xx) or lost response
        if existing == entry_hash:
            return
        if existing is not None:
            raise AnchorConflict(f"seq {seq} is already anchored with a different hash")
        current = self.head()  # a trigger rejection means the store already holds a higher seq
        if current is not None and current.seq >= seq:
            raise AnchorConflict(
                f"anchor store already holds seq {current.seq}, ahead of {seq}"
            )
        raise AnchorUnreachable(
            f"anchor store rejected the insert (HTTP {resp.status_code})"
        )

    @staticmethod
    def _json(resp: httpx.Response) -> list[dict[str, Any]]:
        if not resp.is_success:
            raise AnchorUnreachable(f"anchor store answered HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise AnchorUnreachable("anchor store returned invalid JSON") from None
        if not isinstance(data, list):
            raise AnchorUnreachable("anchor store returned an unexpected body")
        return data

    def close(self) -> None:
        """Release the HTTP client."""
        self._client.close()


class SqlBackend:
    """Postgres through SQLAlchemy (``psycopg`` from the ``anchor`` extra); ``sqlite://`` in tests."""

    def __init__(self, url: str, key: str | None = None) -> None:
        """Connect with ``url`` (a ``postgresql://`` DSN); ``key`` is the role's password."""
        from sqlalchemy import (
            BigInteger,
            Column,
            MetaData,
            String,
            Table,
            create_engine,
        )
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        try:
            parsed = make_url(url)
        except (ArgumentError, ValueError):
            raise AnchorNotConfigured("MAILROOM_ANCHOR_URL is not a valid database URL") from None
        if parsed.drivername in {"postgresql", "postgres"}:
            parsed = parsed.set(drivername="postgresql+psycopg")
        if key and parsed.drivername.startswith("postgresql"):
            parsed = parsed.set(password=key)
        connect_args = (
            {"connect_timeout": int(TIMEOUT_S)}
            if parsed.drivername.startswith("postgresql")
            else {}
        )
        if (
            parsed.drivername.startswith("postgresql")
            and "sslmode" not in parsed.query
            and (parsed.host or "") not in LOOPBACK
        ):
            # verify the certificate and host name unless the operator chose otherwise;
            # a private CA needs ``sslrootcert`` in the DSN
            connect_args["sslmode"] = "verify-full"
        try:
            self._engine = create_engine(parsed, connect_args=connect_args)
        except ModuleNotFoundError:
            raise AnchorNotConfigured(
                "the postgres anchor needs the 'anchor' extra (psycopg)"
            ) from None
        meta = MetaData()
        self._table = Table(
            "mailroom_anchor",
            meta,
            Column("seq", BigInteger, primary_key=True, autoincrement=False),
            Column("entry_hash", String, nullable=False),
        )
        if (
            parsed.get_backend_name() == "sqlite"
        ):  # tests only: Postgres has its own DDL
            meta.create_all(self._engine)

    def _run(self, fn: Any) -> Any:
        from sqlalchemy.exc import SQLAlchemyError

        try:
            with self._engine.begin() as conn:
                return fn(conn)
        except SQLAlchemyError as exc:
            raise AnchorUnreachable(
                f"anchor database error ({type(exc).__name__})"
            ) from None
        except ModuleNotFoundError:
            raise AnchorNotConfigured(
                "the postgres anchor needs the 'anchor' extra (psycopg)"
            ) from None

    def head(self) -> Head | None:
        """Highest anchor."""
        from sqlalchemy import select

        t = self._table
        row = self._run(
            lambda c: c.execute(select(t).order_by(t.c.seq.desc()).limit(1)).first()
        )
        return Head(int(row.seq), str(row.entry_hash)) if row else None

    def get(self, seq: int) -> str | None:
        """The hash at ``seq``."""
        from sqlalchemy import select

        t = self._table
        row = self._run(
            lambda c: c.execute(select(t.c.entry_hash).where(t.c.seq == seq)).first()
        )
        return str(row.entry_hash) if row else None

    def push(self, seq: int, entry_hash: str) -> None:
        """Insert; the client also rejects a non-increasing seq (the server trigger is the real guard)."""
        top = self.head()
        if top is not None and seq <= top.seq:
            if self.get(seq) == entry_hash:
                return
            raise AnchorConflict(
                f"seq {seq} is not greater than the anchored head {top.seq}"
            )
        try:
            self._run(
                lambda c: c.execute(
                    self._table.insert().values(seq=seq, entry_hash=entry_hash)
                )
            )
        except AnchorUnreachable:
            if (
                self.get(seq) == entry_hash
            ):  # lost response / concurrent pusher: same hash is success
                return
            raise

    def close(self) -> None:
        """Dispose the engine."""
        self._engine.dispose()


def make_backend(cfg: AnchorConfig) -> Backend:
    """The remote store for ``cfg`` (``export`` has none)."""
    if cfg.backend == "supabase":
        assert cfg.url and cfg.key
        return SupabaseBackend(cfg.url, cfg.key)
    if cfg.backend == "postgres":
        assert cfg.url
        return SqlBackend(cfg.url, cfg.key)
    raise AnchorNotConfigured(f"backend {cfg.backend!r} has no remote store")


# --------------------------------------------------------------------------- push
@dataclass(frozen=True)
class PushResult:
    """Outcome of one push."""

    status: str  # pushed | already | empty
    seq: int | None = None
    entry_hash: str | None = None


def _local_hash_at(ledger: Ledger, seq: int) -> str | None:
    rows = ledger.entries(since_seq=seq - 1, limit=1)
    return rows[0].entry_hash if rows and rows[0].seq == seq else None


def push_head(ledger: Ledger, backend: Backend) -> PushResult:
    """Anchor the current ledger head, unless the store already holds it.

    Refuses (``AnchorConflict``) to push a head whose history contradicts what is already
    anchored: a shorter ledger (truncation) or a different hash at the anchored seq (rewrite).
    """
    ledger.flush()
    head = ledger.head()
    if head is None:
        return PushResult("empty")
    remote = backend.head()
    if remote is not None:
        if remote.seq > head.seq:
            raise AnchorConflict(
                f"TRUNCATED: anchored head {remote.seq} is ahead of the ledger ({head.seq})"
            )
        if _local_hash_at(ledger, remote.seq) != remote.entry_hash:
            raise AnchorConflict(
                f"REWRITTEN: ledger seq {remote.seq} differs from the anchored hash"
            )
        if remote.seq == head.seq:
            return PushResult("already", head.seq, head.entry_hash)
    try:
        backend.push(head.seq, head.entry_hash)
    except AnchorConflict:
        # another writer may have anchored the same or a newer head in the meantime;
        # that is fine when it is a prefix of our chain, tamper otherwise
        ledger.flush()
        latest = backend.head()
        if (
            latest is not None
            and latest.seq >= head.seq
            and _local_hash_at(ledger, latest.seq) == latest.entry_hash
        ):
            return PushResult("already", latest.seq, latest.entry_hash)
        raise
    return PushResult("pushed", head.seq, head.entry_hash)


# --------------------------------------------------------------------------- verify
@dataclass(frozen=True)
class ExternalVerify:
    """Result of comparing the ledger with the anchor store."""

    status: str  # ok | unanchored | truncated | rewritten | stale | unreachable | not_configured
    exit_code: int
    anchored_seq: int | None = None
    local_seq: int | None = None
    unanchored: int = 0
    detail: str = ""
    key_file_warning: bool = False


def verify_external(
    ledger: Ledger,
    cfg: AnchorConfig,
    backend: Backend,
    *,
    now: datetime | None = None,
) -> ExternalVerify:
    """Compare the ledger with the anchored head.

    ``stale`` is raised only when the oldest unanchored entry is older than 24 h, so an
    idle system never alarms.
    """
    now = now or datetime.now(UTC)
    warn = cfg.key_file_world_readable
    try:
        remote = backend.head()
    except AnchorUnreachable as exc:
        return ExternalVerify(
            "unreachable", EXIT_UNREACHABLE, detail=str(exc), key_file_warning=warn
        )
    ledger.flush()
    head = ledger.head()
    local_seq = head.seq if head else 0
    if remote is None:
        if head is None:
            return ExternalVerify("ok", EXIT_OK, None, 0, 0, "nothing to anchor", warn)
        return _tail("unanchored", ledger, None, local_seq, now, warn)
    if remote.seq > local_seq:
        return ExternalVerify(
            "truncated", EXIT_TAMPER, remote.seq, local_seq, 0,
            f"anchored seq {remote.seq} is beyond the ledger head {local_seq}", warn,
        )  # fmt: skip
    if _local_hash_at(ledger, remote.seq) != remote.entry_hash:
        return ExternalVerify(
            "rewritten", EXIT_TAMPER, remote.seq, local_seq, 0,
            f"ledger entry {remote.seq} differs from its anchored hash", warn,
        )  # fmt: skip
    if remote.seq == local_seq:
        return ExternalVerify("ok", EXIT_OK, remote.seq, local_seq, 0, "", warn)
    return _tail("ok", ledger, remote.seq, local_seq, now, warn)


def _tail(
    status: str,
    ledger: Ledger,
    anchored: int | None,
    local_seq: int,
    now: datetime,
    warn: bool,
) -> ExternalVerify:
    """Report the unanchored tail; ``stale`` when its oldest entry is over 24 h old."""
    first: LedgerEntry = ledger.entries(since_seq=anchored or 0, limit=1)[0]
    try:
        age = now - datetime.fromisoformat(first.ts)
    except (ValueError, TypeError):
        age = timedelta(0)
    unanchored = local_seq - (anchored or 0)
    if age > STALE_AFTER:
        return ExternalVerify(
            "stale", EXIT_STALE, anchored, local_seq, unanchored,
            f"{unanchored} entries have waited for an anchor for {age.days * 24 + age.seconds // 3600} h", warn,
        )  # fmt: skip
    return ExternalVerify(status, EXIT_OK, anchored, local_seq, unanchored, "", warn)


def export_head(ledger: Ledger) -> dict[str, Any] | None:
    """The ledger head as a pinnable record (``seq``, ``entry_hash``, ``count``, ``ts``)."""
    ledger.flush()
    head = ledger.head()
    if head is None:
        return None
    return {
        "seq": head.seq,
        "entry_hash": head.entry_hash,
        "ts": head.ts,
        "exported_at": datetime.now(UTC).isoformat(),
    }


# --------------------------------------------------------------------------- background push
_state_lock = threading.Lock()
_wanted = threading.Event()
_worker: threading.Thread | None = None
_ledger_ref: Ledger | None = None


def schedule_push(ledger: Ledger) -> None:
    """Ask the daemon thread to anchor the head soon (coalesced; never blocks, never raises)."""
    global _worker, _ledger_ref
    try:
        get_config_quiet = _configured_backend()
        if get_config_quiet is None:
            return
        with _state_lock:
            _ledger_ref = ledger
            _wanted.set()
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(
                    target=_loop, name="ledger-anchor", daemon=True
                )
                _worker.start()
    except Exception:
        logger.warning("anchor_schedule_failed", exc_info=True)


def _configured_backend() -> str | None:
    """The remote backend name when configured for background pushes, else ``None``."""
    backend = get_settings().anchor
    return backend if backend in {"supabase", "postgres"} else None


def _loop() -> None:
    while True:
        _wanted.wait()
        _wanted.clear()
        with _state_lock:
            ledger = _ledger_ref
        if ledger is not None:
            _push_with_retries(ledger)


def _settings_redact(text: str) -> str:
    """``text`` redacted with whatever secrets the settings hold (when no config resolved)."""
    s = get_settings()
    return AnchorConfig(s.anchor, s.anchor_url, s.anchor_key).redact(text)


def _push_with_retries(
    ledger: Ledger, delays: tuple[float, ...] = RETRY_DELAYS_S
) -> bool:
    """Up to ``len(delays)`` attempts; logs redacted errors; returns whether it succeeded."""
    cfg: AnchorConfig | None = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            cfg = get_config()
            backend = make_backend(cfg)
            try:
                push_head(ledger, backend)
            finally:
                close = getattr(backend, "close", None)
                if close:
                    close()
            return True
        except AnchorConflict as exc:
            logger.error(
                "anchor_conflict", error=_settings_redact(str(exc))
            )  # tamper signal: never retried
            return False
        except Exception as exc:  # noqa: BLE001
            text = cfg.redact(str(exc)) if cfg else _settings_redact(str(exc))
            logger.warning("anchor_push_failed", attempt=attempt, error=text)
            if attempt < len(delays):
                time.sleep(delay)
    return False


def install(ledger: Ledger) -> None:
    """Hook ``ledger`` so closing a run (or a retention change) schedules an anchor push, and push once now."""

    def on_commit(kinds: set[str]) -> None:
        if kinds & TRIGGER_KINDS:
            schedule_push(ledger)

    ledger.add_commit_hook(on_commit)
    schedule_push(ledger)  # startup: catch up on anything unanchored


def key_env_present() -> bool:
    """Whether an anchor key is available (for health output); never reveals it."""
    s = get_settings()
    return bool(
        s.anchor_key or s.anchor_key_file or os.environ.get("MAILROOM_ANCHOR_KEY")
    )
