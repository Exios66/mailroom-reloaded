"""Pipeline span attributes, decision events and scores (trace-replay Task 2)."""

from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mailroom_reloaded.agents.arbiter import ArbiterDecision
from mailroom_reloaded.agents.judge import JudgeVerdict
from mailroom_reloaded.agents.sorter import SortResult
from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.eval.dataset import EvalContext, GroundTruth
from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.obs.tracing import setup_tracing
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.storage.bins import Bins

U = Usage(prompt_tokens=5, completion_tokens=5, calls=1)
DOC_BODY = "Dear Alice Smith, your SSN 123-45-6789 is on file. Regards."


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("MAILROOM_RUN_ID", raising=False)
    from mailroom_reloaded import settings
    from mailroom_reloaded.storage import db
    from mailroom_reloaded.storage.ledger import reset_ledger

    settings.get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    try:
        yield tmp_path
    finally:
        reset_ledger()
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()


@pytest.fixture
def exporter():
    exp = InMemorySpanExporter()
    setup_tracing(exporter=exp, trace_mask=False)
    return exp


def _handoff(monkeypatch):
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
    handoff = Handoff(SortMode.SUBCLASS_ONLY, "correspondence", "BERT", "fast_path")
    monkeypatch.setattr(
        flow_mod, "classify_primary", lambda text, cfg=None, *, filename=None: verdict
    )
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)


def _sort(*a, **k):
    return SortResult(
        "correspondence",
        "email",
        0.99,
        0.9,
        False,
        SortMode.SUBCLASS_ONLY,
        "self_report",
        False,
        None,
        U,
    )


def _extractor(*confidences, data=None):
    seq = list(confidences)

    def run(text, doc_type, doc_subclass, **kwargs):
        conf = seq.pop(0) if len(seq) > 1 else seq[0]
        return ExtractResult(
            doc_type,
            data if data is not None else {"a": 1},
            True,
            None,
            conf,
            None,
            1,
            U,
        )

    return run


def _run(env, monkeypatch, *, name="letter.txt", **kw):
    bins = Bins(env)
    path = bins.inbox / name
    path.write_text(kw.pop("text", DOC_BODY))
    return flow_mod.run_document(path, worker_id="w1", **kw)


def _by_name(exporter):
    out: dict[str, list] = {}
    for s in exporter.get_finished_spans():
        out.setdefault(s.name, []).append(s)
    return out


def _events(span, name):
    return [e for e in span.events if e.name == name]


def test_happy_path_root_and_node_attributes(env, exporter, monkeypatch) -> None:
    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extractor(0.99))
    state = _run(env, monkeypatch)
    spans = _by_name(exporter)
    root = spans["mailroom.document"][-1]
    a = root.attributes
    assert (
        a["openinference.span.kind"] == "CHAIN" and a["mailroom.doc_id"] == state.doc_id
    )
    assert a["mailroom.filename"] == "letter.txt" and a["mailroom.run_id"].startswith(
        "live-"
    )
    assert a["mailroom.status"] == "archived" and a["mailroom.stage"] == "archive"
    assert a["mailroom.doc_type"] == "correspondence" and a["mailroom.resumed"] is False
    assert json.loads(a["mailroom.route_trail"]) == state.route_trail
    assert a["mailroom.usage.calls"] == state.usage_total.calls
    assert set(json.loads(a["mailroom.usage.by_role"])) == {
        "sorter",
        "correspondence_specialist",
    }
    nodes = {
        n.split(".")[-1]: v[-1]
        for n, v in spans.items()
        if n.startswith("mailroom.node.")
    }
    assert set(nodes) >= {
        "ingest",
        "bert_primary",
        "sort",
        "extract",
        "report_catalog_archive",
    }
    sort = nodes["sort"].attributes
    assert (
        sort["mailroom.station"] == "sorter" and sort["mailroom.phase"] == "intake_sort"
    )
    assert sort["openinference.span.kind"] == "AGENT" and sort["mailroom.attempt"] == 1
    assert sort["mailroom.stage"] == "classify" and sort["mailroom.tokens.used"] == 10
    assert sort["mailroom.sort.doc_type"] == "correspondence"
    assert nodes["extract"].attributes["mailroom.station"] == "specialist"
    assert nodes["extract"].attributes["mailroom.extract.n_fields"] == 1
    assert nodes["ingest"].attributes["mailroom.intake.method"] == "text"
    assert nodes["report_catalog_archive"].attributes["mailroom.stage"] == "archive"
    assert _events(nodes["report_catalog_archive"], "mailroom.archived")


def test_every_run_scores_and_causes_on_the_root(env, exporter, monkeypatch) -> None:
    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extractor(0.99))
    _run(env, monkeypatch)
    a = _by_name(exporter)["mailroom.document"][-1].attributes
    score = lambda n: a["mailroom.score." + n]
    assert score("schema_valid") is True and score("stage_completed") is True
    assert score("success_rate") == 1.0 and score("run_aborted") is False
    assert score("classification_attempts") == 1 and score("extraction_attempts") == 1
    assert score("total_tokens") == 20 and score("llm_call_count") == 2
    assert (
        json.loads(score("review_causes")) == []
        and score("needs_reconsideration") is False
    )
    assert (
        score("classification_confidence") == 0.99
        and score("run_duration_seconds") >= 0
    )


def test_retry_emits_event_and_tags_the_second_attempt(
    env, exporter, monkeypatch
) -> None:
    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extractor(0.1, 1.0))
    state = _run(env, monkeypatch)
    assert state.extract_attempts == 1
    spans = _by_name(exporter)
    root = spans["mailroom.document"][-1]
    retry = _events(root, "mailroom.retry")
    assert (
        len(retry) == 1
        and retry[0].attributes["kind"] == "retry_extract"
        and retry[0].attributes["attempt"] == 1
    )
    extracts = sorted(spans["mailroom.node.extract"], key=lambda s: s.start_time)[-2:]
    assert [s.attributes["mailroom.attempt"] for s in extracts] == [1, 2]
    assert "mailroom.retry_kind" not in extracts[0].attributes
    assert extracts[1].attributes["mailroom.retry_kind"] == "retry_extract"
    assert extracts[1].attributes["mailroom.stage"] == "retry_extract"
    gates = _events(root, "mailroom.gate_decision")
    assert [g.attributes["stage"] for g in gates][-2:] == ["extract", "extract"]
    routes = _events(root, "mailroom.route")
    assert routes and all({"from", "to"} <= set(r.attributes) for r in routes)
    assert root.attributes["mailroom.score.success_rate"] == 0.0
    assert root.attributes["mailroom.score.extraction_attempts"] == 2


def test_deadline_failure_sets_reason_and_class(env, exporter, monkeypatch) -> None:
    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    state = _run(env, monkeypatch, overrides={"deadlines": {"sort": 1e-9}})
    assert state.status == "failed"
    spans = _by_name(exporter)
    node = spans["mailroom.node.sort"][-1]
    assert node.attributes["mailroom.fail_reason"] == "deadline_exceeded"
    assert node.attributes["mailroom.failure_class"] == "run_budget"
    assert node.status.status_code.name == "ERROR"
    root = spans["mailroom.document"][-1]
    assert (
        root.attributes["mailroom.status"] == "failed"
        and root.attributes["mailroom.stage"] == "failed"
    )
    assert root.attributes["mailroom.failure_class"] == "run_budget"
    assert root.attributes["mailroom.score.stage_completed"] is False


def test_parked_document_emits_escalation_and_parked_events(
    env, exporter, monkeypatch
) -> None:
    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(
        flow_mod.MailroomFlow, "_classify_route", lambda self: "human_review"
    )
    state = _run(env, monkeypatch)
    assert state.status == "parked"
    root = _by_name(exporter)["mailroom.document"][-1]
    assert [e.attributes["to"] for e in _events(root, "mailroom.escalation")] == [
        "human_review"
    ]
    assert (
        _events(root, "mailroom.parked")[0].attributes["reason"]
        == "classify_human_review"
    )
    assert (
        root.attributes["mailroom.stage"] == "review"
        and root.attributes["mailroom.status"] == "parked"
    )


def test_crash_marks_the_root_aborted_and_the_node_failed(
    env, exporter, monkeypatch
) -> None:
    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)

    def boom(*a, **k):
        raise TimeoutError("took too long /home/alice/x.pdf")

    monkeypatch.setattr(flow_mod, "_extract", boom)
    with pytest.raises(TimeoutError):
        _run(env, monkeypatch)
    spans = _by_name(exporter)
    node = spans["mailroom.node.extract"][-1]
    assert node.attributes["mailroom.fail_reason"] == "TimeoutError"
    assert node.attributes["mailroom.failure_class"] == "llm_timeout"
    root = spans["mailroom.document"][-1]
    assert (
        root.attributes["mailroom.status"] == "aborted"
        and root.attributes["mailroom.score.run_aborted"] is True
    )


def test_verify_path_records_judge_and_arbiter(env, exporter, monkeypatch) -> None:
    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extractor(0.90))  # verify band
    monkeypatch.setattr(
        flow_mod,
        "judge_verify",
        lambda *a, **k: JudgeVerdict(label="partial", score=0.6, field_findings=[]),
    )
    monkeypatch.setattr(
        flow_mod,
        "arbitrate",
        lambda *a, **k: ArbiterDecision(action="accept_with_caveats", caveats=["x"]),
    )
    _run(env, monkeypatch)
    spans = _by_name(exporter)
    verify = spans["mailroom.node.verify"][-1]
    assert (
        verify.attributes["mailroom.station"] == "judge"
        and verify.attributes["openinference.span.kind"] == "EVALUATOR"
    )
    assert verify.attributes["mailroom.judge.label"] == "partial"
    assert verify.attributes["mailroom.arbiter.decision"] == "accept_with_caveats"
    root = spans["mailroom.document"][-1]
    assert _events(root, "mailroom.judge_gate") and _events(root, "mailroom.arbiter")
    assert json.loads(root.attributes["mailroom.score.review_causes"]) == [
        "judge_partial",
        "reporting_incomplete",
    ]  # score 0.6 is below the floor
    assert root.attributes["mailroom.score.needs_reconsideration"] is True


def test_eval_ground_truth_attributes_and_grounded_scores(
    env, exporter, monkeypatch
) -> None:
    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(
        flow_mod,
        "_extract",
        _extractor(0.99, data={"sender": "alice@example.com", "urgency": "normal"}),
    )
    from mailroom_reloaded.agents.judge import ClassificationFinding, JudgeGrade

    monkeypatch.setattr(
        flow_mod,
        "judge_grade",
        lambda text, dt, data, ctx: JudgeGrade(
            doc_id=ctx.doc_id,
            doc_type=dt,
            fields=[],
            classification=ClassificationFinding(verdict="correct", rationale=""),
            overall=0.8,
            usage=U,
        ),
    )
    bins = Bins(env)
    path = bins.inbox / "letter.txt"
    path.write_text(DOC_BODY)
    from mailroom_reloaded.storage.bins import doc_id_for

    doc_id = doc_id_for(path)
    gt = GroundTruth(
        filename="letter.txt",
        expected="contract",
        expected_subclass="nda",
        fields={"sender": "alice@example.com", "urgency": "high"},
        expected_stage="archived",
    )
    ctx = EvalContext("evalrun1", {doc_id: gt})
    flow_mod.run_document(path, worker_id="w1", eval_ctx=ctx)
    spans = _by_name(exporter)
    root = spans["mailroom.document"][-1]
    a = root.attributes
    assert (
        a["mailroom.run_id"] == "evalrun1"
        and a["mailroom.gt.expected_doc_class"] == "contract"
    )
    assert (
        a["mailroom.score.class_correct"] is False
        and a["mailroom.score.stage_correct"] is True
    )
    assert "class_miss" in json.loads(a["mailroom.score.review_causes"])
    grade = spans["mailroom.node.grade"][-1].attributes
    assert grade["mailroom.score.judge_overall"] == 0.8
    assert grade["mailroom.score.extraction_field_score.sender"] == 1.0
    assert grade["mailroom.score.extraction_field_score.urgency"] < 1.0
    assert 0 < grade["mailroom.score.extraction_overall_score"] < 1
    assert (
        grade["mailroom.score.extraction_precision"] <= 1
        and grade["mailroom.score.tp"] >= 1
    )


def test_no_attribute_anywhere_contains_the_document_body(
    env, exporter, monkeypatch
) -> None:
    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extractor(0.99))
    _run(env, monkeypatch)
    blob = ""
    for s in exporter.get_finished_spans():
        blob += json.dumps(dict(s.attributes), default=str)
        for e in s.events:
            blob += json.dumps(dict(e.attributes or {}), default=str)
    for leaked in ("Alice Smith", "123-45-6789", "your SSN"):
        assert leaked not in blob


def test_a_broken_capture_helper_never_fails_a_document(env, monkeypatch) -> None:
    from mailroom_reloaded.pipeline import trace_capture

    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extractor(0.99))
    monkeypatch.delitem(
        __import__("sys").modules, "pytest"
    )  # production behaviour: swallow
    try:
        monkeypatch.setattr(
            trace_capture,
            "emit_score",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
        )
        state = _run(env, monkeypatch)
    finally:
        __import__("sys").modules["pytest"] = pytest
    assert state.status == "archived"


@pytest.fixture
def mock_provider(monkeypatch):
    from fakes.openai_server import FakeOpenAI

    server = FakeOpenAI()
    server.start()
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", server.base_url)
    try:
        yield server
    finally:
        server.stop()


def test_llm_call_gets_its_own_span_without_content_or_double_counted_tokens(
    exporter, mock_provider
) -> None:
    from mailroom_reloaded.llm.client import call_structured

    mock_provider.reply("a completion that mentions Alice Smith")
    call_structured("sorter", [{"role": "user", "content": DOC_BODY}])
    span = _by_name(exporter)["mailroom.llm.sorter"][-1]
    a = span.attributes
    assert a["mailroom.role"] == "sorter" and a["openinference.span.kind"] == "SPAN"
    assert a["mailroom.tokens.prompt"] == 10 and a["mailroom.tokens.completion"] == 5
    assert a["mailroom.tokens.total"] == 15 and a["mailroom.length_capped"] is False
    assert a["mailroom.model"] and a["mailroom.provider"] == "mock"
    assert not [
        k for k in a if k.startswith("llm.") or k in ("input.value", "output.value")
    ]
    assert "Alice" not in json.dumps(dict(a))


def test_transient_failures_are_recorded_as_retry_events(
    exporter, mock_provider, monkeypatch
) -> None:
    from mailroom_reloaded.llm import retry
    from mailroom_reloaded.llm.client import call_structured

    monkeypatch.setattr(retry, "_sleep", lambda *_: None)
    mock_provider.fail(503)
    mock_provider.reply("ok")
    call_structured("sorter", [{"role": "user", "content": "x"}])
    events = _events(
        _by_name(exporter)["mailroom.llm.sorter"][-1], "mailroom.llm_retry"
    )
    assert len(events) == 1
    assert (
        events[0].attributes["attempt"] == 1
        and events[0].attributes["status_code"] == 503
    )
    assert events[0].attributes["error_type"] and "retry_in_s" in events[0].attributes


def test_a_length_capped_call_is_flagged_on_its_span(exporter, mock_provider) -> None:
    from mailroom_reloaded.llm.client import LengthFinishReasonError, call_structured

    mock_provider.reply("truncated", finish_reason="length")
    with pytest.raises(LengthFinishReasonError):
        call_structured("sorter", [{"role": "user", "content": "x"}])
    span = _by_name(exporter)["mailroom.llm.sorter"][-1]
    assert span.attributes["mailroom.length_capped"] is True
    assert span.attributes["mailroom.tokens.total"] == 15  # the capped call still cost tokens
    assert span.status.status_code.name == "ERROR"


def test_captured_spans_land_in_the_span_store_without_content(
    env, monkeypatch, tmp_path
) -> None:
    from mailroom_reloaded.storage.span_store import SpanStore, SqliteSpanExporter

    store = SpanStore(tmp_path / "traces.db")
    setup_tracing(exporter=SqliteSpanExporter(store), trace_mask=False)
    _handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extractor(0.99))
    state = _run(env, monkeypatch)
    rows = store.spans_for_doc(state.doc_id)
    names = {r["name"] for r in rows}
    assert {"mailroom.document", "mailroom.node.sort", "mailroom.node.extract"} <= names
    run_id = next(r["run_id"] for r in rows if r["name"] == "mailroom.document")
    assert run_id.startswith("live-") and store.list_runs()[0]["run_id"] == run_id
    sort = next(r for r in rows if r["name"] == "mailroom.node.sort")
    assert sort["station"] == "sorter" and sort["attrs"]["mailroom.stage"] == "classify"
    # LLM spans carry the run stamp too (set at start, in the pipeline's thread)
    assert all(r["run_id"] == run_id for r in rows)
    blob = json.dumps(store.spans_between(0, 2**63 - 1))
    for leaked in ("Alice Smith", "123-45-6789"):
        assert leaked not in blob
    store.close()
