"""Ingress simulator: render scenario timelines into messages and meter admission.

Rendering is the scripted path only (``gen: scripted`` templates plus the pinned
attachments); no model is involved. Metering applies ``email/ingress_policy.yaml``
with token buckets driven by *simulated* time, per-sender and hourly inbox caps
and a bounded queue; a full queue sheds to a soft hold (``comms/pending``) and is
never dropped silently.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2.sandbox import SandboxedEnvironment

from mailroom_reloaded.sandbox.server.content import SandboxContent

__all__ = [
    "IngressMeter",
    "PlannedItem",
    "doc_id_for_bytes",
    "parse_offset",
    "plan_scenario",
    "thread_id_for",
]


def parse_offset(at: str | int) -> int:
    """``mm:ss`` or ``hh:mm:ss`` (scenario ``at``) -> seconds.

    YAML 1.1 reads an unquoted ``01:30`` as the sexagesimal int 90, which is the
    same number of seconds, so ints pass through unchanged.
    """
    if isinstance(at, int):
        return at
    parts = [int(p) for p in at.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def doc_id_for_bytes(data: bytes) -> str:
    """Same rule as ``storage.bins.doc_id_for``: first 16 hex of the content sha256."""
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass
class PlannedItem:
    scenario: str
    step: int
    kind: str  # "email" | "document" | "fault"
    offset_s: int
    wire: dict[str, Any]
    truth: dict[str, Any] = field(
        default_factory=dict
    )  # never shown to the Correspondent
    notes: list[str] = field(default_factory=list)


_ENV = SandboxedEnvironment(autoescape=False, keep_trailing_newline=True)
_SUBJECT = re.compile(r"^\s*Subject:\s*(.*?)\s*$", re.MULTILINE)


def _local_name(addr: str) -> str:
    """Return the part of an email address before the first at-sign."""
    return addr.split("@", 1)[0]


def _entry_for(path: Path, name: str, **extra: Any) -> dict:
    data = path.read_bytes()
    return {
        "name": name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "doc_id": doc_id_for_bytes(data),
        "path": str(path),
        "resolved": True,
        **extra,
    }


def _attachment(
    content: SandboxContent,
    spec: dict,
    bound: dict[str, dict],
    synth_dir: Path | None = None,
) -> tuple[dict, str | None]:
    """Resolve one ``attach`` entry to wire metadata, plus a note when unresolved."""
    if "same_as" in spec:
        src = bound.get(spec["same_as"])
        if src is None:
            return {
                "name": spec["same_as"],
                "resolved": False,
            }, f"same_as {spec['same_as']!r} unbound"
        entry = dict(src)
        if "as" in spec:
            entry["name"] = spec["as"]
        entry["same_as"] = spec["same_as"]
        if spec.get("ref"):
            entry["ref"] = spec["ref"]  # the new label for the exact same document
            bound[spec["ref"]] = entry
        return entry, None
    fname = spec.get("file")
    if not fname and synth_dir is not None and spec.get("class"):
        from mailroom_reloaded.sandbox.server.synthetic import materialise_draw

        path, name = materialise_draw(spec, synth_dir)
        entry = _entry_for(
            path,
            name,
            synthetic=True,
            ref=spec.get("ref"),
            draw=f"{spec.get('class')}/{spec.get('stratum')}",
        )
        if spec.get("ref"):
            bound[spec["ref"]] = entry
        return entry, f"synthetic placeholder for dataset draw {entry['draw']}"
    if not fname:
        label = f"{spec.get('class')}/{spec.get('stratum')}"
        return (
            {"name": spec.get("as") or f"<dataset draw {label}>", "resolved": False},
            f"unresolved dataset draw {label} (the dataset is not part of an offline pack)",
        )
    path = content.attachment_path(fname)
    if path is None:
        return {
            "name": spec.get("as") or fname,
            "resolved": False,
        }, f"attachment {fname!r} not in content"
    entry = _entry_for(
        path, spec.get("as") or fname, source_file=fname, ref=spec.get("ref")
    )
    if spec.get("ref"):
        bound[spec["ref"]] = entry
    return entry, None


def _render_email(
    content: SandboxContent, client: dict, attachments: list[dict]
) -> tuple[str, str, list[str]]:
    """Render a local template into subject, body, and notes about missing inputs."""
    notes: list[str] = []
    name = client["template"]
    path = content.template_path(name)
    if path is None:
        return (
            "(template missing)",
            f"template {name!r} is not in this content pack",
            [f"missing template {name}"],
        )
    variables = dict(client.get("vars") or {})
    claimed = client.get("claimed_from", "")
    if "sender_name" not in variables:
        variables["sender_name"] = _local_name(claimed)
        notes.append("sender_name defaulted to the address local part")
    if "attachment_name" not in variables:
        variables["attachment_name"] = attachments[0]["name"] if attachments else ""
    text = _ENV.from_string(path.read_text(encoding="utf-8")).render(**variables)
    # Drop Jinja comment-only lines already removed by the engine; split subject.
    match = _SUBJECT.search(text)
    subject = match.group(1) if match else "(no subject)"
    body = text[match.end() :] if match else text
    return subject, body.strip("\n") + "\n", notes


def plan_scenario(
    content: SandboxContent, name: str, synth_dir: Path | None = None
) -> list[PlannedItem]:
    """Expand a scenario timeline into renderable inbound items (document feeds + emails)."""
    scenario = content.cs.scenarios[name]
    items: list[PlannedItem] = []
    bound: dict[str, dict] = {}
    for i, step in enumerate(scenario.get("timeline", [])):
        offset = parse_offset(step.get("at", "00:00"))
        if "ingress" in step:
            spec = step["ingress"]
            att, note = _attachment(content, dict(spec), bound, synth_dir)
            items.append(
                PlannedItem(
                    name,
                    i,
                    "document",
                    offset,
                    wire={
                        "from": "(document feed)",
                        "subject": att["name"],
                        "body": "",
                        "attachments": [att],
                        "auth": {},
                    },
                    notes=[note] if note else [],
                )
            )
        elif "client" in step:
            c = step["client"]
            atts: list[dict] = []
            notes: list[str] = []
            for spec in c.get("attach", []) or []:
                att, note = _attachment(content, spec, bound, synth_dir)
                atts.append(att)
                if note:
                    notes.append(note)
            subject, body, rnotes = _render_email(content, c, atts)
            notes += rnotes
            auth = dict(c.get("auth") or {})
            if not auth:
                auth = {"spf": "pass", "dkim": "pass", "dmarc": "pass"}
                notes.append(
                    "auth omitted in the scenario: defaulted to spf/dkim/dmarc pass"
                )
            persona = content.personas.get(c.get("persona", ""), {})
            items.append(
                PlannedItem(
                    name,
                    i,
                    "email",
                    offset,
                    wire={
                        "from": c.get("claimed_from", ""),
                        "to": "correspondent@mailroom.sandbox.invalid",
                        "subject": subject,
                        "body": body,
                        "auth": auth,
                        "attachments": atts,
                        "channel": c.get("channel", "email"),
                    },
                    truth={
                        "ref": c.get("ref"),
                        "persona": c.get("persona"),
                        "role": persona.get("role"),
                        "client_id": persona.get("client_id"),
                    },
                    notes=notes,
                )
            )
        elif "fault" in step:
            items.append(
                PlannedItem(
                    name,
                    i,
                    "fault",
                    offset,
                    wire={
                        "from": "(fault)",
                        "subject": str(step["fault"])[:80],
                        "body": "",
                        "attachments": [],
                        "auth": {},
                    },
                    notes=["fault directives are not simulated by the ingress sandbox"],
                )
            )
    return items


# ----------------------------------------------------------------------------- metering


class _Bucket:
    def __init__(self, per_min: float, burst: float, depth_max: int) -> None:
        """Initialize a full token bucket with a refill rate and bounded queue depth."""
        self.rate = per_min / 60.0
        self.burst = float(burst)
        self.tokens = float(burst)
        self.last: float | None = None
        self.depth_max = depth_max

    def take(self, t: float) -> dict:
        """Refill at simulated time t and admit, queue, or shed one item."""
        if self.last is not None and t > self.last:
            self.tokens = min(self.burst, self.tokens + (t - self.last) * self.rate)
        self.last = t if self.last is None else max(self.last, t)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return {"status": "admitted", "wait_s": 0.0, "queue_depth": 0}
        depth = math.ceil(1.0 - self.tokens)
        if depth > self.depth_max:
            return {
                "status": "shed",
                "wait_s": 0.0,
                "queue_depth": depth - 1,
                "reason": "queue_full",
            }
        self.tokens -= 1.0
        return {
            "status": "queued",
            "wait_s": round(-self.tokens / self.rate, 3) if self.rate else 0.0,
            "queue_depth": math.ceil(-self.tokens),
        }


class IngressMeter:
    """Token-bucket admission per edge plus the Correspondent inbox caps.

    A thread counts as "open" for ``THREAD_OPEN_S`` simulated seconds after its
    last message (an approximation: the real inbox closes threads when the Boss
    Desk resolves them).
    """

    THREAD_OPEN_S = 1800.0

    def __init__(self, policy: dict) -> None:
        """Build admission buckets and inbox-cap counters from the ingress policy."""
        self.policy = policy
        src = policy.get("sources", {})
        queues = policy.get("queues", {})
        pdepth = int(queues.get("pipeline_ingress", {}).get("depth_max", 2000))
        cdepth = int(queues.get("correspondent_inbox_queue", {}).get("depth_max", 300))
        self.buckets = {
            "documents": _Bucket(
                src["documents"]["rate"]["max_items_per_minute"],
                src["documents"]["rate"]["burst"],
                pdepth,
            ),
            "emails": _Bucket(
                src["emails"]["rate"]["max_items_per_minute"],
                src["emails"]["rate"]["burst"],
                pdepth,
            ),
            "external_correspondence": _Bucket(
                src["external_correspondence"]["rate"]["max_items_per_minute"],
                src["external_correspondence"]["rate"]["burst"],
                cdepth,
            ),
        }
        self.per_sender_per_hour = int(
            src["external_correspondence"].get("per_sender_per_hour", 12)
        )
        inbox = policy.get("correspondent_inbox", {})
        self.max_per_hour = int(inbox.get("max_admissions_per_hour", 120))
        self.max_open_threads = int(inbox.get("max_concurrent_open_threads", 30))
        self._sender: dict[str, deque[float]] = {}
        self._hour: deque[float] = deque()
        self._threads: dict[str, float] = {}  # thread -> last activity (sim s)

    def reset(self) -> None:
        """Reinitialize all admission counters and buckets from the current policy."""
        self.__init__(self.policy)  # type: ignore[misc]

    def _decision(
        self, edges: list[dict], status: str, reason: str | None, t: float
    ) -> dict:
        """Combine edge decisions into a status, reason, and simulated admission time."""
        wait = max((e.get("wait_s", 0.0) for e in edges), default=0.0)
        return {
            "status": status,
            "reason": reason,
            "edges": edges,
            "admitted_at": t + wait if status != "shed" else None,
        }

    def admit_document(self, t: float) -> dict:
        """Meter a document at simulated time t through the document bucket."""
        e = {"edge": "documents", **self.buckets["documents"].take(t)}
        status = "shed" if e["status"] == "shed" else e["status"]
        return self._decision([e], status, e.get("reason"), t)

    def admit_email(self, t: float, sender: str, thread_id: str) -> dict:
        """Apply inbox caps and both email buckets, refunding capacity on shedding."""
        edges: list[dict] = []
        hist = self._sender.setdefault(sender.lower(), deque())
        while hist and t - hist[0] >= 3600:
            hist.popleft()
        while self._hour and t - self._hour[0] >= 3600:
            self._hour.popleft()
        if len(hist) >= self.per_sender_per_hour:
            return self._decision(
                edges, "shed", f"per_sender_per_hour>{self.per_sender_per_hour}", t
            )
        if len(self._hour) >= self.max_per_hour:
            return self._decision(
                edges, "shed", f"max_admissions_per_hour>{self.max_per_hour}", t
            )
        open_now = {k for k, v in self._threads.items() if t - v < self.THREAD_OPEN_S}
        if thread_id not in open_now and len(open_now) >= self.max_open_threads:
            return self._decision(
                edges, "shed", f"max_concurrent_open_threads>{self.max_open_threads}", t
            )
        for edge in ("emails", "external_correspondence"):
            r = {"edge": edge, **self.buckets[edge].take(t)}
            edges.append(r)
            if r["status"] == "shed":
                if edge == "external_correspondence":
                    self.buckets["emails"].tokens += 1.0
                return self._decision(
                    edges, "shed", f"{edge}:{r.get('reason', 'queue_full')}", t
                )
        hist.append(t)
        self._hour.append(t)
        self._threads[thread_id] = t
        status = "queued" if any(e["status"] == "queued" for e in edges) else "admitted"
        return self._decision(edges, status, None, t)

    def snapshot(self) -> dict:
        """Return current bucket balances, tracked inbox counts, and configured caps."""
        return {
            "buckets": {
                k: {
                    "tokens": round(b.tokens, 2),
                    "burst": b.burst,
                    "per_min": round(b.rate * 60, 2),
                    "depth_max": b.depth_max,
                }
                for k, b in self.buckets.items()
            },
            "inbox": {
                "admissions_last_hour": len(self._hour),
                "max_per_hour": self.max_per_hour,
                "open_threads": len(self._threads),
                "max_open_threads": self.max_open_threads,
                "per_sender_per_hour": self.per_sender_per_hour,
            },
        }


def thread_id_for(subject: str, sender: str) -> str:
    """Wire-visible thread key: normalized subject + sender (no scenario/persona ids)."""
    norm = re.sub(r"^(re|fwd?):\s*", "", subject.strip().lower())
    return "thr_" + hashlib.sha1(f"{norm}|{sender.lower()}".encode()).hexdigest()[:10]
