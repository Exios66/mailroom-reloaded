"""Task 18 tests: OTel traces (OpenInference) and pipeline metrics.

The pipeline uses the ``mock`` provider through ``FakeOpenAI`` for the two
standalone LLM calls, following the harness in ``tests/pipeline/test_flow.py``.
Tracing is configured once at package import; each test attaches its own
in-memory exporter to the already-configured provider through ``setup_tracing``.
"""

from __future__ import annotations

import json

import pytest
from fakes.openai_server import FakeOpenAI
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.llm.client import call_structured
from mailroom_reloaded.obs import metrics as metrics_mod
from mailroom_reloaded.obs.metrics import M, setup_metrics
from mailroom_reloaded.obs.tracing import setup_tracing
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.storage.bins import Bins
from mailroom_reloaded.watcher import Watcher

CORR_SUBCLASS = {
    "doc_subclass": "email",
    "confidence": 0.99,
    "doc_type_disagree": False,
    "doc_type_disagree_reason": None,
}

CORR_EXTRACT = {
    "sender": "alice@example.com",
    "recipient": "bob@example.com",
    "additional_recipients": ["carol@example.com"],
    "communication_type": "email",
    "communication_date": "2026-01-02",
    "demand_amount": 1000.0,
    "action_items": ["reply by Friday"],
    "urgency": "normal",
    "intent": "request",
    "subject_matter": "the deal",
    "keywords": ["deal"],
    "confidence": 0.9,
}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def fake_openai():
    """Yield a local fake OpenAI server and stop it after the test."""
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):
    """Point the mock provider at the local fake OpenAI server."""
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate the base directory and reset settings and SQLite state per test."""
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    from mailroom_reloaded import settings

    settings.get_settings.cache_clear()
    from mailroom_reloaded.storage import db

    monkeypatch.setattr(db, "_default_engine", None)
    try:
        yield tmp_path
    finally:
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    """Disable retry delays and clear tool-support caches around each test."""
    from mailroom_reloaded.llm import retry, tooling

    monkeypatch.setattr(retry, "_sleep", lambda *_: None)
    tooling.reset_tool_support_cache()
    yield
    tooling.reset_tool_support_cache()


# --------------------------------------------------------------------------- helpers


def _attach_exporter(*, mask: bool = False) -> InMemorySpanExporter:
    """Attach an in-memory span exporter with optional content masking."""
    exporter = InMemorySpanExporter()
    setup_tracing(exporter=exporter, trace_mask=mask)
    return exporter


def _patch_handoff(monkeypatch):
    """Stub BERT classification and handoff for a deterministic routing scenario."""
    verdict = BertVerdict(
        available=True,
        reason="ok",
        doc_type="correspondence",
        subclass=None,
        calibrated_confidence=0.99,
        margin=0.5,
        window_agreement=1.0,
        n_windows=1,
        route="fast_path",
    )
    handoff = Handoff(
        SortMode.SUBCLASS_ONLY, "correspondence", "BERT predicts class correspondence", "fast_path"
    )
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None, *, filename=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)


def _reply(provider, payload):
    """Queue a discarded tool-round draft followed by the structured response."""
    provider.reply("thinking").reply(json.dumps(payload))


def _write_inbox(base, text="A short business letter about the deal."):
    """Write a test document to the inbox and return its bins and path."""
    bins = Bins(base)
    path = bins.inbox / "letter.txt"
    path.write_text(text)
    return bins, path


_DECISION_METRICS = {"mailroom.retries", "mailroom.escalations", "mailroom.review.causes"}


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    """Collect all instrument names emitted to the in-memory metric reader."""
    data = reader.get_metrics_data()
    return {
        metric.name
        for resource_metric in data.resource_metrics
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
    }


# --------------------------------------------------------------------------- tests


def test_flow_emits_node_spans(env, mock_provider, monkeypatch):
    """Verify completed pipeline nodes emit spans sharing one trace ID."""
    exporter = _attach_exporter()
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    _reply(mock_provider, CORR_EXTRACT)
    _, path = _write_inbox(env)

    state = flow_mod.run_document(path, worker_id="w1")

    assert state.status == "archived"
    node_spans = [
        span
        for span in exporter.get_finished_spans()
        if span.name.startswith("mailroom.node.")
    ]
    names = {span.name for span in node_spans}
    assert {
        "mailroom.node.ingest",
        "mailroom.node.bert_primary",
        "mailroom.node.sort",
        "mailroom.node.extract",
        "mailroom.node.report_catalog_archive",
    } <= names
    assert len({span.context.trace_id for span in node_spans}) == 1


def test_llm_span_has_genai_attrs(env, mock_provider):
    """Verify the LLM span records prompt, completion and total token counts."""
    exporter = _attach_exporter()
    mock_provider.reply(json.dumps({"ok": True}))

    call_structured("sorter", [{"role": "user", "content": "classify this"}])

    llm_spans = [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get("openinference.span.kind") == "LLM"
    ]
    assert llm_spans
    attrs = llm_spans[-1].attributes
    assert attrs["llm.token_count.prompt"] == 10
    assert attrs["llm.token_count.completion"] == 5
    assert attrs["llm.token_count.total"] == 15


def test_masking_redacts_content(env, mock_provider):
    """Verify exported LLM input and output content are replaced by the mask."""
    exporter = _attach_exporter(mask=True)
    mock_provider.reply("a sensitive completion")

    call_structured("sorter", [{"role": "user", "content": "sensitive document text"}])

    llm_spans = [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get("openinference.span.kind") == "LLM"
    ]
    assert llm_spans
    attrs = llm_spans[-1].attributes
    assert attrs["input.value"] == "<masked>"
    assert attrs["output.value"] == "<masked>"


def test_metric_names_emitted(env, mock_provider, monkeypatch):
    """Verify processing one document emits every declared metric instrument."""
    metrics_mod._reset_for_tests()
    reader = InMemoryMetricReader()
    setup_metrics(reader=reader)
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    _reply(mock_provider, CORR_EXTRACT)
    bins, _path = _write_inbox(env)

    watcher = Watcher(bins, worker_id="w1", concurrency=1)
    assert watcher.drain_once() == 1
    assert list((bins.archive / "correspondence").glob("*.txt"))

    emitted = _metric_names(reader)
    # decision counters only appear when a retry / escalation / review cause actually happens
    assert M.names() - _DECISION_METRICS <= emitted
