"""Offline OTLP/JSON import into the span store (the ``mailroom replay import`` backend).

Reads one OTLP/JSON ``resourceSpans`` document, or JSON lines of them as the collector
``file`` exporter writes, and stores each span through the same attribute allow-list as
the live exporter: only ``mailroom.*``, ``session.id``, the span kind, token/cost and a
few model attributes survive. Prompts, completions and document text are never stored,
and the ``input.value`` / ``output.value`` summaries of node spans are stored masked.

Input is untrusted, so it is parsed strictly and within bounds: a size cap, a span cap,
hex trace/span ids, finite non-negative integer nanosecond times. Every failure raises
:class:`OtlpImportError` with a readable message; nothing is written when parsing fails.
Re-importing a file is harmless: a span id already stored is left alone.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mailroom_reloaded.obs.replay.sessions import parse_session_id
from mailroom_reloaded.storage.span_store import (
    READ_CAP,
    SpanStore,
    filter_attributes,
    filter_event,
)

__all__ = [
    "MAX_INPUT_BYTES",
    "MAX_SPANS",
    "ImportResult",
    "OtlpImportError",
    "import_otlp_file",
    "parse_otlp",
    "read_otlp_file",
]

#: Largest input accepted (bytes).
MAX_INPUT_BYTES = 32 * 1024 * 1024
#: Most spans accepted from one input.
MAX_SPANS = READ_CAP
_MAX_NS = 2**63 - 1  # SQLite signed 64-bit
_MAX_ATTRS = 256
_MAX_EVENTS = 200
_MAX_EVENT_ATTRS = 64
_MAX_KEY = 128
_HEX = frozenset("0123456789abcdef")
_INT_RE = re.compile(r"-?[0-9]+", re.ASCII)
_STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}


class OtlpImportError(ValueError):
    """The input is not importable OTLP/JSON (message is safe to show to the user)."""


@dataclass
class ImportResult:
    """What an import did: spans parsed, rows newly stored, and the ids they carry."""

    parsed: int = 0
    stored: int = 0
    runs: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        """Spans not stored: already present (same span id), or over the per-run row cap."""
        return self.parsed - self.stored


def _hex_id(value: Any, width: int, what: str, *, optional: bool = False) -> str | None:
    if value in (None, "") and optional:
        return None
    if not isinstance(value, str) or len(value) != width:
        raise OtlpImportError(f"{what} must be {width} hex characters")
    low = value.lower()
    if not set(low) <= _HEX or not any(c != "0" for c in low):
        raise OtlpImportError(f"{what} must be {width} non-zero hex characters")
    return low


def _nanos(value: Any, what: str) -> int:
    """A finite, non-negative integer nanosecond count from a string or number."""
    if isinstance(value, bool):
        raise OtlpImportError(f"{what} is not a number")
    if isinstance(value, str):
        if not (value.isascii() and value.isdigit()) or len(value) > 20:
            raise OtlpImportError(f"{what} must be a non-negative integer")
        out = int(value)
    elif isinstance(value, int):
        out = value
    elif isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise OtlpImportError(f"{what} must be a finite whole number")
        out = int(value)
    else:
        raise OtlpImportError(f"{what} is not a number")
    if out < 0 or out > _MAX_NS:
        raise OtlpImportError(f"{what} is out of range")
    return out


def _scalar(value: Any) -> Any:
    """One OTLP ``AnyValue`` as a str/bool/int/float, or ``None`` when unsupported."""
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("stringValue"), str):
        return value["stringValue"]
    if isinstance(value.get("boolValue"), bool):
        return value["boolValue"]
    raw = value.get("intValue")
    if isinstance(raw, str) and len(raw) <= 20 and _INT_RE.fullmatch(raw):
        raw = int(raw)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw if abs(raw) <= 2**64 else None
    raw = value.get("doubleValue")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            out = float(raw)
        except OverflowError:
            return None
        return out if math.isfinite(out) else None
    return None


def _any_value(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("arrayValue"), dict):
        items = value["arrayValue"].get("values")
        if isinstance(items, list):
            return [v for v in (_scalar(i) for i in items[:64]) if v is not None]
        return None
    return _scalar(value)


def _attributes(raw: Any, limit: int, what: str) -> dict[str, Any]:
    """OTLP ``KeyValue`` list as a dict (unsupported values skipped, count bounded)."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise OtlpImportError(f"{what} attributes must be a list")
    out: dict[str, Any] = {}
    for kv in raw[:limit]:
        if not isinstance(kv, dict):
            continue
        key = kv.get("key")
        if isinstance(key, str) and 0 < len(key) <= _MAX_KEY:
            val = _any_value(kv.get("value"))
            if val is not None:
                out[key] = val
    return out


def _status(raw: Any) -> str:
    code = raw.get("code") if isinstance(raw, dict) else None
    if isinstance(code, int) and not isinstance(code, bool):
        return _STATUS.get(code, "UNSET")
    if isinstance(code, str):
        name = code.removeprefix("STATUS_CODE_").upper()
        return name if name in _STATUS.values() else "UNSET"
    return "UNSET"


def _checked_id(kind: str, value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise OtlpImportError(f"{what} must be a string")
    try:
        parse_session_id(f"{kind}:{value}")
    except ValueError:
        raise OtlpImportError(f"{what} {value[:40]!r} is not a valid id") from None
    return value


def _row(
    span: Any, resource: dict[str, Any], run_id: str | None, where: str
) -> dict[str, Any]:
    if not isinstance(span, dict):
        raise OtlpImportError(f"{where}: span must be an object")
    try:
        span_id = _hex_id(span.get("spanId"), 16, "spanId")
        trace_id = _hex_id(span.get("traceId"), 32, "traceId")
        parent = _hex_id(span.get("parentSpanId"), 16, "parentSpanId", optional=True)
        if span.get("startTimeUnixNano") is None:
            raise OtlpImportError("startTimeUnixNano is required")
        start = _nanos(span["startTimeUnixNano"], "startTimeUnixNano")
        if start == 0:
            raise OtlpImportError("startTimeUnixNano must be set")
        end_raw = span.get("endTimeUnixNano")
        end = start if end_raw in (None, "", "0", 0) else _nanos(end_raw, "endTimeUnixNano")
        if end < start:
            raise OtlpImportError("endTimeUnixNano is before startTimeUnixNano")
        name = span.get("name")
        name = name if isinstance(name, str) and name else "span"
        attrs = {**resource, **_attributes(span.get("attributes"), _MAX_ATTRS, "span")}
        if run_id is not None:
            attrs["mailroom.run_id"] = run_id
        events = span.get("events")
        if events is not None and not isinstance(events, list):
            raise OtlpImportError("events must be a list")
        kept_events: list[dict[str, Any]] = []
        for ev in (events or [])[:_MAX_EVENTS]:
            if not isinstance(ev, dict) or not isinstance(ev.get("name"), str):
                continue
            t = ev.get("timeUnixNano")
            stored = filter_event(
                ev["name"],
                start if t in (None, "") else _nanos(t, "event timeUnixNano"),
                _attributes(ev.get("attributes"), _MAX_EVENT_ATTRS, "event"),
            )
            if stored is not None:
                kept_events.append(stored)
    except OtlpImportError as exc:
        raise OtlpImportError(f"{where}: {exc}") from None
    # mask=True: node-span summaries are stored masked, never as imported text
    kept = filter_attributes(attrs, name=name, mask=True)
    for key in ("mailroom.doc_id", "openinference.span.kind", "mailroom.station"):
        if key in kept and not isinstance(kept[key], str):
            del kept[key]  # these feed text columns; a list or number is not an id
    run = kept.get("mailroom.run_id")
    session = kept.get("session.id")
    try:
        if run is not None:
            _checked_id("run", run, "mailroom.run_id")
        if session is not None:
            _checked_id("session", session, "session.id")
    except OtlpImportError as exc:
        raise OtlpImportError(f"{where}: {exc}") from None
    if run is None:
        raise OtlpImportError(f"{where}: span has no mailroom.run_id (use --run-id)")
    return {
        "span_id": span_id,
        "trace_id": trace_id,
        "parent_id": parent,
        "name": name[:128],
        "kind": kept.get("openinference.span.kind"),
        "start_ns": start,
        "end_ns": end,
        "status": _status(span.get("status")),
        "doc_id": kept.get("mailroom.doc_id"),
        "run_id": run,
        "session_id": session,
        "station": kept.get("mailroom.station"),
        "attrs": json.dumps(kept, sort_keys=True, default=str),
        "events": json.dumps(kept_events, default=str),
    }


def _documents(text: str) -> list[Any]:
    """The JSON document(s) in ``text``: one document, or one per non-empty line."""
    try:
        return [json.loads(text)]
    except (ValueError, RecursionError) as whole:
        lines = [(n, ln) for n, ln in enumerate(text.splitlines(), 1) if ln.strip()]
        first_ok = False
        if len(lines) > 1:
            try:
                first_ok = isinstance(json.loads(lines[0][1]), dict)
            except (ValueError, RecursionError):
                pass
        if not first_ok:
            raise OtlpImportError(f"invalid JSON: {whole}") from None
    docs: list[Any] = []
    for n, line in lines:
        try:
            docs.append(json.loads(line))
        except (ValueError, RecursionError) as exc:
            raise OtlpImportError(f"line {n}: invalid JSON: {exc}") from None
    return docs


def parse_otlp(text: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
    """Span-store rows for every span in ``text`` (OTLP/JSON or JSON lines of it).

    ``run_id`` overrides each span's ``mailroom.run_id`` (spans of a foreign export can be
    filed under a run of your choosing). Raises :class:`OtlpImportError` on any problem;
    the result is never partial.
    """
    if len(text) > MAX_INPUT_BYTES:
        raise OtlpImportError(f"input exceeds {MAX_INPUT_BYTES} bytes")
    if run_id is not None:
        _checked_id("run", run_id, "run id")
    if not text.strip():
        raise OtlpImportError("input is empty")
    rows: list[dict[str, Any]] = []
    for doc in _documents(text):
        if not isinstance(doc, dict) or not isinstance(doc.get("resourceSpans"), list):
            raise OtlpImportError("expected an object with a resourceSpans list")
        for rs in doc["resourceSpans"]:
            if not isinstance(rs, dict):
                raise OtlpImportError("resourceSpans entries must be objects")
            res = rs.get("resource")
            resource = _attributes(
                res.get("attributes") if isinstance(res, dict) else None,
                _MAX_ATTRS,
                "resource",
            )
            scopes = rs.get("scopeSpans") or []
            if not isinstance(scopes, list):
                raise OtlpImportError("scopeSpans must be a list")
            for scope in scopes:
                spans = scope.get("spans") if isinstance(scope, dict) else None
                if spans is None:
                    continue
                if not isinstance(spans, list):
                    raise OtlpImportError("spans must be a list")
                for span in spans:
                    if len(rows) >= MAX_SPANS:
                        raise OtlpImportError(f"more than {MAX_SPANS} spans")
                    rows.append(_row(span, resource, run_id, f"span {len(rows) + 1}"))
    if not rows:
        raise OtlpImportError("no spans found")
    return rows


def read_otlp_file(path: str | Path) -> str:
    """The text of ``path``, refusing more than :data:`MAX_INPUT_BYTES` before decoding."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise OtlpImportError(f"cannot read {path}: {exc.strerror or exc}") from None
    if len(raw) > MAX_INPUT_BYTES:
        raise OtlpImportError(f"input exceeds {MAX_INPUT_BYTES} bytes")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise OtlpImportError("input is not valid UTF-8") from None


def import_otlp_file(
    path: str | Path,
    store: SpanStore,
    *,
    run_id: str | None = None,
    append: bool = False,
    pruned: Collection[str] = (),
) -> ImportResult:
    """Parse ``path`` and store its spans in ``store``; idempotent by span id.

    Refuses (:class:`OtlpImportError`) a target run that is a showcase run or is in
    ``pruned`` (the caller passes the ledger's pruned runs), and a run that already has
    spans unless ``append`` is true; otherwise an import could fill a real run's row cap
    or mix foreign spans into it.
    """
    rows = parse_otlp(read_otlp_file(path), run_id=run_id)
    runs = sorted({r["run_id"] for r in rows if r["run_id"]})
    from mailroom_reloaded.storage.retention import SHOWCASE_RUN_IDS

    for run in runs:
        if run in SHOWCASE_RUN_IDS or run.startswith("showcase-"):
            raise OtlpImportError(f"run {run!r} is a showcase run and cannot be imported into")
        if run in pruned:
            raise OtlpImportError(f"run {run!r} was pruned by retention; choose another run id")
        if not append and (have := store.count(run)):
            raise OtlpImportError(
                f"run {run!r} already has {have} spans; use --append to add to it "
                "or --run-id to file the import under a new run"
            )
    stored = store.write(rows)
    return ImportResult(
        parsed=len(rows),
        stored=stored,
        runs=runs,
        sessions=sorted({r["session_id"] for r in rows if r["session_id"]}),
    )
