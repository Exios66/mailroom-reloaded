#!/usr/bin/env python
"""Export evaluator gate-feature rows + ground-truth labels for Jev harvesting.

The ``features`` mode of :mod:`scripts.jev_harvest` needs *production* gate
features, and those are produced by the pipeline during an evaluation run and
persisted in the ``eval_docs`` SQLite table (``eval/runner.py``:
``gate_features`` at ``:70``, built by ``_gate_features`` at ``:229``). This
script reads that table back and, joined to the dataset ground truth, emits the
flat gate-feature schema ``scripts/jev_harvest.py --mode features`` consumes::

    {"split", "stage", "doc_type?", "confidence", "attempts",
     "bert_confidence", "bert_margin", "bert_window_agreement", "schema_valid",
     "field_coverage", "length_capped", "retry_expected"|"review_expected"}

One row is emitted per (document, stage) that actually ran: ``classify`` always
(unless the pipeline never sorted the document) and ``extract`` only when the
extractor produced a confidence. Labels come from the dataset's evaluation
contract (``retry_expected`` for classify, ``review_expected`` for extract) --
nothing is synthesized. Documents with a missing ``gate_features`` blob, a stage
with a null confidence, or a missing label are skipped and counted.

Usage::

    set -a; source .env; set +a
    uv run python scripts/jev_export_gate_features.py \\
        --run-id <run_id> --out /tmp/opencode/jev_gate_features.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

from mailroom_reloaded.eval.dataset import (
    DEFAULT_REVISION,
    REPO,
    DatasetIntegrityError,
    load_split,
)
from mailroom_reloaded.storage import db

DEFAULT_OUT = Path("/tmp/opencode/jev_gate_features.jsonl")
DEFAULT_SPLIT = "train"


def _latest_run_id(engine: Any) -> str | None:
    """Return the most recently inserted ``eval_docs`` run id, if any."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT run_id FROM eval_docs "
                "GROUP BY run_id ORDER BY MAX(rowid) DESC LIMIT 1"
            )
        ).first()
    return None if row is None else str(row[0])


def _load_run(engine: Any, run_id: str) -> list[dict[str, Any]]:
    """Load one run's document identities and gate features."""
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "SELECT filename, content_sha256, doc_type, gate_features FROM eval_docs "
                "WHERE run_id = :rid ORDER BY filename"
            ),
            {"rid": run_id},
        )
        return [dict(row._mapping) for row in result]


def _verify_dataset(engine: Any, run_id: str, args: argparse.Namespace) -> None:
    """Reject unrecorded or different dataset selections before loading labels."""
    with engine.begin() as conn:
        recorded = conn.execute(
            text("SELECT * FROM eval_runs WHERE run_id = :rid"), {"rid": run_id}
        ).mappings().first()
    if recorded is None:
        raise DatasetIntegrityError("Run has no dataset provenance; rerun evaluation")
    selected = {
        "dataset_repo": args.dataset_repo or REPO,
        "dataset_config": args.config,
        "revision": args.revision,
        "split": args.split,
        "local_dir": str(args.local_dir.resolve()) if args.local_dir is not None else None,
    }
    for key, value in selected.items():
        if recorded[key] != value:
            raise DatasetIntegrityError(
                f"Dataset {key} mismatch: run recorded {recorded[key]!r}, selected {value!r}"
            )


def _as_flag(value: object) -> bool:
    """Parse a 0/1, bool or Hub string (``"true"``/``"false"``) escalation label."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def _stage_rows(
    filename: str,
    doc_type: str | None,
    gate: dict[str, Any],
    gt: Any,
    split: str = DEFAULT_SPLIT,
) -> tuple[list[dict[str, Any]], int]:
    """Build the flat classify/extract rows for one document; count skips."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    classify = gate.get("classify") or {}
    extract = gate.get("extract") or {}

    specs = (
        ("classify", classify, "retry_expected"),
        ("extract", extract, "review_expected"),
    )
    for stage, block, label_key in specs:
        confidence = block.get("confidence")
        label = getattr(gt, label_key, None) if gt is not None else None
        if confidence is None or label is None:
            skipped += 1
            continue
        row = {
            "split": split,
            "filename": filename,
            "stage": stage,
            "doc_type": block.get("doc_type") or doc_type,
            "confidence": float(confidence),
            "attempts": int(block.get("attempts") or 0),
            "schema_valid": bool(block.get("schema_valid", True)),
            "field_coverage": (
                1.0 if bool(block.get("schema_valid", True)) else 0.0
            )
            if stage == "extract"
            else 1.0,
            "length_capped": bool(block.get("length_capped", False)),
            label_key: _as_flag(label),
        }
        rows.append(row)
    return rows, skipped


def main(argv: list[str] | None = None) -> int:
    """Export the gate-feature rows; return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", default=None, help="Eval run id (default: latest).")
    parser.add_argument("--revision", default=DEFAULT_REVISION, help="Dataset revision.")
    parser.add_argument("--split", default=DEFAULT_SPLIT, help="Dataset split for labels.")
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=None,
        help="Local dataset dir (default.jsonl/ground_truth.jsonl) instead of the Hub.",
    )
    parser.add_argument(
        "--dataset-repo",
        default=None,
        help="Hub repo override (e.g. Lucius-Morningstar/mailroom-reloaded-fixtures).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Single labeled config (fixtures/bundles) instead of the default join.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"JSONL destination (default: {DEFAULT_OUT}).",
    )
    args = parser.parse_args(argv)

    engine = db.get_engine()
    run_id = args.run_id or _latest_run_id(engine)
    if run_id is None:
        print(
            "FATAL: no eval_docs rows found; run 'mailroom eval' first.",
            file=sys.stderr,
        )
        return 1
    print(f"exporting run_id={run_id}", file=sys.stderr)

    try:
        docs = _load_run(engine, run_id)
    except Exception as exc:  # noqa: BLE001 - surface the exact DB error
        print(f"FATAL: could not read eval_docs: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not docs:
        print(f"FATAL: run {run_id} has no eval_docs rows", file=sys.stderr)
        return 1

    try:
        _verify_dataset(engine, run_id, args)
        blind_docs, gts = load_split(
            args.revision,
            args.split,
            local_dir=args.local_dir,
            repo=args.dataset_repo,
            config=args.config,
        )
        hashes = {doc.filename: doc.content_sha256 for doc in blind_docs}
        for record in docs:
            filename = record["filename"]
            if not record["content_sha256"] or hashes.get(filename) != record["content_sha256"]:
                raise DatasetIntegrityError(f"{filename}: run document content_sha256 mismatch")
    except Exception as exc:  # noqa: BLE001 - surface the exact Hub/dataset error
        print(
            f"FATAL: could not load ground truth {args.revision}/{args.split}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    out_rows: list[dict[str, Any]] = []
    skipped = 0
    no_blob = 0
    for record in docs:
        raw = record.get("gate_features")
        if not raw:
            no_blob += 1
            continue
        try:
            gate = json.loads(raw)
        except (TypeError, ValueError):
            no_blob += 1
            continue
        if not isinstance(gate, dict):
            no_blob += 1
            continue
        gt = gts.get(str(record["filename"]))
        rows, stage_skips = _stage_rows(
            str(record["filename"]), record.get("doc_type"), gate, gt, args.split
        )
        out_rows.extend(rows)
        skipped += stage_skips

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in out_rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    n_classify = sum(1 for r in out_rows if r["stage"] == "classify")
    n_extract = sum(1 for r in out_rows if r["stage"] == "extract")
    print(f"eval_docs rows: {len(docs)} (no gate_features blob: {no_blob})")
    print(
        f"wrote {len(out_rows)} gate-feature rows to {args.out} "
        f"(classify {n_classify}, extract {n_extract}); skipped {skipped} "
        "(null confidence or missing label)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
