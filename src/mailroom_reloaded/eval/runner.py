"""Evaluation runner (spec section 8, Task 20).

``run_eval`` joins blind documents to ground truth, samples them, and runs the
pipeline (``MailroomFlow.kickoff_async`` under an ``asyncio.Semaphore``) or the
SAND-37 ``specialist_cell`` posture (ground-truth class fed straight to
``extract``). Per-document records land in the SQLite table ``eval_docs``.

Ground truth reaches the pipeline only through ``EvalContext``, and only when a
document was selected for grading; the sorter and specialists never see it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Engine, text

from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.agents.specialists import extract as _extract
from mailroom_reloaded.eval.dataset import (
    DEFAULT_REVISION,
    BlindDoc,
    EvalContext,
    GroundTruth,
    doc_id_for_sha,
    load_split,
    sample,
)
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.obs.run_context import run_scope
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.pipeline import run_ledger
from mailroom_reloaded.settings import get_settings, load_taxonomy
from mailroom_reloaded.storage import db
from mailroom_reloaded.storage.bins import Bins

__all__ = ["EvalConfig", "run_eval", "select_graded"]

#: ``eval_docs`` column set: identity, stage outputs, efficiency, gate features
#: and the judge grade (spec section 8).
_EVAL_DDL = """
CREATE TABLE IF NOT EXISTS eval_docs (
    run_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    doc_id TEXT,
    mode TEXT,
    status TEXT,
    doc_type TEXT,
    doc_subclass TEXT,
    sort_confidence REAL,
    sort_mode TEXT,
    extract_confidence REAL,
    schema_valid INTEGER,
    parse_error TEXT,
    error_kind TEXT,
    length_capped INTEGER,
    latency_s REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    calls INTEGER,
    graded INTEGER,
    judge_overall REAL,
    judge_fields TEXT,
    judge_classification TEXT,
    gate_features TEXT,
    route_trail TEXT,
    PRIMARY KEY (run_id, filename)
)
"""

_COLUMNS = (
    "run_id",
    "filename",
    "doc_id",
    "mode",
    "status",
    "doc_type",
    "doc_subclass",
    "sort_confidence",
    "sort_mode",
    "extract_confidence",
    "schema_valid",
    "parse_error",
    "error_kind",
    "length_capped",
    "latency_s",
    "prompt_tokens",
    "completion_tokens",
    "calls",
    "graded",
    "judge_overall",
    "judge_fields",
    "judge_classification",
    "gate_features",
    "route_trail",
)

_INSERT = (
    f"INSERT OR REPLACE INTO eval_docs ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _COLUMNS)})"
)


@dataclass
class EvalConfig:
    """One evaluation posture/cell (spec section 8, Task 20 interfaces)."""

    revision: str = DEFAULT_REVISION
    per_class: int = 20
    seed: int = 42
    classes: list[str] | None = None
    concurrency: int = 8
    posture_label: str = "pipeline"
    gpu: str = "L4"
    gpus: int = 1
    prompt_set: str = "frozen_v1"
    merger_mode: str = "frozen"
    mode: Literal["pipeline", "specialist_cell"] = "pipeline"
    judge_sample_rate: float = 1.0
    split: str = "test"
    local_dir: Path | None = None
    dataset_repo: str | None = None
    dataset_config: str | None = None
    gpu_usd_per_hour: float = 0.80
    extra: dict[str, Any] = field(default_factory=dict)


def select_graded(
    docs: list[BlindDoc], rate: float, seed: int
) -> set[str]:
    """Filenames to grade with the judge, deterministically seeded.

    ``rate <= 0`` grades nothing and ``rate >= 1`` grades everything; otherwise
    the documents are ranked by a seeded hash and the first ``round(rate*n)`` are
    graded, so a given ``(docs, rate, seed)`` always selects the same half.
    """
    if rate <= 0.0:
        return set()
    if rate >= 1.0:
        return {doc.filename for doc in docs}
    ranked = sorted(
        docs,
        key=lambda d: hashlib.sha256(f"{seed}:{d.filename}".encode()).hexdigest(),
    )
    count = round(rate * len(ranked))
    return {doc.filename for doc in ranked[:count]}


# --------------------------------------------------------------------------- sqlite


def _engine() -> Engine:
    """Return the configured database engine for evaluation records."""
    return db.get_engine()


def _ensure_table(engine: Engine) -> None:
    """Create the evaluation results table if it does not already exist."""
    with engine.begin() as conn:
        conn.execute(text(_EVAL_DDL))


def _insert(engine: Engine, row: dict[str, Any]) -> None:
    """Insert or replace one document result using the declared column set."""
    with engine.begin() as conn:
        conn.execute(text(_INSERT), {k: row.get(k) for k in _COLUMNS})


def _json(value: Any) -> str:
    """Serialize values as Unicode JSON, stringifying unsupported objects."""
    return json.dumps(value, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- records


def _write_doc(doc: BlindDoc) -> Path:
    """Write blind document text to the configured inbox and return its path.

    Some dataset filenames are nested paths (Enron ``owner/folder/n.``), so the
    inbox parent directory is created before writing.
    """
    path = Bins(get_settings().base_dir).inbox / doc.filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc.doc_text, encoding="utf-8")
    return path


def _state_doc_type(state: Any) -> str | None:
    """Return the sorter or extractor prediction, or None when both are absent."""
    sort = getattr(state, "sort", None)
    extract = getattr(state, "extract", None)
    for candidate in (
        getattr(sort, "doc_type", None),
        getattr(extract, "doc_type", None),
    ):
        if candidate:
            return str(candidate)
    return None


def _state_subclass(state: Any, gt: GroundTruth | None) -> str | None:
    """Return the sorted subclass, falling back to the ground-truth label."""
    sort = getattr(state, "sort", None)
    candidate = getattr(sort, "doc_subclass", None) or (
        gt.expected_subclass if gt is not None else None
    )
    return str(candidate) if candidate else None


def _usage(state: Any) -> Usage:
    """Return the state's Usage value, or empty counters when unavailable."""
    usage = getattr(state, "usage_total", None)
    return usage if isinstance(usage, Usage) else Usage()


def _grade_parts(grade: Any) -> tuple[float | None, str | None, str | None]:
    """Convert a judge grade into its score and JSON fields for persistence."""
    if grade is None:
        return None, None, None
    overall = getattr(grade, "overall", None)
    fields = getattr(grade, "fields", None)
    classification = getattr(grade, "classification", None)
    return (
        float(overall) if overall is not None else None,
        _json([f.model_dump() if hasattr(f, "model_dump") else f for f in (fields or [])]),
        _json(classification.model_dump() if hasattr(classification, "model_dump") else classification),
    )


def _gate_features(state: Any, doc_type: str | None, extract: ExtractResult | None) -> dict[str, Any]:
    """Collect classification and extraction signals for gate training."""
    sort = getattr(state, "sort", None)
    return {
        "classify": {
            "doc_type": doc_type,
            "confidence": getattr(sort, "confidence", None),
            "attempts": int(getattr(state, "classify_attempts", 0) or 0),
            "doc_type_disagree": bool(getattr(sort, "doc_type_disagree", False)),
            "resorted": bool(getattr(state, "resorted", False)),
        },
        "extract": {
            "confidence": extract.confidence if extract is not None else None,
            "attempts": int(getattr(state, "extract_attempts", 0) or 0),
            "schema_valid": bool(extract.schema_valid) if extract is not None else False,
            "length_capped": bool(
                extract is not None and extract.error_kind == "LengthFinishReasonError"
            ),
        },
    }


def _base_row(
    run_id: str,
    doc: BlindDoc,
    gt: GroundTruth | None,
    *,
    mode: str,
    latency_s: float,
    graded: bool,
) -> dict[str, Any]:
    """Build an evaluation row with identity, timing and error defaults."""
    return {
        "run_id": run_id,
        "filename": doc.filename,
        "doc_id": doc_id_for_sha(doc.content_sha256),
        "mode": mode,
        "status": "error",
        "doc_type": None,
        "doc_subclass": gt.expected_subclass if gt is not None else None,
        "sort_confidence": None,
        "sort_mode": None,
        "extract_confidence": None,
        "schema_valid": 0,
        "parse_error": None,
        "error_kind": None,
        "length_capped": 0,
        "latency_s": round(float(latency_s), 6),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "calls": 0,
        "graded": 1 if graded else 0,
        "judge_overall": None,
        "judge_fields": None,
        "judge_classification": None,
        "gate_features": None,
        "route_trail": None,
    }


def _pipeline_row(
    run_id: str,
    doc: BlindDoc,
    gt: GroundTruth | None,
    state: Any,
    *,
    graded: bool,
    latency_s: float,
) -> dict[str, Any]:
    """Merge pipeline outputs, usage, routes and grading into a result row."""
    sort = getattr(state, "sort", None)
    extract = getattr(state, "extract", None)
    usage = _usage(state)
    doc_type = _state_doc_type(state)
    row = _base_row(run_id, doc, gt, mode="pipeline", latency_s=latency_s, graded=graded)
    row.update(
        status=str(getattr(state, "status", "") or "unknown"),
        doc_type=doc_type,
        doc_subclass=_state_subclass(state, gt),
        sort_confidence=getattr(sort, "confidence", None),
        sort_mode=getattr(getattr(sort, "mode", None), "value", getattr(sort, "mode", None)),
        extract_confidence=extract.confidence if extract is not None else None,
        schema_valid=1 if (extract is not None and extract.schema_valid) else 0,
        parse_error=extract.parse_error if extract is not None else None,
        error_kind=extract.error_kind if extract is not None else None,
        length_capped=1 if (extract is not None and extract.error_kind == "LengthFinishReasonError") else 0,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        calls=usage.calls,
        gate_features=_json(_gate_features(state, doc_type, extract)),
        route_trail=_json(list(getattr(state, "route_trail", []) or [])),
    )
    overall, fields_json, classification_json = _grade_parts(getattr(state, "grade", None))
    row.update(
        judge_overall=overall,
        judge_fields=fields_json,
        judge_classification=classification_json,
    )
    return row


def _cell_row(
    run_id: str,
    doc: BlindDoc,
    gt: GroundTruth | None,
    result: ExtractResult | None,
    *,
    latency_s: float,
) -> dict[str, Any]:
    """Build an ungraded specialist result row, retaining failure diagnostics."""
    row = _base_row(run_id, doc, gt, mode="specialist_cell", latency_s=latency_s, graded=False)
    if result is None:
        return row
    row.update(
        status="ok" if result.schema_valid else "error",
        doc_type=result.doc_type,
        schema_valid=1 if result.schema_valid else 0,
        parse_error=result.parse_error,
        error_kind=result.error_kind,
        length_capped=1 if result.error_kind == "LengthFinishReasonError" else 0,
        extract_confidence=result.confidence,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        calls=result.calls or result.usage.calls,
    )
    return row


# --------------------------------------------------------------------------- running


def _cell_conditions(cfg: EvalConfig, doc_type: str):
    """Override merger conditions only when the configured mode differs."""
    if doc_type != "merger_agreement":
        return None
    cond = load_taxonomy().specialist_conditions(doc_type)
    if cfg.merger_mode == cond.merger_mode:
        return None
    return cond.model_copy(update={"merger_mode": cfg.merger_mode})


async def _run_pipeline(
    cfg: EvalConfig,
    run_id: str,
    doc: BlindDoc,
    gts: dict[str, GroundTruth],
    graded: set[str],
    sem: asyncio.Semaphore,
    engine: Engine,
) -> None:
    """Run one document under the semaphore and persist its result or error."""
    async with sem:
        gt = gts.get(doc.filename)
        path = _write_doc(doc)
        grade_this = doc.filename in graded and gt is not None
        eval_ctx = (
            EvalContext(run_id, {doc_id_for_sha(doc.content_sha256): gt})
            if grade_this
            else None
        )
        overrides = {
            "prompt_set": cfg.prompt_set,
            "merger_mode": cfg.merger_mode,
        }
        start = time.monotonic()
        try:
            state = await flow_mod.MailroomFlow().kickoff_async(
                inputs={
                    "path": path,
                    "worker_id": f"eval-{run_id[:8]}",
                    "overrides": overrides,
                    "eval_ctx": eval_ctx,
                }
            )
        except Exception as exc:  # noqa: BLE001 - one bad doc must not kill the run
            if not getattr(exc, "_ledger_recorded", False):  # it failed before the flow could record it
                run_ledger.record_aborted(
                    run_ledger.ledger_for(None),
                    run_id,
                    doc_id_for_sha(doc.content_sha256),
                    exc,
                    started=start,
                )
            row = _base_row(
                run_id, doc, gt, mode="pipeline", latency_s=time.monotonic() - start, graded=grade_this
            )
            row["error_kind"] = type(exc).__name__
            row["parse_error"] = str(exc)[:500]
            _insert(engine, row)
            return
        _insert(
            engine,
            _pipeline_row(
                run_id,
                doc,
                gt,
                state,
                graded=grade_this,
                latency_s=time.monotonic() - start,
            ),
        )


async def _run_cell(
    cfg: EvalConfig,
    run_id: str,
    doc: BlindDoc,
    gts: dict[str, GroundTruth],
    sem: asyncio.Semaphore,
    engine: Engine,
) -> None:
    """Extract using ground-truth labels and persist an ungraded result or error."""
    async with sem:
        gt = gts.get(doc.filename)
        doc_type = (gt.expected if gt is not None else None) or "unknown"
        doc_subclass = gt.expected_subclass if gt is not None else None
        start = time.monotonic()
        try:
            result = await asyncio.to_thread(
                _extract,
                doc.doc_text,
                doc_type,
                doc_subclass,
                prompt_set=cfg.prompt_set,
                cond=_cell_conditions(cfg, doc_type),
            )
        except Exception as exc:  # noqa: BLE001 - record and continue
            run_ledger.record_aborted(
                run_ledger.ledger_for(None),
                run_id,
                doc_id_for_sha(doc.content_sha256),
                exc,
                doc_type=doc_type,
                started=start,
            )
            row = _base_row(
                run_id, doc, gt, mode="specialist_cell", latency_s=time.monotonic() - start, graded=False
            )
            row["error_kind"] = type(exc).__name__
            row["parse_error"] = str(exc)[:500]
            _insert(engine, row)
            return
        run_ledger.record_cell(
            run_ledger.ledger_for(None),
            run_id,
            doc_id_for_sha(doc.content_sha256),
            doc_type,
            result,
            started=start,
        )
        _insert(
            engine,
            _cell_row(run_id, doc, gt, result, latency_s=time.monotonic() - start),
        )


async def _run_all(
    cfg: EvalConfig,
    run_id: str,
    selected: list[BlindDoc],
    gts: dict[str, GroundTruth],
    graded: set[str],
    engine: Engine,
) -> None:
    """Dispatch selected documents in the configured mode with bounded concurrency."""
    sem = asyncio.Semaphore(max(1, int(cfg.concurrency)))
    if cfg.mode == "specialist_cell":
        await asyncio.gather(
            *(_run_cell(cfg, run_id, doc, gts, sem, engine) for doc in selected)
        )
    else:
        await asyncio.gather(
            *(
                _run_pipeline(cfg, run_id, doc, gts, graded, sem, engine)
                for doc in selected
            )
        )


def run_eval(cfg: EvalConfig) -> str:
    """Run one eval posture and return its ``run_id``.

    Loads and verifies the split, samples it, runs every document under a
    concurrency semaphore, and writes one ``eval_docs`` row per selected
    document. Tasks share an ``eval`` run scope with session ID
    ``eval-<run_id>``; the caller's scope is restored on exit.

    Pipeline/extraction exceptions are recorded as error rows. Dataset loading
    and integrity errors, inbox write errors, and database errors propagate.
    Raises ``RuntimeError`` if called from a thread with a running event loop.
    """
    run_id = uuid.uuid4().hex[:12]
    docs, gts = load_split(
        cfg.revision,
        cfg.split,
        local_dir=cfg.local_dir,
        repo=cfg.dataset_repo,
        config=cfg.dataset_config,
    )
    selected = sample(
        docs, gts, per_class=cfg.per_class, seed=cfg.seed, classes=cfg.classes
    )
    graded = select_graded(selected, cfg.judge_sample_rate, cfg.seed)
    engine = _engine()
    _ensure_table(engine)
    # asyncio tasks (and to_thread) copy this context, so every eval document inherits the scope
    ledger = run_ledger.ledger_for(None)
    run_ledger.open_run(
        ledger,
        run_id,
        "eval",
        mode=cfg.mode,
        posture_label=cfg.posture_label,
        model=get_settings().provider,
        prompt_set=cfg.prompt_set,
        environment="eval",
        source="eval",
    )
    closed_by = "interrupted"
    try:
        with run_scope(run_id, "eval", "eval", session_id=f"eval-{run_id}"):
            asyncio.run(_run_all(cfg, run_id, selected, gts, graded, engine))
        closed_by = "completed"
    finally:
        run_ledger.close_run(ledger, run_id, closed_by, expected=len(selected))
    return run_id
