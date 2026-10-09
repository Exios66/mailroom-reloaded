"""Virtual outbox / egress sink: captures outbound mail, never transmits it.

Every approved draft is checked against the recipient policy
(``email/recipient_policy.yaml``) and the send caps and doom-loop guards in
``email/send_schedule.yaml`` before being *captured*. There is no SMTP/HTTP code
here at all. Two profiles are simulated offline:

* ``closed``: only ``*.sandbox.invalid`` recipients (policy ``closed`` block).
* ``egress``: a ``*.sandbox.invalid`` recipient additionally needs an overlay
  route record (here: the registry's verified addresses); a virtual address with
  no route fails closed. No real egress ever happens in this sandbox.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mailroom_reloaded.sandbox.server.content import PolicyBundle

__all__ = ["VirtualOutbox"]

PROFILES = ("closed", "egress")


class VirtualOutbox:
    def __init__(
        self,
        policy: PolicyBundle,
        routes: dict[str, str],
        data_dir: Path,
        emit: Callable[[str, str, dict], None],
        clock: Callable[[], float],
        profile: str = "closed",
    ) -> None:
        """Initialize virtual drafts, recipient routes, and policy-based capture guards."""
        self.policy = policy
        self.routes = {k.lower(): v for k, v in routes.items()}
        self.data_dir = Path(data_dir)
        self.emit = emit
        self.clock = clock
        self.profile = profile
        self.items: dict[str, dict] = {}
        self._seq = 0
        allowed = " ".join(
            str(x)
            for x in policy.recipient.get("closed", {}).get("allowed_recipients", [])
        )
        self._domains = [d[1:] for d in re.findall(r"\*(\.[\w.-]+)", allowed)] or [
            "sandbox.invalid"
        ]

    # ------------------------------------------------------------------ state
    def set_profile(self, profile: str) -> None:
        """Select a supported egress profile, raising ValueError for unknown names."""
        if profile not in PROFILES:
            raise ValueError(f"profile must be one of {PROFILES}")
        self.profile = profile

    def dump(self) -> dict:
        """Return the outbox items, sequence counter, and profile for persistence."""
        return {"items": self.items, "seq": self._seq, "profile": self.profile}

    def load(self, state: dict) -> None:
        """Restore items and sequence state, retaining the profile when unspecified."""
        self.items = state.get("items", {})
        self._seq = state.get("seq", 0)
        self.profile = state.get("profile", self.profile)

    # ------------------------------------------------------------------ policy
    def check_recipient(self, addr: str, profile: str | None = None) -> dict:
        """Evaluate an address against the chosen profile and explain the decision."""
        profile = profile or self.profile
        a = addr.strip().lower()
        dom = a.rsplit("@", 1)[-1] if "@" in a else ""
        in_domain = any(dom.endswith("." + d) for d in self._domains)
        if not in_domain:
            return {
                "allowed": False,
                "profile": profile,
                "rule": "recipient_policy.closed",
                "reason": f"{dom or addr!r} is outside *.{self._domains[0]}; fail closed",
            }
        if profile == "closed":
            return {
                "allowed": True,
                "profile": profile,
                "rule": "recipient_policy.closed",
                "reason": f"*.{self._domains[0]} recipient",
            }
        route = self.routes.get(a)
        if route is None:
            return {
                "allowed": False,
                "profile": profile,
                "rule": "recipient_policy.egress",
                "reason": "virtual address has no overlay route record; fails closed (S6)",
            }
        return {
            "allowed": True,
            "profile": profile,
            "rule": "recipient_policy.egress",
            "reason": f"overlay route to {route}",
        }

    def probe(self, addr: str) -> dict:
        """Return recipient-policy decisions for both supported profiles."""
        return {p: self.check_recipient(addr, p) for p in PROFILES}

    # ------------------------------------------------------------------ drafts
    def _next_id(self) -> str:
        """Advance the sequence counter and return a unique outbox identifier."""
        self._seq += 1
        return f"out_{self._seq:04d}"

    def add_draft(
        self,
        *,
        message_id: str,
        thread_id: str,
        draft: dict,
        source: str = "correspondent",
    ) -> dict:
        """Store a draft with content hashes and recipient probes, then emit its event."""
        oid = self._next_id()
        item = {
            "id": oid,
            "message_id": message_id,
            "thread_id": thread_id,
            "source": source,
            "to": draft["to"],
            "subject": draft["subject"],
            "body": draft["body"],
            "intent": draft.get("intent", ""),
            "evidence": draft.get("evidence", []),
            "state": "draft",
            "created_ts": self.clock(),
            "history": [],
            "outbound_message_id": f"obm_{oid}",
            "captured": False,
            "dry_run": True,
        }
        item["content_hash"] = hashlib.sha256(
            (item["subject"] + "\n" + item["body"]).encode()
        ).hexdigest()
        item["idempotency_key"] = hashlib.sha256(
            (item["outbound_message_id"] + item["content_hash"]).encode()
        ).hexdigest()
        item["recipient_probe"] = self.probe(item["to"])
        self.items[oid] = item
        self.emit(
            "draft.created",
            message_id,
            {"outbox_id": oid, "to": item["to"], "intent": item["intent"]},
        )
        return item

    def reject(self, oid: str, by: str = "reviewer") -> dict:
        """Mark a nonterminal draft rejected and record the reviewer and event."""
        item = self.items[oid]
        if item["state"] in {"captured", "rejected"}:
            return item
        item["state"] = "rejected"
        item["history"].append({"state": "rejected", "by": by, "ts": self.clock()})
        self.emit("draft.rejected", item["message_id"], {"outbox_id": oid})
        return item

    def _kill_switch(self) -> bool:
        """Check the environment flag and local file that freeze outbound capture."""
        return (
            os.environ.get("MAILROOM_SEND_KILL_SWITCH") == "1"
            or (self.data_dir / "comms" / "KILL_SWITCH").exists()
        )

    def approve(self, oid: str, by: str = "reviewer") -> dict:
        """Capture an approved draft only after recipient, cap, and duplicate checks."""
        item = self.items[oid]
        if (
            item["state"] == "captured"
        ):  # replay of an already-sent outbound id: dropped, audited
            self.emit(
                "send.duplicate_dropped",
                item["message_id"],
                {"outbox_id": oid, "idempotency_key": item["idempotency_key"]},
            )
            return item
        if item["state"] in {"rejected", "blocked"}:
            return item
        item["history"].append({"state": "approved", "by": by, "ts": self.clock()})
        self.emit("draft.approved", item["message_id"], {"outbox_id": oid, "by": by})
        caps = self.policy.send_schedule.get("caps", {})
        dd = self.policy.send_schedule.get("doom_loop_prevention", {})
        now = self.clock()
        captured = [
            i
            for i in self.items.values()
            if i["state"] == "captured" and i["id"] != oid
        ]
        block: str | None = None
        if self._kill_switch():
            block = "kill switch engaged; outbox frozen"
        elif any(i["idempotency_key"] == item["idempotency_key"] for i in captured):
            self.emit("send.duplicate_dropped", item["message_id"], {"outbox_id": oid})
            item["state"] = "blocked"
            item["block"] = {
                "rule": "send_schedule.idempotency",
                "reason": "duplicate send dropped (same key inside 24h window)",
            }
            return self._finish_block(item)
        else:
            rc = self.check_recipient(item["to"])
            if not rc["allowed"]:
                block = rc["reason"]
                item["block"] = {
                    "rule": rc["rule"],
                    "reason": block,
                    "profile": rc["profile"],
                }
            elif len(item["body"].encode()) > int(
                caps.get("max_bytes_per_send", 1048576)
            ):
                block = "message exceeds max_bytes_per_send"
            elif sum(
                1
                for i in captured
                if now - i.get("captured_ts", i["created_ts"]) < 3600
            ) >= int(caps.get("max_sends_per_hour_total", 90)):
                block = "max_sends_per_hour_total reached"
            elif sum(1 for i in captured if i["thread_id"] == item["thread_id"]) >= int(
                caps.get("max_sends_per_thread_per_day", 2)
            ):
                block = "max_sends_per_thread_per_day reached"
            elif sum(
                1
                for i in self.items.values()
                if i["thread_id"] == item["thread_id"] and i["state"] == "captured"
            ) >= int(dd.get("max_reply_depth_per_thread", 6)):
                block = "reply-depth cap: further replies need Boss Desk approval"
        if block:
            item["state"] = "blocked"
            item.setdefault("block", {"rule": "send_schedule", "reason": block})
            return self._finish_block(item)
        item["state"] = "captured"
        item["captured"] = True
        item["captured_ts"] = now
        item["history"].append({"state": "captured", "ts": now})
        self.emit(
            "send.captured",
            item["message_id"],
            {"outbox_id": oid, "to": item["to"], "transmitted": False},
        )
        return item

    def _finish_block(self, item: dict) -> dict:
        """Record the blocking reason in draft history and emit a send-blocked event."""
        item["history"].append(
            {"state": "blocked", "reason": item["block"]["reason"], "ts": self.clock()}
        )
        self.emit(
            "send.blocked",
            item["message_id"],
            {"outbox_id": item["id"], **item["block"]},
        )
        return item

    def attempt(
        self, *, to: str, subject: str, body: str, thread_id: str = "thr_manual"
    ) -> dict:
        """A person (or test) tries to send mail: runs the same gate, then captures or blocks."""
        item = self.add_draft(
            message_id="manual",
            thread_id=thread_id,
            draft={"to": to, "subject": subject, "body": body, "intent": "manual"},
            source="manual",
        )
        return self.approve(item["id"], by="manual")

    def summary(self) -> dict[str, Any]:
        """Count items by state and report the profile and zero real transmissions."""
        counts: dict[str, int] = {}
        for i in self.items.values():
            counts[i["state"]] = counts.get(i["state"], 0) + 1
        return {
            "profile": self.profile,
            "counts": counts,
            "total": len(self.items),
            "transmitted": 0,
        }
