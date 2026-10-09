"""Build a ``replay/v1`` :class:`Timeline` from stored span rows.

:func:`timeline_from_spans` is a pure function over the rows a
:class:`~mailroom_reloaded.storage.span_store.SpanStore` returns. It reads only an
explicit set of attributes (never ``input.value`` / ``output.value`` or any ``llm.*``
message key), so even a row carrying content cannot leak it into the payload.

:func:`build_timeline` resolves a session id, reads the span store, falls back to the
audit log when there are no spans, applies the window and payload cap, and caches the
result keyed by the store watermark.
"""

from __future__ import annotations

import json
import math
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailroom_reloaded.obs import attrs as A
from mailroom_reloaded.obs.scores import SCORE_PREFIX, spec_for
from mailroom_reloaded.schemas.replay import (
    Entity,
    Generation,
    ReplayEvent,
    ReplayScore,
    Rollups,
    Segment,
    Session,
    StationInfo,
    StationStats,
    Timeline,
    Totals,
    Window,
)

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "MAX_SEGMENTS",
    "build_timeline",
    "clear_cache",
    "stations",
    "timeline_from_spans",
]

#: Payload cap per response; above either limit the window is shrunk automatically.
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_SEGMENTS = 20_000
CACHE_SIZE = 16

_MAX_STR = 256
_MAX_PAYLOAD_KEYS = 16
_NODE_PREFIX = "mailroom.node."
_LLM_PREFIX = "mailroom.llm."
_ROOT_NAME = "mailroom.document"
_VERDICTS = {"complete": "CORRECT", "partial": "PARTIAL", "incomplete": "MISS"}
_RETRY_EVENT_KINDS = {"retry"}
#: Event keys that look like they carry content are never passed through.
_CONTENT_HINTS = (
    "text",
    "content",
    "message",
    "prompt",
    "completion",
    "input",
    "output",
    "body",
)


def stations() -> list[StationInfo]:
    """The track, in order, from :data:`obs.attrs.STATIONS`."""
    return [
        StationInfo(
            id=s.id,
            label=s.label,
            phase=s.phase,
            order=i,
            kind=s.kind,  # type: ignore[arg-type]
            color_token=s.color_token,
        )
        for i, s in enumerate(A.STATIONS)
    ]


# ------------------------------------------------------------------ small helpers
def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if math.isfinite(value) else default


def _int(value: Any, default: int = 0) -> int:
    return int(_num(value, default))


def _str(value: Any, limit: int = _MAX_STR) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (``q`` in 0..1) of ``values``."""
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _scalar_payload(attrs: dict[str, Any]) -> dict[str, Any]:
    """Event attributes reduced to bounded scalars (lists and objects are dropped)."""
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if len(out) >= _MAX_PAYLOAD_KEYS:
            break
        if any(h in str(key).lower() for h in _CONTENT_HINTS):
            continue
        if isinstance(value, (bool, int)):
            out[str(key)[:64]] = value
        elif isinstance(value, float):
            if math.isfinite(value):
                out[str(key)[:64]] = value
        elif isinstance(value, str):
            out[str(key)[:64]] = value[:_MAX_STR]
    return out


def _data_type(name: str, value: Any) -> str:
    spec = spec_for(name)
    if spec is not None:
        return spec.data_type
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "numeric"
    return "categorical"


def _score_value(name: str, value: Any, data_type: str) -> Any:
    if data_type == "json" and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value[:_MAX_STR]
    if isinstance(value, str):
        return value[:_MAX_STR]
    return value


def _is_llm(row: dict[str, Any]) -> bool:
    return row["kind"] == "LLM" or str(row["name"]).startswith(_LLM_PREFIX)


# ------------------------------------------------------------------ builder
def timeline_from_spans(
    rows: list[dict[str, Any]], kind: str, key: str
) -> Timeline | None:
    """The timeline of ``rows`` (span-store rows), or ``None`` when there are none."""
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (r["start_ns"], r["span_id"]))
    t0_ns = min(r["start_ns"] for r in rows)
    end_ns = max(max(r["end_ns"], r["start_ns"]) for r in rows)

    def rel(ns: int) -> float:
        return round((ns - t0_ns) / 1e9, 6)

    by_id = {r["span_id"]: r for r in rows}
    root_doc_by_trace = {
        r["trace_id"]: r["doc_id"]
        for r in rows
        if r["name"] == _ROOT_NAME and r["doc_id"]
    }

    def doc_of(row: dict[str, Any]) -> str | None:
        seen = 0
        cur: dict[str, Any] | None = row
        while cur is not None and seen < 32:
            if cur["doc_id"]:
                return cur["doc_id"]
            cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
            seen += 1
        return root_doc_by_trace.get(row["trace_id"])

    def station_of(row: dict[str, Any]) -> str | None:
        cur: dict[str, Any] | None = row
        seen = 0
        while cur is not None and seen < 32:
            st = cur["station"] or cur["attrs"].get(A.STATION)
            if st:
                return str(st)
            cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
            seen += 1
        return None

    segments: list[Segment] = []
    generations: list[Generation] = []
    events: list[ReplayEvent] = []
    scores: list[ReplayScore] = []
    roots: dict[str, dict[str, Any]] = {}
    judge: dict[str, dict[str, Any]] = {}
    review_causes_attr: dict[str, list[str]] = {}
    env = "live"

    for row in rows:
        a: dict[str, Any] = row["attrs"]
        name = str(row["name"])
        doc = doc_of(row)
        if doc is None:
            continue
        t0, t1 = rel(row["start_ns"]), rel(max(row["end_ns"], row["start_ns"]))
        is_node = name.startswith(_NODE_PREFIX)

        if name == _ROOT_NAME:
            roots.setdefault(doc, row)
            env = _str(a.get(A.ENVIRONMENT)) or env
        elif is_node:
            node = str(a.get(A.NODE) or name[len(_NODE_PREFIX) :])
            try:
                station = row["station"] or a.get(A.STATION) or A.station_for(node).id
            except KeyError:
                station = row["station"] or a.get(A.STATION) or node
            failed = row["status"] == "ERROR"
            segments.append(
                Segment(
                    doc_id=doc,
                    node=node[:64],
                    station=str(station)[:64],
                    t0=t0,
                    t1=t1,
                    attempt=max(1, _int(a.get(A.ATTEMPT), 1)),
                    retry_kind=_str(a.get(A.RETRY_KIND), 64),
                    status="failed" if failed else "ok",
                    reason=_str(a.get(A.FAIL_REASON)),
                    tokens=_int(a["mailroom.tokens.used"])
                    if "mailroom.tokens.used" in a
                    else None,
                    cost_usd=_num(a["mailroom.cost_usd"])
                    if "mailroom.cost_usd" in a
                    else None,
                    llm_calls=_int(a["mailroom.llm_calls"])
                    if "mailroom.llm_calls" in a
                    else None,
                    span_id=row["span_id"],
                )
            )
            if node == "verify":
                judge[doc] = a
            if node == "human_review":
                raw = a.get("mailroom.review.causes")
                causes = _causes(raw)
                if causes:
                    review_causes_attr[doc] = causes
        elif _is_llm(row):
            parent = by_id.get(row["parent_id"]) if row["parent_id"] else None
            if (
                row["kind"] == "LLM"
                and parent is not None
                and str(parent["name"]).startswith(_LLM_PREFIX)
            ):
                pass  # the instrumentor span under our own mailroom.llm.* span: count once
            else:
                generations.append(_generation(row, doc, t0, t1))

        # events
        for ev in row["events"]:
            ename = str(ev.get("name", ""))
            eattrs = ev.get("attrs") or {}
            if ename == "mailroom.score":
                continue  # handled with the score attributes below
            kind_ = ename.removeprefix("mailroom.")
            ens = _int(ev.get("t"), row["start_ns"])
            events.append(
                ReplayEvent(
                    t=rel(
                        min(
                            max(ens, row["start_ns"]),
                            max(row["end_ns"], row["start_ns"]),
                        )
                    ),
                    doc_id=doc,
                    kind=kind_[:64],
                    station=station_of(row),
                    payload=_scalar_payload(eattrs),
                )
            )

        # scores: attributes win, score events fill any gap
        seen_scores: set[str] = set()
        for k, v in a.items():
            if k.startswith(SCORE_PREFIX) and len(k) > len(SCORE_PREFIX):
                seen_scores.add(k[len(SCORE_PREFIX) :])
                scores.append(_score(row, doc, k[len(SCORE_PREFIX) :], v, t1))
        for ev in row["events"]:
            if ev.get("name") == "mailroom.score":
                eattrs = ev.get("attrs") or {}
                sname = eattrs.get("name")
                if (
                    isinstance(sname, str)
                    and sname
                    and sname not in seen_scores
                    and "value" in eattrs
                ):
                    seen_scores.add(sname)
                    scores.append(_score(row, doc, sname[:128], eattrs["value"], t1))

    segments.sort(key=lambda s: (s.t0, s.t1, s.node))
    generations.sort(key=lambda g: (g.t0, g.span_id))
    events.sort(key=lambda e: e.t)
    scores.sort(key=lambda s: s.t)

    scores_by_doc: dict[str, dict[str, Any]] = {}
    for sc in scores:
        scores_by_doc.setdefault(sc.doc_id, {})[sc.name] = sc.value

    entities = _entities(
        roots, segments, generations, scores_by_doc, judge, review_causes_attr, rel
    )
    session = Session(
        id=f"{kind}:{key}",
        kind=kind,  # type: ignore[arg-type]
        environment=env,
        t0_iso=datetime.fromtimestamp(t0_ns / 1e9, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        duration_s=round((end_ns - t0_ns) / 1e9, 6),
        source="spans",
        approx=False,
        window=Window(from_s=0.0, to_s=round((end_ns - t0_ns) / 1e9, 6), complete=True),
    )
    tl = Timeline(
        session=session,
        stations=stations(),
        entities=entities,
        segments=segments,
        generations=generations,
        events=events,
        scores=scores,
    )
    tl.rollups = _rollups(tl, scores_by_doc)
    return tl


def _causes(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [p.strip() for p in raw.split(",")]
    if isinstance(raw, list):
        return [str(c)[:64] for c in raw if isinstance(c, str) and c][:32]
    return []


def _score(
    row: dict[str, Any], doc: str, name: str, value: Any, t: float
) -> ReplayScore:
    dtype = _data_type(name, value)
    return ReplayScore(
        doc_id=doc,
        span_id=row["span_id"],
        name=name,
        value=_score_value(name, value, dtype),
        data_type=dtype,
        t=t,
    )


def _generation(row: dict[str, Any], doc: str, t0: float, t1: float) -> Generation:
    a = row["attrs"]

    def pick(*keys: str) -> Any:
        for k in keys:
            if k in a:
                return a[k]
        return None

    def flag(k: str) -> bool | None:
        v = a.get(k)
        return v if isinstance(v, bool) else None

    ttft = a.get("mailroom.ttft_s")
    transport = a.get("mailroom.transport_attempts")
    return Generation(
        doc_id=doc,
        span_id=row["span_id"],
        parent_span_id=row["parent_id"],
        role=_str(a.get("mailroom.role"), 64) or "",
        model=_str(pick("mailroom.model", "llm.model_name"), 128) or "",
        served_model=_str(a.get("mailroom.served_model"), 128),
        prompt_version=_str(a.get("mailroom.prompt.version"), 64),
        t0=t0,
        t1=t1,
        prompt_tokens=_int(pick("mailroom.tokens.prompt", "llm.token_count.prompt")),
        completion_tokens=_int(
            pick("mailroom.tokens.completion", "llm.token_count.completion")
        ),
        cost_usd=_num(pick("mailroom.cost.total", "llm.cost.total")),
        transport_attempts=_int(transport) if transport is not None else None,
        ttft_s=_num(ttft) if ttft is not None else None,
        length_capped=flag("mailroom.length_capped"),
        schema_valid=flag("mailroom.schema_valid"),
    )


def _entities(
    roots: dict[str, dict[str, Any]],
    segments: list[Segment],
    generations: list[Generation],
    scores_by_doc: dict[str, dict[str, Any]],
    judge: dict[str, dict[str, Any]],
    causes_attr: dict[str, list[str]],
    rel: Any,
) -> list[Entity]:
    docs: dict[str, None] = {}
    for d in [*roots, *(s.doc_id for s in segments), *(g.doc_id for g in generations)]:
        docs.setdefault(d)
    span_bounds: dict[str, tuple[float, float]] = {}
    for s in segments:
        lo, hi = span_bounds.get(s.doc_id, (s.t0, s.t1))
        span_bounds[s.doc_id] = (min(lo, s.t0), max(hi, s.t1))
    gen_by_doc: dict[str, list[Generation]] = {}
    for g in generations:
        gen_by_doc.setdefault(g.doc_id, []).append(g)

    out: list[Entity] = []
    for doc in docs:
        root = roots.get(doc)
        a: dict[str, Any] = root["attrs"] if root else {}
        sc = scores_by_doc.get(doc, {})
        if root:
            t_start = rel(root["start_ns"])
            t_end: float | None = rel(max(root["end_ns"], root["start_ns"]))
        else:
            t_start, t_end = span_bounds.get(doc, (0.0, 0.0))
        gens = gen_by_doc.get(doc, [])
        # a score value beats the root usage attributes, which beat summed generations
        tokens = _first_num(
            sc.get("total_tokens"),
            a.get("mailroom.usage.total_tokens"),
            sum(g.prompt_tokens + g.completion_tokens for g in gens),
        )
        cost = _first_num(
            sc.get("estimated_cost_usd"),
            a.get("mailroom.usage.cost_usd"),
            sum(g.cost_usd for g in gens),
        )
        calls = _first_num(
            sc.get("llm_call_count"), a.get("mailroom.usage.calls"), len(gens)
        )
        causes = sc.get("review_causes")
        causes_list = (
            _causes(causes) if causes is not None else causes_attr.get(doc, [])
        )
        verdict = sc.get("mailroom-pipeline-judge")
        quality = sc.get("mailroom-pipeline-quality")
        j = judge.get(doc)
        if verdict is None and j is not None:
            verdict = _VERDICTS.get(str(j.get("mailroom.judge.label")))
        if quality is None and j is not None and "mailroom.judge.score" in j:
            quality = j["mailroom.judge.score"]
        out.append(
            Entity(
                doc_id=doc,
                filename=_str(a.get(A.FILENAME)) or "",
                trace_id=root["trace_id"] if root else None,
                session_id=(root["session_id"] if root else None),
                doc_type=_str(a.get("mailroom.doc_type"), 64),
                doc_subclass=_str(a.get("mailroom.doc_subclass"), 64),
                expected_doc_class=_str(a.get("mailroom.gt.expected_doc_class"), 64),
                expected_subclass=_str(a.get("mailroom.gt.expected_subclass"), 64),
                final_stage=_str(a.get(A.STAGE), 64),
                final_status=_str(a.get(A.STATUS), 64),
                failure_class=_str(a.get(A.FAILURE_CLASS), 64),
                review_causes=causes_list,
                verdict=_str(verdict, 32) if isinstance(verdict, str) else None,
                quality=_num(quality)
                if isinstance(quality, (int, float)) and not isinstance(quality, bool)
                else None,
                t_start=t_start,
                t_end=t_end,
                totals=Totals(
                    tokens=int(tokens),
                    cost_usd=round(cost, 8),
                    llm_calls=int(calls),
                    duration_s=round(max(0.0, (t_end or t_start) - t_start), 6),
                ),
            )
        )
    out.sort(key=lambda e: (e.t_start, e.doc_id))
    return out


def _first_num(*values: Any) -> float:
    """First real number among ``values`` (the last is the fallback and may be 0)."""
    for v in values[:-1]:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return float(values[-1])


def _rollups(tl: Timeline, scores_by_doc: dict[str, dict[str, Any]]) -> Rollups:
    durations: dict[str, list[float]] = {}
    for s in tl.segments:
        durations.setdefault(s.station, []).append(s.t1 - s.t0)
    per_station = {
        st: StationStats(
            p50_s=round(_percentile(v, 0.5), 6),
            p95_s=round(_percentile(v, 0.95), 6),
            n=len(v),
        )
        for st, v in durations.items()
    }
    verdicts: dict[str, int] = {}
    causes: dict[str, int] = {}
    for e in tl.entities:
        if e.verdict:
            verdicts[e.verdict] = verdicts.get(e.verdict, 0) + 1
        for c in e.review_causes:
            causes[c] = causes.get(c, 0) + 1

    retried = {ev.doc_id for ev in tl.events if ev.kind in _RETRY_EVENT_KINDS}
    retried |= {s.doc_id for s in tl.segments if s.retry_kind or s.attempt > 1}
    detoured = {s.doc_id for s in tl.segments if s.station in ("boss", "review")}
    passed = 0
    for e in tl.entities:
        flag = scores_by_doc.get(e.doc_id, {}).get("success_rate")
        if isinstance(flag, (int, float)) and not isinstance(flag, bool):
            passed += 1 if flag >= 1.0 else 0
        elif (
            e.final_status == "archived"
            and e.doc_id not in retried
            and e.doc_id not in detoured
        ):
            passed += 1
    first_pass = round(passed / len(tl.entities), 6) if tl.entities else None

    sums: dict[str, list[float]] = {}
    for per_doc in scores_by_doc.values():
        for name, v in per_doc.items():
            if isinstance(v, bool):
                v = 1.0 if v else 0.0
            if isinstance(v, (int, float)) and math.isfinite(v):
                sums.setdefault(name, []).append(float(v))
    averages = {n: round(sum(v) / len(v), 6) for n, v in sorted(sums.items())}
    return Rollups(
        per_station=per_station,
        verdict_counts=verdicts,
        review_causes=causes,
        cost_usd=round(sum(e.totals.cost_usd for e in tl.entities), 8),
        tokens=sum(e.totals.tokens for e in tl.entities),
        first_pass_rate=first_pass,
        averages=averages,
    )


# ------------------------------------------------------------------ windowing and cap
def _overlaps(t0: float, t1: float, lo: float, hi: float) -> bool:
    return t1 >= lo and t0 <= hi


def _window(tl: Timeline, lo: float, hi: float) -> Timeline:
    """A copy of ``tl`` restricted to ``[lo, hi]``; ``complete`` is false for a strict subset."""
    segments = [s for s in tl.segments if _overlaps(s.t0, s.t1, lo, hi)]
    generations = [g for g in tl.generations if _overlaps(g.t0, g.t1, lo, hi)]
    events = [e for e in tl.events if lo <= e.t <= hi]
    scores = [s for s in tl.scores if lo <= s.t <= hi]
    entities = [
        e
        for e in tl.entities
        if _overlaps(e.t_start, e.t_end if e.t_end is not None else hi, lo, hi)
    ]
    subset = (
        len(segments) < len(tl.segments)
        or len(generations) < len(tl.generations)
        or len(events) < len(tl.events)
        or len(scores) < len(tl.scores)
        or len(entities) < len(tl.entities)
    )
    complete = tl.session.window.complete and not subset
    out = tl.model_copy(
        update={
            "segments": segments,
            "generations": generations,
            "events": events,
            "scores": scores,
            "entities": entities,
            "session": tl.session.model_copy(
                update={"window": Window(from_s=lo, to_s=hi, complete=complete)}
            ),
        }
    )
    return out


def _too_big(tl: Timeline) -> bool:
    if len(tl.segments) > MAX_SEGMENTS:
        return True
    return len(tl.model_dump_json()) > MAX_PAYLOAD_BYTES


def _finalize(tl: Timeline, from_s: float | None, to_s: float | None) -> Timeline:
    """Apply the requested window, then shrink it until the payload fits the cap."""
    full_hi = tl.session.duration_s
    lo = 0.0 if from_s is None else max(0.0, from_s)
    hi = full_hi if to_s is None else min(to_s, full_hi)
    out = tl
    if from_s is not None or to_s is not None:
        out = _window(tl, lo, hi)
    for _ in range(40):
        if not _too_big(out) or hi - lo <= 1e-6:
            break
        hi = lo + (hi - lo) / 2
        out = _window(tl, lo, hi)
    return out


# ------------------------------------------------------------------ cache
_CACHE: OrderedDict[tuple[Any, ...], Timeline] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    """Drop every cached timeline."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_get(key: tuple[Any, ...]) -> Timeline | None:
    with _CACHE_LOCK:
        tl = _CACHE.get(key)
        if tl is not None:
            _CACHE.move_to_end(key)
        return tl


def _cache_put(key: tuple[Any, ...], tl: Timeline) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = tl
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)


# ------------------------------------------------------------------ public entry
def _parse_instant_ns(text: str) -> int:
    """A window bound: epoch nanoseconds, epoch seconds, or an ISO-8601 instant."""
    text = text.strip()
    try:
        number = float(text)
    except ValueError:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1e9)
    return int(number) if number >= 1e17 else int(number * 1e9)


def _read_rows(store: Any, kind: str, key: str) -> list[dict[str, Any]]:
    path = getattr(store, "path", None)
    if isinstance(path, Path) and not path.exists():
        return []  # never create an empty database just to read it
    if kind == "run":
        return store.spans_for_run(key)
    if kind == "session":
        return store.spans_for_session(key)
    if kind == "doc":
        return store.spans_for_doc(key)
    if kind == "window":
        start, _, end = key.partition("..")
        if not start or not end:
            raise ValueError(f"malformed window session id: {key!r}")
        return store.spans_between(_parse_instant_ns(start), _parse_instant_ns(end))
    raise ValueError(f"unknown session kind: {kind!r}")


def build_timeline(
    session_id: str,
    *,
    from_s: float | None = None,
    to_s: float | None = None,
    store: Any = None,
    engine: Any = None,
) -> Timeline | None:
    """The ``replay/v1`` timeline of ``session_id`` (``None`` when nothing is recorded).

    Spans are the exact source; with no spans the audit log gives an approximate
    timeline. ``from_s``/``to_s`` select a window (``complete`` is then false); a
    payload over the cap is windowed automatically. Span-sourced results are cached by
    the store watermark, so a new span invalidates them.
    """
    from mailroom_reloaded.obs.replay.sessions import parse_session_id

    kind, key = parse_session_id(session_id)
    if store is None:
        from mailroom_reloaded.storage.span_store import (
            SpanStore,
            default_span_store_path,
        )

        store = SpanStore(default_span_store_path())

    watermark: int | None = None
    path = getattr(store, "path", None)
    if not (isinstance(path, Path) and not path.exists()):
        watermark = store.watermark()
    cache_key = (session_id, from_s, to_s, str(path), watermark)
    if watermark is not None:
        hit = _cache_get(cache_key)
        if hit is not None:
            return hit

    tl = timeline_from_spans(_read_rows(store, kind, key), kind, key)
    from_spans = tl is not None
    if tl is None:
        from mailroom_reloaded.obs.replay.audit_source import timeline_from_audit

        tl = timeline_from_audit(kind, key, engine=engine)
        if tl is None:
            return None
    result = _finalize(tl, from_s, to_s)
    if from_spans and watermark is not None:
        _cache_put(cache_key, result)
    return result
