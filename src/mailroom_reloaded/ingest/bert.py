"""ModernBERT primary classifier adapter and sorter handoff policy (spec section 5).

Fail-open: any problem (flag off, package or model missing, error, oversize
document) yields an unavailable verdict and the sorter runs in FULL mode.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from mailroom_reloaded.settings import BertCfg, load_taxonomy

logger = logging.getLogger(__name__)

#: ModernBERT window: 8192 tokens at ~4 chars per token.
WINDOW_CHARS = 8192 * 4

_MISSING_MARKERS = ("no_model", "bundle_missing", "model_missing")

Reason = Literal["flag_off", "no_package", "no_model", "error", "too_long", "ok"]


@dataclass(frozen=True)
class BertVerdict:
    available: bool
    reason: Reason
    doc_type: str | None = None
    subclass: str | None = None
    calibrated_confidence: float | None = None
    margin: float | None = None
    window_agreement: float | None = None
    n_windows: int | None = None
    route: str | None = None


def _unavailable(reason: Reason) -> BertVerdict:
    """Return a verdict with no prediction and the supplied unavailability reason."""
    return BertVerdict(available=False, reason=reason)


def classify_primary(text: str, cfg: BertCfg | None = None) -> BertVerdict:
    """Classify with mailroom_ml, using taxonomy BERT settings when ``cfg`` is omitted.

    Disabled BERT, missing dependencies/models, non-dict results, and caught
    exceptions produce unavailable verdicts. Text longer than
    ``32768 * max(max_trusted_windows, 1)`` characters is rejected before
    inference; text exactly at the cap is accepted.
    """
    try:
        cfg = cfg if cfg is not None else load_taxonomy().bert
        if not cfg.enabled:
            return _unavailable("flag_off")
        if len(text) > WINDOW_CHARS * max(cfg.max_trusted_windows, 1):
            return _unavailable("too_long")
        try:
            inference = importlib.import_module("mailroom_ml.inference")
        except ImportError:
            return _unavailable("no_package")
        classifier = inference.classify_document
        try:
            result = classifier(text)
        except FileNotFoundError:
            return _unavailable("no_model")
        except Exception:
            logger.exception("bert_classify_failed")
            return _unavailable("error")
        if not isinstance(result, dict):
            return _unavailable("error")
        if result.get("status") in _MISSING_MARKERS or result.get("reason") in _MISSING_MARKERS:
            return _unavailable("no_model")
        return BertVerdict(
            available=True,
            reason="ok",
            doc_type=result.get("doc_type"),
            subclass=result.get("subclass"),
            calibrated_confidence=result.get("calibrated_confidence"),
            margin=result.get("margin"),
            window_agreement=result.get("agreement"),
            n_windows=result.get("n_windows"),
            route=result.get("route"),
        )
    except Exception:  # defensive: the adapter must never raise
        logger.exception("bert_adapter_failed")
        return _unavailable("error")


class SortMode(Enum):
    FULL = "full"
    SUBCLASS_ONLY = "subclass_only"


@dataclass(frozen=True)
class Handoff:
    mode: SortMode
    locked_doc_type: str | None
    prior: str
    reason: str


def _prior(v: BertVerdict, cfg: BertCfg) -> str:
    """Format the BERT class prior, including a subclass hint only when enabled.

    Return an empty string when the verdict is unavailable or lacks a class.
    """
    if not v.available or not v.doc_type:
        return ""
    parts: list[Any] = [f"BERT predicts class {v.doc_type}"]
    if v.calibrated_confidence is not None:
        parts.append(f" (confidence {v.calibrated_confidence:.2f})")
    if cfg.pass_subclass_hint and v.subclass:
        parts.append(f", candidate subclass {v.subclass} (unverified)")
    return "".join(parts)


def decide_handoff(v: BertVerdict, cfg: BertCfg) -> Handoff:
    """Apply the spec section 5 rules in order.

    Lock the class only for available fast-path predictions outside deferred
    classes and within the trusted window count. Missing or zero window counts
    are treated as one; other verdicts use full sorting with any available prior.
    """
    if not v.available or not v.doc_type:
        return Handoff(SortMode.FULL, None, "", f"bert_unavailable:{v.reason}")
    prior = _prior(v, cfg)
    if v.doc_type in cfg.defer_classes:
        return Handoff(SortMode.FULL, None, prior, "defer_class")
    if (v.n_windows or 1) > cfg.max_trusted_windows:
        return Handoff(SortMode.FULL, None, prior, "multi_window")
    if v.route == "fast_path":
        return Handoff(SortMode.SUBCLASS_ONLY, v.doc_type, prior, "fast_path")
    return Handoff(SortMode.FULL, None, prior, "not_fast_path")
