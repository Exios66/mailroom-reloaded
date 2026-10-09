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
