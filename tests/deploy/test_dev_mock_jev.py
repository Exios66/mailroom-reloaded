"""The dev Jev stand-in speaks the JevClient wire shape; mock_openai honours markers."""

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from mailroom_reloaded.agents.jev import JevClient, _jev_questions
from mailroom_reloaded.settings import JevConfig

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ClientTransport:
    def __init__(self, http):
        self.http = http

    def post(self, url, *, json, headers, timeout):
        return self.http.post("/v1/systemone", json=json)


def _ask(state):
    mod = _load("mock_jev")
    cfg = JevConfig("local", "jevk5", "http://x/v1/systemone", None, 1.0, 0.8, 5.0, 0)
    with TestClient(mod.app) as http:
        assert http.get("/health").status_code == 200
        return JevClient(cfg, transport=_ClientTransport(http)).ask(state, _jev_questions())


def test_markers_choose_route():
    for route in ("proceed", "verify", "boss"):
        answers = _ask(f"doc [jev:{route}]")
        assert answers["route"].choice == route
        assert answers["route"].confidence > 0.5
        assert answers["escalate"].noul < 0.5


def test_feature_state_is_deterministic_by_stage_and_confidence():
    assert _ask({"stage": "classify", "confidence": 0.9})["route"].choice == "proceed"
    assert _ask({"stage": "classify", "confidence": 0.88})["route"].choice == "verify"
    assert _ask({"stage": "extract", "confidence": 0.92})["route"].choice == "proceed"
    assert _ask({"stage": "extract", "confidence": 0.88})["route"].choice == "verify"
    low = _ask({"stage": "extract", "confidence": 0.86})
    assert low["route"].confidence < 0.6 and low["escalate"].noul >= 0.5


def test_mock_openai_confidence_marker():
    mod = _load("mock_openai")
    schema = {"properties": {"doc_subclass": {}, "confidence": {}}}
    body = {
        "messages": [{"role": "user", "content": "hi [confidence:0.88] there"}],
        "response_format": {"json_schema": {"schema": schema}},
    }
    with TestClient(mod.app) as http:
        import json

        content = http.post("/v1/chat/completions", json=body).json()["choices"][0]["message"]["content"]
        assert json.loads(content)["confidence"] == 0.88
        body["messages"][0]["content"] = "no marker"
        content = http.post("/v1/chat/completions", json=body).json()["choices"][0]["message"]["content"]
        assert json.loads(content)["confidence"] == 0.99
