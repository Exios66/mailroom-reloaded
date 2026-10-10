"""Quickstart CLI boundaries: dotenv precedence, redaction, and health URLs."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def quickstart(tmp_path):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/quickstart.sh", project / "scripts/quickstart.sh")
    shutil.copy(ROOT / ".env.example", project / ".env.example")
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / ".env").write_text("DEFAULT_PROVIDER=wrong-caller-provider\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("docker", "curl"):
        stub = bin_dir / name
        stub.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "if '--env-file' in sys.argv:\n"
            "    env_file = Path(sys.argv[sys.argv.index('--env-file') + 1])\n"
            "    if not env_file.exists():\n"
            "        sys.exit('Environment file does not exist')\n"
            "record = {'args': sys.argv[1:]}\n"
            "record['overrides'] = {key: os.environ.get(key) for key in "
            "('DEFAULT_PROVIDER', 'MAILROOM_API_TOKEN')}\n"
            f"with (Path(os.environ['TEST_ROOT']) / '{name}-calls').open('a') as log:\n"
            "    log.write(json.dumps(record) + '\\n')\n"
        )
        stub.chmod(0o755)

    def run(*args, extra_env=None, input=None):
        return subprocess.run(
            ["bash", str(project / "scripts/quickstart.sh"), *args],
            cwd=caller,
            env={
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "TEST_ROOT": str(tmp_path),
                **(extra_env or {}),
            },
            input=input, capture_output=True, text=True, check=False,
        )

    def calls(name="docker"):
        return [json.loads(line) for line in (tmp_path / f"{name}-calls").read_text().splitlines()]

    return project, caller, run, calls


def test_new_env_defaults_and_build_from_other_directory(quickstart):
    project, _, run, calls = quickstart
    result = run("up")
    assert result.returncode == 0, result.stderr
    values = dotenv_values(project / ".env", interpolate=False)
    assert values["DEFAULT_PROVIDER"] == "mock"
    assert values["MAILROOM_API_TOKEN"].startswith("mailroom-dev-token-")
    assert values["GRAFANA_ADMIN_PASSWORD"] == "admin"
    assert values["MAILROOM_API_TOKEN"] not in result.stdout + result.stderr
    assert calls()[0]["args"] == [
        "compose", "--env-file", str(project / ".env"),
        "-f", str(project / "deploy/docker-compose.dev.yml"), "up", "-d", "--build",
    ]
    assert calls()[1]["args"][-1] == "ps"


def test_new_env_persists_literal_options_without_executing_or_printing(quickstart):
    project, caller, run, calls = quickstart
    token = "synthetic 'quoted' \\ $LITERAL $(touch injected) `touch injected` # / &"
    result = run("up", "--provider", "vllm", "--token", token, "--no-build")
    assert result.returncode == 0, result.stderr
    values = dotenv_values(project / ".env", interpolate=False)
    assert values["DEFAULT_PROVIDER"] == "vllm"
    assert values["MAILROOM_API_TOKEN"] == token.replace("$", "$$")
    assert token not in result.stdout + result.stderr
    assert not (caller / "injected").exists()
    assert not (project / "injected").exists()
    assert "--build" not in calls()[0]["args"]
    assert calls()[0]["overrides"] == {
        "DEFAULT_PROVIDER": "vllm", "MAILROOM_API_TOKEN": token,
    }
    # A later invocation retains the initialized identity without explicit overrides.
    assert run("up", "--no-build").returncode == 0
    assert dotenv_values(project / ".env", interpolate=False) == values
    assert calls()[2]["overrides"] == {"DEFAULT_PROVIDER": None, "MAILROOM_API_TOKEN": None}


@pytest.mark.parametrize("options, overrides", [
    ([], {"DEFAULT_PROVIDER": None, "MAILROOM_API_TOKEN": None}),
    (["--provider", "vllm"], {"DEFAULT_PROVIDER": "vllm", "MAILROOM_API_TOKEN": None}),
    (["--token", "new $synthetic 'token'"], {
        "DEFAULT_PROVIDER": None, "MAILROOM_API_TOKEN": "new $synthetic 'token'",
    }),
    (["--provider", "mock", "--token", "new-synthetic"], {
        "DEFAULT_PROVIDER": "mock", "MAILROOM_API_TOKEN": "new-synthetic",
    }),
])
def test_existing_env_keeps_unspecified_settings_and_applies_options(quickstart, options, overrides):
    project, _, run, calls = quickstart
    original = "DEFAULT_PROVIDER=openrouter\nMAILROOM_API_TOKEN=old-synthetic\nUNRELATED=keep-me\n"
    (project / ".env").write_text(original)
    result = run("up", *options)
    assert result.returncode == 0, result.stderr
    assert (project / ".env").read_text() == original
    assert all(call["overrides"] == overrides for call in calls())
    assert "old-synthetic" not in result.stdout + result.stderr
    if overrides["MAILROOM_API_TOKEN"]:
        assert overrides["MAILROOM_API_TOKEN"] not in result.stdout + result.stderr


@pytest.mark.parametrize("command", ["down", "logs", "status", "reset"])
@pytest.mark.parametrize("env_exists", [True, False])
def test_all_compose_commands_use_project_env(quickstart, command, env_exists):
    project, _, run, calls = quickstart
    if env_exists:
        (project / ".env").write_text("MAILROOM_API_TOKEN=synthetic\n")
    result = run(command, input="yes\n" if command == "reset" else None)
    assert result.returncode == 0, result.stderr
    env_file = str(project / ".env") if env_exists else "/dev/null"
    expected_command = {
        "down": ["down"], "logs": ["logs", "-f", "app"],
        "status": ["ps"], "reset": ["down", "-v"],
    }[command]
    assert calls()[0]["args"] == [
        "compose", "--env-file", env_file,
        "-f", str(project / "deploy/docker-compose.dev.yml"), *expected_command,
    ]
    if not env_exists or command == "reset":
        assert not (project / ".env").exists()
    if command == "status":
        assert [call["args"] for call in calls("curl")] == [
            ["-s", "http://127.0.0.1:8000/health"],
            ["-s", "http://127.0.0.1:6006"],
            ["-s", "http://127.0.0.1:9090/-/healthy"],
            ["-s", "http://127.0.0.1:3000/api/health"],
        ]


def test_generated_env_roundtrips_through_compose(quickstart):
    docker = shutil.which("docker")
    if not docker or subprocess.run(
        [docker, "compose", "version"], capture_output=True, check=False,
    ).returncode:
        pytest.skip("Docker Compose plugin is unavailable")
    project, _, run, _ = quickstart
    token = "synthetic 'quoted' \\ $LITERAL $(touch injected) `touch injected` # \"quote\" end\\"
    result = run("up", "--provider", "llamafile", "--token", token)
    assert result.returncode == 0, result.stderr
    compose_file = project / "compose.yaml"
    compose_file.write_text(
        "services:\n  app:\n    image: alpine:3.23\n    environment:\n"
        "      DEFAULT_PROVIDER: ${DEFAULT_PROVIDER}\n"
        "      MAILROOM_API_TOKEN: ${MAILROOM_API_TOKEN}\n"
    )
    result = subprocess.run(
        [docker, "compose", "--env-file", str(project / ".env"),
         "-f", str(compose_file), "config", "--environment"],
        env={key: value for key, value in os.environ.items()
             if key not in {"MAILROOM_API_TOKEN", "DEFAULT_PROVIDER"}},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    # --environment returns the parsed values; config JSON escapes dollar signs again.
    settings = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert settings["MAILROOM_API_TOKEN"] == token
    assert settings["DEFAULT_PROVIDER"] == "llamafile"
