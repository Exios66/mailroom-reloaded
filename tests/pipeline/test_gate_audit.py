"""Gate decisions are recorded in the audit chain (incl. Jev-sourced ones)."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import trace

from mailroom_reloaded.agents.gate import BandGate
from mailroom_reloaded.agents.jev import JevAnswer, JevGate
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.schemas.manifest import Manifest
from mailroom_reloaded.settings import load_taxonomy
from mailroom_reloaded.storage import audit_log, db


class _FakeJev:
    def __init__(self):
        self.cfg = SimpleNamespace(accept_threshold=0.8)

    def ask(self, state, questions):
        return {
            "route": JevAnswer(type="choice", choice="proceed", confidence=0.95),
            "escalate": JevAnswer(type="noul", noul=0.0),
        }


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("CREWAI_TELEMETRY_DISABLED", "true")
    from mailroom_reloaded import settings

    settings.get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    inst = flow_mod.MailroomFlow()
    inst.state.doc_id = "doc-gate"
    inst._overrides = {}
    inst._bins = Mock()
    inst._manifest = Manifest(doc_id="doc-gate", filename="a.txt", content_sha256="x")
    inst._tracer = trace.NoOpTracer()
    inst.state.sort = SimpleNamespace(
        doc_type="correspondence", confidence=0.9, doc_type_disagree=False
    )
    inst.state.bert = None
    try:
        yield inst
    finally:
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()


def test_classify_gate_decision_audited_with_band_source(flow):
    flow.state.sort.confidence = 0.99
    flow._gate = BandGate(load_taxonomy())
    assert flow._classify_route() == "do_extract"
    (entry,) = audit_log.entries("doc-gate")
    assert (entry.node, entry.event) == ("gate_classify", "gate_decision")
    assert entry.payload["action"] == "proceed"
    assert entry.payload["source"] == "band"
    assert entry.payload["confidence"] == 0.99
    assert audit_log.verify_chain(audit_log.entries("doc-gate")).ok


def test_jev_sourced_decision_recorded_as_jev(flow):
    flow._gate = JevGate(BandGate(load_taxonomy()), _FakeJev())
    assert flow._classify_route() == "do_extract"  # 0.90 is in the medium band
    (entry,) = audit_log.entries("doc-gate")
    assert entry.node == "gate_classify"
    assert entry.payload["source"] == "jev"
    assert entry.payload["action"] == "proceed"
    assert "jev choice" in entry.payload["reason"]


def test_extract_gate_decision_audited(flow):
    flow.state.extract = SimpleNamespace(
        confidence=0.9, schema_valid=True, error_kind=None, doc_type="correspondence"
    )
    flow._gate = JevGate(BandGate(load_taxonomy()), _FakeJev())
    flow._extract_route()
    (entry,) = audit_log.entries("doc-gate")
    assert entry.node == "gate_extract"
    assert entry.payload["source"] == "jev"
