"""Correspondent interface and the deterministic, offline STAND-IN implementation.

The real External Correspondence Agent (a CrewAI agent, protocol section 1.1) does
not exist in this repository yet. :class:`StandInCorrespondent` is a clearly
labelled rule-based substitute so the ingress sandbox can run end to end with no
LLM and no network. It implements the same observable contract the protocol
gives the agent (pre-filter, safety screen, trust level, intent, signals,
attachment lanes, relation proposals, drafts) with plain heuristics, and it is
NOT a model of how the real agent reasons. Swap it by registering another
:class:`CorrespondentAgent` in :data:`AGENTS`.

Guarantees mirrored from the protocol:

* input is wire-visible data only (no scenario, persona or label);
* message text is data: no tool call is ever driven by instructions in it;
* quarantined attachments are never opened (``read_attachment_text`` is not
  called for them);
* hostile / injection / auto-reply mail gets no reply;
* callback numbers come from the registry, never from the message;
* drafts only: sending is the outbox's job (approval gate).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from jinja2.sandbox import SandboxedEnvironment

__all__ = [
    "AGENTS",
    "AttachmentView",
    "CorrespondentAgent",
    "CorrespondentResult",
    "CorrespondentTools",
    "Draft",
    "StandInCorrespondent",
    "WireMessage",
    "create_correspondent",
]


@dataclass
class AttachmentView:
    name: str
    size: int = 0
    sha256: str = ""
    doc_id: str = ""
    resolved: bool = True


@dataclass
class WireMessage:
    message_id: str
    thread_id: str
    from_addr: str
    subject: str
    body: str
    auth: dict[str, str]
    attachments: list[AttachmentView]
    received_sim_ts: float = 0.0
    channel: str = "email"


class CorrespondentTools(Protocol):
    """Read-only tool surface (protocol 1.1): no pipeline-mutating tools exist."""

    def registry(self) -> dict[str, dict]: ...
    def lookup_catalog(self) -> list[dict]: ...
    def read_attachment_text(self, att: AttachmentView) -> str: ...


@dataclass
class Draft:
    to: str
    subject: str
    body: str
    intent: str
    in_reply_to: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class CorrespondentResult:
    agent: dict
    prefilter: str | None
    trust: str
    trust_reasons: list[str]
    intent: str
    issue_class: str
    client: dict | None
    signals: list[dict]
    attachment_lanes: list[dict]
    relations: list[dict]
    drafts: list[Draft]
    callback: dict | None
    entities: dict[str, list[str]]
    summary: str
    reasons: list[str]
    llm_calls: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class CorrespondentAgent(Protocol):
    name: str
    stand_in: bool

    def handle(
        self, msg: WireMessage, tools: CorrespondentTools
    ) -> CorrespondentResult: ...


# --------------------------------------------------------------------------- heuristics

_REF = re.compile(r"\b[A-Z]{1,4}-\d{2,4}(?:-\d{2,6})+\b")
_AUTO = re.compile(
    r"(out of office|automatic reply|auto-?reply|autoreply|undeliverable|delivery status notification|mailer-daemon)",
    re.IGNORECASE,
)
_INJECTION = re.compile(
    r"(ignore (all |any |the )?(prior|previous|above) (instructions|messages)|note to (the )?(automated )?(assistant|system|ai)"
    r"|disregard (your|the) (system|previous) (prompt|instructions)|you are now (a|an|the)\b|forward .{0,40}(last|all) \d* ?(processed )?documents)",
    re.IGNORECASE,
)
_PAYMENT = re.compile(
    r"((wire|wiring|routing|remittance|bank|account)\b.{0,50}\b(instruction|detail|number|change|changed|updated|new)\b"
    r"|\b(updated|new|changed)\b.{0,30}\b(wire|wiring|bank|remittance|routing)\b)",
    re.IGNORECASE | re.DOTALL,
)
_CALL_SUPPRESS = re.compile(
    r"(do not call|don't call|cannot take calls|no calls|email only|reply (to this email )?only|by email only)",
    re.IGNORECASE,
)
_CRED = re.compile(
    r"(verify your (account|password|identity)|reset your password|sign in to (view|review)|enter your (password|credentials)|re-?authenticate)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_LEGAL = re.compile(
    r"(subpoena|court order|summons|demand letter|regulator|regulatory inquiry|legal notice|cease and desist|litigation hold)",
    re.IGNORECASE,
)
_PRIVACY = re.compile(
    r"(delete (all )?(of )?my (personal )?data|privacy request|gdpr|ccpa|right to be forgotten|erase my)",
    re.IGNORECASE,
)
_BULK = re.compile(
    r"(send (me )?(every|all) (the )?(claims|documents|files)|every claim for|export (all|everything))",
    re.IGNORECASE,
)
_STATUS = re.compile(
    r"(status (check|update|of|on)|where is|what is the status|update on|still in flight|in flight, archived)",
    re.IGNORECASE,
)
_COMPLAINT = re.compile(
    r"(unacceptable|frustrat|third (time|message)|far too long|taking too long|fed up|ridiculous)",
    re.IGNORECASE,
)
_URGENT = re.compile(
    r"(closing is today|deadline (is )?today|expedite|asap|urgent(ly)? need|by end of day)",
    re.IGNORECASE,
)
_SUPERSEDE = re.compile(
    r"(supersede|supersedes|replaces?|replaced|treat the earlier version as replaced)",
    re.IGNORECASE,
)
_AMEND = re.compile(
    r"(amend|amended|redline|corrected|correction|v\d+ corrects)", re.IGNORECASE
)
_WITHDRAW = re.compile(
    r"(ignore the earlier|withdraw|wrong file|disregard (the )?(earlier|previous) (upload|file))",
    re.IGNORECASE,
)
_SUBMIT = re.compile(r"(attached|enclosed|please find|find attached)", re.IGNORECASE)
_CONFIRM = re.compile(r"(confirm|acknowledg)", re.IGNORECASE)
_VENDOR = re.compile(
    r"(book a (\d+-minute )?demo|final notice|limited[- ]time offer|new vendor|we help (teams|you)|worth a \d+-minute|free trial)",
    re.IGNORECASE,
)
_LOCKOUT = re.compile(
    r"(locked out|lost access|cannot (log|sign) in|writing from my personal)",
    re.IGNORECASE,
)
_INJ_NAME = re.compile(
    r"(?:this is|i am|i'm|my name is)\s+([A-Z][a-z]+)\s+([A-Z][a-z]+)"
)
_RISKY_EXT = {
    ".docm",
    ".xlsm",
    ".pptm",
    ".exe",
    ".js",
    ".scr",
    ".bat",
    ".vbs",
    ".jar",
    ".msi",
    ".iso",
}
_ARCHIVE_EXT = {".zip", ".7z", ".rar"}

_HOMOGLYPH = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "-": "", "_": ""})


def _norm_domain(domain: str) -> str:
    return domain.lower().translate(_HOMOGLYPH).replace("rn", "m").replace("vv", "w")


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _ext(name: str) -> str:
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def _titles(text: str) -> str:
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return re.sub(r"\s*\(.*?\)\s*", " ", first).strip()


_ENV = SandboxedEnvironment(autoescape=False, keep_trailing_newline=True)

_FOOTER = "\n[Draft produced by the offline sandbox stand-in Correspondent; captured, never sent.]\n"
_T_STATUS = _ENV.from_string(
    "Hello,\n\nThank you for your message about {{ refs or 'your request' }}. "
    "{% if facts %}Here is what our processing records show:\n{% for f in facts %}- {{ f }}\n{% endfor %}"
    "{% else %}I do not have any processed documents on file for that reference yet, so I cannot give a status. "
    "I have not assumed anything; I will follow up once records exist.\n{% endif %}"
    "\nBest regards,\nMailroom Correspondent\n"
)
_T_SUBMISSION = _ENV.from_string(
    "Hello,\n\nThank you. We received {{ names }} and logged it for processing"
    "{% if doc_ids %} (reference {{ doc_ids }}){% endif %}.\n"
    "{% for r in relations %}It appears to {{ r.kind }} {{ r.b_name }}; this is a proposed link pending review, not a confirmed change.\n{% endfor %}"
    "\nBest regards,\nMailroom Correspondent\n"
)
_T_HOLD = _ENV.from_string(
    "Hello,\n\nThank you for your note. We received your message. For security, items "
    "that arrive from an address we have not verified are held until we confirm with the contact "
    "we have on file through our usual channel. We are not able to confirm or discuss any matter "
    "details by email in the meantime, and we will follow up as soon as that check is complete.\n\n"
    "Best regards,\nMailroom Correspondent\n"
)
_T_HOLDING = _ENV.from_string(
    "Hello,\n\nThank you for your message, and we are sorry for the delay. We have flagged your "
    "request for review so it gets attention. We will follow up with a concrete update as soon as "
    "we have one.\n\nBest regards,\nMailroom Correspondent\n"
)
_T_ACK = _ENV.from_string(
    "Hello,\n\nWe received your message and have flagged it for priority handling. "
    "We will confirm once we have an update.\n\nBest regards,\nMailroom Correspondent\n"
)


class StandInCorrespondent:
    """Deterministic rule-based Correspondent. STAND-IN, not the real agent."""

    name = "rule-based-standin/v1"
    stand_in = True

    # ---------------------------------------------------------------- client / trust
    def _resolve_client(
        self, msg: WireMessage, registry: dict[str, dict]
    ) -> tuple[dict | None, str | None]:
        addr, dom = msg.from_addr.lower(), _domain(msg.from_addr)
        for cid, c in registry.items():
            if addr in [a.lower() for a in c.get("verified_addresses", [])]:
                return {"client_id": cid, "how": "address"}, cid
        for cid, c in registry.items():
            if dom in [d.lower() for d in c.get("verified_domains", [])]:
                return {"client_id": cid, "how": "domain"}, cid
        for cid, c in registry.items():
            for d in c.get("verified_domains", []):
                if (
                    dom
                    and dom != d.lower()
                    and (
                        _norm_domain(dom) == _norm_domain(d)
                        or (
                            len(dom.split(".")[0]) > 6
                            and _lev(dom.split(".")[0], d.split(".")[0]) <= 1
                            and dom.split(".")[1:] == d.split(".")[1:]
                        )
                    )
                ):
                    return {"client_id": cid, "how": "lookalike_domain", "of": d}, cid
        m = _INJ_NAME.search(msg.body)
        if m:
            key = (m.group(1)[0] + m.group(2)).lower()
            for cid, c in registry.items():
                for a in c.get("verified_addresses", []):
                    if a.split("@")[0].lower() == key:
                        return {
                            "client_id": cid,
                            "how": "name_hint",
                            "name": f"{m.group(1)} {m.group(2)}",
                        }, cid
        return None, None

    def _trust(self, match: dict | None, auth: dict[str, str]) -> tuple[str, list[str]]:
        vals = [auth.get(k, "none") for k in ("spf", "dkim", "dmarc")]
        ok = all(v == "pass" for v in vals)
        bad = any(v in {"fail", "softfail", "permerror"} for v in vals)
        how = match["how"] if match else None
        reasons = [
            f"auth spf/dkim/dmarc = {'/'.join(vals)}",
            f"registry match: {how or 'none'}",
        ]
        if how == "lookalike_domain":
            return ("hostile" if bad else "suspicious"), reasons + [
                f"domain is a lookalike of {match['of']}"
            ]
        if how == "address":
            if ok:
                return "verified", reasons
            return ("suspicious" if bad else "unverified"), reasons
        if how in {"domain", "name_hint"}:
            return ("suspicious" if bad else "unverified"), reasons
        return ("suspicious" if bad else "unverified"), reasons

    # ---------------------------------------------------------------- main
    def handle(
        self, msg: WireMessage, tools: CorrespondentTools
    ) -> CorrespondentResult:
        text = f"{msg.subject}\n{msg.body}"
        reasons: list[str] = []
        registry = tools.registry()
        match, cid = self._resolve_client(msg, registry)
        client = registry.get(cid) if cid else None
        client_view = (
            {
                "client_id": cid,
                "display_name": client.get("display_name"),
                "matched_by": match["how"],
                **(
                    {"matched_name": match["name"]}
                    if match and match.get("name")
                    else {}
                ),
            }
            if client
            else None
        )
        entities = {"refs": sorted(set(_REF.findall(text)))}
        trust, trust_reasons = self._trust(match, msg.auth)
        agent = {"name": self.name, "stand_in": True, "llm_calls": 0}

        def result(**kw: Any) -> CorrespondentResult:
            base = {
                "agent": agent,
                "prefilter": None,
                "trust": trust,
                "trust_reasons": trust_reasons,
                "client": client_view,
                "signals": [],
                "attachment_lanes": [],
                "relations": [],
                "drafts": [],
                "callback": None,
                "entities": entities,
                "reasons": reasons,
                "llm_calls": 0,
            }
            base.update(kw)
            return CorrespondentResult(**base)

        def lanes(default: str, why: str) -> list[dict]:
            out = []
            for a in msg.attachments:
                if not a.resolved:
                    out.append(
                        {
                            "name": a.name,
                            "doc_id": a.doc_id,
                            "lane": "unavailable",
                            "reason": "attachment bytes not in content pack",
                        }
                    )
                    continue
                ext = _ext(a.name)
                if ext in _RISKY_EXT:
                    out.append(
                        {
                            "name": a.name,
                            "doc_id": a.doc_id,
                            "lane": "quarantine",
                            "reason": f"risky extension {ext}",
                        }
                    )
                elif ext in {".png", ".jpg", ".jpeg"} and "qr" in a.name.lower():
                    out.append(
                        {
                            "name": a.name,
                            "doc_id": a.doc_id,
                            "lane": "quarantine",
                            "reason": "QR image attachment",
                        }
                    )
                elif ext in _ARCHIVE_EXT:
                    out.append(
                        {
                            "name": a.name,
                            "doc_id": a.doc_id,
                            "lane": "hold",
                            "reason": "archive (password pending / not opened)",
                        }
                    )
                else:
                    out.append(
                        {
                            "name": a.name,
                            "doc_id": a.doc_id,
                            "lane": default,
                            "reason": why,
                        }
                    )
            return out

        # 1. deterministic pre-filter (no LLM)
        if not msg.body.strip() and not msg.attachments:
            reasons.append("prefilter: empty body, no attachments")
            return result(
                prefilter="empty",
                intent="unrelated",
                issue_class="autoreply_storm",
                signals=[{"kind": "fyi", "priority": "low", "state": "dismissed"}],
                summary="Empty message; dismissed without reply.",
            )
        if _AUTO.search(text):
            reasons.append(
                "prefilter: auto-reply / bounce wording; loop guard: never answered"
            )
            return result(
                prefilter="auto_reply",
                intent="auto_reply",
                issue_class="autoreply_storm",
                signals=[{"kind": "fyi", "priority": "low", "state": "dismissed"}],
                summary="Auto-reply or bounce; logged only.",
            )

        # 2. safety screen
        if _INJECTION.search(text):
            reasons.append(
                "safety: instruction-like text directed at the assistant; zero tool calls driven by it"
            )
            return result(
                trust="hostile" if trust in {"suspicious", "hostile"} else "suspicious",
                intent="possible_prompt_injection",
                issue_class="possible_prompt_injection",
                signals=[
                    {
                        "kind": "possible_attack",
                        "attack_class": "injection",
                        "priority": "critical",
                        "state": "pending",
                    }
                ],
                attachment_lanes=lanes(
                    "quarantine", "attachments of a flagged message"
                ),
                summary="Message contains instruction-like text aimed at an assistant; treated as data, escalated.",
            )
        payment = bool(_PAYMENT.search(text))
        suppress = bool(_CALL_SUPPRESS.search(text))
        if payment:
            reasons.append(
                "safety: payment/identity change language"
                + ("; call suppression" if suppress else "")
            )
            bad_sender = trust in {"hostile", "suspicious"} or (
                suppress and trust != "verified"
            )
            if bad_sender:
                final_trust = "hostile"
                cb = self._callback(
                    client_view,
                    registry,
                    "payment change from an unverified or suspicious origin",
                )
                return result(
                    trust=final_trust,
                    intent="payment_or_identity_change",
                    issue_class="payment_or_identity_change_attack",
                    signals=[
                        {
                            "kind": "possible_attack",
                            "attack_class": "payment_fraud",
                            "priority": "critical",
                            "state": "pending",
                        }
                    ],
                    attachment_lanes=lanes(
                        "quarantine", "payment-fraud indicators; never opened"
                    ),
                    callback=cb,
                    summary="Payment instruction change with lookalike/failed-auth indicators; no reply, callback uses the registry number.",
                )
            cb = self._callback(
                client_view,
                registry,
                "payment change from a verified sender needs callback confirmation",
            )
            return result(
                intent="payment_or_identity_change",
                issue_class="payment_or_identity_change_genuine",
                signals=[
                    {"kind": "payment_change", "priority": "high", "state": "pending"}
                ],
                attachment_lanes=lanes(
                    "hold", "money/identity change held until callback verifies"
                ),
                callback=cb,
                summary="Verified sender announces a payment change; attachments held pending callback.",
            )
        if _CRED.search(text) and _URL.search(text):
            reasons.append("safety: credential request with a link")
            return result(
                trust="hostile" if trust != "verified" else "suspicious",
                intent="spam_or_phishing",
                issue_class="spam_or_phishing",
                signals=[
                    {
                        "kind": "possible_attack",
                        "attack_class": "credential_phish",
                        "priority": "critical",
                        "state": "pending",
                    }
                ],
                attachment_lanes=lanes("quarantine", "credential-phish message"),
                summary="Credential-harvest wording with a link; defanged, escalated, no reply.",
            )
        risky = [
            a
            for a in msg.attachments
            if _ext(a.name) in _RISKY_EXT
            or (_ext(a.name) in {".png", ".jpg", ".jpeg"} and "qr" in a.name.lower())
        ]
        if risky:
            reasons.append("safety: malicious-looking attachment type")
            return result(
                trust="suspicious" if trust == "verified" else trust,
                intent="spam_or_phishing",
                issue_class="malicious_attachment",
                signals=[
                    {
                        "kind": "possible_attack",
                        "attack_class": "malicious_attachment",
                        "priority": "high",
                        "state": "pending",
                    }
                ],
                attachment_lanes=lanes(
                    "quarantine", "malicious-looking attachment type"
                ),
                summary="Attachment type is on the hard-hold list; quarantined unopened.",
            )

        # 3. triage (rule-based; the real agent makes one LLM call here)
        atts = msg.attachments
        has_att = bool(atts)
        if _LEGAL.search(text):
            intent, issue, sig, pri = (
                "legal_notice",
                "legal_notice",
                "legal_notice",
                "critical",
            )
        elif _PRIVACY.search(text):
            intent, issue, sig, pri = (
                "privacy_request",
                "privacy_request",
                "privacy_request",
                "high",
            )
        elif _BULK.search(text):
            intent, issue, sig, pri = (
                "disclosure_request",
                "disclosure_request_bulk",
                "possible_attack",
                "high",
            )
        elif _WITHDRAW.search(text):
            intent, issue, sig, pri = (
                "retraction_or_withdrawal",
                "retraction_or_withdrawal",
                "correction",
                "normal",
            )
        elif _COMPLAINT.search(text):
            intent, issue, sig, pri = "complaint", "complaint", "complaint", "high"
        elif _URGENT.search(text):
            intent, issue, sig, pri = (
                "urgent_deadline",
                "urgent_deadline",
                "urgent",
                "high",
            )
        elif _LOCKOUT.search(text):
            intent, issue, sig, pri = (
                "general_question",
                "general_question",
                "fyi",
                "normal",
            )
        elif has_att and (_AMEND.search(text)):
            intent, issue, sig, pri = (
                "correction_or_amendment",
                "document_submission",
                "doc_relation",
                "normal",
            )
        elif has_att and (_SUPERSEDE.search(text) or _SUBMIT.search(text)):
            intent, issue, sig, pri = (
                "document_submission",
                "document_submission",
                "new_info",
                "normal",
            )
        elif _STATUS.search(text) and not has_att:
            intent, issue, sig, pri = (
                "status_request",
                "status_request",
                "status_request",
                "normal",
            )
        elif _VENDOR.search(text) and match is None and not has_att:
            intent, issue, sig, pri = "unrelated", "spam_or_phishing", "fyi", "low"
        elif has_att:
            intent, issue, sig, pri = (
                "document_submission",
                "document_submission",
                "new_info",
                "normal",
            )
        else:
            intent, issue, sig, pri = (
                "general_question",
                "general_question",
                "fyi",
                "normal",
            )
        reasons.append(f"triage: intent={intent} (keyword rules)")

        if intent == "disclosure_request":
            return result(
                intent=intent,
                issue_class=issue,
                signals=[
                    {
                        "kind": "possible_attack",
                        "attack_class": "exfiltration",
                        "priority": "high",
                        "state": "pending",
                    }
                ],
                summary="Bulk or cross-client disclosure request; refused and flagged.",
            )

        # 4. attachments, relations, callback, drafts
        if not has_att:
            lane_list: list[dict] = []
        elif trust == "verified":
            lane_list = lanes("handoff", "verified sender; shared intake hand-off")
        elif trust == "unverified":
            lane_list = lanes(
                "hold",
                "unverified sender; soft hold pending callback to the registered contact",
            )
        else:
            lane_list = lanes(
                "quarantine" if trust == "hostile" else "hold", f"{trust} sender"
            )
        callback = None
        if trust == "unverified" and has_att and client_view:
            callback = self._callback(
                client_view,
                registry,
                "unverified sender with an attachment; verify by callback",
            )
        relations = (
            self._relations(msg, text, lane_list, tools, trust, reasons)
            if has_att
            else []
        )
        if relations and sig == "new_info":
            sig = "doc_relation"
        if trust == "unverified" and sig == "doc_relation":
            sig = "new_info"
        signals = [
            {
                "kind": sig,
                "priority": pri,
                "state": "dismissed" if intent == "unrelated" else "pending",
            }
        ]
        drafts = self._drafts(msg, intent, trust, lane_list, relations, tools, entities)
        return result(
            intent=intent,
            issue_class=issue,
            signals=signals,
            attachment_lanes=lane_list,
            relations=relations,
            drafts=drafts,
            callback=callback,
            summary=f"{intent} from {trust} sender; {len(drafts)} draft(s), {len(lane_list)} attachment(s).",
        )

    # ---------------------------------------------------------------- pieces
    def _callback(
        self, client_view: dict | None, registry: dict, reason: str
    ) -> dict | None:
        if not client_view:
            return {
                "client": None,
                "contact": None,
                "phone": None,
                "reason": reason + " (no registry client resolved)",
            }
        c = registry[client_view["client_id"]]
        cb = c.get("callback") or {}
        return {
            "client": client_view["client_id"],
            "contact": cb.get("contact"),
            "phone": cb.get("phone"),
            "reason": reason,
            "source": "registry (never a number from the message)",
        }

    def _relations(self, msg, text, lane_list, tools, trust, reasons) -> list[dict]:
        kind = (
            "supersedes"
            if _SUPERSEDE.search(text)
            else "amends"
            if _AMEND.search(text)
            else "withdraws"
            if _WITHDRAW.search(text)
            else None
        )
        catalog = tools.lookup_catalog()
        out: list[dict] = []
        lane_by = {ln["name"]: ln for ln in lane_list}
        for a in msg.attachments:
            ln = lane_by.get(a.name)
            if not ln or ln["lane"] == "quarantine" or not a.resolved:
                continue
            a_text = tools.read_attachment_text(a)
            a_title = _titles(a_text).lower()
            for doc in catalog:
                if doc["doc_id"] == a.doc_id:
                    if doc["status"] in {"archived", "parked"}:
                        out.append(
                            {
                                "a": a.name,
                                "b": doc["filename"],
                                "b_doc_id": doc["doc_id"],
                                "kind": "duplicates",
                                "confidence": 1.0,
                                "evidence": ["identical content hash"],
                                "auto_link": True,
                            }
                        )
                    continue
                d_title = _titles(doc.get("text", "")).lower()
                evid: list[str] = []
                score = 0.0
                if d_title and (d_title in text.lower() or d_title == a_title):
                    score += 0.3
                    evid.append(
                        f"title '{_titles(doc.get('text', ''))}' appears in message/attachment"
                    )
                shared = set(_REF.findall(a_text + " " + text)) & set(
                    _REF.findall(doc.get("text", ""))
                )
                if shared:
                    score += 0.3
                    evid.append("shared reference " + ", ".join(sorted(shared)))
                if kind and score >= 0.3:
                    score += 0.3
                    evid.append(f"message wording implies '{kind}'")
                if trust == "verified" and score >= 0.3:
                    score += 0.1
                    evid.append("sender is verified")
                if score >= 0.5:
                    k = kind or "references"
                    out.append(
                        {
                            "a": a.name,
                            "b": doc["filename"],
                            "b_doc_id": doc["doc_id"],
                            "kind": k,
                            "confidence": round(min(score, 0.95), 2),
                            "evidence": evid,
                            "auto_link": score >= 0.8,
                        }
                    )
        return out

    def _drafts(
        self, msg, intent, trust, lane_list, relations, tools, entities
    ) -> list[Draft]:
        if trust in {"hostile", "suspicious"} or intent in {
            "auto_reply",
            "bounce",
            "unrelated",
            "spam_or_phishing",
            "possible_prompt_injection",
            "legal_notice",
            "disclosure_request",
            "payment_or_identity_change",
        }:
            return []
        reply_subject = (
            msg.subject
            if msg.subject.lower().startswith("re:")
            else f"Re: {msg.subject}"
        )
        base = {
            "to": msg.from_addr,
            "subject": reply_subject,
            "in_reply_to": msg.message_id,
        }
        if intent == "status_request":
            refs = entities.get("refs", [])
            facts, ev = [], []
            for doc in tools.lookup_catalog():
                if refs and any(r in doc.get("text", "") for r in refs):
                    facts.append(
                        f"{doc['filename']}: {doc['status']} (doc_id {doc['doc_id']})"
                    )
                    ev.append(doc["doc_id"])
            body = _T_STATUS.render(refs=", ".join(refs), facts=facts)
            return [Draft(**base, body=body + _FOOTER, intent=intent, evidence=ev)]
        if (
            intent in {"document_submission", "correction_or_amendment"}
            and trust == "verified"
        ):
            if relations or _CONFIRM.search(msg.body):
                names = ", ".join(a.name for a in msg.attachments) or "your message"
                ids = ", ".join(a.doc_id for a in msg.attachments if a.doc_id)
                body = _T_SUBMISSION.render(
                    names=names, doc_ids=ids, relations=relations
                )
                return [
                    Draft(
                        **base,
                        body=body + _FOOTER,
                        intent=intent,
                        evidence=[r["b_doc_id"] for r in relations],
                    )
                ]
            return []
        if intent == "general_question" and trust == "unverified":
            return [
                Draft(**base, body=_T_HOLD.render() + _FOOTER, intent="status_update")
            ]
        if intent == "complaint" and trust == "verified":
            return [Draft(**base, body=_T_HOLDING.render() + _FOOTER, intent=intent)]
        if intent == "urgent_deadline" and trust == "verified":
            return [Draft(**base, body=_T_ACK.render() + _FOOTER, intent=intent)]
        return []


AGENTS: dict[str, type] = {"standin": StandInCorrespondent}


def create_correspondent(kind: str = "standin") -> CorrespondentAgent:
    """Factory seam: register the real agent under another key to replace the stand-in."""
    try:
        return AGENTS[kind]()  # type: ignore[no-any-return]
    except KeyError:
        raise ValueError(
            f"unknown correspondent {kind!r}; available: {sorted(AGENTS)}"
        ) from None
