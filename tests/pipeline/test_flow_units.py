"""Isolated tests of pipeline routing, guards, and agent context boundaries."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import trace
from pydantic import BaseModel

from mailroom_reloaded.agents.arbiter import ArbiterDecision
from mailroom_reloaded.agents.boss import BossDecision
from mailroom_reloaded.agents.judge import JudgeVerdict
from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import Handoff, SortMode
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.pipeline.archivist import ArchiveResult
from mailroom_reloaded.pipeline.guards import NodeFailed, guarded
from mailroom_reloaded.schemas.manifest import Manifest


@pytest.fixture
def flow(monkeypatch):
    """Build a flow with synthetic state and mocked persistence, bins and tracing."""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("CREWAI_TELEMETRY_DISABLED", "true")
    instance = flow_mod.MailroomFlow()
    instance.state.doc_id = "doc-1"
    instance.state.text = "source text"
    instance.state.path = "processing/letter.txt"
    instance._overrides = {}
    instance._resume_done = set()
    instance._resume_from = None
    instance._eval_ctx = None
    instance._llm_calls = 0
    instance._bins = Mock()
    instance._bins.move.return_value = "failed/letter.txt"
    instance._manifest = Manifest(
        doc_id="doc-1", filename="letter.txt", content_sha256="abc"
    )
    instance._tracer = trace.NoOpTracer()
    instance._gate = Mock()
    monkeypatch.setattr(flow_mod, "save_manifest", Mock())
    monkeypatch.setattr(flow_mod.audit_log, "append", Mock())
    return instance


def extraction(**kwargs):
    """Build a valid extraction result with caller-supplied field overrides."""
    values = {
        "doc_type": "correspondence",
        "data": {"sender": "Alice"},
        "schema_valid": True,
        "parse_error": None,
        "confidence": 0.99,
        "error_kind": None,
        "calls": 1,
        "usage": Usage(calls=1),
    }
    values.update(kwargs)
    return ExtractResult(**values)


@pytest.mark.parametrize(
    "action,route",
    [
        ("proceed", "do_extract"),
        ("retry", "retry_sort"),
        ("re_sort", "re_sort"),
        ("human_review", "human_review"),
        ("unexpected", "human_review"),
    ],
)
def test_classification_route_without_results_fails_closed(flow, action, route):
    """Verify missing classification signals default safely and unknown actions park."""
    flow._gate.decide.return_value = SimpleNamespace(action=action)
    flow.state.classify_attempts = 2
    assert flow._classify_route() == route
    features = flow._gate.decide.call_args.args[0]
    assert features.stage == "classify"
    assert features.doc_type is None
    assert features.confidence == 0
    assert features.attempts == 2
    assert (
        features.bert_confidence
        == features.bert_margin
        == features.bert_window_agreement
        == 0
    )


@pytest.mark.parametrize(
    "action,route",
    [
        ("proceed", "report"),
        ("retry", "retry_extract"),
        ("verify", "do_verify"),
        ("boss", "do_boss"),
        ("human_review", "human_review"),
        ("unexpected", "human_review"),
    ],
)
def test_extraction_route_preserves_failure_signals(flow, action, route):
    """Verify extraction routing passes schema and truncation failures to the gate."""
    flow.state.extract = extraction(
        schema_valid=False, confidence=None, error_kind="LengthFinishReasonError"
    )
    flow.state.extract_attempts = 2
    flow._gate.decide.return_value = SimpleNamespace(action=action)
    assert flow._extract_route() == route
    features = flow._gate.decide.call_args.args[0]
    assert features.stage == "extract"
    assert features.doc_type == "correspondence"
    assert features.confidence == 0
    assert features.attempts == 2
    assert features.schema_valid is False
    assert features.field_coverage == 0
    assert features.length_capped is True


def test_missing_extraction_is_not_treated_as_valid(flow):
    """Verify an absent extraction produces invalid-schema and zero-confidence features."""
    flow._gate.decide.return_value = SimpleNamespace(action="human_review")
    assert flow._extract_route() == "human_review"
    features = flow._gate.decide.call_args.args[0]
    assert features.schema_valid is False
    assert features.confidence == features.field_coverage == 0
    assert features.length_capped is False


@pytest.mark.parametrize(
    "action,expected",
    [
        (None, "report"),
        ("accept", "report"),
        ("accept_with_caveats", "report"),
        ("re_extract", "retry_extract"),
        ("escalate", "do_boss"),
    ],
)
def test_arbiter_routes(flow, action, expected):
    """Verify each arbiter action selects the expected next route."""
    flow.state.arbiter = ArbiterDecision(action=action) if action else None
    assert flow._arbiter_route() == expected


def test_explicit_type_and_subclass_override_boss_and_handoff(flow):
    """Verify explicit corrections take priority over boss reassignment and BERT locks."""
    flow.state.handoff = Handoff(SortMode.SUBCLASS_ONLY, "correspondence", "", "fast")
    flow.state.boss = BossDecision(
        action="reassign_class", doc_type="contract", doc_subclass="license"
    )
    assert flow._effective_doc_type() == "contract"
    assert flow._effective_subclass() == "license"
    flow._overrides = {"doc_type": "insurance_claim", "doc_subclass": "fnol"}
    assert flow._effective_doc_type() == "insurance_claim"
    assert flow._effective_subclass() == "fnol"
    flow._overrides = {}
    flow.state.boss = BossDecision(
        action="accept", doc_type="contract", doc_subclass="license"
    )
    assert flow._effective_doc_type() == "correspondence"
    assert flow._effective_subclass() is None


def test_resort_unlocks_type_but_keeps_bert_prior(flow):
    """Verify re-sorting removes the class lock while retaining the BERT hint."""
    flow.state.handoff = Handoff(
        SortMode.SUBCLASS_ONLY, "contract", "BERT prior", "fast"
    )
    flow._reset_handoff_full()
    assert flow.state.handoff == Handoff(SortMode.FULL, None, "BERT prior", "re_sort")


@pytest.mark.parametrize(
    "budget,used,failed",
    [(10, 10, False), (10, 11, True), (0, 11, False), (-1, 11, False)],
)
def test_guard_token_budget_is_per_node_and_inclusive(flow, budget, used, failed):
    """Verify token guards measure each node's usage and permit the exact budget."""
    flow.state.usage_total = Usage(prompt_tokens=100, calls=4)
    flow._overrides = {"token_budgets": {"extract": budget}}

    def work(instance):
        instance.state.usage_total += Usage(completion_tokens=used, calls=1)
        return "result"

    if failed:
        with pytest.raises(NodeFailed) as exc:
            flow._guard_node("extract", 0, 999, work, (), {})
        assert exc.value.node == "extract"
        assert exc.value.reason == "token_budget_exceeded"
        assert flow.state.status == flow._manifest.status == "failed"
        assert flow._manifest.completed_nodes == []
        flow._bins.move.assert_called_once()
        assert flow._manifest.state["usage_total"]["completion_tokens"] == used
        assert flow_mod.audit_log.append.call_args.args[2] == "node_failed"
    else:
        assert flow._guard_node("extract", 0, 999, work, (), {}) == "result"
        assert flow._manifest.completed_nodes == ["extract"]
        assert flow.state.status == "processing"
        flow._bins.move.assert_not_called()


@pytest.mark.parametrize(
    "deadline,elapsed,failed",
    [(5, 5, False), (5, 5.01, True), (0, 100, False), (-1, 100, False)],
)
def test_guard_deadline_boundary_without_sleep(
    flow, monkeypatch, deadline, elapsed, failed
):
    """Verify elapsed time must exceed a positive deadline to fail a node."""
    monkeypatch.setattr(
        flow_mod.time, "monotonic", Mock(side_effect=[20.0, 20.0 + elapsed])
    )
    flow._overrides = {"deadlines": {"sort": deadline}}
    work = Mock(return_value="sorted")
    if failed:
        with pytest.raises(NodeFailed, match="deadline_exceeded"):
            flow._guard_node("sort", 999, 0, work, (), {})
        assert flow._manifest.status == "failed"
    else:
        assert flow._guard_node("sort", 999, 0, work, (), {}) == "sorted"
        assert flow._manifest.completed_nodes == ["sort"]
    work.assert_called_once_with(flow)


def test_guard_skips_resumed_node_before_calling_work(flow):
    """Verify completed nodes skip execution, checkpointing and audit writes."""
    flow._resume_done = {"sort"}
    work = Mock(side_effect=AssertionError("completed work must not repeat"))
    assert flow._guard_node("sort", 1, 1, work, (), {}) is None
    work.assert_not_called()
    flow_mod.save_manifest.assert_not_called()
    flow_mod.audit_log.append.assert_not_called()


def test_guard_checkpoints_partial_state_on_unexpected_exception(flow):
    """Verify unexpected errors checkpoint partial progress and propagate unchanged."""
    flow._manifest.completed_nodes = ["ingest"]
    error = RuntimeError("provider unavailable")

    def work(instance):
        instance.state.extract_attempts = 1
        raise error

    with pytest.raises(RuntimeError) as exc:
        flow._guard_node("extract", 0, 0, work, (), {})
    assert exc.value is error
    assert flow._manifest.completed_nodes == ["ingest"]
    assert flow._manifest.state["extract_attempts"] == 1
    assert flow._manifest.status == "processing"
    flow_mod.save_manifest.assert_called_once_with(flow._bins, flow._manifest)
    flow_mod.audit_log.append.assert_not_called()


def test_retry_records_work_once_in_manifest_but_audits_each_attempt(flow):
    """Verify retries deduplicate completed nodes while recording each audit attempt."""
    work = Mock()
    for _ in range(2):
        flow._guard_node("sort", 0, 0, work, (), {})
    assert work.call_count == 2
    assert flow._manifest.completed_nodes == ["sort"]
    assert flow_mod.audit_log.append.call_count == 2


def test_failed_relocation_still_checkpoints_failure(flow):
    """Verify a failed file move still persists and audits the node failure."""
    flow._bins.move.side_effect = OSError("destination unavailable")
    with pytest.raises(NodeFailed, match="ingest_failed"):
        flow._fail_node("ingest", "ingest_failed")
    assert flow._manifest.status == flow._manifest.state["status"] == "failed"
    assert flow._manifest.state["path"] == "processing/letter.txt"
    flow_mod.save_manifest.assert_called_once()
    assert flow_mod.audit_log.append.call_args.args[2] == "node_failed"


def test_guarded_decorator_forwards_arguments_and_preserves_metadata():
    """Verify the node decorator forwards calls and preserves function metadata."""
    class Example:
        _guard_node = Mock(return_value="guard result")

        @guarded("example", deadline_s=12, token_budget=34)
        def node(self, value, *, attempt):
            """Node documentation."""
            raise AssertionError("the guard owns execution")

    example = Example()
    assert example.node("payload", attempt=2) == "guard result"
    example._guard_node.assert_called_once_with(
        "example", 12, 34, Example.node.__wrapped__, ("payload",), {"attempt": 2}
    )
    assert Example.node.__name__ == "node"
    assert Example.node.__doc__ == "Node documentation."
    assert Example.node.__mailroom_node__ == "example"


def test_verification_never_receives_eval_ground_truth(flow, monkeypatch):
    """Verify live verification excludes evaluation labels from judge and arbiter context."""
    flow._eval_ctx = SimpleNamespace(ground_truth={"doc-1": {"secret_label": "target"}})
    flow.state.eval_mode = True
    flow.state.extract = extraction()
    verdict = JudgeVerdict(label="partial", score=0.5)
    judge = Mock(return_value=verdict)
    arbiter = Mock(return_value=ArbiterDecision(action="accept"))
    monkeypatch.setattr(flow_mod, "judge_verify", judge)
    monkeypatch.setattr(flow_mod, "arbitrate", arbiter)
    flow._node_verify()
    ctx = judge.call_args.args[-1]
    assert ctx.eval_mode is False
    assert ctx.ground_truth is None
    assert ctx.doc_id == "doc-1"
    assert ctx.doc_text == "source text"
    judge.assert_called_once_with(
        "source text", "correspondence", {"sender": "Alice"}, ctx
    )
    arbiter.assert_called_once_with(
        "source text", "correspondence", {"sender": "Alice"}, verdict, ctx
    )
    assert flow.state.verdict == verdict


@dataclass
class DataclassTruth:
    answer: str


class ModelTruth(BaseModel):
    answer: str


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"answer": "value"}, {"answer": "value"}),
        (DataclassTruth("value"), {"answer": "value"}),
        (ModelTruth(answer="value"), {"answer": "value"}),
        ("unsupported", {}),
        (None, {}),
        (DataclassTruth, {}),
    ],
)
def test_eval_ground_truth_supported_formats(flow, row, expected):
    """Verify grading normalizes supported truth records and ignores unsupported values."""
    flow._eval_ctx = SimpleNamespace(ground_truth={"doc-1": row})
    fetch = flow._ground_truth_fn()
    assert fetch("doc-1") == expected
    assert fetch("missing") == {}


def test_grade_failure_does_not_fail_archived_document(flow, monkeypatch):
    """Verify grading failure preserves archived status and checkpoints the grade node."""
    flow.state.status = "archived"
    flow.state.extract = extraction()
    flow._eval_ctx = SimpleNamespace(ground_truth={"doc-1": {"sender": "Alice"}})
    judge = Mock(side_effect=RuntimeError("grading unavailable"))
    monkeypatch.setattr(flow_mod, "judge_grade", judge)
    flow._node_grade()
    ctx = judge.call_args.args[-1]
    assert ctx.eval_mode is True
    assert ctx.ground_truth("doc-1") == {"sender": "Alice"}
    assert flow.state.grade is None
    assert flow.state.status == "archived"
    assert "grade" in flow._manifest.completed_nodes


def test_live_document_does_not_invoke_grader(flow, monkeypatch):
    """Verify documents outside evaluation never call the grading judge."""
    judge = Mock(side_effect=AssertionError("live grading is forbidden"))
    monkeypatch.setattr(flow_mod, "judge_grade", judge)
    flow._node_grade()
    judge.assert_not_called()


def test_extraction_passes_overrides_and_accumulates_usage(flow, monkeypatch):
    """Verify extraction receives configured overrides and adds its token usage."""
    flow._overrides = {
        "doc_type": "merger_agreement",
        "doc_subclass": "public",
        "prompt_set": "sand37",
        "merger_mode": "maud",
    }
    flow.state.extract_attempts = 2
    flow.state.usage_total = Usage(prompt_tokens=10, calls=1)
    result = extraction(
        doc_type="merger_agreement", usage=Usage(completion_tokens=5, calls=2)
    )
    extract = Mock(return_value=result)
    monkeypatch.setattr(flow_mod, "_extract", extract)
    flow._node_extract()
    args, kwargs = extract.call_args
    assert args == ("source text", "merger_agreement", "public")
    assert kwargs["attempt"] == 2
    assert kwargs["prompt_set"] == "sand37"
    assert kwargs["cond"].merger_mode == "maud"
    assert flow.state.extract == result
    assert flow.state.usage_total == Usage(
        prompt_tokens=10, completion_tokens=5, calls=3
    )
    assert flow._llm_calls == 1


@pytest.mark.parametrize(
    "action,expected", [("accept", "archived"), ("human_review", "parked")]
)
def test_driver_handles_boss_terminal_decisions(flow, monkeypatch, action, expected):
    """Verify boss acceptance archives and human-review decisions park the document."""
    flow._resume_from = "boss"

    def boss():
        flow.state.boss = BossDecision(action=action)

    def archive():
        flow.state.status = "archived"

    monkeypatch.setattr(flow, "_node_boss", boss)
    archived = Mock(side_effect=archive)
    monkeypatch.setattr(flow, "_node_report_catalog_archive", archived)
    assert flow._drive().status == expected
    if action == "accept":
        archived.assert_called_once()
        assert flow.state.route_trail == ["boss", "report_catalog_archive"]
    else:
        archived.assert_not_called()
        assert flow.state.route_trail == ["boss", "human_review"]
        assert flow._manifest.status == "parked"
        assert flow_mod.audit_log.append.call_args.args[-1] == {
            "reason": "boss_human_review"
        }


def test_driver_reextracts_after_arbiter_request(flow, monkeypatch):
    """Verify an arbiter retry increments attempts and re-extracts before archival."""
    flow._resume_from = "gate_extract"
    routes = Mock(side_effect=["do_verify", "report"])
    monkeypatch.setattr(flow, "_extract_route", routes)

    def verify():
        flow.state.arbiter = ArbiterDecision(action="re_extract")

    monkeypatch.setattr(flow, "_node_verify", verify)
    extract = Mock()
    monkeypatch.setattr(flow, "extract", extract)
    archive = Mock(side_effect=lambda: setattr(flow.state, "status", "archived"))
    monkeypatch.setattr(flow, "_node_report_catalog_archive", archive)
    state = flow._drive()
    assert state.status == "archived"
    assert state.extract_attempts == 1
    extract.assert_called_once()
    archive.assert_called_once()
    assert state.route_trail == [
        "gate_extract",
        "verify",
        "extract",
        "gate_extract",
        "report_catalog_archive",
    ]


def test_explicit_resume_reexecutes_requested_completed_node(flow, monkeypatch):
    """Verify an explicit resume reruns extraction even when previously completed."""
    flow._manifest.completed_nodes = ["extract", "report_catalog_archive"]
    flow._resume_from = "extract"
    flow.state.extract = extraction()
    flow._gate.decide.return_value = SimpleNamespace(action="proceed")
    extract = Mock(return_value=extraction())
    monkeypatch.setattr(flow_mod, "_extract", extract)
    archive = Mock(side_effect=lambda: setattr(flow.state, "status", "archived"))
    monkeypatch.setattr(flow, "_node_report_catalog_archive", archive)
    assert flow._drive().status == "archived"
    extract.assert_called_once()
    archive.assert_called_once()
    assert flow._resume_done == set()


def test_catalog_failure_does_not_undo_successful_archive(flow, monkeypatch):
    """Verify catalog errors preserve the successful archive and its checkpoint."""
    result = ArchiveResult(
        Path("archive/letter.txt"), "sha256", Path("archive/letter.report.json")
    )
    archive = Mock(return_value=result)
    monkeypatch.setattr(flow_mod, "archive_document", archive)
    upsert = Mock(side_effect=OSError("catalog unavailable"))
    monkeypatch.setattr(flow_mod.catalog, "upsert", upsert)
    flow._llm_calls = 3
    flow._node_report_catalog_archive()
    archive.assert_called_once_with(flow._bins, flow._manifest, flow.state)
    record = upsert.call_args.args[0]
    assert record.status == "archived"
    assert record.archive_path == str(result.path)
    assert record.file_sha256 == "sha256"
    assert flow.state.status == flow._manifest.status == "archived"
    assert flow.state.report["llm_calls"] == 3
    assert flow._manifest.state["status"] == "archived"


@pytest.mark.parametrize("status", ["archived", "failed", "processing"])
@pytest.mark.parametrize("resume_from", [None, "extract"])
def test_configure_terminal_manifest_fresh_or_explicit_resume(flow, tmp_path, monkeypatch, status, resume_from):
    from mailroom_reloaded.pipeline.state import MailroomState
    from mailroom_reloaded.storage.bins import Bins, doc_id_for

    bins = Bins(tmp_path)
    path = bins.inbox / "rerun.txt"
    path.write_text("same content")
    manifest = Manifest(
        doc_id=doc_id_for(path), filename=path.name, status=status,
        content_sha256=flow_mod._sha256_file(path),
        completed_nodes=["ingest", "sort"],
        state=MailroomState(status=status, text="old text", classify_attempts=2).model_dump(mode="json"),
    )
    monkeypatch.setattr(flow_mod, "load_manifest", lambda *_: manifest)
    monkeypatch.setattr(flow_mod, "load_gate", Mock())
    flow._configure(path, "worker", resume_from, {"bins": bins}, None)
    if resume_from is None and status in {"archived", "failed"}:
        assert flow._manifest is not manifest
        assert flow._manifest.completed_nodes == []
        assert flow.state.classify_attempts == 0
        assert flow.state.status != status
        assert flow._resume_start() == "ingest"
    else:
        assert flow._manifest is manifest
        assert flow.state.text == "old text"
        assert flow.state.classify_attempts == 2
