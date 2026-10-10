"""OTLP/JSON import into the span store: allow-list, bounds, idempotence, round trip."""

from __future__ import annotations

import json

import pytest

from mailroom_reloaded.obs.replay import otlp_import
from mailroom_reloaded.obs.replay.otlp_import import (
    OtlpImportError,
    import_otlp_file,
    parse_otlp,
)
from mailroom_reloaded.obs.replay.timeline import build_timeline, clear_cache
from mailroom_reloaded.schemas.replay import Timeline
from mailroom_reloaded.storage.span_store import SpanStore

RUN = "run-otlp-1"
T0 = 1_760_000_000 * 10**9
TRACE = "ab" * 16


def _kv(key: str, value) -> dict:
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": value}
    return {"key": key, "value": v}


def _span(n: int, name: str, start_s: float, end_s: float, parent: int | None = None, **attrs):
    return {
        "traceId": TRACE,
        "spanId": f"{n:016x}",
        **({"parentSpanId": f"{parent:016x}"} if parent else {}),
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(T0 + int(start_s * 1e9)),
        "endTimeUnixNano": str(T0 + int(end_s * 1e9)),
        "attributes": [_kv(k, v) for k, v in attrs.items()],
        "status": {"code": 1},
    }


def _doc(spans: list[dict], resource: dict | None = None) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_kv(k, v) for k, v in (resource or {}).items()]},
                "scopeSpans": [{"scope": {"name": "t"}, "spans": spans}],
            }
        ]
    }


def _spans() -> list[dict]:
    common = {"mailroom.run_id": RUN, "mailroom.doc_id": "doc-1"}
    return [
        _span(1, "mailroom.document", 0, 3, **common, **{"openinference.span.kind": "CHAIN"}),
        _span(
            2, "mailroom.node.sorter", 0.5, 2, parent=1, **common,
            **{"mailroom.node": "sorter", "mailroom.station": "sorter"},
        ),
        _span(
            3, "llm.call", 0.6, 1.8, parent=2, **common,
            **{
                "openinference.span.kind": "LLM",
                "llm.model_name": "m",
                "llm.token_count.total": 12,
                "llm.input_messages.0.message.content": "SECRET PROMPT",
                "llm.output_messages.0.message.content": "SECRET COMPLETION",
                "input.value": "DOCUMENT TEXT",
                "output.value": "MORE TEXT",
                "document.text": "body",
            },
        ),
    ]


@pytest.fixture
def store(tmp_path):
    s = SpanStore(tmp_path / "traces.db")
    clear_cache()
    yield s
    s.close()
    clear_cache()


def _write(tmp_path, content: str | bytes, name: str = "in.json"):
    p = tmp_path / name
    p.write_bytes(content if isinstance(content, bytes) else content.encode())
    return p


def test_import_build_export_validate(tmp_path, store) -> None:
    path = _write(tmp_path, json.dumps(_doc(_spans())))
    res = import_otlp_file(path, store)
    assert (res.parsed, res.stored, res.runs) == (3, 3, [RUN])
    tl = build_timeline(f"run:{RUN}", store=store)
    assert tl is not None and tl.version == "replay/v1"
    assert tl.session.source == "spans"
    assert any(s.station == "sorter" for s in tl.segments)
    again = Timeline.model_validate_json(tl.model_dump_json())
    assert again == tl


def test_idempotent_reimport(tmp_path, store) -> None:
    path = _write(tmp_path, json.dumps(_doc(_spans())))
    import_otlp_file(path, store)
    with pytest.raises(OtlpImportError, match="--append"):
        import_otlp_file(path, store)
    res = import_otlp_file(path, store, append=True)
    assert (res.parsed, res.stored, res.skipped) == (3, 0, 3)
    assert store.count(RUN) == 3


def test_allow_list_strips_content(tmp_path, store) -> None:
    import_otlp_file(_write(tmp_path, json.dumps(_doc(_spans()))), store)
    blob = json.dumps(store.spans_for_run(RUN))
    for secret in ("SECRET", "DOCUMENT TEXT", "MORE TEXT", "body", "input_messages"):
        assert secret not in blob
    llm = next(r for r in store.spans_for_run(RUN) if r["name"] == "llm.call")
    assert llm["attrs"]["llm.token_count.total"] == 12
    assert "document.text" not in llm["attrs"]


def test_node_summary_is_masked(tmp_path, store) -> None:
    span = _span(
        1, "mailroom.node.sorter", 0, 1, **{"mailroom.run_id": RUN, "input.value": "TEXT"}
    )
    import_otlp_file(_write(tmp_path, json.dumps(_doc([span]))), store)
    row = store.spans_for_run(RUN)[0]
    assert row["attrs"]["input.value"] == "<masked>"


def test_run_id_override_and_resource_attrs(tmp_path, store) -> None:
    span = _span(1, "mailroom.document", 0, 1)
    doc = _doc([span], resource={"session.id": "sess-1"})
    import_otlp_file(_write(tmp_path, json.dumps(doc)), store, run_id="forced-1")
    row = store.spans_for_run("forced-1")[0]
    assert row["session_id"] == "sess-1"
    assert store.spans_for_session("sess-1")


def test_jsonl(tmp_path, store) -> None:
    spans = _spans()
    lines = [json.dumps(_doc([s])) for s in spans]
    res = import_otlp_file(_write(tmp_path, "\n".join(lines) + "\n\n"), store)
    assert res.stored == 3


def test_optional_fields_tolerated(tmp_path, store) -> None:
    span = {
        "traceId": TRACE,
        "spanId": f"{7:016x}",
        "startTimeUnixNano": T0,
        "attributes": [_kv("mailroom.run_id", RUN), {"bogus": 1}, "junk"],
    }
    import_otlp_file(_write(tmp_path, json.dumps(_doc([span]))), store)
    row = store.spans_for_run(RUN)[0]
    assert row["name"] == "span" and row["end_ns"] == row["start_ns"]
    assert row["parent_id"] is None and row["status"] == "UNSET"


def test_events_filtered(tmp_path, store) -> None:
    span = _span(1, "mailroom.document", 0, 1, **{"mailroom.run_id": RUN})
    span["events"] = [
        {"name": "mailroom.retry", "timeUnixNano": str(T0), "attributes": [
            _kv("attempt", 2), _kv("evil", "text")]},
        {"name": "exception", "attributes": [
            _kv("exception.type", "ValueError"), _kv("exception.message", "SECRET")]},
        {"name": "other"},
    ]
    import_otlp_file(_write(tmp_path, json.dumps(_doc([span]))), store)
    evs = store.spans_for_run(RUN)[0]["events"]
    assert [e["name"] for e in evs] == ["mailroom.retry", "exception"]
    assert evs[0]["attrs"] == {"attempt": 2}
    assert "SECRET" not in json.dumps(evs)


def _bad(**over) -> str:
    span = _span(1, "mailroom.document", 0, 1, **{"mailroom.run_id": RUN})
    span.update(over)
    return json.dumps(_doc([span]))


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   \n",
        "not json",
        "[1, 2]",
        "{}",
        '{"resourceSpans": "x"}',
        '{"resourceSpans": []}',
        '{"resourceSpans": [{"scopeSpans": [{"spans": ["x"]}]}]}',
        "[" * 100_000,
        _bad(spanId="zz" * 8),
        _bad(spanId="0" * 16),
        _bad(spanId="abc"),
        _bad(traceId="ab" * 8),
        _bad(parentSpanId="xyz"),
        _bad(startTimeUnixNano="-5"),
        _bad(startTimeUnixNano=-5),
        _bad(startTimeUnixNano=None),
        _bad(startTimeUnixNano="0"),
        _bad(startTimeUnixNano="1e9"),
        _bad(startTimeUnixNano=True),
        _bad(startTimeUnixNano=float("nan")),
        _bad(startTimeUnixNano=float("inf")),
        _bad(startTimeUnixNano=str(2**70)),
        _bad(endTimeUnixNano="1"),
        _bad(events="x"),
        _bad(attributes="x"),
        _bad(attributes=[_kv("mailroom.run_id", "bad id with spaces")]),
        _bad(attributes=[_kv("mailroom.doc_id", "no-run")]),
    ],
)
def test_malformed_rejected_cleanly(tmp_path, store, content) -> None:
    with pytest.raises(OtlpImportError):
        import_otlp_file(_write(tmp_path, content), store)
    assert store.count() == 0


def test_nan_literal_rejected() -> None:
    with pytest.raises(OtlpImportError):
        parse_otlp(_bad().replace(f'"{T0}"', "NaN", 1))


def test_jsonl_error_names_line(tmp_path, store) -> None:
    good = json.dumps(_doc(_spans()[:1]))
    with pytest.raises(OtlpImportError, match="line 2"):
        parse_otlp(good + "\n{oops\n")
    assert store.count() == 0


def test_bad_run_id_option(tmp_path) -> None:
    with pytest.raises(OtlpImportError):
        parse_otlp(_bad(), run_id="bad id")


def test_oversized_input(tmp_path, store, monkeypatch) -> None:
    monkeypatch.setattr(otlp_import, "MAX_INPUT_BYTES", 100)
    with pytest.raises(OtlpImportError, match="exceeds"):
        import_otlp_file(_write(tmp_path, json.dumps(_doc(_spans()))), store)
    with pytest.raises(OtlpImportError, match="exceeds"):
        parse_otlp("x" * 101)


def test_too_many_spans(monkeypatch) -> None:
    monkeypatch.setattr(otlp_import, "MAX_SPANS", 2)
    with pytest.raises(OtlpImportError, match="more than 2"):
        parse_otlp(json.dumps(_doc(_spans())))


def test_unreadable_and_non_utf8(tmp_path, store) -> None:
    with pytest.raises(OtlpImportError, match="cannot read"):
        import_otlp_file(tmp_path / "missing.json", store)
    with pytest.raises(OtlpImportError, match="UTF-8"):
        import_otlp_file(_write(tmp_path, b"\xff\xfe\x00bad"), store)


def _file(tmp_path, spans: list[dict], name: str = "x.json"):
    return _write(tmp_path, json.dumps(_doc(spans)), name)


def test_refuses_showcase_and_pruned_runs(tmp_path, store) -> None:
    path = _file(tmp_path, [_span(1, "mailroom.document", 0, 1, **{"mailroom.run_id": "x"})])
    with pytest.raises(OtlpImportError, match="showcase"):
        import_otlp_file(path, store, run_id="showcase-clean")
    with pytest.raises(OtlpImportError, match="showcase"):
        import_otlp_file(path, store, run_id="showcase-new", append=True)
    with pytest.raises(OtlpImportError, match="pruned"):
        import_otlp_file(path, store, run_id="gone-1", append=True, pruned={"gone-1"})
    assert store.count() == 0


def test_existing_run_not_starved(tmp_path) -> None:
    st = SpanStore(tmp_path / "t.db", rows_per_run=5)
    path = _file(tmp_path, [_span(i, "evil", 0, 1) for i in range(1, 10)])
    st.write(parse_otlp(json.dumps(_doc([_span(100, "real", 0, 1)])), run_id="R"))
    with pytest.raises(OtlpImportError, match="already has 1"):
        import_otlp_file(path, st, run_id="R")
    assert [r["name"] for r in st.spans_for_run("R")] == ["real"]
    st.close()


def test_cross_run_span_id_collision_reported(tmp_path, store) -> None:
    import_otlp_file(_file(tmp_path, [_span(1, "a", 0, 1)]), store, run_id="R")
    res = import_otlp_file(_file(tmp_path, [_span(1, "a", 0, 1)], "y.json"), store, run_id="Q")
    assert (res.parsed, res.stored, res.skipped) == (1, 0, 1)


def test_session_only_spans_need_run_id(tmp_path, store) -> None:
    span = _span(1, "mailroom.document", 0, 1, **{"session.id": "sess-1"})
    path = _file(tmp_path, [span])
    with pytest.raises(OtlpImportError, match="no mailroom.run_id"):
        import_otlp_file(path, store)
    res = import_otlp_file(path, store, run_id="r-sess")
    assert res.sessions == ["sess-1"]


def _attr_span(*attrs: dict, **over) -> str:
    span = {
        "traceId": TRACE,
        "spanId": f"{1:016x}",
        "startTimeUnixNano": "5",
        "attributes": [_kv("mailroom.run_id", RUN), *attrs],
        **over,
    }
    return json.dumps(_doc([span]))


@pytest.mark.parametrize(
    "value",
    [
        {"doubleValue": 10**400},
        {"intValue": "--5"},
        {"intValue": "\u00b2"},
        {"intValue": "\u0663"},
        {"intValue": 10**4000},
        {"intValue": "9" * 30},
    ],
)
def test_odd_numbers_never_traceback(value) -> None:
    rows = parse_otlp(_attr_span({"key": "mailroom.x", "value": value}))
    assert "mailroom.x" not in json.loads(rows[0]["attrs"])


def test_huge_json_numbers_clean_error() -> None:
    big = "1" + "0" * 5000
    with pytest.raises(OtlpImportError):
        parse_otlp(_attr_span().replace('"5"', big))
    with pytest.raises(OtlpImportError):
        parse_otlp(_attr_span() + "\n" + _attr_span().replace('"5"', big))
    with pytest.raises(OtlpImportError):
        parse_otlp("[" * 100_000)


def test_int_value_forms() -> None:
    rows = parse_otlp(_attr_span({"key": "mailroom.n", "value": {"intValue": "-7"}}))
    assert json.loads(rows[0]["attrs"])["mailroom.n"] == -7


@pytest.mark.parametrize(
    "key,value",
    [
        ("mailroom.doc_id", {"arrayValue": {"values": [{"stringValue": "a"}]}}),
        ("openinference.span.kind", {"arrayValue": {"values": [{"stringValue": "a"}]}}),
        ("mailroom.station", {"intValue": "5"}),
        ("mailroom.doc_id", {"boolValue": True}),
    ],
)
def test_non_string_columns_dropped(tmp_path, store, key, value) -> None:
    path = _write(tmp_path, _attr_span({"key": key, "value": value}))
    import_otlp_file(path, store)
    row = store.spans_for_run(RUN)[0]
    assert key not in row["attrs"]
    assert row["doc_id"] is None and row["kind"] is None and row["station"] is None
