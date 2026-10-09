"""Task 1 tests: the ``/tui`` browser terminal is served with vendored tokens."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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


def test_tui_page_served(client):
    """Verify /tui and /tui/ serve the shell HTML."""
    for path in ("/tui", "/tui/"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert 'id="output"' in resp.text
        assert "assets/main.js" in resp.text
        assert "assets/tokens.css" in resp.text
        assert "assets/tui.css" in resp.text


def test_tui_assets_served(client):
    """Verify vendored brand tokens are served as static assets."""
    resp = client.get("/tui/assets/tokens.css")
    assert resp.status_code == 200
    assert "--term-amber: #ffb86c" in resp.text
    assert "--term-bg-deep: #050709" in resp.text
    assert "--term-magenta: #f472b6" in resp.text
    assert "--term-yellow: #facc15" in resp.text
    assert "--term-station-judge: var(--term-magenta)" in resp.text
    assert "--term-station-review: var(--term-yellow)" in resp.text
    assert client.get("/tui/assets/tui.css").status_code == 200


def test_tui_asset_traversal_rejected(client):
    """Verify path traversal out of the tui directory is refused."""
    assert client.get("/tui/assets/..%2f..%2fsettings.py").status_code == 404
    assert client.get("/tui/assets/../../settings.py").status_code == 404


def test_tui_public_with_token_set(env, monkeypatch, client):
    """Verify /tui is public like /ui while /v1 stays token-gated."""
    monkeypatch.setenv("MAILROOM_API_TOKEN", "s3cret")
    from mailroom_reloaded import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    assert client.get("/tui").status_code == 200
    assert client.get("/tui/assets/tokens.css").status_code == 200
    assert client.get("/v1/documents").status_code == 401


def test_ui_unchanged(client):
    """Verify /ui still serves."""
    assert client.get("/ui").status_code == 200


def test_ui_header_links_to_tui(client):
    """Verify the /ui header carries the tui link."""
    assert 'href="/tui"' in client.get("/ui").text
