#!/usr/bin/env python
"""Harvest Jev answers over the real mailroom TRAIN distribution (issue #8).

This is the data-collection half of the Jev calibration pipeline. It has two
modes:

``docs`` (default)
    The original document-text harvest:

    1. load the ground-truthed TRAIN split from the Hub
       (``Lucius-Morningstar/mailroom-dataset`` @ ``ed7576b6``);

    2. draw a stratified ``per_class=50`` sample with the shared
       :func:`sample` helper (nested on ``seed``, so a smaller draw is a prefix
       of this one);

    3. ask Jev, for each sampled document, a single ``choice`` question whose
       criteria are the taxonomy document classes (key -> human description);

    4. write one JSONL row per usable answer and print the accuracy and skip
       count.

    The hosted OpenRouter Decisions endpoint rejects an oversized ``state`` with
    ``max_tokens_exceeded`` (observed between 96k and 128k chars of legal text),
    so each document's text is deterministically truncated to
    :data:`MAX_STATE_CHARS` before it is sent. The production gate never ships
    raw document text to Jev -- it sends the compact ``GateFeatures`` state --
    so a bounded prefix is a faithful, minimal context. The truncated-document
    count is reported.

``features``
    The production-faithful harvest. Reads gate-feature rows (``--rows``) with
    the ``eval/train_gate.py`` schema::

        {split, stage, doc_type?, confidence, attempts, bert_confidence,
         bert_margin, bert_window_agreement, schema_valid, field_coverage,
         length_capped, retry_expected|review_expected}

    and, for each row, sends the *compact* production state
    (:func:`_jev_state` over the built :class:`GateFeatures`) -- never the
    document text and never truncated. The label is ``retry_expected`` for
    ``stage="classify"`` and ``review_expected`` for ``stage="extract"``;
    ``correct = escalated == bool(expected_escalate)`` where an escalation is
    any route other than ``proceed``/``verify`` (matching
    ``metrics.gate_kpis``). Rows missing the label, or whose route answer is
    unusable, are skipped and counted. A batch whose labels are all one class is
    refused: a single-class fit is degenerate (issue #14).

Both modes write the rows ``mailroom jev calibrate`` consumes::

    {"split": "train", "provider": ..., "model": ..., "doc_type": ...,
     "confidence": <float>, "correct": 0 | 1}

The script is deliberately thin and honest: if the Hub load, a row file, or any
live Jev call fails it reports the exact error and exits non-zero rather than
inventing rows or numbers. It never prints the API key (``JevClient`` holds it
privately).

Usage::

    set -a; source .env; set +a
    uv run python scripts/jev_harvest.py --out /tmp/opencode/jev_rows.jsonl
    uv run python scripts/jev_harvest.py --mode features \\
        --rows /tmp/opencode/jev_gate_features.jsonl \\
        --out /tmp/opencode/jev_features.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from mailroom_reloaded.agents.gate import GateFeatures
from mailroom_reloaded.agents.jev import (
    JevClient,
    _jev_questions,
    _jev_state,
    choice,
)
from mailroom_reloaded.eval.dataset import load_split, sample
from mailroom_reloaded.settings import jev_config, load_taxonomy

DEFAULT_OUT = Path("/tmp/opencode/jev_rows.jsonl")
REVISION = "ed7576b6"
SPLIT = "train"
PER_CLASS = 50
SEED = 42
CONCURRENCY = 8
#: Hosted Decisions ``state`` cap: longer inputs return ``max_tokens_exceeded``.
MAX_STATE_CHARS = 60_000

#: Routes that are neither ``proceed`` nor the mid-confidence ``verify`` tier
#: are escalations: ``retry``/``re_sort`` (classify) and ``human_review``/``boss``
#: (extract). The earlier "any non-proceed" test wrongly counted an extract
#: ``verify`` as a review, which ``metrics.gate_kpis`` does not (issue #14).
_ESCALATION_EXCLUDED = frozenset({"proceed", "verify"})

QUESTION = "doc_type"
INSTRUCTIONS = (
    "Classify the document into exactly one of the provided document classes. "
    "Answer with the class key whose description best matches the document."
)


@dataclass(frozen=True)
class _Target:
    """One document to classify (ground truth already validated as usable)."""

    filename: str
    doc_text: str
    expected: str


@dataclass(frozen=True)
class _FeatureTarget:
    """One gate-feature row to route with Jev (label validated as usable)."""

    index: int
    features: GateFeatures
    doc_type: str | None
    expected_escalate: bool
    split: str


def _criteria() -> dict[str, str]:
    """Taxonomy class key -> human description, the ``choice`` criteria map."""
    return {key: dc.description for key, dc in load_taxonomy().classes.items()}


def _as_flag(value: object) -> bool:
    """Parse a 0/1, bool or Hub string (``"true"``/``"false"``) escalation label."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def _targets(docs, gts) -> tuple[list[_Target], int, int]:
    """Build usable targets, counting skipped and over-length (truncated) docs."""
    targets: list[_Target] = []
    skipped = 0
    truncated = 0
    for doc in docs:
        gt = gts.get(doc.filename)
        if gt is None or not gt.expected or not doc.doc_text.strip():
            skipped += 1
            continue
        text = doc.doc_text
        if len(text) > MAX_STATE_CHARS:
            text = text[:MAX_STATE_CHARS]
            truncated += 1
        targets.append(_Target(doc.filename, text, gt.expected))
    return targets, skipped, truncated


def _run(client: JevClient, name: str, payload: dict, target: _Target) -> dict | None:
    """Ask Jev one ``choice`` question; return a row, or None on an unusable answer."""
    answers = client.ask(target.doc_text, {name: payload})
    answer = answers.get(name)
    if answer is None or answer.choice is None or answer.confidence is None:
        return None
    return {
        "split": SPLIT,
        "provider": client.cfg.provider,
        "model": client.cfg.model,
        "doc_type": target.expected,
        "confidence": float(answer.confidence),
        "correct": 1 if answer.choice == target.expected else 0,
    }


def _feature_target(row: dict, index: int) -> _FeatureTarget | None:
    """Build a features-mode target from one gate-feature row, or None.

    ``stage`` must be ``classify`` or ``extract`` and the matching escalation
    label (``retry_expected`` for classify, ``review_expected`` for extract)
    must be present; otherwise the row is unusable. Numeric fields absent from
    an export (``bert_*``, ``field_coverage``) fall back to their
    :class:`GateFeatures` defaults, and the row's own ``split`` is carried
    through (defaulting to ``train``).
    """
    stage = row.get("stage")
    if stage not in ("classify", "extract"):
        return None
    label_key = "retry_expected" if stage == "classify" else "review_expected"
    if row.get(label_key) is None:
        return None

    def number(key: str, default: float) -> float:
        value = row.get(key)
        return default if value is None else float(value)

    def integer(key: str, default: int) -> int:
        value = row.get(key)
        return default if value is None else int(value)

    def flag(key: str, default: bool) -> bool:
        value = row.get(key)
        return default if value is None else bool(value)

    features = GateFeatures(
        stage=stage,
        doc_type=str(row["doc_type"]) if row.get("doc_type") else None,
        confidence=number("confidence", 0.0),
        attempts=integer("attempts", 0),
        bert_confidence=number("bert_confidence", 0.0),
        bert_margin=number("bert_margin", 0.0),
        bert_window_agreement=number("bert_window_agreement", 0.0),
        schema_valid=flag("schema_valid", True),
        field_coverage=number("field_coverage", 1.0),
        length_capped=flag("length_capped", False),
    )
    return _FeatureTarget(
        index=index,
        features=features,
        doc_type=features.doc_type,
        expected_escalate=_as_flag(row[label_key]),
        split=str(row.get("split") or SPLIT),
    )


def _run_feature(client: JevClient, target: _FeatureTarget) -> dict | None:
    """Ask Jev over the compact ``GateFeatures`` state; None on unusable answer."""
    answers = client.ask(_jev_state(target.features), _jev_questions())
    route = answers.get("route")
    if route is None or route.choice is None or route.confidence is None:
        return None
    escalated = route.choice not in _ESCALATION_EXCLUDED
    return {
        "split": target.split,
        "provider": client.cfg.provider,
        "model": client.cfg.model,
        "doc_type": target.doc_type,
        "confidence": float(route.confidence),
        "correct": 1 if escalated == target.expected_escalate else 0,
    }


def _harvest_features(args: argparse.Namespace, cfg) -> int:
    """Run the features-mode harvest and return a process exit code."""
    try:
        rows = [
            json.loads(line)
            for line in args.rows.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError) as exc:
        print(
            f"FATAL: could not read gate-feature rows from {args.rows}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    targets: list[_FeatureTarget] = []
    skipped = 0
    for index, row in enumerate(rows):
        target = _feature_target(row, index)
        if target is None:
            skipped += 1
        else:
            targets.append(target)
    print(
        f"read {len(rows)} gate-feature rows; usable {len(targets)}; "
        f"skipped (missing stage/label) {skipped}",
        file=sys.stderr,
    )
    if not targets:
        print("FATAL: no usable feature rows after filtering", file=sys.stderr)
        return 1

    labels = {t.expected_escalate for t in targets}
    if len(labels) < 2:
        print(
            f"FATAL: all {len(targets)} usable rows share expected_escalate="
            f"{next(iter(labels))}; a fit on a single label class is degenerate "
            "(issue #14). Re-export the gate features from a config that carries "
            "positive retry/review labels (e.g. --config fixtures).",
            file=sys.stderr,
        )
        return 1

    client = JevClient(cfg)
    by_index: dict[int, dict] = {}
    unusable = 0

    pool = ThreadPoolExecutor(max_workers=CONCURRENCY)
    try:
        futures = {pool.submit(_run_feature, client, t): t for t in targets}
        for future in as_completed(futures):
            target = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001 - surface the exact live-call error
                print(
                    f"FATAL: Jev call failed for feature row {target.index}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                for pending in futures:
                    pending.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                return 1
            if row is None:
                unusable += 1
            else:
                by_index[target.index] = row
    finally:
        pool.shutdown(wait=True)

    # Deterministic row order: the input row order, not completion order.
    out_rows = [by_index[t.index] for t in targets if t.index in by_index]
    _write_rows(args.out, out_rows)

    correct = sum(row["correct"] for row in out_rows)
    accuracy = correct / len(out_rows) if out_rows else 0.0
    print(f"wrote {len(out_rows)} rows to {args.out}")
    print(f"Jev features accuracy: {accuracy:.4f} ({correct}/{len(out_rows)})")
    print(
        f"skipped: {skipped + unusable} "
        f"(missing stage/label: {skipped}, unusable answer: {unusable})"
    )
    return 0


def _write_rows(out: Path, rows: list[dict]) -> None:
    """Write deterministic UTF-8 JSONL rows (one object per line)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    """Run the harvest; return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mode",
        choices=("docs", "features"),
        default="docs",
        help="docs: raw document text (default); features: compact production state.",
    )
    parser.add_argument(
        "--rows",
        type=Path,
        default=None,
        help="Gate-feature JSONL for --mode features (eval_docs echoes).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"JSONL destination (default: {DEFAULT_OUT}).",
    )
    args = parser.parse_args(argv)

    cfg = jev_config()
    if not cfg.enabled:
        print(
            "Jev is off (MAILROOM_JEV_PROVIDER=off). Set a provider "
            "(openrouter|typesafe|local) before harvesting.",
            file=sys.stderr,
        )
        return 1
    print(f"Jev provider={cfg.provider} model={cfg.model}", file=sys.stderr)

    if args.mode == "features":
        if args.rows is None:
            print("FATAL: --mode features requires --rows <gate-feature JSONL>", file=sys.stderr)
            return 1
        if not args.rows.is_file():
            print(f"FATAL: --rows not found: {args.rows}", file=sys.stderr)
            return 1
        return _harvest_features(args, cfg)

    return _harvest_docs(args, cfg)


def _harvest_docs(args: argparse.Namespace, cfg) -> int:
    """Run the document-text harvest and return a process exit code."""
    try:
        docs, gts = load_split(revision=REVISION, split=SPLIT)
    except Exception as exc:  # noqa: BLE001 - surface the exact Hub/dataset error
        print(
            f"FATAL: could not load {REVISION}/{SPLIT} from the Hub: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    sampled = sample(docs, gts, per_class=PER_CLASS, seed=SEED)
    targets, skipped, truncated = _targets(sampled, gts)
    print(
        f"loaded {len(docs)} docs; sampled {len(sampled)}; "
        f"usable {len(targets)}; skipped (no expected/empty text) {skipped}; "
        f"truncated to {MAX_STATE_CHARS} chars {truncated}",
        file=sys.stderr,
    )
    if not targets:
        print("FATAL: no usable targets after sampling", file=sys.stderr)
        return 1

    name, payload = choice(QUESTION, INSTRUCTIONS, _criteria())
    client = JevClient(cfg)
    by_name: dict[str, dict] = {}
    unusable = 0

    pool = ThreadPoolExecutor(max_workers=CONCURRENCY)
    try:
        futures = {pool.submit(_run, client, name, payload, t): t for t in targets}
        for future in as_completed(futures):
            target = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001 - surface the exact live-call error
                print(
                    f"FATAL: Jev call failed for {target.filename}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                for pending in futures:
                    pending.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                return 1
            if row is None:
                unusable += 1
            else:
                by_name[target.filename] = row
    finally:
        pool.shutdown(wait=True)

    # Deterministic row order: the target (sample) order, not completion order.
    rows = [by_name[t.filename] for t in targets if t.filename in by_name]
    _write_rows(args.out, rows)

    correct = sum(row["correct"] for row in rows)
    accuracy = correct / len(rows) if rows else 0.0
    print(f"wrote {len(rows)} rows to {args.out}")
    print(f"Jev accuracy: {accuracy:.4f} ({correct}/{len(rows)})")
    print(
        f"skipped: {skipped + unusable} "
        f"(no expected/empty text: {skipped}, unusable answer: {unusable})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
