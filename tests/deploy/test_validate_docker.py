"""Exercise validation exit codes and Compose selection without a Docker daemon."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    "docker-compose.yml", "docker-compose.dev.yml", "docker-compose.sandbox.yml",
)
DOCKERFILES = ("Dockerfile", "Dockerfile.dev", "Dockerfile.sandbox")


@pytest.fixture
def validator(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/validate-docker.sh", tmp_path / "scripts")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name in DOCKERFILES:
        (deploy / name).write_text("FROM scratch\nCOPY . /app\n")
    for name in COMPOSE_FILES:
        shutil.copy(ROOT / "deploy" / name, deploy / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Keep the caller's installed Docker/Compose out of the fixture's PATH.
    for name in ("dirname", "basename", "awk", "tr", "grep"):
        (bin_dir / name).symlink_to(shutil.which(name))
    fake = f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
interface = Path(sys.argv[0]).name
with Path(os.environ["CALLS"]).open("a") as stream:
    stream.write(json.dumps({"interface": interface, "args": args}) + "\\n")
if args == ["--version"]:
    print("Docker version 27.0.0, build fake")
    sys.exit(0)
if interface == "docker":
    if os.environ["MODE"] != "plugin":
        sys.exit(1)
    assert args.pop(0) == "compose"
elif os.environ["MODE"] != "standalone":
    sys.exit(1)
if args[0] == "version":
    print("2.29.0")
    sys.exit(0)
assert args[0] == "-f"
assert args[2:] == ["config", "--quiet"]
name = Path(args[1]).name
if name == "docker-compose.yml":
    assert os.environ.get("MAILROOM_API_TOKEN")
    assert os.environ.get("GRAFANA_ADMIN_PASSWORD")
if name == os.environ.get("FAIL_CONFIG"):
    print("synthetic-secret-must-not-leak")
    print("synthetic-secret-must-not-leak", file=sys.stderr)
    sys.exit(1)
'''
    for name in ("docker", "docker-compose"):
        executable = bin_dir / name
        executable.write_text(fake)
        executable.chmod(0o755)

    def run(mode="plugin", fail_config=""):
        calls_path = tmp_path / "calls.jsonl"
        result = subprocess.run(
            [shutil.which("bash"), str(tmp_path / "scripts/validate-docker.sh")],
            cwd=tmp_path,
            env={
                "PATH": str(bin_dir), "CALLS": str(calls_path),
                "MODE": mode, "FAIL_CONFIG": fail_config,
                "MAILROOM_API_TOKEN": "", "GRAFANA_ADMIN_PASSWORD": "",
            },
            capture_output=True, text=True, check=False,
        )
        calls = (
            [json.loads(line) for line in calls_path.read_text().splitlines()]
            if calls_path.exists() else []
        )
        return result, calls

    return tmp_path, run


@pytest.mark.parametrize("mode", ["plugin", "standalone"])
def test_selects_working_compose_for_all_config_checks(validator, mode):
    _, run = validator
    result, calls = run(mode=mode)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All validations passed!" in result.stdout
    assert "2.29.0" in result.stdout
    config_calls = [call for call in calls if "config" in call["args"]]
    assert [Path(call["args"][-3]).name for call in config_calls] == list(COMPOSE_FILES)
    expected_interface = "docker" if mode == "plugin" else "docker-compose"
    assert all(call["interface"] == expected_interface for call in config_calls)
    assert any(
        call["interface"] == expected_interface and call["args"][-2:] == ["version", "--short"]
        for call in calls
    )
    if mode == "plugin":
        assert all(call["interface"] == "docker" for call in calls)


def test_missing_compose_is_failure(validator):
    _, run = validator
    result, calls = run(mode="missing")
    assert result.returncode != 0
    assert "Docker Compose not found" in result.stdout
    assert "All validations passed!" not in result.stdout
    assert not any("config" in call["args"] for call in calls)


def test_missing_docker_is_failure(validator):
    root, run = validator
    (root / "bin/docker").unlink()
    result, _ = run()
    assert result.returncode != 0
    assert "Docker not found" in result.stdout
    assert "All validations passed!" not in result.stdout


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_config_failure_is_reported_without_secrets_and_checks_continue(validator, name):
    _, run = validator
    result, calls = run(fail_config=name)
    assert result.returncode != 0
    assert f"✗ {name}" in result.stdout
    assert "All validations passed!" not in result.stdout
    assert "synthetic-secret-must-not-leak" not in result.stdout + result.stderr
    assert len([call for call in calls if "config" in call["args"]]) == 3


@pytest.mark.parametrize("name", DOCKERFILES)
def test_invalid_dockerfile_fails_and_compose_checks_continue(validator, name):
    root, run = validator
    (root / "deploy" / name).write_text("# Required directives absent\n")
    result, calls = run()
    assert result.returncode != 0
    assert f"✗ {name}" in result.stdout
    assert "All validations passed!" not in result.stdout
    assert len([call for call in calls if "config" in call["args"]]) == 3


@pytest.mark.parametrize("name", (*DOCKERFILES, *COMPOSE_FILES))
def test_missing_required_file_fails(validator, name):
    root, run = validator
    (root / "deploy" / name).unlink()
    result, _ = run()
    assert result.returncode != 0
    assert f"✗ {name}" in result.stdout
    assert "missing file" in result.stdout
    assert "All validations passed!" not in result.stdout
