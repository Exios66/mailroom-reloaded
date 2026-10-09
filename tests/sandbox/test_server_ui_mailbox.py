"""Runs the Boss mailbox panel's JS under node with a stub DOM (offline)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_mailbox_panel_js_suite() -> None:
    """Run the mailbox panel JavaScript tests with Node when it is available."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    res = subprocess.run(
        [node, "--test", "tests/sandbox/js/mailbox.test.mjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_ui_is_offline_and_serves_the_panel(tmp_path):
    """Verify the UI serves the mailbox panel and uses no explicit HTTP asset URLs."""
    from fastapi.testclient import TestClient

    from mailroom_reloaded.sandbox.server.app import create_sandbox_app
    from mailroom_reloaded.sandbox.server.content import load_sandbox_content
    from mailroom_reloaded.sandbox.server.service import SandboxService

    svc = SandboxService(load_sandbox_content(), tmp_path / "s")
    with TestClient(create_sandbox_app(svc)) as c:
        html = c.get("/ui").text
        assert "mbx-dock" in html and "/ui/mailbox.js" in html
        js = c.get("/ui/mailbox.js")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
        for text in (html, js.text, c.get("/ui/app.js").text):
            assert 'src="http' not in text and 'fetch("http' not in text
