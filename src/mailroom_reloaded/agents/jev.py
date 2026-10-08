"""Jev (TypeSafe System One) learned route gate (issue #8).

Jev is an OPT-IN probabilistic decision model. When enabled and calibrated,
``agents.gate.load_gate`` prefers a :class:`JevGate` over the band/learned gate;
when disabled the pipeline behaviour is unchanged.

The client posts a single envelope to the configured endpoint::

    {"model": <cfg.model>, "state": <state>, "questions": {name: question}}

Each question is built by :func:`choice`, :func:`noul` or :func:`score`, which
return ``(name, {"type", "instructions", "criteria"})``. ``noul`` normalizes a
missing ``criteria`` to an empty mapping. The response is parsed from an
``answers`` mapping (keyed by question name) or an ``answers`` list of objects
carrying a ``name``/``question``/``id`` field:

* choice: ``{"type": "choice", "choice": str, "probabilities": {...},
  "confidence": float}`` — when ``confidence`` is absent it defaults to the
  probability of the chosen option (else the max probability);
* noul: ``{"type": "noul", "noul": float}``;
* score: ``{"type": "score", "score": float, "legend": {...}}``.

The HTTP transport sits behind the small :class:`Transport` protocol so tests
can inject a fake with zero network. Only HTTP 429 and 5xx responses are
retried (with exponential backoff, honoring ``Retry-After``); any other non-2xx
raises :class:`JevError` immediately.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from mailroom_reloaded.agents.gate import (
    Action,
    BandGate,
    GateDecision,
    GateFeatures,
    RouteGate,
)
from mailroom_reloaded.eval.jev_calibration import JevCalibration, load_jev_calibration
from mailroom_reloaded.settings import (
    JevConfig,
    Taxonomy,
    get_settings,
    jev_config,
    load_taxonomy,
)

__all__ = [
    "JevAnswer",
    "JevClient",
    "JevError",
    "JevGate",
    "Transport",
    "choice",
    "load_jev_gate",
    "noul",
    "score",
]

# Probability at which the noul ("escalate to a human?") answer maps to review.
_NOUL_ESCALATE = 0.5


class JevError(RuntimeError):
    """Raised when Jev returns a non-2xx response after retries are exhausted."""


@dataclass(frozen=True)
class JevAnswer:
    """One parsed Jev answer (choice, noul or score shape)."""

    type: str
    choice: str | None = None
    probabilities: dict[str, float] | None = None
    confidence: float | None = None
    score: float | None = None
    noul: float | None = None
    legend: dict[str, str] | None = None
    raw: dict | None = None


def choice(
    name: str, instructions: str, criteria: Mapping[str, str]
) -> tuple[str, dict[str, Any]]:
    """Build a ``choice`` question: criteria maps each option to a description."""
    return name, {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def noul(
    name: str, instructions: str, criteria: Mapping[str, str] | None = None
) -> tuple[str, dict[str, Any]]:
    """Build a ``noul`` question (missing criteria normalizes to an empty mapping)."""
    return name, {
        "type": "noul",
        "instructions": instructions,
        "criteria": dict(criteria or {}),
    }


def score(
    name: str, instructions: str, criteria: Sequence[str]
) -> tuple[str, dict[str, Any]]:
    """Build a ``score`` question: criteria is the ordered list of score labels."""
    return name, {"type": "score", "instructions": instructions, "criteria": list(criteria)}


class Transport(Protocol):
    """Minimal HTTP transport seam; the default wraps ``httpx.post``."""

    def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        """POST ``json`` to ``url`` and return the raw response."""
        ...


class _HttpxTransport:
    """Default :class:`Transport` using a module-level ``httpx.post``."""

    def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        return httpx.post(url, json=json, headers=headers, timeout=timeout)


def _infer_type(data: dict) -> str:
    """Infer an answer type when the server omits ``type``."""
    if data.get("score") is not None:
        return "score"
    if data.get("noul") is not None:
        return "noul"
    return "choice"


def _as_answer(data: dict) -> JevAnswer:
    """Normalize one answer object into a :class:`JevAnswer`."""
    type_ = str(data.get("type") or _infer_type(data))
    choice_value = data.get("choice")
    probabilities = data.get("probabilities")
    if probabilities is not None:
        probabilities = {str(k): float(v) for k, v in dict(probabilities).items()}

    confidence = data.get("confidence")
    if confidence is None and probabilities:
        if choice_value is not None and str(choice_value) in probabilities:
            confidence = probabilities[str(choice_value)]
        else:
            confidence = max(probabilities.values())

    score_value = data.get("score")
    noul_value = data.get("noul")
    legend = data.get("legend")
    return JevAnswer(
        type=type_,
        choice=str(choice_value) if choice_value is not None else None,
        probabilities=probabilities,
        confidence=float(confidence) if confidence is not None else None,
        score=float(score_value) if score_value is not None else None,
        noul=float(noul_value) if noul_value is not None else None,
        legend={str(k): str(v) for k, v in dict(legend).items()} if legend else None,
        raw=dict(data),
    )


def _parse_answers(body: dict) -> dict[str, JevAnswer]:
    """Parse the ``answers`` mapping or list from a response envelope."""
    answers = body.get("answers", body)
    out: dict[str, JevAnswer] = {}
    if isinstance(answers, dict):
        for name, value in answers.items():
            if isinstance(value, dict):
                out[str(name)] = _as_answer(value)
    elif isinstance(answers, list):
        for item in answers:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("question") or item.get("id")
            if name is not None:
                out[str(name)] = _as_answer(item)
    return out


def _retry_after(response: httpx.Response) -> float | None:
    """Parse a non-negative ``Retry-After`` seconds value, if present."""
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if raw in (None, ""):
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


class JevClient:
    """POST decision requests to Jev and parse the answers envelope."""

    def __init__(
        self, cfg: JevConfig | None = None, transport: Transport | None = None
    ) -> None:
        """Bind to ``cfg`` (resolved from env/taxonomy) and the transport seam."""
        self.cfg = cfg or jev_config()
        self.transport: Transport = transport or _HttpxTransport()
        self._sleep: Callable[[float], None] = time.sleep

    def ask(
        self, state: str | list | dict, questions: Mapping[str, dict]
    ) -> dict[str, JevAnswer]:
        """Ask Jev one or more questions; return answers keyed by question name."""
        payload = {"model": self.cfg.model, "state": state, "questions": dict(questions)}
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        response = self._post_with_retry(payload, headers)
        try:
            body = response.json()
        except ValueError as exc:
            raise JevError("Jev returned a non-JSON response") from exc
        if not isinstance(body, dict):
            raise JevError("Jev returned an unexpected response body")
        return _parse_answers(body)

    def _post_with_retry(self, payload: dict, headers: dict[str, str]) -> httpx.Response:
        """POST once, retrying 429/5xx with backoff; raise ``JevError`` otherwise."""
        attempt = 0
        while True:
            response = self.transport.post(
                self.cfg.base_url, json=payload, headers=headers, timeout=self.cfg.timeout_s
            )
            status = response.status_code
            if 200 <= status < 300:
                return response
            if (status == 429 or 500 <= status < 600) and attempt < self.cfg.max_retries:
                delay = 0.5 * (2**attempt)
                retry_after = _retry_after(response)
                if retry_after is not None:
                    delay = max(delay, retry_after)
                self._sleep(delay)
                attempt += 1
                continue
            raise JevError(f"Jev request failed with HTTP {status}")


def _logit(p: float) -> float:
    """Log-odds of ``p`` after clamping to ``[1e-6, 1 - 1e-6]``."""
    p = min(max(float(p), 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    """Standard logistic function."""
    return 1.0 / (1.0 + math.exp(-x))


def _jev_state(f: GateFeatures) -> dict[str, Any]:
    """Serialize gate features as the Jev ``state`` payload."""
    return {
        "stage": f.stage,
        "doc_type": f.doc_type,
        "confidence": f.confidence,
        "attempts": f.attempts,
        "bert_confidence": f.bert_confidence,
        "bert_margin": f.bert_margin,
        "bert_window_agreement": f.bert_window_agreement,
        "schema_valid": f.schema_valid,
        "field_coverage": f.field_coverage,
        "length_capped": f.length_capped,
    }


def _jev_questions() -> dict[str, dict]:
    """The route ``choice`` question plus the noul escalation question."""
    return dict(
        [
            choice(
                "route",
                "Choose the next routing action for this document.",
                {
                    "proceed": "Accept the current result and continue.",
                    "retry": "Re-run the current stage.",
                    "verify": "Run the judge/verify step.",
                    "boss": "Escalate to the boss review step.",
                    "human_review": "Send to a human reviewer.",
                },
            ),
            noul("escalate", "Should this document be escalated to a human reviewer?"),
        ]
    )


class JevGate:
    """Jev-backed :class:`RouteGate` (mirrors ``LearnedGate``'s guard).

    Jev only overrides the band decision inside the medium confidence band and
    never overrides a ``rule`` decision (``re_sort``, length cap, invalid
    schema). Its chosen action is accepted when the Jev confidence (temperature
    scaled by the calibration when present) is at least the accept threshold and
    the noul answer does not call for escalation; otherwise the gate maps to
    ``human_review``.
    """

    def __init__(
        self,
        band: BandGate,
        client: JevClient,
        calibration: JevCalibration | None = None,
    ) -> None:
        """Bind to ``band``, a ``client`` and an optional ``calibration``."""
        self.band = band
        self.client = client
        self.calibration = calibration

    def _confidence(self, route: JevAnswer) -> float | None:
        """Temperature-scale the Jev confidence when a calibration is bound."""
        value = route.confidence
        if value is None:
            return None
        if self.calibration is not None and self.calibration.temperature > 0:
            return _sigmoid(_logit(value) / self.calibration.temperature)
        return float(value)

    def decide(self, f: GateFeatures) -> GateDecision:
        """Ask Jev inside the medium band; map to a ``GateDecision``."""
        base = self.band.decide(f)
        if base.source == "rule":
            return base
        t = self.band.thresholds(f.doc_type)
        upper = t.high if f.stage == "classify" else t.judge_band_high
        if not (t.low <= f.confidence < upper):
            return base

        answers = self.client.ask(_jev_state(f), _jev_questions())
        route = answers.get("route")
        escalate = answers.get("escalate")
        if route is None or route.choice is None:
            return GateDecision("human_review", "jev: no route answer", "jev")

        accepted = (
            float(self.calibration.accept_threshold)
            if self.calibration is not None
            else float(self.client.cfg.accept_threshold)
        )
        confidence = self._confidence(route)
        if confidence is None or confidence < accepted:
            return GateDecision(
                "human_review",
                f"jev confidence {confidence} < accept {accepted}",
                "jev",
            )
        if escalate is not None and escalate.noul is not None and escalate.noul >= _NOUL_ESCALATE:
            return GateDecision(
                "human_review", f"jev noul {escalate.noul:.3f} escalate", "jev"
            )
        if route.choice not in ("proceed", "retry", "verify", "boss", "human_review"):
            return GateDecision("human_review", f"jev unknown action {route.choice!r}", "jev")
        action: Action = route.choice  # type: ignore[assignment]
        return GateDecision(action, f"jev choice {route.choice} p={confidence:.3f}", "jev")


def load_jev_gate(taxonomy: Taxonomy | None = None) -> RouteGate | None:
    """Return a :class:`JevGate` when enabled and calibrated, else ``None``.

    Requires ``jev_config().enabled`` and a readable
    ``<base_dir>/models/jev_calibration.json``. The default (Jev off) returns
    ``None`` so ``load_gate`` keeps its existing learned/band behaviour.
    """
    cfg = jev_config()
    if not cfg.enabled:
        return None
    path: Path = get_settings().base_dir / "models" / "jev_calibration.json"
    if not path.is_file():
        return None
    calibration = load_jev_calibration(path)
    return JevGate(BandGate(taxonomy or load_taxonomy()), JevClient(cfg), calibration)
