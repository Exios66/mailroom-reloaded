"""Approximate timeline from the audit log (replay fallback)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from mailroom_reloaded.eval.runner import _EVAL_DDL
from mailroom_reloaded.obs.replay.audit_source import timeline_from_audit
from mailroom_reloaded.schemas.audit import AuditLogEntry, compute_entry_hash
from mailroom_reloaded.schemas.replay import Timeline
from mailroom_reloaded.storage.db import audit_table, catalog_table, init_db

BASE = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
HOSTILE = "../../etc/passwd\n<img src=x onerror=alert(1)> SSN 123-45-6789"


@pytest.fixture
def engine(tmp_path):
    eng = init_db(tmp_path / "mailroom.db")
    with eng.begin() as conn:
        conn.execute(text(_EVAL_DDL))
    yield eng
    eng.dispose()


def _chain(engine, doc: str, rows: list[tuple[str, str, dict, float]]) -> None:
    """Seed a doc's hash chain; each row is (node, event, payload, seconds after BASE)."""
    prev = ""
    with engine.begin() as conn:
        for seq, (node, event, payload, at) in enumerate(rows, start=1):
            e = AuditLogEntry(
                doc_id=doc,
                seq=seq,
                node=node,
                event=event,
                payload=payload,
                ts=BASE + timedelta(seconds=at),
                prev_hash=prev,
            )
            e.entry_hash = compute_entry_hash(e)
            prev = e.entry_hash
            conn.execute(
                audit_table.insert().values(
                    doc_id=doc,
                    seq=seq,
                    node=node,
                    event=event,
                    payload=json.dumps(payload),
                    ts=e.ts.isoformat(),
                    prev_hash=e.prev_hash,
                    entry_hash=e.entry_hash,
                )
            )


def _eval(engine, run: str, filename: str, doc: str, **cols) -> None:
    cols = {"mode": "pipeline", "status": "archived", **cols}
    names = ["run_id", "filename", "doc_id", *cols]
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO eval_docs ({', '.join(names)}) "
                f"VALUES ({', '.join(':' + n for n in names)})"
            ),
            {"run_id": run, "filename": filename, "doc_id": doc, **cols},
        )


def _happy(engine, doc: str = "d1", offset: float = 0.0) -> None:
    _chain(
        engine,
        doc,
        [
            ("ingest", "completed", {"elapsed_s": 1.0}, offset + 1.0),
            ("sort", "completed", {"elapsed_s": 2.0}, offset + 3.0),
            (
                "gate_classify",
                "gate_decision",
                {
                    "action": "proceed",
                    "reason": "high conf",
                    "source": "rules",
                    "confidence": 0.93,
                },
                offset + 3.1,
            ),
            ("extract", "completed", {"elapsed_s": 4.0}, offset + 7.1),
            (
                "archive",
                "archived",
                {"file_sha256": "ab" * 32, "path": "/x/y", "doc_type": "invoice"},
                offset + 7.5,
            ),
        ],
    )


def test_run_timeline_is_ordered_and_approximate(engine) -> None:
    _happy(engine)
    _eval(
        engine,
        "eval-r1",
        "invoice.pdf",
        "d1",
        doc_type="invoice",
        prompt_tokens=100,
        completion_tokens=50,
        calls=3,
        latency_s=7.4,
        judge_overall=0.9,
    )
    tl = timeline_from_audit("run", "eval-r1", engine=engine)
    assert isinstance(tl, Timeline)
    Timeline.model_validate(tl.model_dump(mode="json"))
    s = tl.session
    assert (s.id, s.kind, s.source, s.approx, s.environment) == (
        "run:eval-r1",
        "run",
        "audit",
        True,
        "eval",
    )
    assert s.links == {}
    assert s.t0_iso == BASE.isoformat()  # ingest started at BASE + 1 - 1
    assert [x.node for x in tl.segments] == ["ingest", "sort", "extract"]
    assert [x.station for x in tl.segments] == ["intake", "sorter", "specialist"]
    assert all(x.approx for x in tl.segments)
    starts = [x.t0 for x in tl.segments]
    assert starts == sorted(starts)
    assert tl.segments[0].t0 == 0.0 and tl.segments[0].t1 == 1.0
    assert tl.segments[1].t0 == 1.0 and tl.segments[1].t1 == 3.0
    assert s.duration_s == pytest.approx(7.5)
    assert [e.kind for e in tl.events] == ["gate_decision", "archived"]
    gate = tl.events[0]
    assert gate.station == "gate"
    assert gate.payload["action"] == "proceed" and gate.payload["confidence"] == 0.93
    assert tl.events[1].station == "archive"
    assert (
        "path" not in tl.events[1].payload and "file_sha256" not in tl.events[1].payload
    )
    (ent,) = tl.entities
    assert ent.filename == "invoice.pdf" and ent.final_status == "archived"
    assert ent.final_stage == "specialist"
    assert ent.totals.tokens == 150 and ent.totals.llm_calls == 3
    assert ent.totals.duration_s == pytest.approx(7.4)
    assert ent.quality == 0.9
    assert tl.rollups.tokens == 150
    assert tl.rollups.per_station["sorter"].n == 1
    assert [st.id for st in tl.stations][:2] == ["intake", "sorter"]


def test_failure_text_never_reaches_payload(engine) -> None:
    _chain(
        engine,
        "dx",
        [
            (
                "ingest",
                "node_failed",
                {"reason": f"ingest_failed:{HOSTILE}", "elapsed_s": 0.5},
                2.0,
            ),
        ],
    )
    _chain(
        engine,
        "dy",
        [
            (
                "gate_extract",
                "gate_decision",
                {"action": HOSTILE, "reason": HOSTILE, "source": HOSTILE},
                3.0,
            ),
            ("human_review", "parked", {"reason": HOSTILE}, 3.5),
            (
                "review",
                "review_resolved",
                {"action": HOSTILE, "reviewer": "alice@example.com"},
                4.0,
            ),
            (
                "extract",
                "node_failed",
                {"reason": "deadline_exceeded", "elapsed_s": 1.0},
                5.0,
            ),
        ],
    )
    _eval(engine, "live-1", HOSTILE + ".pdf", "dx", status="failed")
    _eval(engine, "live-1", "ok.pdf", "dy")
    tl = timeline_from_audit("run", "live-1", engine=engine)
    assert tl is not None
    dumped = tl.model_dump(mode="json")
    entities = dumped.pop("entities")
    blob = json.dumps(dumped)
    for needle in ("passwd", "onerror", "123-45", "<img", "alice@", "\\n"):
        assert needle not in blob
    by_doc = {e["doc_id"]: e for e in entities}
    assert by_doc["dx"]["failure_class"] == "io_error"
    assert by_doc["dy"]["failure_class"] == "run_budget"
    assert by_doc["dx"]["final_status"] == "failed"
    reasons = {s.doc_id: s.reason for s in tl.segments}
    assert reasons == {"dx": "ingest_failed", "dy": "deadline_exceeded"}
    assert all(s.status == "failed" for s in tl.segments)
    parked = next(e for e in tl.events if e.kind == "parked")
    assert parked.payload == {"reason": "other"}
    review = next(e for e in tl.events if e.kind == "review_resolved")
    assert review.payload == {"action": "other"}
    # the filename is the one free-form field: bounded and control-free
    fn = by_doc["dx"]["filename"]
    assert len(fn) <= 120 and "\n" not in fn
    long_blob = json.dumps(entities)
    assert "alice@" not in long_blob


def test_filename_is_bounded(engine) -> None:
    _happy(engine)
    _eval(engine, "eval-big", "x" * 1000 + ".pdf", "d1")
    tl = timeline_from_audit("run", "eval-big", engine=engine)
    assert tl is not None and len(tl.entities[0].filename) == 120


def test_unknown_node_is_skipped(engine) -> None:
    _chain(
        engine,
        "du",
        [
            ("mystery_node", "completed", {"elapsed_s": 1.0}, 1.0),
            ("sort", "completed", {"elapsed_s": 1.0}, 2.0),
        ],
    )
    tl = timeline_from_audit("doc", "du", engine=engine)
    assert tl is not None
    assert [s.node for s in tl.segments] == ["sort"]


def test_retry_attempts_numbered(engine) -> None:
    _chain(
        engine,
        "dr",
        [
            ("sort", "completed", {"elapsed_s": 1.0}, 1.0),
            ("gate_classify", "gate_decision", {"action": "retry"}, 1.1),
            ("sort", "completed", {"elapsed_s": 1.5}, 3.0),
        ],
    )
    tl = timeline_from_audit("doc", "dr", engine=engine)
    assert tl is not None
    assert [(s.attempt, s.retry_kind) for s in tl.segments] == [
        (1, None),
        (2, "retry_sort"),
    ]


def test_doc_filename_from_catalog(engine) -> None:
    _happy(engine, "dc")
    with engine.begin() as conn:
        conn.execute(
            catalog_table.insert().values(
                doc_id="dc",
                filename="cat.pdf",
                doc_type="invoice",
                status="archived",
                updated_at="x",
            )
        )
    tl = timeline_from_audit("doc", "dc", engine=engine)
    assert tl is not None
    assert tl.entities[0].filename == "cat.pdf" and tl.entities[0].doc_type == "invoice"


def test_window_collects_documents_in_range(engine) -> None:
    _happy(engine, "w1")
    _happy(engine, "w2", offset=86_400)
    lo = int((BASE - timedelta(minutes=1)).timestamp() * 1e9)
    hi = int((BASE + timedelta(hours=1)).timestamp() * 1e9)
    tl = timeline_from_audit("window", f"{lo}-{hi}", engine=engine)
    assert tl is not None
    assert [e.doc_id for e in tl.entities] == ["w1"]
    assert tl.session.kind == "window"


def test_no_data_returns_none(engine) -> None:
    assert timeline_from_audit("run", "nope", engine=engine) is None
    assert timeline_from_audit("doc", "nope", engine=engine) is None
    assert timeline_from_audit("session", "abc", engine=engine) is None
    _eval(engine, "eval-empty", "a.pdf", "ghost")  # eval row but no audit rows
    assert timeline_from_audit("run", "eval-empty", engine=engine) is None


def test_run_without_eval_docs_table(tmp_path) -> None:
    eng = init_db(tmp_path / "bare.db")
    try:
        assert timeline_from_audit("run", "x", engine=eng) is None
        _happy(eng, "z1")
        tl = timeline_from_audit("doc", "z1", engine=eng)
        assert tl is not None and tl.session.environment == "live"
    finally:
        eng.dispose()
