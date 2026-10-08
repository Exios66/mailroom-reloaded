"""Static checks of the Docker topology (spec §9) — no daemon required."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
COMPOSE = DEPLOY / "docker-compose.yml"


@pytest.mark.parametrize(
    "profiles",
    [[], ["local-llm"], ["gpu"], ["split-watcher"], ["local-llm", "gpu"]],
    ids=lambda p: "+".join(p) or "default",
)
def test_compose_config_valid(profiles):
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    cmd = ["docker", "compose", "-f", str(COMPOSE)]
    for p in profiles:
        cmd += ["--profile", p]
    cmd += ["config", "-q"]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=False)
    if res.returncode != 0 and "unknown shorthand flag" in res.stderr + res.stdout:
        pytest.skip("docker compose plugin not available")
    assert res.returncode == 0, res.stderr


def test_compose_services_profiles_and_volumes():
    cfg = yaml.safe_load(COMPOSE.read_text())
    svc = cfg["services"]
    expected = {
        "app": None,
        "otel-collector": None,
        "phoenix": None,
        "prometheus": None,
        "grafana": None,
        "watcher": ["split-watcher"],
        "llamafile": ["local-llm"],
        "vllm": ["gpu"],
        "dcgm-exporter": ["gpu"],
    }
    assert set(svc) == set(expected)
    for name, profiles in expected.items():
        assert svc[name].get("profiles") == profiles, name
    assert set(cfg["volumes"]) == {
        "mailroom_data",
        "hf_cache",
        "llamafile_models",
        "phoenix_data",
        "prometheus_data",
        "grafana_data",
    }
    cmd = svc["vllm"]["command"]
    vllm_cmd = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    assert "--enable-prefix-caching" in vllm_cmd
    assert "--kv-cache-dtype fp8" in vllm_cmd
    assert "--tensor-parallel-size ${VLLM_TP:-1}" in vllm_cmd
    assert svc["vllm"]["image"] == "vllm/vllm-openai:v0.29.0"
    assert "deploy" in svc["vllm"]


def test_collector_config_parses():
    cfg = yaml.safe_load((DEPLOY / "otel-collector.yaml").read_text())
    receivers, exporters = set(cfg["receivers"]), set(cfg["exporters"])
    processors = set(cfg["processors"])
    pipelines = cfg["service"]["pipelines"]
    assert set(pipelines) == {"traces", "metrics"}
    for name, pipe in pipelines.items():
        assert set(pipe["receivers"]) <= receivers, name
        assert set(pipe["processors"]) <= processors, name
        assert set(pipe["exporters"]) <= exporters, name
    assert pipelines["traces"]["exporters"] == ["otlphttp/phoenix"]
    assert (
        cfg["exporters"]["otlphttp/phoenix"]["traces_endpoint"]
        == "http://phoenix:6006/v1/traces"
    )
    assert {"otlp", "prometheus", "docker_stats"} <= set(
        pipelines["metrics"]["receivers"]
    )
    assert pipelines["metrics"]["exporters"] == ["prometheus"]
    assert cfg["exporters"]["prometheus"]["endpoint"].endswith(":8889")
    scrape = json.dumps(cfg["receivers"]["prometheus"])
    for target in ("vllm:8001", "dcgm-exporter:9400", "VLLM_METRICS_URLS"):
        assert target in scrape


def test_prometheus_scrapes_collector():
    cfg = yaml.safe_load((DEPLOY / "prometheus.yml").read_text())
    targets = [
        t
        for s in cfg["scrape_configs"]
        for c in s["static_configs"]
        for t in c["targets"]
    ]
    assert "otel-collector:8889" in targets


def _dashboards():
    return sorted((DEPLOY / "grafana" / "dashboards").glob("*.json"))


def _datasource_uids(node, acc=None):
    acc = set() if acc is None else acc
    if isinstance(node, dict):
        d = node.get("datasource")
        if isinstance(d, dict):
            acc.add(d.get("uid"))
        for v in node.values():
            _datasource_uids(v, acc)
    elif isinstance(node, list):
        for v in node:
            _datasource_uids(v, acc)
    return acc


def test_dashboards_valid_json_with_datasource_uid():
    ds = yaml.safe_load(
        next(
            (DEPLOY / "grafana" / "provisioning" / "datasources").glob("*.yaml")
        ).read_text()
    )
    uid = ds["datasources"][0]["uid"]
    files = _dashboards()
    assert {f.stem for f in files} == {"pipeline", "serving-gpu", "quality"}
    for f in files:
        dash = json.loads(f.read_text())
        assert dash["title"] and dash["panels"], f.name
        uids = _datasource_uids(dash)
        assert uids == {uid}, f.name
        names = {v["name"] for v in dash["templating"]["list"]}
        if f.stem in ("pipeline", "quality"):
            assert "run_id" in names, f.name
