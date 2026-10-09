"""Task 16 tests: the deterministic MailroomFlow driver.

The pipeline uses the ``mock`` provider through ``FakeOpenAI`` for the two
standalone LLM calls (sorter, specialist). BERT and the CrewAI judge/arbiter/boss
agents are monkeypatched where a scenario needs them, so the tests stay fast and
deterministic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fakes.openai_server import FakeOpenAI

from mailroom_reloaded.agents.arbiter import ArbiterDecision
from mailroom_reloaded.agents.boss import BossDecision
from mailroom_reloaded.agents.judge import JudgeVerdict
from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.storage import audit_log
from mailroom_reloaded.storage.bins import Bins

CORR_SUBCLASS = {
    "doc_subclass": "email",
    "confidence": 0.99,
    "doc_type_disagree": False,
    "doc_type_disagree_reason": None,
}

CORR_FULL = {
    "doc_type": "correspondence",
    "doc_subclass": "email",
    "confidence": 0.99,
    "doc_type_disagree": False,
    "doc_type_disagree_reason": None,
}

# Every correspondence field non-empty so the deterministic extraction coverage
# is 1.0 and gate_extract proceeds.
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

FAST_TRAIL = [
    "ingest",
    "bert_primary",
    "sort",
    "gate_classify",
    "extract",
    "gate_extract",
    "report_catalog_archive",
]


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


def _patch_handoff(monkeypatch, mode=SortMode.SUBCLASS_ONLY, doc_type="correspondence", route="fast_path"):
    """Stub BERT classification and handoff for a deterministic routing scenario."""
    locked = doc_type if mode is SortMode.SUBCLASS_ONLY else None
    verdict = BertVerdict(
        available=True,
        reason="ok",
        doc_type=doc_type,
        subclass=None,
        calibrated_confidence=0.99,
        margin=0.5,
        window_agreement=1.0,
        n_windows=1,
        route=route,
    )
    handoff = Handoff(mode, locked, f"BERT predicts class {doc_type}", route)
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None, *, filename=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)
    return verdict, handoff


def _patch_bert_unavailable(monkeypatch):
    """Force full LLM sorting by simulating disabled BERT inference."""
    verdict = BertVerdict(available=False, reason="flag_off")
    handoff = Handoff(SortMode.FULL, None, "", "bert_unavailable:flag_off")
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None, *, filename=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)


def _reply(provider, payload):
    """Queue a discarded tool-round draft then the final structured reply."""
    provider.reply("thinking").reply(json.dumps(payload))


def _write_inbox(base, text="A short business letter about the deal."):
    """Write a test document to the inbox and return its bins and path."""
    bins = Bins(base)
    path = bins.inbox / "letter.txt"
    path.write_text(text)
    return bins, path


def _fake_extract(confidence=1.0, data=None):
    """Build an extractor stub with fixed confidence, data and token usage."""
    def run(text, doc_type, doc_subclass, **kwargs):
        return ExtractResult(
            doc_type,
            data if data is not None else {},
            True,
            None,
            confidence,
            None,
            1,
            Usage(prompt_tokens=5, completion_tokens=5, calls=1),
        )

    return run


# --------------------------------------------------------------------------- tests


def test_fast_path_two_llm_calls(env, mock_provider, monkeypatch):
    """Verify the BERT fast path archives with two agent calls and the expected route."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    _reply(mock_provider, CORR_EXTRACT)
    bins, path = _write_inbox(env)

    state = flow_mod.run_document(path, worker_id="w1")

    assert state.route_trail == FAST_TRAIL
    assert state.status == "archived"
    assert state.report["llm_calls"] == 2
    assert state.report["usage"]["calls"] == 4  # 2 role calls, each with a tool round
    archived = list((bins.archive / "correspondence").glob("*.txt"))
    assert len(archived) == 1
    assert archived[0].read_text() == "A short business letter about the deal."


def test_contract_deferred_full_sort(env, mock_provider, monkeypatch):
    """Verify deferred contracts receive full sorting and archive under their class."""
    _patch_handoff(monkeypatch, mode=SortMode.FULL, doc_type="contract", route="defer_class")
    _reply(
        mock_provider,
        {
            "doc_type": "contract",
            "doc_subclass": "license",
            "confidence": 0.99,
            "doc_type_disagree": False,
            "doc_type_disagree_reason": None,
        },
    )
    monkeypatch.setattr(
        flow_mod,
        "_extract",
        _fake_extract(confidence=0.99, data={"document_name": "License"}),
    )
    bins, path = _write_inbox(env)

    state = flow_mod.run_document(path, worker_id="w1")

    assert state.sort is not None
    assert state.sort.mode is SortMode.FULL
    assert state.sort.doc_type == "contract"
    assert state.status == "archived"
    assert list((bins.archive / "contract").glob("*.txt"))


def test_subclass_disagree_resorts_once(env, mock_provider, monkeypatch):
    """Verify subclass disagreement triggers one full re-sort before archival."""
    _patch_handoff(monkeypatch)
    _reply(
        mock_provider,
        {
            "doc_subclass": "memo",
            "confidence": 0.99,
            "doc_type_disagree": True,
            "doc_type_disagree_reason": "looks like a contract",
        },
    )
    _reply(mock_provider, CORR_FULL)
    monkeypatch.setattr(flow_mod, "_extract", _fake_extract(confidence=1.0))
    bins, path = _write_inbox(env)

    state = flow_mod.run_document(path, worker_id="w1")

    assert state.resorted is True
    assert state.route_trail.count("sort") == 2
    assert state.sort is not None and state.sort.mode is SortMode.FULL
    assert state.report["llm_calls"] == 3
    assert state.status == "archived"
    assert list((bins.archive / "correspondence").glob("*.txt"))


def test_classify_low_conf_retries_then_parks(env, mock_provider, monkeypatch):
    """Verify persistent low classification confidence exhausts retries and parks."""
    _patch_bert_unavailable(monkeypatch)
    low = {
        "doc_type": "correspondence",
        "doc_subclass": "email",
        "confidence": 0.5,
        "doc_type_disagree": False,
        "doc_type_disagree_reason": None,
    }
    for _ in range(3):
        _reply(mock_provider, low)
    bins, path = _write_inbox(env)

    state = flow_mod.run_document(path, worker_id="w1")

    assert state.status == "parked"
    assert state.classify_attempts == 2
    assert state.report is None
    assert list(bins.review.glob("*.txt"))


def test_extract_medium_band_verifies(env, mock_provider, monkeypatch):
    """Verify medium extraction confidence invokes verification and retains caveats."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    # correspondence low=0.85, judge_band_high=0.94: 0.90 lands in the verify band
    monkeypatch.setattr(flow_mod, "_extract", _fake_extract(confidence=0.90))
    monkeypatch.setattr(
        flow_mod,
        "judge_verify",
        lambda *a, **k: JudgeVerdict(label="partial", score=0.6, field_findings=[]),
    )
    monkeypatch.setattr(
        flow_mod,
        "arbitrate",
        lambda *a, **k: ArbiterDecision(
            action="accept_with_caveats", caveats=["date format differs"]
        ),
    )
    bins, path = _write_inbox(env)

    state = flow_mod.run_document(path, worker_id="w1")

    assert "verify" in state.route_trail
    assert state.arbiter is not None
    assert state.arbiter.action == "accept_with_caveats"
    assert state.report["caveats"] == ["date format differs"]
    assert state.status == "archived"
    assert list((bins.archive / "correspondence").glob("*.txt"))


def test_extract_low_conf_boss_reassign(env, mock_provider, monkeypatch):
    """Verify boss reassignment retries extraction under the corrected class."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    seen: list[str] = []

    def fake_extract(text, doc_type, doc_subclass, **kwargs):
        seen.append(doc_type)
        confidence = 0.99 if doc_type == "insurance_claim" else 0.4
        return ExtractResult(
            doc_type,
            {"field": "value"},
            True,
            None,
            confidence,
            None,
            1,
            Usage(prompt_tokens=5, completion_tokens=5, calls=1),
        )

    monkeypatch.setattr(flow_mod, "_extract", fake_extract)
    monkeypatch.setattr(
        flow_mod,
        "escalate",
        lambda *a, **k: BossDecision(
            action="reassign_class",
            doc_type="insurance_claim",
            doc_subclass="fnol",
            reason="coverage-denial paperwork",
        ),
    )
    bins, path = _write_inbox(env)

    state = flow_mod.run_document(path, worker_id="w1")

    assert seen[0] == "correspondence"
    assert seen[-1] == "insurance_claim"
    assert state.boss is not None and state.boss.action == "reassign_class"
    assert state.status == "archived"
    assert list((bins.archive / "insurance_claim").glob("*.txt"))


def test_resume_skips_completed_nodes(env, mock_provider, monkeypatch):
    """Verify crash recovery skips completed sorting and preserves the audit chain."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)

    def boom(*a, **k):
        raise RuntimeError("boom after sort")

    monkeypatch.setattr(flow_mod, "_extract", boom)
    bins, path = _write_inbox(env)

    with pytest.raises(RuntimeError):
        flow_mod.run_document(path, worker_id="w1")

    processing = list(bins.processing("w1").glob("*.txt"))
    assert len(processing) == 1
    processing_path = processing[0]

    real_sort = flow_mod._sort
    calls = {"sort": 0}

    def counting_sort(text, handoff, attempt=0, **kwargs):
        calls["sort"] += 1
        return real_sort(text, handoff, attempt=attempt, **kwargs)

    monkeypatch.setattr(flow_mod, "_sort", counting_sort)
    monkeypatch.setattr(flow_mod, "_extract", _fake_extract(confidence=1.0))

    state = flow_mod.run_document(processing_path, worker_id="w1")

    assert calls["sort"] == 0  # sort was skipped on resume
    assert state.status == "archived"
    entries = audit_log.entries(state.doc_id)
    assert sum(1 for entry in entries if entry.node == "sort") == 1
    assert audit_log.verify_chain(entries).ok


def test_deadline_guard_fails_doc(env, monkeypatch):
    """Verify a sorter exceeding its deadline moves the document to failed."""
    _patch_handoff(monkeypatch)

    def slow_sort(text, handoff, attempt=0, **kwargs):
        time.sleep(0.02)
        from mailroom_reloaded.agents.sorter import SortResult

        return SortResult(
            "correspondence",
            "email",
            0.99,
            0.9,
            False,
            handoff.mode,
            "self_report",
            False,
            None,
            Usage(prompt_tokens=1, completion_tokens=1, calls=1),
        )

    monkeypatch.setattr(flow_mod, "_sort", slow_sort)
    bins, path = _write_inbox(env)

    state = flow_mod.run_document(
        path, worker_id="w1", overrides={"deadlines": {"sort": 0.001}}
    )

    assert state.status == "failed"
    assert list(bins.failed.glob("*.txt"))


async def test_kickoff_async_runs_document(env, mock_provider, monkeypatch):
    """Verify asynchronous kickoff follows the fast path through archival."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    _reply(mock_provider, CORR_EXTRACT)
    bins, path = _write_inbox(env)

    state = await flow_mod.MailroomFlow().kickoff_async(
        inputs={"path": path, "worker_id": "w1"}
    )

    assert state.status == "archived"
    assert state.route_trail == FAST_TRAIL
    assert list((bins.archive / "correspondence").glob("*.txt"))


def test_gate_retry_after_crash_resume_reexecutes_extract(env, mock_provider, monkeypatch):
    """Finding 10: a gate-driven retry of an already-completed node must re-run it."""
    _patch_handoff(monkeypatch)
    _reply(mock_provider, CORR_SUBCLASS)
    calls = {"n": 0}

    def extract(text, doc_type, doc_subclass, **kwargs):
        calls["n"] += 1
        return _fake_extract(confidence=0.1 if calls["n"] == 1 else 1.0)(
            text, doc_type, doc_subclass, **kwargs
        )

    monkeypatch.setattr(flow_mod, "_extract", extract)
    real_route = flow_mod.MailroomFlow._extract_route
    crashed = {"done": False}

    def crash_once(self):
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("crash after first extract")
        return real_route(self)

    monkeypatch.setattr(flow_mod.MailroomFlow, "_extract_route", crash_once)
    bins, path = _write_inbox(env)
    with pytest.raises(RuntimeError):
        flow_mod.run_document(path, worker_id="w1")
    processing = list(bins.processing("w1").glob("*.txt"))
    assert calls["n"] == 1

    state = flow_mod.run_document(processing[0], worker_id="w1")

    assert calls["n"] == 2  # the gate-requested retry really re-executed extract
    assert state.extract_attempts == 1
    assert state.status == "archived"


def test_gate_retry_after_crash_resume_reexecutes_sort(env, mock_provider, monkeypatch):
    """Finding 10: resume replays the gate; its retry must run sort, not burn an attempt."""
    from mailroom_reloaded.agents.sorter import SortResult

    _patch_handoff(monkeypatch)
    calls = {"n": 0}

    def sort(text, handoff, attempt=0, **kwargs):
        calls["n"] += 1
        conf = 0.1 if calls["n"] == 1 else 0.99
        return SortResult(
            "correspondence", "email", conf, 0.9, False, handoff.mode,
            "self_report", False, None,
            Usage(prompt_tokens=1, completion_tokens=1, calls=1),
        )

    monkeypatch.setattr(flow_mod, "_sort", sort)
    monkeypatch.setattr(flow_mod, "_extract", _fake_extract(confidence=1.0))
    real_route = flow_mod.MailroomFlow._classify_route
    crashed = {"done": False}

    def crash_once(self):
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("crash after first sort")
        return real_route(self)

    monkeypatch.setattr(flow_mod.MailroomFlow, "_classify_route", crash_once)
    bins, path = _write_inbox(env)
    with pytest.raises(RuntimeError):
        flow_mod.run_document(path, worker_id="w1")
    processing = list(bins.processing("w1").glob("*.txt"))

    state = flow_mod.run_document(processing[0], worker_id="w1")

    assert calls["n"] == 2
    assert state.classify_attempts == 1  # one retry, not a skipped one plus a real one
    assert state.status == "archived"


def test_initial_checkpoint_exists_before_ingest(env, monkeypatch):
    from mailroom_reloaded.storage.bins import doc_id_for, load_manifest

    bins, path = _write_inbox(env)
    doc_id = doc_id_for(path)

    def crash(*args):
        checkpoint = load_manifest(bins, doc_id)
        assert checkpoint is not None
        assert Path(checkpoint.state['path']).is_file()
        raise RuntimeError('ingest interruption')

    monkeypatch.setattr(flow_mod, '_ingest', crash)
    with pytest.raises(RuntimeError):
        flow_mod.run_document(path, worker_id='w1')
    manifest = load_manifest(bins, doc_id)
    assert manifest is not None
    assert manifest.status == 'processing'
    assert Path(manifest.state['path']).is_file()
