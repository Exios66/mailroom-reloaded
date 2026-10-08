"""Static checks of the DEV topology (spec §9, docs/DEV_SERVER.md).

Mirrors tests/deploy/test_compose.py: pure YAML parsing, no daemon required.
Docker-backed `config -q` validation is skipped when the docker CLI (or its
compose plugin) is absent, so the suite stays hermetic on a bare checkout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
DEV_COMPOSE = DEPLOY / "docker-compose.dev.yml"
BASE_COMPOSE = DEPLOY / "docker-compose.yml"
DOCKERFILE_DEV = DEPLOY / "Dockerfile.dev"

#: Host ports documented in spec §9 that the dev stack must expose.
DEV_PORTS = {"8000", "6006", "9090", "3000"}


def _dev() -> dict:
    return yaml.safe_load(DEV_COMPOSE.read_text())


def _base() -> dict:
    return yaml.safe_load(BASE_COMPOSE.read_text())


def _host_ports(services: dict) -> dict[str, set[str]]:
    """Map service name -> host ports from short-syntax port strings."""
    out: dict[str, set[str]] = {}
    for name, svc in services.items():
        ports: set[str] = set()
        for entry in svc.get("ports", []) or []:
            text = entry if isinstance(entry, str) else str(entry.get("published", ""))
            text = text.split("/", 1)[0]
            parts = text.split(":")
            if len(parts) <= 2:
                ports.add(parts[0])
            else:
                ports.add(parts[-2])
        out[name] = ports
    return out


def test_dev_compose_services_and_profiles():
    svc = _dev()["services"]
    assert set(svc) == {
        "app",
        "mock",
        "data-init",
        "watcher",
        "otel-collector",
        "otel-targets",
        "phoenix",
        "prometheus",
        "grafana",
        "llamafile",
    }
    # The split watcher is always on in dev (not behind a profile).
    assert svc["watcher"].get("profiles") is None
    assert svc["llamafile"].get("profiles") == ["local-llm"]
    # No GPU services in the dev topology.
    assert "vllm" not in svc and "dcgm-exporter" not in svc


def test_dev_app_reload_and_build():
    svc = _dev()["services"]
    app = svc["app"]
    cmd = app["command"]
    cmd = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    assert "uvicorn" in cmd
    assert "mailroom_reloaded.api.app:app" in cmd
    assert "--reload" in cmd
    assert "--reload-dir /app/src" in cmd
    build = app["build"]
    assert build["context"] == ".."
    assert build["dockerfile"] == "deploy/Dockerfile.dev"
    assert DOCKERFILE_DEV.is_file()


def test_dev_watcher_split_and_data_volume_wired():
    svc = _dev()["services"]
    app_vols = svc["app"]["volumes"]
    watcher_vols = svc["watcher"]["volumes"]
    assert "../data:/data" in app_vols
    assert "../src:/app/src" in app_vols
    # The split watcher shares the exact same live data + code mounts.
    assert "../data:/data" in watcher_vols
    assert "../src:/app/src" in watcher_vols
    assert svc["watcher"]["command"] == ["mailroom", "watch"]
    # `app` must NOT also run an embedded watcher (no lock contention).
    assert svc["app"]["environment"]["MAILROOM_EMBED_WATCHER"] == "0"
    assert svc["watcher"]["environment"]["MAILROOM_EMBED_WATCHER"] == "0"
    assert svc["app"]["environment"]["MAILROOM_BASE_DIR"] == "/data"


def test_dev_defaults_mock_provider_and_no_gpu():
    svc = _dev()["services"]
    env = svc["app"]["environment"]
    assert env["DEFAULT_PROVIDER"] == "${DEFAULT_PROVIDER:-mock}"
    text = yaml.safe_dump(_dev()).lower()
    assert "nvidia" not in text
    assert "gpu" not in {p for s in svc.values() for p in (s.get("profiles") or [])}
    assert "deploy" not in svc["app"]


def test_dev_ports_loopback_unique_and_consistent_with_base():
    dev = _dev()["services"]
    dev_ports = _host_ports(dev)
    seen: set[str] = set()
    for name, ports in dev_ports.items():
        assert not (ports & seen), f"{name} reuses a host port: {ports & seen}"
        seen |= ports
        if ports:
            assert all(
                str(p).startswith("127.0.0.1:")
                for p in dev[name]["ports"]
            ), name
    assert DEV_PORTS <= seen
    # Dev binds only ports the base compose already declares, so the two
    # topologies never disagree about the documented host bindings.
    base_ports = _host_ports(_base()["services"])
    assert seen <= set().union(*base_ports.values())


def test_dev_observability_wiring_and_healthchecks():
    cfg = _dev()
    svc = cfg["services"]
    for name in ("app", "phoenix", "prometheus", "grafana"):
        assert svc[name].get("healthcheck"), name
    assert svc["watcher"]["healthcheck"] == {"disable": True}
    assert svc["app"]["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "http://otel-collector:4318"
    )
    collector = svc["otel-collector"]
    assert any(
        v.startswith("./otel-collector.dev.yaml:") for v in collector["volumes"]
    )
    assert (
        collector["depends_on"]["otel-targets"]["condition"]
        == "service_completed_successfully"
    )
    assert svc["phoenix"]["ports"] == ["127.0.0.1:6006:6006"]
    assert svc["prometheus"]["ports"] == ["127.0.0.1:9090:9090"]
    assert svc["grafana"]["ports"] == ["127.0.0.1:3000:3000"]
    assert svc["app"]["ports"] == ["127.0.0.1:8000:8000"]


@pytest.mark.parametrize(
    "profiles",
    [[], ["local-llm"]],
    ids=lambda p: "+".join(p) or "default",
)
def test_dev_compose_config_valid(profiles):
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    cmd = ["docker", "compose", "-f", str(DEV_COMPOSE)]
    for p in profiles:
        cmd += ["--profile", p]
    cmd += ["config", "-q"]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
        env={**os.environ},
    )
    if res.returncode != 0 and "unknown shorthand flag" in res.stderr + res.stdout:
        pytest.skip("docker compose plugin not available")
    assert res.returncode == 0, res.stderr


def test_dev_mock_and_state_ready_before_writers():
    services = _dev()["services"]
    assert "profiles" not in services["mock"]
    assert services["mock"]["healthcheck"]
    for name in ("app", "watcher"):
        svc = services[name]
        assert svc["environment"]["MOCK_BASE_URL"] == "${MOCK_BASE_URL:-http://mock:8000/v1}"
        assert svc["build"]["args"]["APP_UID"] == "${APP_UID:-1000}"
        assert svc["depends_on"]["mock"]["condition"] == "service_healthy"
        assert svc["depends_on"]["data-init"]["condition"] == "service_completed_successfully"
    assert services["data-init"]["volumes"] == ["../data:/data"]


def test_dev_collector_isolated_from_docker_metrics():
    collector = _dev()["services"]["otel-collector"]
    assert all("docker.sock" not in v for v in collector["volumes"])
    dev = yaml.safe_load((DEPLOY / "otel-collector.dev.yaml").read_text())
    base = yaml.safe_load((DEPLOY / "otel-collector.yaml").read_text())
    assert "docker_stats" not in dev["receivers"]
    assert "docker_stats" not in dev["service"]["pipelines"]["metrics"]["receivers"]
    assert "docker_stats" in base["receivers"]
    assert "docker_stats" in base["service"]["pipelines"]["metrics"]["receivers"]
