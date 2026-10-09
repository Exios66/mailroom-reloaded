"""scripts/tui_seed_replay: the dev run carries every replay outcome, and seeding is idempotent."""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from mailroom_reloaded import settings
from mailroom_reloaded.obs.replay.timeline import build_timeline
from mailroom_reloaded.schemas.replay import Timeline
from mailroom_reloaded.storage import db, retention
from mailroom_reloaded.storage.db import init_db
from mailroom_reloaded.storage.ledger import Ledger, reset_ledger
from mailroom_reloaded.storage.span_store import SpanStore

SEED = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "tui_seed_replay"
    / "seed_replay.py"
)


@pytest.fixture(scope="module")
def seed_mod():
    spec = importlib.util.spec_from_file_location("seed_replay", SEED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def store(tmp_path):
    s = SpanStore(tmp_path / "traces.db")
    yield s
    s.close()


@pytest.fixture
def ledger(tmp_path):
    eng = init_db(tmp_path / "mailroom.db")
    lg = Ledger(eng)
    yield lg
    lg.close()
    eng.dispose()


def _timeline(seed_mod, store) -> Timeline:
    tl = build_timeline(f"run:{seed_mod.RUN_ID}", store=store)
    assert tl is not None
    return tl


def test_seeded_run_has_each_outcome(seed_mod, store, ledger) -> None:
    assert seed_mod.seed(store, ledger) > 0
    tl = _timeline(seed_mod, store)
    assert tl.session.id == f"run:{seed_mod.RUN_ID}"
    assert len(tl.entities) == 6
    status = {e.doc_id: e.final_status for e in tl.entities}
    assert Counter(status.values()) == {"archived": 4, "failed": 1, "parked": 1}

    # retry: the sort node ran twice, the second attempt tagged as a retry_sort
    (retried,) = {s.doc_id for s in tl.segments if s.attempt > 1}
    sorts = [s for s in tl.segments if s.doc_id == retried and s.node == "sort"]
    assert [s.attempt for s in sorts] == [1, 2]
    assert sorts[1].retry_kind == "retry_sort"
    assert [
        e.payload for e in tl.events if e.doc_id == retried and e.kind == "retry"
    ] == [{"kind": "retry_sort", "attempt": 1, "max_attempts": 2, "confidence": 0.6}]

    # failure: the document ends failed at the failed stage, its extract segment failed
    (failed,) = (e for e in tl.entities if e.final_status == "failed")
    assert failed.final_stage == "failed"
    assert failed.failure_class == "run_budget"
    failed_segs = [
        s for s in tl.segments if s.doc_id == failed.doc_id and s.status == "failed"
    ]
    assert [(s.node, s.reason) for s in failed_segs] == [
        ("extract", "deadline_exceeded")
    ]

    # parked: the document waits at human review with its judge cause
    (parked,) = (e for e in tl.entities if e.final_status == "parked")
    assert parked.final_stage == "review"
    assert parked.review_causes == ["judge_partial"]
    review = [
        s for s in tl.segments if s.doc_id == parked.doc_id and s.station == "review"
    ]
    assert [(s.node, s.status) for s in review] == [("human_review", "ok")]
    assert [
        e.payload for e in tl.events if e.doc_id == parked.doc_id and e.kind == "parked"
    ] == [{"reason": "needs review"}]

    # boss: a segment at the boss station and an escalation event addressed to it
    (boss_seg,) = [s for s in tl.segments if s.station == "boss"]
    assert status[boss_seg.doc_id] == "archived"
    assert [
        e.payload
        for e in tl.events
        if e.doc_id == boss_seg.doc_id and e.kind == "escalation"
    ] == [{"to": "boss", "reason": "conflict"}]

    # the rest are clean: archived, one attempt per node, no review causes
    clean = [
        e
        for e in tl.entities
        if e.doc_id not in {retried, boss_seg.doc_id} and e.final_status == "archived"
    ]
    assert len(clean) == 2
    assert all(e.review_causes == [] for e in clean)
    assert all(
        s.attempt == 1 for e in clean for s in tl.segments if s.doc_id == e.doc_id
    )

    # content-free: the fixtures' secret text never reaches the payload
    assert "SECRET" not in tl.model_dump_json()


def test_seed_is_idempotent(seed_mod, store, ledger) -> None:
    first = seed_mod.seed(store, ledger)
    before = _timeline(seed_mod, store).model_dump()
    assert seed_mod.seed(store, ledger) == 0
    assert store.count(seed_mod.RUN_ID) == first
    assert _timeline(seed_mod, store).model_dump() == before
    assert retention.pinned_runs(ledger) == {seed_mod.RUN_ID}
    assert len(ledger.entries(kind="pinned")) == 1


def test_pinned_run_survives_retention_prune(seed_mod, store, ledger) -> None:
    written = seed_mod.seed(store, ledger)
    # the rows are dated months before "now", so only the pin keeps them
    assert (
        retention.prune(store, ledger, settings=settings.Settings(_env_file=None)) == {}
    )
    assert store.count(seed_mod.RUN_ID) == written


def test_main_prints_run_and_path(seed_mod, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("MAILROOM_TRACE_STORE_PATH", raising=False)
    settings.get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    try:
        assert seed_mod.main() == 0
        assert seed_mod.main() == 0
    finally:
        reset_ledger()
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(f"replay run {seed_mod.RUN_ID}: ") and lines[0].endswith(
        "spans written"
    )
    assert lines[1] == f"/tui#replay=run:{seed_mod.RUN_ID}"
    assert lines[2] == f"replay run {seed_mod.RUN_ID}: already seeded"
    assert lines[3] == lines[1]
    assert (tmp_path / "traces.db").exists()


@pytest.mark.parametrize("value", [None, ""])
def test_main_refuses_without_base_dir(
    seed_mod, tmp_path, monkeypatch, capsys, value
) -> None:
    """A standalone run must never fall back to the default ./data store."""
    if value is None:
        monkeypatch.delenv("MAILROOM_BASE_DIR", raising=False)
    else:
        monkeypatch.setenv("MAILROOM_BASE_DIR", value)
    monkeypatch.chdir(tmp_path)
    assert seed_mod.main() == 2
    assert "MAILROOM_BASE_DIR" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
