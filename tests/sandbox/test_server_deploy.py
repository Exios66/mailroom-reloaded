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
    return yaml.safe_load(COMPOSE.read_text())["services"]["sandbox"]


def test_compose_single_offline_service_published_on_loopback_by_default():
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
    text = (ROOT / "deploy" / "Dockerfile.sandbox").read_text()
    assert "--extra sandbox" in text and "--extra dev" not in text
    assert "USER app" in text and "sandbox" in text.split("CMD")[-1]


def test_script_is_executable_and_parses():
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
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
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
    res = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    if "is not a docker command" in res.stderr or "unknown shorthand" in res.stderr:
        pytest.skip("docker compose plugin not available")
    assert res.returncode == 0, res.stderr
