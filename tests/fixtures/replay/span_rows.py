"""Hand-built span-store rows for the replay timeline tests.

Five documents (happy, retry, failed, parked, boss) in one run, with times in seconds
after ``T0``. Rows have the shape ``SpanStore`` returns (``attrs``/``events`` decoded).
``to_store_rows`` re-encodes them for ``SpanStore.write``.
"""

from __future__ import annotations

import json
from typing import Any

T0 = 1_760_000_000  # epoch seconds of the earliest span
RUN = "run-1"
SESSION = "sess-1"
_n = 0


def ns(t: float) -> int:
    return int((T0 + t) * 1e9)


def _sid() -> str:
    global _n
    _n += 1
    return f"{_n:016x}"


def row(
    name: str,
    start: float,
    end: float,
    *,
    doc: str | None = None,
    trace: str = "t" * 32,
    parent: str | None = None,
    kind: str | None = None,
    status: str = "OK",
    station: str | None = None,
    attrs: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    span_id: str | None = None,
) -> dict[str, Any]:
    a = dict(attrs or {})
    if doc:
        a["mailroom.doc_id"] = doc
    a["mailroom.run_id"] = RUN
    a["session.id"] = SESSION
    if station:
        a["mailroom.station"] = station
    if kind:
        a["openinference.span.kind"] = kind
    return {
        "span_id": span_id or _sid(),
        "trace_id": trace,
        "parent_id": parent,
        "name": name,
        "kind": kind,
        "start_ns": ns(start),
        "end_ns": ns(end),
        "status": status,
        "doc_id": doc,
        "run_id": RUN,
        "session_id": SESSION,
        "station": station,
        "attrs": a,
        "events": list(events or []),
    }


def ev(name: str, t: float, **attrs: Any) -> dict[str, Any]:
    return {"name": f"mailroom.{name}", "t": ns(t), "attrs": attrs}


def score_ev(name: str, value: Any, t: float) -> dict[str, Any]:
    return {
        "name": "mailroom.score",
        "t": ns(t),
        "attrs": {"name": name, "value": value},
    }


def _node(
    doc, trace, root, node, station, t0, t1, *, attrs=None, events=None, status="OK"
):
    a = {"mailroom.node": node, "mailroom.attempt": 1, **(attrs or {})}
    return row(
        f"mailroom.node.{node}", t0, t1, doc=doc, trace=trace, parent=root["span_id"],
        kind="AGENT", status=status, station=station, attrs=a, events=events,
    )  # fmt: skip


def _llm(doc, trace, parent, role, t0, t1, prompt, completion, cost, *, leak=False):
    a = {
        "mailroom.role": role,
        "mailroom.model": "m-1",
        "mailroom.tokens.prompt": prompt,
        "mailroom.tokens.completion": completion,
        "mailroom.cost.total": cost,
    }
    if leak:
        a["llm.input_messages.0.message.content"] = "SECRET PROMPT TEXT"
        a["llm.output_messages.0.message.content"] = "SECRET COMPLETION"
        a["input.value"] = "SECRET DOCUMENT BODY"
    return row(
        f"mailroom.llm.{role}", t0, t1, doc=None, trace=trace, parent=parent["span_id"],
        kind="SPAN", attrs=a,
    )  # fmt: skip


def _root(doc, trace, t0, t1, *, status, stage, scores, extra=None, events=None):
    a = {
        "mailroom.filename": f"{doc}.pdf",
        "mailroom.environment": "eval",
        "mailroom.status": status,
        "mailroom.stage": stage,
        "mailroom.doc_type": "invoice",
        "mailroom.gt.expected_doc_class": "invoice",
        "input.value": json.dumps({"filename": f"{doc}.pdf"}),
        **(extra or {}),
    }
    for k, v in scores.items():
        a[f"mailroom.score.{k}"] = v
    return row(
        "mailroom.document", t0, t1, doc=doc, trace=trace, kind="CHAIN",
        status="ERROR" if status == "failed" else "OK", attrs=a, events=events,
    )  # fmt: skip


def happy() -> list[dict[str, Any]]:
    d, tr = "doc-a", "a" * 32
    root = _root(
        d, tr, 0, 10, status="archived", stage="archive",
        scores={"success_rate": 1.0, "extraction_confidence": 0.9, "total_tokens": 500,
                "estimated_cost_usd": 0.01, "llm_call_count": 2},
        extra={"mailroom.usage.total_tokens": 999, "mailroom.usage.cost_usd": 9.9},
    )  # fmt: skip
    sort = _node(d, tr, root, "sort", "sorter", 1, 3)
    ex = _node(d, tr, root, "extract", "specialist", 3.5, 7)
    return [
        root,
        _node(d, tr, root, "ingest", "intake", 0, 1,
              attrs={"mailroom.score.intake_prep_completeness": 1.0},
              events=[score_ev("intake_prep_completeness", 1.0, 0.9)]),
        sort,
        _llm(d, tr, sort, "sorter", 1.2, 2.8, 100, 20, 0.001, leak=True),
        _node(d, tr, root, "gate_classify", "gate", 3, 3.5,
              events=[ev("gate_decision", 3.2, stage="classify", action="pass", reason="ok", confidence=0.95)]),
        ex,
        _llm(d, tr, ex, "specialist", 4, 6.5, 300, 80, 0.002),
        _node(d, tr, root, "verify", "judge", 7, 8.5, attrs={"mailroom.judge.label": "complete", "mailroom.judge.score": 0.9}),
        _node(d, tr, root, "report_catalog_archive", "archive", 8.5, 10),
    ]  # fmt: skip


def retry() -> list[dict[str, Any]]:
    d, tr = "doc-b", "b" * 32
    root = _root(
        d, tr, 2, 12, status="archived", stage="archive",
        scores={"success_rate": 0.0, "extraction_confidence": 0.8, "total_tokens": 900,
                "estimated_cost_usd": 0.02, "llm_call_count": 4},
    )  # fmt: skip
    return [
        root,
        _node(d, tr, root, "sort", "sorter", 2, 4),
        _node(d, tr, root, "gate_classify", "gate", 4, 4.5, events=[
            ev("gate_decision", 4.2, stage="classify", action="retry", reason="low confidence", confidence=0.6),
            ev("retry", 4.4, kind="retry_sort", attempt=1, max_attempts=2, confidence=0.6),
        ]),
        _node(d, tr, root, "sort", "sorter", 4.5, 6.5, attrs={"mailroom.attempt": 2, "mailroom.retry_kind": "retry_sort"}),
        _node(d, tr, root, "extract", "specialist", 7, 11),
        _node(d, tr, root, "report_catalog_archive", "archive", 11, 12),
    ]  # fmt: skip


def failed() -> list[dict[str, Any]]:
    d, tr = "doc-c", "c" * 32
    root = _root(
        d, tr, 3, 9, status="failed", stage="failed",
        scores={"success_rate": 0.0, "total_tokens": 300, "estimated_cost_usd": 0.005,
                "review_causes": json.dumps(["extraction_miss"])},
        extra={"mailroom.failure_class": "run_budget"},
        events=[{"name": "exception", "t": ns(9), "attrs": {"exception.type": "TimeoutError"}}],
    )  # fmt: skip
    return [
        root,
        _node(d, tr, root, "ingest", "intake", 3, 4),
        _node(d, tr, root, "extract", "specialist", 4, 9, status="ERROR",
              attrs={"mailroom.fail_reason": "deadline_exceeded", "mailroom.failure_class": "run_budget"}),
    ]  # fmt: skip


def parked() -> list[dict[str, Any]]:
    """No token/cost scores: totals come from the summed generations."""
    d, tr = "doc-d", "d" * 32
    root = _root(
        d, tr, 4, 10, status="parked", stage="review",
        scores={"success_rate": 0.0, "review_causes": json.dumps(["judge_partial"])},
    )  # fmt: skip
    sort = _node(d, tr, root, "sort", "sorter", 4, 5)
    ex = _node(d, tr, root, "extract", "specialist", 5, 8)
    return [
        root,
        sort,
        _llm(d, tr, sort, "sorter", 4.1, 4.9, 100, 20, 0.002),
        ex,
        _llm(d, tr, ex, "specialist", 5.1, 7.9, 50, 10, 0.001),
        _node(d, tr, root, "human_review", "review", 8, 10,
              attrs={"mailroom.judge.label": "partial"},
              events=[ev("parked", 8.1, reason="needs review", text="SECRET EVENT TEXT")]),
        row("mailroom.node.verify", 7.5, 8, doc=d, trace=tr, parent=root["span_id"], kind="EVALUATOR",
            station="judge", attrs={"mailroom.node": "verify", "mailroom.judge.label": "partial", "mailroom.judge.score": 0.5}),
    ]  # fmt: skip


def boss() -> list[dict[str, Any]]:
    d, tr = "doc-e", "e" * 32
    root = _root(
        d, tr, 5, 16, status="archived", stage="archive",
        scores={"success_rate": 0.0, "extraction_confidence": 0.7, "total_tokens": 1500,
                "estimated_cost_usd": 0.05},
    )  # fmt: skip
    return [
        root,
        _node(d, tr, root, "sort", "sorter", 5, 9),
        _node(d, tr, root, "verify", "judge", 9, 11, attrs={"mailroom.judge.label": "complete", "mailroom.judge.score": 0.7},
              events=[ev("arbiter", 10.5, decision="escalate", retry_count=1),
                      ev("custom_thing", 10.6, note="x" * 1000, nested=["a"], n=3)]),
        _node(d, tr, root, "boss", "boss", 11, 14, events=[ev("escalation", 11.1, to="boss", reason="conflict")]),
        _node(d, tr, root, "report_catalog_archive", "archive", 14, 16),
    ]  # fmt: skip


def llm_child() -> list[dict[str, Any]]:
    """An LLM span without ``mailroom.tokens.*`` whose instrumentor child carries the counts."""
    d, tr = "doc-f", "f" * 32
    root = _root(d, tr, 0, 4, status="archived", stage="archive", scores={})
    node = _node(d, tr, root, "sort", "sorter", 0, 4)
    parent = row(
        "mailroom.llm.sorter", 1, 3, trace=tr, parent=node["span_id"], kind="SPAN",
        attrs={"mailroom.role": "sorter", "mailroom.model": "m-1"},
    )  # fmt: skip
    child = row(
        "ChatCompletion", 1.1, 2.9, trace=tr, parent=parent["span_id"], kind="LLM",
        attrs={"llm.token_count.prompt": 40, "llm.token_count.completion": 7},
    )  # fmt: skip
    return [root, node, parent, child]


def all_rows() -> list[dict[str, Any]]:
    return happy() + retry() + failed() + parked() + boss()


def to_store_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-encode decoded rows for ``SpanStore.write``."""
    return [
        {**r, "attrs": json.dumps(r["attrs"]), "events": json.dumps(r["events"])}
        for r in rows
    ]
