"""Dev-only Jev (TypeSafe System One) stand-in: ``POST /v1/systemone``.

Deterministic, no inference. Answers the two questions ``agents.jev`` asks:
``route`` (choice) and ``escalate`` (noul). Decision rules, in order:

1. A marker ``[jev:proceed|verify|boss|retry|human_review]`` in a string state
   (or in the JSON of the state) picks the route.
2. Otherwise, from the gate features in the state (both stages): confidence
   >= 0.90 proceeds, 0.87..0.90 verifies (a mid-confidence answer, inside the
   calibrated verify band) and lower confidences are escalated to a human. Note
   the classify stage maps ``verify`` to human review; the dev correspondence
   extract confidence never reaches the medium band, so Jev is consulted at
   classify only.
3. Anything else proceeds, except ~10% of states (stable hash) that verify.
"""

import hashlib
import json
import re

from fastapi import FastAPI

app = FastAPI()

ROUTES = ("proceed", "retry", "verify", "boss", "human_review")
_MARKER = re.compile(r"\[jev:(proceed|retry|verify|boss|human_review)\]")


def _choose(state) -> tuple[str, float, float]:
    """Return ``(route, route_confidence, escalate_probability)``."""
    text = state if isinstance(state, str) else json.dumps(state, sort_keys=True)
    marker = _MARKER.search(text)
    if marker:
        route = marker.group(1)
        return route, (0.55 if route == "human_review" else 0.93), (
            0.9 if route == "human_review" else 0.05
        )
    if isinstance(state, dict) and "stage" in state:
        conf = float(state.get("confidence") or 0.0)
        if conf >= 0.90:
            return "proceed", 0.95, 0.05
        if conf >= 0.87:
            return "verify", 0.75, 0.10
        return "human_review", 0.55, 0.90
    bucket = int(hashlib.sha256(text.encode()).hexdigest(), 16) % 100
    return ("verify", 0.75, 0.10) if bucket < 10 else ("proceed", 0.95, 0.05)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/systemone")
def systemone(body: dict):
    route, conf, escalate = _choose(body.get("state"))
    questions = body.get("questions") or {}
    answers: dict = {}
    for name, question in questions.items():
        kind = (question or {}).get("type")
        if kind == "choice":
            options = list((question.get("criteria") or {}) or ROUTES)
            rest = (1.0 - conf) / max(len(options) - 1, 1)
            probs = {o: (conf if o == route else rest) for o in options}
            answers[name] = {
                "type": "choice", "choice": route if route in options else options[0],
                "probabilities": probs, "confidence": conf,
            }
        elif kind == "noul":
            answers[name] = {"type": "noul", "noul": escalate}
        elif kind == "score":
            labels = list(question.get("criteria") or [])
            answers[name] = {
                "type": "score", "score": conf, "legend": {str(l): str(l) for l in labels},
            }
    return {"answers": answers}
