"""``mailroom`` command line (plan Task 19; console script ``mailroom``).

Commands:

* ``mailroom serve`` — run the FastAPI app (and, by default, the embedded
  watcher) under uvicorn.
* ``mailroom watch`` — run the standalone filesystem watcher.
* ``mailroom run <file>`` — run one document through the pipeline.
* ``mailroom eval ...`` — run an evaluation posture (Task 20).
* ``mailroom train-gate ...`` — fit the route-gate model / calibration (Task 13).
* ``mailroom card ...`` — SAND-37 scorecards, one ``mailroom.card/v1`` per run
  or the aggregated master card (Task 21).
* ``mailroom conformance ...`` — behavioural conformance suite (Task 24).
* ``mailroom replay import|export|sessions`` — offline OTLP import and replay/v1 export.
"""

# ruff: noqa: B008 - Typer/FastAPI options are function calls in defaults by design.

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import structlog
import typer

logger = structlog.get_logger(__name__)

app = typer.Typer(
    name="mailroom",
    help="Compressed Digital Mailroom: run, watch, serve and evaluate.",
    no_args_is_help=True,
    add_completion=False,
)


def listen_host() -> str:
    """Read MAILROOM_API_HOST, defaulting to the loopback address."""
    return (os.environ.get("MAILROOM_API_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def listen_port() -> int:
    """Read PORT before MAILROOM_API_PORT, defaulting to port 8000."""
    platform = (os.environ.get("PORT") or "").strip()
    if platform:
        return int(platform)
    return int((os.environ.get("MAILROOM_API_PORT") or "8000").strip() or "8000")


@app.command()
def serve(
    host: str = typer.Option(None, "--host", help="Bind host (default MAILROOM_API_HOST)."),
    port: int = typer.Option(None, "--port", help="Bind port (default MAILROOM_API_PORT/PORT)."),
    watch: bool = typer.Option(
        True, "--watch/--no-watch", help="Run the embedded inbox watcher with the API."
    ),
) -> None:
    """Serve the API and the ``/ui`` page."""
    import uvicorn

    from mailroom_reloaded.api.app import app as fastapi_app
    from mailroom_reloaded.api.app import assert_bind_allowed

    resolved_host = host or listen_host()
    resolved_port = port if port is not None else listen_port()
    assert_bind_allowed(resolved_host)
    os.environ["MAILROOM_EMBED_WATCHER"] = "1" if watch else "0"
    # keep the lifespan guard in step with the host uvicorn.run() is given below
    os.environ["MAILROOM_API_HOST"] = resolved_host
    os.environ["UVICORN_HOST"] = resolved_host
    uvicorn.run(fastapi_app, host=resolved_host, port=resolved_port)


@app.command()
def watch(
    worker_id: str = typer.Option("cli-watcher", "--worker-id"),
    concurrency: int = typer.Option(1, "--concurrency", min=1, max=32),
) -> None:
    """Drain ``inbox/`` forever as a standalone watcher."""
    from mailroom_reloaded.settings import get_settings
    from mailroom_reloaded.storage.bins import Bins
    from mailroom_reloaded.watcher import Watcher

    Watcher(Bins(get_settings().base_dir), worker_id, concurrency).run_forever()


@app.command()
def run(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    worker_id: str = typer.Option("cli", "--worker-id"),
) -> None:
    """Run one document through the pipeline and print a summary."""
    from mailroom_reloaded.pipeline.flow import run_document

    state = run_document(file, worker_id=worker_id)
    typer.echo(
        json.dumps(
            {
                "doc_id": state.doc_id,
                "status": state.status,
                "doc_type": state.sort.doc_type if state.sort is not None else None,
                "route_trail": state.route_trail,
            }
        )
    )


@app.command()
def eval(
    revision: str = typer.Option("ed7576b6", "--revision"),
    per_class: int = typer.Option(20, "--per-class", min=1),
    seed: int = typer.Option(42, "--seed"),
    classes: str = typer.Option("", "--classes", help="Comma-separated class filter."),
    concurrency: int = typer.Option(8, "--concurrency", min=1),
    posture_label: str = typer.Option("pipeline", "--posture-label"),
    gpu: str = typer.Option("L4", "--gpu"),
    gpus: int = typer.Option(1, "--gpus", min=1),
    prompt_set: str = typer.Option("frozen_v1", "--prompt-set"),
    merger_mode: str = typer.Option("frozen", "--merger-mode"),
    mode: str = typer.Option("pipeline", "--mode", help="pipeline | specialist_cell"),
    judge_sample_rate: float = typer.Option(1.0, "--judge-sample-rate", min=0.0, max=1.0),
    split: str = typer.Option("test", "--split"),
    local_dir: Path = typer.Option(None, "--local-dir", exists=True, file_okay=False),
    dataset_repo: str = typer.Option(
        None, "--dataset-repo", help="Hub repo override (e.g. the fixtures adaptation)."
    ),
    config: str = typer.Option(
        None, "--config", help="Single labeled config (fixtures/bundles) instead of the join."
    ),
    gpu_usd_per_hour: float = typer.Option(0.80, "--gpu-usd-per-hour"),
) -> None:
    """Run an evaluation posture and print its ``run_id``."""
    from mailroom_reloaded.eval.runner import EvalConfig, run_eval

    cfg = EvalConfig(
        revision=revision,
        per_class=per_class,
        seed=seed,
        classes=[c.strip() for c in classes.split(",") if c.strip()] or None,
        concurrency=concurrency,
        posture_label=posture_label,
        gpu=gpu,
        gpus=gpus,
        prompt_set=prompt_set,
        merger_mode=merger_mode,
        mode=mode,  # type: ignore[arg-type]
        judge_sample_rate=judge_sample_rate,
        split=split,
        local_dir=local_dir,
        dataset_repo=dataset_repo,
        dataset_config=config,
        gpu_usd_per_hour=gpu_usd_per_hour,
    )
    run_id = run_eval(cfg)
    typer.echo(run_id)


@app.command("train-gate")
def train_gate_command(
    rows: Path = typer.Option(..., "--rows", exists=True, dir_okay=False, readable=True,
                             help="JSONL feature rows (eval_docs echoes)."),
    out: Path = typer.Option(Path("models/route_gate.json"), "--out"),
    calibration: bool = typer.Option(
        False, "--calibration", help="Fit temperature calibration instead of the gate."
    ),
) -> None:
    """Fit the route gate (or calibration) and print the metrics."""
    from mailroom_reloaded.eval.train_gate import fit_calibration, train_gate

    data = [
        json.loads(line)
        for line in rows.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result = fit_calibration(data, out) if calibration else train_gate(data, out)
    typer.echo(json.dumps(result, default=str))


@app.command()
def card(
    run_id: list[str] = typer.Option(
        ..., "--run-id", help="Eval run id to scorecard (repeat for several)."
    ),
    doc_type: str = typer.Option(
        None, "--doc-type", help="Specialist cell (default: every cell in the run)."
    ),
    master: bool = typer.Option(
        False, "--master", help="Write the aggregated SAND-37 master card instead."
    ),
    out: Path = typer.Option(Path("runs"), "--out", help="Output root."),
) -> None:
    """Write SAND-37 scorecards for one or more eval runs (plan Task 21).

    A single ``--run-id`` writes ``<out>/<run_id>/cards/card-<doc_type|all>.{json,md}``;
    ``--master`` (or two or more run ids) writes ``<out>/master.{json,md}``. Echoes
    the path of the Markdown card written.
    """
    from mailroom_reloaded.eval.cards import build_card, build_master, render_card_md

    if master or len(run_id) > 1:
        data, markdown = build_master(list(run_id))
        target = (out / "master").with_suffix(".md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.with_suffix(".json").write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )
        target.write_text(markdown, encoding="utf-8")
        typer.echo(str(target))
        return

    rid = run_id[0]
    card_data = build_card(rid, doc_type)
    markdown = render_card_md(card_data)
    target = (out / rid / "cards" / f"card-{doc_type or 'all'}").with_suffix(".md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.with_suffix(".json").write_text(
        json.dumps(card_data, indent=2, default=str), encoding="utf-8"
    )
    target.write_text(markdown, encoding="utf-8")
    typer.echo(str(target))


@app.command(name="conformance")
def conformance(
    provider: str = typer.Option(
        "", "--provider", help="Provider to conformance-test (default: configured provider)."
    ),
    per_class: int = typer.Option(2, "--per-class", min=1),
    revision: str = typer.Option("ed7576b6", "--revision"),
    split: str = typer.Option("train", "--split"),
    local_dir: Path = typer.Option(None, "--local-dir", exists=True, file_okay=False),
    out: Path = typer.Option(Path("runs/conformance"), "--out"),
) -> None:
    """Run the behavioural conformance suite (plan Task 24) and write a card."""
    from mailroom_reloaded.eval.conformance import run_conformance

    card = run_conformance(
        provider or None,
        per_class=per_class,
        revision=revision,
        split=split,
        local_dir=local_dir,
        out_dir=out,
    )
    typer.echo(
        json.dumps(
            {
                "provider": card.provider,
                "model": card.model,
                "roles": {
                    role: stats.to_dict() for role, stats in card.roles.items()
                },
                "out": str(out),
            }
        )
    )


audit_app = typer.Typer(
    name="audit",
    help="Archive ledger: verify the hash chain, anchor its head off-host, export the head.",
    no_args_is_help=True,
)
app.add_typer(audit_app, name="audit")


def _short(h: str | None) -> str:
    """First 12 hex characters of a hash, or ``-``."""
    return (h or "-")[:12]


@audit_app.command("verify")
def audit_verify(
    run: str = typer.Option(
        None, "--run", help="Verify one run instead of the whole ledger."
    ),
    external: bool = typer.Option(
        False, "--external", help="Also compare the head with the external anchor."
    ),
) -> None:
    """Verify the ledger chain (and with ``--external`` the anchor).

    Exit codes: 0 ok, 1 tamper (chain broken, TRUNCATED or REWRITTEN), 3 anchor store
    unreachable, 4 anchor not configured, 5 STALE (entries unanchored for over 24 h).
    Code 2 is left to the CLI's own usage errors.
    """
    from mailroom_reloaded.storage import anchor
    from mailroom_reloaded.storage.ledger import get_ledger

    ledger = get_ledger(anchor=False)
    verdict = ledger.verify(run)
    if run is not None and not verdict.ok and verdict.detail == "unknown run":
        typer.echo(f"unknown run {run!r}")
        raise typer.Exit(2)
    if not verdict.ok:
        where = f" at {verdict.broken_at}" if verdict.broken_at is not None else ""
        typer.echo(f"chain: broken{where} ({verdict.detail or 'invalid'})")
        raise typer.Exit(anchor.EXIT_TAMPER)
    merkle = (
        ""
        if verdict.merkle_ok is None
        else (", merkle ok" if verdict.merkle_ok else ", merkle BAD")
    )
    typer.echo(
        f"chain: ok ({verdict.count} entries, head {verdict.head_seq} {_short(verdict.head_hash)}{merkle})"
    )
    if not external:
        return
    try:
        cfg = anchor.get_config()
    except anchor.AnchorNotConfigured as exc:
        typer.echo(f"anchor: not configured ({exc})")
        raise typer.Exit(anchor.EXIT_NOT_CONFIGURED) from None
    if cfg.backend == "export":
        typer.echo("anchor: export only; pin `mailroom audit export-head` off-host")
        raise typer.Exit(anchor.EXIT_NOT_CONFIGURED)
    try:
        backend = anchor.make_backend(cfg)
    except anchor.AnchorError as exc:
        typer.echo(f"anchor: not configured ({cfg.redact(str(exc))})")
        raise typer.Exit(anchor.EXIT_NOT_CONFIGURED) from None
    try:
        result = anchor.verify_external(ledger, cfg, backend)
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is never "tamper"
        typer.echo(f"anchor: unreachable ({type(exc).__name__})")
        raise typer.Exit(anchor.EXIT_UNREACHABLE) from None
    finally:
        close = getattr(backend, "close", None)
        if close:
            close()
    if result.key_file_warning:
        typer.echo("warning: the anchor key file is world-readable")
    detail = f": {cfg.redact(result.detail)}" if result.detail else ""
    typer.echo(
        f"anchor: {result.status.upper()} (anchored {result.anchored_seq}, local {result.local_seq}, "
        f"{result.unanchored} unanchored){detail}"
    )
    raise typer.Exit(result.exit_code)


@audit_app.command("anchor")
def audit_anchor() -> None:
    """Push the ledger head to the external anchor now (exit codes as for ``verify``)."""
    from mailroom_reloaded.storage import anchor
    from mailroom_reloaded.storage.ledger import get_ledger

    try:
        cfg = anchor.get_config()
        backend = anchor.make_backend(cfg)
    except anchor.AnchorError as exc:
        typer.echo(f"anchor: not configured ({exc})")
        raise typer.Exit(anchor.EXIT_NOT_CONFIGURED) from None
    ledger = get_ledger(anchor=False)
    try:
        result = anchor.push_head(ledger, backend)
    except anchor.AnchorConflict as exc:
        typer.echo(f"anchor: {cfg.redact(str(exc))}")
        raise typer.Exit(anchor.EXIT_TAMPER) from None
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is never "tamper"
        typer.echo(f"anchor: unreachable ({cfg.redact(str(exc)) if isinstance(exc, anchor.AnchorError) else type(exc).__name__})")
        raise typer.Exit(anchor.EXIT_UNREACHABLE) from None
    finally:
        close = getattr(backend, "close", None)
        if close:
            close()
    if result.status == "empty":
        typer.echo("ledger empty; nothing to anchor")
        return
    typer.echo(f"anchor: {result.status} {result.seq} {_short(result.entry_hash)}")


@audit_app.command("export-head")
def audit_export_head(
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the record here instead of stdout."
    ),
) -> None:
    """Print the ledger head as JSON for off-host pinning (works with any anchor setting)."""
    from mailroom_reloaded.storage import anchor
    from mailroom_reloaded.storage.ledger import get_ledger

    record = anchor.export_head(get_ledger(anchor=False))
    if record is None:
        typer.echo("ledger empty")
        return
    text = json.dumps(record, sort_keys=True)
    if out is not None:
        out.write_text(text + "\n", encoding="utf-8")
        typer.echo(str(out))
    else:
        typer.echo(text)


replay_app = typer.Typer(
    name="replay",
    help="Trace replay: import OTLP/JSON spans, export a replay/v1 timeline, list sessions.",
    no_args_is_help=True,
)
app.add_typer(replay_app, name="replay")


def _replay_fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


@replay_app.command("import")
def replay_import(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="OTLP/JSON or JSON-lines file."
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="File every span under this run id (overrides mailroom.run_id)."
    ),
    append: bool = typer.Option(
        False, "--append", help="Allow adding to a run that already has spans."
    ),
) -> None:
    """Import OTLP/JSON spans into the span store (allow-listed, idempotent)."""
    from mailroom_reloaded.obs.replay.otlp_import import (
        OtlpImportError,
        import_otlp_file,
    )
    from mailroom_reloaded.storage.retention import read_pruned_run_ids
    from mailroom_reloaded.storage.span_store import SpanStore, default_span_store_path

    store = SpanStore(default_span_store_path())
    try:
        with contextlib.redirect_stdout(sys.stderr):
            pruned = read_pruned_run_ids()
        result = import_otlp_file(file, store, run_id=run_id, append=append, pruned=pruned)
    except OtlpImportError as exc:
        raise _replay_fail(str(exc), 2) from None
    finally:
        store.close()
    typer.echo(
        json.dumps(
            {
                "parsed": result.parsed,
                "stored": result.stored,
                "skipped": result.skipped,
                "runs": result.runs,
                "sessions": result.sessions,
            }
        )
    )
    if result.skipped:
        typer.echo(
            f"warning: {result.skipped} of {result.parsed} spans were not stored "
            "(already present or over the per-run row cap)",
            err=True,
        )


@replay_app.command("export")
def replay_export(
    session: str = typer.Argument(..., help="Session id: run:<id>, session:<id>, doc:<id>."),
    out: Path = typer.Option(None, "--out", "-o", help="Write here instead of stdout."),
) -> None:
    """Write the ``replay/v1`` timeline of a session as JSON (exit 3: its data was pruned)."""
    from mailroom_reloaded.obs.replay.sessions import parse_session_id
    from mailroom_reloaded.obs.replay.timeline import build_timeline
    from mailroom_reloaded.storage.db import get_engine
    from mailroom_reloaded.storage.retention import read_pruned_run_ids

    try:
        kind, key = parse_session_id(session)
    except ValueError:
        raise _replay_fail("invalid session id", 2) from None
    try:
        with contextlib.redirect_stdout(sys.stderr):  # keep log lines out of the JSON
            pruned = kind == "run" and key in read_pruned_run_ids()
            tl = build_timeline(session, engine=get_engine())
    except Exception:
        logger.warning("replay_export_failed", exc_info=True)
        raise _replay_fail("timeline unavailable") from None
    if tl is None:
        raise _replay_fail("data pruned" if pruned else f"no timeline for {session}", 3 if pruned else 1)
    if pruned:
        tl.session.data_pruned = True
    text = tl.model_dump_json()
    if out is None:
        typer.echo(text)
        return
    try:
        out.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        raise _replay_fail(f"cannot write {out}: {exc.strerror or exc}") from None
    typer.echo(str(out))


@replay_app.command("sessions")
def replay_sessions(
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    as_json: bool = typer.Option(False, "--json", help="Print the API's JSON shape."),
) -> None:
    """List replayable sessions, newest first."""
    from mailroom_reloaded.obs.replay.sessions import list_sessions
    from mailroom_reloaded.storage.db import get_engine
    from mailroom_reloaded.storage.retention import read_pruned_run_ids

    try:
        with contextlib.redirect_stdout(sys.stderr):  # keep log lines out of the JSON
            sessions = list_sessions(limit, engine=get_engine())
            pruned = read_pruned_run_ids()
        for sm in sessions:
            if sm.kind == "run" and sm.id.removeprefix("run:") in pruned:
                sm.data_pruned = True
    except Exception:
        logger.warning("replay_sessions_failed", exc_info=True)
        raise _replay_fail("sessions unavailable") from None
    if as_json:
        typer.echo(json.dumps({"sessions": [s.model_dump(mode="json") for s in sessions]}))
        return
    if not sessions:
        typer.echo("no sessions")
    for s in sessions:
        typer.echo(
            f"{s.id}\t{s.environment}\t{s.documents} docs\t{s.duration_s}s\t{s.started_at}\t{s.source}"
            + ("\tdata pruned" if s.data_pruned else "")
        )


jev_app = typer.Typer(
    name="jev",
    help="Jev (TypeSafe System One) decision model: ask decisions and calibrate.",
    no_args_is_help=True,
)
app.add_typer(jev_app, name="jev")


def _jev_off() -> None:
    """Print the disabled message and exit non-zero."""
    typer.echo(
        "Jev is off (MAILROOM_JEV_PROVIDER=off). Set a provider "
        "(openrouter|typesafe|local) to enable it.",
        err=True,
    )
    raise typer.Exit(code=1)


@jev_app.command()
def decide(
    state: str = typer.Option(..., "--state", help="State text/prompt passed to Jev."),
    question_type: str = typer.Option(..., "--type", help="choice | noul | score."),
    instructions: str = typer.Option(..., "--instructions", help="Question instructions."),
    criteria: list[str] = typer.Option(
        None, "--criteria", help="Repeatable KEY=DESCRIPTION (choice/noul)."
    ),
    criteria_list: str = typer.Option(
        None, "--criteria-list", help="Comma-separated criteria labels (score)."
    ),
) -> None:
    """Ask Jev one question and print the JSON ``{"answers": ...}`` envelope."""
    from mailroom_reloaded.agents.jev import JevClient, choice, noul, score
    from mailroom_reloaded.settings import jev_config

    cfg = jev_config()
    if not cfg.enabled:
        _jev_off()

    pairs: dict[str, str] = {}
    for item in criteria or []:
        key, _, description = item.partition("=")
        pairs[key.strip()] = description.strip()
    labels = [c.strip() for c in (criteria_list or "").split(",") if c.strip()]

    if question_type == "choice":
        question = choice("question", instructions, pairs)
    elif question_type == "noul":
        question = noul("question", instructions, pairs or None)
    elif question_type == "score":
        question = score("question", instructions, labels)
    else:
        typer.echo(f"unknown --type {question_type!r}; use choice|noul|score", err=True)
        raise typer.Exit(code=1)

    name, payload = question
    answers = JevClient(cfg).ask(state, {name: payload})
    envelope = {"answers": {key: asdict(value) for key, value in answers.items()}}
    typer.echo(json.dumps(envelope, default=str))


@jev_app.command()
def calibrate(
    rows: Path = typer.Option(
        ...,
        "--rows",
        exists=True,
        dir_okay=False,
        readable=True,
        help="JSONL rows with split=train, confidence and correct.",
    ),
    out: Path = typer.Option(Path("models/jev_calibration.json"), "--out"),
) -> None:
    """Fit the Jev temperature + thresholds and write the calibration JSON."""
    from mailroom_reloaded.eval.jev_calibration import fit_jev_calibration

    data = [
        json.loads(line)
        for line in rows.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result = fit_jev_calibration(data, out)
    typer.echo(json.dumps(result, default=str))


gmail_app = typer.Typer(
    name="gmail",
    help="Gmail attachment intake (requires the optional 'gmail' extra).",
    no_args_is_help=True,
)
app.add_typer(gmail_app, name="gmail")

from mailroom_reloaded.sandbox.content.cli import sandbox_app
from mailroom_reloaded.sandbox.server import (
    cli as _sandbox_server_cli,  # noqa: F401  (registers `sandbox serve`)
)

app.add_typer(sandbox_app, name="sandbox")


def _gmail_failure(exc: Exception) -> typer.Exit:
    """Print a Gmail command failure and return a non-zero exit."""
    typer.echo(f"mailroom gmail: {exc}", err=True)
    return typer.Exit(code=1)


def _drain_inbox(worker_id: str) -> int:
    from mailroom_reloaded.settings import get_settings
    from mailroom_reloaded.storage.bins import Bins
    from mailroom_reloaded.watcher import Watcher

    return Watcher(Bins(get_settings().base_dir), worker_id, 1).drain_once()


@gmail_app.command()
def auth() -> None:
    """Run the OAuth installed-app flow and cache a token under the data dir."""
    from mailroom_reloaded.intake import gmail as gmail_intake

    try:
        intake = gmail_intake.GmailIntake.from_env()
        intake.authenticate()
    except (gmail_intake.GmailNotInstalled, gmail_intake.GmailAuthError) as exc:
        raise _gmail_failure(exc) from exc
    typer.echo(f"Authenticated. Token cached at {intake.config.token_path}")


@gmail_app.command()
def poll(
    limit: int = typer.Option(25, "--limit", min=1, max=200),
    process: bool = typer.Option(
        False, "--process/--no-process", help="Drain the inbox once after importing."
    ),
    worker_id: str = typer.Option("gmail-cli", "--worker-id"),
) -> None:
    """Fetch new Gmail attachments into ``inbox/`` and print the created doc_ids."""
    from mailroom_reloaded.intake import gmail as gmail_intake

    try:
        doc_ids = gmail_intake.poll_and_ingest(limit=limit)
    except (gmail_intake.GmailNotInstalled, gmail_intake.GmailAuthError) as exc:
        raise _gmail_failure(exc) from exc
    summary: dict = {"doc_ids": doc_ids, "count": len(doc_ids)}
    if process and doc_ids:
        summary["processed"] = _drain_inbox(worker_id)
    typer.echo(json.dumps(summary))


@gmail_app.command("watch")
def gmail_watch(
    interval: float = typer.Option(30.0, "--interval", min=1.0),
    limit: int = typer.Option(25, "--limit", min=1, max=200),
    process: bool = typer.Option(
        False, "--process/--no-process", help="Drain the inbox after each poll."
    ),
    worker_id: str = typer.Option("gmail-cli", "--worker-id"),
) -> None:
    """Poll Gmail on an interval until interrupted (Ctrl-C)."""
    import time

    from mailroom_reloaded.intake import gmail as gmail_intake

    typer.echo(f"Polling Gmail every {interval:g}s (Ctrl-C to stop)...")
    try:
        while True:
            try:
                doc_ids = gmail_intake.poll_and_ingest(limit=limit)
            except (gmail_intake.GmailNotInstalled, gmail_intake.GmailAuthError) as exc:
                raise _gmail_failure(exc) from exc
            except Exception:
                logger.exception("gmail_poll_failed")
                doc_ids = []
            if doc_ids:
                summary: dict = {"doc_ids": doc_ids, "count": len(doc_ids)}
                if process:
                    summary["processed"] = _drain_inbox(worker_id)
                typer.echo(json.dumps(summary))
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("Stopped.")


def main() -> None:
    """Console-script entry point (``mailroom = mailroom_reloaded.cli:main``)."""
    app()


if __name__ == "__main__":
    main()
