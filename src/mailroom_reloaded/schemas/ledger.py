"""Archive-ledger entry schema, payload allow-list and hash helpers.

The ledger is one global hash chain over every pipeline run. Hashing follows the
per-document audit log (``schemas/audit.py``): sha256 over canonical JSON
(``sort_keys``, compact separators) of the entry body without ``entry_hash``.
``ts`` is hashed as the stored ISO string so no re-serialisation can differ.

Payloads are allow-listed per kind and every value is bounded, so a hostile
filename, exception text, judge note or prompt can never reach the ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from mailroom_reloaded.obs.attrs import FAILURE_CLASSES, FAILURE_REASONS

__all__ = [
    "KINDS",
    "MAX_PAYLOAD_BYTES",
    "LedgerEntry",
    "LedgerVerify",
    "canonical_json",
    "compute_ledger_hash",
    "payload_digest",
    "sanitize_payload",
]

#: Entry kinds (see the plan's "Entry kinds" table).
KINDS: tuple[str, ...] = (
    "run_opened",
    "doc_closed",
    "gap",
    "run_closed",
    "checkpoint",
    "pinned",
    "unpinned",
    "policy",
    "pruned",
    "anchor",
    "score_late",
)

MAX_PAYLOAD_BYTES = 8192
_MAX_STR = 120
_LABEL_RE = re.compile(r"[^A-Za-z0-9._:/@+=-]")
_HEX_RE = re.compile(r"^[0-9a-f]{1,64}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class LedgerEntry(BaseModel):
    """One row of the ``ledger`` table."""

    seq: int
    kind: str
    run_id: str
    doc_id: str | None = None
    ts: str
    payload: dict[str, Any] = Field(default_factory=dict)
    digest: str = ""
    prev_hash: str = ""
    entry_hash: str = ""


class LedgerVerify(BaseModel):
    """Outcome of verifying the chain (whole ledger, or one run)."""

    ok: bool
    count: int = 0
    head_seq: int | None = None
    head_hash: str | None = None
    broken_at: int | None = None  # seq of the first failing entry
    merkle_ok: bool | None = None  # None when the run has no run_closed yet
    detail: str = ""


def canonical_json(value: Any) -> str:
    """The canonical JSON form used for every ledger hash."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def payload_digest(payload: dict[str, Any]) -> str:
    """sha256 of the canonical payload (the Merkle leaf for ``doc_closed``)."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_ledger_hash(entry: LedgerEntry) -> str:
    """sha256 over the canonical JSON of the entry without ``entry_hash``."""
    body = entry.model_dump(mode="json", exclude={"entry_hash"})
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- allow-list
def _label(value: Any) -> str | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    return _LABEL_RE.sub("_", str(value))[:_MAX_STR]


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 6)


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _hex(value: Any) -> str | None:
    return value if isinstance(value, str) and _HEX_RE.match(value) else None


def _enum(*allowed: str) -> Callable[[Any], str | None]:
    def check(value: Any) -> str | None:
        return value if isinstance(value, str) and value in allowed else None

    return check


def _names(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [s for s in (_label(v) for v in value[:64]) if s]


def _head(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    seq, h = _int(value.get("seq")), _hex(value.get("entry_hash"))
    return {"seq": seq, "entry_hash": h} if seq is not None and h else None


def _heads(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    out = []
    for item in value[:200]:
        head = _head(item)
        run = _label(item.get("run_id")) if isinstance(item, dict) else None
        if head and run:
            out.append({"run_id": run, **head})
    return out


def _counts(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, int] = {}
    for key, raw in list(value.items())[:32]:
        n = _int(raw)
        if isinstance(key, str) and _KEY_RE.match(key) and n is not None:
            out[key] = n
    return out


def _usage_by_role(value: Any) -> dict[str, dict[str, float]] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, dict[str, float]] = {}
    for role, usage in list(value.items())[:32]:
        if not (
            isinstance(role, str) and _KEY_RE.match(role) and isinstance(usage, dict)
        ):
            continue
        out[role] = {
            k: n
            for k in ("prompt_tokens", "completion_tokens", "calls", "cost_usd")
            if (n := _num(usage.get(k))) is not None
        }
    return out


_RUN_KIND = _enum("eval", "live")
_OUTCOME = _enum("completed", "failed", "parked", "aborted", "reconciled")
_CLOSED_BY = _enum("completed", "interrupted", "reconcile")
_FAILURE_REASON = _enum(*FAILURE_REASONS)
_FAILURE_CLASS = _enum(*FAILURE_CLASSES)

_SPECS: dict[str, dict[str, Callable[[Any], Any]]] = {
    "run_opened": {
        "kind": _RUN_KIND,
        "mode": _label,
        "posture_label": _label,
        "model": _label,
        "prompt_set": _label,
        "config_sha": _hex,
        "environment": _label,
        "source": _label,
        "pid": _int,
    },
    "doc_closed": {
        "invocation": _int,
        "outcome": _OUTCOME,
        "doc_type": _label,
        "audit_head": _head,
        "metrics_digest": _hex,
        "rows": _int,
        "usage_by_role": _usage_by_role,
        "usage_complete": _bool,
        "usage_partial_nodes": _names,
        "failure_reason": _FAILURE_REASON,
        "failure_class": _FAILURE_CLASS,
        "route_trail": _names,
        "duration_s": _num,
    },
    "gap": {"reason": _enum("row_cap", "queue_overflow"), "count": _int},
    "run_closed": {
        "counts": _counts,
        "docs": _int,
        "expected": _int,
        "merkle_root": _hex,
        "dropped_rows": _int,
        "closed_by": _CLOSED_BY,
    },
    "checkpoint": {"heads": _heads},
    "pinned": {"target": _label, "actor": _label},
    "unpinned": {"target": _label, "actor": _label},
    "policy": {"value": _label, "actor": _label},
    "pruned": {"target": _label, "digest": _hex, "counts": _counts},
    "anchor": {"head": _head, "count": _int, "backend": _label},
    "score_late": {},
}


def sanitize_payload(kind: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the allow-listed, well-typed keys for ``kind``.

    Unknown keys and invalid values are dropped, never raised: the ledger is
    best effort and must not fail a document over a malformed field. Raises
    ``ValueError`` for an unknown kind or a payload above ``MAX_PAYLOAD_BYTES``.
    """
    if kind not in _SPECS:
        raise ValueError(f"unknown ledger kind: {kind!r}")
    clean: dict[str, Any] = {}
    for key, check in _SPECS[kind].items():
        if payload and key in payload:
            value = check(payload[key])
            if value is not None:
                clean[key] = value
    if len(canonical_json(clean).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"ledger payload for {kind!r} exceeds {MAX_PAYLOAD_BYTES} bytes"
        )
    return clean
