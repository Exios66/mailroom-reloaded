"""Retention: keep classes, pin/unpin, prune ledger entries and the showcase seed."""

from __future__ import annotations

import hashlib
import json
import re
from importlib import resources

import pytest

from mailroom_reloaded.settings import Settings
from mailroom_reloaded.storage import retention
from mailroom_reloaded.storage.db import init_db
from mailroom_reloaded.storage.ledger import Ledger
from mailroom_reloaded.storage.retention import (
    SHOWCASE_RUN_IDS,
    KeepPolicy,
    effective_policy,
    maintain,
    parse_policy,
    pin,
    pinned_runs,
    prune,
    pruned_runs,
    seed_showcase,
    set_policy,
    unpin,
)
from mailroom_reloaded.storage.span_store import SpanStore

DAY = 86_400 * 1_000_000_000
NOW = 100 * DAY


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


def _row(span_id: str, run: str | None, start_ns: int) -> dict:
    return {
        "span_id": span_id,
        "trace_id": "t" + span_id,
        "parent_id": None,
        "name": "mailroom.document",
        "kind": "CHAIN",
        "start_ns": start_ns,
        "end_ns": start_ns + 10,
        "status": "OK",
        "doc_id": "d1",
        "run_id": run,
        "session_id": None,
        "station": None,
        "attrs": "{}",
        "events": "[]",
    }


OLD = NOW - 10 * DAY
FRESH = NOW - DAY


def _fill(store: SpanStore) -> None:
    store.write(
        [
            _row("a1", "eval-a", OLD),
            _row("a2", "eval-a", OLD + 1),
            _row("b1", "eval-b", OLD + 100),
            _row("c1", "eval-c", OLD + 200),
            _row("d1", "eval-d", FRESH),
            _row("s1", "showcase-clean", OLD),
            _row("u1", None, OLD),
        ]
    )


def _runs(store: SpanStore) -> set[str | None]:
    return {r["run_id"] for r in store.spans_between(0, 2**62)}


def test_default_pinned_drops_old_unkept(store, ledger):
    _fill(store)
    pin(ledger, "eval-b")
    ledger.flush()
    out = prune(store, ledger, now_ns=NOW, settings=Settings(_env_file=None))
    assert out == {"eval-a": 2, "eval-c": 1, "unscoped": 1}
    assert _runs(store) == {"eval-b", "eval-d", "showcase-clean"}


def test_recent_keeps_exactly_n_newest_besides_showcase(store, ledger):
    _fill(store)
    settings = Settings(_env_file=None, trace_keep="recent:2")
    prune(store, ledger, now_ns=NOW, settings=settings)
    # showcase-clean does not take a slot: the two newest ordinary runs are kept
    assert _runs(store) == {"eval-d", "eval-c", "showcase-clean"}


def test_open_run_is_never_pruned(store, ledger):
    _fill(store)
    ledger.append("run_opened", "eval-a", payload={"kind": "eval"})
    ledger.flush()
    out = prune(store, ledger, now_ns=NOW, settings=Settings(_env_file=None))
    assert "eval-a" not in out
    assert "eval-a" in _runs(store)


def test_pruned_entry_is_durable_before_spans_are_deleted(store, ledger):
    _fill(store)
    seen: list[int] = []
    real = store.delete_spans

    def spy(ids):
        seen.append(len(ledger.entries(kind="pruned")))
        return real(ids)

    store.delete_spans = spy
    prune(store, ledger, now_ns=NOW, settings=Settings(_env_file=None))
    assert seen == [3]  # eval-a, eval-b, eval-c already committed


def test_nothing_is_deleted_when_ledger_rejects(store, ledger, monkeypatch):
    _fill(store)
    before = store.count()
    monkeypatch.setattr(ledger, "append", lambda *a, **k: False)
    assert prune(store, ledger, now_ns=NOW, settings=Settings(_env_file=None)) == {}
    assert store.count() == before


def test_seed_rejects_traversal_name(store, tmp_path):
    d = _showcase_dir(tmp_path)
    m = json.loads((d / "manifest.json").read_text())
    m["files"]["../evil.json"] = "0" * 64
    (d / "manifest.json").write_text(json.dumps(m))
    assert set(seed_showcase(store, base=d)) == {
        "showcase-clean",
        "showcase-escalation",
    }


def test_all_deletes_nothing(store, ledger):
    _fill(store)
    before = store.count()
    assert (
        prune(
            store,
            ledger,
            now_ns=NOW,
            settings=Settings(_env_file=None, trace_keep="all"),
        )
        == {}
    )
    assert store.count() == before


def test_ledger_policy_overrides_settings(store, ledger):
    _fill(store)
    settings = Settings(_env_file=None, trace_keep="pinned")
    set_policy(ledger, "all")
    ledger.flush()
    assert effective_policy(ledger, settings) == KeepPolicy("all")
    assert prune(store, ledger, now_ns=NOW, settings=settings) == {}
    set_policy(ledger, "recent:3")
    ledger.flush()
    assert effective_policy(ledger, settings) == KeepPolicy("recent", 3)
    set_policy(ledger, "pinned")
    ledger.flush()
    assert (
        effective_policy(ledger, Settings(_env_file=None, trace_keep="all")).mode
        == "pinned"
    )


def test_prune_appends_one_pruned_entry_per_run_and_keeps_ledger(store, ledger):
    _fill(store)
    pin(ledger, "eval-b")
    ledger.flush()
    rows_before = len(ledger.entries(limit=1000))
    out = prune(store, ledger, now_ns=NOW, settings=Settings(_env_file=None))
    ledger.flush()
    entries = ledger.entries(kind="pruned", limit=1000)
    assert sorted(e.run_id for e in entries) == ["eval-a", "eval-c"]
    assert len(ledger.entries(limit=1000)) == rows_before + 2
    for e in entries:
        assert re.fullmatch(r"[0-9a-f]{64}", e.digest and e.payload["digest"])
        assert e.payload["target"] == e.run_id
        assert e.payload["counts"] == {"spans": out[e.run_id]}
    expected = hashlib.sha256(b"a1\na2").hexdigest()
    assert (
        next(e for e in entries if e.run_id == "eval-a").payload["digest"] == expected
    )
    assert pruned_runs(ledger) == {"eval-a", "eval-c"}
    assert ledger.verify().ok
    # second prune finds nothing more and appends nothing
    assert prune(store, ledger, now_ns=NOW, settings=Settings(_env_file=None)) == {}
    ledger.flush()
    assert len(ledger.entries(kind="pruned", limit=1000)) == 2


def test_pin_unpin_round_trip(ledger):
    pin(ledger, "eval-x")
    pin(ledger, "eval-y")
    ledger.flush()
    assert pinned_runs(ledger) == {"eval-x", "eval-y"}
    unpin(ledger, "eval-x")
    ledger.flush()
    assert pinned_runs(ledger) == {"eval-y"}
    pin(ledger, "eval-x")
    ledger.flush()
    assert pinned_runs(ledger) == {"eval-x", "eval-y"}


def test_unpin_showcase_and_invalid_ids_raise(ledger):
    with pytest.raises(ValueError):
        unpin(ledger, SHOWCASE_RUN_IDS[0])
    for bad in ("", "bad id", "x\n", "a" * 121, "é"):
        with pytest.raises(ValueError):
            pin(ledger, bad)
    with pytest.raises(ValueError):
        set_policy(ledger, "recent:0")
    ledger.flush()
    assert ledger.entries(limit=10) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, KeepPolicy("pinned")),
        ("", KeepPolicy("pinned")),
        ("  ", KeepPolicy("pinned")),
        ("pinned", KeepPolicy("pinned")),
        ("ALL", KeepPolicy("all")),
        ("recent:5", KeepPolicy("recent", 5)),
        (" Recent:1000 ", KeepPolicy("recent", 1000)),
        ("recent:0", KeepPolicy("pinned")),
        ("recent:1001", KeepPolicy("pinned")),
        ("recent:-3", KeepPolicy("pinned")),
        ("recent:x", KeepPolicy("pinned")),
        ("recent:", KeepPolicy("pinned")),
        ("bogus", KeepPolicy("pinned")),
    ],
)
def test_parse_policy(value, expected):
    assert parse_policy(value) == expected


def test_settings_trace_keep_never_raises(monkeypatch):
    monkeypatch.setenv("MAILROOM_TRACE_KEEP", "recent:99999")
    assert Settings(_env_file=None).trace_keep == "pinned"
    monkeypatch.setenv("MAILROOM_TRACE_KEEP", "Recent: 7")
    assert Settings(_env_file=None).trace_keep == "recent:7"
    monkeypatch.delenv("MAILROOM_TRACE_KEEP")
    assert Settings(_env_file=None).trace_keep == "pinned"


def test_spans_older_than(store):
    _fill(store)
    got = store.spans_older_than(
        NOW - 5 * DAY, exclude_runs={"eval-a", "showcase-clean"}
    )
    assert sorted(got, key=str) == sorted(
        [("eval-b", "b1"), ("eval-c", "c1"), (None, "u1")], key=str
    )


# ------------------------------------------------------------------ showcase seed
def _showcase_dir(tmp_path, runs=("showcase-clean", "showcase-escalation")):
    d = tmp_path / "showcase"
    d.mkdir()
    files = {}
    for i, run in enumerate(runs):
        rows = [
            {
                **_row(f"{run}-{n}", run, OLD + n),
                "attrs": {"mailroom.run_id": run},
                "events": [],
            }
            for n in range(3)
        ]
        raw = json.dumps({"run_id": run, "title": run, "spans": rows}).encode()
        (d / f"f{i}.json").write_bytes(raw)
        files[f"f{i}.json"] = hashlib.sha256(raw).hexdigest()
    (d / "manifest.json").write_text(json.dumps({"files": files}))
    return d


def test_seed_is_idempotent(store, tmp_path):
    d = _showcase_dir(tmp_path)
    assert seed_showcase(store, base=d) == ["showcase-clean", "showcase-escalation"]
    assert store.count() == 6
    assert seed_showcase(store, base=d) == ["showcase-clean", "showcase-escalation"]
    assert store.count() == 6
    row = store.spans_for_run("showcase-clean")[0]
    assert row["attrs"] == {"mailroom.run_id": "showcase-clean"}


def test_seed_skips_modified_file(store, tmp_path):
    d = _showcase_dir(tmp_path)
    (d / "f1.json").write_bytes((d / "f1.json").read_bytes() + b" ")
    assert seed_showcase(store, base=d) == ["showcase-clean"]
    assert store.count("showcase-escalation") == 0


def test_seed_skips_non_showcase_and_garbage(store, tmp_path):
    d = _showcase_dir(tmp_path, runs=("eval-sneaky",))
    assert seed_showcase(store, base=d) == []
    (d / "manifest.json").write_text("not json")
    assert seed_showcase(store, base=d) == []
    assert store.count() == 0


def test_seeded_showcase_survives_prune(store, ledger, tmp_path):
    seed_showcase(store, base=_showcase_dir(tmp_path))
    assert prune(store, ledger, now_ns=NOW, settings=Settings(_env_file=None)) == {}
    assert store.count() == 6


def test_real_showcase_files_load_and_replay(store):
    from mailroom_reloaded.obs.replay.timeline import timeline_from_spans

    base = resources.files("mailroom_reloaded") / "showcase"
    if not (base / "manifest.json").is_file():
        pytest.skip("showcase files not present")
    manifest = json.loads((base / "manifest.json").read_text("utf-8"))
    assert len(manifest["files"]) == 4
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((base / name).read_bytes()).hexdigest() == digest
    assert sorted(seed_showcase(store)) == sorted(SHOWCASE_RUN_IDS)
    for run_id in SHOWCASE_RUN_IDS:
        assert (
            timeline_from_spans(store.spans_for_run(run_id), "run", run_id) is not None
        )


def test_maintain_never_raises(tmp_path, ledger):
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    maintain(Broken(), ledger)  # type: ignore[arg-type]
    maintain(SpanStore(tmp_path / "no" / "such" / "\0bad.db"), ledger)
    assert retention.prune(Broken(), ledger) == {}  # type: ignore[arg-type]
