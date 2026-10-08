"""Offline tests for the Gmail attachment intake.

A fake Gmail service object is injected into :class:`GmailIntake` so no test
touches the network or real credentials. The pipeline test reuses the
``FakeOpenAI`` + BERT-patch harness from ``tests/api/test_api.py``.
"""

from __future__ import annotations

import base64
import json
import sys

import pytest
from fakes.openai_server import FakeOpenAI
from fastapi.testclient import TestClient

from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.intake import gmail as gmail_intake
from mailroom_reloaded.intake.gmail import (
    GmailAuthError,
    GmailConfig,
    GmailIntake,
    GmailNotInstalled,
    decode_pubsub_push,
)
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.storage.bins import Bins, doc_id_for
from mailroom_reloaded.watcher import Watcher

CORR_SUBCLASS = {
    "doc_subclass": "email",
    "confidence": 0.99,
    "doc_type_disagree": False,
    "doc_type_disagree_reason": None,
}

CORR_EXTRACT = {
    "sender": "alice@example.com",
    "recipient": "bob@example.com",
    "additional_recipients": ["carol@example.com"],
    "communication_type": "email",
    "communication_date": "2026-01-02",
    "demand_amount": 1000.0,
    "action_items": ["reply by Friday"],
    "urgency": "normal",
    "intent": "request",
    "subject_matter": "the deal",
    "keywords": ["deal"],
    "confidence": 0.9,
}

LETTER = b"A short business letter about the deal."


# --------------------------------------------------------------------------- fake Gmail


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _part(filename, mime, *, data=None, attachment_id=None, size=None):
    body: dict = {}
    if data is not None:
        body["size"] = len(data) if size is None else size
        body["data"] = _b64(data)
    elif size is not None:
        body["size"] = size
    if attachment_id is not None:
        body["attachmentId"] = attachment_id
    return {"filename": filename, "mimeType": mime, "body": body}


def _message(mid: str, parts: list[dict]) -> dict:
    return {
        "id": mid,
        "threadId": f"t-{mid}",
        "payload": {"mimeType": "multipart/mixed", "parts": parts},
    }


class _Request:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _AttachmentsResource:
    def __init__(self, service):
        self._service = service

    def get(self, userId, messageId, id):
        return _Request({"size": len(self._service.blobs.get(id, b"")), "data": _b64(self._service.blobs.get(id, b""))})


class _MessagesResource:
    def __init__(self, service):
        self._service = service

    def list(self, userId, q, maxResults):
        stubs = [{"id": mid} for mid in self._service.message_ids]
        return _Request({"messages": stubs[:maxResults]})

    def get(self, userId, id, format):
        return _Request(self._service.by_id[id])

    def attachments(self):
        return _AttachmentsResource(self._service)


class FakeGmailService:
    """Minimal stand-in for ``googleapiclient``'s chained resources."""

    def __init__(self, message_ids, messages, blobs=None):
        self.message_ids = list(message_ids)
        self.by_id = dict(messages)
        self.blobs = dict(blobs or {})

    def users(self):
        return self

    def messages(self):
        return _MessagesResource(self)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def fake_openai():
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    from mailroom_reloaded import settings

    settings.get_settings.cache_clear()
    from mailroom_reloaded.storage import db

    monkeypatch.setattr(db, "_default_engine", None)
    try:
        yield tmp_path
    finally:
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()


@pytest.fixture
def client(env):
    from mailroom_reloaded.api.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    from mailroom_reloaded.llm import retry, tooling

    monkeypatch.setattr(retry, "_sleep", lambda *_: None)
    tooling.reset_tool_support_cache()
    yield
    tooling.reset_tool_support_cache()


def _patch_handoff(monkeypatch, doc_type="correspondence"):
    verdict = BertVerdict(
        available=True,
        reason="ok",
        doc_type=doc_type,
        subclass=None,
        calibrated_confidence=0.99,
        margin=0.5,
        window_agreement=1.0,
        n_windows=1,
        route="fast_path",
    )
    handoff = Handoff(SortMode.SUBCLASS_ONLY, doc_type, f"BERT predicts {doc_type}", "fast_path")
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)


def _reply(provider, payload):
    provider.reply("thinking").reply(json.dumps(payload))


def _config(tmp_path, **overrides) -> GmailConfig:
    base = {
        "credentials_path": tmp_path / "creds.json",
        "token_path": tmp_path / "token.json",
        "state_path": tmp_path / "gmail_state.json",
    }
    base.update(overrides)
    return GmailConfig(**base)


# --------------------------------------------------------------------------- tests


def test_attachment_filtering_by_extension_and_size(tmp_path):
    service = FakeGmailService(
        ["m1"],
        {"m1": None},
        blobs={"a1": b"%PDF-1.4 small"},
    )
    service.by_id["m1"] = _message(
        "m1",
        [
            _part("letter.txt", "text/plain", data=LETTER),
            _part("notes.exe", "application/octet-stream", data=b"MZ executable"),
            _part("big.pdf", "application/pdf", data=b"x" * 200, size=200),
            _part("report.pdf", "application/pdf", attachment_id="a1"),
        ],
    )
    bins = Bins(tmp_path)
    intake = GmailIntake(
        _config(tmp_path, max_attachment_bytes=50, allowed_extensions=(".txt", ".pdf")),
        bins=bins,
        service=service,
    )

    doc_ids = intake.ingest_attachments(service.by_id["m1"])

    assert sorted(p.name for p in bins.inbox.iterdir()) == ["letter.txt", "report.pdf"]
    assert len(doc_ids) == 2
    assert doc_id_for(bins.inbox / "letter.txt") in doc_ids
    assert doc_id_for(bins.inbox / "report.pdf") in doc_ids


def test_supported_attachment_is_processable_by_pipeline(env, fake_openai, monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    _patch_handoff(monkeypatch)
    _reply(fake_openai, CORR_SUBCLASS)
    _reply(fake_openai, CORR_EXTRACT)

    service = FakeGmailService(
        ["m1"], {"m1": _message("m1", [_part("letter.txt", "text/plain", data=LETTER)])}
    )
    bins = Bins(env)
    intake = GmailIntake(_config(env), bins=bins, service=service)

    doc_ids = intake.poll()

    assert len(doc_ids) == 1
    assert (bins.inbox / "letter.txt").is_file()
    assert Watcher(bins, "w1", 1).drain_once() == 1

    from mailroom_reloaded.storage.catalog import get as catalog_get

    record = catalog_get(doc_ids[0])
    assert record is not None and record.status == "archived"


def test_repoll_does_not_duplicate(tmp_path):
    service = FakeGmailService(
        ["m1"], {"m1": _message("m1", [_part("a.txt", "text/plain", data=b"hello")])}
    )
    bins = Bins(tmp_path)
    intake = GmailIntake(_config(tmp_path), bins=bins, service=service)

    first = intake.poll()
    second = intake.poll()

    assert len(first) == 1
    assert second == []
    assert len(list(bins.inbox.iterdir())) == 1
    assert intake.processed_message_ids() == {"m1"}
    assert (tmp_path / "gmail_state.json").is_file()


def test_decode_pubsub_push():
    data = _b64(json.dumps({"emailAddress": "user@example.com", "historyId": "42"}).encode())
    payload = {
        "message": {"data": data, "messageId": "pubsub-1"},
        "subscription": "projects/p/subscriptions/s",
    }

    note = decode_pubsub_push(payload)

    assert note.email_address == "user@example.com"
    assert note.history_id == "42"
    assert note.message_id == "pubsub-1"
    assert note.subscription == "projects/p/subscriptions/s"

    with pytest.raises(ValueError):
        decode_pubsub_push({})
    with pytest.raises(ValueError):
        decode_pubsub_push({"message": {"data": "!!!not-base64url!!!"}})


def test_missing_extra_names_the_extra(monkeypatch, tmp_path):
    for name in (
        "google.auth.transport.requests",
        "google.oauth2.credentials",
        "google_auth_oauthlib.flow",
        "googleapiclient.discovery",
    ):
        monkeypatch.setitem(sys.modules, name, None)

    intake = GmailIntake(_config(tmp_path))

    with pytest.raises(GmailNotInstalled) as excinfo:
        intake.authenticate()

    message = str(excinfo.value)
    assert "gmail" in message
    assert "extra" in message


def test_authenticate_is_headless_safe(tmp_path, monkeypatch):
    # With the extra importable but no TTY and no token, auth must not hang.
    monkeypatch.setattr(gmail_intake, "_load_google", lambda: (object, object, object, object))
    monkeypatch.setattr(gmail_intake, "_interactive", lambda: False)

    intake = GmailIntake(_config(tmp_path))

    with pytest.raises(GmailAuthError):
        intake.authenticate()


def test_poll_endpoint_returns_doc_ids(env, client, monkeypatch):
    monkeypatch.setattr(
        gmail_intake, "poll_and_ingest", lambda limit=None: ["abc123", "def456"]
    )

    resp = client.post("/v1/intake/gmail/poll")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"doc_ids": ["abc123", "def456"], "count": 2}


def test_poll_endpoint_reports_missing_extra(env, client, monkeypatch):
    def boom(limit=None):
        raise GmailNotInstalled("requires the 'gmail' extra")

    monkeypatch.setattr(gmail_intake, "poll_and_ingest", boom)

    resp = client.post("/v1/intake/gmail/poll")

    assert resp.status_code == 503
    assert "gmail" in resp.json()["detail"]


def test_webhook_returns_204_and_requires_auth(env, client, monkeypatch):
    # ``from mailroom_reloaded.api import app`` would bind the FastAPI instance;
    # reach the module object through sys.modules to patch its task function.
    app_mod = sys.modules["mailroom_reloaded.api.app"]

    calls: list[int] = []
    monkeypatch.setattr(app_mod, "_gmail_poll_task", lambda: calls.append(1))

    body = {
        "message": {
            "data": _b64(json.dumps({"emailAddress": "u@example.com", "historyId": "9"}).encode()),
            "messageId": "pubsub-2",
        },
        "subscription": "projects/p/subscriptions/s",
    }

    resp = client.post("/v1/intake/gmail", json=body)
    assert resp.status_code == 204, resp.text
    assert calls == [1]

    bad = client.post("/v1/intake/gmail", json={"message": {}})
    assert bad.status_code == 400

    monkeypatch.setenv("MAILROOM_API_TOKEN", "s3cret")
    from mailroom_reloaded import settings as settings_mod

    settings_mod.get_settings.cache_clear()

    assert client.post("/v1/intake/gmail", json=body).status_code == 401
    ok = client.post(
        "/v1/intake/gmail", json=body, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 204


@pytest.mark.parametrize("workers", ["threads", "processes"])
def test_concurrent_polls_ingest_message_once(tmp_path, workers):
    import multiprocessing
    import queue
    import threading

    context = multiprocessing.get_context("fork")
    worker_type = threading.Thread if workers == "threads" else context.Process
    event_type = threading.Event if workers == "threads" else context.Event
    result_queue = queue.Queue() if workers == "threads" else context.Queue()
    first_fetch = event_type()
    release = event_type()
    second_started = event_type()
    second_fetch = event_type()
    message = _message("m1", [_part("letter.txt", "text/plain", data=LETTER)])
    bins = Bins(tmp_path)

    def poll(first):
        intake = GmailIntake(
            _config(tmp_path), bins=bins,
            service=FakeGmailService(["m1"], {"m1": message}),
        )
        original_fetch = intake.fetch_new

        def fetch(limit=None):
            (first_fetch if first else second_fetch).set()
            if first:
                assert release.wait(5)
            return original_fetch(limit)

        intake.fetch_new = fetch
        if not first:
            second_started.set()
        result_queue.put(intake.poll())

    first = worker_type(target=poll, args=(True,))
    second = worker_type(target=poll, args=(False,))
    first.start()
    try:
        assert first_fetch.wait(5)
        second.start()
        assert second_started.wait(5)
        assert not second_fetch.wait(0.2)
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    results = [result_queue.get(timeout=2), result_queue.get(timeout=2)]
    assert sorted(map(len, results)) == [0, 1]
    assert len(list(bins.inbox.iterdir())) == 1
    assert json.loads((tmp_path / "gmail_state.json").read_text())["processed"] == ["m1"]
    assert not list(tmp_path.glob("*.tmp"))


def test_state_saves_use_distinct_temporary_paths(tmp_path, monkeypatch):
    intake = GmailIntake(_config(tmp_path), bins=Bins(tmp_path))
    paths = []
    original_replace = gmail_intake.os.replace

    def replace(source, dest):
        paths.append(source)
        if len(paths) == 1:
            intake._save_state({"processed": ["inner"]})
        original_replace(source, dest)

    monkeypatch.setattr(gmail_intake.os, "replace", replace)
    intake._save_state({"processed": ["outer"]})
    assert len(set(paths)) == 2
    assert intake.processed_message_ids() == {"outer"}
    assert not list(tmp_path.glob("*.tmp"))
