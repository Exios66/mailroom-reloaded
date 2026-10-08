"""``mailroom`` command line (plan Task 19; console script ``mailroom``).

Commands:

* ``mailroom serve`` — run the FastAPI app (and, by default, the embedded
  watcher) under uvicorn.
* ``mailroom watch`` — run the standalone filesystem watcher.
* ``mailroom run <file>`` — run one document through the pipeline.
* ``mailroom eval ...`` — run an evaluation posture (Task 20).
* ``mailroom train-gate ...`` — fit the route-gate model / calibration (Task 13).
* ``mailroom card ...`` — SAND-37 cards (Task 21, not yet implemented).
* ``mailroom conformance ...`` — behavioural conformance suite (Task 24, not yet
  implemented).

The two not-yet-built commands exit non-zero with an explicit message; they do
not fabricate output.
"""

# ruff: noqa: B008 - Typer/FastAPI options are function calls in defaults by design.

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

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


@app.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
)
def card(ctx: typer.Context) -> None:
    """SAND-37 scorecards (plan Task 21) — not yet implemented."""
    typer.echo(
        "mailroom card: not yet implemented (plan Task 21)", err=True
    )
    raise typer.Exit(code=1)


@app.command(
    name="conformance",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
def conformance(ctx: typer.Context) -> None:
    """Behavioural conformance suite (plan Task 24) — not yet implemented."""
    typer.echo(
        "mailroom conformance: not yet implemented (plan Task 24)", err=True
    )
    raise typer.Exit(code=1)


def main() -> None:
    """Console-script entry point (``mailroom = mailroom_reloaded.cli:main``)."""
    app()


if __name__ == "__main__":
    main()
