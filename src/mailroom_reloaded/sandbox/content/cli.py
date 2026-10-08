# ruff: noqa: B008
"""``mailroom sandbox content pull|validate|build|bump|status``."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import typer

from mailroom_reloaded.sandbox.content import bundle as bundle_mod
from mailroom_reloaded.sandbox.content.compat import CompatError, check_compat
from mailroom_reloaded.sandbox.content.loader import SMOKE_DIR, load_content
from mailroom_reloaded.sandbox.content.lock import (
    ContentLock,
    LockError,
    sha256_file,
    verify_bundle,
)

content_app = typer.Typer(name="content", help="Pinned sandbox content pack.", no_args_is_help=True)
sandbox_app = typer.Typer(name="sandbox", help="Testing sandbox.", no_args_is_help=True)
sandbox_app.add_typer(content_app, name="content")

LockOpt = typer.Option(Path("sandbox/content.lock"), "--lock", help="Path to content.lock.")


def _fail(msg: str) -> typer.Exit:
    """Print a prefixed error to stderr and return, without raising, an exit with code 1."""
    typer.echo(f"mailroom sandbox content: {msg}", err=True)
    return typer.Exit(code=1)


@content_app.command()
def status(lock: Path = LockOpt) -> None:
    """Print JSON with the pin, smoke version/tag match, validity, and scenario count.

    LockError, CompatError, and FileNotFoundError become exit code 1. Reported
    smoke validation errors or a tag mismatch do not cause a nonzero exit.
    """
    try:
        pin = ContentLock.read(lock)
        smoke = load_content(SMOKE_DIR)
    except (LockError, CompatError, FileNotFoundError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(json.dumps({
        "lock": pin.__dict__,
        "smoke_content_version": smoke.meta.get("content_version"),
        "smoke_matches_lock": f"v{smoke.meta.get('content_version')}" == pin.tag,
        "smoke_valid": smoke.report.ok,
        "smoke_scenarios": len(smoke.scenarios),
    }, indent=2))


@content_app.command()
def validate(
    path: Path = typer.Argument(SMOKE_DIR, help="Content dir or smoke export (default: committed smoke)."),
) -> None:
    """Validate a content directory or smoke export, defaulting to committed smoke.

    Print a success summary or report validation errors to stderr and exit 1.
    CompatError and FileNotFoundError also become exit code 1; other loading
    errors propagate.
    """
    try:
        cs = load_content(path)
    except (CompatError, FileNotFoundError) as exc:
        raise _fail(str(exc)) from exc
    for e in cs.report.errors:
        typer.echo(e, err=True)
    if not cs.report.ok:
        raise typer.Exit(code=1)
    typer.echo(f"ok: {cs.kind} {cs.root} ({cs.report.checked} files checked)")


@content_app.command()
def pull(
    from_bundle: Path | None = typer.Option(None, "--from-bundle", help="Local .tar.zst bundle."),
    from_dir: Path | None = typer.Option(None, "--from-dir", help="Already materialized content dir."),
    url: str | None = typer.Option(None, "--url", help="https bundle URL (needs --allow-network)."),
    allow_network: bool = typer.Option(False, "--allow-network", help="Permit the URL fetch (off by default)."),
    dest: Path = typer.Option(Path(".sandbox-content"), "--dest"),
    lock: Path = LockOpt,
) -> None:
    """Materialize content into --dest from exactly one directory, bundle, or URL.

    Directory imports check compatibility and the pinned tag, then replace dest.
    Bundles check the pinned SHA-256 before extraction and compatibility afterward;
    unrelated destination files remain. URL downloads require --allow-network.
    Failures may leave destination changes in place. Invalid source selection,
    LockError, CompatError, OSError, ValueError, and RuntimeError become exit code 1;
    archive and decompression errors propagate.
    """
    if sum(x is not None for x in (from_bundle, from_dir, url)) != 1:
        raise _fail("give exactly one of --from-bundle, --from-dir, --url")
    try:
        pin = ContentLock.read(lock)
        if from_dir is not None:
            meta = json.loads((from_dir / "content.json").read_text(encoding="utf-8"))
            check_compat(meta)
            if f"v{meta.get('version')}" != pin.tag:
                raise LockError(f"dir is content {meta.get('version')}, lock pins {pin.tag}")
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(from_dir, dest)
        else:
            if url is not None:
                if not allow_network:
                    raise LockError("--url requires --allow-network")
                from_bundle = bundle_mod.fetch_url(url, Path(tempfile.mkdtemp()) / "bundle.tar.zst")
            assert from_bundle is not None
            bundle_mod.extract_bundle(from_bundle, dest, pin)
            check_compat(json.loads((dest / "content.json").read_text(encoding="utf-8")))
    except (LockError, CompatError, OSError, ValueError, RuntimeError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"pulled {pin.tag} -> {dest}")


@content_app.command()
def build(
    from_dir: Path = typer.Option(Path(".sandbox-content"), "--from-dir"),
    out: Path = typer.Option(SMOKE_DIR, "--out", help="Smoke fixtures dir to (re)write."),
) -> None:
    """Validate a content dir, then regenerate the smoke fixtures with its tools/export_smoke.py."""
    try:
        cs = load_content(from_dir)
    except (CompatError, FileNotFoundError) as exc:
        raise _fail(str(exc)) from exc
    if not cs.report.ok:
        for e in cs.report.errors:
            typer.echo(e, err=True)
        raise typer.Exit(code=1)
    tool = from_dir / "tools" / "export_smoke.py"
    if not tool.is_file():
        raise _fail(f"{tool} not found")
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "smoke"
        r = subprocess.run(
            [sys.executable, "-I", str(tool), "--root", str(from_dir), "--out", str(staged)],
            capture_output=True, text=True, check=False,
        )
        if r.returncode:
            raise _fail(f"export_smoke failed: {r.stderr.strip() or r.stdout.strip()}")
        check = load_content(staged)
        if not check.report.ok:
            raise _fail("exported smoke set invalid:\n" + "\n".join(check.report.errors))
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(staged, out)
    typer.echo(f"smoke fixtures written to {out}")


@content_app.command()
def bump(
    bundle: Path = typer.Option(..., "--bundle", help="Local bundle to pin."),
    tag: str = typer.Option(..., "--tag"),
    commit: str = typer.Option(..., "--commit"),
    lock: Path = LockOpt,
) -> None:
    """Rewrite content.lock for a new bundle (sha256, schema_version and dataset_revision come from it)."""
    try:
        meta = bundle_mod.read_content_json(bundle)
        check_compat(meta)
        new = ContentLock(
            repo=ContentLock.read(lock).repo if lock.exists() else "Exios66/mailroom-sandbox-content",
            tag=tag, commit=commit, bundle_sha256=sha256_file(bundle),
            schema_version=str(meta["schema_version"]), dataset_revision=str(meta["dataset_revision"]),
        )
        new.write(lock)
        verify_bundle(bundle, new)
    except (LockError, CompatError, OSError, ValueError, RuntimeError, KeyError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"lock bumped to {tag} ({new.bundle_sha256[:12]})")
