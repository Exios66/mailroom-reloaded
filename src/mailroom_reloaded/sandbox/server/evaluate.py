"""Expected-vs-actual for one scenario (ground truth is for inspection only).

The Correspondent never sees any of this: it is computed after the fact from the
scenario's ``expect`` block and the recorded message results. ``ok`` is ``True``
(match), ``False`` (mismatch) or ``None`` (not checkable in this sandbox).
"""

from __future__ import annotations

from typing import Any

__all__ = ["compare_scenario"]

_BENIGN_ROLES = {"real", "personal-address"}


_PRIMARY_NOTE = {
    True: "first email in the scenario",
    False: "the email the scenario's expectations are about (see _primary)",
}
_TRUST_RANK = {"hostile": 0, "suspicious": 1, "unverified": 2, "verified": 3}


def _primary(scenario: dict, emails: list[dict]) -> dict:
    """The email that ``expect.intent`` / ``expect.trust`` describe.

    1. the message named by ``a`` of an expected relation (scenario ``ref``), or the one
       carrying the attachment it names, else
    2. for scenarios that expect a hostile or suspicious sender, the email the
       Correspondent rated least trusted (the scenario asserts the adversarial message;
       if nothing was rated that low, the first email is compared and fails), else
    3. the first email.
    """
    expect = scenario.get("expect", {})
    by_ref = {m["truth"]["ref"]: m for m in emails if (m.get("truth") or {}).get("ref")}
    by_att = {
        a["ref"]: m for m in emails for a in m["wire"]["attachments"] if a.get("ref")
    }
    for er in expect.get("relations", []) or []:
        if er["a"] in by_ref:
            return by_ref[er["a"]]
        if er["a"] in by_att:  # the message that carries the document being related
            return by_att[er["a"]]
    want = (expect.get("trust") or {}).get("sender_level")
    if want in {"hostile", "suspicious"}:
        low = min(emails, key=lambda m: _TRUST_RANK.get(m["correspondent"]["trust"], 9))
        if _TRUST_RANK.get(low["correspondent"]["trust"], 9) <= _TRUST_RANK[want]:
            return low
    return emails[0]


def _chk(key: str, expected: Any, actual: Any, ok: bool | None, note: str = "") -> dict:
    """Package an expected-versus-actual check with a tri-state result and note."""
    return {"key": key, "expected": expected, "actual": actual, "ok": ok, "note": note}


def compare_scenario(
    scenario: dict,
    msgs: list[dict],
    outbox: dict[str, dict],
    *,
    stuck: list[str],
    personas: dict[str, dict],
) -> dict:
    """Compare recorded outcomes with scenario expectations and summarize the verdict."""
    expect = scenario.get("expect", {})
    emails = [m for m in msgs if m["kind"] == "email" and m.get("correspondent")]
    checks: list[dict] = []
    if not emails:
        return {
            "scenario": scenario["name"],
            "verdict": "not_run",
            "checks": [],
            "summary": {"passed": 0, "failed": 0, "unchecked": 0},
        }
    primary_msg = _primary(scenario, emails)
    primary = primary_msg["correspondent"]
    checks.append(
        _chk(
            "intent",
            expect.get("intent"),
            primary["intent"],
            expect.get("intent") == primary["intent"],
            "primary = " + _PRIMARY_NOTE[primary_msg["id"] == emails[0]["id"]],
        )
    )
    if "trust" in expect:
        exp = expect["trust"]["sender_level"]
        checks.append(
            _chk("trust.sender_level", exp, primary["trust"], exp == primary["trust"])
        )

    all_sig = [s for m in emails for s in m["correspondent"]["signals"]]
    for es in expect.get("signals", []):
        hit = any(
            s["kind"] == es["kind"]
            and s["priority"] == es["priority"]
            and (
                "attack_class" not in es or s.get("attack_class") == es["attack_class"]
            )
            for s in all_sig
        )
        checks.append(
            _chk(
                "signal",
                {k: es[k] for k in ("kind", "priority", "attack_class") if k in es},
                [
                    {
                        k: s.get(k)
                        for k in ("kind", "priority", "attack_class")
                        if k in s
                    }
                    for s in all_sig
                ],
                hit,
                "latency (`within`) is not measured: simulated time only"
                if "within" in es
                else "",
            )
        )

    lanes: dict[str, list[str]] = {"quarantine": [], "hold": []}
    for m in emails:
        for ln in m["correspondent"]["attachment_lanes"]:
            if ln["lane"] in lanes:
                lanes[ln["lane"]].append(ln["name"])
    if "quarantine" in expect:
        checks.append(
            _chk(
                "quarantine",
                sorted(expect["quarantine"]),
                sorted(lanes["quarantine"]),
                sorted(expect["quarantine"]) == sorted(lanes["quarantine"]),
            )
        )
    if "soft_hold" in expect:
        checks.append(
            _chk(
                "soft_hold",
                sorted(expect["soft_hold"]),
                sorted(lanes["hold"]),
                sorted(expect["soft_hold"]) == sorted(lanes["hold"]),
            )
        )

    if "outbox" in expect:
        actual = [outbox[o] for m in emails for o in m["outbox_ids"] if o in outbox]
        exp = expect["outbox"]
        ok = len(exp) == len(actual) and all(
            any(
                a["intent"] == e.get("intent")
                and (not e.get("to_sender") or "@" in a["to"])
                for a in actual
            )
            for e in exp
        )
        checks.append(
            _chk(
                "outbox",
                [
                    {
                        "intent": e.get("intent"),
                        "state": e.get("state"),
                        "to_sender": e.get("to_sender"),
                    }
                    for e in exp
                ],
                [
                    {"intent": a["intent"], "state": a["state"], "to": a["to"]}
                    for a in actual
                ],
                ok,
                "expected draft state is the pre-approval state"
                if exp
                else "no reply to the sender",
            )
        )

    if "boss_actions" in expect:
        have: dict[str, str] = {}
        for m in emails:
            for a in m["bossdesk"]:
                have.setdefault(a["action"], a["state"])
        missing = [a for a in expect["boss_actions"] if a not in have]
        pend = [a for a in expect["boss_actions"] if have.get(a) == "pending_human"]
        checks.append(
            _chk(
                "boss_actions",
                expect["boss_actions"],
                have,
                not missing,
                ("awaiting human approval: " + ", ".join(pend))
                if pend
                else (
                    "extra actions are allowed: "
                    + ", ".join(sorted(set(have) - set(expect["boss_actions"])))
                    if set(have) - set(expect["boss_actions"])
                    else ""
                ),
            )
        )

    if expect.get("relations"):
        att_by_ref: dict[str, str] = {}
        msg_by_ref: dict[str, dict] = {}
        for m in msgs:
            if (m.get("truth") or {}).get("ref"):
                msg_by_ref[m["truth"]["ref"]] = m
            for a in m["wire"]["attachments"]:
                if a.get("ref"):
                    att_by_ref.setdefault(a["ref"], a["name"])
        for st in scenario.get("timeline", []):  # pinned files are named by themselves
            if "ingress" in st and st["ingress"].get("ref"):
                ing = st["ingress"]
                att_by_ref.setdefault(ing["ref"], ing.get("as") or ing.get("file"))
        rels_by_msg = {m["id"]: m["correspondent"]["relations"] for m in emails}
        rels = [r for lst in rels_by_msg.values() for r in lst]

        def names(ref: str) -> set[str]:
            if ref in att_by_ref:
                return {att_by_ref[ref]}
            if ref in msg_by_ref:
                return {a["name"] for a in msg_by_ref[ref]["wire"]["attachments"]}
            return {ref}

        for er in expect["relations"]:
            if str(er["a"]).startswith("matter:") or str(er["b"]).startswith("matter:"):
                checks.append(
                    _chk(
                        "relation",
                        er,
                        None,
                        None,
                        "matter-level relations are not modelled",
                    )
                )
                continue
            a_names, b_names = names(er["a"]), names(er["b"])
            a_msg = msg_by_ref.get(er["a"])
            pool = rels_by_msg.get(a_msg["id"], []) if a_msg else rels
            hit = next(
                (
                    r
                    for r in pool
                    if (a_msg is not None or r["a"] in a_names)
                    and r["b"] in b_names
                    and r["kind"] == er["kind"]
                    and r["confidence"] >= er.get("min_conf", 0)
                ),
                None,
            )
            checks.append(
                _chk(
                    "relation",
                    er,
                    [{k: r[k] for k in ("a", "b", "kind", "confidence")} for r in rels],
                    hit is not None,
                )
            )

    if "overblocking" in expect:
        hard = 0
        for m in msgs:
            role = (m.get("truth") or {}).get("role") or personas.get(
                (m.get("truth") or {}).get("persona") or "", {}
            ).get("role")
            if (
                m["kind"] == "email"
                and role in _BENIGN_ROLES
                and m.get("correspondent")
            ):
                hard += sum(
                    1 for a in m["bossdesk"] if a["action"] == "quarantine_attachments"
                )
        exp = expect["overblocking"].get("benign_hard_actions", 0)
        checks.append(
            _chk(
                "overblocking.benign_hard_actions",
                exp,
                hard,
                hard <= exp,
                "hard action = quarantine on a benign sender's message",
            )
        )

    docs = [h["pipeline"] for m in msgs for h in m["handoffs"] if h.get("pipeline")]
    for inv in expect.get("invariants", []):
        if inv == "audit_chain_ok":
            checks.append(
                _chk(
                    inv,
                    True,
                    [d["audit_chain_ok"] for d in docs],
                    all(d["audit_chain_ok"] for d in docs) if docs else None,
                    "" if docs else "no document reached the pipeline",
                )
            )
        elif inv == "no_stuck_docs":
            checks.append(_chk(inv, [], stuck, not stuck))
        elif inv == "fast_path_two_calls":
            fresh = [d for d in docs if not d["reused"]]
            checks.append(
                _chk(
                    inv,
                    "<=2 structured LLM calls per document",
                    [d["structured_llm_calls"] for d in fresh],
                    all(d["structured_llm_calls"] <= 2 for d in fresh)
                    if fresh
                    else None,
                    "" if fresh else "no fresh document run",
                )
            )
        elif inv == "comms_offpath":
            ok = all(
                m["correspondent"]["agent"].get("pipeline_tool_calls", 0) == 0
                for m in emails
            )
            checks.append(
                _chk(
                    inv,
                    "Correspondent uses no pipeline tools",
                    "stand-in has read-only tools only",
                    ok,
                    "by construction",
                )
            )

    passed = sum(1 for c in checks if c["ok"] is True)
    failed = sum(1 for c in checks if c["ok"] is False)
    unchecked = sum(1 for c in checks if c["ok"] is None)
    return {
        "scenario": scenario["name"],
        "title": scenario.get("title"),
        "verdict": "pass" if failed == 0 else "fail",
        "checks": checks,
        "summary": {"passed": passed, "failed": failed, "unchecked": unchecked},
    }
