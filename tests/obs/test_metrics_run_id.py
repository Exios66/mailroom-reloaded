"""Every metric data point carries ``run_id`` and ``environment`` (trace-replay Task 4)."""

from __future__ import annotations

import datetime as dt

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from mailroom_reloaded.agents.sorter import SortResult
from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.obs import metrics as metrics_mod
from mailroom_reloaded.obs.metrics import M, run_labels, setup_metrics
from mailroom_reloaded.obs.run_context import run_scope
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.storage.bins import Bins
from mailroom_reloaded.watcher import Watcher

U = Usage(prompt_tokens=5, completion_tokens=5, calls=1)
#: the only attribute keys any data point may carry (cardinality guard: never doc_id)
ALLOWED_KEYS = {
    "run_id", "environment", "stage", "status", "doc_type", "node", "role", "provider", "model",
    "decision", "route", "bin", "worker", "kind", "to", "cause",
    "gen_ai.request.model", "gen_ai.provider.name", "gen_ai.token.type", "gen_ai.operation.name",
}  # fmt: skip


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
def reader():
    metrics_mod._reset_for_tests()
    r = InMemoryMetricReader()
    setup_metrics(reader=r)
    return r


def _points(reader: InMemoryMetricReader):
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    yield metric.name, dict(point.attributes)


def _patch(monkeypatch, confidences=(1.0,)):
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
    monkeypatch.setattr(
        flow_mod,
        "_sort",
        lambda *a, **k: SortResult(
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
        ),
    )
    seq = list(confidences)

    def extract(text, doc_type, doc_subclass, **kw):
        conf = seq.pop(0) if len(seq) > 1 else seq[0]
        return ExtractResult(doc_type, {"a": 1}, True, None, conf, None, 1, U)

    monkeypatch.setattr(flow_mod, "_extract", extract)


def _inbox(base, n):
    bins = Bins(base)
    for i in range(n):
        (bins.inbox / f"letter{i}.txt").write_text(f"letter number {i}")
    return bins


def test_run_labels_outside_and_inside_a_scope() -> None:
    assert run_labels() == {"run_id": "unscoped", "environment": "unscoped"}
    with run_scope("abc", "eval"):
        assert run_labels() == {"run_id": "abc", "environment": "eval"}


def test_instruments_merge_labels_and_explicit_labels_win(reader) -> None:
    with run_scope("r1", "eval"):
        M.documents.add(1, {"stage": "pipeline", "status": "archived"})
        M.node_duration.record(0.5, {"node": "sort"})
        M.inflight.set(2, {"worker": "w"})
        M.documents.add(1, {"run_id": "explicit"})
    points = list(_points(reader))
    by_name = {n: a for n, a in points}
    assert by_name["mailroom.node.duration"] == {
        "run_id": "r1",
        "environment": "eval",
        "node": "sort",
    }
    assert by_name["mailroom.inflight"]["run_id"] == "r1"
    assert {a["run_id"] for n, a in points if n == "mailroom.documents"} == {
        "r1",
        "explicit",
    }


def test_instrument_wrapper_passes_through_other_attributes(reader) -> None:
    assert M.documents.__class__.__name__ == "_Instrument"
    assert hasattr(M.documents, "add") and M.documents is M.documents


def test_a_pipeline_run_labels_every_data_point_and_never_doc_id(
    env, reader, monkeypatch
) -> None:
    _patch(monkeypatch)
    bins = _inbox(env, 1)
    with run_scope("evalrun", "eval"):
        flow_mod.run_document(next(bins.inbox.glob("*.txt")), worker_id="w1")
    points = list(_points(reader))
    assert points
    for name, attrs in points:
        assert attrs.get("run_id") == "evalrun", name
        assert attrs.get("environment") == "eval", name
        assert set(attrs) <= ALLOWED_KEYS, (name, set(attrs) - ALLOWED_KEYS)
        assert "doc_id" not in attrs


def test_live_documents_use_the_daily_bucket(env, reader, monkeypatch) -> None:
    _patch(monkeypatch)
    bins = _inbox(env, 1)
    flow_mod.run_document(next(bins.inbox.glob("*.txt")), worker_id="w1")
    runs = {a["run_id"] for n, a in _points(reader)}
    assert runs == {f"live-{dt.datetime.now(dt.UTC):%Y%m%d}"}


def test_watcher_concurrency_never_produces_unscoped_points(
    env, reader, monkeypatch
) -> None:
    _patch(monkeypatch)
    bins = _inbox(env, 6)
    assert Watcher(bins, worker_id="w1", concurrency=4).drain_once() == 6
    points = list(_points(reader))
    assert points
    assert "unscoped" not in {a["run_id"] for _, a in points}
    names = {n for n, _ in points}
    assert {"mailroom.inflight", "mailroom.queue.depth", "mailroom.documents"} <= names


def test_retry_and_review_cause_counters(env, reader, monkeypatch) -> None:
    _patch(monkeypatch, confidences=(0.1, 1.0))
    bins = _inbox(env, 1)
    flow_mod.run_document(next(bins.inbox.glob("*.txt")), worker_id="w1")
    retries = [
        (a["kind"], a["run_id"]) for n, a in _points(reader) if n == "mailroom.retries"
    ]
    assert retries and retries[0][0] == "retry_extract"


def test_escalation_counter_on_park(env, reader, monkeypatch) -> None:
    _patch(monkeypatch)
    monkeypatch.setattr(
        flow_mod.MailroomFlow, "_classify_route", lambda self: "human_review"
    )
    bins = _inbox(env, 1)
    flow_mod.run_document(next(bins.inbox.glob("*.txt")), worker_id="w1")
    esc = [a["to"] for n, a in _points(reader) if n == "mailroom.escalations"]
    assert esc == ["human_review"]
