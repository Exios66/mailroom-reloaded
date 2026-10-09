"""Stand-in Boss Desk: turns a Correspondent result into typed Boss actions.

Actions come from ``protocol/delegation_matrix.csv`` (``boss_action`` and
``autonomy`` columns), adjusted for what the Correspondent actually produced
(for example ``link_documents`` only when a relation was proposed). It never
calls a pipeline node and never dials a phone: ``recommend_callback`` is a task
carrying the registry number.
"""

from __future__ import annotations

import re

from mailroom_reloaded.sandbox.server.correspondent import CorrespondentResult

__all__ = ["StandInBossDesk"]

_TOKEN = re.compile(r"^\s*([a-z_]+)\s*(?:\((.*)\))?\s*$")
_ATTACK_ISSUES = {
    "payment_or_identity_change_attack",
    "possible_prompt_injection",
    "malicious_attachment",
    "impersonation",
    "disclosure_request_bulk",
}


_CATEGORY = {
    "credential_phish": "phishing",
    "payment_fraud": "phishing",
    "impersonation": "phishing",
    "malicious_attachment": "malware",
}
CATEGORIES = ("phishing", "malware", "other")


class StandInBossDesk:
    name = "rule-based-standin-bossdesk/v1"
    stand_in = True

    def __init__(self, delegation: dict[str, dict]) -> None:
        """Store the delegation rules used to derive stand-in Boss Desk actions."""
        self.delegation = delegation

    def decide(self, res: CorrespondentResult) -> list[dict]:
        """Return proposed Boss actions from the delegation matrix and classification.

        Actions include review gates and attachment dispositions; this method
        does not execute them. Missing relations produce ``no_candidate`` links,
        and attachment release and draft approval remain ``pending_human``.
        """
        row = self.delegation.get(res.issue_class, {})
        autonomy = row.get("autonomy", "n/a")
        source = f"delegation_matrix:{res.issue_class}" if row else "stand-in default"
        actions: list[dict] = []

        def add(
            name: str, params: str | None = None, state: str = "done", why: str = ""
        ) -> None:
            """Append an action with policy context unless its name and parameters exist."""
            if any(a["action"] == name and a.get("params") == params for a in actions):
                return
            actions.append(
                {
                    "action": name,
                    "params": params,
                    "state": state,
                    "autonomy": autonomy,
                    "source": source,
                    "why": why,
                }
            )

        kinds = {x["kind"] for x in res.signals if x["state"] != "dismissed"}
        attack = "possible_attack" in kinds
        for tok in re.split(r"\s\+\s|\s*\+\s*(?![^()]*\))", row.get("boss_action", "")):
            m = _TOKEN.match(tok)
            if not m:
                continue
            name, params = m.group(1), m.group(2)
            if name == "link_documents" and not res.relations:
                # the matrix names it for this issue class: considered, nothing to link
                add(name, params, state="no_candidate", why="no related document found")
                continue
            if name == "task_correspondent" and not res.drafts:
                continue
            if attack and name == "dismiss_signal":
                continue  # an attack is escalated, never folded into the digest
            if name == "release_attachments":
                # human-only, after approval: listed as awaiting, never done here
                add(name, params, state="pending_human", why="after human approval")
                continue
            add(name, params)
        if row.get("owner") == "human_reviewer":
            add(
                "request_human_review",
                why="the delegation matrix assigns this class to a human",
            )
        lanes = {ln["lane"] for ln in res.attachment_lanes}
        if "quarantine" in lanes:
            add(
                "quarantine_attachments", why="hard lane comms/quarantine; never opened"
            )
        if "hold" in lanes:
            add("hold_attachments", why="soft hold comms/pending")
        if res.callback is not None:
            add("recommend_callback", why="task for a human; registry number only")
        if any(
            r["kind"] == "duplicates" and r["confidence"] >= 0.95 for r in res.relations
        ):
            add(
                "dismiss_signal",
                why="identical content hash: duplicate folded, benign-by-policy",
            )
        if res.relations and any(
            r["auto_link"] and r["kind"] != "contradicts" for r in res.relations
        ):
            add(
                "link_documents",
                why="relation confidence at or above the auto threshold",
            )
        sig = res.signals[0] if res.signals else None
        if (
            sig
            and sig["state"] != "dismissed"
            and res.issue_class not in _ATTACK_ISSUES
            and not attack
            and sig["kind"] not in {"legal_notice", "privacy_request", "payment_change"}
        ):
            add("ack_signal")
        if attack:
            add("request_human_review", why="attack signal escalates to a human")
        if "urgent" in kinds or res.intent == "legal_notice":
            add("raise_priority", why="deadline or legal window")
        if "urgent" in kinds or "privacy_request" in kinds:
            add("request_human_review", why="deadline or privacy matter needs a human")
        if res.drafts:
            add(
                "approve_outbound",
                state="pending_human",
                why="draft waits for the approval gate",
            )
        return actions

    # ------------------------------------------------------------- mailbox inbox
    def read_forward(self, entry: dict) -> dict | None:
        """Open a review case from a ``hostile_forward`` mailbox entry.

        The Desk reads the mailbox entry only (its payload), never the Correspondent's
        result object or the message store. Returns ``None`` for other entry kinds.
        The case uses the highest signal priority, defaulting to ``high`` when
        there are no signals. Missing required fields raise ``KeyError``; priorities
        outside ``low``, ``normal``, ``high``, and ``critical`` raise ``ValueError``.
        """
        if entry.get("kind") != "hostile_forward":
            return None
        pl = entry["payload"]
        attacks = pl.get("signals", [])
        classes = sorted(pl.get("attack_classes") or ["other"])
        order = ["low", "normal", "high", "critical"]
        top = max(
            (s.get("priority", "normal") for s in attacks),
            key=order.index,
            default="high",
        )
        return {
            "message_id": entry["message_id"],
            "thread_id": entry["thread_id"],
            "forward_entry_id": entry["id"],
            "decision_entry_id": None,
            "state": "pending",
            "attack_classes": classes,
            "priority": top,
            "signals": [dict(x) for x in attacks],
            "attachments": [ln["name"] for ln in pl.get("attachment_lanes", [])],
            "category": _CATEGORY.get(classes[0], "other")
            if len(classes) == 1
            else ("malware" if "malicious_attachment" in classes else "other"),
            "decision": None,
            "reason": None,
            "decided_by": None,
        }

    def unattended_decision(self, case: dict, autonomy: str) -> tuple[str, str] | None:
        """Return a quarantine decision and reason for ``sandbox`` autonomy.

        Other autonomy values return ``None``. The caller applies the decision;
        this method does not change the case or its attachments.
        """
        if autonomy == "sandbox":
            return (
                "quarantine",
                f"unattended default: hostile ({', '.join(case['attack_classes'])}) stays quarantined",
            )
        return None
