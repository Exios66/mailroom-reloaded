"""``replay/v1`` timeline builder: spans -> Timeline, rollups, windowing, cache, privacy."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fixtures.replay import span_rows as F

from mailroom_reloaded.obs import attrs as A
from mailroom_reloaded.obs.replay import timeline as tlmod
from mailroom_reloaded.obs.replay.timeline import (
    build_timeline,
    clear_cache,
    timeline_from_spans,
)
from mailroom_reloaded.schemas.replay import Session, Timeline
from mailroom_reloaded.storage.span_store import SpanStore


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Stub the audit source (no database) and start with a cold cache."""
    clear_cache()
    audit = types.ModuleType("mailroom_reloaded.obs.replay.audit_source")
    audit.calls = []
    audit.result = None

    def timeline_from_audit(kind, key, *, engine=None):
        audit.calls.append((kind, key))
        return audit.result

    audit.timeline_from_audit = timeline_from_audit
    monkeypatch.setitem(sys.modules, "mailroom_reloaded.obs.replay.audit_source", audit)
    yield audit
    clear_cache()


@pytest.fixture
def tl() -> Timeline:
    out = timeline_from_spans(F.all_rows(), "run", F.RUN)
    assert out is not None
    return out


def _segs(tl: Timeline, doc: str):
    return [s for s in tl.segments if s.doc_id == doc]


def test_empty_is_none() -> None:
    assert timeline_from_spans([], "run", "x") is None


def test_session_header(tl: Timeline) -> None:
    assert tl.version == "replay/v1"
    assert tl.session.id == "run:run-1"
    assert tl.session.source == "spans" and tl.session.approx is False
    assert tl.session.environment == "eval"
    assert tl.session.duration_s == 16.0
    assert tl.session.window.complete is True
    t0 = datetime.fromtimestamp(F.T0, tz=UTC).isoformat().replace("+00:00", "Z")
    assert tl.session.t0_iso == t0
    assert [s.id for s in tl.stations] == [s.id for s in A.STATIONS]
    assert [s.order for s in tl.stations] == list(range(len(A.STATIONS)))


def test_happy_path_order_and_durations(tl: Timeline) -> None:
    segs = _segs(tl, "doc-a")
    assert [s.station for s in segs] == [
        "intake",
        "sorter",
        "gate",
        "specialist",
        "judge",
        "archive",
    ]
    order = {s.id: s.order for s in tl.stations}
    assert [order[s.station] for s in segs] == sorted(order[s.station] for s in segs)
    assert [round(s.t1 - s.t0, 6) for s in segs] == [1.0, 2.0, 0.5, 3.5, 1.5, 1.5]
    assert all(s.status == "ok" and s.reason is None for s in segs)
    assert segs[0].t0 == 0.0 and segs[-1].t1 == 10.0


def test_retry_segments_and_events(tl: Timeline) -> None:
    sorts = [s for s in _segs(tl, "doc-b") if s.node == "sort"]
    assert [(s.attempt, s.retry_kind) for s in sorts] == [(1, None), (2, "retry_sort")]
    kinds = [(e.kind, e.station) for e in tl.events if e.doc_id == "doc-b"]
    assert kinds == [("gate_decision", "gate"), ("retry", "gate")]
    retry = next(e for e in tl.events if e.kind == "retry")
    assert retry.t == 4.4 - 2.0 + 2.0 and retry.payload["kind"] == "retry_sort"


def test_failed_segment(tl: Timeline) -> None:
    seg = next(s for s in _segs(tl, "doc-c") if s.node == "extract")
    assert seg.status == "failed" and seg.reason == "deadline_exceeded"
    ent = next(e for e in tl.entities if e.doc_id == "doc-c")
    assert ent.final_status == "failed" and ent.failure_class == "run_budget"
    assert any(
        e.kind == "exception" and e.payload == {"exception.type": "TimeoutError"}
        for e in tl.events
    )


def test_long_reason_is_bounded() -> None:
    rows = F.failed()
    rows[-1]["attrs"]["mailroom.fail_reason"] = "x" * 1000
    out = timeline_from_spans(rows, "run", F.RUN)
    assert out is not None
    assert max(len(s.reason or "") for s in out.segments) == 256


def test_parked_and_boss_events(tl: Timeline) -> None:
    parked = next(e for e in tl.events if e.kind == "parked")
    assert (
        parked.doc_id == "doc-d"
        and parked.station == "review"
        and parked.payload == {"reason": "needs review"}
    )
    assert next(e for e in tl.events if e.kind == "escalation").payload == {
        "to": "boss",
        "reason": "conflict",
    }
    assert [s.station for s in _segs(tl, "doc-e")] == [
        "sorter",
        "judge",
        "boss",
        "archive",
    ]


def test_unknown_event_payload_is_scalar_and_bounded(tl: Timeline) -> None:
    custom = next(e for e in tl.events if e.kind == "custom_thing")
    assert custom.payload == {"note": "x" * 256, "n": 3}


def test_scores_at_span_end_time(tl: Timeline) -> None:
    conf = next(
        s
        for s in tl.scores
        if s.doc_id == "doc-a" and s.name == "extraction_confidence"
    )
    assert conf.t == 10.0 and conf.value == 0.9 and conf.data_type == "numeric"
    prep = [s for s in tl.scores if s.name == "intake_prep_completeness"]
    assert (
        len(prep) == 1 and prep[0].t == 1.0
    )  # attribute and event are one score, at span end
    causes = next(
        s for s in tl.scores if s.name == "review_causes" and s.doc_id == "doc-c"
    )
    assert causes.value == ["extraction_miss"] and causes.t == 9.0 - 0.0
    assert [s.t for s in tl.scores] == sorted(s.t for s in tl.scores)


def test_generations_metadata_only(tl: Timeline) -> None:
    gens = [g for g in tl.generations if g.doc_id == "doc-a"]
    assert [
        (g.role, g.prompt_tokens, g.completion_tokens, g.cost_usd) for g in gens
    ] == [
        ("sorter", 100, 20, 0.001),
        ("specialist", 300, 80, 0.002),
    ]
    assert gens[0].t0 == 1.2 and gens[0].t1 == 2.8
    assert gens[0].parent_span_id is not None and gens[0].model == "m-1"


def test_entities_and_totals_precedence(tl: Timeline) -> None:
    by = {e.doc_id: e for e in tl.entities}
    assert list(by) == ["doc-a", "doc-b", "doc-c", "doc-d", "doc-e"]
    a = by["doc-a"]
    # score beats the root usage attributes and the summed generations
    assert (a.totals.tokens, a.totals.cost_usd, a.totals.llm_calls) == (500, 0.01, 2)
    assert a.totals.duration_s == 10.0 and a.t_start == 0.0 and a.t_end == 10.0
    assert (
        a.filename == "doc-a.pdf"
        and a.expected_doc_class == "invoice"
        and a.verdict == "CORRECT"
    )
    # no score and no usage attributes: generations are summed
    d = by["doc-d"]
    assert (d.totals.tokens, d.totals.cost_usd, d.totals.llm_calls) == (180, 0.003, 2)
    assert (
        d.verdict == "PARTIAL"
        and d.quality == 0.5
        and d.review_causes == ["judge_partial"]
    )


def test_rollups(tl: Timeline) -> None:
    r = tl.rollups
    assert r.per_station["sorter"].n == 5
    assert (r.per_station["sorter"].p50_s, r.per_station["sorter"].p95_s) == (2.0, 3.6)
    assert (r.per_station["judge"].p50_s, r.per_station["judge"].p95_s) == (1.5, 1.95)
    assert r.per_station["boss"].n == 1 and r.per_station["boss"].p50_s == 3.0
    assert r.verdict_counts == {"CORRECT": 2, "PARTIAL": 1}
    assert r.review_causes == {"extraction_miss": 1, "judge_partial": 1}
    assert r.cost_usd == pytest.approx(0.088)
    assert r.tokens == 3380
    assert r.first_pass_rate == 0.2  # only doc-a has success_rate 1
    assert r.averages["extraction_confidence"] == pytest.approx(0.8)
    assert r.averages["success_rate"] == pytest.approx(0.2)
    assert "review_causes" not in r.averages


def test_first_pass_derived_without_score() -> None:
    rows = F.happy()
    for row in rows:
        row["attrs"].pop("mailroom.score.success_rate", None)
    out = timeline_from_spans(rows, "run", F.RUN)
    assert out is not None and out.rollups.first_pass_rate == 1.0
    retried = F.retry()
    retried[0]["attrs"].pop("mailroom.score.success_rate")
    out = timeline_from_spans(rows + retried, "run", F.RUN)
    assert out is not None and out.rollups.first_pass_rate == 0.5


def test_no_content_in_serialised_timeline(tl: Timeline) -> None:
    blob = tl.model_dump_json()
    for needle in (
        "SECRET",
        "input_messages",
        "output_messages",
        "input.value",
        "output.value",
    ):
        assert needle not in blob
    # the fixture really carries the content the builder must ignore
    rows = F.all_rows()
    assert any("llm.input_messages.0.message.content" in r["attrs"] for r in rows)
    assert any("input.value" in r["attrs"] for r in rows)
    assert any(ev["attrs"].get("text") for r in rows for ev in r["events"])


# ------------------------------------------------------------------ windowing
def _store(tmp_path: Path, rows=None) -> SpanStore:
    store = SpanStore(tmp_path / "traces.db")
    store.write(F.to_store_rows(rows if rows is not None else F.all_rows()))
    return store


def test_window_marks_incomplete(tmp_path: Path) -> None:
    store = _store(tmp_path)
    full = build_timeline("run:run-1", store=store)
    assert full is not None and full.session.window.complete is True
    part = build_timeline("run:run-1", from_s=0, to_s=5, store=store)
    assert part is not None
    assert part.session.window.complete is False
    assert (part.session.window.from_s, part.session.window.to_s) == (0.0, 5.0)
    assert part.session.duration_s == 16.0
    assert 0 < len(part.segments) < len(full.segments)
    assert all(s.t0 <= 5 for s in part.segments) and all(e.t <= 5 for e in part.events)
    assert all(s.t <= 5 for s in part.scores)
    assert part.rollups == full.rollups  # rollups describe the whole session
    whole = build_timeline("run:run-1", from_s=0, to_s=16, store=store)
    assert whole is not None and whole.session.window.complete is True


def test_payload_cap_auto_windows(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(tlmod, "MAX_SEGMENTS", 8)
    out = build_timeline("run-1", store=store)
    assert out is not None
    assert out.session.window.complete is False
    assert 0 < len(out.segments) <= 8


def test_byte_cap_auto_windows(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    full = build_timeline("run-1", store=store)
    assert full is not None
    monkeypatch.setattr(tlmod, "MAX_PAYLOAD_BYTES", len(full.model_dump_json()) // 2)
    clear_cache()
    out = build_timeline("run-1", store=store)
    assert out is not None and out.session.window.complete is False
    assert len(out.model_dump_json()) <= len(full.model_dump_json()) // 2


# ------------------------------------------------------------------ sources and cache
def test_kinds_resolve_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert build_timeline("session:sess-1", store=store) is not None
    one = build_timeline("doc:doc-b", store=store)
    assert one is not None and [e.doc_id for e in one.entities] == ["doc-b"]
    lo, hi = F.ns(0), F.ns(3.5)
    win = build_timeline(f"window:{lo}-{hi}", store=store)
    assert win is not None and {e.doc_id for e in win.entities} == {
        "doc-a",
        "doc-b",
        "doc-c",
    }
    assert build_timeline("run:nope", store=store) is None


def test_audit_fallback_and_missing_store(tmp_path: Path, _isolate) -> None:
    audit = _isolate
    store = SpanStore(tmp_path / "absent" / "traces.db")
    assert build_timeline("run:r", store=store) is None
    assert audit.calls == [("run", "r")]
    assert not store.path.exists()  # reading never creates the database
    audit.result = Timeline(session=Session(id="run:r", source="audit", approx=True))
    out = build_timeline("run:r", store=store)
    assert (
        out is not None and out.session.source == "audit" and out.session.approx is True
    )


def test_malformed_id_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_timeline("window:not-a-range", store=_store(tmp_path))


def test_cache_hit_and_watermark_miss(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    calls = []
    real = tlmod.timeline_from_spans

    def counting(rows, kind, key):
        calls.append(len(rows))
        return real(rows, kind, key)

    monkeypatch.setattr(tlmod, "timeline_from_spans", counting)
    first = build_timeline("run:run-1", store=store)
    again = build_timeline("run:run-1", store=store)
    assert again == first and again is not first and len(calls) == 1
    build_timeline("run:run-1", from_s=0, to_s=5, store=store)  # different window: miss
    assert len(calls) == 2
    store.write(
        F.to_store_rows(
            [
                F.row(
                    "mailroom.node.sort",
                    20,
                    21,
                    doc="doc-z",
                    trace="z" * 32,
                    station="sorter",
                    attrs={"mailroom.node": "sort"},
                )
            ]
        )
    )
    fresh = build_timeline("run:run-1", store=store)
    assert len(calls) == 3 and fresh is not first
    assert fresh is not None and fresh.session.duration_s == 21.0
    clear_cache()
    build_timeline("run:run-1", store=store)
    assert len(calls) == 4


def test_cache_is_bounded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(tlmod.CACHE_SIZE + 5):
        build_timeline("run:run-1", from_s=float(i), store=store)
    assert len(tlmod._CACHE) == tlmod.CACHE_SIZE


def test_cached_timeline_is_isolated_from_caller_mutation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clear_cache()
    first = build_timeline("run-1", store=store)
    assert first is not None
    n = len(first.segments)
    first.segments.clear()
    first.session.id = "tampered"
    again = build_timeline("run-1", store=store)
    assert again is not None and len(again.segments) == n and again.session.id != "tampered"


def test_window_id_round_trips_between_sessions_and_timeline(tmp_path: Path) -> None:
    from mailroom_reloaded.obs.replay.sessions import format_session_id

    store = _store(tmp_path)
    sid = format_session_id("window", f"{F.ns(0)}-{F.ns(3.5)}")
    assert build_timeline(sid, store=store) is not None
