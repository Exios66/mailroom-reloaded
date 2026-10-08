"""Task 19 tests: the FastAPI ``/v1`` API, the ``/ui`` page and the bind guard.

The pipeline uses the ``mock`` provider through ``FakeOpenAI`` for the two
standalone LLM calls (sorter, specialist), and BERT is monkeypatched for the
deterministic scenarios, following ``tests/pipeline/test_flow.py``.
"""

from __future__ import annotations

import json

import pytest
from fakes.openai_server import FakeOpenAI
from fastapi.testclient import TestClient

from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.storage.bins import Bins
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

LOW_CLASS = {
    "doc_type": "correspondence",
    "doc_subclass": "email",
    "confidence": 0.5,
    "doc_type_disagree": False,
    "doc_type_disagree_reason": None,
}

LETTER = b"A short business letter about the deal."


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
def mock_provider(monkeypatch, fake_openai):
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


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


# --------------------------------------------------------------------------- helpers


def _patch_handoff(
    monkeypatch,
    mode=SortMode.SUBCLASS_ONLY,
    doc_type="correspondence",
    route="fast_path",
):
    locked = doc_type if mode is SortMode.SUBCLASS_ONLY else None
    verdict = BertVerdict(
        available=True,
        reason="ok",
        doc_type=doc_type,
        subclass=None,
        calibrated_confidence=0.99,
        margin=0.5,
        window_agreement=1.0,
        n_windows=1,
        route=route,
    )
    handoff = Handoff(mode, locked, f"BERT predicts class {doc_type}", route)
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)
    return verdict, handoff


def _patch_bert_unavailable(monkeypatch):
    verdict = BertVerdict(available=False, reason="flag_off")
    handoff = Handoff(SortMode.FULL, None, "", "bert_unavailable:flag_off")
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)


def _reply(provider, payload):
    """Queue a discarded tool-round draft then the final structured reply."""
    provider.reply("thinking").reply(json.dumps(payload))


def _upload(client, name="letter.txt", content=LETTER):
    resp = client.post(
        "/v1/documents",
        files={"file": (name, content, "text/plain")},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["doc_id"]


def _fast_path_doc(env, mock_provider, monkeypatch, client, name="letter.txt"):
    """Upload, drain and return (doc_id, bins) for an archived document."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    _reply(mock_provider, CORR_EXTRACT)
    doc_id = _upload(client, name=name)
    bins = Bins(env)
    assert Watcher(bins, "w1", 1).drain_once() == 1
    return doc_id, bins


def _parked_doc(env, mock_provider, monkeypatch, client):
    """Upload and drain a document to the review bin; return (doc_id, bins)."""
    _patch_bert_unavailable(monkeypatch)
    for _ in range(3):
        _reply(mock_provider, LOW_CLASS)
    doc_id = _upload(client)
    bins = Bins(env)
    assert Watcher(bins, "w1", 1).drain_once() == 1
    return doc_id, bins


# --------------------------------------------------------------------------- tests


def test_upload_then_process(env, mock_provider, monkeypatch, client):
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    _reply(mock_provider, CORR_EXTRACT)

    resp = client.post(
        "/v1/documents",
        files={"file": ("letter.txt", LETTER, "text/plain")},
    )
    assert resp.status_code == 202, resp.text
    doc_id = resp.json()["doc_id"]
    assert len(doc_id) == 16
    assert resp.json()["status"] == "accepted"

    # The upload is queued in the inbox; no manifest exists until it is drained.
    assert (Bins(env).inbox / "letter.txt").is_file()
    assert client.get(f"/v1/documents/{doc_id}").status_code == 404

    assert Watcher(Bins(env), "w1", 1).drain_once() == 1

    got = client.get(f"/v1/documents/{doc_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "archived"
    assert body["manifest"]["doc_id"] == doc_id
    assert body["report"] is not None


def test_audit_verify_ok(env, mock_provider, monkeypatch, client):
    doc_id, _bins = _fast_path_doc(env, mock_provider, monkeypatch, client)

    resp = client.get(f"/v1/audit/{doc_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chain"]["ok"] is True
    assert body["chain"]["broken_at"] is None
    assert body["entries"]
    assert all(entry["doc_id"] == doc_id for entry in body["entries"])


def test_review_resolve_endpoint(env, mock_provider, monkeypatch, client):
    doc_id, bins = _parked_doc(env, mock_provider, monkeypatch, client)

    parked = client.get(f"/v1/documents/{doc_id}").json()
    assert parked["status"] == "parked"
    assert list(bins.review.glob("*.txt"))

    resp = client.post(
        f"/v1/review/{doc_id}/resolve",
        json={"action": "reject", "reviewer": "alice"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["doc_id"] == doc_id
    assert body["status"] == "failed"

    assert client.get(f"/v1/documents/{doc_id}").json()["status"] == "failed"
    assert list(bins.failed.glob("*.txt"))

    # Unknown / already-resolved documents are a 404, not a crash.
    assert client.post(
        "/v1/review/deadbeefdeadbeef/resolve", json={"action": "approve"}
    ).status_code == 404


def test_ui_served(client):
    resp = client.get("/ui")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    text = resp.text
    assert "mailroom" in text.lower()
    assert "6006" in text  # Phoenix link
    assert "3000" in text  # Grafana link


def test_runs_shape(client):
    resp = client.get("/v1/runs")
    assert resp.status_code == 200
    assert resp.json() == {"runs": []}

    cards = client.get("/v1/runs/nope/cards")
    assert cards.status_code == 200
    assert cards.json() == {"run_id": "nope", "cards": []}


def test_offbind_without_token_refuses(env, monkeypatch):
    monkeypatch.delenv("MAILROOM_API_TOKEN", raising=False)
    from mailroom_reloaded import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    from mailroom_reloaded.api.app import assert_bind_allowed

    assert_bind_allowed("127.0.0.1")
    assert_bind_allowed("localhost")
    assert_bind_allowed("::1")
    with pytest.raises(SystemExit):
        assert_bind_allowed("0.0.0.0")
    assert_bind_allowed("0.0.0.0", token="secret")


def test_bearer_required_when_token_set(env, monkeypatch, client):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "s3cret")
    from mailroom_reloaded import settings as settings_mod

    settings_mod.get_settings.cache_clear()

    assert client.get("/v1/documents").status_code == 401
    assert client.get("/v1/documents", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/v1/documents", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert ok.json() == {"documents": [], "count": 0}

    # /health stays public.
    assert client.get("/health").status_code == 200
