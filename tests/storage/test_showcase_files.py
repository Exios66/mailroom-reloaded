"""The shipped showcase trace runs: integrity, store round-trip, replayability, privacy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fixtures.replay import make_showcase

from mailroom_reloaded.obs.replay.timeline import clear_cache, timeline_from_spans
from mailroom_reloaded.storage.span_store import SpanStore

SHOWCASE = Path(make_showcase.OUT_DIR)
RUN_IDS = (
    "showcase-clean",
    "showcase-escalation",
    "showcase-parked-failed",
    "showcase-judge-arbiter",
)
FILES = ("clean.json", "escalation.json", "parked-failed.json", "judge-arbiter.json")
FORBIDDEN = (
    "input.value",
    "output.value",
    "llm.input_messages",
    "llm.output_messages",
    "exception.message",
)


def _load(name: str) -> dict:
    return json.loads((SHOWCASE / name).read_text())


@pytest.fixture(scope="module")
def payloads() -> dict[str, dict]:
    return {name: _load(name) for name in FILES}


def _store_rows(spans: list[dict]) -> list[dict]:
    return [
        {**s, "attrs": json.dumps(s["attrs"]), "events": json.dumps(s["events"])}
        for s in spans
    ]


def test_manifest_matches_files() -> None:
    manifest = json.loads((SHOWCASE / "manifest.json").read_text())
    assert sorted(manifest["files"]) == sorted(FILES)
    assert list(manifest["files"]) == sorted(manifest["files"])
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((SHOWCASE / name).read_bytes()).hexdigest() == digest


def test_run_ids_and_shape(payloads) -> None:
    assert tuple(p["run_id"] for p in payloads.values()) == RUN_IDS
    for name, p in payloads.items():
        assert set(p) == {"run_id", "title", "spans"}
        assert p["title"]
        assert len((SHOWCASE / name).read_bytes()) < 60_000
        spans = p["spans"]
        assert [(s["start_ns"], s["span_id"]) for s in spans] == sorted(
            (s["start_ns"], s["span_id"]) for s in spans
        )
        for s in spans:
            assert s["run_id"] == p["run_id"]
            assert s["session_id"].startswith("showcase-")
            assert isinstance(s["attrs"], dict) and isinstance(s["events"], list)
            assert s["start_ns"] >= 1_767_225_600 * 10**9  # 2026-01-01
            assert len(s["span_id"]) == 16 and len(s["trace_id"]) == 32
            if s["doc_id"]:
                assert s["doc_id"].startswith("doc-showcase-")


@pytest.mark.parametrize("name", FILES)
def test_loads_into_span_store_without_drops(name, tmp_path, payloads) -> None:
    p = payloads[name]
    store = SpanStore(tmp_path / "traces.db")
    assert store.write(_store_rows(p["spans"])) == len(p["spans"])
    stored = store.spans_for_run(p["run_id"])
    assert len(stored) == len(p["spans"])
    by_id = {s["span_id"]: s for s in stored}
    # the allow-list dropped nothing: stored attrs and events equal the shipped ones
    for s in p["spans"]:
        assert by_id[s["span_id"]]["attrs"] == s["attrs"]
        assert by_id[s["span_id"]]["events"] == s["events"]


@pytest.mark.parametrize("name", FILES)
def test_timeline_builds(name, tmp_path, payloads) -> None:
    clear_cache()
    p = payloads[name]
    store = SpanStore(tmp_path / "traces.db")
    store.write(_store_rows(p["spans"]))
    tl = timeline_from_spans(store.spans_for_run(p["run_id"]), "run", p["run_id"])
    assert tl is not None
    assert len({s.doc_id for s in tl.segments}) >= 3
    clear_cache()


def test_ids_unique_across_files(payloads) -> None:
    span_ids, trace_ids, doc_ids = [], set(), set()
    for p in payloads.values():
        span_ids += [s["span_id"] for s in p["spans"]]
        trace_ids |= {s["trace_id"] for s in p["spans"]}
        doc_ids |= {s["doc_id"] for s in p["spans"] if s["doc_id"]}
    assert len(span_ids) == len(set(span_ids))
    all_traces = sum(
        len({s["trace_id"] for s in p["spans"]}) for p in payloads.values()
    )
    assert all_traces == len(trace_ids)
    all_docs = sum(
        len({s["doc_id"] for s in p["spans"] if s["doc_id"]}) for p in payloads.values()
    )
    assert all_docs == len(doc_ids)


def _events(p: dict):
    return [e for s in p["spans"] for e in s["events"]]


def test_scenarios(payloads) -> None:
    esc = payloads["escalation.json"]
    assert any(e["name"] == "mailroom.escalation" for e in _events(esc))
    assert any(e["name"] == "mailroom.retry" for e in _events(esc))
    pf = payloads["parked-failed.json"]
    assert any(e["name"] == "mailroom.parked" for e in _events(pf))
    assert any(
        s["status"] == "ERROR" and s["name"].startswith("mailroom.node.")
        for s in pf["spans"]
    )
    clean = payloads["clean.json"]
    assert all(
        s["attrs"]["mailroom.status"] == "archived"
        for s in clean["spans"]
        if s["name"] == "mailroom.document"
    )
    assert not any(e["name"] == "mailroom.retry" for e in _events(clean))


def test_judge_arbiter_has_most_generations(payloads) -> None:
    def gens(p: dict) -> int:
        return sum(1 for s in p["spans"] if s["name"].startswith("mailroom.llm."))

    counts = {n: gens(p) for n, p in payloads.items()}
    top = counts.pop("judge-arbiter.json")
    assert top > max(counts.values())
    roles = {
        s["attrs"].get("mailroom.role") for s in payloads["judge-arbiter.json"]["spans"]
    }
    assert {"judge", "arbiter"} <= roles


def test_no_content(payloads) -> None:
    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                yield k
                yield from walk(x)
        elif isinstance(v, list):
            for x in v:
                yield from walk(x)
        else:
            yield v

    for p in payloads.values():
        for s in p["spans"]:
            for container in (s["attrs"], s["events"]):
                for item in walk(container):
                    if isinstance(item, str):
                        assert len(item) <= 300
                        assert not any(f in item for f in FORBIDDEN)
    for name in FILES:
        text = (SHOWCASE / name).read_text()
        assert not any(f in text for f in FORBIDDEN)
        assert "SECRET" not in text and ".pdf" not in text


def test_generator_is_reproducible() -> None:
    first = make_showcase.render()
    assert first == make_showcase.render()
    for name, data in first.items():
        assert (SHOWCASE / name).read_bytes() == data
