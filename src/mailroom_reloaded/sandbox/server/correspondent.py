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

from mailroom_reloaded.sandbox.server.triage import (
    INTENT_ISSUE,
    INTENT_SIGNAL,
    extract_features,
    lexicon_score,
    score_intents,
)

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

    def registry(self) -> dict[str, dict]:
        """Return registered clients and their verified contact information."""
        ...

    def lookup_catalog(self) -> list[dict]:
        """Return document records available to correspondence decisions."""
        ...

    def read_attachment_text(self, att: AttachmentView) -> str:
        """Read attachment text, refusing access to quarantined attachments."""
        ...


@dataclass
class Draft:
    to: str
    subject: str
    body: str
    intent: str
    in_reply_to: str = ""
    evidence: list[str] = field(default_factory=list)


def hostile_forward(msg: WireMessage, res: CorrespondentResult) -> dict | None:
    """Build the Correspondent -> Boss mailbox forward for a possible-attack result.

    Include all ``possible_attack`` signals, even dismissed ones, with the message
    content and attachment lanes. Return ``None`` when no such signals exist.
    This builds the payload without posting it or changing attachment status.
    """
    attacks = [s for s in res.signals if s.get("kind") == "possible_attack"]
    if not attacks:
        return None
    return {
        "kind": "hostile_forward",
        "payload": {
            "message": {
                "message_id": msg.message_id,
                "from": msg.from_addr,
                "subject": msg.subject,
                "body": msg.body,
                "auth": dict(msg.auth),
            },
            "attachment_lanes": [dict(x) for x in res.attachment_lanes],
            "attack_classes": sorted(
                {s.get("attack_class", "other") for s in attacks}
            ),
            "signals": [dict(s) for s in attacks],
            "trust": res.trust,
            "trust_reasons": list(res.trust_reasons),
            "reasoning": list(res.reasons),
            "summary": res.summary,
            "held": "message and attachments are held; no reply sent",
        },
    }


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
    flags: list[str] = field(default_factory=list)
    # messages this agent writes to the boss mailbox: [{"kind": ..., "payload": {...}}]
    to_boss: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize the result and its nested dataclasses as a dictionary."""
        out = asdict(self)
        out.pop("to_boss", None)  # mailbox entries travel on the mailbox, not in traces
        return out


class CorrespondentAgent(Protocol):
    name: str
    stand_in: bool

    def handle(
        self, msg: WireMessage, tools: CorrespondentTools
    ) -> CorrespondentResult:
        """Triage a wire-visible message using the supplied read-only tools."""
        ...


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
    r"|\b(updated|new|changed)\b.{0,30}\b(wire|wiring|bank|remittance|routing)\b"
    r"|\b(new|different|another) (bank )?account\b|\baccount (is|are) being closed\b)",
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
    r"(subpoena|court order|summons|demand letter|regulator|regulatory inquiry|legal notice|cease and desist|litigation hold"
    r"|formal demand|demand for production|administrative action|legal action|\blawsuit\b|\bsue\b|notice of (default|violation))",
    re.IGNORECASE,
)
_PRIVACY = re.compile(
    r"(delete (all )?(of )?my (personal )?data|privacy request|gdpr|ccpa|right to be forgotten|erase my"
    r"|(delet|eras|remov)\w*\b.{0,40}\b(personal|my) (information|data)"
    r"|(personal (information|data)|my (information|data))\b.{0,60}\b(delet|eras|remov)\w*)",
    re.IGNORECASE | re.DOTALL,
)
_BULK = re.compile(
    r"(send (me )?(every|all) (the )?(claims|documents|files)|every claim for|export (all|everything)"
    r"|\b(send|provide|forward|export|release)\b[^.]{0,50}\b(complete|entire|full) (file|record|history)"
    r"|\b(need|want|require)\b[^.]{0,30}\b(all|every)\b[^.]{0,30}\b(claims?|notes|correspondence|records)\b)",
    re.IGNORECASE,
)
_STATUS = re.compile(
    r"(status (check|update|of|on|request)|processing status|where is|where are we|what is the status|update on"
    r"|still in flight|in flight, archived|(come|came|go|went) through|(did|have) you (get|got|receive)\w*)",
    re.IGNORECASE,
)
_COMPLAINT = re.compile(
    r"(unacceptable|frustrat|third (time|message|email|request)|far too long|taking too long|fed up|ridiculous"
    r"|nobody (answers|responds|replies)|no one (answers|responds|replies)|sitting for \d+ days)",
    re.IGNORECASE,
)
_URGENT = re.compile(
    r"(closing is today|deadline (is )?today|expedite|asap|urgent(ly)? need|by end of day"
    r"|\bdeadline\b|court gave me|needs? to be (processed|done|filed) (by|before)|processed before)",
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
_SUBMIT = re.compile(
    r"(attached|enclosed|please find|find attached|\bhere (is|are)\b|i am (sending|forwarding)|forwarding)",
    re.IGNORECASE,
)
_UNRELATED = re.compile(
    r"(webinar|newsletter|unsubscribe|you(?:'|\u2019)re invited|you are invited|calendar invit"
    r"|no (action|reply|response) (is )?(required|needed)|\bself-?test\b|out-of-band notification)",
    re.IGNORECASE,
)
_ANNOUNCE = re.compile(
    r"(is|are) being sent|will (be sent|arrive|follow|reach you)|coming (separately|shortly)|should reach you|tie it to this matter",
    re.IGNORECASE,
)
_NOTE_AMEND = re.compile(
    r"(takes? effect|effective date|parties agreed|agreed (today|that)|note it against|quick note for the file)",
    re.IGNORECASE,
)
_QUESTION = re.compile(
    r"(\?|\b(can|could|would) you (please )?(give|tell|send|explain|summari[sz]e|confirm)\b|\bconfirm what\b)",
    re.IGNORECASE,
)
_IMPERSONATE = re.compile(
    r"\b(this is|i am|it(?:'|\u2019)s)\s+(the\s+)?(pi|principal investigator|boss|ceo|owner|managing partner|director)\b"
    r"|keep this between us|do not loop in|don(?:'|\u2019)t loop in",
    re.IGNORECASE,
)
_CTA = re.compile(
    r"(review and sign|sign here|sign in|click (here|the link|below)|open the link|view (the )?document online)",
    re.IGNORECASE,
)
_FORWARDED = re.compile(
    r"(forwarded message|^\s*>|^fwd:)", re.IGNORECASE | re.MULTILINE
)
_ADVISORY = re.compile(
    r"(is this legit|is this real|scam|phishing|suspicious (email|message))",
    re.IGNORECASE,
)
_PRIVACY_Q = re.compile(
    r"(who can see|(share|disclose|give)\w* my (information|data|documents))",
    re.IGNORECASE,
)
_MISSING = re.compile(
    r"(still (need|missing)|what (else )?(is|are) (missing|needed)|missing (document|page|exhibit))",
    re.IGNORECASE,
)
_EXFIL_IN_TEXT = re.compile(
    r"(forward|send|export|email)\b.{0,60}\b(documents|files|records)",
    re.IGNORECASE | re.DOTALL,
)
_PASSWORD = re.compile(
    r"(password[- ]protected|encrypted|password (to follow|in (a )?separate|will follow))",
    re.IGNORECASE,
)
_LEGAL_THREAT = re.compile(
    r"(lawyer|attorney|legal action|sue\b|will be contacting you)", re.IGNORECASE
)
_REPROCESS = re.compile(r"(re-?run|re-?extract|reprocess)", re.IGNORECASE)
_ANOMALY = re.compile(
    r"((outside|unlike|different from)\b.{0,30}\b(normal|usual|typical)|which matter (this|it) should)",
    re.IGNORECASE | re.DOTALL,
)
_COURT_URGENT = re.compile(
    r"(court|closing is today|deadline (is )?today|filing)", re.IGNORECASE
)
_EN_STOP = frozenset(
    [
        "the",
        "and",
        "to",
        "of",
        "a",
        "in",
        "is",
        "it",
        "you",
        "that",
        "for",
        "with",
        "this",
        "be",
        "are",
        "on",
        "as",
        "at",
        "have",
        "not",
        "we",
        "your",
        "i",
        "from",
        "or",
        "my",
        "can",
        "me",
    ]
)


def _matrix_row(tools: Any, intent: str) -> dict:
    """The delegation-matrix row for an intent (empty when the tool surface has none)."""
    get = getattr(tools, "delegation", None)
    rows = get() if callable(get) else {}
    return rows.get(INTENT_ISSUE.get(intent, ""), {}) or {}


def _matrix_drafts(row: dict) -> bool:
    """Does the matrix put a drafted reply on this issue class?"""
    action = row.get("boss_action", "")
    notes = row.get("notes", "").lower()
    return "task_correspondent" in action or (
        "acknowledgment draft" in notes and "if a human approves" not in notes
    )


def _non_english(text: str) -> bool:
    toks = re.findall(r"[a-zA-Z\u00c0-\u024f']+", text.lower())
    if len(toks) < 8:
        return False
    return sum(t in _EN_STOP for t in toks) / len(toks) < 0.12


_REL_KINDS = [  # relation kind from message wording (protocol section 8.2 vocabulary)
    (
        "withdraws",
        re.compile(
            r"(ignore the earlier|withdraw|wrong file|disregard (the )?(earlier|previous))",
            re.IGNORECASE,
        ),
    ),
    (
        "duplicates",
        re.compile(
            r"(resend|resending|re-?sent|duplicate|nothing has changed)", re.IGNORECASE
        ),
    ),
    (
        "completes",
        re.compile(
            r"(part \d+ of \d+|left out|completes|remaining (pages|exhibits)|missing (page|exhibit|part)|late exhibit|\(late\))",
            re.IGNORECASE,
        ),
    ),
    (
        "supersedes",
        re.compile(
            r"(supersede|replaces?|replaced|this time|updated version|newer version|instead of)",
            re.IGNORECASE,
        ),
    ),
    (
        "amends",
        re.compile(r"(amend|redline|corrected|corrects|correction)", re.IGNORECASE),
    ),
    (
        "answers",
        re.compile(
            r"(you asked for|the document you|as you requested|requested document)",
            re.IGNORECASE,
        ),
    ),
]
_ASK = re.compile(
    r"(\?|\b(can|could|would) you\b|\bplease (send|provide|give|tell|confirm what)\b|\bwhere (is|are)\b"
    r"|\bwho can\b|\bwhat (is|are|does|was)\b)",
    re.IGNORECASE,
)
_CONTEXT = re.compile(
    r"(part \d+ of \d+|missing|\blate\b|left out|counterparty|read together|heads up"
    r"|will follow|being sent|takes? effect|note it against|password)",
    re.IGNORECASE,
)
_NO_CHANGE = re.compile(r"\bno (changes?|updates?) (to|in|on)\b", re.IGNORECASE)
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
    """Normalize common lookalike characters for heuristic domain comparison."""
    return domain.lower().translate(_HOMOGLYPH).replace("rn", "m").replace("vv", "w")


def _lev(a: str, b: str) -> int:
    """Return the Levenshtein edit distance between two strings."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _ext(name: str) -> str:
    """Return the lowercase final filename extension, or an empty string."""
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _domain(addr: str) -> str:
    """Return the lowercase email domain, or an empty string without an at-sign."""
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def _titles(text: str) -> str:
    """Extract the first nonblank text line with parenthesized annotations removed."""
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
    "{% for r in relations %}It appears that {{ r.a }} {{ r.kind }} {{ r.b }}; this is a proposed link pending review, not a confirmed change.\n{% endfor %}"
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
_T_PRIVACY = _ENV.from_string(
    "Hello,\n\nWe received your privacy request. It has been passed to a person who is responsible "
    "for these requests. Nothing is disclosed or deleted until that review is complete, and we "
    "will write to you with the outcome.\n\nBest regards,\nMailroom Correspondent\n"
)
_T_QUESTION = _ENV.from_string(
    "Hello,\n\nThank you for your question. We have noted it and a team member will confirm the "
    "details with you. We do not share document contents or extracted details by email "
    "until they have been checked.\n\nBest regards,\nMailroom Correspondent\n"
)
_T_MISSING = _ENV.from_string(
    "Hello,\n\nThank you for asking. We are checking what is outstanding on our side and a team "
    "member will confirm exactly which documents we still need.\n\nBest regards,\nMailroom Correspondent\n"
)
_T_CLARIFY = _ENV.from_string(
    "Hello,\n\nThank you for sending {{ names }}. It differs from what we normally receive from "
    "you, so could you confirm which matter it belongs to?\n\nBest regards,\nMailroom Correspondent\n"
)
_T_ACK = _ENV.from_string(
    "Hello,\n\nWe received your message and have flagged it for priority handling. "
    "We will confirm once we have an update.\n\nBest regards,\nMailroom Correspondent\n"
)


class StandInCorrespondent:
    """Deterministic rule-based Correspondent. STAND-IN, not the real agent."""

    name = "rule-based-standin/v2"
    stand_in = True

    # ---------------------------------------------------------------- client / trust
    def _resolve_client(
        self, msg: WireMessage, registry: dict[str, dict]
    ) -> tuple[dict | None, str | None]:
        """Match a sender to registry data using addresses, domains, or name hints."""
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
        """Derive sender trust and reasons from the registry match and email auth."""
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
        if how in {"address", "domain"}:
            # a registered address or a registered (primary or alias) domain with fully
            # passing authentication is verified; failing auth is suspicious; missing
            # auth results leave the sender unverified
            if ok:
                return "verified", reasons
            return ("suspicious" if bad else "unverified"), reasons
        if how == "name_hint":
            return ("suspicious" if bad else "unverified"), reasons
        return ("suspicious" if bad else "unverified"), reasons

    def _triage_hook(self, msg, text, feats, tri, tools):
        """Extension point: a subclass may return a replacement Triage (None keeps the rules)."""
        return

    # ---------------------------------------------------------------- main
    def handle(
        self, msg: WireMessage, tools: CorrespondentTools
    ) -> CorrespondentResult:
        """Triage a message; hostile mail is also written to the Boss mailbox at detection."""
        res = self._classify(msg, tools)
        fwd = hostile_forward(msg, res)
        if fwd is not None:
            res.to_boss.append(fwd)
        return res

    def _classify(self, msg: WireMessage, tools: CorrespondentTools) -> CorrespondentResult:
        """Apply offline safety and triage rules to produce signals, lanes, and drafts."""
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
        agent = {
            "name": self.name,
            "stand_in": True,
            "llm_calls": 0,
            "pipeline_tool_calls": 0,
        }

        def result(**kw: Any) -> CorrespondentResult:
            """Build a triage result from shared message context and branch overrides."""
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
            """Assign attachment lanes, overriding the default for missing or risky files."""
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
                elif ext in _ARCHIVE_EXT or _PASSWORD.search(msg.body):
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
                trust="hostile" if trust != "verified" else "suspicious",
                intent="possible_prompt_injection",
                issue_class="possible_prompt_injection",
                signals=[
                    {
                        "kind": "possible_attack",
                        "attack_class": "injection",
                        "priority": "critical",
                        "state": "pending",
                    },
                    *(
                        [
                            {
                                "kind": "possible_attack",
                                "attack_class": "exfiltration",
                                "priority": "high",
                                "state": "pending",
                            }
                        ]
                        if _EXFIL_IN_TEXT.search(text)
                        else []
                    ),
                ],
                attachment_lanes=lanes(
                    "quarantine", "attachments of a flagged message"
                ),
                summary="Message contains instruction-like text aimed at an assistant; treated as data, escalated.",
            )
        # text extraction only: attachments that are not on the hard-hold list are screened
        # for instruction-like text addressed to an assistant (never rendered or executed)
        for a in msg.attachments:
            if (
                a.resolved
                and _ext(a.name) not in _RISKY_EXT | _ARCHIVE_EXT
                and _INJECTION.search(tools.read_attachment_text(a) or "")
            ):
                reasons.append(
                    f"safety: instruction-like text inside attachment {a.name}; zero tool calls driven by it"
                )
                return result(
                    trust="hostile" if trust != "verified" else "suspicious",
                    intent="possible_prompt_injection",
                    issue_class="possible_prompt_injection",
                    signals=[
                        {
                            "kind": "possible_attack",
                            "attack_class": "injection",
                            "priority": "critical",
                            "state": "pending",
                        },
                        {
                            "kind": "possible_attack",
                            "attack_class": "exfiltration",
                            "priority": "high",
                            "state": "pending",
                        },
                    ],
                    attachment_lanes=lanes(
                        "quarantine", "attachment carries injected instructions"
                    ),
                    summary="Attachment text contains instruction-like content aimed at an assistant; quarantined, escalated.",
                )
        if _IMPERSONATE.search(text) and trust in {"suspicious", "hostile"}:
            reasons.append("safety: claims a senior identity from an unverified origin")
            return result(
                trust="hostile",
                intent="spam_or_phishing",
                issue_class="impersonation",
                signals=[
                    {
                        "kind": "possible_attack",
                        "attack_class": "impersonation",
                        "priority": "high",
                        "state": "pending",
                    }
                ],
                attachment_lanes=lanes("quarantine", "impersonation indicators"),
                summary="Sender claims a senior identity from a failed-auth origin; no reply, escalated.",
            )
        payment = lexicon_score(
            text, "payment_or_identity_change"
        ) >= 2.6 and not _NO_CHANGE.search(text)
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
                # a registered sender whose authentication fails stays "suspicious"; a
                # lookalike or unknown origin is hostile
                final_trust = (
                    "suspicious"
                    if match
                    and match["how"] in {"address", "domain"}
                    and trust == "suspicious"
                    else "hostile"
                )
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
                            "priority": "critical" if suppress else "high",
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
        if _URL.search(text) and (
            _CRED.search(text)
            or (_CTA.search(text) and trust in {"suspicious", "hostile"})
        ):
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
                        "priority": "critical",
                        "state": "pending",
                    }
                ],
                attachment_lanes=lanes(
                    "quarantine", "malicious-looking attachment type"
                ),
                summary="Attachment type is on the hard-hold list; quarantined unopened.",
            )

        # 3. triage: scored features (the real agent makes one LLM call here)
        atts = msg.attachments
        has_att = bool(atts)
        feats = extract_features(
            msg,
            trust,
            match,
            {
                d["doc_id"]
                for d in tools.lookup_catalog()
                if d["status"] in {"archived", "parked"}
            },
        )
        tri = score_intents(text, feats, msg.subject)
        tri = self._triage_hook(msg, text, feats, tri, tools) or tri
        intent = tri.intent
        issue = INTENT_ISSUE[intent]
        sig, pri = INTENT_SIGNAL[intent]
        if intent == "urgent_deadline" and _COURT_URGENT.search(text):
            pri = "critical"
        if intent == "general_question":
            if _ADVISORY.search(text):
                sig, pri = "fyi", "low"  # a client asking whether a message is genuine
            elif trust != "verified":
                sig = "fyi"
        if intent == "spam_or_phishing" and trust in {"suspicious", "hostile"}:
            sig, pri = "possible_attack", "high"
        review_flag = tri.needs_review
        reasons.append(
            f"triage: intent={intent} confidence={tri.confidence}"
            + (
                " (abstained: low score, general_question + review)"
                if tri.abstained
                else ""
            )
        )
        legal_attack = False
        if intent == "legal_notice" and trust == "suspicious":
            trust = "hostile"  # a legal demand that fails sender authentication
            legal_attack = (
                True  # also forwarded to the Boss as a possible impersonation
            )

        if intent == "disclosure_request":
            return result(
                trust="hostile" if trust != "verified" else trust,
                intent=intent,
                issue_class=issue,
                signals=[
                    {
                        "kind": "possible_attack",
                        "attack_class": "exfiltration",
                        "priority": "critical" if trust != "verified" else "high",
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
        flags: list[str] = []
        if sig == "fyi" and intent == "general_question":
            if _ADVISORY.search(text):
                pri = (
                    "low"  # a client asking whether a message is genuine: advisory only
                )
            elif trust == "verified":
                sig = "new_info"  # a verified client conveying information
        signals = [
            {
                "kind": sig,
                "priority": pri,
                "state": "dismissed" if intent == "unrelated" else "pending",
            }
        ]

        if legal_attack:
            signals.append(
                {
                    "kind": "possible_attack",
                    "attack_class": "impersonation",
                    "priority": "critical",
                    "state": "pending",
                }
            )

        def extra(kind: str, priority: str) -> None:
            if all(x["kind"] != kind for x in signals):
                signals.append({"kind": kind, "priority": priority, "state": "pending"})

        if intent != "unrelated" and trust == "verified":
            if intent == "complaint" and re.search(
                r"(third|nobody|no one|sitting for|again)", text, re.IGNORECASE
            ):
                extra("urgent", "high")  # an aged, repeated request is escalated
            if relations:
                extra(
                    "doc_relation", "normal"
                )  # the attachment relates to a known document
            if _PRIVACY_Q.search(text):
                extra("privacy_request", "high")
            if (has_att or _FORWARDED.search(msg.body)) and sig != "new_info":
                extra("new_info", "normal")
            if _MISSING.search(text):
                extra("missing_doc", "normal")
        if review_flag or feats.non_english and intent == "general_question":
            flags.append("needs_review")
        if intent == "complaint" and _LEGAL_THREAT.search(text):
            extra("urgent", "normal")  # a legal threat is escalated, not just answered
            flags.append("needs_review")
        if _REPROCESS.search(text):
            flags.append(
                "needs_review"
            )  # reprocessing is a human decision (matrix: reprocessing_request)
        if _ANOMALY.search(text) and has_att:
            flags.append("annotate")
        if intent == "correction_or_amendment":
            flags.append("annotate")
        drafts = self._drafts(msg, intent, trust, lane_list, relations, tools, entities)
        return result(
            intent=intent,
            issue_class=issue,
            signals=signals,
            attachment_lanes=lane_list,
            relations=relations,
            drafts=drafts,
            callback=callback,
            flags=flags,
            summary=f"{intent} from {trust} sender; {len(drafts)} draft(s), {len(lane_list)} attachment(s).",
        )

    # ---------------------------------------------------------------- pieces
    def reply_after_release(
        self, msg: WireMessage, tools: CorrespondentTools
    ) -> list[Draft]:
        """Draft the reply for a message the Boss judged legitimate (drafts only)."""
        subject = (
            msg.subject
            if msg.subject.lower().startswith("re:")
            else f"Re: {msg.subject}"
        )
        base = {"to": msg.from_addr, "subject": subject, "in_reply_to": msg.message_id}
        if msg.attachments:
            names = ", ".join(a.name for a in msg.attachments)
            ids = ", ".join(a.doc_id for a in msg.attachments if a.doc_id)
            body = _T_SUBMISSION.render(names=names, doc_ids=ids, relations=[])
            return [Draft(**base, body=body + _FOOTER, intent="document_submission")]
        return [
            Draft(
                **base, body=_T_QUESTION.render() + _FOOTER, intent="general_question"
            )
        ]

    def _callback(
        self, client_view: dict | None, registry: dict, reason: str
    ) -> dict | None:
        """Build a callback task using registry contact details when a client is known."""
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
        """Propose catalog relations from hashes, text, references, and sender trust."""
        kind = next(
            (
                k
                for k, rx in _REL_KINDS
                if rx.search(text) and not (k == "withdraws" and msg.attachments)
            ),
            None,
        )  # a message that brings a new document replaces, it does not withdraw
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
                    for rk in [k, "references"] if k != "references" else [k]:
                        # a stronger relation (completes, supersedes, ...) also references the document
                        out.append(
                            {
                                "a": a.name,
                                "b": doc["filename"],
                                "b_doc_id": doc["doc_id"],
                                "kind": rk,
                                "confidence": round(min(score, 0.95), 2),
                                "evidence": evid,
                                "auto_link": score >= 0.8,
                            }
                        )
        return out

    def _drafts(
        self, msg, intent, trust, lane_list, relations, tools, entities
    ) -> list[Draft]:
        """Render permitted reply drafts from intent, trust, and available catalog facts."""
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
        asked = bool(_ASK.search(msg.body))
        text = f"{msg.subject}\n{msg.body}"
        row = _matrix_row(tools, intent)
        quarantined = any(ln["lane"] == "quarantine" for ln in lane_list)
        if intent == "status_request":
            if not asked or (row and not _matrix_drafts(row)):
                return []  # the sender is reporting status, or the matrix drafts no reply
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
        if intent in {"document_submission", "correction_or_amendment"} and (
            trust == "verified" and not quarantined
        ):
            names = ", ".join(a.name for a in msg.attachments) or "your message"
            if _ANOMALY.search(msg.body) and msg.attachments:
                return [
                    Draft(
                        **base,
                        body=_T_CLARIFY.render(names=names) + _FOOTER,
                        intent="clarifying_question",
                    )
                ]
            if not (
                relations
                or _CONFIRM.search(msg.body)
                or _CONTEXT.search(text)
                or not msg.body.strip()
            ):
                return []  # a bare transmittal needs no acknowledgement draft
            ids = ", ".join(a.doc_id for a in msg.attachments if a.doc_id)
            draft_relations = {}
            for relation in relations:
                key = (relation["a"], relation["b_doc_id"])
                if (
                    key not in draft_relations
                    or draft_relations[key]["kind"] == "references"
                ):
                    draft_relations[key] = relation
            body = _T_SUBMISSION.render(
                names=names, doc_ids=ids, relations=draft_relations.values()
            )
            return [
                Draft(
                    **base,
                    body=body + _FOOTER,
                    intent=intent,
                    evidence=[r["b_doc_id"] for r in relations],
                )
            ]
        if intent == "general_question" and trust == "unverified":
            return [
                Draft(**base, body=_T_HOLD.render() + _FOOTER, intent="status_update")
            ]
        fallback = {
            "status_request",
            "complaint",
            "general_question",
            "privacy_request",
            "missing_document_followup",
        }
        allowed = _matrix_drafts(row) if row else intent in fallback
        if trust == "verified" and allowed:
            if intent == "general_question" and (
                not asked
                or _ADVISORY.search(msg.body)
                or _FORWARDED.search(msg.body)
                or _MISSING.search(msg.body)
                or _non_english(msg.body)
            ):
                return []
            tpl = {
                "privacy_request": _T_PRIVACY,
                "complaint": _T_HOLDING,
                "missing_document_followup": _T_MISSING,
            }.get(intent, _T_QUESTION)
            return [Draft(**base, body=tpl.render() + _FOOTER, intent=intent)]
        return []


AGENTS: dict[str, type] = {"standin": StandInCorrespondent}


def create_correspondent(kind: str = "standin", **options: Any) -> CorrespondentAgent:
    """Factory seam: register the real agent under another key to replace the stand-in."""
    if kind == "llm":
        from mailroom_reloaded.sandbox.server.llm_correspondent import LLMCorrespondent

        return LLMCorrespondent(**options)
    try:
        return AGENTS[kind](**options)  # type: ignore[no-any-return]
    except KeyError:
        raise ValueError(
            f"unknown correspondent {kind!r}; available: {sorted(AGENTS)}"
        ) from None
