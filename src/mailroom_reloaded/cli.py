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
"""

# ruff: noqa: B008 - Typer/FastAPI options are function calls in defaults by design.

from __future__ import annotations

import json
import os
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
