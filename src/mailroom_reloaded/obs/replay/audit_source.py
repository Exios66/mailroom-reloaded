"""Approximate ``replay/v1`` timeline from the hash-chained audit log.

The fallback when the span store has nothing for a session. The audit log records
when a node finished (``ts``) and how long it took (``elapsed_s``), so a segment
runs from ``ts - elapsed_s`` to ``ts`` and is flagged ``approx``. Decisions
(``gate_decision``, ``parked``, ``review_resolved``, ``archived``) become events.

Audit row shapes consumed (``audit_log``: ``doc_id, seq, node, event, payload, ts``):

* ``completed``: ``{"elapsed_s": float}``
* ``node_failed``: ``{"reason": str, "elapsed_s": float}``; the reason can embed a
  filename or exception text and is collapsed onto ``obs.attrs.FAILURE_REASONS``
* ``gate_decision`` (node ``gate_<stage>``): ``{"action", "reason", "source", "confidence"}``
* ``parked`` (node ``human_review``): ``{"reason"}``
* ``review_resolved`` (node ``review``): ``{"action", "reviewer", ...}``; the reviewer is dropped
* ``archived`` (node ``archive``): ``{"file_sha256", "path", ...}``; only ``doc_type`` is kept

No free text reaches the payload: failures go through the closed failure
vocabulary, other strings are reduced to a bounded token charset, and the one
free-form field, the filename, is control-stripped and bounded.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import SQLAlchemyError

from mailroom_reloaded.obs import attrs
from mailroom_reloaded.obs.replay.sessions import (
    format_session_id,
    parse_session_id,
    window_bounds_ns,
)
from mailroom_reloaded.schemas.replay import (
    Entity,
    ReplayEvent,
    Rollups,
    Segment,
    Session,
    StationInfo,
    StationStats,
    Timeline,
    Totals,
    Window,
)
from mailroom_reloaded.storage.db import (
    audit_table,
    catalog_table,
    get_engine,
    ledger_table,
)

__all__ = ["timeline_from_audit"]

logger = structlog.get_logger(__name__)

MAX_DOCS = 1000
MAX_FILENAME = 120
MAX_TOKEN = 64
_CHUNK = 400
_EVENT_NODES = {"archive": "archive", "review": "review"}
_EVENTS = {"gate_decision", "parked", "review_resolved", "archived"}
_PARK_REASONS = frozenset(
    {
        "flow_human_review",
        "classify_human_review",
        "extract_human_review",
        "boss_human_review",
        "arbiter_retries_spent",
    }
)
_REVIEW_ACTIONS = frozenset({"approve", "correct", "reject"})
_GATE_ACTIONS = frozenset({"proceed", "retry", "verify", "boss", "human_review"})
_GATE_SOURCES = frozenset({"band", "model", "rule", "jev"})
_NUM = r"(?:-?[0-9]+(?:\.[0-9]+)?(?:e[-+]?[0-9]+)?|None)"
#: Gate reasons the pipeline emits (``agents/gate.py``, ``agents/jev.py``); numbers are
#: the only variable parts. Anything else (prose, paths, markup) collapses to ``other``.
_GATE_REASON_RES = tuple(
    re.compile(p.replace("NUM", _NUM))
    for p in (
        r"bert/sorter doc_type disagree",
        r"confidence >= high NUM",
        r"confidence < high NUM",
        r"classify retries spent",
        r"(?:length capped|schema invalid)(?:, retries spent)?",
        r"confidence >= judge_band_high NUM",
        r"NUM <= confidence < NUM",
        r"confidence < low NUM",
        r"extract retries spent below low",
        r"learned p=NUM thr=NUM",
        r"jev: no route answer",
        r"jev confidence NUM < verify NUM",
        r"jev noul NUM escalate",
        r"jev confidence NUM in verify band \(< accept NUM\)",
        r"jev confidence NUM < accept NUM",
        r"jev choice (?:proceed|retry|verify|boss|human_review) p=NUM",
    )
)
_JEV_UNKNOWN_ACTION = "jev unknown action"
_TOKEN_BAD = re.compile(r"[^A-Za-z0-9_.:-]+")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _token(value: Any, limit: int = MAX_TOKEN) -> str:
    """A bounded identifier-like string (anything else becomes ``_``)."""
    return _TOKEN_BAD.sub("_", str(value))[:limit]


def _gate_reason(value: Any) -> str:
    """The reason if it is one of the pipeline's known shapes, else ``other``."""
    reason = str(value or "")
    if reason.startswith(_JEV_UNKNOWN_ACTION + " "):
        return _JEV_UNKNOWN_ACTION  # the repr of the unrecognised action is dropped
    return reason if any(r.fullmatch(reason) for r in _GATE_REASON_RES) else "other"


def _filename(value: Any) -> str:
    """A bounded, control-stripped filename (the only free-form field)."""
    return _CTRL.sub("", str(value or ""))[:MAX_FILENAME]


def _ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _payload(raw: str) -> dict[str, Any]:
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _station_id(node: str) -> str | None:
    try:
        return attrs.station_for(node).id
    except KeyError:
        return _EVENT_NODES.get(node)


def _chunks(items: list[str]) -> list[list[str]]:
    return [items[i : i + _CHUNK] for i in range(0, len(items), _CHUNK)]


def _doc_ids(engine: Engine, kind: str, key: str) -> tuple[list[str], bool]:
    """Document ids of the session (empty when the kind has no audit data).

    At most ``MAX_DOCS`` ids; the flag is true when more existed (one extra is fetched).
    """
    ids: list[str] = []
    cap = MAX_DOCS + 1
    with engine.connect() as conn:
        if kind == "doc":
            return [key], False
        if kind == "run":
            try:
                rows = conn.execute(
                    text(
                        "SELECT DISTINCT doc_id FROM eval_docs "
                        "WHERE run_id = :r AND doc_id IS NOT NULL LIMIT :n"
                    ),
                    {"r": key, "n": cap},
                )
                ids += [str(r[0]) for r in rows]
            except SQLAlchemyError:
                pass  # eval_docs is created lazily by the eval runner
            lt = ledger_table
            rows = conn.execute(
                select(lt.c.doc_id)
                .where(lt.c.run_id == key, lt.c.doc_id.is_not(None))
                .distinct()
                .limit(cap)
            )
            ids += [str(r[0]) for r in rows]
        elif kind == "window":
            lo, hi = window_bounds_ns(key)
            t_lo = datetime.fromtimestamp(lo / 1e9, tz=UTC).isoformat()
            t_hi = datetime.fromtimestamp(hi / 1e9, tz=UTC).isoformat()
            rows = conn.execute(
                select(audit_table.c.doc_id)
                .where(audit_table.c.ts >= t_lo, audit_table.c.ts < t_hi)
                .distinct()
                .limit(cap)
            )
            ids += [str(r[0]) for r in rows]
    unique = list(dict.fromkeys(ids))  # a session: has no audit ids
    return unique[:MAX_DOCS], len(unique) > MAX_DOCS


def _audit_rows(engine: Engine, doc_ids: list[str]) -> list[Any]:
    out: list[Any] = []
    t = audit_table
    with engine.connect() as conn:
        for chunk in _chunks(doc_ids):
            out += conn.execute(
                select(t).where(t.c.doc_id.in_(chunk)).order_by(t.c.doc_id, t.c.seq)
            ).all()
    return out


def _eval_rows(engine: Engine, run_id: str) -> tuple[dict[str, dict[str, Any]], bool]:
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM eval_docs WHERE run_id = :r LIMIT :n"),
                {"r": run_id, "n": MAX_DOCS + 1},
            ).mappings()
            out = {str(r["doc_id"]): dict(r) for r in rows if r["doc_id"]}
    except SQLAlchemyError:
        return {}, False
    kept = dict(list(out.items())[:MAX_DOCS])
    return kept, len(out) > MAX_DOCS


def _catalog_rows(engine: Engine, doc_ids: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    t = catalog_table
    with engine.connect() as conn:
        for chunk in _chunks(doc_ids):
            for r in conn.execute(select(t).where(t.c.doc_id.in_(chunk))):
                out[r.doc_id] = r
    return out


def _eval_cost(row: dict[str, Any]) -> float:
    """Approximate USD from the stored token counts, priced at the specialist model."""
    try:
        from mailroom_reloaded.llm.client import cost_for
        from mailroom_reloaded.llm.usage import Usage

        usage = Usage(
            prompt_tokens=int(row.get("prompt_tokens") or 0),
            completion_tokens=int(row.get("completion_tokens") or 0),
        )
        return round(cost_for("specialist", usage), 8)
    except Exception:  # noqa: BLE001 - costing must never fail a replay
        return 0.0


def _event_payload(event: str, node: str, p: dict[str, Any]) -> dict[str, Any]:
    if event == "gate_decision":
        out: dict[str, Any] = {
            "stage": _token(node.removeprefix("gate_"), 32),
            "action": p.get("action") if p.get("action") in _GATE_ACTIONS else "other",
            "source": p.get("source") if p.get("source") in _GATE_SOURCES else "other",
            "reason": _gate_reason(p.get("reason")),
        }
        conf = _num(p.get("confidence"))
        if conf is not None:
            out["confidence"] = round(conf, 4)
        return out
    if event == "parked":
        reason = p.get("reason")
        return {"reason": reason if reason in _PARK_REASONS else "other"}
    if event == "review_resolved":
        action = p.get("action")
        out = {"action": action if action in _REVIEW_ACTIONS else "other"}
        for k in ("doc_type", "doc_subclass"):
            if p.get(k):
                out[k] = _token(p[k], 48)
        return out
    out = {}
    if p.get("doc_type"):
        out["doc_type"] = _token(p["doc_type"], 48)
    return out


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = q * (len(ordered) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)


def timeline_from_audit(
    kind: str, key: str, *, engine: Engine | None = None
) -> Timeline | None:
    """Build the approximate timeline of ``kind:key`` from the audit log, or ``None``.

    ``run:`` sessions take their documents from ``eval_docs`` and the archive ledger
    (tokens, cost, route trail and judge score fill the entity), ``doc:`` is a single
    document, ``window:`` is every document with an audit row in the window. A
    ``session:`` has no audit representation and yields ``None``. Times are seconds
    from the earliest segment start; ``session.t0_iso`` is that instant.
    """
    engine = engine or get_engine()
    doc_ids, truncated = _doc_ids(engine, kind, key)
    if not doc_ids:
        return None
    audit = _audit_rows(engine, doc_ids)
    if not audit:
        return None
    evals, evals_truncated = _eval_rows(engine, key) if kind == "run" else ({}, False)
    truncated = truncated or evals_truncated
    catalog = _catalog_rows(engine, doc_ids)

    # (doc, node, event, abs start, abs end/at, payload)
    segs: list[tuple[str, str, str, datetime, datetime, dict[str, Any]]] = []
    evs: list[tuple[str, str, str, datetime, dict[str, Any]]] = []
    seen_docs: dict[str, None] = {}
    for r in audit:
        at = _ts(r.ts)
        if at is None:
            continue
        seen_docs[r.doc_id] = None
        p = _payload(r.payload)
        if r.event in ("completed", "node_failed"):
            if _station_id(r.node) is None or r.node in _EVENT_NODES:
                continue  # unknown node: tolerated, skipped
            elapsed = max(_num(p.get("elapsed_s")) or 0.0, 0.0)
            segs.append(
                (r.doc_id, r.node, r.event, at - timedelta(seconds=elapsed), at, p)
            )
        elif r.event in _EVENTS:
            evs.append((r.doc_id, r.node, r.event, at, p))
    if not segs and not evs:
        return None

    base = min([s[3] for s in segs] + [e[3] for e in evs])

    def rel(dt: datetime) -> float:
        return round((dt - base).total_seconds(), 4)

    segments: list[Segment] = []
    attempts: dict[tuple[str, str], int] = defaultdict(int)
    last_failed: dict[str, tuple[str, str, float]] = {}  # doc -> (node, reason, end)
    last_station: dict[str, str] = {}
    for doc, node, event, t0, t1, p in sorted(segs, key=lambda s: (s[3], s[4], s[0])):
        n = attempts[(doc, node)] = attempts[(doc, node)] + 1
        station = _station_id(node) or ""
        failed = event == "node_failed"
        reason = None
        if failed:
            reason = attrs.failure_reason_for(
                p.get("reason") if isinstance(p.get("reason"), str) else None
            )
            last_failed[doc] = (node, reason, rel(t1))
        elif doc in last_failed and last_failed[doc][0] == node:
            del last_failed[doc]  # a later attempt of the failed node completed
        last_station[doc] = station
        segments.append(
            Segment(
                doc_id=doc,
                node=node,
                station=station,
                t0=rel(t0),
                t1=rel(t1),
                attempt=n,
                retry_kind=f"retry_{node}" if n > 1 else None,
                status="failed" if failed else "ok",
                reason=reason,
                approx=True,
            )
        )

    events = [
        ReplayEvent(
            t=rel(at),
            doc_id=doc,
            kind=event,
            station=_station_id(node),
            payload=_event_payload(event, node, p),
        )
        for doc, node, event, at, p in sorted(evs, key=lambda e: (e[3], e[0]))
    ]
    ev_kinds: dict[str, set[str]] = defaultdict(set)
    archived_at: dict[str, float] = {}
    for e in events:
        ev_kinds[e.doc_id].add(e.kind)
        if e.kind == "archived":
            archived_at[e.doc_id] = max(e.t, archived_at.get(e.doc_id, e.t))
    for doc in [d for d, f in last_failed.items() if archived_at.get(d, -1.0) >= f[2]]:
        del last_failed[doc]  # archived after the last failure: the doc recovered

    entities: list[Entity] = []
    order = list(dict.fromkeys([*seen_docs, *evals]))
    for doc in order:
        ev, cat = evals.get(doc), catalog.get(doc)
        mine = [s for s in segments if s.doc_id == doc]
        t_start = min((s.t0 for s in mine), default=0.0)
        t_end = max((s.t1 for s in mine), default=None)
        failure = (
            attrs.failure_class_for(last_failed[doc][1]) if doc in last_failed else None
        )
        kinds = ev_kinds.get(doc, set())
        if doc in last_failed:
            status = "failed"
        elif "archived" in kinds:
            status = "archived"
        elif "parked" in kinds and "review_resolved" not in kinds:
            status = "parked"
        elif ev and ev.get("status"):
            status = _token(ev["status"], 32)
        else:
            status = "processing" if mine else None
        tokens = calls = 0
        cost = 0.0
        duration = (t_end - t_start) if t_end is not None else 0.0
        quality = None
        doc_type = doc_subclass = filename = None
        if ev:
            tokens = int(ev.get("prompt_tokens") or 0) + int(
                ev.get("completion_tokens") or 0
            )
            calls = int(ev.get("calls") or 0)
            cost = _eval_cost(ev)
            duration = _num(ev.get("latency_s")) or duration
            quality = _num(ev.get("judge_overall"))
            doc_type, doc_subclass = ev.get("doc_type"), ev.get("doc_subclass")
            filename = ev.get("filename")
        if cat is not None:
            doc_type = doc_type or cat.doc_type
            doc_subclass = doc_subclass or cat.doc_subclass
            filename = filename or cat.filename
        entities.append(
            Entity(
                doc_id=doc,
                filename=_filename(filename),
                doc_type=_token(doc_type, 48) if doc_type else None,
                doc_subclass=_token(doc_subclass, 48) if doc_subclass else None,
                final_stage=last_station.get(doc),
                final_status=status,
                failure_class=failure,
                quality=quality,
                t_start=t_start,
                t_end=t_end,
                totals=Totals(
                    tokens=tokens,
                    cost_usd=cost,
                    llm_calls=calls,
                    duration_s=round(duration, 4),
                ),
            )
        )

    ends = [s.t1 for s in segments] + [e.t for e in events]
    duration_s = round(max(ends, default=0.0), 4)
    per_station: dict[str, list[float]] = defaultdict(list)
    for s in segments:
        per_station[s.station].append(s.t1 - s.t0)
    rollups = Rollups(
        per_station={
            st: StationStats(
                p50_s=round(statistics.median(v), 4),
                p95_s=round(_percentile(v, 0.95), 4),
                n=len(v),
            )
            for st, v in per_station.items()
        },
        cost_usd=round(sum(e.totals.cost_usd for e in entities), 8),
        tokens=sum(e.totals.tokens for e in entities),
    )
    try:
        session_id = format_session_id(*parse_session_id(f"{kind}:{key}"))
    except ValueError:  # a direct caller bypassed validation: sanitise rather than echo
        session_id = f"{kind}:{_token(key, 200)}"
    return Timeline(
        session=Session(
            id=session_id,
            kind=kind,  # type: ignore[arg-type]
            environment="eval" if evals or re.match(r"eval[-_]", key) else "live",
            t0_iso=base.isoformat(),
            duration_s=duration_s,
            source="audit",
            approx=True,
            window=Window(from_s=0.0, to_s=duration_s, complete=not truncated),
            links={},
        ),
        stations=[
            StationInfo(
                id=s.id,
                label=s.label,
                phase=s.phase,
                order=i,
                kind=s.kind,  # type: ignore[arg-type]
                color_token=s.color_token,
            )
            for i, s in enumerate(attrs.STATIONS)
        ],
        entities=entities,
        segments=segments,
        events=events,
        rollups=rollups,
    )
