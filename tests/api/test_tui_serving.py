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


def test_ui_page_imports_its_module(client):
    """Verify /ui loads its pure helpers as an ES module (the tested seam)."""
    assert "/ui/assets/app.js" in client.get("/ui").text


def test_ui_page_shares_the_terminal_tokens(client):
    """Verify /ui links the vendored terminal tokens and its own layout sheet."""
    text = client.get("/ui").text
    assert 'href="/tui/assets/tokens.css"' in text
    assert 'href="/ui/assets/ui.css"' in text


def test_ui_page_keeps_the_ids_its_script_needs(client):
    """Verify every element the page's inline script looks up still exists, and no markup path."""
    text = client.get("/ui").text
    for el_id in (
        "token", "refresh", "statusFilter", "documents", "review", "auditDocId",
        "auditGo", "auditResult", "runs", "cards", "detail", "phoenixLink", "grafanaLink",
    ):
        assert f'id="{el_id}"' in text, el_id
    assert "innerHTML" not in text


def test_ui_stylesheet_served(client):
    """Verify the /ui layout sheet is served and only uses --term-* tokens."""
    resp = client.get("/ui/assets/ui.css")
    assert resp.status_code == 200
    assert "css" in resp.headers["content-type"]
    assert "--term-" in resp.text
    # the terminal edition never mixes in the console/obs token sets
    assert "--obs-" not in resp.text


def test_ui_page_is_accessible_by_construction(client):
    """Pin the landmark, table names, live regions and focus ring the headless audit
    checks, so a regression is caught without a browser."""
    text = client.get("/ui").text
    assert '<h1 class="sr-only">' in text
    # detail/cards/audit result update asynchronously: each must announce itself
    assert text.count('aria-live="polite"') == 3
    # every header cell is scoped: documents 4 + review 3 + runs 5
    assert text.count('scope="col"') == 12
    for label in ("Documents", "Review queue", "Eval runs"):
        assert f'aria-label="{label}"' in text
    assert "outline: 2px solid var(--term-amber)" in client.get("/ui/assets/ui.css").text


def test_ui_page_shares_the_tui_theme(client):
    """Verify /ui reads the theme /tui persists instead of hardcoding dark."""
    text = client.get("/ui").text
    assert "mailroom.tui.theme" in text
    assert "data-theme" in text


def test_tui_page_is_accessible_by_construction(client):
    """Pin the landmark, heading, focusable scrollback and prompt landmark the headless
    audit checks, so a regression is caught without a browser."""
    text = client.get("/tui").text
    # the h1 lives in the banner landmark, not in #output (which the trimmer and `clear` empty)
    assert '<header class="status-bar"><h1 class="sr-only">' in text
    # the scrollback scrolls, so keyboard users must be able to focus and scroll it
    assert 'id="output"' in text and 'tabindex="0"' in text
    # the command line is a landmark, so its prompt and input are not loose page content
    assert 'role="form"' in text and 'aria-label="terminal command"' in text

    css = client.get("/tui/assets/tui.css").text
    assert ".sr-only" in css
    # the dark theme derives an AA dim locally; the vendored token is only the light/hc value
    assert "--tui-dim: var(--term-fg-dim)" in css
    assert ':root[data-theme="dark"]' in css and "color-mix" in css
    assert ".dim { color: var(--tui-dim); }" in css
    # a focusable scrollback needs a visible indicator
    assert ".terminal:focus-visible" in css
    # and the vendored kit bytes stay upstream, still carrying its own flagged value
    assert "--term-fg-dim: #6b7585" in client.get("/tui/assets/tokens.css").text


def test_ui_module_served(client):
    """Verify the /ui ES module is served for the browser to import."""
    resp = client.get("/ui/assets/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "export function mergeLinks" in resp.text
    # the per-run link expressions the page depends on live here now
    assert "/tui#replay=run:" in resp.text
    assert "/d/mailroom-quality?var-run_id=" in resp.text
