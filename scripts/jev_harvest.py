#!/usr/bin/env python
"""Harvest Jev ``choice`` answers over the real mailroom TRAIN split (issue #8).

This is the data-collection half of the Jev calibration pipeline:

1. load the ground-truthed TRAIN split from the Hub
   (``Lucius-Morningstar/mailroom-dataset`` @ ``ed7576b6``);

2. draw a stratified ``per_class=50`` sample with the shared :func:`sample`
   helper (nested on ``seed``, so a smaller draw is a prefix of this one);

3. ask Jev, for each sampled document, a single ``choice`` question whose
   criteria are the taxonomy document classes (key -> human description);

4. write one JSONL row per usable answer and print the accuracy and skip count.

The hosted OpenRouter Decisions endpoint rejects an oversized ``state`` with
``max_tokens_exceeded`` (observed between 96k and 128k chars of legal text), so
each document's text is deterministically truncated to :data:`MAX_STATE_CHARS`
before it is sent. The production gate never ships raw document text to Jev --
it sends the compact ``GateFeatures`` state -- so a bounded prefix is a faithful,
minimal context. The truncated-document count is reported.

The output rows are exactly what ``mailroom jev calibrate`` consumes::

    {"split": "train", "provider": ..., "model": ..., "doc_type": ...,
     "confidence": <float>, "correct": 0 | 1}

The script is deliberately thin and honest: if the Hub load or any live Jev
call fails it reports the exact error and exits non-zero rather than inventing
rows or numbers. It never prints the API key (``JevClient`` holds it privately).

Usage::

    set -a; source .env; set +a
    uv run python scripts/jev_harvest.py --out /tmp/opencode/jev_rows.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from mailroom_reloaded.agents.jev import JevClient, choice
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


def _criteria() -> dict[str, str]:
    """Taxonomy class key -> human description, the ``choice`` criteria map."""
    return {key: dc.description for key, dc in load_taxonomy().classes.items()}


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
