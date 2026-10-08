"""Jev client/gate tests. Every transport is a fake: NO network is used."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from mailroom_reloaded.agents.gate import BandGate, GateFeatures, LearnedGate, load_gate
from mailroom_reloaded.agents.jev import (
    JevAnswer,
    JevClient,
    JevError,
    JevGate,
    _jev_questions,
    choice,
    load_jev_gate,
    noul,
    score,
)
from mailroom_reloaded.eval.jev_calibration import JevCalibration
from mailroom_reloaded.settings import JevConfig, load_taxonomy


class FakeTransport:
    """Return queued ``httpx.Response`` objects and record every call."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, *, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _cfg(**overrides) -> JevConfig:
    base = {
        "provider": "openrouter",
        "model": "typesafe/jev-1.13",
        "base_url": "https://example.test/decisions",
        "api_key": "secret",
        "temperature": 1.0,
        "accept_threshold": 0.8,
        "timeout_s": 10.0,
        "max_retries": 2,
    }
    base.update(overrides)
    return JevConfig(**base)


def _resp(status: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


def _no_sleep(client: JevClient) -> list[float]:
    sleeps: list[float] = []
    client._sleep = sleeps.append
    return sleeps


# ------------------------------------------------------------------ payload/parsing


def test_ask_posts_payload_and_parses_choice():
    raw = {
        "type": "choice",
        "choice": "retry",
        "probabilities": {"proceed": 0.1, "retry": 0.7},
        "confidence": 0.7,
    }
    transport = FakeTransport(_resp(200, {"answers": {"route": raw}}))
    client = JevClient(_cfg(), transport=transport)

    out = client.ask("doc text", {"route": {"type": "choice"}})

    call = transport.calls[0]
    assert call["url"] == "https://example.test/decisions"
    assert call["json"] == {
        "model": "typesafe/jev-1.13",
        "state": "doc text",
        "questions": {"route": {"type": "choice"}},
    }
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["timeout"] == 10.0
    assert out["route"] == JevAnswer(
        type="choice",
        choice="retry",
        probabilities={"proceed": 0.1, "retry": 0.7},
        confidence=0.7,
        raw=raw,
    )


def test_ask_parses_noul_and_score():
    body = {
        "answers": {
            "escalate": {"type": "noul", "noul": 0.2},
            "quality": {
                "type": "score",
                "score": 0.77,
                "legend": {"1": "bad", "5": "good"},
            },
        }
    }
    client = JevClient(_cfg(), transport=FakeTransport(_resp(200, body)))

    out = client.ask("x", {"escalate": {"type": "noul"}, "quality": {"type": "score"}})

    assert out["escalate"].type == "noul"
    assert out["escalate"].noul == 0.2
    assert out["escalate"].legend is None
    assert out["quality"].type == "score"
    assert out["quality"].score == 0.77
    assert out["quality"].legend == {"1": "bad", "5": "good"}


def test_ask_derives_confidence_from_probabilities():
    body = {"answers": {"route": {"type": "choice", "choice": "proceed",
                                   "probabilities": {"proceed": 0.9, "retry": 0.1}}}}
    client = JevClient(_cfg(), transport=FakeTransport(_resp(200, body)))
    assert client.ask("x", {"route": {}})["route"].confidence == pytest.approx(0.9)


def test_ask_parses_answer_list():
    body = {
        "answers": [
            {"name": "route", "type": "choice", "choice": "verify",
             "probabilities": {"verify": 0.6, "retry": 0.4}}
        ]
    }
    client = JevClient(_cfg(), transport=FakeTransport(_resp(200, body)))
    assert client.ask("x", {"route": {}})["route"].choice == "verify"


def test_question_builders_shape():
    assert choice("r", "i", {"a": "A"}) == (
        "r",
        {"type": "choice", "instructions": "i", "criteria": {"a": "A"}},
    )
    assert noul("n", "i") == ("n", {"type": "noul", "instructions": "i"})
    assert noul("n", "i", {"x": "X"})[1]["criteria"] == {"x": "X"}
    assert score("s", "i", ["a", "b"]) == (
        "s",
        {"type": "score", "instructions": "i", "criteria": ["a", "b"]},
    )


def test_jev_questions_noul_payload_is_schema_valid():
    """The built route questions omit/validate ``criteria`` per the decisions schema."""
    questions = _jev_questions()

    escalate = questions["escalate"]
    assert escalate["type"] == "noul"
    assert "criteria" not in escalate

    for question in questions.values():
        if question["type"] == "noul":
            # When present, noul criteria must be a non-empty true/false mapping.
            criteria = question.get("criteria")
            assert criteria is None or set(criteria) <= {"true", "false"}
            assert criteria != {}
        if "criteria" in question:
            assert question["criteria"] not in ({}, None)


def test_ask_no_key_omits_authorization():
    transport = FakeTransport(_resp(200, {"answers": {}}))
    client = JevClient(_cfg(api_key=None, provider="local"), transport=transport)
    client.ask("x", {})
    assert "Authorization" not in transport.calls[0]["headers"]


# ------------------------------------------------------------------ retry policy


def test_retry_on_529_then_success():
    transport = FakeTransport(
        _resp(529),
        _resp(529),
        _resp(200, {"answers": {"route": {"choice": "proceed", "confidence": 0.9}}}),
    )
    client = JevClient(_cfg(max_retries=2), transport=transport)
    sleeps = _no_sleep(client)

    out = client.ask("x", {"route": {}})

    assert out["route"].choice == "proceed"
    assert len(transport.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_retry_honors_retry_after():
    transport = FakeTransport(
        _resp(429, headers={"Retry-After": "3"}),
        _resp(200, {"answers": {}}),
    )
    client = JevClient(_cfg(), transport=transport)
    sleeps = _no_sleep(client)

    client.ask("x", {})

    assert sleeps == [3.0]
    assert len(transport.calls) == 2


def test_exhausted_retries_raise():
    transport = FakeTransport(_resp(503), _resp(503), _resp(503))
    client = JevClient(_cfg(max_retries=2), transport=transport)
    _no_sleep(client)

    with pytest.raises(JevError, match="HTTP 503"):
        client.ask("x", {})

    assert len(transport.calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_retryable_4xx_raises_immediately(status):
    transport = FakeTransport(_resp(status))
    client = JevClient(_cfg(), transport=transport)
    sleeps = _no_sleep(client)

    with pytest.raises(JevError, match=f"HTTP {status}"):
        client.ask("x", {})

    assert len(transport.calls) == 1
    assert sleeps == []


# ------------------------------------------------------------------ JevGate


class StubClient:
    """Duck-typed ``JevClient`` returning canned answers; records calls."""

    def __init__(self, answers: dict, accept: float = 0.8) -> None:
        self.cfg = SimpleNamespace(accept_threshold=accept)
        self.answers = answers
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        self.state = state
        self.questions = questions
        return self.answers


def _ans(type_: str, **fields) -> JevAnswer:
    return JevAnswer(type=type_, **fields)


def _feature(**overrides) -> GateFeatures:
    base = {
        "stage": "classify",
        "doc_type": None,
        "confidence": 0.8,
        "attempts": 0,
        "bert_confidence": 0.9,
        "bert_margin": 0.5,
        "bert_window_agreement": 1.0,
        "schema_valid": True,
        "field_coverage": 1.0,
        "length_capped": False,
        "doc_type_disagree": False,
        "resorted": False,
    }
    base.update(overrides)
    return GateFeatures(**base)


def test_jev_gate_maps_choice_to_action():
    client = StubClient(
        {
            "route": _ans("choice", choice="verify", confidence=0.95),
            "escalate": _ans("noul", noul=0.0),
        }
    )
    decision = JevGate(BandGate(load_taxonomy()), client).decide(_feature())
    assert decision.action == "verify"
    assert decision.source == "jev"


def test_jev_gate_low_confidence_to_human_review():
    client = StubClient(
        {
            "route": _ans("choice", choice="proceed", confidence=0.5),
            "escalate": _ans("noul", noul=0.0),
        }
    )
    decision = JevGate(BandGate(load_taxonomy()), client).decide(_feature())
    assert decision.action == "human_review"
    assert decision.source == "jev"


def test_jev_gate_noul_escalation_to_human_review():
    client = StubClient(
        {
            "route": _ans("choice", choice="proceed", confidence=0.99),
            "escalate": _ans("noul", noul=0.9),
        }
    )
    decision = JevGate(BandGate(load_taxonomy()), client).decide(_feature())
    assert decision.action == "human_review"
    assert decision.source == "jev"


def test_jev_gate_never_overrides_rule_decision():
    client = StubClient({"route": _ans("choice", choice="proceed", confidence=0.99)})
    band = BandGate(load_taxonomy())
    f = _feature(doc_type_disagree=True)
    base = band.decide(f)
    assert base.source == "rule" and base.action == "re_sort"

    assert JevGate(band, client).decide(f) == base
    assert client.calls == 0


def test_jev_gate_ignores_outside_medium_band():
    client = StubClient({"route": _ans("choice", choice="proceed", confidence=0.99)})
    band = BandGate(load_taxonomy())
    f = _feature(confidence=0.99)
    assert JevGate(band, client).decide(f) == band.decide(f)
    assert client.calls == 0


def test_jev_gate_uses_calibration_accept_threshold():
    client = StubClient({"route": _ans("choice", choice="verify", confidence=0.9)})
    calibration = JevCalibration(
        temperature=1.0,
        accept_threshold=0.95,
        verify_threshold=0.5,
        ece_before=0.0,
        ece_after=0.0,
        n=1,
    )
    decision = JevGate(BandGate(load_taxonomy()), client, calibration).decide(_feature())
    assert decision.action == "verify"  # 0.5 <= 0.9 < 0.95 -> medium band
    assert decision.source == "jev"


def test_jev_gate_below_verify_threshold_is_human_review():
    client = StubClient({"route": _ans("choice", choice="proceed", confidence=0.4)})
    calibration = JevCalibration(
        temperature=1.0,
        accept_threshold=0.95,
        verify_threshold=0.5,
        ece_before=0.0,
        ece_after=0.0,
        n=1,
    )
    decision = JevGate(BandGate(load_taxonomy()), client, calibration).decide(_feature())
    assert decision.action == "human_review"  # 0.4 < verify 0.5
    assert decision.source == "jev"


def test_jev_gate_medium_band_keeps_escalation_choice():
    client = StubClient({"route": _ans("choice", choice="boss", confidence=0.9)})
    calibration = JevCalibration(
        temperature=1.0,
        accept_threshold=0.95,
        verify_threshold=0.5,
        ece_before=0.0,
        ece_after=0.0,
        n=1,
    )
    decision = JevGate(BandGate(load_taxonomy()), client, calibration).decide(_feature())
    assert decision.action == "human_review"  # escalation is never downgraded to verify
    assert decision.source == "jev"


# ------------------------------------------------------------------ loading


def _neutral_calibration() -> dict:
    return {
        "temperature": 1.0,
        "accept_threshold": 0.8,
        "verify_threshold": 0.5,
        "ece_before": 0.0,
        "ece_after": 0.0,
        "n": 0,
    }


def test_disabled_provider_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("MAILROOM_JEV_PROVIDER", raising=False)
    monkeypatch.delenv("JEV_PROVIDER", raising=False)

    assert load_jev_gate() is None
    assert isinstance(load_gate(), BandGate)

    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "route_gate.json").write_text(
        json.dumps(
            {
                "classify": {
                    "features": ["confidence"],
                    "coef": [0.0],
                    "intercept": 0.0,
                    "threshold": 0.5,
                }
            }
        ),
        encoding="utf-8",
    )
    assert isinstance(load_gate(), LearnedGate)


def test_enabled_with_calibration_prefers_jev_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "local")
    models = tmp_path / "models"
    models.mkdir()
    (models / "jev_calibration.json").write_text(
        json.dumps(_neutral_calibration()), encoding="utf-8"
    )

    gate = load_jev_gate()
    assert isinstance(gate, JevGate)
    assert isinstance(load_gate(), JevGate)


def test_corrupt_calibration_falls_back_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "local")
    models = tmp_path / "models"
    models.mkdir()
    (models / "jev_calibration.json").write_text("{ not valid json", encoding="utf-8")

    assert load_jev_gate() is None
    assert isinstance(load_gate(), BandGate)


def test_incomplete_calibration_falls_back_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "local")
    models = tmp_path / "models"
    models.mkdir()
    (models / "jev_calibration.json").write_text(
        json.dumps({"temperature": 1.0}), encoding="utf-8"
    )

    assert load_jev_gate() is None
    assert isinstance(load_gate(), BandGate)


def test_invariant_violating_calibration_falls_back_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "local")
    models = tmp_path / "models"
    models.mkdir()
    bad = {**_neutral_calibration(), "temperature": 0.0}
    (models / "jev_calibration.json").write_text(json.dumps(bad), encoding="utf-8")

    assert load_jev_gate() is None
    assert isinstance(load_gate(), BandGate)
