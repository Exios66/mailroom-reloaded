"""Passive per-provider LLM health (pager input): every chat-completion
attempt through ``llm/tooling.chat_create`` is recorded; nothing ever probes a provider."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai import APIStatusError


@pytest.fixture
def ph(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("MAILROOM_PROVIDER_HEALTH", "1")
    from mailroom_reloaded.llm import health

    monkeypatch.setattr(health, "_BUF", health._Buffer())
    return health


def test_provider_key_mapping(monkeypatch):
    from mailroom_reloaded.llm.health import provider_key

    monkeypatch.setenv("VLLM_BASE_URL", "https://acme--mailroom-vllm-serve.modal.run/v1")
    assert provider_key("https://acme--mailroom-vllm-serve.modal.run/v1", "x") == "modal:vllm"
    assert provider_key("https://acme--mailroom-vllm-extract.modal.run/v1", "x") == "modal:extract"
    assert provider_key("https://openrouter.ai/api/v1", "qwen/q") == "openrouter"
    assert provider_key("http://localhost:8000/v1", "m") == "other:localhost"
    assert provider_key(None, None) == "other:unknown"


def test_window_counts_and_consecutive(ph):
    url, t = "https://openrouter.ai/api/v1", 1_000_000.0
    for i, (ok, st) in enumerate([(True, 200), (False, 503), (False, 502), (True, 200), (False, 503), (False, 503)]):
        ph.record_llm_outcome(url, "m", ok=ok, status=st, latency_s=1.0 + i, now=t + i)
    h = ph.read_provider_health(now=t + 10)["openrouter"]
    assert h["calls"] == 6 and h["errors"] == 4 and h["consecutive_errors"] == 2
    assert h["count_503"] == 3 and h["last_status"] == 503 and h["last_ok_at"] == t + 3
    assert h["p50_latency_s"] == 3.5


def test_old_events_age_out(ph):
    url, t = "https://openrouter.ai/api/v1", 1_000_000.0
    ph.record_llm_outcome(url, "m", ok=False, status=503, latency_s=1.0, now=t)
    ph.record_llm_outcome(url, "m", ok=True, status=200, latency_s=1.0, now=t + 700)
    h = ph.read_provider_health(now=t + 700)["openrouter"]
    assert h["calls"] == 1 and h["errors"] == 0 and h["count_503"] == 0
    assert ph.read_provider_health(now=t + 5000) == {}


def test_corrupt_file_ignored_then_rewritten(ph):
    path = ph._path()
    path.write_text("{not json")
    assert ph.read_provider_health(now=1.0) == {}
    ph.record_llm_outcome("https://openrouter.ai/api/v1", "m", ok=False, status=500, latency_s=0.5, now=2.0)
    ph._BUF.flush()
    assert json.loads(path.read_text())["openrouter"][0][1] is False


def test_two_writers_merge(ph):
    a, b = ph._Buffer(), ph._Buffer()
    a.add("modal:vllm", [10.0, True, 200, 1.0], now=10.0)
    b.add("modal:vllm", [11.0, False, 503, 2.0], now=11.0)
    b.add("openrouter", [12.0, True, 200, 0.4], now=12.0)
    a.flush()
    b.flush()
    data = json.loads(ph._path().read_text())
    assert len(data["modal:vllm"]) == 2 and len(data["openrouter"]) == 1
    assert ph.read_provider_health(now=20.0)["modal:vllm"]["calls"] == 2


def test_cap_200_events_per_key(ph):
    for i in range(250):
        ph._BUF.add("openrouter", [float(i), True, 200, 0.1], now=float(i))
    ph._BUF.flush()
    assert len(json.loads(ph._path().read_text())["openrouter"]) == 200


def test_disabled_is_noop(monkeypatch):
    from mailroom_reloaded.llm import health

    monkeypatch.setenv("MAILROOM_PROVIDER_HEALTH", "0")
    monkeypatch.setattr(health, "_BUF", health._Buffer())
    health.record_llm_outcome("https://openrouter.ai/api/v1", "m", ok=False, status=500, latency_s=1, now=1.0)
    assert health._BUF.events == {}


def test_chat_create_records_each_attempt(ph, monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "https://acme--mailroom-vllm-serve.modal.run/v1")
    from mailroom_reloaded.llm.tooling import chat_create

    resp = MagicMock(status_code=503, headers={}, http_version="1.1")
    client = MagicMock()
    client.base_url = "https://acme--mailroom-vllm-serve.modal.run/v1"
    ok = SimpleNamespace(usage=None)
    client.chat.completions.create.side_effect = [APIStatusError("cold", response=resp, body=None), ok]
    assert chat_create(client, {"model": "m", "messages": []})[0] is ok
    h = ph.read_provider_health()["modal:vllm"]
    assert h["calls"] == 2 and h["count_503"] == 1 and h["consecutive_errors"] == 0
