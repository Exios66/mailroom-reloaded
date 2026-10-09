"""Archive-ledger intake: one ``doc_closed`` per invocation, run open/close, best effort."""

from __future__ import annotations

import json

import pytest
from fakes.openai_server import FakeOpenAI

from mailroom_reloaded.agents.arbiter import ArbiterDecision
from mailroom_reloaded.agents.judge import JudgeVerdict
from mailroom_reloaded.agents.sorter import SortResult
from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.pipeline import run_ledger
from mailroom_reloaded.storage import audit_log
from mailroom_reloaded.storage.bins import Bins
from mailroom_reloaded.storage.ledger import get_ledger, reset_ledger

CORR_SUBCLASS = {
    "doc_subclass": "email",
    "confidence": 0.99,
    "doc_type_disagree": False,
    "doc_type_disagree_reason": None,
}
U = Usage(prompt_tokens=5, completion_tokens=5, calls=1)


def _reset_registry() -> None:
    for registry in (run_ledger._open, run_ledger._invocations):
        registry.clear()
    for registry in (run_ledger._closed, run_ledger._rolled):
        registry.clear()


@pytest.fixture
def fake_openai():
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("MAILROOM_RUN_ID", raising=False)
    from mailroom_reloaded import settings
    from mailroom_reloaded.storage import db

    settings.get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    _reset_registry()
    try:
        yield tmp_path
    finally:
        reset_ledger()
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        _reset_registry()
        settings.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    from mailroom_reloaded.llm import retry, tooling

    monkeypatch.setattr(retry, "_sleep", lambda *_: None)
    tooling.reset_tool_support_cache()
    yield
    tooling.reset_tool_support_cache()


def _patch_handoff(monkeypatch):
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


def _extract(confidence=1.0):
    def run(text, doc_type, doc_subclass, **kwargs):
        return ExtractResult(doc_type, {"a": 1}, True, None, confidence, None, 1, U)

    return run


def _inbox(base, name="letter.txt", text="A short business letter about the deal."):
    bins = Bins(base)
    path = bins.inbox / name
    path.write_text(text)
    return bins, path


def _entries(**kw):
    lg = get_ledger()
    assert lg.flush()
    return lg.entries(limit=1000, **kw)


def test_completed_document_is_recorded_with_audit_head_and_usage(
    env, monkeypatch
) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extract())
    _, path = _inbox(env)

    state = flow_mod.run_document(path, worker_id="w1")

    es = _entries()
    assert [e.kind for e in es] == ["run_opened", "doc_closed"]
    opened, closed = es
    assert opened.payload["kind"] == "live" and opened.run_id.startswith("live-")
    p = closed.payload
    assert (
        closed.doc_id == state.doc_id
        and p["outcome"] == "completed"
        and p["invocation"] == 1
    )
    chain = audit_log.entries(state.doc_id)
    assert p["audit_head"] == {"seq": chain[-1].seq, "entry_hash": chain[-1].entry_hash}
    assert set(p["usage_by_role"]) == {"sorter", "correspondence_specialist"}
    assert p["usage_complete"] is True and p["usage_partial_nodes"] == []
    assert p["rows"] > 0 and get_ledger().metric_rows(opened.run_id)
    assert get_ledger().verify().ok


def test_second_document_reuses_the_open_live_run(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extract())
    for name, text in (("a.txt", "first letter"), ("b.txt", "second letter")):
        _, path = _inbox(env, name, text)
        flow_mod.run_document(path, worker_id="w1")
    kinds = [e.kind for e in _entries()]
    assert kinds == ["run_opened", "doc_closed", "doc_closed"]


def test_failed_document_records_a_bounded_reason(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    _, path = _inbox(env)
    state = flow_mod.run_document(
        path, worker_id="w1", overrides={"deadlines": {"sort": -0.0 + 1e-9}}
    )
    assert state.status == "failed"
    closed = _entries(kind="doc_closed")[0].payload
    assert closed["outcome"] == "failed"
    assert (
        closed["failure_reason"] == "deadline_exceeded"
        and closed["failure_class"] == "run_budget"
    )


def test_parked_document_is_recorded(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(
        flow_mod.MailroomFlow, "_classify_route", lambda self: "human_review"
    )
    _, path = _inbox(env)
    state = flow_mod.run_document(path, worker_id="w1")
    assert state.status == "parked"
    closed = _entries(kind="doc_closed")[0].payload
    assert closed["outcome"] == "parked" and "failure_reason" not in closed


def test_crashing_node_records_aborted_and_reraises_the_original(
    env, monkeypatch
) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)

    def boom(*a, **k):
        raise RuntimeError("provider /home/alice/secret.pdf exploded")

    monkeypatch.setattr(flow_mod, "_extract", boom)
    _, path = _inbox(env)
    with pytest.raises(RuntimeError, match="exploded"):
        flow_mod.run_document(path, worker_id="w1")
    closed = _entries(kind="doc_closed")[0].payload
    assert closed["outcome"] == "aborted" and closed["failure_class"] == "unexpected"
    assert closed["usage_complete"] is False and closed["usage_partial_nodes"] == [
        "extract"
    ]
    assert closed["usage_by_role"].keys() == {"sorter"}
    assert "alice" not in json.dumps(closed)


def test_resumed_document_reports_per_invocation_deltas(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)

    def boom(*a, **k):
        raise RuntimeError("crash")

    monkeypatch.setattr(flow_mod, "_extract", boom)
    bins, path = _inbox(env)
    with pytest.raises(RuntimeError):
        flow_mod.run_document(path, worker_id="w1")
    processing = next(iter(bins.processing("w1").glob("*.txt")))

    monkeypatch.setattr(flow_mod, "_extract", _extract())
    state = flow_mod.run_document(processing, worker_id="w1")

    first, second = [e.payload for e in _entries(kind="doc_closed")]
    assert (first["invocation"], second["invocation"]) == (1, 2)
    assert first["usage_by_role"].keys() == {"sorter"}
    assert second["usage_by_role"].keys() == {
        "correspondence_specialist"
    }  # sort was not repeated
    spent = sum(
        r["prompt_tokens"] for p in (first, second) for r in p["usage_by_role"].values()
    )
    assert spent == state.usage_total.prompt_tokens  # nothing counted twice


def test_judge_boss_and_arbiter_spend_lands_in_the_entry(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extract(confidence=0.90))  # verify band

    def judge(text, doc_type, data, ctx):
        ctx.usage_sink.append(("judge", U))
        return JudgeVerdict(label="complete", score=0.9, field_findings=[])

    def arbiter(text, doc_type, data, verdict, ctx):
        ctx.usage_sink.append(("arbiter", U))
        return ArbiterDecision(action="accept")

    monkeypatch.setattr(flow_mod, "judge_verify", judge)
    monkeypatch.setattr(flow_mod, "arbitrate", arbiter)
    _, path = _inbox(env)
    flow_mod.run_document(path, worker_id="w1")
    roles = _entries(kind="doc_closed")[0].payload["usage_by_role"]
    assert {"sorter", "judge", "arbiter"} <= roles.keys()


def test_hostile_strings_never_reach_the_ledger(env, monkeypatch) -> None:
    hostile = "Alice Smith SSN 123-45-6789"  # no suffix: the ingest error text embeds the name
    bins = Bins(env)
    path = bins.inbox / hostile
    path.write_text("body text that must stay out")
    state = flow_mod.run_document(path, worker_id="w1")
    assert state.status == "failed"
    blob = json.dumps([e.model_dump() for e in _entries()])
    for leaked in ("Alice", "123-45", "body text"):
        assert leaked not in blob
    closed = _entries(kind="doc_closed")[0].payload
    assert closed["failure_reason"] == "ingest_failed"


def test_a_broken_ledger_never_changes_the_outcome(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extract())
    monkeypatch.setattr(
        run_ledger,
        "ledger_for",
        lambda overrides: (_ for _ in ()).throw(OSError("disk full")),
    )
    _, path = _inbox(env)
    state = flow_mod.run_document(path, worker_id="w1")
    assert state.status == "archived"


def test_a_failing_append_never_masks_the_documents_exception(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)

    def boom(*a, **k):
        raise ValueError("the real error")

    monkeypatch.setattr(flow_mod, "_extract", boom)
    ledger = get_ledger()
    monkeypatch.setattr(
        ledger,
        "append",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )
    _, path = _inbox(env)
    with pytest.raises(ValueError, match="the real error"):
        flow_mod.run_document(path, worker_id="w1")


def test_rollover_closes_the_previous_live_run(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extract())
    monkeypatch.setenv("MAILROOM_RUN_ID", "day-1")
    _, path = _inbox(env, "a.txt", "first")
    flow_mod.run_document(path, worker_id="w1")
    monkeypatch.setenv("MAILROOM_RUN_ID", "day-2")
    _, path = _inbox(env, "b.txt", "second")
    flow_mod.run_document(path, worker_id="w1")
    es = _entries()
    closed = [e for e in es if e.kind == "run_closed"]
    assert [(e.run_id, e.payload["closed_by"], e.payload["docs"]) for e in closed] == [
        ("day-1", "completed", 1)
    ]
    assert get_ledger().open_runs("live") == ["day-2"]
    assert get_ledger().verify("day-1").merkle_ok is True


def test_startup_closes_runs_a_dead_process_left_open_but_not_todays(
    env, monkeypatch
) -> None:
    from mailroom_reloaded.obs.run_context import live_run_id
    from mailroom_reloaded.watcher import Watcher

    lg = get_ledger()
    lg.append("run_opened", "live-19990101", payload={"kind": "live"})
    lg.append("run_opened", live_run_id(), payload={"kind": "live"})
    lg.append("run_opened", "evalrun", payload={"kind": "eval"})
    lg.flush()
    Watcher(Bins(env), worker_id="w1").resume_processing()
    lg.flush()
    closed = {e.run_id: e.payload["closed_by"] for e in lg.entries(kind="run_closed")}
    assert closed == {"live-19990101": "interrupted"}
    assert sorted(lg.open_runs()) == sorted([live_run_id(), "evalrun"])


def test_reconcile_archived_records_a_reconciled_document(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extract())
    bins, path = _inbox(env)
    state = flow_mod.run_document(path, worker_id="w1")
    from mailroom_reloaded.storage.bins import load_manifest

    manifest = load_manifest(bins, state.doc_id)
    manifest.status = "processing"
    assert flow_mod.reconcile_archived(bins, manifest) is True
    outcomes = [e.payload["outcome"] for e in _entries(kind="doc_closed")]
    assert outcomes == ["completed", "reconciled"]
    assert _entries(kind="doc_closed")[1].payload["usage_complete"] is False


def test_a_failed_run_opened_is_retried_by_the_next_document(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extract())
    ledger = get_ledger()
    real_append = ledger.append
    calls = {"n": 0}

    def flaky(kind, *a, **k):
        if kind == "run_opened":
            calls["n"] += 1
            if calls["n"] == 1:
                return False
        return real_append(kind, *a, **k)

    monkeypatch.setattr(ledger, "append", flaky)
    for name, text in (("a.txt", "first"), ("b.txt", "second")):
        _, path = _inbox(env, name, text)
        flow_mod.run_document(path, worker_id="w1")
    kinds = [e.kind for e in _entries()]
    assert kinds.count("run_opened") == 1 and kinds.count("doc_closed") == 2


def test_a_closed_run_is_never_reopened_or_appended_to(env, monkeypatch) -> None:
    _patch_handoff(monkeypatch)
    monkeypatch.setattr(flow_mod, "_sort", _sort)
    monkeypatch.setattr(flow_mod, "_extract", _extract())
    monkeypatch.setenv("MAILROOM_RUN_ID", "pilot")
    _, path = _inbox(env, "a.txt", "first")
    flow_mod.run_document(path, worker_id="w1")
    run_ledger.close_run(
        get_ledger(), "pilot", "completed"
    )  # e.g. rolled over by another process
    _, path = _inbox(env, "b.txt", "second")
    flow_mod.run_document(path, worker_id="w1")
    es = _entries()
    pilot = [e.kind for e in es if e.run_id == "pilot"]
    assert pilot == [
        "run_opened",
        "doc_closed",
        "run_closed",
    ]  # one pair, nothing after the close
    later = [e for e in es if e.run_id != "pilot"]
    assert [e.kind for e in later] == ["run_opened", "doc_closed"] and later[
        0
    ].run_id.startswith("live-")
    assert get_ledger().verify().ok


def test_an_id_closed_in_the_ledger_by_another_process_is_not_reopened(env) -> None:
    lg = get_ledger()
    lg.append("run_opened", "old", payload={"kind": "live"})
    lg.append("run_closed", "old", payload={"closed_by": "completed"})
    lg.flush()
    assert run_ledger.open_run(lg, "old", "live") is False
    assert [e.kind for e in lg.entries(run_id="old")] == ["run_opened", "run_closed"]
