"""Static checks of the sandbox deploy files (no daemon needed)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "docker-compose.sandbox.yml"
SCRIPT = ROOT / "scripts" / "sandbox.sh"


def _svc() -> dict:
    """Read the sandbox service definition from its Compose configuration."""
    return yaml.safe_load(COMPOSE.read_text())["services"]["sandbox"]


def test_compose_single_offline_service_published_on_loopback_by_default():
    """Verify Compose defaults to one restricted sandbox service bound to loopback."""
    doc = yaml.safe_load(COMPOSE.read_text())
    assert list(doc["services"]) == ["sandbox"]
    svc = _svc()
    assert svc["ports"] == ["${SANDBOX_BIND:-127.0.0.1}:${SANDBOX_PORT:-8100}:8100"]
    cmd = " ".join(str(c) for c in svc["command"])
    assert "sandbox serve" in cmd.replace("\n", " ") or svc["command"][:3] == [
        "mailroom",
        "sandbox",
        "serve",
    ]
    assert "privileged" not in svc and svc["cap_drop"] == ["ALL"]
    assert svc["healthcheck"]["test"][0] == "CMD"
    env = svc["environment"]
    assert env["MAILROOM_ALLOW_UNAUTHENTICATED_BIND"] == "${SANDBOX_ALLOW_UNAUTH:-1}"
    # no provider key or external endpoint is wired into the container
    assert not any(
        k in env for k in ("OPENROUTER_API_KEY", "HF_TOKEN", "AGENTMAIL_API_KEY")
    )


def test_dockerfile_builds_the_sandbox_extra_only():
    """Verify the image installs the sandbox extra and uses an unprivileged user."""
    text = (ROOT / "deploy" / "Dockerfile.sandbox").read_text()
    assert "--extra sandbox" in text and "--extra dev" not in text
    assert "USER app" in text and "sandbox" in text.split("CMD")[-1]


def test_script_is_executable_and_parses():
    """Verify shell syntax, executable permissions, help text, and invalid-command exit."""
    assert os.access(SCRIPT, os.X_OK)
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    out = subprocess.run(
        [str(SCRIPT), "--help"], capture_output=True, text=True, check=True
    ).stdout
    for word in ("up", "down", "status", "logs", "run"):
        assert f"scripts/sandbox.sh {word}" in out
    bad = subprocess.run(
        [str(SCRIPT), "nope"], capture_output=True, text=True, check=False
    )
    assert bad.returncode == 2


def test_expose_without_token_refuses(monkeypatch):
    """Verify explicit exposure fails when no API token is configured."""
    env = {k: v for k, v in os.environ.items() if k != "MAILROOM_API_TOKEN"}
    res = subprocess.run(
        [str(SCRIPT), "up", "--expose"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res.returncode != 0 and "MAILROOM_API_TOKEN" in res.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_compose_config_validates():
    """Validate Compose interpolation when the Docker Compose CLI is available."""
    res = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    if "is not a docker command" in res.stderr or "unknown shorthand" in res.stderr:
        pytest.skip("docker compose plugin not available")
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize(
    ("bind", "expose", "token", "allowed"),
    [
        (None, False, "", True),
        ("127.0.0.1", False, "", True),
        ("127.10.20.30", False, "", True),
        ("[::1]", False, "", True),
        ("0.0.0.0", False, "", False),
        ("0.0.0.0", False, "test-token", False),
        ("192.0.2.1", False, "test-token", False),
        ("::", False, "test-token", False),
        ("127.0.0.999", False, "", False),
        ("0.0.0.0", True, "", False),
        ("0.0.0.0", True, "   ", False),
        (None, True, "test-token", True),
        ("192.0.2.1", True, "test-token", True),
    ],
)
def test_startup_bind_policy(tmp_path, bind, expose, token, allowed):
    """Verify bind-address and exposure rules before a stubbed Docker launch."""
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        'if [[ "$*" == *"up -d --build"* ]]; then\n'
        '  echo "started:${SANDBOX_BIND:-127.0.0.1}:${SANDBOX_ALLOW_UNAUTH:-1}"\n'
        "fi\n"
    )
    docker.chmod(0o755)
    env = dict(
        os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", MAILROOM_API_TOKEN=token
    )
    env.pop("SANDBOX_BIND", None)
    # Explicit exposure must override an inherited opt-out.
    env["SANDBOX_ALLOW_UNAUTH"] = "1"
    if bind is not None:
        env["SANDBOX_BIND"] = bind
    res = subprocess.run(
        [str(SCRIPT), "up", *(["--expose"] if expose else [])],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert (res.returncode == 0) == allowed, res.stderr
    if allowed:
        expected_bind = bind or ("0.0.0.0" if expose else "127.0.0.1")
        assert f"started:{expected_bind}:{0 if expose else 1}" in res.stdout
    else:
        assert "started:" not in res.stdout
        assert "MAILROOM_API_TOKEN" in res.stderr
