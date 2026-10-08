"""Deterministic route gate (replaces the reviewer nodes). Zero LLM calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

from mailroom_reloaded.settings import Taxonomy, get_settings, load_taxonomy

Stage = Literal["classify", "extract"]
Action = Literal["proceed", "retry", "re_sort", "verify", "boss", "human_review"]

NUMERIC_FEATURES = (
    "confidence",
    "attempts",
    "bert_confidence",
    "bert_margin",
    "bert_window_agreement",
    "schema_valid",
    "field_coverage",
    "length_capped",
)


@dataclass(frozen=True)
class GateFeatures:
    stage: Stage
    doc_type: str | None
    confidence: float
    attempts: int
    bert_confidence: float = 0.0
    bert_margin: float = 0.0
    bert_window_agreement: float = 0.0
    schema_valid: bool = True
    field_coverage: float = 1.0
    length_capped: bool = False
    doc_type_disagree: bool = False
    resorted: bool = False


@dataclass(frozen=True)
class GateDecision:
    action: Action
    reason: str
    source: Literal["band", "model", "rule"]


class RouteGate(Protocol):
    def decide(self, f: GateFeatures) -> GateDecision:
        """Return the routing decision for the supplied pipeline features."""
        ...


class BandGate:
    """Threshold bands from the taxonomy (per class once doc_type is known)."""

    def __init__(self, taxonomy: Taxonomy | None = None) -> None:
        """Use the supplied taxonomy, or load the packaged taxonomy when omitted."""
        self.taxonomy = taxonomy or load_taxonomy()

    def thresholds(self, doc_type: str | None):
        """Return class thresholds, falling back to global defaults for unknown classes."""
        return self.taxonomy.confidence_for(doc_type)

    def decide(self, f: GateFeatures) -> GateDecision:
        """Choose an action from confidence bands, retry counts, and hard rules.

        Classification disagreements trigger one re-sort. Length-capped or invalid
        extractions retry before human review. Threshold equality belongs to the
        upper band, and retries stop when ``attempts >= retry_max``.
        """
        t = self.thresholds(f.doc_type)
        if f.stage == "classify":
            if f.doc_type_disagree and not f.resorted:
                return GateDecision("re_sort", "bert/sorter doc_type disagree", "rule")
            if f.confidence >= t.high:
                return GateDecision("proceed", f"confidence >= high {t.high}", "band")
            if f.attempts < t.retry_max:
                return GateDecision("retry", f"confidence < high {t.high}", "band")
            return GateDecision("human_review", "classify retries spent", "band")
        # extract
        if f.length_capped or not f.schema_valid:
            why = "length capped" if f.length_capped else "schema invalid"
            if f.attempts < t.retry_max:
                return GateDecision("retry", why, "rule")
            return GateDecision("human_review", why + ", retries spent", "rule")
        if f.confidence >= t.judge_band_high:
            return GateDecision(
                "proceed", f"confidence >= judge_band_high {t.judge_band_high}", "band"
            )
        if f.confidence >= t.low:
            return GateDecision("verify", f"{t.low} <= confidence < {t.judge_band_high}", "band")
        if f.attempts < t.retry_max:
            return GateDecision("retry", f"confidence < low {t.low}", "band")
        return GateDecision("boss", "extract retries spent below low", "band")


def feature_vector(names: list[str], f: GateFeatures) -> np.ndarray:
    """Numeric features by name; ``doc_type=<key>`` names are one-hot."""
    out = []
    for n in names:
        if n.startswith("doc_type="):
            out.append(1.0 if f.doc_type == n.split("=", 1)[1] else 0.0)
        else:
            out.append(float(getattr(f, n)))
    return np.asarray(out, dtype=float)


class LearnedGate:
    """Logistic model (plain JSON coefficients, numpy inference).

    Overrides the band decision only inside the medium band: classify
    ``low <= confidence < high``; extract ``low <= confidence < judge_band_high``.
    Hard rules (re_sort, length cap, invalid schema) are never overridden.

    Model output p is P(escalate) (label ``retry_expected`` for classify,
    ``review_expected`` for extract). With p >= threshold:
      classify -> ``retry`` while attempts < retry_max else ``human_review``;
      extract  -> ``verify``.
    Otherwise -> ``proceed``. Stages missing from the file fall back to bands.
    Inside the medium band a model ``proceed`` may override a band
    ``human_review`` (classify with retries spent); this is intentional.
    Unknown feature names in the file raise ValueError at load.
    """

    def __init__(self, band: BandGate, coef_path: Path) -> None:
        """Load stage models from a JSON coefficient file with ``band`` as fallback.

        Unknown features or mismatched coefficient counts raise ``ValueError``.
        File errors, JSON decoding errors, and missing model keys propagate.
        """
        self.band = band
        self.models: dict = json.loads(Path(coef_path).read_text("utf-8"))
        for stage, m in self.models.items():
            for n in m["features"]:
                if n not in NUMERIC_FEATURES and not n.startswith("doc_type="):
                    raise ValueError(f"unknown gate feature {n!r} in stage {stage!r}")
            if len(m["coef"]) != len(m["features"]):
                raise ValueError(f"coef/features length mismatch in stage {stage!r}")

    def _p(self, m: dict, f: GateFeatures) -> float:
        """Return the logistic escalation probability for a stage model and features."""
        x = feature_vector(m["features"], f)
        z = float(x @ np.asarray(m["coef"], dtype=float) + float(m["intercept"]))
        return float(1.0 / (1.0 + np.exp(-np.clip(z, -500, 500))))

    def decide(self, f: GateFeatures) -> GateDecision:
        """Apply the learned escalation threshold inside the stage's medium band.

        Preserve hard-rule decisions and use band decisions outside the medium
        band or when no model exists for the stage. Equality with the model
        threshold escalates.
        """
        base = self.band.decide(f)
        m = self.models.get(f.stage)
        if m is None or base.source == "rule":
            return base
        t = self.band.thresholds(f.doc_type)
        upper = t.high if f.stage == "classify" else t.judge_band_high
        if not (t.low <= f.confidence < upper):
            return base
        p = self._p(m, f)
        reason = f"learned p={p:.3f} thr={m['threshold']}"
        if p < m["threshold"]:
            return GateDecision("proceed", reason, "model")
        if f.stage == "classify":
            action: Action = "retry" if f.attempts < t.retry_max else "human_review"
        else:
            action = "verify"
        return GateDecision(action, reason, "model")


def load_gate() -> RouteGate:
    """Load ``<base_dir>/models/route_gate.json`` if present, otherwise use bands.

    Errors loading or validating an existing model file propagate.
    """
    band = BandGate(load_taxonomy())
    path = get_settings().base_dir / "models" / "route_gate.json"
    if path.exists():
        return LearnedGate(band, path)
    return band


def ece(confidences, correct, bins: int = 10) -> float:
    """Expected calibration error with equal-width bins."""
    c = np.asarray(confidences, dtype=float)
    y = np.asarray(correct, dtype=float)
    if c.size == 0:
        return 0.0
    idx = np.minimum((c * bins).astype(int), bins - 1)
    total = 0.0
    for b in range(bins):
        mask = idx == b
        if mask.any():
            total += mask.sum() / c.size * abs(y[mask].mean() - c[mask].mean())
    return float(total)
