"""CLI surface tests (plan Task 19 / Task 21).

The heavy machinery lives in the modules under test; these pin the command
wiring so a broken option name or output path fails loudly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from mailroom_reloaded import cli
from mailroom_reloaded.eval import cards, conformance

runner = CliRunner()


def _stub_card(run_id: str, doc_type: str | None, **_kwargs: object) -> dict:
    return {"schema": "mailroom.card/v1", "run_id": run_id, "doc_type": doc_type}


def test_card_requires_run_id() -> None:
    result = runner.invoke(cli.app, ["card"])
    assert result.exit_code != 0


def test_card_help_mentions_master() -> None:
    result = runner.invoke(cli.app, ["card", "--help"])
    assert result.exit_code == 0
    assert "--master" in result.stdout


def test_card_single_writes_json_and_md(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cards, "build_card", _stub_card)
    monkeypatch.setattr(cards, "render_card_md", lambda card: "# card\n")

    result = runner.invoke(
        cli.app,
        ["card", "--run-id", "run-a", "--doc-type", "merger_agreement", "--out", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    card_json = tmp_path / "run-a" / "cards" / "card-merger_agreement.json"
    card_md = tmp_path / "run-a" / "cards" / "card-merger_agreement.md"
    assert card_json.exists() and card_md.exists()
    assert json.loads(card_json.read_text())["run_id"] == "run-a"
    assert result.stdout.strip() == str(card_md)


def test_card_multi_run_writes_master(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        cards, "build_master", lambda run_ids: ({"runs": list(run_ids)}, "# master\n")
    )

    result = runner.invoke(
        cli.app,
        ["card", "--run-id", "run-a", "--run-id", "run-b", "--out", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "master.json").exists()
    assert (tmp_path / "master.md").read_text() == "# master\n"


def test_conformance_command_writes_card(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_run(provider, **kwargs):
        seen["provider"] = provider
        seen["out_dir"] = kwargs.get("out_dir")
        return conformance.ConformanceCard(
            provider=provider or "mock",
            model="fake-model",
            roles={"sorter": conformance.RoleStats(1.0, 1.0, [])},
        )

    monkeypatch.setattr(conformance, "run_conformance", fake_run)

    result = runner.invoke(
        cli.app,
        ["conformance", "--provider", "mock", "--out", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert seen["provider"] == "mock"
    assert seen["out_dir"] == tmp_path
    payload = json.loads(result.stdout)
    assert payload["out"] == str(tmp_path)
    assert payload["roles"]["sorter"]["tool_call_success_rate"] == 1.0


def test_card_master_flag_single_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        cards, "build_master", lambda run_ids: ({"runs": list(run_ids)}, "# master\n")
    )

    result = runner.invoke(
        cli.app,
        ["card", "--run-id", "r", "--master", "--out", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "master.json").exists()
    assert (tmp_path / "master.md").exists()


def test_gmail_watch_retries_once_per_interval(monkeypatch):
    import time
    from unittest.mock import Mock, call

    from mailroom_reloaded.intake import gmail

    poll = Mock(side_effect=[RuntimeError("temporary"), ["doc"], KeyboardInterrupt])
    sleep = Mock()
    log = Mock()
    monkeypatch.setattr(gmail, "poll_and_ingest", poll)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(cli.logger, "exception", log)
    result = runner.invoke(cli.app, ["gmail", "watch", "--interval", "2"])
    assert result.exit_code == 0, result.output
    assert '"doc_ids": ["doc"]' in result.output
    assert sleep.call_args_list == [call(2), call(2)]
    log.assert_called_once_with("gmail_poll_failed")


def test_gmail_watch_auth_and_install_errors_are_fatal(monkeypatch):
    import time
    from unittest.mock import Mock

    from mailroom_reloaded.intake import gmail

    sleep = Mock()
    monkeypatch.setattr(time, "sleep", sleep)
    for error in (gmail.GmailAuthError, gmail.GmailNotInstalled):
        poll = Mock(side_effect=error("fatal"))
        monkeypatch.setattr(gmail, "poll_and_ingest", poll)
        result = runner.invoke(cli.app, ["gmail", "watch"])
        assert result.exit_code == 1
        assert "fatal" in result.output
        poll.assert_called_once()
    sleep.assert_not_called()


@pytest.fixture
def cli_runner(monkeypatch):
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
    cli_runner, monkeypatch, watch, expected
):
    import os

    import uvicorn

    from mailroom_reloaded.api.app import app

    monkeypatch.setenv("MAILROOM_API_HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "invalid")
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "old")
    serve = Mock()
    monkeypatch.setattr(uvicorn, "run", serve)

    result = cli_runner.invoke(
        cli.app, ["serve", "--host", "::1", "--port", "0", watch]
    )

    assert result.exit_code == 0, result.output
    serve.assert_called_once_with(app, host="::1", port=0)
    assert os.environ["MAILROOM_EMBED_WATCHER"] == expected


def test_serve_refuses_unprotected_public_bind_before_starting(cli_runner, monkeypatch):
    import os

    import uvicorn

    serve = Mock()
    monkeypatch.setattr(uvicorn, "run", serve)
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "unchanged")

    result = cli_runner.invoke(cli.app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 1
    assert "Refusing to bind" in result.output
    serve.assert_not_called()
    assert os.environ["MAILROOM_EMBED_WATCHER"] == "unchanged"


@pytest.mark.parametrize("doc_type", [None, "correspondence"])
def test_run_prints_machine_readable_summary(
    cli_runner, tmp_path, monkeypatch, doc_type
):
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

    result = cli_runner.invoke(cli.app, ["run", str(path), "--worker-id", "worker-2"])

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
    cli_runner, tmp_path, monkeypatch, existing_directory
):
    from mailroom_reloaded.pipeline import flow

    run = Mock()
    monkeypatch.setattr(flow, "run_document", run)
    path = tmp_path if existing_directory else tmp_path / "missing.txt"
    result = cli_runner.invoke(cli.app, ["run", str(path)])
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
    cli_runner, tmp_path, monkeypatch, classes, expected
):
    from mailroom_reloaded.eval import runner as eval_runner

    run = Mock(return_value="run-123")
    monkeypatch.setattr(eval_runner, "run_eval", run)
    result = cli_runner.invoke(
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
def test_eval_rejects_out_of_range_options(cli_runner, monkeypatch, option, value):
    from mailroom_reloaded.eval import runner as eval_runner

    run = Mock()
    monkeypatch.setattr(eval_runner, "run_eval", run)
    result = cli_runner.invoke(cli.app, ["eval", option, value])
    assert result.exit_code == 2
    run.assert_not_called()


@pytest.mark.parametrize("calibration", [False, True])
def test_train_gate_loads_jsonl_and_selects_trainer(
    cli_runner, tmp_path, monkeypatch, calibration
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
    result = cli_runner.invoke(
        cli.app, args + (["--calibration"] if calibration else [])
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"output": str(out), "rows": 2}
    fit.assert_called_once_with([{"confidence": 0.9}, {"confidence": 0.2}], out)
    other.assert_not_called()


def test_train_gate_malformed_json_does_not_call_trainer(
    cli_runner, tmp_path, monkeypatch
):
    from mailroom_reloaded.eval import train_gate

    rows = tmp_path / "rows.jsonl"
    rows.write_text('{"confidence": 0.9}\ninvalid\n')
    fit = Mock()
    monkeypatch.setattr(train_gate, "train_gate", fit)
    result = cli_runner.invoke(cli.app, ["train-gate", "--rows", str(rows)])
    assert result.exit_code != 0
    assert isinstance(result.exception, json.JSONDecodeError)
    fit.assert_not_called()


# --- mailroom replay (R-13) -------------------------------------------------------------


def _otlp_file(path, run: str = "run-cli-1") -> None:
    t0 = 1_760_000_000 * 10**9

    def kv(k: str, v: str) -> dict:
        return {"key": k, "value": {"stringValue": v}}

    spans = [
        {
            "traceId": "cd" * 16,
            "spanId": f"{i:016x}",
            "name": name,
            "startTimeUnixNano": str(t0 + i * 10**9),
            "endTimeUnixNano": str(t0 + (i + 1) * 10**9),
            "attributes": [kv("mailroom.run_id", run), kv("mailroom.doc_id", "d1"), kv(
                "llm.input_messages.0.message.content", "SECRET")],
        }
        for i, name in ((1, "mailroom.document"), (2, "mailroom.node.sorter"))
    ]
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def replay_env(tmp_path, monkeypatch):
    """Point settings and the default engine at ``tmp_path`` for one test."""
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    from mailroom_reloaded import settings
    from mailroom_reloaded.obs.replay.timeline import clear_cache
    from mailroom_reloaded.storage import db

    settings.get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    clear_cache()
    try:
        yield tmp_path
    finally:
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()
        clear_cache()


def test_replay_import_export_sessions(replay_env) -> None:
    src = replay_env / "in.json"
    _otlp_file(src)
    res = runner.invoke(cli.app, ["replay", "import", str(src)])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == {
        "parsed": 2, "stored": 2, "skipped": 0, "runs": ["run-cli-1"], "sessions": []
    }
    # a second import stores nothing new
    refused = runner.invoke(cli.app, ["replay", "import", str(src)])
    assert refused.exit_code == 2 and "--append" in refused.output
    again = runner.invoke(cli.app, ["replay", "import", str(src), "--append"])
    assert json.loads(again.stdout)["stored"] == 0
    assert json.loads(again.stdout)["skipped"] == 2
    assert "warning: 2 of 2 spans were not stored" in again.output
    assert "SECRET" not in (replay_env / "traces.db").read_bytes().decode("latin-1")

    out = replay_env / "tl.json"
    res = runner.invoke(cli.app, ["replay", "export", "run:run-cli-1", "-o", str(out)])
    assert res.exit_code == 0, res.output
    assert json.loads(out.read_text())["version"] == "replay/v1"
    res = runner.invoke(cli.app, ["replay", "export", "run-cli-1"])
    assert json.loads(res.stdout)["session"]["id"] == "run:run-cli-1"

    res = runner.invoke(cli.app, ["replay", "sessions", "--json", "--limit", "5"])
    assert res.exit_code == 0, res.output
    assert [s["id"] for s in json.loads(res.stdout)["sessions"]] == ["run:run-cli-1"]
    res = runner.invoke(cli.app, ["replay", "sessions"])
    assert "run:run-cli-1" in res.stdout


def test_replay_import_run_id_override(replay_env) -> None:
    src = replay_env / "in.json"
    _otlp_file(src)
    res = runner.invoke(cli.app, ["replay", "import", str(src), "--run-id", "other-1"])
    assert json.loads(res.stdout)["runs"] == ["other-1"]


def test_replay_errors(replay_env) -> None:
    bad = replay_env / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    res = runner.invoke(cli.app, ["replay", "import", str(bad)])
    assert res.exit_code == 2 and "error:" in res.output
    assert "Traceback" not in res.output
    res = runner.invoke(cli.app, ["replay", "import", str(replay_env / "missing.json")])
    assert res.exit_code != 0
    res = runner.invoke(cli.app, ["replay", "import", str(bad), "--run-id", "bad id"])
    assert res.exit_code == 2 and "not a valid id" in res.output
    res = runner.invoke(cli.app, ["replay", "export", "bad id"])
    assert res.exit_code == 2 and "invalid session id" in res.output
    res = runner.invoke(cli.app, ["replay", "export", "run:nothing-here"])
    assert res.exit_code == 1 and "no timeline" in res.output
    res = runner.invoke(cli.app, ["replay", "sessions", "--limit", "0"])
    assert res.exit_code != 0
    res = runner.invoke(cli.app, ["replay", "sessions"])
    assert res.exit_code == 0 and "no sessions" in res.stdout


def test_replay_pruned_run(replay_env) -> None:
    from mailroom_reloaded.storage.db import get_engine
    from mailroom_reloaded.storage.ledger import Ledger
    from mailroom_reloaded.storage.retention import read_pruned_run_ids

    src = replay_env / "in.json"
    _otlp_file(src)
    assert runner.invoke(cli.app, ["replay", "import", str(src)]).exit_code == 0
    ledger = Ledger(get_engine())
    payload = {"target": "run-cli-1", "digest": "x", "counts": {"spans": 2}}
    assert ledger.append("pruned", "run-cli-1", payload=payload)
    ledger.flush()
    assert "run-cli-1" in read_pruned_run_ids()

    res = runner.invoke(cli.app, ["replay", "import", str(src), "--append"])
    assert res.exit_code == 2 and "pruned" in res.output
    # spans still present: export succeeds and carries the marker
    res = runner.invoke(cli.app, ["replay", "export", "run:run-cli-1"])
    assert json.loads(res.stdout)["session"]["data_pruned"] is True
    res = runner.invoke(cli.app, ["replay", "sessions", "--json"])
    assert json.loads(res.stdout)["sessions"][0]["data_pruned"] is True
    # spans gone: exit 3
    from mailroom_reloaded.storage.span_store import SpanStore, default_span_store_path

    st = SpanStore(default_span_store_path())
    st.delete_runs(["run-cli-1"])
    st.close()
    res = runner.invoke(cli.app, ["replay", "export", "run:run-cli-1"])
    assert res.exit_code == 3 and "data pruned" in res.output


def test_replay_import_refuses_showcase(replay_env) -> None:
    src = replay_env / "in.json"
    _otlp_file(src)
    res = runner.invoke(cli.app, ["replay", "import", str(src), "--run-id", "showcase-clean"])
    assert res.exit_code == 2 and "showcase" in res.output
