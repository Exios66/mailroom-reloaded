"""Evaluation error isolation and ground-truth boundaries without model calls."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, text

from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.eval import runner
from mailroom_reloaded.eval.dataset import BlindDoc, GroundTruth, sha256_text
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline.state import MailroomState


@pytest.fixture
def engine():
    """Yield an in-memory evaluation database and dispose it after the test."""
    engine = create_engine("sqlite:///:memory:")
    runner._ensure_table(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def document():
    """Return a blind document with a hash matching its synthetic text."""
    return BlindDoc("letter.txt", "blind source", sha256_text("blind source"))


def rows(engine):
    """Read all evaluation rows as mappings ordered by filename."""
    with engine.connect() as conn:
        return list(
            conn.execute(text("SELECT * FROM eval_docs ORDER BY filename")).mappings()
        )


@pytest.mark.parametrize(
    "selected,has_truth", [(False, True), (True, False), (True, True)]
)
async def test_pipeline_exposes_truth_only_when_selected(
    engine, document, monkeypatch, tmp_path, selected, has_truth
):
    """Verify evaluation context requires both grading selection and available truth."""
    truth = GroundTruth(
        document.filename, expected="correspondence", fields={"sender": "GT canary"}
    )
    gts = {document.filename: truth} if has_truth else {}
    kickoff = AsyncMock(return_value=MailroomState(status="archived"))
    monkeypatch.setattr(runner.flow_mod.MailroomFlow, "kickoff_async", kickoff)
    monkeypatch.setattr(
        runner, "_write_doc", Mock(return_value=tmp_path / document.filename)
    )
    graded = {document.filename} if selected else set()
    await runner._run_pipeline(
        runner.EvalConfig(), "run", document, gts, graded, asyncio.Semaphore(1), engine
    )
    inputs = kickoff.call_args.kwargs["inputs"]
    assert inputs["overrides"] == {"prompt_set": "frozen_v1", "merger_mode": "frozen"}
    if selected and has_truth:
        assert inputs["eval_ctx"].ground_truth == {document.content_sha256[:16]: truth}
    else:
        assert inputs["eval_ctx"] is None
    stored = rows(engine)
    assert len(stored) == 1
    assert stored[0]["status"] == "archived"
    assert stored[0]["graded"] == int(selected and has_truth)


@pytest.mark.parametrize("mode", ["pipeline", "specialist_cell"])
async def test_document_error_is_recorded_and_next_document_runs(
    engine, document, monkeypatch, tmp_path, mode
):
    """Verify each mode records a bounded error and continues with the next document."""
    other = BlindDoc("other.txt", "other source", sha256_text("other source"))
    failure = RuntimeError("x" * 600)
    if mode == "pipeline":
        worker = AsyncMock(side_effect=[failure, MailroomState(status="archived")])
        monkeypatch.setattr(runner.flow_mod.MailroomFlow, "kickoff_async", worker)
        monkeypatch.setattr(runner, "_write_doc", lambda doc: tmp_path / doc.filename)
    else:
        result = ExtractResult("unknown", {}, True, None, 1.0, None, 1, Usage(calls=1))
        worker = Mock(side_effect=[failure, result])
        monkeypatch.setattr(runner, "_extract", worker)
    await runner._run_all(
        runner.EvalConfig(mode=mode, concurrency=1),
        "run",
        [document, other],
        {},
        set(),
        engine,
    )
    failed, succeeded = rows(engine)
    assert failed["status"] == "error"
    assert failed["error_kind"] == "RuntimeError"
    assert failed["parse_error"] == "x" * 500
    assert succeeded["status"] == ("archived" if mode == "pipeline" else "ok")
    assert worker.call_count == 2


async def test_specialist_cell_receives_labels_but_never_ground_truth_fields(
    engine, document, monkeypatch
):
    """Verify cell inputs exclude truth fields while failure details are persisted."""
    truth = GroundTruth(
        document.filename,
        expected="merger_agreement",
        expected_subclass="public",
        fields={"secret": "GT canary"},
    )
    result = ExtractResult(
        "merger_agreement",
        None,
        False,
        "truncated",
        0.2,
        "LengthFinishReasonError",
        0,
        Usage(prompt_tokens=7, completion_tokens=3, calls=2),
    )
    extract = Mock(return_value=result)
    monkeypatch.setattr(runner, "_extract", extract)
    cfg = runner.EvalConfig(
        mode="specialist_cell", prompt_set="sand37", merger_mode="maud"
    )
    await runner._run_cell(
        cfg, "run", document, {document.filename: truth}, asyncio.Semaphore(1), engine
    )
    args, kwargs = extract.call_args
    assert args == ("blind source", "merger_agreement", "public")
    assert set(kwargs) == {"prompt_set", "cond"}
    assert kwargs["prompt_set"] == "sand37"
    assert kwargs["cond"].merger_mode == "maud"
    stored = rows(engine)[0]
    assert stored["mode"] == "specialist_cell"
    assert stored["graded"] == 0
    assert stored["status"] == "error"
    assert stored["schema_valid"] == 0
    assert stored["length_capped"] == 1
    assert stored["error_kind"] == "LengthFinishReasonError"
    assert stored["parse_error"] == "truncated"
    assert (stored["prompt_tokens"], stored["completion_tokens"], stored["calls"]) == (
        7,
        3,
        2,
    )


def test_empty_eval_returns_run_id_without_calling_pipeline(engine, monkeypatch):
    """Verify an empty evaluation creates a run ID without invoking the flow."""
    monkeypatch.setattr(runner, "load_split", lambda *a, **k: ([], {}))
    monkeypatch.setattr(runner, "_engine", lambda: engine)
    kickoff = AsyncMock(side_effect=AssertionError("empty run must not call model"))
    monkeypatch.setattr(runner.flow_mod.MailroomFlow, "kickoff_async", kickoff)
    run_id = runner.run_eval(runner.EvalConfig())
    assert len(run_id) == 12
    int(run_id, 16)
    assert rows(engine) == []
    kickoff.assert_not_called()


def test_missing_predictions_never_use_ground_truth(engine, document):
    from types import SimpleNamespace

    from mailroom_reloaded.eval.metrics import sorter_kpis

    truth = GroundTruth(document.filename, expected="contract")
    for state in (None, SimpleNamespace(sort=None, extract=None)):
        assert runner._state_doc_type(state) is None
    predicted = SimpleNamespace(doc_type="correspondence")
    assert runner._state_doc_type(SimpleNamespace(sort=predicted)) == "correspondence"
    assert runner._state_doc_type(SimpleNamespace(extract=predicted)) == "correspondence"
    row = runner._base_row("run", document, truth, mode="pipeline", latency_s=0, graded=False)
    runner._insert(engine, row)
    assert rows(engine)[0]["doc_type"] is None
    assert sorter_kpis([{**row, "expected_doc_type": truth.expected}])["primary_accuracy"] == 0


async def test_specialist_cells_extract_concurrently_off_event_loop(engine, document, monkeypatch):
    import threading

    loop_thread = threading.get_ident()
    barrier = threading.Barrier(2, timeout=5)
    worker_threads = []

    def extract(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        barrier.wait()
        return ExtractResult("unknown", {}, True, None, 1.0, None, 1, Usage(calls=1))

    monkeypatch.setattr(runner, "_extract", extract)
    other = BlindDoc("other.txt", "other", sha256_text("other"))
    await runner._run_all(
        runner.EvalConfig(mode="specialist_cell", concurrency=2),
        "run", [document, other], {}, set(), engine,
    )
    assert len(worker_threads) == 2
    assert loop_thread not in worker_threads
    assert all(row["status"] == "ok" for row in rows(engine))
