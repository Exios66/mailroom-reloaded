"""`mailroom sandbox serve` wiring: options, bind policy, content resolution."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from mailroom_reloaded import cli
from mailroom_reloaded.sandbox.server.content import (
    ContentSpecError,
    resolve_content_spec,
)

runner = CliRunner()


def test_serve_is_registered_with_documented_options():
    """Verify sandbox serve exposes the documented command-line options."""
    res = runner.invoke(cli.app, ["sandbox", "serve", "--help"])
    assert res.exit_code == 0
    for opt in (
        "--host",
        "--port",
        "--content",
        "--data-dir",
        "--egress",
        "--autonomy",
        "--no-expected",
    ):
        assert opt in res.output


def test_serve_defaults_are_loopback_8100_smoke():
    """Verify serve defaults to loopback port 8100 and the offline smoke set."""
    import inspect

    from mailroom_reloaded.sandbox.server.cli import serve

    params = inspect.signature(serve).parameters
    assert (
        params["host"].default.default == "127.0.0.1"
        and params["port"].default.default == 8100
    )
    assert params["content"].default.default == "smoke"


def test_off_loopback_bind_requires_token(monkeypatch):
    """Verify an unauthenticated non-loopback bind is refused by default."""
    monkeypatch.delenv("MAILROOM_ALLOW_UNAUTHENTICATED_BIND", raising=False)
    monkeypatch.delenv("MAILROOM_API_TOKEN", raising=False)
    res = runner.invoke(cli.app, ["sandbox", "serve", "--host", "0.0.0.0"])
    assert res.exit_code != 0 and isinstance(res.exception, SystemExit)
    assert "MAILROOM_API_TOKEN" in str(res.exception)


def test_banner_prints_ui_and_inbox_deep_link(monkeypatch, tmp_path):
    """Verify the startup banner adds the Inbox deep link next to the UI URL."""
    import uvicorn

    from mailroom_reloaded.sandbox.server.guard import NetworkGuard

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(NetworkGuard, "install", lambda self: self)
    res = runner.invoke(
        cli.app,
        ["sandbox", "serve", "--port", "8123", "--data-dir", str(tmp_path / "s")],
    )
    assert res.exit_code == 0, res.output
    assert "UI http://127.0.0.1:8123/ui\n" in res.output
    assert (
        "Inbox http://127.0.0.1:8123/ui#tab=messages&mailbox=open&role=correspondent"
        in res.output
    )


def test_bad_arguments_exit_nonzero(monkeypatch):
    """Verify missing content and invalid egress profiles return error exit codes."""
    monkeypatch.delenv("MAILROOM_API_TOKEN", raising=False)
    assert (
        runner.invoke(
            cli.app, ["sandbox", "serve", "--content", "/definitely/not/here"]
        ).exit_code
        == 1
    )
    assert (
        runner.invoke(cli.app, ["sandbox", "serve", "--egress", "bogus"]).exit_code == 2
    )


def test_content_spec_resolution(tmp_path):
    """Verify smoke and directory resolution and errors for unavailable content."""
    assert resolve_content_spec("smoke").name == "smoke"
    assert resolve_content_spec(str(tmp_path)) == tmp_path
    with pytest.raises(ContentSpecError):
        resolve_content_spec("locked", pull_dir=tmp_path / "missing")
    with pytest.raises(ContentSpecError):
        resolve_content_spec("nonsense-dir")


def test_conformance_is_registered_with_documented_options():
    """Verify sandbox conformance exposes the documented command-line options."""
    res = runner.invoke(cli.app, ["sandbox", "conformance", "--help"])
    assert res.exit_code == 0
    for opt in (
        "--content",
        "--data-dir",
        "--json",
        "--only",
        "--heldout",
        "--slim",
        "--lofo",
    ):
        assert opt in res.output


def test_conformance_rejects_invalid_content_before_side_effects(monkeypatch):
    """Verify conformance refuses an invalid pack before starting any service."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from mailroom_reloaded.sandbox.server import content, service, telemetry

    loaded = SimpleNamespace(
        cs=SimpleNamespace(report=SimpleNamespace(ok=False, errors=["invalid scenario"]))
    )
    monkeypatch.setattr(content, "load_sandbox_content", lambda _: loaded)
    silence = Mock()
    start = Mock()
    monkeypatch.setattr(telemetry, "silence_exporters", silence)
    monkeypatch.setattr(service, "SandboxService", start)
    res = runner.invoke(cli.app, ["sandbox", "conformance", "--content", "smoke"])
    assert res.exit_code == 1
    assert "content has validation errors:\ninvalid scenario" in res.output
    silence.assert_not_called()
    start.assert_not_called()


def test_conformance_uninstalls_guard_when_service_start_fails(monkeypatch):
    """Verify a failing service start still uninstalls the network guard."""
    from types import SimpleNamespace

    from mailroom_reloaded.sandbox.server import content, service, telemetry
    from mailroom_reloaded.sandbox.server import guard as guard_mod

    loaded = SimpleNamespace(
        cs=SimpleNamespace(report=SimpleNamespace(ok=True, errors=[]))
    )
    monkeypatch.setattr(content, "load_sandbox_content", lambda _: loaded)
    monkeypatch.setattr(telemetry, "silence_exporters", lambda: None)
    events = []

    class FakeGuard:
        def install(self):
            events.append("install")
            return self

        def uninstall(self):
            events.append("uninstall")

    class BoomService:
        def __init__(self, *a, **k):
            pass

        def start(self, **k):
            raise OSError("state dir not writable")

    monkeypatch.setattr(guard_mod, "NetworkGuard", FakeGuard)
    monkeypatch.setattr(service, "SandboxService", BoomService)
    res = runner.invoke(cli.app, ["sandbox", "conformance", "--content", "smoke"])
    assert res.exit_code != 0
    assert events == ["install", "uninstall"]
