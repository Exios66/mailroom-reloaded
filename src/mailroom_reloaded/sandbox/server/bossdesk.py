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
        self.delegation = delegation

    def decide(self, res: CorrespondentResult) -> list[dict]:
        row = self.delegation.get(res.issue_class, {})
        autonomy = row.get("autonomy", "n/a")
        source = f"delegation_matrix:{res.issue_class}" if row else "stand-in default"
        actions: list[dict] = []

        def add(
            name: str, params: str | None = None, state: str = "done", why: str = ""
        ) -> None:
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
                continue
            if name == "task_correspondent" and not res.drafts:
                continue
            if attack and name == "dismiss_signal":
                continue  # an attack is escalated, never folded into the digest
            if name == "release_attachments":
                continue  # human-only, after approval
            add(name, params)
        lanes = {ln["lane"] for ln in res.attachment_lanes}
        if "quarantine" in lanes:
            add(
                "quarantine_attachments", why="hard lane comms/quarantine; never opened"
            )
        if "hold" in lanes:
            add("hold_attachments", why="soft hold comms/pending")
        if res.callback is not None:
            add("recommend_callback", why="task for a human; registry number only")
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
        if "needs_review" in res.flags:
            add("request_human_review", why="low-confidence classification")
        if "annotate" in res.flags:
            add("annotate_document", why="note recorded against the document")
        if res.drafts:
            add(
                "approve_outbound",
                state="pending_human",
                why="draft waits for the approval gate",
            )
        return actions

    # ------------------------------------------------------------- signal inbox
    def consume_signals(
        self, message_id: str, signals: list[dict], attachments: list[str]
    ) -> dict | None:
        """Open a review case from ``possible_attack`` signals on the signal channel.

        The Desk looks at the signals only (kind, attack_class, priority), never at the
        message text or the Correspondent's other output. Returns ``None`` when the
        message carried no pending attack signal.
        """
        attacks = [
            s
            for s in signals
            if s.get("kind") == "possible_attack" and s.get("state") != "dismissed"
        ]
        if not attacks:
            return None
        classes = sorted({s.get("attack_class", "other") for s in attacks})
        order = ["low", "normal", "high", "critical"]
        top = max((s["priority"] for s in attacks), key=order.index)
        return {
            "message_id": message_id,
            "state": "pending",
            "attack_classes": classes,
            "priority": top,
            "signals": [dict(s) for s in attacks],
            "attachments": list(attachments),
            "category": _CATEGORY.get(classes[0], "other")
            if len(classes) == 1
            else ("malware" if "malicious_attachment" in classes else "other"),
            "decision": None,
            "reason": None,
            "decided_by": None,
        }

    def unattended_decision(self, case: dict, autonomy: str) -> tuple[str, str] | None:
        """Default for runs with nobody at the desk: ``human`` leaves it pending,
        ``sandbox`` keeps the hostile message quarantined."""
        if autonomy == "sandbox":
            return (
                "quarantine",
                f"unattended default: hostile ({', '.join(case['attack_classes'])}) stays quarantined",
            )
        return None
