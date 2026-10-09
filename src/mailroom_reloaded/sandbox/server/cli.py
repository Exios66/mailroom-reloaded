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
    correspondent: str = typer.Option(
        "standin",
        "--correspondent",
        help="standin (rules, default) | llm (optional, loopback endpoint, falls back to rules).",
    ),
    llm_base_url: str = typer.Option(
        "",
        "--llm-base-url",
        help="OpenAI-compatible loopback URL for --correspondent llm.",
    ),
    llm_model: str = typer.Option(
        "sandbox-correspondent", "--llm-model", help="Model name sent to the endpoint."
    ),
    expected: bool = typer.Option(
        True,
        "--expected/--no-expected",
        help="Show scenario expected outcomes next to actual.",
    ),
) -> None:
    """Serve the offline sandbox with rule or optional loopback LLM triage.

    The document pipeline uses a mock LLM. Content loading/validation failures
    exit with code 1; invalid profile, autonomy, or Correspondent options exit
    with code 2.
    """
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
    if correspondent not in {"standin", "llm"}:
        typer.echo("--correspondent must be standin|llm", err=True)
        raise typer.Exit(code=2)
    options: dict = {}
    if correspondent == "llm":
        from mailroom_reloaded.sandbox.server.llm_correspondent import _check_loopback

        try:
            if not llm_base_url:
                raise ValueError("--correspondent llm needs --llm-base-url")
            _check_loopback(llm_base_url)
        except ValueError as exc:
            typer.echo(f"mailroom sandbox serve: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        options = {"base_url": llm_base_url, "model": llm_model}
    silence_exporters()
    guard = NetworkGuard().install()
    service = SandboxService(
        loaded,
        data_dir.resolve(),
        egress_profile=egress,
        autonomy=autonomy,
        show_expected=expected,
        guard=guard,
        correspondent=correspondent,
        correspondent_options=options,
    )
    typer.echo(
        f"mailroom sandbox: content={loaded.kind} {root} data={data_dir.resolve()} (offline: network guard on)"
    )
    typer.echo(
        f"mailroom sandbox: UI http://{'127.0.0.1' if host in {'0.0.0.0', '::'} else host}:{port}/ui"
    )
    uvicorn.run(create_sandbox_app(service), host=host, port=port, log_level="info")


@sandbox_app.command("conformance")
def conformance(
    content: str = typer.Option(
        "locked", "--content", help="smoke | locked | <content dir>."
    ),
    data_dir: Path = typer.Option(
        Path(".sandbox-conformance"), "--data-dir", help="Throwaway state dir."
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Write the full per-check result JSON here."
    ),
    only: list[str] = typer.Option(
        [], "--only", help="Restrict to these scenario ids (repeatable)."
    ),
    slim: bool = typer.Option(
        False, "--slim", help="Omit per-check rows from the JSON (baseline form)."
    ),
    lofo_out: Path | None = typer.Option(
        None, "--lofo", help="Also write the leave-one-family-out report here."
    ),
) -> None:
    """Run selected scenarios in isolation and print conformance and LOFO tables.

    The data directory is disposable: runs reset sandbox and pipeline state.
    Content loading/validation failures exit with code 1. Scenario failures are
    reported without setting a failing exit code. Single-family runs report
    no training rate; output I/O errors propagate.
    """
    from mailroom_reloaded.sandbox.server.conformance import (
        dumps,
        format_lofo,
        format_table,
        lofo,
        run_conformance,
    )
    from mailroom_reloaded.sandbox.server.content import (
        ContentSpecError,
        load_sandbox_content,
        resolve_content_spec,
    )
    from mailroom_reloaded.sandbox.server.guard import NetworkGuard
    from mailroom_reloaded.sandbox.server.service import SandboxService
    from mailroom_reloaded.sandbox.server.telemetry import silence_exporters

    try:
        loaded = load_sandbox_content(resolve_content_spec(content))
    except (ContentSpecError, FileNotFoundError, ValueError, OSError) as exc:
        typer.echo(f"mailroom sandbox conformance: {exc}", err=True)
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
    svc = SandboxService(loaded, data_dir.resolve(), guard=guard).start(worker=False)
    try:
        result = run_conformance(svc, only or None)
    finally:
        svc.stop()
        guard.uninstall()
    typer.echo(format_table(result))
    rep = lofo(result["scenarios"])
    typer.echo(format_lofo(rep))
    if lofo_out is not None:
        import json

        lofo_out.write_text(
            json.dumps(rep, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    if json_out is not None:
        json_out.write_text(dumps(result, slim=slim), encoding="utf-8")
