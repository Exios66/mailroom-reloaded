"""Expected-vs-actual for one scenario (ground truth is for inspection only).

The Correspondent never sees any of this: it is computed after the fact from the
scenario's ``expect`` block and the recorded message results. ``ok`` is ``True``
(match), ``False`` (mismatch) or ``None`` (not checkable in this sandbox).
"""

from __future__ import annotations

from typing import Any

__all__ = ["compare_scenario"]

_BENIGN_ROLES = {"real", "personal-address"}


def _chk(key: str, expected: Any, actual: Any, ok: bool | None, note: str = "") -> dict:
    return {"key": key, "expected": expected, "actual": actual, "ok": ok, "note": note}


def compare_scenario(
    scenario: dict,
    msgs: list[dict],
    outbox: dict[str, dict],
    *,
    stuck: list[str],
    personas: dict[str, dict],
) -> dict:
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
    primary = emails[0]["correspondent"]
    checks.append(
        _chk(
            "intent",
            expect.get("intent"),
            primary["intent"],
            expect.get("intent") == primary["intent"],
            "primary = first email in the scenario",
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
        refs = {}
        for st in scenario.get("timeline", []):
            if "ingress" in st and st["ingress"].get("ref"):
                refs[st["ingress"]["ref"]] = st["ingress"].get("file")
        rels = [r for m in emails for r in m["correspondent"]["relations"]]
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
            b_name = refs.get(er["b"], er["b"])
            hit = next(
                (
                    r
                    for r in rels
                    if r["a"] == er["a"]
                    and r["b"] == b_name
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
            ok = all(m["correspondent"]["llm_calls"] == 0 for m in emails)
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
