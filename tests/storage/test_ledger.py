"""Archive ledger: chain, writer thread, row cap, Merkle root and verification."""

from __future__ import annotations

import hashlib
import json
import threading

import pytest
from sqlalchemy import text

from mailroom_reloaded.schemas.ledger import (
    LedgerEntry,
    canonical_json,
    compute_ledger_hash,
    sanitize_payload,
)
from mailroom_reloaded.storage.db import init_db
from mailroom_reloaded.storage.ledger import Ledger, MetricRow, merkle_root

HEAD = {"seq": 3, "entry_hash": "ab" * 32}


@pytest.fixture
def engine(tmp_path):
    eng = init_db(tmp_path / "mailroom.db")
    yield eng
    eng.dispose()


@pytest.fixture
def ledger(engine):
    lg = Ledger(engine)
    yield lg
    lg.close()


def _doc(lg: Ledger, run: str, n: int, **payload) -> None:
    lg.append(
        "doc_closed",
        run,
        doc_id=f"doc{n}",
        payload={
            "invocation": 1,
            "outcome": "completed",
            "audit_head": HEAD,
            **payload,
        },
    )


def _run(lg: Ledger, run: str, docs: int = 3) -> None:
    lg.append("run_opened", run, payload={"kind": "eval", "mode": "pipeline"})
    for n in range(docs):
        _doc(lg, run, n)
    lg.append("run_closed", run, payload={"closed_by": "completed", "expected": docs})
    assert lg.flush()


def test_genesis_and_linked_chain(ledger) -> None:
    _run(ledger, "r1")
    es = ledger.entries()
    assert [e.seq for e in es] == [1, 2, 3, 4, 5]
    assert es[0].prev_hash == ""
    assert all(es[i].prev_hash == es[i - 1].entry_hash for i in range(1, 5))
    assert all(compute_ledger_hash(e) == e.entry_hash for e in es)
    v = ledger.verify()
    assert v.ok and v.count == 5 and v.head_seq == 5 and v.merkle_ok is True


def test_canonical_json_matches_the_audit_log_canonicalisation() -> None:
    body = {"b": [1.5, "é"], "a": {"z": None, "y": True}}
    assert canonical_json(body) == json.dumps(
        body, sort_keys=True, separators=(",", ":"), default=str
    )
    entry = LedgerEntry(
        seq=1,
        kind="gap",
        run_id="r",
        ts="2026-01-01T00:00:00+00:00",
        payload={"count": 1},
    )
    manual = hashlib.sha256(
        json.dumps(
            entry.model_dump(mode="json", exclude={"entry_hash"}),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    assert compute_ledger_hash(entry) == manual


def test_repeated_identical_payload_is_not_deduplicated(ledger) -> None:
    for _ in range(3):
        ledger.append("pinned", "r1", payload={"target": "r1", "actor": "tui"})
        ledger.append("unpinned", "r1", payload={"target": "r1", "actor": "tui"})
    ledger.flush()
    assert len(ledger.entries(run_id="r1")) == 6
    assert ledger.verify().ok


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ledger SET payload = '{\"docs\": 99}' WHERE seq = 3",
        "UPDATE ledger SET prev_hash = 'deadbeef' WHERE seq = 3",
        "UPDATE ledger SET digest = 'deadbeef' WHERE seq = 3",
        "UPDATE ledger SET entry_hash = 'deadbeef' WHERE seq = 3",
        "DELETE FROM ledger WHERE seq = 3",
    ],
)
def test_tampering_is_detected_at_the_right_seq(ledger, engine, sql) -> None:
    _run(ledger, "r1")
    with engine.begin() as conn:
        conn.execute(text(sql))
    v = ledger.verify()
    assert not v.ok
    assert v.broken_at in (3, 4)  # the edited row, or the first row after a deletion
    assert not ledger.verify("r1").ok


def test_truncating_the_head_is_detected(ledger, engine) -> None:
    _run(ledger, "r1")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ledger WHERE seq = 1"))
    v = ledger.verify()
    assert not v.ok and v.broken_at == 2


def test_merkle_root_matches_independent_recomputation(ledger) -> None:
    _run(ledger, "r1", docs=5)
    es = ledger.entries(run_id="r1")
    digests = [e.digest for e in es if e.kind == "doc_closed"]
    closed = es[-1]
    assert closed.payload["merkle_root"] == merkle_root(digests)
    assert closed.payload["docs"] == 5
    # independent recomputation with leaf/node domain separation
    level = [hashlib.sha256(b"\x00" + d.encode()).digest() for d in digests]
    while len(level) > 1:
        nxt = [
            hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest()
            for i in range(0, len(level) - 1, 2)
        ]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    assert closed.payload["merkle_root"] == level[0].hex()


def test_merkle_root_edge_cases() -> None:
    assert merkle_root([]) != merkle_root(["aa"])
    assert merkle_root(["aa", "bb"]) != merkle_root(["bb", "aa"])
    assert merkle_root(["aa", "bb", "cc"]) != merkle_root(
        ["aa", "bb", "cc", "cc"]
    )  # no odd-node duplication


def test_a_forged_run_closed_with_a_wrong_root_fails(ledger, engine) -> None:
    _run(ledger, "r1")
    # rewrite the run_closed row consistently (payload, digest, hash) but with a wrong root
    last = ledger.entries(run_id="r1")[-1]
    forged_payload = {**last.payload, "merkle_root": "00" * 32}
    forged = last.model_copy(update={"payload": forged_payload})
    from mailroom_reloaded.schemas.ledger import payload_digest

    forged.digest = payload_digest(forged_payload)
    forged.entry_hash = compute_ledger_hash(forged)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ledger SET payload=:p, digest=:d, entry_hash=:h WHERE seq=:s"),
            {
                "p": json.dumps(forged_payload, sort_keys=True),
                "d": forged.digest,
                "h": forged.entry_hash,
                "s": last.seq,
            },
        )
    assert ledger.verify("r1").merkle_ok is False
    assert ledger.verify().merkle_ok is False


def test_unknown_run_fails_verification(ledger) -> None:
    _run(ledger, "r1")
    v = ledger.verify("nope")
    assert not v.ok and v.detail == "unknown run"


def test_verify_run_checks_links_without_scanning_other_runs(ledger, engine) -> None:
    _run(ledger, "r1")
    _run(ledger, "r2")
    assert ledger.verify("r2").ok and ledger.verify("r1").ok
    with engine.begin() as conn:
        conn.execute(text("UPDATE ledger SET payload='{}' WHERE seq = 2"))  # inside r1
    assert not ledger.verify("r1").ok
    assert ledger.verify("r2").ok  # r2 only cites r1's tail hash, which is unchanged


def test_open_run_verifies_without_a_merkle_check(ledger) -> None:
    ledger.append("run_opened", "live-1", payload={"kind": "live"})
    _doc(ledger, "live-1", 1)
    ledger.flush()
    v = ledger.verify("live-1")
    assert v.ok and v.merkle_ok is None


def test_metric_rows_round_trip_and_tiers(ledger) -> None:
    ledger.append(
        "doc_closed",
        "r1",
        doc_id="d1",
        payload={"outcome": "completed"},
        metrics=[
            MetricRow("tokens", 10, 1, "abc"),
            MetricRow("score.f1", 0.5, 2),
            MetricRow("x", None, 9),
        ],
    )
    ledger.flush()
    rows = ledger.metric_rows("r1")
    assert [(r["name"], r["value"], r["tier"], r["doc_id"]) for r in rows] == [
        ("tokens", 10.0, 1, "d1"),
        ("score.f1", 0.5, 2, "d1"),
        ("x", None, 3, "d1"),
    ]
    assert [r["name"] for r in ledger.metric_rows("r1", max_tier=1)] == ["tokens"]
    assert ledger.metric_rows("other") == []


def test_non_finite_metric_values_are_dropped(ledger) -> None:
    ledger.append_metrics(
        "r1",
        "d1",
        [MetricRow("a", float("nan")), MetricRow("b", float("inf")), MetricRow("c", 1)],
    )
    ledger.flush()
    assert [r["name"] for r in ledger.metric_rows("r1")] == ["c"]


def test_row_cap_writes_exactly_one_gap_and_counts_drops(engine) -> None:
    lg = Ledger(engine, row_cap=10)
    try:
        lg.append("run_opened", "r1", payload={"kind": "eval"})
        for n in range(5):
            lg.append_metrics("r1", f"d{n}", [MetricRow(f"m{i}", i) for i in range(6)])
        lg.append("run_closed", "r1", payload={"closed_by": "completed"})
        assert lg.flush()
        assert len(lg.metric_rows("r1")) == 10
        gaps = lg.entries(run_id="r1", kind="gap")
        assert len(gaps) == 1 and gaps[0].payload["reason"] == "row_cap"
        closed = lg.entries(run_id="r1", kind="run_closed")[0]
        assert closed.payload["dropped_rows"] == 20
        assert lg.verify().ok
    finally:
        lg.close()


def test_row_cap_survives_a_restart(engine) -> None:
    first = Ledger(engine, row_cap=4)
    first.append_metrics("r1", "d", [MetricRow(f"m{i}", i) for i in range(6)])
    first.close()
    second = Ledger(engine, row_cap=4)
    try:
        second.append_metrics("r1", "d", [MetricRow("late", 1)])
        second.flush()
        assert len(second.metric_rows("r1")) == 4
        assert len(second.entries(run_id="r1", kind="gap")) == 1  # not written twice
    finally:
        second.close()


def test_eight_threads_keep_one_linear_chain(ledger) -> None:
    def worker(t: int) -> None:
        for i in range(25):
            ledger.append(
                "doc_closed",
                f"run{t}",
                doc_id=f"d{i}",
                payload={"outcome": "completed"},
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert ledger.flush()
    v = ledger.verify()
    assert v.ok and v.count == 200
    assert [e.seq for e in ledger.entries(limit=500)] == list(range(1, 201))


def test_two_ledger_instances_share_one_chain(engine) -> None:
    a, b = Ledger(engine), Ledger(engine)
    try:
        for i in range(10):
            (a if i % 2 else b).append("pinned", "r", payload={"target": str(i)})
            assert (a if i % 2 else b).flush()
        assert ledger_ok(a)
    finally:
        a.close()
        b.close()


def ledger_ok(lg: Ledger) -> bool:
    v = lg.verify()
    return v.ok and v.count == 10


def test_queue_overflow_drops_metric_rows_but_never_chain_entries(
    engine, monkeypatch
) -> None:
    lg = Ledger(engine, max_pending_rows=5)
    gate = threading.Event()
    real_write = lg._write

    def slow_write(batch):
        gate.wait(5)
        return real_write(batch)

    monkeypatch.setattr(lg, "_write", slow_write)
    try:
        lg.append("run_opened", "r1", payload={"kind": "eval"})
        for n in range(4):
            lg.append_metrics("r1", f"d{n}", [MetricRow("m", i) for i in range(4)])
        for n in range(10):
            lg.append(
                "doc_closed", "r1", doc_id=f"d{n}", payload={"outcome": "completed"}
            )
        lg.append("run_closed", "r1", payload={"closed_by": "completed"})
        gate.set()
        assert lg.flush()
        kinds = [e.kind for e in lg.entries(run_id="r1")]
        assert kinds.count("doc_closed") == 10 and kinds.count("run_closed") == 1
        gaps = lg.entries(run_id="r1", kind="gap")
        assert len(gaps) == 1 and gaps[0].payload["reason"] == "queue_overflow"
        closed = lg.entries(run_id="r1", kind="run_closed")[0]
        assert closed.payload["dropped_rows"] > 0
        assert lg.verify().ok
    finally:
        gate.set()
        lg.close()


def test_close_flushes_everything_queued(engine) -> None:
    lg = Ledger(engine)
    for i in range(50):
        lg.append("pinned", "r", payload={"target": str(i)})
    lg.close()
    other = Ledger(engine)
    try:
        assert len(other.entries(limit=100)) == 50
    finally:
        other.close()


def test_a_failing_write_is_retried_and_never_raises(engine, monkeypatch) -> None:
    lg = Ledger(engine)
    real_write = lg._write
    calls = {"n": 0}

    def flaky(batch):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("database is locked")
        return real_write(batch)

    monkeypatch.setattr(lg, "_write", flaky)
    try:
        assert lg.append("pinned", "r", payload={"target": "x"}) is True
        assert lg.flush(timeout=15)
        assert len(lg.entries()) == 1 and calls["n"] == 3
    finally:
        lg.close()


def test_append_never_raises_on_bad_input(ledger) -> None:
    assert ledger.append("not_a_kind", "r") is False
    assert (
        ledger.append("gap", "r", payload={"reason": "row_cap", "count": "x" * 10_000})
        is True
    )
    ledger.flush()
    assert ledger.entries()[0].payload == {
        "reason": "row_cap"
    }  # the bad value was dropped


def test_payload_allow_list_blocks_content_and_bounds_values() -> None:
    hostile = {
        "outcome": "completed",
        "filename": "Alice Smith SSN 123-45-6789.pdf",
        "judge_notes": "the plaintiff said...",
        "failure_reason": "FileNotFoundError: /home/alice/secret.pdf",
        "failure_class": "llm_timeout",
        "doc_type": "contract\n<script>alert(1)</script>",
        "usage_by_role": {
            "judge": {"prompt_tokens": 5, "calls": 1, "note": "x", "cost_usd": 0.5},
            "../bad key": {},
        },
        "usage_partial_nodes": ["verify", {"x": 1}],
        "audit_head": {"seq": 2, "entry_hash": "not hex"},
        "reviewer": "bob",
    }
    clean = sanitize_payload("doc_closed", hostile)
    assert set(clean) == {
        "outcome",
        "failure_class",
        "doc_type",
        "usage_by_role",
        "usage_partial_nodes",
    }
    assert clean["usage_by_role"] == {
        "judge": {"prompt_tokens": 5.0, "calls": 1.0, "cost_usd": 0.5}
    }
    assert "<" not in clean["doc_type"] and "\n" not in clean["doc_type"]
    assert clean["usage_partial_nodes"] == ["verify"]
    blob = canonical_json(clean)
    for leaked in ("Alice", "plaintiff", "secret", "bob"):
        assert leaked not in blob


def test_oversize_payload_is_rejected() -> None:
    with pytest.raises(ValueError):
        sanitize_payload(
            "checkpoint",
            {
                "heads": [
                    {"run_id": "r" * 60, "seq": i, "entry_hash": "a" * 64}
                    for i in range(200)
                ]
            },
        )
    with pytest.raises(ValueError):
        sanitize_payload("nope", {})


def test_entries_filters_and_paging(ledger) -> None:
    _run(ledger, "r1")
    _run(ledger, "r2", docs=1)
    assert {e.run_id for e in ledger.entries(run_id="r2")} == {"r2"}
    assert [e.kind for e in ledger.entries(kind="run_opened")] == [
        "run_opened",
        "run_opened",
    ]
    assert [e.doc_id for e in ledger.entries(doc_id="doc1")] == ["doc1"]
    assert [e.seq for e in ledger.entries(limit=2, offset=1)] == [2, 3]
    assert ledger.entries(descending=True, limit=1)[0].seq == ledger.head().seq
    assert [e.seq for e in ledger.entries(since_seq=7)] == [8]


def test_empty_ledger(ledger) -> None:
    assert ledger.head() is None
    v = ledger.verify()
    assert v.ok and v.count == 0


def test_non_finite_numbers_in_payloads_are_dropped_not_raised() -> None:
    clean = sanitize_payload(
        "doc_closed",
        {
            "rows": float("inf"),
            "duration_s": float("nan"),
            "invocation": 2,
            "usage_by_role": {"judge": {"cost_usd": float("inf"), "calls": 1}},
        },
    )
    assert clean == {"invocation": 2, "usage_by_role": {"judge": {"calls": 1.0}}}


def test_malformed_stored_payload_fails_verification_instead_of_raising(
    ledger, engine
) -> None:
    _run(ledger, "r1")
    with engine.begin() as conn:
        conn.execute(text("UPDATE ledger SET payload = 'not json' WHERE seq = 2"))
    v = ledger.verify()
    assert not v.ok and v.broken_at == 2 and v.detail == "undecodable entry"
    v = ledger.verify("r1")
    assert not v.ok and v.broken_at == 2 and v.detail == "undecodable entry"


def test_row_cap_holds_across_two_instances_on_one_run(engine) -> None:
    a, b = Ledger(engine, row_cap=10), Ledger(engine, row_cap=10)
    try:
        for i in range(4):
            lg = a if i % 2 else b
            lg.append_metrics("r1", "d", [MetricRow(f"m{j}", j) for j in range(4)])
            assert lg.flush()
        assert len(a.metric_rows("r1")) == 10
        assert len(a.entries(run_id="r1", kind="gap")) == 1
    finally:
        a.close()
        b.close()


def test_a_rolled_back_batch_does_not_double_count_drops(engine, monkeypatch) -> None:
    lg = Ledger(engine, row_cap=3)
    real_insert = lg._insert_entry
    calls = {"n": 0}

    def failing_once(conn, state, item):
        if item.kind == "run_closed" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("disk hiccup")
        return real_insert(conn, state, item)

    monkeypatch.setattr(lg, "_insert_entry", failing_once)
    try:
        lg.append_metrics(
            "r1", "d", [MetricRow(f"m{i}", i) for i in range(8)]
        )  # 5 over the cap
        lg.append("run_closed", "r1", payload={"closed_by": "completed"})
        assert lg.flush(timeout=15)
        closed = lg.entries(run_id="r1", kind="run_closed")[0]
        assert closed.payload["dropped_rows"] == 5
        assert len(lg.metric_rows("r1")) == 3
    finally:
        lg.close()


def test_dropped_rows_survive_a_restart_through_the_gap_count(engine) -> None:
    first = Ledger(engine, row_cap=2)
    first.append_metrics(
        "r1", "d", [MetricRow(f"m{i}", i) for i in range(6)]
    )  # 4 dropped
    first.close()
    second = Ledger(engine, row_cap=2)
    try:
        second.append("run_closed", "r1", payload={"closed_by": "completed"})
        assert second.flush()
        assert (
            second.entries(run_id="r1", kind="run_closed")[0].payload["dropped_rows"]
            == 4
        )
    finally:
        second.close()


def test_persistent_write_failures_shed_metric_rows_but_never_chain_entries(
    engine, monkeypatch
) -> None:
    lg = Ledger(engine, retry_base_s=0.001)
    real_write = lg._write

    def flaky(batch):
        if any(i.kind is None for i in batch):  # metric rows can never be written
            raise RuntimeError("database is locked")
        return real_write(batch)

    monkeypatch.setattr(lg, "_write", flaky)
    try:
        lg.append("run_opened", "r1", payload={"kind": "eval"})
        lg.append_metrics("r1", "d", [MetricRow("m", 1)])
        lg.append("doc_closed", "r1", doc_id="d", payload={"outcome": "completed"})
        assert lg.flush(timeout=20)
        kinds = [e.kind for e in lg.entries(run_id="r1")]
        assert kinds.count("run_opened") == 1 and kinds.count("doc_closed") == 1
        gaps = lg.entries(run_id="r1", kind="gap")
        assert [g.payload["reason"] for g in gaps] == ["write_failed"]
        assert lg.metric_rows("r1") == []
        assert lg.verify().ok
    finally:
        lg.close()


def test_chain_entries_are_retried_past_five_failures(engine, monkeypatch) -> None:
    lg = Ledger(engine, retry_base_s=0.001)
    real_write = lg._write
    calls = {"n": 0}

    def flaky(batch):
        calls["n"] += 1
        if calls["n"] <= 8:
            raise RuntimeError("database is locked")
        return real_write(batch)

    monkeypatch.setattr(lg, "_write", flaky)
    try:
        lg.append("pinned", "r1", payload={"target": "r1"})
        assert lg.flush(timeout=20)
        assert [e.kind for e in lg.entries()] == ["pinned"] and calls["n"] == 9
    finally:
        lg.close()
