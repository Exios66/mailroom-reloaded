"""Scored, feature-based intent triage for the stand-in Correspondent (deterministic, offline).

Structural features come first because they transfer across wordings: authentication
results, registry match class, attachment type and structure, thread shape, language and
length. Lexical features come from small intent lexicons written from the policy sources
(``protocol/delegation_matrix.csv`` examples and notes, the reply templates, the intent and
signal vocabulary in ``schemas/``), not from scenario text. Every intent gets a weighted
score; the result carries a confidence and an explicit abstain path: when nothing scores
high enough the intent falls back to ``general_question`` with ``needs_review`` set rather
than a confident wrong label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "ABSTAIN_BELOW",
    "INTENT_ISSUE",
    "INTENT_SIGNAL",
    "Features",
    "Triage",
    "extract_features",
    "lexicon_score",
    "score_intents",
]

ABSTAIN_BELOW = 2.0  # best score under this: abstain
REVIEW_BELOW_CONF = (
    0.4  # best share of the total score under this: keep the label, flag review
)

I = re.IGNORECASE


def _rx(p: str, flags: int = I) -> re.Pattern[str]:
    """Compile a lexicon pattern, using case-insensitive matching by default."""
    return re.compile(p, flags)


# intent -> (delegation_matrix issue_class)
INTENT_ISSUE = {
    "status_request": "status_request",
    "missing_document_followup": "missing_document_followup",
    "document_submission": "document_submission",
    "correction_or_amendment": "document_submission",
    "duplicate_submission": "duplicate_submission",
    "complaint": "complaint",
    "urgent_deadline": "urgent_deadline",
    "general_question": "general_question",
    "unrelated": "spam_or_phishing",
    "auto_reply": "autoreply_storm",
    "bounce": "autoreply_storm",
    "spam_or_phishing": "spam_or_phishing",
    "possible_prompt_injection": "possible_prompt_injection",
    "legal_notice": "legal_notice",
    "privacy_request": "privacy_request",
    "payment_or_identity_change": "payment_or_identity_change_genuine",
    "conflicting_instructions": "conflicting_instructions",
    "retraction_or_withdrawal": "retraction_or_withdrawal",
    "disclosure_request": "disclosure_request_bulk",
}
# intent -> (signal kind, priority); kinds and priorities from protocol sections 2, 4 and 5
INTENT_SIGNAL = {
    "status_request": ("status_request", "normal"),
    "missing_document_followup": ("missing_doc", "normal"),
    "document_submission": ("new_info", "normal"),
    "correction_or_amendment": ("correction", "normal"),
    "duplicate_submission": ("doc_relation", "normal"),
    "complaint": ("complaint", "high"),
    "urgent_deadline": ("urgent", "high"),
    "general_question": ("new_info", "normal"),
    "unrelated": ("fyi", "low"),
    "auto_reply": ("fyi", "low"),
    "bounce": ("fyi", "low"),
    "spam_or_phishing": ("fyi", "low"),
    "possible_prompt_injection": ("possible_attack", "critical"),
    "legal_notice": ("legal_notice", "critical"),
    "privacy_request": ("privacy_request", "high"),
    "payment_or_identity_change": ("payment_change", "high"),
    "conflicting_instructions": ("urgent", "high"),
    "retraction_or_withdrawal": ("correction", "normal"),
    "disclosure_request": ("possible_attack", "critical"),
}

# intent -> [(pattern, weight)] ; each pattern counts once
LEXICON: dict[str, list[tuple[re.Pattern[str], float]]] = {
    "status_request": [
        (_rx(r"\bstatus\b"), 1.6),
        (_rx(r"\bwhere (is|are|'s)\b"), 2.2),
        (_rx(r"\bupdate on\b|\bany (news|update|progress)\b"), 1.6),
        (_rx(r"\bin flight\b|\barchived\b|\bparked\b"), 1.4),
        (
            _rx(
                r"\b(come|came|go|went) through\b|\bdid (you|part|it)\b.{0,30}\b(arrive|come|get|receive)"
            ),
            2.0,
        ),
        (_rx(r"\bhave you (received|got)\b"), 2.0),
        (_rx(r"\bwhen (will|can|should)\b"), 0.8),
    ],
    "missing_document_followup": [
        (
            _rx(
                r"\b(still )?(need|missing|require)\b.{0,40}\bfrom me\b|\bwhat (exactly )?(else )?(do you|are you|is)\b.{0,30}\b(need|missing)"
            ),
            3.0,
        ),
        (_rx(r"\bstill (need|missing)\b"), 2.2),
        (_rx(r"\bwhich (document|page|exhibit)\b"), 1.5),
    ],
    "document_submission": [
        (
            _rx(
                r"\battached\b|\benclosed\b|\bplease find\b|\bfind attached\b|\bhere (is|are)\b"
            ),
            1.6,
        ),
        (
            _rx(r"\bi am (sending|forwarding)\b|\bforwarding\b|\bsending (you|over)\b"),
            1.4,
        ),
        (_rx(r"\b(signed|executed|execution version|clean copy|final)\b"), 0.8),
        (_rx(r"\bfor (your|the) (files?|records?)\b"), 0.8),
        (
            _rx(
                r"\b(is|are) being sent\b|\bwill (be sent|arrive|follow|reach you)\b|\bcoming (separately|shortly)\b|\bshould reach you\b|\btie it to\b"
            ),
            2.4,
        ),
        (_rx(r"\bconfirm receipt\b"), 0.8),
        (_rx(r"\bpart \d+ of \d+\b"), 1.0),
    ],
    "correction_or_amendment": [
        (
            _rx(
                r"\bcorrect(ion|ed|s)\b|\bamended\b|\bredline\b|\brevis(ed|ion)\b|\bupdated version\b"
            ),
            1.6,
        ),
        (_rx(r"\bamendment\b|\bv\d+\b"), 0.6),
        (
            _rx(
                r"\b(is|was|are) (wrong|incorrect)\b|\bnot .{0,25}\b(like|as) (your|the)\b|\bplease correct\b"
            ),
            2.4,
        ),
        (
            _rx(
                r"\btakes? effect\b|\bparties agreed\b|\bnote it against\b|\bquick note for the file\b"
            ),
            2.6,
        ),
        (_rx(r"\beffective date\b"), 1.2),
        (_rx(r"\bre-?run\b|\bre-?extract|\breprocess"), 1.4),
    ],
    "duplicate_submission": [
        (
            _rx(
                r"\bresend(ing)?\b|\bre-?sent\b|\bduplicate\b|\bsame (file|document|attachment)\b"
            ),
            2.4,
        ),
        (_rx(r"\bin case\b.{0,50}\b(did not|didn't|not) (come|arrive|get)"), 2.0),
        (_rx(r"\b(again|asked (last|before)|already (sent|asked))\b"), 1.4),
        (_rx(r"\bnothing has changed\b"), 1.4),
    ],
    "complaint": [
        (
            _rx(
                r"\bunacceptable\b|\bfrustrat|\bangry\b|\bridiculous\b|\bfed up\b|\bfar too long\b|\btaking too long\b"
            ),
            2.6,
        ),
        (
            _rx(
                r"\b(nobody|no one) (answers|responds|replies)\b|\bwhat is going on\b|\bstill waiting\b"
            ),
            2.2,
        ),
        (_rx(r"\b(third|fourth|second) (time|email|message|request|follow)"), 2.0),
        (
            _rx(
                r"\bsitting for \d+ days\b|\b\d+ days\b.{0,30}\b(waiting|no answer|no response)"
            ),
            1.6,
        ),
        (
            _rx(
                r"\blawyer\b|\battorney\b|\bescalate\b|\bwill be contacting you\b|\bcomplain"
            ),
            2.0,
        ),
        (
            _rx(
                r"\b(not|n't) heard back\b|\bno (response|reply|answer)\b|\bhaven't heard\b|\bhave not heard\b"
            ),
            1.5,
        ),
    ],
    "urgent_deadline": [
        (
            _rx(
                r"\burgent(ly)?\b|\basap\b|\bimmediately\b|\bexpedite\b|\bprioriti[sz]e\b"
            ),
            1.6,
        ),
        (
            _rx(
                r"\bdeadline\b|\bcloses? at\b|\bclosing is\b|\bby (end of day|noon|tomorrow|friday)\b|\bbefore then\b"
            ),
            2.0,
        ),
        (
            _rx(
                r"\bcourt (gave|date|ordered)\b|\buntil (january|february|march|april|may|june|july|august|september|october|november|december)\b"
            ),
            2.0,
        ),
        (_rx(r"\b\d{1,2} ?(am|pm|a\.m\.|p\.m\.)\b|\btoday\b"), 0.8),
    ],
    "general_question": [
        (_rx(r"\?"), 0.9),
        (_rx(r"\b(can|could|would) you\b"), 0.7),
        (
            _rx(
                r"\bwho can\b|\bwhat (is|are|does|was)\b|\bhow (do|does|long|many)\b|\bwhen does\b"
            ),
            1.3,
        ),
        (
            _rx(
                r"\bsummary\b|\bsummari[sz]e\b|\bexplain\b|\bextracted\b|\bplain-english\b"
            ),
            1.4,
        ),
        (_rx(r"\bconfirm what\b|\bplease confirm\b"), 1.2),
        (_rx(r"\bprocessing hours\b|\bopening hours\b"), 1.6),
        (
            _rx(
                r"\blocked out\b|\blost access\b|\bcannot (log|sign) in\b|\bwriting from my personal\b"
            ),
            3.0,
        ),
        (_rx(r"\bis this (legit|real|genuine)\b|\bscam\b"), 2.2),
    ],
    "unrelated": [
        (
            _rx(
                r"\bwebinar\b|\bnewsletter\b|\bunsubscribe\b|\bcalendar invit|\boffsite\b|\bteam (lunch|outing)\b"
            ),
            2.6,
        ),
        (_rx(r"\byou(?:'|’)re invited\b|\byou are invited\b|\binvitation\b"), 2.0),
        (
            _rx(
                r"\bnothing (needed|required)\b|\bno (action|reply|response) (is )?(required|needed)\b|\bfor your information\b"
            ),
            2.2,
        ),
        (_rx(r"\bself-?test\b|\broutine message\b|\bout-of-band notification\b"), 3.0),
        (
            _rx(
                r"\bbook a (\d+-minute )?demo\b|\bfree trial\b|\blimited[- ]time\b|\bfinal notice\b|\bwe help (teams|you)\b|\bworth a \d+-minute\b|\bnew vendor\b"
            ),
            3.0,
        ),
    ],
    "auto_reply": [
        (
            _rx(
                r"\bout of office\b|\bautomatic reply\b|\bauto-?reply\b|\bautoreply\b|\bnot monitored\b|\bautomated response\b|\baway until\b"
            ),
            4.0,
        ),
    ],
    "bounce": [
        (
            _rx(
                r"\bundeliverable\b|\bdelivery status notification\b|\bmailer-daemon\b|\bcould not be delivered\b"
            ),
            4.0,
        ),
    ],
    "legal_notice": [
        (
            _rx(
                r"\bsubpoena\b|\bcourt order\b|\bsummons\b|\bcease and desist\b|\blitigation hold\b|\bhereby ordered\b"
            ),
            3.0,
        ),
        (
            _rx(
                r"\bregulator|\bregulatory\b|\bbureau of\b|\bformal demand\b|\bdemand (letter|for production)\b|\badministrative action\b"
            ),
            2.6,
        ),
        (
            _rx(
                r"\bpursuant to\b|\bpreserve all records\b|\bnotice of (default|violation)\b|\bsanctions\b|\blegal action\b"
            ),
            1.8,
        ),
    ],
    "privacy_request": [
        (
            _rx(
                r"\b(delet|eras|remov)\w*\b.{0,40}\b(personal|my) (information|data)|\b(personal (information|data)|my (information|data))\b.{0,60}\b(delet|eras|remov)\w*",
                I | re.DOTALL,
            ),
            3.2,
        ),
        (_rx(r"\bright to be forgotten\b|\bgdpr\b|\bccpa\b|\bprivacy request\b"), 3.0),
        (_rx(r"\beveryone you have shared\b|\bwho has (access|seen)\b"), 1.6),
    ],
    "payment_or_identity_change": [
        (
            _rx(
                r"\b(wire|wiring|routing|remittance|bank|account|disbursement)\b.{0,50}\b(instruction|detail|number|change|changed|updated|new|closed)\b",
                I | re.DOTALL,
            ),
            2.6,
        ),
        (
            _rx(
                r"\b(updated|new|changed|replacement)\b.{0,30}\b(wire|wiring|bank|remittance|routing|account|disbursement)\b",
                I | re.DOTALL,
            ),
            2.6,
        ),
        (
            _rx(
                r"\b(new|different|another|replacement) (bank )?account\b|\baccount (ending|number)\b"
            ),
            1.8,
        ),
        (_rx(r"\bupdate (the )?(vendor|payment|banking|remittance|disbursement)"), 2.2),
        (
            _rx(
                r"\bdo not call\b|\bdon't call\b|\bemail only\b|\bcannot take calls\b|\breply (to this email )?only\b"
            ),
            1.6,
        ),
    ],
    "conflicting_instructions": [
        (
            _rx(
                r"\binstead of\b|\bcontrary to\b|\bconflict(ing|s)?\b|\bincompatible\b|\bdo not wait\b"
            ),
            2.0,
        ),
        (_rx(r"\b(hold|release|stop|proceed)\b.{0,40}\b(until|today|now)\b"), 0.8),
    ],
    "retraction_or_withdrawal": [
        (
            _rx(
                r"\bignore the (earlier|previous)\b|\bwithdraw|\bwrong file\b|\bdisregard (the )?(earlier|previous)\b|\bsent in error\b|\bplease discard\b|\bretract"
            ),
            3.4,
        ),
    ],
    "disclosure_request": [
        (
            _rx(
                r"\b(send|provide|forward|export|release)\b(?:(?!status)[^.]){0,50}\b(every|all|complete|entire|full)\b[^.]{0,30}\b(claims?|documents?|files?|records?|history|correspondence)\b"
            ),
            2.6,
        ),
        (
            _rx(
                r"\b(need|want|require)\b[^.]{0,30}\b(all|every)\b[^.]{0,30}\b(claims?|notes|correspondence|records)\b"
            ),
            2.6,
        ),
        (_rx(r"\bother client\b|\banother client\b|\bwhat did .{0,30} submit\b"), 3.0),
        (_rx(r"\bsigned release\b|\bpolicyholder\b"), 0.8),
    ],
}
LEXICON["spam_or_phishing"] = [
    (
        _rx(
            r"\bverify your (account|password|identity)\b|\breset your password\b|\benter your (password|credentials)\b|\bre-?authenticate\b"
        ),
        3.0,
    ),
    (
        _rx(
            r"\breview and sign\b|\bsign here\b|\bclick (here|the link|below)\b|\bopen the link\b"
        ),
        2.0,
    ),
]

_NEGATOR = _rx(
    r"\bnothing (is )?urgent\b|\bnot urgent\b|\bno rush\b|\bat your convenience\b|\bout of habit\b|\bjust a routine\b|\broutine item\b"
)
_FORWARDED = _rx(r"forwarded message|^\s*>|^fwd:", I | re.MULTILINE)
_URL = _rx(r"https?://\S+|www\.\S+")
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


@dataclass
class Features:
    words: int
    question_marks: int
    has_url: bool
    n_attachments: int
    forwarded: bool
    reply_prefix: bool
    non_english: bool
    caps_ratio: float
    auth_all_pass: bool
    auth_bad: bool
    match_how: str | None
    trust: str
    has_resolved_attachment: bool
    duplicate_of_archived: bool


def _non_english(text: str) -> bool:
    """Flag text with at least eight tokens and under 12% English stop words.

    This is a language heuristic; shorter text always returns ``False``.
    """
    toks = re.findall(r"[a-zA-ZÀ-ɏ']+", text.lower())
    if len(toks) < 8:
        return False
    return sum(t in _EN_STOP for t in toks) / len(toks) < 0.12


def extract_features(
    msg, trust: str, match: dict | None, catalog_ids: set[str]
) -> Features:
    """Derive triage features from wire data and previously resolved trust.

    ``match`` is the registry match, and ``catalog_ids`` contains archived or
    parked document IDs used to flag duplicate attachments. Attachment bytes
    are not read.
    """
    body = msg.body or ""
    letters = [c for c in msg.subject + body if c.isalpha()]
    vals = [msg.auth.get(k, "none") for k in ("spf", "dkim", "dmarc")]
    return Features(
        words=len(body.split()),
        question_marks=body.count("?") + body.count("¿"),
        has_url=bool(_URL.search(body)),
        n_attachments=len(msg.attachments),
        forwarded=bool(_FORWARDED.search(body)),
        reply_prefix=msg.subject.strip().lower().startswith(("re:", "fwd:", "fw:")),
        non_english=_non_english(body),
        caps_ratio=(sum(c.isupper() for c in letters) / len(letters))
        if letters
        else 0.0,
        auth_all_pass=all(v == "pass" for v in vals),
        auth_bad=any(v in {"fail", "softfail", "permerror"} for v in vals),
        match_how=match["how"] if match else None,
        trust=trust,
        has_resolved_attachment=any(a.resolved for a in msg.attachments),
        duplicate_of_archived=any(
            a.doc_id and a.doc_id in catalog_ids for a in msg.attachments
        ),
    )


@dataclass
class Triage:
    intent: str
    confidence: float
    scores: dict[str, float]
    abstained: bool = False
    needs_review: bool = False
    evidence: list[str] = field(default_factory=list)


def score_intents(text: str, f: Features, subject: str = "") -> Triage:
    """Return deterministic intent scores, confidence, evidence, and review flags.

    ``text`` includes subject and body; matching ``subject`` cues get a 50%
    weight bonus. Confidence is the best score's share of all scores, rounded
    to three decimals. A best score below 2.0 abstains to ``general_question``
    with review; otherwise confidence below 0.4 flags review. This scoring
    step does not replace the Correspondent's safety screen.
    """
    scores: dict[str, float] = {k: 0.0 for k in LEXICON}
    for k in ("legal_notice", "payment_or_identity_change"):
        scores.setdefault(k, 0.0)
    scores.setdefault("possible_prompt_injection", 0.0)
    evidence: list[str] = []
    for intent, terms in LEXICON.items():
        for rx, w in terms:
            if rx.search(text):
                bonus = (
                    0.5 * w if subject and rx.search(subject) else 0.0
                )  # a subject hit counts 1.5x
                scores[intent] += w + bonus
                evidence.append(f"{intent}+{w + bonus}:{rx.pattern[:28]}")
    # --- structural evidence
    if f.n_attachments:
        scores["document_submission"] += 2.0
        scores["general_question"] -= 0.5
    if f.duplicate_of_archived:
        scores["duplicate_submission"] += 3.0
    if f.question_marks and not f.n_attachments:
        scores["status_request"] += 0.3
    if f.caps_ratio > 0.5 and f.words > 3:
        scores["complaint"] += 0.8
    if f.has_url and f.trust in {"suspicious", "hostile", "unverified"}:
        scores["spam_or_phishing"] += 1.5
    if f.words < 3 and not f.n_attachments:
        scores["general_question"] += 0.5
    if f.forwarded:
        scores["general_question"] += 0.5
    if f.n_attachments:
        # "disregard the earlier version" with a new file attached is a replacement, not a retraction
        scores["retraction_or_withdrawal"] -= 2.5
    if f.trust == "verified":
        scores["disclosure_request"] *= (
            0.5  # a registered sender asking about its own matter
        )
    # negated urgency / complaint ("nothing urgent", "no rush", "routine"): the cue is a mention, not a claim
    if _NEGATOR.search(text):
        scores["urgent_deadline"] -= 2.5
        scores["complaint"] -= 1.5
        scores["status_request"] += 0.8
    # the attachment itself is shared evidence: a message whose wording is about something
    # else (status, complaint, urgency, correction) is not additionally a bare submission
    strong_other = max(
        scores[k]
        for k in (
            "status_request",
            "complaint",
            "urgent_deadline",
            "correction_or_amendment",
        )
    )
    if (
        f.n_attachments
        and strong_other >= 2.0
        and not LEXICON_HIT(text, "document_submission")
    ):
        scores["document_submission"] -= 1.8
    if (
        f.n_attachments
        and strong_other >= 2.0
        and scores["correction_or_amendment"] >= 2.0
    ):
        scores["document_submission"] -= 2.0
    # an attachment with a question and no hand-over wording and nothing stronger is a question about it
    if (
        f.n_attachments
        and f.question_marks
        and strong_other < 2.0
        and not LEXICON_HIT(text, "document_submission")
    ):
        scores["general_question"] += 1.5
    scores = {k: max(0.0, round(v, 3)) for k, v in scores.items()}
    order = sorted(
        scores, key=lambda k: (-scores[k], _TIE.index(k) if k in _TIE else 99)
    )
    best = order[0]
    total = sum(scores.values()) or 1.0
    conf = round(scores[best] / total, 3)
    if scores[best] < ABSTAIN_BELOW:
        return Triage("general_question", conf, scores, True, True, evidence)
    return Triage(best, conf, scores, False, conf < REVIEW_BELOW_CONF, evidence)


def lexicon_score(text: str, intent: str) -> float:
    """Sum of the lexicon weights of one intent that match ``text`` (no structure)."""
    return sum(w for rx, w in LEXICON[intent] if rx.search(text))


def LEXICON_HIT(text: str, intent: str) -> bool:
    """Return whether either of an intent's first two lexicon patterns matches.

    Raise ``KeyError`` for an intent absent from ``LEXICON``.
    """
    return any(rx.search(text) for rx, _ in LEXICON[intent][:2])


# deterministic tie-break: the more specific / more consequential intent wins
_TIE = [
    "legal_notice",
    "privacy_request",
    "disclosure_request",
    "payment_or_identity_change",
    "retraction_or_withdrawal",
    "complaint",
    "urgent_deadline",
    "conflicting_instructions",
    "auto_reply",
    "bounce",
    "unrelated",
    "duplicate_submission",
    "correction_or_amendment",
    "missing_document_followup",
    "status_request",
    "document_submission",
    "spam_or_phishing",
    "general_question",
]
