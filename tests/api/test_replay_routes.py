"""``/v1/replay/*``: the token-gated read API over the span store and the ledger."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mailroom_reloaded.obs.replay.timeline import clear_cache
from mailroom_reloaded.storage.ledger import get_ledger, reset_ledger
from mailroom_reloaded.storage.retention import seed_showcase
from mailroom_reloaded.storage.span_store import SpanStore

RUN = "showcase-clean"
TOKEN = "s3cret-token"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate base dir, settings, the default engine and the ledger per test."""
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("MAILROOM_API_TOKEN", raising=False)
    from mailroom_reloaded import settings
    from mailroom_reloaded.storage import db

    settings.get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    clear_cache()
    try:
        yield tmp_path
    finally:
        reset_ledger()
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()
        clear_cache()


@pytest.fixture
def store(env):
    """The span store the app reads (``<base_dir>/traces.db``), showcase seeded."""
    s = SpanStore(env / "traces.db")
    assert RUN in seed_showcase(s)
    yield s
    s.close()


@pytest.fixture
def client(env, store):
    from mailroom_reloaded.api.app import app

    with TestClient(app) as c:
        yield c


def test_auth_required_when_token_set(client, monkeypatch):
    from mailroom_reloaded import settings

    monkeypatch.setenv("MAILROOM_API_TOKEN", TOKEN)
    settings.get_settings.cache_clear()
    paths = [
        "/v1/replay/sessions",
        f"/v1/replay/sessions/{RUN}/timeline",
        f"/v1/replay/sessions/{RUN}/export",
    ]
    for path in paths:
        assert client.get(path).status_code == 401
        ok = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200


def test_sessions_list_shape(client):
    body = client.get("/v1/replay/sessions").json()
    rows = {s["id"]: s for s in body["sessions"]}
    assert f"run:{RUN}" in rows
    row = rows[f"run:{RUN}"]
    assert row["kind"] == "run" and row["source"] == "spans"
    assert row["documents"] > 0 and row["data_pruned"] is False


@pytest.mark.parametrize("limit,status", [(0, 422), (501, 422), (1, 200), (500, 200)])
def test_sessions_limit_bounds(client, limit, status):
    assert client.get(f"/v1/replay/sessions?limit={limit}").status_code == status


def test_sessions_limit_applies(client):
    assert len(client.get("/v1/replay/sessions?limit=1").json()["sessions"]) == 1


def test_timeline_happy_path(client):
    r = client.get(f"/v1/replay/sessions/{RUN}/timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "replay/v1"
    assert body["session"]["id"] == f"run:{RUN}"
    assert body["session"]["data_pruned"] is False
    assert body["segments"] and body["entities"]


def test_timeline_bad_id_is_400(client):
    r = client.get("/v1/replay/sessions/bad%20id/timeline")
    assert r.status_code == 400
    assert r.json() == {"detail": "Invalid session ID"}


def test_timeline_missing_is_404(client):
    assert client.get("/v1/replay/sessions/no-such-run/timeline").status_code == 404


def test_pruned_run_is_410_and_marked_in_list(client, store):
    ledger = get_ledger()
    ledger.append("pruned", "eval-gone", payload={"target": "eval-gone"})
    assert ledger.flush()
    r = client.get("/v1/replay/sessions/eval-gone/timeline")
    assert r.status_code == 410 and r.json() == {"detail": "data pruned"}
    assert client.get("/v1/replay/sessions/eval-gone/export").status_code == 410
    # a pruned run that still has spans (or audit rows) is listed and flagged
    ledger.append("pruned", RUN, payload={"target": RUN})
    assert ledger.flush()
    rows = {s["id"]: s for s in client.get("/v1/replay/sessions").json()["sessions"]}
    assert rows[f"run:{RUN}"]["data_pruned"] is True
    tl = client.get(f"/v1/replay/sessions/{RUN}/timeline").json()
    assert tl["session"]["data_pruned"] is True


def test_export_headers(client):
    r = client.get(f"/v1/replay/sessions/{RUN}/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert (
        r.headers["content-disposition"] == f'attachment; filename="{RUN}.replay.json"'
    )
    assert r.json()["version"] == "replay/v1"


def test_export_filename_is_sanitised(client, store):
    run = "a:b@c+d"
    row = store.spans_for_run(RUN)[0]
    store.write(
        [
            {
                **row,
                "span_id": "zz1",
                "run_id": run,
                "attrs": "{}",
                "events": "[]",
            }
        ]
    )
    r = client.get(f"/v1/replay/sessions/{run}/export")
    assert r.status_code == 200
    assert 'filename="a_b_c_d.replay.json"' in r.headers["content-disposition"]


def test_window_params(client):
    full = client.get(f"/v1/replay/sessions/{RUN}/timeline").json()
    dur = full["session"]["duration_s"]
    win = client.get(
        f"/v1/replay/sessions/{RUN}/timeline?from_s=0&to_s={dur / 2}"
    ).json()
    assert win["session"]["window"]["to_s"] == pytest.approx(dur / 2)
    assert win["session"]["window"]["complete"] is False
    assert full["session"]["window"]["complete"] is True


@pytest.mark.parametrize("q", ["from_s=-1", "to_s=-0.5", "from_s=nan", "to_s=inf"])
def test_window_rejects_bad_values(client, q):
    r = client.get(f"/v1/replay/sessions/{RUN}/timeline?{q}")
    assert r.status_code == 422


def test_from_after_to_is_422(client):
    r = client.get(f"/v1/replay/sessions/{RUN}/timeline?from_s=5&to_s=1")
    assert r.status_code == 422


def test_reads_never_install_the_anchor_hook(client, monkeypatch):
    from mailroom_reloaded.storage import anchor

    calls: list[object] = []
    monkeypatch.setattr(anchor, "install", lambda ledger: calls.append(ledger))
    reset_ledger()
    assert client.get("/v1/replay/sessions").status_code == 200
    assert client.get(f"/v1/replay/sessions/{RUN}/timeline").status_code == 200
    assert calls == []
