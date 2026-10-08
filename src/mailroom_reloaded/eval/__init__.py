"""Evaluation: blind/ground-truth dataset loader and the eval runner (Task 20)."""

from __future__ import annotations

from .dataset import (
    DEFAULT_REVISION,
    REPO,
    BlindDoc,
    DatasetIntegrityError,
    EvalContext,
    GroundTruth,
    load_split,
    sample,
)
from .runner import EvalConfig, run_eval, select_graded

__all__ = [
    "DEFAULT_REVISION",
    "REPO",
    "BlindDoc",
    "DatasetIntegrityError",
    "EvalConfig",
    "EvalContext",
    "GroundTruth",
    "load_split",
    "run_eval",
    "sample",
    "select_graded",
]
