# ruff: noqa: B008
"""``mailroom sandbox serve``: the offline ingress simulation server."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from mailroom_reloaded.sandbox.content.cli import sandbox_app


@sandbox_app.command("serve")
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. Non-loopback requires MAILROOM_API_TOKEN.",
    ),
    port: int = typer.Option(8100, "--port", help="Bind port."),
    content: str = typer.Option(
        "smoke", "--content", help="smoke | locked | <content dir>."
    ),
    data_dir: Path = typer.Option(
        Path(".sandbox-state"), "--data-dir", help="Isolated state dir (never ./data)."
    ),
    egress: str = typer.Option(
        "closed", "--egress", help="Simulated recipient profile: closed | egress."
    ),
    autonomy: str = typer.Option(
        "human",
        "--autonomy",
        help="human (drafts wait for approval) | sandbox (auto-approve into the sink).",
    ),
    expected: bool = typer.Option(
        True,
        "--expected/--no-expected",
        help="Show scenario expected outcomes next to actual.",
    ),
) -> None:
    """Serve the offline ingress sandbox (Correspondent stand-in + real pipeline on a mock LLM)."""
    import uvicorn

    from mailroom_reloaded.api.app import assert_bind_allowed
    from mailroom_reloaded.sandbox.server.app import create_sandbox_app
    from mailroom_reloaded.sandbox.server.content import (
        ContentSpecError,
        load_sandbox_content,
        resolve_content_spec,
    )
    from mailroom_reloaded.sandbox.server.guard import NetworkGuard
    from mailroom_reloaded.sandbox.server.service import SandboxService
    from mailroom_reloaded.sandbox.server.telemetry import silence_exporters

    # Same policy as `mailroom serve`: off-loopback needs a token. A container that
    # binds 0.0.0.0 but publishes its port on loopback only may opt out explicitly.
    if (os.environ.get("MAILROOM_ALLOW_UNAUTHENTICATED_BIND") or "").strip() not in {
        "1",
        "true",
        "yes",
    }:
        assert_bind_allowed(host)
    if egress not in {"closed", "egress"} or autonomy not in {"human", "sandbox"}:
        typer.echo(
            "--egress must be closed|egress and --autonomy human|sandbox", err=True
        )
        raise typer.Exit(code=2)
    try:
        root = resolve_content_spec(content)
        loaded = load_sandbox_content(root)
    except (ContentSpecError, FileNotFoundError, ValueError, OSError) as exc:
        typer.echo(f"mailroom sandbox serve: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not loaded.cs.report.ok:
        typer.echo(
            "content has validation errors:\n"
            + "\n".join(loaded.cs.report.errors[:10]),
            err=True,
        )
        raise typer.Exit(code=1)
    silence_exporters()
    guard = NetworkGuard().install()
    service = SandboxService(
        loaded,
        data_dir.resolve(),
        egress_profile=egress,
        autonomy=autonomy,
        show_expected=expected,
        guard=guard,
    )
    typer.echo(
        f"mailroom sandbox: content={loaded.kind} {root} data={data_dir.resolve()} (offline: network guard on)"
    )
    typer.echo(
        f"mailroom sandbox: UI http://{'127.0.0.1' if host in {'0.0.0.0', '::'} else host}:{port}/ui"
    )
    uvicorn.run(create_sandbox_app(service), host=host, port=port, log_level="info")
