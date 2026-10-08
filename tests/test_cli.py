"""CLI contracts with service, pipeline, and training work replaced by mocks."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from mailroom_reloaded import cli


@pytest.fixture
def runner(monkeypatch):
    for name in (
        "MAILROOM_API_HOST",
        "MAILROOM_API_PORT",
        "PORT",
        "MAILROOM_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    return CliRunner()


@pytest.mark.parametrize(
    "value,expected", [(None, "127.0.0.1"), ("  ", "127.0.0.1"), (" ::1 ", "::1")]
)
def test_listen_host(monkeypatch, value, expected):
    monkeypatch.delenv("MAILROOM_API_HOST", raising=False)
    if value is not None:
        monkeypatch.setenv("MAILROOM_API_HOST", value)
    assert cli.listen_host() == expected


@pytest.mark.parametrize(
    "platform,configured,expected",
    [
        (None, None, 8000),
        (" ", " ", 8000),
        (None, " 9000 ", 9000),
        (" 7000 ", "9000", 7000),
    ],
)
def test_listen_port_precedence(monkeypatch, platform, configured, expected):
    for key, value in [("PORT", platform), ("MAILROOM_API_PORT", configured)]:
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    assert cli.listen_port() == expected


def test_invalid_platform_port_is_not_silently_ignored(monkeypatch):
    monkeypatch.setenv("PORT", "invalid")
    monkeypatch.setenv("MAILROOM_API_PORT", "8000")
    with pytest.raises(ValueError):
        cli.listen_port()


@pytest.mark.parametrize("watch,expected", [("--watch", "1"), ("--no-watch", "0")])
def test_serve_explicit_options_override_environment(
    runner, monkeypatch, watch, expected
):
    import os

    import uvicorn

    from mailroom_reloaded.api.app import app

    monkeypatch.setenv("MAILROOM_API_HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "invalid")
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "old")
    serve = Mock()
    monkeypatch.setattr(uvicorn, "run", serve)

    result = runner.invoke(cli.app, ["serve", "--host", "::1", "--port", "0", watch])

    assert result.exit_code == 0, result.output
    serve.assert_called_once_with(app, host="::1", port=0)
    assert os.environ["MAILROOM_EMBED_WATCHER"] == expected


def test_serve_refuses_unprotected_public_bind_before_starting(runner, monkeypatch):
    import os

    import uvicorn

    serve = Mock()
    monkeypatch.setattr(uvicorn, "run", serve)
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "unchanged")

    result = runner.invoke(cli.app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 1
    assert "Refusing to bind" in result.output
    serve.assert_not_called()
    assert os.environ["MAILROOM_EMBED_WATCHER"] == "unchanged"


@pytest.mark.parametrize("doc_type", [None, "correspondence"])
def test_run_prints_machine_readable_summary(runner, tmp_path, monkeypatch, doc_type):
    from mailroom_reloaded.pipeline import flow

    path = tmp_path / "letter.txt"
    path.write_text("letter")
    state = SimpleNamespace(
        doc_id="abc",
        status="parked",
        route_trail=["sort", "human_review"],
        sort=SimpleNamespace(doc_type=doc_type) if doc_type else None,
    )
    run = Mock(return_value=state)
    monkeypatch.setattr(flow, "run_document", run)

    result = runner.invoke(cli.app, ["run", str(path), "--worker-id", "worker-2"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "doc_id": "abc",
        "status": "parked",
        "doc_type": doc_type,
        "route_trail": ["sort", "human_review"],
    }
    run.assert_called_once_with(path, worker_id="worker-2")


@pytest.mark.parametrize("existing_directory", [False, True])
def test_run_rejects_invalid_path_before_processing(
    runner, tmp_path, monkeypatch, existing_directory
):
    from mailroom_reloaded.pipeline import flow

    run = Mock()
    monkeypatch.setattr(flow, "run_document", run)
    path = tmp_path if existing_directory else tmp_path / "missing.txt"
    result = runner.invoke(cli.app, ["run", str(path)])
    assert result.exit_code == 2
    run.assert_not_called()


@pytest.mark.parametrize(
    "classes,expected",
    [
        ("", None),
        (" , ", None),
        ("contract, correspondence,", ["contract", "correspondence"]),
    ],
)
def test_eval_passes_options_without_running_models(
    runner, tmp_path, monkeypatch, classes, expected
):
    from mailroom_reloaded.eval import runner as eval_runner

    run = Mock(return_value="run-123")
    monkeypatch.setattr(eval_runner, "run_eval", run)
    result = runner.invoke(
        cli.app,
        [
            "eval",
            "--classes",
            classes,
            "--revision",
            "fixture-revision",
            "--per-class",
            "3",
            "--seed",
            "7",
            "--concurrency",
            "2",
            "--judge-sample-rate",
            "0",
            "--local-dir",
            str(tmp_path),
            "--mode",
            "specialist_cell",
            "--split",
            "validation",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "run-123"
    run.assert_called_once()
    cfg = run.call_args.args[0]
    assert cfg == eval_runner.EvalConfig(
        classes=expected,
        revision="fixture-revision",
        per_class=3,
        seed=7,
        concurrency=2,
        judge_sample_rate=0,
        local_dir=tmp_path,
        mode="specialist_cell",
        split="validation",
    )


@pytest.mark.parametrize(
    "option,value",
    [
        ("--per-class", "0"),
        ("--concurrency", "0"),
        ("--gpus", "0"),
        ("--judge-sample-rate", "-0.01"),
        ("--judge-sample-rate", "1.01"),
    ],
)
def test_eval_rejects_out_of_range_options(runner, monkeypatch, option, value):
    from mailroom_reloaded.eval import runner as eval_runner

    run = Mock()
    monkeypatch.setattr(eval_runner, "run_eval", run)
    result = runner.invoke(cli.app, ["eval", option, value])
    assert result.exit_code == 2
    run.assert_not_called()


@pytest.mark.parametrize("calibration", [False, True])
def test_train_gate_loads_jsonl_and_selects_trainer(
    runner, tmp_path, monkeypatch, calibration
):
    from mailroom_reloaded.eval import train_gate

    rows = tmp_path / "rows.jsonl"
    rows.write_text('\n{"confidence": 0.9}\n  \n{"confidence": 0.2}\n')
    out = tmp_path / "model.json"
    fit = Mock(return_value={"output": out, "rows": 2})
    other = Mock()
    monkeypatch.setattr(train_gate, "fit_calibration", fit if calibration else other)
    monkeypatch.setattr(train_gate, "train_gate", other if calibration else fit)
    args = ["train-gate", "--rows", str(rows), "--out", str(out)]
    result = runner.invoke(cli.app, args + (["--calibration"] if calibration else []))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"output": str(out), "rows": 2}
    fit.assert_called_once_with([{"confidence": 0.9}, {"confidence": 0.2}], out)
    other.assert_not_called()


def test_train_gate_malformed_json_does_not_call_trainer(runner, tmp_path, monkeypatch):
    from mailroom_reloaded.eval import train_gate

    rows = tmp_path / "rows.jsonl"
    rows.write_text('{"confidence": 0.9}\ninvalid\n')
    fit = Mock()
    monkeypatch.setattr(train_gate, "train_gate", fit)
    result = runner.invoke(cli.app, ["train-gate", "--rows", str(rows)])
    assert result.exit_code != 0
    assert isinstance(result.exception, json.JSONDecodeError)
    fit.assert_not_called()


@pytest.mark.parametrize("command", ["card", "conformance"])
def test_unimplemented_commands_fail_explicitly(runner, command):
    result = runner.invoke(cli.app, [command, "--future-option", "value"])
    assert result.exit_code == 1
    assert "not yet implemented" in result.stderr
    assert result.stdout == ""
