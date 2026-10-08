"""Blind/ground-truth dataset loader and nested sampler (spec section 8, Task 20).

Ground truth comes from ``Lucius-Morningstar/mailroom-dataset`` at revision
``ed7576b6`` (SAND-37). Configs are ``default`` (blind) and ``ground_truth``,
joined on ``filename``; ``content_sha256`` is verified at load so a corrupted or
truncated document cannot silently reach the pipeline.

The blind side carries no label fields at all (``BlindDoc``). Ground truth lives
only here and is reachable from just two places: the ``grade`` step and the
``get_ground_truth`` tool (spec section 8, "The ground truth stays out of the
agents' path by construction").
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_REVISION",
    "REPO",
    "BlindDoc",
    "DatasetIntegrityError",
    "EvalContext",
    "GroundTruth",
    "load_split",
    "sample",
]

REPO = "Lucius-Morningstar/mailroom-dataset"
DEFAULT_REVISION = "ed7576b6"

#: Candidate source columns for the blind text and its declared hash.
_TEXT_KEYS = ("doc_text", "text", "content", "document", "body")
_SHA_KEYS = ("content_sha256", "sha256", "content_hash")


class DatasetIntegrityError(Exception):
    """A blind document's ``content_sha256`` does not match its bytes."""


@dataclass(frozen=True)
class BlindDoc:
    """A document as the pipeline sees it: no label fields, ever."""

    filename: str
    doc_text: str
    content_sha256: str


@dataclass(frozen=True)
class GroundTruth:
    """The evaluation contract for one blind document (joined on ``filename``)."""

    filename: str
    expected: str | None = None
    expected_subclass: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    cuad_clause_labels: list[str] = field(default_factory=list)
    maud_clause_labels: list[str] = field(default_factory=list)
    retry_expected: bool | None = None
    review_expected: bool | None = None
    expected_stage: str | None = None


@dataclass(frozen=True)
class EvalContext:
    """The seam ``pipeline/flow.py`` reads as ``eval_ctx``.

    ``flow`` accesses ``self._eval_ctx.ground_truth`` and calls
    ``gt_map.get(doc_id)``; the map is therefore keyed by ``doc_id`` (the first
    16 hex characters of ``content_sha256``), not by filename.
    """

    run_id: str
    ground_truth: dict[str, GroundTruth] = field(default_factory=dict)


# --------------------------------------------------------------------------- helpers


def sha256_text(text: str) -> str:
    """sha256 over the UTF-8 bytes of ``text`` (what ``load_split`` verifies)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def doc_id_for_sha(content_sha256: str) -> str:
    """The flow's ``doc_id``: the first 16 hex characters of the content sha256."""
    return str(content_sha256)[:16]


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except ValueError:
            return [value]
        if isinstance(loaded, list):
            return [str(v) for v in loaded]
    return []


def _row_get(row: Any, keys: tuple[str, ...], default: Any = None) -> Any:
    """First present, non-null value for ``keys`` in a mapping-like row."""
    for key in keys:
        try:
            value = row[key]
        except (KeyError, TypeError):
            value = None
        if value is None and not isinstance(row, dict):
            value = getattr(row, key, None)
        if value is not None:
            return value
    return default


def _blind_from_row(row: Any) -> BlindDoc:
    filename = str(_row_get(row, ("filename", "file", "name"), "")).strip()
    text = str(_row_get(row, _TEXT_KEYS, "") or "")
    declared = _row_get(row, _SHA_KEYS)
    if not filename:
        raise DatasetIntegrityError("blind row is missing a filename")
    if declared is None:
        raise DatasetIntegrityError(f"{filename}: blind row is missing content_sha256")
    actual = sha256_text(text)
    if str(declared) != actual:
        raise DatasetIntegrityError(
            f"{filename}: content_sha256 mismatch "
            f"(declared {declared}, actual {actual})"
        )
    return BlindDoc(filename=filename, doc_text=text, content_sha256=actual)


def _ground_truth_from_row(row: Any) -> GroundTruth:
    filename = str(_row_get(row, ("filename", "file", "name"), "")).strip()
    if not filename:
        raise DatasetIntegrityError("ground-truth row is missing a filename")
    return GroundTruth(
        filename=filename,
        expected=_row_get(row, ("expected", "expected_doc_type", "label", "doc_type")),
        expected_subclass=_row_get(row, ("expected_subclass", "doc_subclass", "subclass")),
        fields=_as_dict(_row_get(row, ("fields", "extraction", "ground_truth_fields"))),
        cuad_clause_labels=_as_list(_row_get(row, ("cuad_clause_labels", "cuad_clauses"))),
        maud_clause_labels=_as_list(_row_get(row, ("maud_clause_labels", "maud_clauses"))),
        retry_expected=_row_get(row, ("retry_expected",)),
        review_expected=_row_get(row, ("review_expected",)),
        expected_stage=_row_get(row, ("expected_stage",)),
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------- loaders


def _load_local(local_dir: Path) -> tuple[list[BlindDoc], dict[str, GroundTruth]]:
    base = Path(local_dir)
    blind_rows = _read_jsonl(base / "default.jsonl")
    gt_rows = _read_jsonl(base / "ground_truth.jsonl")
    return _join(blind_rows, gt_rows)


def _load_live(
    revision: str, split: str
) -> tuple[list[BlindDoc], dict[str, GroundTruth]]:
    try:
        import datasets
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "the 'eval' extra is required for live dataset loading: "
            "pip install 'mailroom-reloaded[eval]'"
        ) from exc

    blind_rows = _load_hf_config(datasets, "default", revision, split)
    gt_rows = _load_hf_config(datasets, "ground_truth", revision, split)
    return _join(blind_rows, gt_rows)


def _load_hf_config(datasets: Any, config: str, revision: str, split: str) -> list[Any]:
    try:
        dataset = datasets.load_dataset(REPO, config, revision=revision, split=split)
    except Exception:  # noqa: BLE001 - fall back to a split-column filter
        dataset = datasets.load_dataset(REPO, config, revision=revision)
        return [row for row in dataset if _row_get(row, ("split",), split) == split]
    return list(dataset)


def _join(
    blind_rows: list[Any], gt_rows: list[Any]
) -> tuple[list[BlindDoc], dict[str, GroundTruth]]:
    docs = [_blind_from_row(row) for row in blind_rows]
    gts = {gt.filename: gt for gt in (_ground_truth_from_row(row) for row in gt_rows)}
    return docs, gts


def load_split(
    revision: str = DEFAULT_REVISION,
    split: str = "test",
    *,
    local_dir: Path | None = None,
) -> tuple[list[BlindDoc], dict[str, GroundTruth]]:
    """Load the blind docs and ground truth for one split.

    ``local_dir`` reads ``default.jsonl`` / ``ground_truth.jsonl`` (tests and
    offline runs); otherwise ``datasets.load_dataset`` is used behind the
    ``eval`` extra. Every blind document's ``content_sha256`` is verified against
    its bytes and a mismatch raises :class:`DatasetIntegrityError`.
    """
    if local_dir is not None:
        return _load_local(Path(local_dir))
    return _load_live(revision, split)


# --------------------------------------------------------------------------- sampler


def sample(
    docs: list[BlindDoc],
    gts: dict[str, GroundTruth],
    *,
    per_class: int,
    seed: int = 42,
    classes: list[str] | None = None,
) -> list[BlindDoc]:
    """Draw up to ``per_class`` documents per class, nested across ``per_class``.

    Each class's documents are ordered by a shuffle seeded on ``(seed, class)``,
    so the ``n=20`` draw is a prefix of the ``n=50`` draw for the same class and
    seed. Class order follows the taxonomy (unknown classes follow, sorted).
    """
    by_class: dict[str | None, list[BlindDoc]] = {}
    for doc in docs:
        gt = gts.get(doc.filename)
        by_class.setdefault(gt.expected if gt is not None else None, []).append(doc)

    from mailroom_reloaded.settings import load_taxonomy

    known = list(load_taxonomy().classes)
    wanted = set(classes) if classes is not None else None
    order = [c for c in known if c in by_class and (wanted is None or c in wanted)]
    extras = sorted(
        (c for c in by_class if c not in known and (wanted is None or c in wanted)),
        key=lambda c: "" if c is None else str(c),
    )

    out: list[BlindDoc] = []
    for cls in [*order, *extras]:
        items = sorted(by_class[cls], key=lambda d: d.filename)
        random.Random(f"{seed}:{cls}").shuffle(items)
        out.extend(items[: max(0, int(per_class))])
    return out
