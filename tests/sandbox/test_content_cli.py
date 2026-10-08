"""Command behavior with local fixtures and mocked download/export boundaries."""

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from mailroom_reloaded import cli
from mailroom_reloaded.sandbox.content import cli as content_cli
from mailroom_reloaded.sandbox.content.loader import SMOKE_DIR, load_content
from mailroom_reloaded.sandbox.content.lock import ContentLock, sha256_file

runner = CliRunner()


def invoke(*args):
    return runner.invoke(cli.app, ["sandbox", "content", *map(str, args)])


@pytest.mark.parametrize("sources", [
    [], ["--from-dir", "content", "--from-bundle", "bundle"],
    ["--from-dir", "content", "--url", "https://example.invalid/bundle"],
    ["--from-bundle", "bundle", "--url", "https://example.invalid/bundle"],
    ["--from-dir", "content", "--from-bundle", "bundle", "--url", "https://example.invalid/a"],
])
def test_pull_requires_exactly_one_source(tmp_path, sources):
    dest = tmp_path / "out"
    result = invoke("pull", *sources, "--dest", dest)
    assert result.exit_code == 1
    assert "give exactly one of" in result.stderr
    assert not dest.exists()


def test_pull_from_directory_replaces_destination(content_dir, lock_path, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "stale").write_text("old")
    result = invoke("pull", "--from-dir", content_dir, "--lock", lock_path, "--dest", dest)
    assert result.exit_code == 0, result.output
    assert "pulled v0.5.0" in result.stdout
    assert not (dest / "stale").exists()
    expected = {p.relative_to(content_dir): p.read_bytes() for p in content_dir.rglob("*") if p.is_file()}
    assert {p.relative_to(dest): p.read_bytes() for p in dest.rglob("*") if p.is_file()} == expected


@pytest.mark.parametrize("metadata,message", [
    ({"version": "0.6.0", "schema_version": "2.0"}, "lock pins v0.5.0"),
    ({"version": "0.5.0", "schema_version": "3.0"}, "schema major"),
    (None, "content.json"),
])
def test_pull_rejection_preserves_existing_destination(content_dir, lock_path, tmp_path, metadata, message):
    path = content_dir / "content.json"
    if metadata is None:
        path.unlink()
    else:
        path.write_text(json.dumps(metadata))
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep").write_text("existing content")
    result = invoke("pull", "--from-dir", content_dir, "--lock", lock_path, "--dest", dest)
    assert result.exit_code == 1
    assert message in result.stderr
    assert [p.name for p in dest.iterdir()] == ["keep"]
    assert (dest / "keep").read_text() == "existing content"


def test_url_pull_without_opt_in_never_calls_fetch(monkeypatch, lock_path, tmp_path):
    fetch = Mock(side_effect=AssertionError("network access attempted"))
    monkeypatch.setattr(content_cli.bundle_mod, "fetch_url", fetch)
    dest = tmp_path / "out"
    result = invoke("pull", "--url", "https://example.invalid/bundle", "--lock", lock_path, "--dest", dest)
    assert result.exit_code == 1
    assert "--url requires --allow-network" in result.stderr
    fetch.assert_not_called()
    assert not dest.exists()


@pytest.mark.parametrize("tamper", [False, True], ids=["valid", "digest-mismatch"])
def test_opted_in_download_is_verified_before_extraction(
    monkeypatch, make_bundle, content_lock, lock_path, tmp_path, tamper,
):
    meta = {"version": "0.5.0", "schema_version": "2.0"}
    archive = make_bundle([("content.json", json.dumps(meta).encode())])
    replace(content_lock, bundle_sha256=sha256_file(archive)).write(lock_path)
    payload = archive.read_bytes() + (b"tampered" if tamper else b"")
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    monkeypatch.setattr(content_cli.tempfile, "mkdtemp", lambda: str(download_dir))

    def fetch(url, out):
        assert url == "https://example.invalid/bundle"
        out.write_bytes(payload)
        return out

    fetch_mock = Mock(side_effect=fetch)
    monkeypatch.setattr(content_cli.bundle_mod, "fetch_url", fetch_mock)
    dest = tmp_path / "out"
    result = invoke("pull", "--url", "https://example.invalid/bundle", "--allow-network",
                    "--lock", lock_path, "--dest", dest)
    fetch_mock.assert_called_once()
    if tamper:
        assert result.exit_code == 1
        assert "does not match lock" in result.stderr
        assert not dest.exists()
    else:
        assert result.exit_code == 0, result.output
        assert json.loads((dest / "content.json").read_text()) == meta


def test_validate_reports_schema_diagnostics(content_dir):
    (content_dir / "gen/specs/vendor.yaml").write_text("{}")
    result = invoke("validate", content_dir)
    assert result.exit_code == 1
    assert "gen/specs/vendor.yaml: <root>:" in result.stderr
    assert "required property" in result.stderr
    assert "ok:" not in result.stdout


@pytest.mark.parametrize("command", ["validate", "build"])
def test_missing_content_produces_cli_error(tmp_path, command):
    path = tmp_path / "missing"
    args = [path] if command == "validate" else ["--from-dir", path]
    result = invoke(command, *args)
    assert result.exit_code == 1
    assert "neither content.json nor manifest.json" in result.stderr


def test_status_reports_mismatched_smoke_version(content_lock, lock_path):
    replace(content_lock, tag="v9.9.9").write(lock_path)
    result = invoke("status", "--lock", lock_path)
    assert result.exit_code == 0, result.output
    status = json.loads(result.stdout)
    assert status["lock"]["repo"] == content_lock.repo
    assert status["smoke_matches_lock"] is False
    assert status["smoke_valid"] is True
    assert status["smoke_scenarios"] == 6


def test_status_missing_lock_is_actionable(tmp_path):
    result = invoke("status", "--lock", tmp_path / "missing")
    assert result.exit_code == 1
    assert "cannot read lock" in result.stderr


@pytest.mark.parametrize("failure", ["invalid-content", "missing-exporter"])
def test_build_preconditions_do_not_run_exporter_or_replace_output(
    monkeypatch, content_dir, tmp_path, failure,
):
    if failure == "invalid-content":
        (content_dir / "dist/registry.yaml").unlink()
    run = Mock(side_effect=AssertionError("exporter must not run"))
    monkeypatch.setattr(content_cli.subprocess, "run", run)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep").write_text("existing")
    result = invoke("build", "--from-dir", content_dir, "--out", out)
    assert result.exit_code == 1
    expected = "missing registry" if failure == "invalid-content" else "export_smoke.py not found"
    assert expected in result.stderr
    run.assert_not_called()
    assert (out / "keep").read_text() == "existing"


@pytest.mark.parametrize("outcome", ["success", "stderr", "stdout", "invalid-smoke"])
def test_build_validates_export_before_replacing_output(monkeypatch, content_dir, tmp_path, outcome):
    tool = content_dir / "tools/export_smoke.py"
    tool.parent.mkdir()
    tool.touch()
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep").write_text("existing")
    staged_paths = []

    def run(argv, **kwargs):
        assert argv[:5] == [sys.executable, "-I", str(tool), "--root", str(content_dir)]
        assert argv[5] == "--out"
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        staged = Path(argv[6])
        staged_paths.append(staged)
        assert (out / "keep").read_text() == "existing"
        if outcome in {"stderr", "stdout"}:
            output = {"stdout": "", "stderr": "", outcome: "export failed"}
            return subprocess.CompletedProcess(argv, 1, **output)
        shutil.copytree(SMOKE_DIR, staged)
        if outcome == "invalid-smoke":
            (staged / "templates/status_inquiry.j2").write_text("tampered")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    run_mock = Mock(side_effect=run)
    monkeypatch.setattr(content_cli.subprocess, "run", run_mock)
    result = invoke("build", "--from-dir", content_dir, "--out", out)
    run_mock.assert_called_once()
    assert not staged_paths[0].parent.exists()
    if outcome == "success":
        assert result.exit_code == 0, result.output
        assert "smoke fixtures written" in result.stdout
        assert not (out / "keep").exists()
        assert load_content(out, strict=True).kind == "smoke"
    else:
        assert result.exit_code == 1
        expected = "exported smoke set invalid" if outcome == "invalid-smoke" else "export failed"
        assert expected in result.stderr
        assert [p.name for p in out.iterdir()] == ["keep"]
        assert (out / "keep").read_text() == "existing"


def test_bump_preserves_repository_and_pins_bundle_metadata(make_bundle, content_lock, lock_path):
    meta = {"version": "0.6.0", "schema_version": "2.1", "dataset_revision": "next"}
    archive = make_bundle([("content.json", json.dumps(meta).encode())])
    result = invoke("bump", "--bundle", archive, "--tag", "v0.6.0", "--commit", "1234567",
                    "--lock", lock_path)
    assert result.exit_code == 0, result.output
    assert ContentLock.read(lock_path) == ContentLock(
        content_lock.repo, "v0.6.0", "1234567", sha256_file(archive), "2.1", "next",
    )


@pytest.mark.parametrize("meta,message", [
    ({"schema_version": "3.0", "dataset_revision": "next"}, "schema major"),
    ({"schema_version": "2.0"}, "dataset_revision"),
])
def test_failed_bump_preserves_existing_lock(make_bundle, lock_path, meta, message):
    original = lock_path.read_bytes()
    archive = make_bundle([("content.json", json.dumps(meta).encode())])
    result = invoke("bump", "--bundle", archive, "--tag", "v0.6.0", "--commit", "1234567",
                    "--lock", lock_path)
    assert result.exit_code == 1
    assert message in result.stderr
    assert lock_path.read_bytes() == original
