"""Local span store: allow-list, masking, caps, retention primitives, run stamping."""

from __future__ import annotations

import json
import threading

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.trace import StatusCode

from mailroom_reloaded.obs.run_context import run_scope
from mailroom_reloaded.obs.tracing import RunScopeSpanProcessor
from mailroom_reloaded.storage.span_store import (
    SpanStore,
    SqliteSpanExporter,
    filter_attributes,
)

DOC_TEXT = "Alice Smith's SSN is 123-45-6789 and the plaintiff alleges..."


@pytest.fixture
def store(tmp_path):
    s = SpanStore(tmp_path / "traces.db")
    yield s
    s.close()


def _provider(
    store: SpanStore, *, mask: bool = False, batch: bool = False
) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(RunScopeSpanProcessor())
    exporter = SqliteSpanExporter(store, mask=mask)
    provider.add_span_processor(
        BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)
    )
    return provider


def _named(store: SpanStore, name: str) -> dict:
    return next(r for r in store.spans_between(0, 2**63 - 1) if r["name"] == name)


def _fake_run(provider: TracerProvider, doc: str = "doc1") -> None:
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("mailroom.document") as root:
        root.set_attribute("mailroom.doc_id", doc)
        root.set_attribute("openinference.span.kind", "CHAIN")
        root.set_attribute("input.value", json.dumps({"filename": "a.txt"}))
        with tracer.start_as_current_span("mailroom.node.sort") as node:
            node.set_attribute("mailroom.doc_id", doc)
            node.set_attribute("mailroom.station", "sorter")
            node.set_attribute("output.value", json.dumps({"doc_type": "contract"}))
            node.add_event(
                "mailroom.retry",
                {"kind": "retry_sort", "attempt": 2, "secret": DOC_TEXT},
            )
            with tracer.start_as_current_span("openai.chat") as llm:
                llm.set_attribute("openinference.span.kind", "LLM")
                llm.set_attribute("llm.model_name", "m")
                llm.set_attribute("llm.token_count.prompt", 10)
                llm.set_attribute("llm.cost.total", 0.001)
                llm.set_attribute("input.value", DOC_TEXT)
                llm.set_attribute("output.value", "completion with " + DOC_TEXT)
                llm.set_attribute("llm.input_messages.0.message.content", DOC_TEXT)
                llm.set_attribute("llm.output_messages.0.message.content", DOC_TEXT)
                llm.set_attribute(
                    "llm.invocation_parameters",
                    json.dumps({"api_key": "sk-secret", "max_tokens": 5}),
                )


def test_round_trip_with_run_stamping(store) -> None:
    provider = _provider(store)
    with run_scope("run1", "eval", "eval", session_id="eval-run1"):
        _fake_run(provider)
    rows = store.spans_for_run("run1")
    assert [r["name"] for r in rows] == [
        "mailroom.document",
        "mailroom.node.sort",
        "openai.chat",
    ]
    root, node, llm = rows
    assert (
        root["parent_id"] is None
        and node["parent_id"] == root["span_id"]
        and llm["parent_id"] == node["span_id"]
    )
    assert {r["trace_id"] for r in rows} == {root["trace_id"]}
    assert (
        root["doc_id"] == "doc1"
        and node["station"] == "sorter"
        and node["session_id"] == "eval-run1"
    )
    assert (
        llm["attrs"]["llm.token_count.prompt"] == 10
        and llm["attrs"]["llm.cost.total"] == 0.001
    )
    assert (
        store.spans_for_session("eval-run1") == rows
        and len(store.spans_for_doc("doc1")) == 2
    )
    assert (
        store.list_runs()[0]["run_id"] == "run1" and store.list_runs()[0]["spans"] == 3
    )


def test_no_document_text_or_secrets_are_stored_even_unmasked(store) -> None:
    _fake_run(_provider(store, mask=False))
    blob = json.dumps(store.spans_between(0, 2**63 - 1))
    for leaked in ("Alice", "123-45", "plaintiff", "sk-secret", "completion with"):
        assert leaked not in blob
    llm = _named(store, "openai.chat")
    assert not any(
        k.startswith("llm.input_messages") or k in ("input.value", "output.value")
        for k in llm["attrs"]
    )
    assert "llm.invocation_parameters" not in llm["attrs"]


def test_summary_values_are_kept_on_root_and_node_and_masked_on_request(
    tmp_path,
) -> None:
    plain, masked = SpanStore(tmp_path / "a.db"), SpanStore(tmp_path / "b.db")
    try:
        _fake_run(_provider(plain, mask=False))
        _fake_run(_provider(masked, mask=True))
        node = lambda s: _named(s, "mailroom.node.sort")
        root = lambda s: _named(s, "mailroom.document")
        assert json.loads(node(plain)["attrs"]["output.value"]) == {
            "doc_type": "contract"
        }
        assert json.loads(root(plain)["attrs"]["input.value"]) == {"filename": "a.txt"}
        assert node(masked)["attrs"]["output.value"] == "<masked>"
        assert root(masked)["attrs"]["input.value"] == "<masked>"
    finally:
        plain.close()
        masked.close()


def test_events_keep_allow_listed_names_and_only_the_exception_type(store) -> None:
    provider = _provider(store)
    tracer = provider.get_tracer("t")
    with (
        pytest.raises(RuntimeError),
        tracer.start_as_current_span("mailroom.node.extract") as span,
    ):
        span.add_event("mailroom.retry", {"kind": "retry_extract"})
        span.add_event("some.other.event", {"x": 1})
        raise RuntimeError("failed reading /home/alice/secret.pdf")
    row = store.spans_between(0, 2**63 - 1)[0]
    names = [e["name"] for e in row["events"]]
    assert names == ["mailroom.retry", "exception"]
    exc = row["events"][1]
    assert exc["attrs"] == {"exception.type": "RuntimeError"}
    assert row["status"] == StatusCode.ERROR.name
    assert "alice" not in json.dumps(row)


def test_strings_are_bounded() -> None:
    out = filter_attributes(
        {"mailroom.fail_reason": "x" * 5000, "llm.model_name": "y" * 999}, name="s"
    )
    assert len(out["mailroom.fail_reason"]) == 256 and len(out["llm.model_name"]) == 256
    summary = filter_attributes({"input.value": "z" * 9000}, name="mailroom.node.sort")
    assert len(summary["input.value"]) == 2000


def test_per_run_cap_and_duplicate_span_ids(tmp_path) -> None:
    capped = SpanStore(tmp_path / "c.db", rows_per_run=4)
    try:
        provider = _provider(capped)
        with run_scope("busy"):
            for i in range(3):
                _fake_run(provider, f"d{i}")  # 3 spans each
        assert capped.count("busy") == 4
        rows = capped.spans_for_run("busy")
        encoded = {
            **rows[0],
            "attrs": json.dumps(rows[0]["attrs"]),
            "events": json.dumps(rows[0]["events"]),
        }
        assert capped.write([{**encoded, "span_id": "f" * 16, "run_id": "other"}]) == 1
        assert capped.count() == 5
        capped.write(
            [{**encoded, "span_id": "f" * 16, "run_id": "other"}]
        )  # duplicate span id is ignored
        assert capped.count() == 5
    finally:
        capped.close()


def test_cap_counts_survive_a_new_store_instance(tmp_path) -> None:
    path = tmp_path / "d.db"
    first = SpanStore(path, rows_per_run=3)
    with run_scope("r"):
        _fake_run(_provider(first))
    first.close()
    second = SpanStore(path, rows_per_run=3)
    try:
        with run_scope("r"):
            _fake_run(_provider(second), "again")
        assert second.count("r") == 3
    finally:
        second.close()


def test_prune_and_delete_runs(store) -> None:
    provider = _provider(store)
    for run in ("old", "kept", "gone"):
        with run_scope(run):
            _fake_run(provider, run)
    _fake_run(provider, "unscoped")
    assert (
        store.prune(keep_runs={"kept"}, older_than_ns=2**62) == 9
    )  # old + gone + unscoped
    assert [r["run_id"] for r in store.list_runs()] == ["kept"]
    assert store.delete_runs({"kept"}) == 3 and store.count() == 0
    assert store.delete_runs(set()) == 0


def test_prune_respects_the_age_cutoff(store) -> None:
    with run_scope("r"):
        _fake_run(_provider(store))
    assert store.prune(keep_runs=set(), older_than_ns=0) == 0
    assert store.count() == 3


def test_run_scope_is_stamped_per_worker_thread(store) -> None:
    provider = _provider(store)

    def work(n: int) -> None:
        with (
            run_scope(f"run{n}"),
            provider.get_tracer("t").start_as_current_span("mailroom.document") as s,
        ):
            s.set_attribute("mailroom.doc_id", f"d{n}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {r["run_id"] for r in store.spans_between(0, 2**63 - 1)} == {
        f"run{n}" for n in range(6)
    }


def test_spans_without_a_scope_have_no_run_id(store) -> None:
    _fake_run(_provider(store))
    assert {r["run_id"] for r in store.spans_between(0, 2**63 - 1)} == {None}
    assert store.list_runs() == []


def test_concurrent_writers_share_the_wal_file(tmp_path) -> None:
    path = tmp_path / "w.db"

    def writer(n: int) -> None:
        s = SpanStore(path)
        try:
            with run_scope(f"w{n}"):
                provider = _provider(s)
                for i in range(5):
                    _fake_run(provider, f"d{n}-{i}")
        finally:
            s.close()

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    reader = SpanStore(path)
    try:
        assert reader.count() == 4 * 5 * 3
    finally:
        reader.close()


def test_batch_processor_path_exports_on_flush(store) -> None:
    provider = _provider(store, batch=True)
    with run_scope("b"):
        _fake_run(provider)
    provider.force_flush()
    assert store.count("b") == 3
    provider.shutdown()


def test_export_failure_is_reported_not_raised(tmp_path, monkeypatch) -> None:
    s = SpanStore(tmp_path / "e.db")
    exporter = SqliteSpanExporter(s)
    monkeypatch.setattr(
        s, "write", lambda rows: (_ for _ in ()).throw(OSError("disk full"))
    )
    from opentelemetry.sdk.trace.export import SpanExportResult

    provider = TracerProvider()
    span_holder = []

    class Grab(SimpleSpanProcessor):
        def on_end(self, span):
            span_holder.append(span)

    provider.add_span_processor(Grab(exporter))
    with provider.get_tracer("t").start_as_current_span("x"):
        pass
    assert exporter.export(span_holder) is SpanExportResult.FAILURE


def test_watermark_and_empty_store(store) -> None:
    assert store.watermark() == 0 and store.list_runs() == []
    _fake_run(_provider(store))
    assert store.watermark() > 0


def test_cap_holds_across_two_store_instances_and_counts_only_real_inserts(
    tmp_path,
) -> None:
    path = tmp_path / "shared.db"
    a, b = SpanStore(path, rows_per_run=5), SpanStore(path, rows_per_run=5)
    try:
        with run_scope("r"):
            _fake_run(_provider(a), "d1")  # 3 spans
            _fake_run(_provider(b), "d2")  # only 2 fit
        assert a.count("r") == 5 and b.count("r") == 5
        rows = a.spans_for_run("r")
        enc = {
            **rows[0],
            "attrs": json.dumps(rows[0]["attrs"]),
            "events": json.dumps(rows[0]["events"]),
        }
        assert b.write([{**enc, "span_id": "e" * 16, "run_id": "other"}]) == 1
        assert (
            b.write([{**enc, "span_id": "e" * 16, "run_id": "other"}]) == 0
        )  # a duplicate is not "stored"
    finally:
        a.close()
        b.close()


def test_a_blank_trace_store_path_means_the_default(monkeypatch, tmp_path) -> None:
    from mailroom_reloaded import settings
    from mailroom_reloaded.storage.span_store import default_span_store_path

    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("MAILROOM_TRACE_STORE_PATH", "")
    settings.get_settings.cache_clear()
    try:
        assert settings.get_settings().trace_store_path is None
        assert default_span_store_path() == tmp_path / "traces.db"
        monkeypatch.setenv("MAILROOM_TRACE_STORE_PATH", str(tmp_path / "x.db"))
        settings.get_settings.cache_clear()
        assert default_span_store_path() == tmp_path / "x.db"
    finally:
        settings.get_settings.cache_clear()
