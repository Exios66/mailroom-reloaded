"""Task 19 tests: the FastAPI ``/v1`` API, the ``/ui`` page and the bind guard.

The pipeline uses the ``mock`` provider through ``FakeOpenAI`` for the two
standalone LLM calls (sorter, specialist), and BERT is monkeypatched for the
deterministic scenarios, following ``tests/pipeline/test_flow.py``.
"""

from __future__ import annotations

import base64
import json
import time

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
    """Yield a local fake OpenAI server and stop it after the test."""
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):
    """Point the mock provider at the local fake OpenAI server."""
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate the base directory and reset settings and SQLite state per test."""
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
    """Yield an API test client with the application lifespan active."""
    from mailroom_reloaded.api.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    """Disable retry delays and clear tool-support caches around each test."""
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
    """Stub BERT classification and handoff for a deterministic routing scenario."""
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
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None, *, filename=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)
    return verdict, handoff


def _patch_bert_unavailable(monkeypatch):
    """Force full LLM sorting by simulating disabled BERT inference."""
    verdict = BertVerdict(available=False, reason="flag_off")
    handoff = Handoff(SortMode.FULL, None, "", "bert_unavailable:flag_off")
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None, *, filename=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)


def _reply(provider, payload):
    """Queue a discarded tool-round draft then the final structured reply."""
    provider.reply("thinking").reply(json.dumps(payload))


def _upload(client, name="letter.txt", content=LETTER):
    """Upload a text file, require acceptance and return its document ID."""
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
    """Verify an accepted upload becomes queryable after watcher archival."""
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
    """Verify an archived document exposes a valid audit chain through the API."""
    doc_id, _bins = _fast_path_doc(env, mock_provider, monkeypatch, client)

    resp = client.get(f"/v1/audit/{doc_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chain"]["ok"] is True
    assert body["chain"]["broken_at"] is None
    assert body["entries"]
    assert all(entry["doc_id"] == doc_id for entry in body["entries"])


def test_review_resolve_endpoint(env, mock_provider, monkeypatch, client):
    """Verify rejection moves a parked document to failed and unknown IDs return 404."""
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
    """Verify the UI serves HTML containing the observability links."""
    resp = client.get("/ui")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    text = resp.text
    assert "mailroom" in text.lower()
    assert "6006" in text  # Phoenix link
    assert "3000" in text  # Grafana link
    assert '"/tui#replay=run:" + encodeURIComponent(r.run_id)' in text  # per-run replay link
    assert "ev.stopPropagation()" in text  # the link must not also open the cards
    assert "/d/mailroom-quality?var-run_id=" in text  # per-run Grafana link expression
    assert "phoenix ↗" in text  # per-run Phoenix link
    assert "/links" in text  # the header links are refreshed from the public config


def test_links_public(client, monkeypatch):
    """Verify /links returns the observability base URLs from the settings."""
    monkeypatch.setenv("MAILROOM_PUBLIC_URL", "https://mailroom.example")
    monkeypatch.setenv("MAILROOM_PHOENIX_URL", "https://phoenix.example")
    monkeypatch.setenv("MAILROOM_GRAFANA_URL", "https://grafana.example")
    monkeypatch.setenv("MAILROOM_PHOENIX_PROJECT", "proj-x")
    from mailroom_reloaded.settings import get_settings

    get_settings.cache_clear()
    resp = client.get("/links")
    assert resp.status_code == 200
    assert resp.json() == {
        "public_url": "https://mailroom.example",
        "phoenix_url": "https://phoenix.example",
        "grafana_url": "https://grafana.example",
        "phoenix_project": "proj-x",
    }


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)//",
        "data:text/html,x",
        "ftp://host",
        "https://svc:hunter2@phoenix.internal:6006",
        "https://phoenix.example/?token=abc",
        "https://phoenix.example/#frag",
        "not a url",
    ],
)
def test_link_settings_reject_unsafe_values(monkeypatch, bad):
    """Verify the public link settings accept only credential-free http(s) URLs."""
    from pydantic import ValidationError

    from mailroom_reloaded.settings import Settings

    monkeypatch.setenv("MAILROOM_GRAFANA_URL", bad)
    with pytest.raises(ValidationError):
        Settings()


def test_link_settings_strip_trailing_slash(monkeypatch):
    """Verify a trailing slash is dropped so joined paths never double the slash."""
    from mailroom_reloaded.settings import Settings

    monkeypatch.setenv("MAILROOM_GRAFANA_URL", "https://g.example/")
    assert Settings().grafana_url == "https://g.example"


def test_links_public_without_token(client, monkeypatch):
    """Verify /links stays public while every /v1 route requires the token."""
    monkeypatch.setenv("MAILROOM_API_TOKEN", "s3cret")
    from mailroom_reloaded.settings import get_settings

    get_settings.cache_clear()
    assert client.get("/v1/documents").status_code == 401
    assert client.get("/links").status_code == 200


def test_runs_shape(client):
    """Verify absent evaluation runs and cards produce empty response lists."""
    resp = client.get("/v1/runs")
    assert resp.status_code == 200
    assert resp.json() == {"runs": []}

    cards = client.get("/v1/runs/000000000000/cards")
    assert cards.status_code == 200
    assert cards.json() == {"run_id": "000000000000", "cards": []}


def test_offbind_without_token_refuses(env, monkeypatch):
    """Verify public binding requires a token while loopback binding does not."""
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
    """Verify protected routes require the token while health stays public."""
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


@pytest.mark.parametrize("run_id", ["%2e%2e", "%5c..", "not-a-run", "a" * 11, "a" * 13, "A" * 12])
def test_cards_reject_invalid_run_ids(client, run_id):
    assert client.get(f"/v1/runs/{run_id}/cards").status_code == 400


def test_cards_reads_valid_run(env, client):
    cards_dir = env / "runs" / "012345abcdef" / "cards"
    cards_dir.mkdir(parents=True)
    (cards_dir / "card.json").write_text('{"score": 1}')
    assert client.get("/v1/runs/012345abcdef/cards").json() == {
        "run_id": "012345abcdef", "cards": [{"score": 1}]
    }


@pytest.mark.parametrize("authorization", [b"Basic s3cret", b"Bearer ", b"Bearer \xff", b"bearer s3cret"])
def test_invalid_authorization_is_unauthorized(env, client, monkeypatch, authorization):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "s3cret")
    from mailroom_reloaded.settings import get_settings

    get_settings.cache_clear()
    assert client.get("/v1/documents", headers={b"Authorization": authorization}).status_code == 401


@pytest.mark.parametrize("failure", [None, "lock", "unexpected"])
def test_embedded_watcher_logs_errors_and_joins(env, monkeypatch, failure):
    import importlib
    import threading
    from unittest.mock import Mock

    from mailroom_reloaded.watcher import WatcherLockHeld

    app_mod = importlib.import_module("mailroom_reloaded.api.app")
    started = threading.Event()
    stopped = threading.Event()
    finished = threading.Event()
    error = WatcherLockHeld("occupied") if failure == "lock" else RuntimeError("failed")

    class FakeWatcher:
        def __init__(self, *args):
            self.ready = threading.Event()

        def run_forever(self):
            started.set()
            try:
                if failure != "lock":
                    self.ready.set()
                if failure:
                    raise error
                assert stopped.wait(5)
            finally:
                finished.set()

        def stop(self):
            stopped.set()

    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "1")
    monkeypatch.setattr("mailroom_reloaded.watcher.Watcher", FakeWatcher)
    log = Mock()
    monkeypatch.setattr(app_mod, "logger", log)
    application = app_mod.create_app()
    with TestClient(application):
        assert started.wait(5)
    assert stopped.is_set()
    assert finished.is_set()
    assert not application.state.watcher_thread.is_alive()
    events = [c.args[0] for c in log.info.call_args_list]
    if failure == "lock":
        log.exception.assert_not_called()
        log.warning.assert_called_once()
        assert log.warning.call_args.args[0] == "embedded_watcher_not_started"
        assert "embedded_watcher_started" not in events
    elif failure:
        log.exception.assert_called_once_with("embedded_watcher_failed")
    else:
        log.exception.assert_not_called()
        assert "embedded_watcher_started" in events


# ---------------------------------------------------------------- review fixes

PUSH_ENVELOPE = {
    "message": {
        "data": base64.urlsafe_b64encode(
            json.dumps({"emailAddress": "a@example.com", "historyId": 1}).encode()
        ).decode()
    }
}


def test_bind_policy_enforced_at_startup_without_cli(env, monkeypatch):
    """Direct uvicorn start must apply the same bind guard as ``mailroom serve``."""
    import importlib

    app_mod = importlib.import_module("mailroom_reloaded.api.app")
    from mailroom_reloaded import settings

    monkeypatch.setenv("MAILROOM_API_HOST", "0.0.0.0")
    monkeypatch.delenv("MAILROOM_API_TOKEN", raising=False)
    settings.get_settings.cache_clear()
    with pytest.raises(SystemExit):
        app_mod._enforce_bind_policy()
    monkeypatch.setenv("MAILROOM_ALLOW_UNAUTHENTICATED_BIND", "1")
    app_mod._enforce_bind_policy()
    monkeypatch.delenv("MAILROOM_ALLOW_UNAUTHENTICATED_BIND")
    monkeypatch.setenv("MAILROOM_API_TOKEN", "secret")
    settings.get_settings.cache_clear()
    app_mod._enforce_bind_policy()
    calls = []
    monkeypatch.setattr(app_mod, "_enforce_bind_policy", lambda: calls.append(1))
    with TestClient(app_mod.app):
        pass
    assert calls == [1]


def _push_client(env, monkeypatch, **envvars):
    import importlib

    from mailroom_reloaded import settings

    app_mod = importlib.import_module("mailroom_reloaded.api.app")

    for k, v in envvars.items():
        monkeypatch.setenv(k, v)
    settings.get_settings.cache_clear()
    monkeypatch.setattr(app_mod, "_gmail_poll_task", lambda: None)
    return app_mod, TestClient(app_mod.app)


def test_push_route_accepts_google_oidc_for_configured_service_account(env, monkeypatch):
    app_mod, c = _push_client(
        env, monkeypatch,
        MAILROOM_API_TOKEN="secret",
        MAILROOM_GMAIL_PUSH_AUDIENCE="https://host/v1/intake/gmail",
        MAILROOM_GMAIL_PUSH_SERVICE_ACCOUNT="push@proj.iam.gserviceaccount.com",
    )
    good = {"email": "push@proj.iam.gserviceaccount.com", "email_verified": True}
    seen = {}

    def fake(token, audience):
        seen["audience"] = audience
        if token == "jwt-good":
            return good
        if token == "jwt-other":
            return {**good, "email": "evil@x.com"}
        if token == "jwt-unverified":
            return {**good, "email_verified": False}
        raise ValueError("bad signature")

    monkeypatch.setattr(app_mod, "_verify_google_oidc", fake)
    url = "/v1/intake/gmail"
    hdr = lambda t: {"Authorization": f"Bearer {t}"}
    assert c.post(url, json=PUSH_ENVELOPE, headers=hdr("jwt-good")).status_code == 204
    assert seen["audience"] == "https://host/v1/intake/gmail"
    assert c.post(url, json=PUSH_ENVELOPE, headers=hdr("secret")).status_code == 204
    for bad in ("jwt-other", "jwt-unverified", "jwt-forged"):
        assert c.post(url, json=PUSH_ENVELOPE, headers=hdr(bad)).status_code == 401
    assert c.post(url, json=PUSH_ENVELOPE).status_code == 401
    # other /v1 routes still reject a Google JWT
    assert c.get("/v1/documents", headers=hdr("jwt-good")).status_code == 401


def test_push_route_unconfigured_oidc_rejects_non_static_token(env, monkeypatch):
    app_mod, c = _push_client(env, monkeypatch, MAILROOM_API_TOKEN="secret")
    monkeypatch.setattr(app_mod, "_verify_google_oidc", lambda *_: pytest.fail("no OIDC"))
    h = {"Authorization": "Bearer jwt"}
    assert c.post("/v1/intake/gmail", json=PUSH_ENVELOPE, headers=h).status_code == 401


def test_jev_status_off_by_default(env, monkeypatch, client):
    # Force "off" explicitly rather than deleting the keys: a developer's local
    # ``.env`` may set MAILROOM_JEV_PROVIDER, and environment variables take
    # precedence over ``.env`` in pydantic-settings, so a delete would be
    # refilled from the file. Setting the knob to ``off`` is the default state
    # this test asserts and is robust in any checkout.
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "off")
    monkeypatch.delenv("JEV_PROVIDER", raising=False)
    body = client.get("/v1/jev").json()
    assert body["enabled"] is False
    assert body["provider"] == "off"
    assert body["calibrated"] is False and body["calibration"] is None
    assert body["gate"] == "band"


def test_jev_status_with_calibration_and_no_key_leak(env, monkeypatch, client):
    import json

    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "local")
    monkeypatch.setenv("MAILROOM_JEV_API_KEY", "sk-super-secret")
    models = env / "models"
    models.mkdir()
    (models / "jev_calibration.json").write_text(
        json.dumps(
            {
                "temperature": 1.1,
                "accept_threshold": 0.9,
                "verify_threshold": 0.6,
                "ece_before": 0.2,
                "ece_after": 0.05,
                "n": 60,
            }
        )
    )
    resp = client.get("/v1/jev")
    body = resp.json()
    assert body["enabled"] is True and body["provider"] == "local"
    assert body["calibrated"] is True and body["gate"] == "jev"
    assert body["calibration"]["accept_threshold"] == 0.9
    assert body["calibration"]["n"] == 60
    assert "sk-super-secret" not in resp.text
    assert "api_key" not in resp.text


def test_jev_status_requires_token(env, monkeypatch):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "t0k")
    from fastapi.testclient import TestClient

    from mailroom_reloaded.api.app import app

    with TestClient(app) as c:
        assert c.get("/v1/jev").status_code == 401
        assert c.get("/v1/jev", headers={"Authorization": "Bearer t0k"}).status_code == 200


def test_startup_runs_retention_and_survives_failure(env, monkeypatch):
    """`maintain` runs at startup (when the span store is enabled); a failure never blocks it."""
    from mailroom_reloaded.api.app import app
    from mailroom_reloaded.obs import tracing
    from mailroom_reloaded.storage import retention

    calls: list[int] = []

    def boom() -> None:
        calls.append(1)
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(tracing, "span_store_enabled", lambda: True)
    monkeypatch.setattr(retention, "maintain", boom)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        for _ in range(200):
            if calls:
                break
            time.sleep(0.01)
    assert calls == [1]


def test_startup_skips_retention_under_pytest(env, monkeypatch):
    from mailroom_reloaded.api.app import app
    from mailroom_reloaded.storage import retention

    monkeypatch.setattr(retention, "maintain", lambda: pytest.fail("must not run"))
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def _watcher_claims_on_publish(monkeypatch, base):
    """Simulate a watcher that claims each visible inbox name the moment it appears.

    Returns the list of bytes the watcher read from every file it claimed.
    """
    import os
    from pathlib import Path

    bins = Bins(base)
    inbox = bins.inbox.resolve()
    seen: list[bytes] = []

    def on_publish(dest):
        dest = Path(dest)
        if dest.parent.resolve() == inbox and not dest.name.startswith("."):
            seen.append(bins.claim(dest, "watcher").read_bytes())

    real_open, real_link = os.open, os.link

    def open_hook(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            on_publish(path)
        return fd

    def link_hook(src, dst, *args, **kwargs):
        real_link(src, dst, *args, **kwargs)
        on_publish(dst)

    monkeypatch.setattr(os, "open", open_hook)
    monkeypatch.setattr(os, "link", link_hook)
    return seen


def test_upload_is_claimable_only_after_complete_write(client, env, monkeypatch):
    import hashlib

    seen = _watcher_claims_on_publish(monkeypatch, env)
    response = client.post("/v1/documents", files={"file": ("letter.txt", LETTER)})
    assert response.status_code == 202
    assert seen == [LETTER]
    assert response.json()["doc_id"] == hashlib.sha256(LETTER).hexdigest()[:16]
