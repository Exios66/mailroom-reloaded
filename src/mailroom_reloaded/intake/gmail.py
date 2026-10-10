"""Gmail attachment intake: list supported attachments and queue them in ``inbox/``.

This is a scope amendment beyond the original design (spec section 1 listed Gmail
intake as out of scope). It adds a thin, optional adapter that turns a Gmail
mailbox into a document source for the existing pipeline:

* :class:`GmailIntake` authenticates with the installed-app OAuth flow, lists
  messages matching a query, decodes supported attachments and writes each one
  into ``inbox/`` via the same content-addressed path as the HTTP upload route.
* Processed Gmail message ids are persisted in a small JSON state file under the
  mailroom data dir, so repeated polls and push events never re-ingest a message.
* :func:`decode_pubsub_push` decodes the Cloud Pub/Sub push envelope the Gmail
  API sends on mailbox changes.

Contracts verified against the official docs (2026-09-15 revisions):

* Push notification envelope — ``message.data`` is a Base64URL-encoded JSON
  object ``{"emailAddress": ..., "historyId": ...}``; acknowledge by returning
  HTTP 200:
  https://developers.google.com/gmail/api/guides/push
* ``users.messages.list`` — ``q`` + ``maxResults`` return ``messages[]`` stubs:
  https://developers.google.com/gmail/api/reference/rest/v1/users.messages/list
* ``users.messages.get`` / ``users.messages.attachments.get`` — a ``full``
  message carries ``payload.parts[].body`` with ``data`` (small parts) or
  ``attachmentId`` (fetched separately); both are Base64URL:
  https://developers.google.com/gmail/api/reference/rest/v1/users.messages/get
  https://developers.google.com/gmail/api/reference/rest/v1/users.messages.attachments/get

The Google client libraries are an optional extra (``mailroom-reloaded[gmail]``);
imports are lazy so the rest of the mailroom never depends on them.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from mailroom_reloaded.settings import get_settings
from mailroom_reloaded.storage.bins import Bins

logger = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_ALLOWED_EXTENSIONS",
    "DEFAULT_MAX_ATTACHMENT_BYTES",
    "DEFAULT_QUERY",
    "GmailAuthError",
    "GmailConfig",
    "GmailIntake",
    "GmailNotInstalled",
    "GmailNotification",
    "decode_pubsub_push",
    "poll_and_ingest",
]

#: Only the read scope is requested: listing + fetching messages and attachments,
#: and registering a ``users.watch`` push subscription. Least privilege.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

DEFAULT_QUERY = "is:unread has:attachment"
DEFAULT_ALLOWED_EXTENSIONS: tuple[str, ...] = (
    ".pdf",
    ".docx",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
)
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
DEFAULT_FETCH_LIMIT = 25
#: Upper bound on list calls per poll, so a mailbox full of processed mail cannot page forever.
MAX_LIST_PAGES_PER_POLL = 20
_STATE_KIND = "mailroom.gmail.state/v1"
_POLL_LOCK = threading.Lock()

_MISSING_EXTRA_MESSAGE = (
    "Gmail intake requires the optional 'gmail' extra: "
    "install it with `pip install 'mailroom-reloaded[gmail]'` "
    "(or `uv sync --extra gmail`)."
)


class GmailNotInstalled(RuntimeError):
    """The optional Google client libraries are not installed."""


class GmailAuthError(RuntimeError):
    """OAuth cannot complete (no valid token and no interactive terminal)."""


# --------------------------------------------------------------------------- config


@dataclass
class GmailConfig:
    """Configuration for the Gmail intake.

    ``allowed_extensions`` are normalised to lower-case, leading-dot form.
    ``max_attachment_bytes`` bounds each attachment (inclusive).
    """

    credentials_path: Path = Path("data/gmail_credentials.json")
    token_path: Path = Path("data/gmail_token.json")
    query: str = DEFAULT_QUERY
    allowed_extensions: tuple[str, ...] = DEFAULT_ALLOWED_EXTENSIONS
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES
    state_path: Path | None = None
    fetch_limit: int = DEFAULT_FETCH_LIMIT

    def __post_init__(self) -> None:
        self.credentials_path = Path(self.credentials_path)
        self.token_path = Path(self.token_path)
        if self.state_path is not None:
            self.state_path = Path(self.state_path)
        normalised: list[str] = []
        for ext in self.allowed_extensions:
            cleaned = str(ext).strip().lower()
            if not cleaned:
                continue
            normalised.append(cleaned if cleaned.startswith(".") else f".{cleaned}")
        self.allowed_extensions = tuple(normalised)


@dataclass(frozen=True)
class GmailNotification:
    """A decoded Cloud Pub/Sub push notification from the Gmail API."""

    email_address: str
    history_id: str
    message_id: str | None = None
    subscription: str | None = None


# --------------------------------------------------------------------------- helpers


def _load_google():
    """Import the optional Google client stack or raise a clear error."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise GmailNotInstalled(_MISSING_EXTRA_MESSAGE) from exc
    return Request, Credentials, InstalledAppFlow, build


def _interactive() -> bool:
    """True when an interactive OAuth prompt is possible (a TTY on stdin/stdout)."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - detached streams
        return False


def _b64url_decode(value: str) -> bytes:
    """Decode a Base64URL string, tolerating missing padding."""
    if isinstance(value, bytes):
        value = value.decode("ascii")
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("value is not valid Base64URL data") from exc


def _safe_filename(name: str | None) -> str:
    """The basename of an attachment filename; empty when unusable/hidden."""
    if not name:
        return ""
    base = Path(str(name).replace("\\", "/")).name
    if not base or base.startswith(".") or "\x00" in base:
        return ""
    return base


def _iter_attachment_parts(payload: dict[str, Any] | None) -> Iterator[dict[str, Any]]:
    """Yield every MIME part that carries a named attachment body."""
    if not isinstance(payload, dict):
        return
    body = payload.get("body")
    if (
        payload.get("filename")
        and isinstance(body, dict)
        and (body.get("attachmentId") or body.get("data"))
    ):
        yield payload
    for child in payload.get("parts") or []:
        yield from _iter_attachment_parts(child)


def decode_pubsub_push(payload: dict[str, Any]) -> GmailNotification:
    """Decode a Cloud Pub/Sub push body into a :class:`GmailNotification`.

    Raises :class:`ValueError` for a malformed envelope so the webhook can 400.
    """
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        # ValueError (not TypeError) is the webhook's 400 contract.
        raise ValueError("Pub/Sub push body is missing the 'message' object")  # noqa: TRY004
    raw = message.get("data")
    if not raw or not isinstance(raw, str):
        raise ValueError("Pub/Sub push message is missing the base64url 'data' field")
    try:
        decoded = _b64url_decode(raw)
        data = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Pub/Sub push 'data' is not Base64URL-encoded JSON") from exc
    if not isinstance(data, dict):
        # ValueError (not TypeError) is the webhook's 400 contract.
        raise ValueError(  # noqa: TRY004
            "Pub/Sub push 'data' did not decode to a JSON object"
        )
    return GmailNotification(
        email_address=str(data.get("emailAddress") or ""),
        history_id=str(data.get("historyId") or ""),
        message_id=message.get("messageId"),
        subscription=payload.get("subscription"),
    )


def _parse_extensions(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    exts = tuple(part.strip() for part in raw.replace(";", ",").split(",") if part.strip())
    return exts or None


# --------------------------------------------------------------------------- intake


class GmailIntake:
    """Authenticate, list new mail, and queue supported attachments in ``inbox/``.

    Passing ``service`` injects an already-built (or fake) Gmail client and skips
    all Google imports, which is how the tests stay offline.
    """

    def __init__(
        self,
        config: GmailConfig | None = None,
        *,
        bins: Bins | None = None,
        service: Any | None = None,
    ) -> None:
        self.config = config or GmailConfig()
        self._bins = bins
        self._service = service

    # ------------------------------------------------------------- construction
    @classmethod
    def from_env(cls, *, service: Any | None = None) -> GmailIntake:
        """Build an intake from ``MAILROOM_GMAIL_*`` env vars and the data dir."""
        settings = get_settings()
        base = Path(settings.base_dir)
        credentials = os.environ.get("MAILROOM_GMAIL_CREDENTIALS")
        token = os.environ.get("MAILROOM_GMAIL_TOKEN")
        state = os.environ.get("MAILROOM_GMAIL_STATE")
        max_bytes = os.environ.get("MAILROOM_GMAIL_MAX_ATTACHMENT_BYTES")
        cfg = GmailConfig(
            credentials_path=Path(credentials) if credentials else base / "gmail_credentials.json",
            token_path=Path(token) if token else base / "gmail_token.json",
            query=os.environ.get("MAILROOM_GMAIL_QUERY") or DEFAULT_QUERY,
            allowed_extensions=_parse_extensions(os.environ.get("MAILROOM_GMAIL_EXTENSIONS"))
            or DEFAULT_ALLOWED_EXTENSIONS,
            max_attachment_bytes=int(max_bytes) if max_bytes else DEFAULT_MAX_ATTACHMENT_BYTES,
            state_path=Path(state) if state else base / "gmail_state.json",
        )
        return cls(cfg, service=service)

    @property
    def bins(self) -> Bins:
        if self._bins is None:
            self._bins = Bins(get_settings().base_dir)
        return self._bins

    # ------------------------------------------------------------- auth
    def authenticate(self) -> Any:
        """Return an authenticated Gmail client (OAuth installed-app flow).

        Headless-safe: without a usable cached token and without a TTY this
        raises :class:`GmailAuthError` instead of blocking on a browser prompt.
        """
        if self._service is not None:
            return self._service
        Request, Credentials, InstalledAppFlow, build = _load_google()

        creds = None
        token_path = self.config.token_path
        if token_path and Path(token_path).is_file():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            except (ValueError, OSError):
                creds = None
        if creds is not None and not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:  # noqa: BLE001 - refresh failures fall back to re-auth
                    creds = None
            else:
                creds = None
        if creds is None:
            if not _interactive():
                raise GmailAuthError(
                    "No valid Gmail token and no interactive terminal to start the "
                    f"OAuth flow. Run `mailroom gmail auth` at a terminal, or place a "
                    f"token at {token_path}."
                )
            if not Path(self.config.credentials_path).is_file():
                raise GmailAuthError(
                    "Missing OAuth client secrets at "
                    f"{self.config.credentials_path} (see docs/gmail-intake.md)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.config.credentials_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            _write_private(token_path, creds.to_json())

        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    # ------------------------------------------------------------- listing
    def fetch_new(self, limit: int | None = None) -> list[dict[str, Any]]:
        """List message stubs matching the query, excluding already-processed ids."""
        service = self.authenticate()
        bound = int(limit) if limit is not None else self.config.fetch_limit
        if bound <= 0:
            return []
        processed = self.processed_message_ids()
        fresh: list[dict[str, Any]] = []
        seen = set(processed)
        page_token = None
        visited_tokens: set[str] = set()
        pages = 0
        while len(fresh) < bound and pages < MAX_LIST_PAGES_PER_POLL:
            pages += 1
            params = {"userId": "me", "q": self.config.query, "maxResults": min(500, bound)}
            if page_token:
                params["pageToken"] = page_token
            response = service.users().messages().list(**params).execute()
            for stub in response.get("messages") or []:
                if isinstance(stub, dict) and stub.get("id") and stub["id"] not in seen:
                    fresh.append(stub)
                    seen.add(stub["id"])
                    if len(fresh) == bound:
                        break
            page_token = response.get("nextPageToken")
            if not page_token or page_token in visited_tokens:
                break
            visited_tokens.add(page_token)
        return fresh

    def _get_message(self, message_id: str) -> dict[str, Any]:
        return (
            self.authenticate()
            .users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

    # ------------------------------------------------------------- ingest
    def ingest_attachments(self, message: dict[str, Any]) -> list[str]:
        """Decode supported attachments from ``message`` into ``inbox/``.

        Returns the content-addressed ``doc_id`` for each attachment written.
        """
        message_id = str(message.get("id") or "")
        doc_ids: list[str] = []
        for part in _iter_attachment_parts(message.get("payload") or {}):
            filename = _safe_filename(part.get("filename"))
            if not filename:
                continue
            suffix = Path(filename).suffix.lower()
            if suffix not in self.config.allowed_extensions:
                logger.info(
                    "gmail_attachment_skipped", reason="extension", filename=filename
                )
                continue
            body = part.get("body") or {}
            declared = body.get("size")
            if isinstance(declared, int) and declared > self.config.max_attachment_bytes:
                logger.info(
                    "gmail_attachment_skipped",
                    reason="size",
                    filename=filename,
                    size=declared,
                )
                continue
            content = self._read_attachment_body(message_id, body)
            if not content:
                logger.info(
                    "gmail_attachment_skipped", reason="empty", filename=filename
                )
                continue
            if len(content) > self.config.max_attachment_bytes:
                logger.info(
                    "gmail_attachment_skipped",
                    reason="size",
                    filename=filename,
                    size=len(content),
                )
                continue
            doc_ids.append(self._write_to_inbox(content, filename))
        return doc_ids

    def _read_attachment_body(self, message_id: str, body: dict[str, Any]) -> bytes:
        raw = body.get("data")
        if raw:
            return _b64url_decode(raw)
        attachment_id = body.get("attachmentId")
        if not attachment_id:
            return b""
        response = (
            self.authenticate()
            .users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        return _b64url_decode(response.get("data") or "")

    def _write_to_inbox(self, content: bytes, filename: str) -> str:
        dest = self.bins.enqueue(content, filename)
        doc_id = hashlib.sha256(content).hexdigest()[:16]
        logger.info(
            "gmail_attachment_ingested",
            doc_id=doc_id,
            file=dest.name,
            size=len(content),
        )
        return doc_id

    # ------------------------------------------------------------- poll loop
    def poll(self, limit: int | None = None) -> list[str]:
        """Fetch new messages and ingest their attachments; return created doc_ids."""
        with _POLL_LOCK:
            lock_path = self.state_path.with_name(f"{self.state_path.name}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    return self._poll_locked(limit)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _poll_locked(self, limit: int | None) -> list[str]:
        """Poll while holding both the process and state-file locks."""
        doc_ids: list[str] = []
        for stub in self.fetch_new(limit=limit):
            message_id = str(stub.get("id") or "")
            if not message_id:
                continue
            message = self._get_message(message_id)
            created = self.ingest_attachments(message)
            self.mark_processed(message_id)
            doc_ids.extend(created)
        return doc_ids

    # ------------------------------------------------------------- idempotency
    @property
    def state_path(self) -> Path:
        if self.config.state_path is not None:
            return Path(self.config.state_path)
        return Path(get_settings().base_dir) / "gmail_state.json"

    def _load_state(self) -> dict[str, Any]:
        path = self.state_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"kind": _STATE_KIND, "processed": []}
        if not isinstance(data, dict):
            return {"kind": _STATE_KIND, "processed": []}
        return data

    def processed_message_ids(self) -> set[str]:
        """The Gmail message ids already ingested (persisted under the data dir)."""
        raw = self._load_state().get("processed")
        if not isinstance(raw, list):
            return set()
        return {str(item) for item in raw if item}

    def mark_processed(self, message_id: str) -> None:
        """Persist ``message_id`` as processed (atomic replace; idempotent)."""
        state = self._load_state()
        processed = state.get("processed")
        if not isinstance(processed, list):
            processed = []
        if message_id not in processed:
            processed.append(message_id)
        state["kind"] = _STATE_KIND
        state["processed"] = processed
        self._save_state(state)

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f"{path.name}.", suffix=".tmp", delete=False,
        ) as fh:
            tmp = Path(fh.name)
            try:
                fh.write(json.dumps(state, indent=2))
                fh.close()
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable only by the owner (mode 0600)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def poll_and_ingest(limit: int | None = None) -> list[str]:
    """Convenience entry point: build from the environment, poll, return doc_ids."""
    return GmailIntake.from_env().poll(limit=limit)
