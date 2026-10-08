"""Modal vLLM deploy config: pure, runs without the modal package."""

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "modal_vllm.py"
ENV_KEYS = (
    "MODAL_GPU",
    "MODAL_GPU_COUNT",
    "MODAL_MAX_CONTAINERS",
    "MODAL_SCALEDOWN_WINDOW",
    "VLLM_MODEL",
    "VLLM_MAX_LEN",
)


def _load():
    spec = importlib.util.spec_from_file_location("modal_vllm_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _flag_value(flags, name):
    return flags[flags.index(name) + 1]


def test_deploy_config_defaults():
    cfg = _load().deploy_config()
    assert cfg["app_name"] == "mailroom-vllm"
    assert cfg["gpu"] == "L4"
    assert cfg["gpu_count"] == 1
    assert cfg["model"] == "Qwen/Qwen3-8B-AWQ"
    assert "--enable-prefix-caching" in cfg["flags"]
    assert _flag_value(cfg["flags"], "--kv-cache-dtype") == "fp8"
    assert _flag_value(cfg["flags"], "--tensor-parallel-size") == "1"
    assert _flag_value(cfg["flags"], "--max-model-len") == str(cfg["max_len"])
    assert cfg["max_containers"] == 1
    assert cfg["scaledown_window"] > 0


def test_gpu_count_sets_tp(monkeypatch):
    monkeypatch.setenv("MODAL_GPU_COUNT", "2")
    cfg = _load().deploy_config()
    assert cfg["gpu_count"] == 2
    assert _flag_value(cfg["flags"], "--tensor-parallel-size") == "2"
    assert cfg["gpu_spec"] == "L4:2"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("MODAL_GPU", "A10G")
    monkeypatch.setenv("MODAL_MAX_CONTAINERS", "3")
    monkeypatch.setenv("MODAL_SCALEDOWN_WINDOW", "120")
    monkeypatch.setenv("VLLM_MODEL", "Qwen/Qwen3-4B-AWQ")
    monkeypatch.setenv("VLLM_MAX_LEN", "16384")
    cfg = _load().deploy_config()
    assert (cfg["gpu"], cfg["max_containers"], cfg["scaledown_window"]) == ("A10G", 3, 120)
    assert cfg["model"] == "Qwen/Qwen3-4B-AWQ"
    assert _flag_value(cfg["flags"], "--max-model-len") == "16384"


def test_module_imports_without_modal(monkeypatch):
    # Force `import modal` to fail even if the deploy extra is installed.
    monkeypatch.setitem(sys.modules, "modal", None)
    module = _load()
    assert module.modal is None
    assert module.app is None
    assert module.deploy_config()["model"] == "Qwen/Qwen3-8B-AWQ"


def test_baked_config_survives_clean_container_env(monkeypatch):
    monkeypatch.setenv("MODAL_GPU_COUNT", "2")
    monkeypatch.setenv("VLLM_MODEL", "x")
    module = _load()
    baked = module.baked_env(module.deploy_config())
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(module.CFG_ENV, baked[module.CFG_ENV])
    cfg = module.resolved_config()
    cmd = module.build_command(cfg)
    assert cfg["model"] == "x"
    assert _flag_value(cmd, "--tensor-parallel-size") == "2"
    assert "x" in cmd
