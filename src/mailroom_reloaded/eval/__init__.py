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
from .cards import CARD_SCHEMA, build_card, build_master, render_card_md
from .cost import cell_cost, token_split
from .metrics import gate_kpis, sorter_kpis, specialist_kpis
from .runner import EvalConfig, run_eval, select_graded
from .vllm_telemetry import ReplicaTelemetry, scrape, telemetry_delta

__all__ = [
    "DEFAULT_REVISION",
    "REPO",
    "BlindDoc",
    "CARD_SCHEMA",
    "DatasetIntegrityError",
    "EvalConfig",
    "EvalContext",
    "GroundTruth",
    "ReplicaTelemetry",
    "build_card",
    "build_master",
    "cell_cost",
    "gate_kpis",
    "load_split",
    "render_card_md",
    "run_eval",
    "sample",
    "scrape",
    "select_graded",
    "sorter_kpis",
    "specialist_kpis",
    "telemetry_delta",
    "token_split",
]
