"""``/v1/ledger/*``: the token-gated read and write API over the archive ledger."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from mailroom_reloaded.storage.ledger import get_ledger, reset_ledger
from mailroom_reloaded.storage.retention import SHOWCASE_RUN_IDS

TOKEN = "s3cret-token"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate base dir, settings, the default engine and the ledger per test."""
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("MAILROOM_API_TOKEN", raising=False)
    monkeypatch.delenv("MAILROOM_TRACE_KEEP", raising=False)
    from mailroom_reloaded import settings
    from mailroom_reloaded.storage import db

    settings.get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    try:
        yield tmp_path
    finally:
        reset_ledger()
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()


@pytest.fixture
def client(env):
    from mailroom_reloaded.api.app import app

    with TestClient(app) as c:
        yield c


def _seed() -> None:
    """A small chain: two runs, a pin, a doc and a closed run."""
    ledger = get_ledger()
    ledger.append("run_opened", "run-a", payload={"kind": "eval"})
    ledger.append("doc_closed", "run-a", doc_id="d1", payload={"outcome": "completed"})
    ledger.append("run_closed", "run-a", payload={"closed_by": "completed"})
    ledger.append("run_opened", "run-b", payload={"kind": "live"})
    ledger.append("pinned", "run-b", payload={"target": "run-b"})
    assert ledger.flush()


def test_auth_required_when_token_set(client, monkeypatch):
    from mailroom_reloaded import settings

    monkeypatch.setenv("MAILROOM_API_TOKEN", TOKEN)
    settings.get_settings.cache_clear()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    for path in [
        "/v1/ledger",
        "/v1/ledger/head",
        "/v1/ledger/verify",
        "/v1/ledger/keep",
    ]:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=auth).status_code == 200
    for path, body in [
        ("/v1/ledger/pin", {"run_id": "run-a"}),
        ("/v1/ledger/unpin", {"run_id": "run-a"}),
        ("/v1/ledger/policy", {"value": "all"}),
    ]:
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers=auth).status_code == 200


def test_entries_shape_and_default_order(client):
    _seed()
    body = client.get("/v1/ledger").json()
    seqs = [e["seq"] for e in body["entries"]]
    assert seqs == [5, 4, 3, 2, 1]  # newest first
    first = body["entries"][0]
    assert set(first) == {
        "seq",
        "kind",
        "run_id",
        "doc_id",
        "ts",
        "payload",
        "digest",
        "prev_hash",
        "entry_hash",
    }
    assert body["head"] == {"seq": 5, "entry_hash": first["entry_hash"]}
    asc = client.get("/v1/ledger?descending=false").json()
    assert [e["seq"] for e in asc["entries"]] == [1, 2, 3, 4, 5]


def test_entries_filters(client):
    _seed()
    by_run = client.get("/v1/ledger?run_id=run-a").json()["entries"]
    assert {e["run_id"] for e in by_run} == {"run-a"} and len(by_run) == 3
    by_kind = client.get("/v1/ledger?kind=run_opened").json()["entries"]
    assert [e["run_id"] for e in by_kind] == ["run-b", "run-a"]
    since = client.get("/v1/ledger?since_seq=3&descending=false").json()["entries"]
    assert [e["seq"] for e in since] == [4, 5]
    limited = client.get("/v1/ledger?limit=2").json()["entries"]
    assert [e["seq"] for e in limited] == [5, 4]


@pytest.mark.parametrize("limit,status", [(0, 422), (501, 422), (1, 200), (500, 200)])
def test_entries_limit_bounds(client, limit, status):
    assert client.get(f"/v1/ledger?limit={limit}").status_code == status


def test_entries_since_seq_must_be_non_negative(client):
    assert client.get("/v1/ledger?since_seq=-1").status_code == 422


def test_entries_invalid_filters_are_400(client):
    r = client.get("/v1/ledger?run_id=bad%20id")
    assert r.status_code == 400 and r.json() == {"detail": "Invalid run ID"}
    r = client.get("/v1/ledger?kind=nope")
    assert r.status_code == 400 and r.json() == {"detail": "Invalid kind"}
    assert client.get("/v1/ledger/verify?run_id=bad%20id").status_code == 400


def test_head_empty_and_non_empty(client):
    assert client.get("/v1/ledger/head").json() == {"head": None, "count": 0}
    assert client.get("/v1/ledger").json() == {"entries": [], "head": None}
    _seed()
    body = client.get("/v1/ledger/head").json()
    assert body["count"] == 5
    head = body["head"]
    assert set(head) == {"seq", "entry_hash", "ts", "kind"}
    assert head["seq"] == 5 and head["kind"] == "pinned"
    newest = client.get("/v1/ledger").json()["entries"][0]
    assert head["entry_hash"] == newest["entry_hash"] and head["ts"] == newest["ts"]


def test_verify_ok_and_per_run(client):
    _seed()
    whole = client.get("/v1/ledger/verify").json()
    assert whole["ok"] is True and whole["count"] == 5 and whole["head_seq"] == 5
    run = client.get("/v1/ledger/verify?run_id=run-a").json()
    assert run["ok"] is True and run["count"] == 3 and run["merkle_ok"] is True
    missing = client.get("/v1/ledger/verify?run_id=nobody").json()
    assert missing["ok"] is False and missing["detail"] == "unknown run"


def test_verify_detects_tamper(client):
    from mailroom_reloaded.storage.db import get_engine

    _seed()
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE ledger SET payload = :p WHERE seq = 2"),
            {"p": '{"outcome": "failed"}'},
        )
    whole = client.get("/v1/ledger/verify").json()
    assert whole["ok"] is False and whole["broken_at"] == 2
    run = client.get("/v1/ledger/verify?run_id=run-a").json()
    assert run["ok"] is False and run["broken_at"] == 2


def test_keep_env_default_then_ledger_override(client, monkeypatch):
    from mailroom_reloaded import settings

    monkeypatch.setenv("MAILROOM_TRACE_KEEP", "recent:7")
    settings.get_settings.cache_clear()
    body = client.get("/v1/ledger/keep").json()
    assert body == {
        "policy": "recent:7",
        "source": "env",
        "pinned": [],
        "showcase": list(SHOWCASE_RUN_IDS),
    }
    assert client.post("/v1/ledger/policy", json={"value": "all"}).json() == {
        "ok": True
    }
    body = client.get("/v1/ledger/keep").json()
    assert body["policy"] == "all" and body["source"] == "ledger"
    assert (
        client.post("/v1/ledger/policy", json={"value": "recent:3"}).status_code == 200
    )
    assert client.get("/v1/ledger/keep").json()["policy"] == "recent:3"
    assert client.post("/v1/ledger/policy", json={"value": "pinned"}).status_code == 200
    assert client.get("/v1/ledger/keep").json()["policy"] == "pinned"


def test_pin_unpin_round_trip(client):
    assert client.post("/v1/ledger/pin", json={"run_id": "zeta"}).json() == {"ok": True}
    assert client.post("/v1/ledger/pin", json={"run_id": "alpha"}).status_code == 200
    assert client.get("/v1/ledger/keep").json()["pinned"] == ["alpha", "zeta"]
    assert client.post("/v1/ledger/unpin", json={"run_id": "zeta"}).json() == {
        "ok": True
    }
    assert client.get("/v1/ledger/keep").json()["pinned"] == ["alpha"]
    kinds = [
        e["kind"] for e in client.get("/v1/ledger?descending=false").json()["entries"]
    ]
    assert kinds == ["pinned", "pinned", "unpinned"]


def test_unpin_showcase_is_400(client):
    r = client.post("/v1/ledger/unpin", json={"run_id": SHOWCASE_RUN_IDS[0]})
    assert r.status_code == 400
    assert r.json() == {"detail": "Showcase runs cannot be unpinned"}
    assert client.get("/v1/ledger/head").json()["count"] == 0


def test_invalid_writes_are_400(client):
    for path in ("/v1/ledger/pin", "/v1/ledger/unpin"):
        r = client.post(path, json={"run_id": "bad id"})
        assert r.status_code == 400 and r.json() == {"detail": "Invalid run ID"}
    r = client.post("/v1/ledger/policy", json={"value": "forever"})
    assert r.status_code == 400 and r.json() == {"detail": "Invalid policy"}
    assert client.get("/v1/ledger/head").json()["count"] == 0


def test_write_bodies_are_strict(client):
    assert (
        client.post("/v1/ledger/pin", json={"run_id": "a", "x": 1}).status_code == 422
    )
    assert client.post("/v1/ledger/pin", json={"run_id": "a" * 121}).status_code == 422
    assert client.post("/v1/ledger/pin", json={}).status_code == 422
    assert (
        client.post("/v1/ledger/policy", json={"value": "all", "x": 1}).status_code
        == 422
    )


def test_reads_never_install_the_anchor_hook(client, monkeypatch):
    from mailroom_reloaded.storage import anchor

    calls: list[object] = []
    monkeypatch.setattr(anchor, "install", lambda ledger: calls.append(ledger))
    reset_ledger()
    for path in [
        "/v1/ledger",
        "/v1/ledger/head",
        "/v1/ledger/verify",
        "/v1/ledger/keep",
    ]:
        assert client.get(path).status_code == 200
    assert calls == []
    assert client.post("/v1/ledger/pin", json={"run_id": "run-a"}).status_code == 200
    assert len(calls) == 1  # writes do use the process-wide ledger


def test_pin_and_unpin_are_idempotent(client):
    for _ in range(3):
        assert (
            client.post("/v1/ledger/pin", json={"run_id": "eval-x"}).status_code == 200
        )
    assert (
        client.post("/v1/ledger/unpin", json={"run_id": "never-pinned"}).status_code
        == 200
    )
    kinds = [
        e["kind"] for e in client.get("/v1/ledger?descending=false").json()["entries"]
    ]
    assert kinds.count("pinned") == 1
    assert "unpinned" not in kinds


def test_since_seq_beyond_sqlite_int_is_422(client):
    assert client.get("/v1/ledger?since_seq=99999999999999999999").status_code == 422
