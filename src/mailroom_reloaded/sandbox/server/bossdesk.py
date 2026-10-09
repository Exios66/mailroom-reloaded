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

        for tok in re.split(r"\s\+\s|\s*\+\s*(?![^()]*\))", row.get("boss_action", "")):
            m = _TOKEN.match(tok)
            if not m:
                continue
            name, params = m.group(1), m.group(2)
            if name == "link_documents" and not res.relations:
                continue
            if name == "task_correspondent" and not res.drafts:
                continue
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
            and sig["kind"] not in {"legal_notice", "privacy_request", "payment_change"}
        ):
            add("ack_signal")
        if res.drafts:
            add(
                "approve_outbound",
                state="pending_human",
                why="draft waits for the approval gate",
            )
        return actions
