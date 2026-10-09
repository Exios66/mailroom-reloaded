"""Dev generator for the shipped showcase trace runs in ``src/mailroom_reloaded/showcase``.

Run ``PYTHONPATH=src:tests python tests/fixtures/replay/make_showcase.py``; output is deterministic.
Rows come from the ``span_rows`` builders, re-id'd, re-timed (2026) and stripped to content-free attrs.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fixtures.replay import span_rows as F

from mailroom_reloaded.storage.span_store import (
    _ATTR_EXACT,
    _ATTR_PREFIXES,
    _EVENT_KEYS,
)

OUT_DIR = Path(__file__).resolve().parents[3] / "src" / "mailroom_reloaded" / "showcase"
DOC_TYPES = [
    "invoice",
    "receipt",
    "purchase_order",
    "bank_statement",
    "contract",
    "tax_form",
]
Builder = Callable[[], list[dict[str, Any]]]


def _h(*parts: Any, width: int) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:width]


def _extras(
    rows: list[dict[str, Any]], plan: list[tuple[str, float, float, int, int, float]]
):
    """Add judge/arbiter generations under the document's ``verify`` node."""
    node = next(r for r in rows if r["attrs"].get("mailroom.node") == "verify")
    for role, t0, t1, prompt, completion, cost in plan:
        t0 += node["start_ns"] / 1e9 - F.T0
        g = F._llm(
            "", node["trace_id"], node, role, t0, t0 + t1, prompt, completion, cost
        )
        g["attrs"]["mailroom.model"] = f"{role}-model-1"
        rows.append(g)


def _keep(key: str) -> bool:
    return (
        key in _ATTR_EXACT
        or key.startswith(_ATTR_PREFIXES)
        and key != "mailroom.filename"
    )


def _instance(rows, run, session, n, slug, base_ns, scale, doc_type, plan=None):
    rows = [dict(r, attrs=dict(r["attrs"])) for r in rows]
    if plan:
        _extras(rows, plan)
    sid = {r["span_id"]: _h(run, n, i, width=16) for i, r in enumerate(rows)}
    tid = {r["trace_id"]: _h(run, n, "trace", width=32) for r in rows}
    t_zero = F.ns(0)

    def shift(t: int) -> int:
        return base_ns + int((t - t_zero) * scale)

    doc = f"doc-showcase-{slug}-{n}"
    out = []
    for r in rows:
        attrs = {k: v for k, v in r["attrs"].items() if _keep(k)}
        attrs["mailroom.run_id"] = run
        attrs["session.id"] = session
        if r["doc_id"]:
            attrs["mailroom.doc_id"] = doc
        if "mailroom.doc_type" in attrs:
            attrs["mailroom.doc_type"] = doc_type
        if "mailroom.gt.expected_doc_class" in attrs:
            attrs["mailroom.gt.expected_doc_class"] = doc_type
        events = []
        for e in r["events"]:
            if e["name"] == "mailroom.custom_thing":
                continue
            ea = e.get("attrs") or {}
            if e["name"] != "exception":
                ea = {k: v for k, v in ea.items() if k in _EVENT_KEYS}
            events.append({"name": e["name"], "t": shift(e["t"]), "attrs": ea})
        out.append(
            {
                **r,
                "span_id": sid[r["span_id"]],
                "trace_id": tid[r["trace_id"]],
                "parent_id": sid[r["parent_id"]] if r["parent_id"] else None,
                "start_ns": shift(r["start_ns"]),
                "end_ns": shift(r["end_ns"]),
                "doc_id": doc if r["doc_id"] else None,
                "run_id": run,
                "session_id": session,
                "attrs": attrs,
                "events": events,
            }
        )
    return out


_JA = [("judge", 0.1, 0.5, 220, 40, 0.0021), ("arbiter", 0.7, 0.6, 340, 70, 0.0034)]
_J2 = [("judge", 0.05, 0.4, 200, 35, 0.0019), ("judge", 0.5, 0.4, 210, 38, 0.002),
       ("arbiter", 0.95, 0.7, 380, 90, 0.0041)]  # fmt: skip

# slug -> (title, start (UTC), [(builder, plan)])
SPECS: dict[str, tuple[str, datetime, list[tuple[Builder, Any]]]] = {
    "clean": (
        "Clean run: five documents archived",
        datetime(2026, 2, 2, 9, 0, tzinfo=UTC),
        [(F.happy, None)] * 5,
    ),
    "escalation": (
        "Retries and a boss escalation",
        datetime(2026, 2, 9, 14, 30, tzinfo=UTC),
        [
            (F.happy, None),
            (F.retry, None),
            (F.boss, None),
            (F.retry, None),
            (F.boss, None),
            (F.happy, None),
        ],
    ),
    "parked-failed": (
        "Parked and failed documents among archived ones",
        datetime(2026, 2, 16, 11, 15, tzinfo=UTC),
        [
            (F.happy, None),
            (F.parked, None),
            (F.failed, None),
            (F.parked, None),
            (F.failed, None),
            (F.happy, None),
        ],
    ),
    "judge-arbiter": (
        "Judge and arbiter generations",
        datetime(2026, 2, 23, 16, 45, tzinfo=UTC),
        [
            (F.happy, _JA),
            (F.boss, _J2),
            (F.happy, _J2),
            (F.parked, _JA),
            (F.boss, _JA),
            (F.llm_child, None),
        ],
    ),
}


def build() -> dict[str, dict[str, Any]]:
    """``{file name: payload}`` for the four showcase runs."""
    out: dict[str, dict[str, Any]] = {}
    for slug, (title, start, docs) in SPECS.items():
        run, session = f"showcase-{slug}", f"showcase-{slug}-session"
        spans: list[dict[str, Any]] = []
        for n, (builder, plan) in enumerate(docs, 1):
            base = int(start.timestamp() * 1e9) + (n - 1) * 25 * 10**9
            scale = 1.0 + 0.15 * (n % 3)
            spans += _instance(
                builder(), run, session, n, slug, base, scale,
                DOC_TYPES[(n - 1) % len(DOC_TYPES)], plan,
            )  # fmt: skip
        spans.sort(key=lambda s: (s["start_ns"], s["span_id"]))
        out[f"{slug}.json"] = {"run_id": run, "title": title, "spans": spans}
    return out


def render() -> dict[str, bytes]:
    """File name -> exact bytes, including ``manifest.json``."""
    files = {
        name: (json.dumps(p, sort_keys=True, indent=1) + "\n").encode()
        for name, p in build().items()
    }
    digests = {n: hashlib.sha256(b).hexdigest() for n, b in sorted(files.items())}
    manifest = json.dumps({"files": digests}, sort_keys=True, indent=1) + "\n"
    return {**files, "manifest.json": manifest.encode()}


def main(out_dir: Path = OUT_DIR) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in render().items():
        (out_dir / name).write_bytes(data)
        print(f"wrote {out_dir / name} ({len(data)} bytes)")


if __name__ == "__main__":
    sys.exit(main())
