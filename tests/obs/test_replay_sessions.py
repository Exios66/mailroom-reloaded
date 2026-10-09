"""Replay session ids and the session picker."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from mailroom_reloaded.eval.runner import _EVAL_DDL
from mailroom_reloaded.obs.replay.sessions import list_sessions, parse_session_id
from mailroom_reloaded.schemas.audit import AuditLogEntry, compute_entry_hash
from mailroom_reloaded.storage.db import audit_table, init_db
from mailroom_reloaded.storage.span_store import SpanStore

BASE = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    eng = init_db(tmp_path / "mailroom.db")
    with eng.begin() as conn:
        conn.execute(text(_EVAL_DDL))
    yield eng
    eng.dispose()


@pytest.fixture
def store(tmp_path):
    s = SpanStore(tmp_path / "traces.db")
    yield s
    s.close()


def _span(
    span_id: str, run: str | None, session: str | None, start: int, end: int
) -> dict:
    return {
        "span_id": span_id,
        "trace_id": "t" * 32,
        "parent_id": None,
        "name": "mailroom.document",
        "kind": "CHAIN",
        "start_ns": start,
        "end_ns": end,
        "status": "OK",
        "doc_id": f"d-{span_id}",
        "run_id": run,
        "session_id": session,
        "station": None,
        "attrs": "{}",
        "events": "[]",
    }


def _audit(engine, doc: str, at: datetime) -> None:
    entry = AuditLogEntry(doc_id=doc, seq=1, node="sort", event="completed", ts=at)
    entry.entry_hash = compute_entry_hash(entry)
    with engine.begin() as conn:
        conn.execute(
            audit_table.insert().values(
                doc_id=doc,
                seq=1,
                node="sort",
                event="completed",
                payload=json.dumps({}),
                ts=at.isoformat(),
                prev_hash="",
                entry_hash=entry.entry_hash,
            )
        )


def _eval_row(engine, run: str, filename: str, doc: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO eval_docs (run_id, filename, doc_id, mode, status) "
                "VALUES (:r, :f, :d, 'pipeline', 'archived')"
            ),
            {"r": run, "f": filename, "d": doc},
        )


# --------------------------------------------------------------- id parsing
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("live-20261009", ("run", "live-20261009")),
        (
            "eval-3fa9c1d2-0b7e-4c11-9a52-1d2e3f405162",
            ("run", "eval-3fa9c1d2-0b7e-4c11-9a52-1d2e3f405162"),
        ),
        ("run:eval-abc", ("run", "eval-abc")),
        ("session:user@host+tag=1", ("session", "user@host+tag=1")),
        ("doc:a1b2c3/d4.e5", ("doc", "a1b2c3/d4.e5")),
        (
            "window:1760000000000000000-1760003600000000000",
            ("window", "1760000000000000000-1760003600000000000"),
        ),
        ("weird:thing", ("run", "weird:thing")),
    ],
)
def test_parse_accepts(raw, expected) -> None:
    assert parse_session_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "run:",
        "doc:",
        "a" * 201,
        "run:" + "a" * 197,
        "live 2026",
        "live\n",
        "live\x00",
        "a\x1b[31m",
        "<script>alert(1)</script>",
        "run:'; DROP TABLE spans;--",
        "doc:a;b",
        "run:café",
        "window:abc",
        "window:5-5",
        "window:9-1",
        "window:1-2-3",
        "window:",
        "window:1-9223372036854775808",  # hi > int64 max overflows SQLite
        "window:99999999999999999999-99999999999999999999",
    ],
)
def test_parse_rejects(raw) -> None:
    with pytest.raises(ValueError):
        parse_session_id(raw)


def test_parse_window_int64_boundary() -> None:
    top = 2**63 - 1
    assert parse_session_id(f"window:1-{top}") == ("window", f"1-{top}")


def test_parse_limit_boundary() -> None:
    assert parse_session_id("a" * 200) == ("run", "a" * 200)


# --------------------------------------------------------------- list_sessions
def test_merge_dedupe_prefers_spans_and_orders_newest_first(engine, store) -> None:
    t0 = int(BASE.timestamp() * 1e9)
    store.write(
        [
            _span("a1", "live-20261009", None, t0, t0 + 5_000_000_000),
            _span("a2", "eval-both", "sess-1", t0 - 10_000_000_000, t0 - 9_000_000_000),
        ]
    )
    _eval_row(engine, "eval-both", "f.pdf", "dd1")
    _audit(engine, "dd1", BASE - timedelta(days=1))
    _eval_row(engine, "eval-old", "g.pdf", "dd2")
    _audit(engine, "dd2", BASE - timedelta(days=3))

    rows = list_sessions(50, store=store, engine=engine)
    ids = [r.id for r in rows]
    assert ids == [
        "run:live-20261009",
        "run:eval-both",
        "session:sess-1",
        "run:eval-old",
    ] or ids == [
        "run:live-20261009",
        "session:sess-1",
        "run:eval-both",
        "run:eval-old",
    ]
    by_id = {r.id: r for r in rows}
    assert len(ids) == len(set(ids))
    assert by_id["run:eval-both"].source == "spans"
    assert by_id["run:eval-old"].source == "audit"
    assert by_id["run:eval-old"].environment == "eval"
    assert by_id["run:live-20261009"].environment == "live"
    assert by_id["session:sess-1"].kind == "session"
    assert by_id["run:live-20261009"].duration_s == 5.0
    assert ids[-1] == "run:eval-old"


def test_limit_is_clamped(engine, store) -> None:
    t0 = int(BASE.timestamp() * 1e9)
    store.write(
        [_span(f"s{i}", f"run-{i}", None, t0 + i, t0 + i + 1) for i in range(5)]
    )
    assert len(list_sessions(2, store=store, engine=engine)) == 2
    assert len(list_sessions(0, store=store, engine=engine)) == 1
    assert len(list_sessions(-7, store=store, engine=engine)) == 1
    assert len(list_sessions(10_000, store=store, engine=engine)) == 5


def test_empty_sources(engine, store) -> None:
    assert list_sessions(store=store, engine=engine) == []


def test_missing_eval_docs_table_is_tolerated(tmp_path, store) -> None:
    eng = init_db(tmp_path / "bare.db")
    try:
        assert list_sessions(store=store, engine=eng) == []
    finally:
        eng.dispose()


def test_hostile_stored_ids_are_skipped(engine, store) -> None:
    t0 = int(BASE.timestamp() * 1e9)
    store.write(
        [
            _span("h1", None, "bad id <b>", t0, t0 + 1),
            _span("h2", "ok-run", None, t0, t0 + 1),
        ]
    )
    ids = [r.id for r in list_sessions(store=store, engine=engine)]
    assert ids == ["run:ok-run"]


def test_hostile_run_id_is_skipped(engine, store) -> None:
    t0 = int(BASE.timestamp() * 1e9)
    store.write(
        [
            _span("h1", "bad run <b>", None, t0, t0 + 1),
            _span("h2", "ok-run", None, t0, t0 + 1),
        ]
    )
    assert [r.id for r in list_sessions(store=store, engine=engine)] == ["run:ok-run"]


def test_limit_is_applied_after_audit_duplicates_are_dropped(engine, store) -> None:
    t0 = int((BASE - timedelta(days=10)).timestamp() * 1e9)
    store.write(
        [
            _span("a", "run-a", None, t0 + 2, t0 + 3),
            _span("b", "run-b", None, t0, t0 + 1),
        ]
    )
    # audit copies of both span runs are the newest audit rows; the audit-only run
    # sits between them and must not be crowded out of the page
    _eval_row(engine, "run-a", "a.pdf", "da")
    _audit(engine, "da", BASE)
    _eval_row(engine, "run-b", "b.pdf", "db")
    _audit(engine, "db", BASE - timedelta(hours=1))
    _eval_row(engine, "run-c", "c.pdf", "dc")
    _audit(engine, "dc", BASE - timedelta(days=1))
    ids = [r.id for r in list_sessions(2, store=store, engine=engine)]
    assert ids == ["run:run-c", "run:run-a"]
