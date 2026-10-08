"""CLI surface tests (plan Task 19 / Task 21).

The heavy machinery lives in the modules under test; these pin the command
wiring so a broken option name or output path fails loudly.
"""

from __future__ import annotations

import json

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
