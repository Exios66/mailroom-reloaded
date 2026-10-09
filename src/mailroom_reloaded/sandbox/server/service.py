"""SandboxService: ties ingress, Correspondent (Flow A), pipeline (Flow B) and egress.

One service instance owns a data directory::

    <data-dir>/sandbox/state.json     messages, events, outbox (survives restarts)
    <data-dir>/pipeline/              the pipeline's own Bins + mailroom.db (NOT ./data)
    <data-dir>/comms/pending/         soft-held attachment copies (never opened)
    <data-dir>/comms/quarantine/      metadata only for quarantined attachments

Messages are processed one at a time by a worker thread in simulated-time order.
"""

from __future__ import annotations

import copy
import json
import os
import queue
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mailroom_reloaded.sandbox.server.bossdesk import StandInBossDesk
from mailroom_reloaded.sandbox.server.content import SandboxContent
from mailroom_reloaded.sandbox.server.correspondent import (
    AttachmentView,
    WireMessage,
    create_correspondent,
)
from mailroom_reloaded.sandbox.server.egress import VirtualOutbox
from mailroom_reloaded.sandbox.server.evaluate import compare_scenario
from mailroom_reloaded.sandbox.server.guard import NetworkGuard
from mailroom_reloaded.sandbox.server.ingress import (
    IngressMeter,
    plan_scenario,
    thread_id_for,
)
from mailroom_reloaded.sandbox.server.pipeline_runner import PipelineRunner

__all__ = ["FLOWS", "SandboxService"]

FLOWS = ("correspondent", "pipeline")


def extract_text(path: str) -> str:
    """Plain text of an attachment for the stand-in's link heuristics (no OCR, no network)."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            import pypdf

            return "\n".join(
                (pg.extract_text() or "") for pg in pypdf.PdfReader(str(p)).pages
            )
        if ext in {".txt", ".md", ".csv"}:
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - unreadable attachment: no text
        return ""
    return ""


class _Tools:
    """The Correspondent's read-only tool surface bound to one message."""

    def __init__(
        self, svc: SandboxService, att_paths: dict[str, str], quarantined: set[str]
    ) -> None:
        """Bind read-only tools to a service and message-specific attachment access rules."""
        self.svc = svc
        self.att_paths = att_paths
        self.quarantined = quarantined

    def registry(self) -> dict[str, dict]:
        """Return the content pack registry clients."""
        return self.svc.content.registry_clients

    def lookup_catalog(self) -> list[dict]:
        """Snapshot document identifiers, filenames, statuses, and text under the lock."""
        with self.svc._lock:
            return [
                {
                    "doc_id": d["doc_id"],
                    "filename": d["filename"],
                    "status": d["status"],
                    "text": d.get("text", ""),
                }
                for d in self.svc.docs.values()
            ]

    def read_attachment_text(self, att: AttachmentView) -> str:
        """Extract attachment text unless its name is explicitly quarantined."""
        if att.name in self.quarantined:
            raise PermissionError("quarantined attachments are never opened")
        return extract_text(self.att_paths.get(att.name, ""))


class SandboxService:
    def __init__(
        self,
        content: SandboxContent,
        data_dir: Path | str,
        *,
        egress_profile: str = "closed",
        autonomy: str = "human",
        show_expected: bool = True,
        clock: Callable[[], float] = time.time,
        guard: NetworkGuard | None = None,
        pipeline: PipelineRunner | None = None,
    ) -> None:
        """Configure isolated sandbox components and empty state without starting them."""
        self.content = content
        self.data_dir = Path(data_dir)
        self.state_dir = self.data_dir / "sandbox"
        self.show_expected = show_expected
        self.autonomy = autonomy
        self.clock = clock
        self.guard = guard
        self._lock = threading.RLock()
        self._work_lock = threading.RLock()
        self._q: queue.Queue[tuple[str, list[str]]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self.agent = create_correspondent("standin")
        self.desk = StandInBossDesk(content.policy.delegation)
        self.meter = IngressMeter(content.policy.ingress)
        self.pipeline = pipeline or PipelineRunner(self.data_dir)
        routes = {
            a: f"sender-pool inbox for {cid}"
            for cid, c in content.registry_clients.items()
            for a in c.get("verified_addresses", [])
        }
        self.messages: dict[str, dict] = {}
        self.events: list[dict] = []
        self.docs: dict[str, dict] = {}
        self.batches: list[dict] = []
        self._mseq = 0
        self._bseq = 0
        self._sim_next = 0.0
        self.outbox = VirtualOutbox(
            content.policy, routes, self.data_dir, self.emit, clock, egress_profile
        )

    # ------------------------------------------------------------------ lifecycle
    def start(self, worker: bool = True) -> SandboxService:
        """Activate the pipeline, restore saved state, and optionally start the worker."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.pipeline.activate()
        self._load()
        if worker and self._worker is None:
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._run_worker, name="sandbox-worker", daemon=True
            )
            self._worker.start()
        return self

    def stop(self) -> None:
        """Request worker shutdown, persist state, and deactivate the pipeline."""
        self._stop.set()
        if self._worker is not None:
            self._q.put(("", []))
            self._worker.join(timeout=10)
            self._worker = None
        self._save()
        self.pipeline.deactivate()

    def _run_worker(self) -> None:
        """Process queued messages serially, recording failures and completing each task."""
        while not self._stop.is_set():
            mid, flows = self._q.get()
            try:
                if mid:
                    self._process(mid, flows)
            except Exception as exc:  # noqa: BLE001 - one bad message must not kill the worker
                self._fail(mid, exc)
            finally:
                self._q.task_done()

    def wait_idle(self, timeout: float = 120.0) -> bool:
        """Block until queued work is done (tests, ``wait=true`` API calls)."""
        if self._worker is None:
            while not self._q.empty():
                mid, flows = self._q.get()
                try:
                    if mid:
                        self._process(mid, flows)
                except Exception as exc:  # noqa: BLE001
                    self._fail(mid, exc)
                finally:
                    self._q.task_done()
            return True
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        return not self._q.unfinished_tasks

    # ------------------------------------------------------------------ persistence
    def _save(self) -> None:
        """Atomically replace the persisted JSON snapshot while holding the state lock."""
        with self._lock:
            blob = {
                "messages": self.messages,
                "events": self.events,
                "docs": self.docs,
                "batches": self.batches,
                "mseq": self._mseq,
                "bseq": self._bseq,
                "sim_next": self._sim_next,
                "outbox": self.outbox.dump(),
                "autonomy": self.autonomy,
            }
            tmp = self.state_dir / "state.json.tmp"
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(blob, default=str), encoding="utf-8")
            os.replace(tmp, self.state_dir / "state.json")

    def _load(self) -> None:
        """Restore readable saved state and mark interrupted processing as admitted."""
        path = self.state_dir / "state.json"
        if not path.is_file():
            return
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            self.messages = blob.get("messages", {})
            self.events = blob.get("events", [])
            self.docs = blob.get("docs", {})
            self.batches = blob.get("batches", [])
            self._mseq, self._bseq = blob.get("mseq", 0), blob.get("bseq", 0)
            self._sim_next = blob.get("sim_next", 0.0)
            self.autonomy = blob.get("autonomy", self.autonomy)
            self.outbox.load(blob.get("outbox", {}))
            for m in self.messages.values():  # interrupted work resumes as pending
                if m["state"] == "processing":
                    m["state"] = "admitted"

    def _drain_queue(self) -> None:
        """Discard pending queue entries while balancing unfinished-task accounting."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return
            self._q.task_done()

    def reset(self) -> None:
        """Drain queued work, wait for processing, then clear sandbox and pipeline state."""
        self._drain_queue()
        self.wait_idle(30)
        with self._work_lock, self._lock:
            self._drain_queue()
            self.messages, self.events, self.docs, self.batches = {}, [], {}, []
            self._mseq = self._bseq = 0
            self._sim_next = 0.0
            self.meter.reset()
            self.outbox.items, self.outbox._seq = {}, 0
            self.pipeline.reset()
            for sub in ("comms/pending", "comms/quarantine"):
                shutil.rmtree(self.data_dir / sub, ignore_errors=True)
            self._save()

    # ------------------------------------------------------------------ events
    def emit(self, kind: str, ref_id: str, payload: dict | None = None) -> dict:
        """Append and return a timestamped event with the next sequence number."""
        with self._lock:
            ev = {
                "seq": len(self.events) + 1,
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "kind": kind,
                "ref_id": ref_id,
                "payload": payload or {},
            }
            self.events.append(ev)
            return ev

    def events_since(
        self, since: int = 0, ref_id: str | None = None, limit: int = 500
    ) -> list[dict]:
        """Copy events after a sequence number, optionally filtering by reference."""
        with self._lock:
            out = [
                e
                for e in self.events
                if e["seq"] > since and (ref_id is None or e["ref_id"] == ref_id)
            ]
            return copy.deepcopy(out[:limit])

    # ------------------------------------------------------------------ inject
    def inject(
        self,
        scenario_ids: list[str] | str,
        *,
        flows: list[str] | None = None,
        stagger_seconds: int = 0,
        process: bool = True,
    ) -> dict:
        """Render and meter scenarios in simulated-time order, optionally queuing work."""
        flows = list(flows) if flows is not None else list(FLOWS)
        bad = [f for f in flows if f not in FLOWS]
        if bad:
            raise ValueError(f"unknown flows {bad}; choose from {list(FLOWS)}")
        names = (
            self.content.scenario_ids() if scenario_ids == "all" else list(scenario_ids)
        )
        unknown = [n for n in names if n not in self.content.cs.scenarios]
        if unknown:
            raise KeyError(unknown)
        planned = []
        for idx, name in enumerate(names):
            for it in plan_scenario(self.content, name):
                planned.append((idx * stagger_seconds + it.offset_s, idx, it))
        planned.sort(key=lambda t: (t[0], t[1], t[2].step))
        with self._lock:
            self._bseq += 1
            bid = f"b{self._bseq:03d}"
            base = max(self._sim_next, self.clock())
            batch = {
                "id": bid,
                "scenarios": names,
                "flows": flows,
                "sim_base": base,
                "messages": [],
                "created": datetime.now(UTC).isoformat(),
            }
            self.batches.append(batch)
        ids: list[str] = []
        for off, _idx, it in planned:
            with self._lock:
                self._mseq += 1
                mid = f"m{self._mseq:04d}"
                sim_ts = base + off
                wire = it.wire
                thread = (
                    thread_id_for(wire.get("subject", ""), wire.get("from", ""))
                    if it.kind == "email"
                    else ""
                )
                msg = {
                    "id": mid,
                    "batch_id": bid,
                    "scenario": it.scenario,
                    "step": it.step,
                    "kind": it.kind,
                    "sim_offset_s": off,
                    "sim_ts": sim_ts,
                    "thread_id": thread,
                    "wire": wire,
                    "truth": it.truth,
                    "notes": it.notes,
                    "flows": flows,
                    "flows_done": [],
                    "state": "injected",
                    "admission": None,
                    "correspondent": None,
                    "bossdesk": [],
                    "handoffs": [],
                    "outbox_ids": [],
                    "error": None,
                }
                self.messages[mid] = msg
                batch["messages"].append(mid)
                self._sim_next = max(self._sim_next, sim_ts + 1)
            self.emit(
                "ingress.sent",
                mid,
                {
                    "scenario": it.scenario,
                    "kind": it.kind,
                    "from": wire.get("from"),
                    "sim_ts": sim_ts,
                },
            )
            if it.kind == "fault":
                with self._lock:
                    msg["state"] = "skipped"
                self.emit("fault.skipped", mid, {"note": it.notes[0]})
            else:
                self._admit(msg)
            ids.append(mid)
        if process:
            for mid in ids:
                if self.messages[mid]["state"] in {"admitted", "queued"}:
                    self._q.put((mid, flows))
        self._save()
        return {
            "batch_id": bid,
            "message_ids": ids,
            "states": {m: self.messages[m]["state"] for m in ids},
        }

    def _admit(self, msg: dict) -> None:
        """Apply admission policy, update message state, and record any shed hold."""
        with self._lock:
            if msg["kind"] == "document":
                dec = self.meter.admit_document(msg["sim_ts"])
            else:
                dec = self.meter.admit_email(
                    msg["sim_ts"], msg["wire"].get("from", ""), msg["thread_id"]
                )
            msg["admission"] = dec
            msg["state"] = {"admitted": "admitted", "queued": "queued", "shed": "shed"}[
                dec["status"]
            ]
        kind = {
            "admitted": "ingress.admitted",
            "queued": "ingress.queued",
            "shed": "ingress.shed",
        }[dec["status"]]
        self.emit(
            kind,
            msg["id"],
            {
                "edges": dec["edges"],
                "reason": dec["reason"],
                "destination": "comms/pending" if dec["status"] == "shed" else None,
            },
        )
        if dec["status"] == "shed":
            self._hold_shed(msg)

    def _hold_shed(self, msg: dict) -> None:
        """Write a shed-message marker and reason under comms/pending."""
        d = self.data_dir / "comms" / "pending" / msg["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "message.json").write_text(
            json.dumps({"id": msg["id"], "reason": msg["admission"]["reason"]}),
            encoding="utf-8",
        )

    def release_message(self, mid: str) -> dict:
        """Human release of a shed message from comms/pending: bypasses metering, audited."""
        msg = self._get(mid)
        if msg["state"] != "shed":
            raise ValueError(f"{mid} is {msg['state']}, not shed")
        with self._lock:
            msg["state"] = "admitted"
            msg["admission"] = {**msg["admission"], "released": True}
        self.emit("ingress.released", mid, {"by": "human"})
        self._q.put((mid, msg["flows"]))
        return self.message(mid)

    # ------------------------------------------------------------------ processing
    def _get(self, mid: str) -> dict:
        """Return the mutable stored message or raise KeyError for an unknown identifier."""
        with self._lock:
            if mid not in self.messages:
                raise KeyError(mid)
            return self.messages[mid]

    def run_message(self, mid: str, flows: list[str]) -> dict:
        """Queue selected flows for a message, refusing messages still in a shed state."""
        msg = self._get(mid)
        if msg["state"] == "shed":
            raise ValueError("message is shed; release it first")
        with self._lock:
            msg["flows"] = sorted(set(msg["flows"]) | set(flows))
        self._q.put((mid, flows))
        return self.message(mid)

    def _fail(self, mid: str, exc: Exception) -> None:
        """Record a processing error and event if the message still exists."""
        if mid in self.messages:
            with self._lock:
                self.messages[mid]["state"] = "error"
                self.messages[mid]["error"] = f"{type(exc).__name__}: {exc}"
            self.emit("run.error", mid, {"error": str(exc)})

    def _process(self, mid: str, flows: list[str]) -> None:
        """Run selected flows under the work lock, record completion, and persist state."""
        with self._work_lock:
            msg = self._get(mid)
            if msg["state"] in {"shed", "skipped"}:
                return
            with self._lock:
                msg["state"] = "processing"
            if msg["kind"] == "document":
                self._process_document(msg, flows)
            else:
                self._process_email(msg, flows)
            with self._lock:
                msg["state"] = "processed"
            self.emit("message.processed", mid, {"flows_done": msg["flows_done"]})
            self._save()

    def _run_pipeline_for(self, msg: dict, entry: dict, path: str) -> None:
        """Process an attachment and record its handoff result, catalog view, and events."""
        self.emit(
            "pipeline.started",
            msg["id"],
            {"doc_id": entry["doc_id"], "name": entry["name"]},
        )
        res = self.pipeline.run(Path(path), entry["name"])
        with self._lock:
            entry["pipeline"] = res
            entry["status"] = (
                "pipeline_done" if not res.get("error") else "pipeline_error"
            )
            self.docs[res["doc_id"]] = {
                "doc_id": res["doc_id"],
                "filename": entry["name"],
                "status": res["status"],
                "text": res.get("text", ""),
                "message_id": msg["id"],
                "scenario": msg["scenario"],
                "doc_type": res.get("doc_type"),
            }
        self.emit(
            "pipeline.done",
            msg["id"],
            {
                k: res[k]
                for k in (
                    "doc_id",
                    "status",
                    "route_trail",
                    "audit_chain_ok",
                    "reused",
                    "structured_llm_calls",
                )
            },
        )

    def _process_document(self, msg: dict, flows: list[str]) -> None:
        """Run a resolved document feed through Flow B once when requested."""
        att = msg["wire"]["attachments"][0]
        if "pipeline" not in flows or "pipeline" in msg["flows_done"]:
            return
        if not att.get("resolved"):
            self.emit(
                "pipeline.skipped",
                msg["id"],
                {"reason": "document bytes not in content"},
            )
            return
        entry = {
            "name": att["name"],
            "doc_id": att["doc_id"],
            "lane": "document_feed",
            "status": "queued",
            "reason": "document ingress edge",
            "pipeline": None,
        }
        with self._lock:
            msg["handoffs"] = [entry]
        self.emit(
            "attachment.handoff",
            msg["id"],
            {"name": att["name"], "doc_id": att["doc_id"], "via": "documents edge"},
        )
        self._run_pipeline_for(msg, entry, att["path"])
        msg["flows_done"].append("pipeline")

    def _wire(self, msg: dict) -> WireMessage:
        """Build agent-visible message data without scenario truth or attachment paths."""
        w = msg["wire"]
        return WireMessage(
            message_id=msg["id"],
            thread_id=msg["thread_id"],
            from_addr=w["from"],
            subject=w["subject"],
            body=w["body"],
            auth=w.get("auth", {}),
            attachments=[
                AttachmentView(
                    a["name"],
                    a.get("size", 0),
                    a.get("sha256", ""),
                    a.get("doc_id", ""),
                    a.get("resolved", False),
                )
                for a in w["attachments"]
            ],
            received_sim_ts=msg["sim_ts"],
            channel=w.get("channel", "email"),
        )

    def _process_email(self, msg: dict, flows: list[str]) -> None:
        """Run requested correspondence and pipeline flows, recording decisions and drafts."""
        mid = msg["id"]
        atts = {a["name"]: a for a in msg["wire"]["attachments"]}
        if "correspondent" in flows and "correspondent" not in msg["flows_done"]:
            self.emit(
                "message.received",
                mid,
                {
                    "from": msg["wire"]["from"],
                    "subject": msg["wire"]["subject"],
                    "attachments": list(atts),
                },
            )
            wire = self._wire(msg)
            quarantined: set[str] = set()
            res = self.agent.handle(
                wire,
                _Tools(
                    self, {n: a.get("path", "") for n, a in atts.items()}, quarantined
                ),
            )
            actions = self.desk.decide(res)
            with self._lock:
                msg["correspondent"] = res.to_dict()
                msg["bossdesk"] = actions
                msg["handoffs"] = [
                    {
                        "name": ln["name"],
                        "doc_id": ln["doc_id"],
                        "lane": ln["lane"],
                        "reason": ln["reason"],
                        "status": "pending",
                        "pipeline": None,
                    }
                    for ln in res.attachment_lanes
                ]
            self.emit(
                "message.triaged",
                mid,
                {
                    "trust": res.trust,
                    "intent": res.intent,
                    "issue_class": res.issue_class,
                    "prefilter": res.prefilter,
                },
            )
            for sig in res.signals:
                self.emit("signal.emitted", mid, sig)
            for a in actions:
                self.emit("boss.action", mid, a)
            for entry in msg["handoffs"]:
                self._apply_lane(msg, entry, atts.get(entry["name"]))
            for d in res.drafts:
                with self._lock:
                    item = self.outbox.add_draft(
                        message_id=mid,
                        thread_id=msg["thread_id"],
                        draft={
                            "to": d.to,
                            "subject": d.subject,
                            "body": d.body,
                            "intent": d.intent,
                            "evidence": d.evidence,
                        },
                    )
                    msg["outbox_ids"].append(item["id"])
                    if self.autonomy == "sandbox":
                        self.outbox.approve(
                            item["id"], by="boss-desk(autonomy=sandbox)"
                        )
                        for a in msg["bossdesk"]:
                            if a["action"] == "approve_outbound":
                                a["state"] = "done"
            msg["flows_done"].append("correspondent")
        if "pipeline" in flows and "pipeline" not in msg["flows_done"]:
            if "correspondent" not in msg["flows_done"]:
                # pipeline-only baseline: no Correspondent gating, every resolved attachment goes in
                with self._lock:
                    msg["handoffs"] = [
                        {
                            "name": a["name"],
                            "doc_id": a.get("doc_id", ""),
                            "lane": "baseline_direct",
                            "reason": "pipeline-only run: no Correspondent gating",
                            "status": "pending",
                            "pipeline": None,
                        }
                        for a in msg["wire"]["attachments"]
                    ]
            for entry in msg["handoffs"]:
                a = atts.get(entry["name"])
                if (
                    entry["lane"] in {"handoff", "baseline_direct"}
                    and a
                    and a.get("resolved")
                    and entry["status"] in {"pending", "deferred"}
                ):
                    self.emit(
                        "attachment.handoff",
                        mid,
                        {
                            "name": entry["name"],
                            "doc_id": entry["doc_id"],
                            "lane": entry["lane"],
                        },
                    )
                    self._run_pipeline_for(msg, entry, a["path"])
            msg["flows_done"].append("pipeline")

    def _apply_lane(self, msg: dict, entry: dict, att: dict | None) -> None:
        """Apply a handoff lane by recording quarantine, copying a hold, or deferring work."""
        mid, lane = msg["id"], entry["lane"]
        if lane == "quarantine":
            d = self.data_dir / "comms" / "quarantine" / mid
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{entry['name']}.meta.json").write_text(
                json.dumps(
                    {
                        "name": entry["name"],
                        "doc_id": entry["doc_id"],
                        "sha256": (att or {}).get("sha256"),
                        "reason": entry["reason"],
                        "opened": False,
                    }
                ),
                encoding="utf-8",
            )
            entry["status"] = "quarantined"
            self.emit(
                "attachment.quarantined",
                mid,
                {"name": entry["name"], "reason": entry["reason"], "opened": False},
            )
        elif lane == "hold":
            if att and att.get("resolved") and att.get("path"):
                d = self.data_dir / "comms" / "pending" / mid
                d.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(att["path"], d / Path(entry["name"]).name)
            entry["status"] = "held"
            self.emit(
                "attachment.held",
                mid,
                {"name": entry["name"], "reason": entry["reason"]},
            )
        elif lane == "handoff":
            entry["status"] = "pending" if "pipeline" in msg["flows"] else "deferred"
        else:
            entry["status"] = lane

    def release_attachment(self, mid: str, name: str, by: str = "human") -> dict:
        """Human release of a soft-held attachment into the pipeline (never quarantine)."""
        msg = self._get(mid)
        entry = next((h for h in msg["handoffs"] if h["name"] == name), None)
        if entry is None:
            raise KeyError(name)
        if entry["status"] == "quarantined":
            raise PermissionError(
                "quarantined attachments are released by a human outside the sandbox; they are never opened here"
            )
        if entry["status"] != "held":
            raise ValueError(f"attachment is {entry['status']}, not held")
        att = next(a for a in msg["wire"]["attachments"] if a["name"] == name)
        self.emit("attachment.released", mid, {"name": name, "by": by})
        with self._work_lock:
            self.emit(
                "attachment.handoff",
                mid,
                {"name": name, "doc_id": entry["doc_id"], "via": "release"},
            )
            self._run_pipeline_for(msg, entry, att["path"])
        self._save()
        return self.message(mid)

    # ------------------------------------------------------------------ outbox
    def approve_outbound(self, oid: str, by: str = "reviewer") -> dict:
        """Apply outbox approval under the state lock, update Boss actions, and persist."""
        with self._lock:
            item = self.outbox.approve(oid, by)
            msg = self.messages.get(item["message_id"])
            if msg:
                for a in msg["bossdesk"]:
                    if a["action"] == "approve_outbound":
                        a["state"] = "done"
        self._save()
        return item

    def reject_outbound(self, oid: str, by: str = "reviewer") -> dict:
        """Reject an outbox item under the state lock and persist the result."""
        with self._lock:
            item = self.outbox.reject(oid, by)
        self._save()
        return item

    def set_egress_profile(self, profile: str) -> None:
        """Change the outbox profile under the lock, emit its event, and persist state."""
        with self._lock:
            self.outbox.set_profile(profile)
        self.emit("config.changed", "config", {"egress_profile": profile})
        self._save()

    # ------------------------------------------------------------------ views
    def _public(self, msg: dict) -> dict:
        """Copy a message with local paths removed and truth hidden when configured."""
        m = copy.deepcopy(msg)
        if not self.show_expected:
            m.pop("truth", None)
        for a in m["wire"].get("attachments", []):
            a.pop("path", None)
        return m

    def message(self, mid: str) -> dict:
        """Return an independent public view of one stored message."""
        with self._lock:
            return self._public(self._get(mid))

    def list_messages(
        self,
        *,
        state: str | None = None,
        scenario: str | None = None,
        batch: str | None = None,
    ) -> list[dict]:
        """Return filtered public message views with bodies truncated for list display."""
        with self._lock:
            out = [
                self._public(m)
                for m in self.messages.values()
                if (state is None or m["state"] == state)
                and (scenario is None or m["scenario"] == scenario)
                and (batch is None or m["batch_id"] == batch)
            ]
        for m in out:  # keep list payload small
            m["wire"] = {**m["wire"], "body": m["wire"].get("body", "")[:200]}
        return out

    def scenario_messages(self, name: str) -> list[dict]:
        """Copy messages from the most recent batch containing the named scenario."""
        with self._lock:
            msgs = [m for m in self.messages.values() if m["scenario"] == name]
            if not msgs:
                return []
            last = msgs[-1]["batch_id"]
            return [copy.deepcopy(m) for m in msgs if m["batch_id"] == last]

    def evaluation(self, name: str) -> dict | None:
        """Evaluate the latest scenario batch when expectations are visible and data exists."""
        if not self.show_expected:
            return None
        msgs = self.scenario_messages(name)
        if not msgs:
            return None
        with self._lock:
            outbox = {k: copy.deepcopy(v) for k, v in self.outbox.items.items()}
        return compare_scenario(
            self.content.cs.scenarios[name],
            msgs,
            outbox,
            stuck=self.pipeline.stuck_documents(),
            personas=self.content.personas,
        )

    def trace(self, mid: str) -> dict:
        """Assemble a message trace across ingress, both flows, outbox, events, and checks."""
        msg = self.message(mid)
        with self._lock:
            out = [
                copy.deepcopy(self.outbox.items[o])
                for o in msg["outbox_ids"]
                if o in self.outbox.items
            ]
        sender = msg["wire"].get("from", "")
        return {
            "message_id": mid,
            "stand_in": {
                "correspondent": self.agent.name,
                "bossdesk": self.desk.name,
                "pipeline_llm": "offline mock provider",
            },
            "ingress": {
                "scenario": msg["scenario"],
                "kind": msg["kind"],
                "batch_id": msg["batch_id"],
                "sim_ts": msg["sim_ts"],
                "sim_offset_s": msg["sim_offset_s"],
                "admission": msg["admission"],
                "state": msg["state"],
                "notes": msg["notes"],
                "wire": msg["wire"],
                "thread_id": msg["thread_id"],
            },
            "correspondent": msg["correspondent"],
            "bossdesk": msg["bossdesk"],
            "pipeline": [h for h in msg["handoffs"]],
            "egress": {
                "outbox": out,
                "sender_recipient_probe": self.outbox.probe(sender)
                if "@" in sender
                else None,
                "profile": self.outbox.profile,
            },
            "events": self.events_since(0, ref_id=mid),
            "expected": self.evaluation(msg["scenario"])
            if self.show_expected
            else None,
            "error": msg["error"],
        }

    def documents(self) -> list[dict]:
        """Snapshot document summaries with extracted text omitted."""
        with self._lock:
            return [
                {k: v for k, v in d.items() if k != "text"} for d in self.docs.values()
            ]

    def status(self) -> dict:
        """Summarize content, component configuration, message counts, and runtime counters."""
        with self._lock:
            counts: dict[str, int] = {}
            for m in self.messages.values():
                counts[m["state"]] = counts.get(m["state"], 0) + 1
        return {
            "service": "mailroom-sandbox",
            "content": self.content.info(),
            "stand_ins": {
                "correspondent": {
                    "name": self.agent.name,
                    "stand_in": True,
                    "note": "rule-based; the real agent does not exist yet",
                },
                "bossdesk": {"name": self.desk.name, "stand_in": True},
                "pipeline": {
                    "real": True,
                    "llm": "offline mock provider",
                    "data_dir": str(self.pipeline.base),
                },
            },
            "egress": self.outbox.summary(),
            "autonomy": self.autonomy,
            "ingress": self.meter.snapshot(),
            "messages": counts,
            "queue_pending": self._q.unfinished_tasks,
            "network_guard": self.guard.summary()
            if self.guard
            else {"installed": False},
            "show_expected": self.show_expected,
            "mock_llm": dict(self.pipeline.mock.stats),
        }
