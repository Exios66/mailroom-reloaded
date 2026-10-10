"""``GET /ready``: aggregate readiness, public summary vs token-gated detail (P6-A, A4)."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from mailroom_reloaded.api.routes import ready

COMPONENTS = [
    "db",
    "ledger",
    "span_store",
    "collector",
    "phoenix",
    "prometheus",
    "grafana",
    "llm_provider",
    "watcher",
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolate the base directory and the default SQLite engine per test."""
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("MAILROOM_API_TOKEN", raising=False)
    monkeypatch.delenv("MAILROOM_EMBED_WATCHER", raising=False)
    from mailroom_reloaded.storage import db

    monkeypatch.setattr(db, "_default_engine", None)
    try:
        yield tmp_path
    finally:
        if db._default_engine is not None:
            db._default_engine.dispose()


@pytest.fixture()
def client(env):
    from mailroom_reloaded.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def _setenv(monkeypatch, name, value):
    """Set an env var and drop the cached settings so the next request reads it."""
    from mailroom_reloaded.settings import get_settings

    monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def _probe(name, status="ok", detail=None, critical=False):
    return ready.Probe(name, lambda _req: (status, detail), critical=critical)


def test_default_stack_is_ok_with_every_component(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["status"] == "ok"
    assert [c["name"] for c in body["components"]] == COMPONENTS
    by_name = {c["name"]: c for c in body["components"]}
    assert by_name["db"]["status"] == "ok"
    assert by_name["ledger"]["status"] == "ok"
    assert by_name["ledger"]["detail"] == "empty"
    for name in ("collector", "phoenix", "prometheus", "grafana", "llm_provider"):
        assert by_name[name]["status"] == "unconfigured"
        assert "not wired yet" in by_name[name]["detail"]
    assert by_name["watcher"]["status"] == "unconfigured"
    for c in body["components"]:
        assert c["status"] in ready.STATUSES
        assert isinstance(c["latency_ms"], (int, float)) and c["latency_ms"] >= 0
    assert resp.headers["cache-control"] == "no-store"


def test_no_token_request_returns_public_summary_only(client, monkeypatch):
    _setenv(monkeypatch, "MAILROOM_API_TOKEN", "secret-token")
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "secret-token"}):
        resp = client.get("/ready", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {"schema_version": 1, "status": "ok"}


def test_token_request_returns_components(client, monkeypatch):
    _setenv(monkeypatch, "MAILROOM_API_TOKEN", "secret-token")
    resp = client.get("/ready", headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 200
    assert [c["name"] for c in resp.json()["components"]] == COMPONENTS


def test_probe_timeout_returns_degraded_not_500(client, monkeypatch):
    release = threading.Event()

    def hang(_req):
        release.wait(5)
        return "ok", None

    monkeypatch.setattr(ready, "PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        ready, "PROBES", [_probe("db", critical=True), ready.Probe("phoenix", hang)]
    )
    try:
        started = time.perf_counter()
        resp = client.get("/ready")
        elapsed = time.perf_counter() - started
    finally:
        release.set()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    phoenix = body["components"][1]
    assert phoenix == {
        "name": "phoenix",
        "status": "degraded",
        "latency_ms": phoenix["latency_ms"],
        "detail": "timed out after 0.05s",
    }
    assert elapsed < 2


def test_critical_timeout_is_degraded_not_down(client, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(ready, "PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        ready,
        "PROBES",
        [ready.Probe("db", lambda _r: (release.wait(5), ("ok", None))[1], critical=True)],
    )
    try:
        resp = client.get("/ready")
    finally:
        release.set()
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"


def test_503_when_down(client, monkeypatch):
    def boom(_req):
        raise RuntimeError("sqlite:////secret/path/mailroom.db is locked")

    monkeypatch.setattr(
        ready, "PROBES", [ready.Probe("db", boom, critical=True), _probe("ledger", critical=True)]
    )
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "down"
    db_row = body["components"][0]
    assert db_row["status"] == "down"
    assert db_row["detail"] == "probe failed: RuntimeError"
    assert "secret" not in resp.text  # the exception message never reaches the response


def test_public_503_still_hides_detail(client, monkeypatch):
    _setenv(monkeypatch, "MAILROOM_API_TOKEN", "secret-token")
    monkeypatch.setattr(ready, "PROBES", [_probe("db", "down", "x", critical=True)])
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {"schema_version": 1, "status": "down"}


def test_non_critical_down_is_degraded(client, monkeypatch):
    def boom(_req):
        raise OSError("connection refused")

    monkeypatch.setattr(
        ready, "PROBES", [_probe("db", critical=True), ready.Probe("grafana", boom)]
    )
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["components"][1]["status"] == "down"


def test_unknown_status_and_hostile_detail_are_normalised(client, monkeypatch):
    monkeypatch.setattr(
        ready,
        "PROBES",
        [
            _probe("weird", "great"),
            _probe("noisy", "ok", "line1\nline2\u202e" + "x" * 500),
        ],
    )
    rows = client.get("/ready").json()["components"]
    assert rows[0]["status"] == "degraded"
    assert rows[0]["detail"] == "probe returned an unknown status"
    assert "\n" not in rows[1]["detail"] and "\u202e" not in rows[1]["detail"]
    assert len(rows[1]["detail"]) == 200


def test_watcher_probe_reports_embedded_thread_state(client):
    class FakeThread:
        def __init__(self, alive):
            self.alive = alive

        def is_alive(self):
            return self.alive

    state = client.app.state
    state.watcher_thread = FakeThread(True)
    try:
        rows = {c["name"]: c for c in client.get("/ready").json()["components"]}
        assert rows["watcher"]["status"] == "ok"
        state.watcher_thread = FakeThread(False)
        resp = client.get("/ready")
        rows = {c["name"]: c for c in resp.json()["components"]}
        assert rows["watcher"]["status"] == "down"
        assert resp.json()["status"] == "degraded"  # not critical: still 200
        assert resp.status_code == 200
    finally:
        del state.watcher_thread


def test_span_store_probe_does_not_create_the_store(client, env, monkeypatch):
    path = env / "spans" / "traces.db"
    _setenv(monkeypatch, "MAILROOM_TRACE_STORE_PATH", str(path))
    rows = {c["name"]: c for c in client.get("/ready").json()["components"]}
    assert rows["span_store"] == {
        "name": "span_store",
        "status": "ok",
        "latency_ms": rows["span_store"]["latency_ms"],
        "detail": "no spans recorded yet",
    }
    assert not path.exists()


def test_span_store_probe_reads_an_existing_store(client, env, monkeypatch):
    from mailroom_reloaded.storage.span_store import SpanStore

    path = env / "traces.db"
    store = SpanStore(path)
    store.watermark()  # creates the file and table
    store.close()
    _setenv(monkeypatch, "MAILROOM_TRACE_STORE_PATH", str(path))
    rows = {c["name"]: c for c in client.get("/ready").json()["components"]}
    assert rows["span_store"]["status"] == "ok"
    assert "detail" not in rows["span_store"]


def test_ledger_probe_reports_the_head(client, env):
    from mailroom_reloaded.storage.db import get_engine
    from mailroom_reloaded.storage.ledger import Ledger

    ledger = Ledger(get_engine())
    ledger.append("pinned", "r1", payload={"target": "r1", "actor": "test"})
    assert ledger.flush()
    ledger.close()
    rows = {c["name"]: c for c in client.get("/ready").json()["components"]}
    assert rows["ledger"]["detail"].startswith("head seq ")


def test_ready_is_not_under_v1_and_health_is_unchanged(client, monkeypatch):
    _setenv(monkeypatch, "MAILROOM_API_TOKEN", "secret-token")
    assert client.get("/health").json() == {"status": "ok", "service": "mailroom"}
    assert client.get("/v1/ready").status_code in (401, 404)
    assert client.get("/ready").status_code == 200


def test_aggregate_rules():
    probes = [_probe("db", critical=True), _probe("phoenix")]

    def rows(db, phoenix):
        return [{"name": "db", "status": db}, {"name": "phoenix", "status": phoenix}]

    assert ready.aggregate(rows("ok", "ok"), probes) == "ok"
    assert ready.aggregate(rows("ok", "unconfigured"), probes) == "ok"
    assert ready.aggregate(rows("ok", "down"), probes) == "degraded"
    assert ready.aggregate(rows("degraded", "ok"), probes) == "degraded"
    assert ready.aggregate(rows("down", "ok"), probes) == "down"
