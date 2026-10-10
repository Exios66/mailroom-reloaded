"""Blind/ground-truth dataset loader and nested sampler (spec section 8, Task 20).

Ground truth comes from ``Lucius-Morningstar/mailroom-dataset`` at revision
``ed7576b6`` (SAND-37). Configs are ``default`` (blind: ``filename``,
``doc_text``, ``prompt``, ``metadata`` -- no labels, no hash) and
``ground_truth`` (labels in the ``gt_fields`` JSON blob, plus ``expected`` /
``expected_subclass`` / ``content_sha256``), joined on ``filename``;
``content_sha256`` (``sha256(doc_text)``) is verified at load so a corrupted or
truncated document cannot silently reach the pipeline.

The blind side carries no label fields at all (``BlindDoc``). Ground truth lives
only here and is reachable from just two places: the ``grade`` step and the
``get_ground_truth`` tool (spec section 8, "The ground truth stays out of the
agents' path by construction").
"""

from __future__ import annotations

import ast
import hashlib
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_REVISION",
    "REPO",
    "TRAINING_REPO",
    "BlindDoc",
    "DatasetIntegrityError",
    "EvalContext",
    "GroundTruth",
    "bert_manifest_overlap",
    "load_manifest_sha256",
    "load_split",
    "sample",
]

REPO = "Lucius-Morningstar/mailroom-dataset"
DEFAULT_REVISION = "ed7576b6"

#: The ModernBERT training set: its ``documents`` config carries the
#: ``content_sha256`` values the spec section 5 leakage check compares against.
TRAINING_REPO = "Lucius-Morningstar/mailroom-modernbert-training"
TRAINING_CONFIG = "documents"

#: Candidate source columns for the blind text and its declared hash.
_TEXT_KEYS = ("doc_text", "text", "content", "document", "body")
_SHA_KEYS = ("content_sha256", "sha256", "content_hash")

#: Ground-truth columns accepted as a fallback for label fields when the live
#: ``gt_fields`` blob is absent (local JSONL fixtures).
_FIELD_KEYS = ("fields", "extraction", "ground_truth_fields")

#: Richer ground-truth columns that encode the escalation decision the two
#: boolean labels are meant to carry (issue #14). ``expected_stage == "review"``
#: is the canonical review signal; a non-empty ``expected_post_retry_state``
#: (``human_review``/``archived``) is the canonical retry signal. ``review_reason``
#: alone is *not* sufficient -- fixtures pair a reason with ``review_expected ==
#: "false"`` (e.g. ``ambiguous``), including when ``expected_stage`` is absent.
_STAGE_KEYS = ("expected_stage",)
_POST_RETRY_KEYS = ("expected_post_retry_state",)


class DatasetIntegrityError(Exception):
    """A dataset document has invalid identity or content."""


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
    """Accept a dict or decode a JSON object string; otherwise return {}."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _parse_json_container(value: Any) -> Any:
    """Parse a stringified JSON list/object; leave scalars/bare strings alone."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{" or stripped[-1] not in "]}":
        return value
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return value
    return parsed if isinstance(parsed, (list, dict)) else value


def _parse_gt_fields(raw: Any) -> dict[str, Any]:
    """Parse a Hub ``gt_fields`` value (JSON/Python string or mapping).

    Values that are themselves stringified JSON containers (the Hub shape --
    ``"cuad_clause_labels": "{}"``) are parsed one level in. Anything
    unparseable degrades to ``{}`` so a malformed row cannot abort the load.
    """
    if isinstance(raw, dict):
        obj: Any = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            try:
                obj = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                return {}
    else:
        return {}
    if not isinstance(obj, dict):
        return {}
    return {str(key): _parse_json_container(value) for key, value in obj.items()}


def _as_list(value: Any) -> list[str]:
    """Coerce a clause-label field to a list of label names.

    The live Hub shape is a mapping ``{clause: [spans...]}``; fixtures use a
    plain list. A mapping yields its keys, a list yields its stringified
    items, and a stringified container is parsed first.
    """
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    if isinstance(value, dict):
        return [str(k) for k in value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    if value is None:
        return []
    return [str(value)]


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


def _as_flag(value: Any) -> bool | None:
    """Parse a boolean ground-truth label.

    The Hub serves ``retry_expected``/``review_expected`` as the strings
    ``"true"``/``"false"``; ``None``/blank stay ``None`` and booleans/ints pass
    through. Returning a ``bool`` (not the truthy string ``"false"``) is the
    contract the ``GroundTruth`` dataclass declares.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        return text in {"1", "true", "t", "yes", "y"} if text else None
    return bool(value)


def _resolve_flag(explicit: bool | None, derived: bool | None) -> bool | None:
    """Merge an explicit boolean label with one derived from richer columns.

    A positive derived signal wins over an explicit ``False`` (the richer
    columns are authoritative per issue #14); otherwise the explicit value is
    kept, and an explicit ``None`` with no derived signal stays ``None``.
    """
    if explicit is True or derived is True:
        return True
    if explicit is False:
        return False
    return None


def _metadata_sha(row: Any) -> Any:
    """``content_sha256`` from the blind row's ``metadata`` blob, if present."""
    meta = _row_get(row, ("metadata",))
    if isinstance(meta, str):
        meta = _as_dict(meta)
    if isinstance(meta, dict):
        return _row_get(meta, _SHA_KEYS)
    return None


def _blind_from_row(row: Any, declared: Any = None) -> BlindDoc:
    """Build a verified :class:`BlindDoc`.

    ``declared`` is the hash from the joined ``ground_truth`` row (the live
    layout); when absent the call falls back to a hash on the blind row
    itself, then to the blind ``metadata`` blob (the historical layout).
    """
    filename = str(_row_get(row, ("filename", "file", "name"), "")).strip()
    text = str(_row_get(row, _TEXT_KEYS, "") or "")
    if declared is None:
        declared = _row_get(row, _SHA_KEYS)
    if declared is None:
        declared = _metadata_sha(row)
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
    """Normalize label aliases and fields, requiring a nonempty filename."""
    filename = str(_row_get(row, ("filename", "file", "name"), "")).strip()
    if not filename:
        raise DatasetIntegrityError("ground-truth row is missing a filename")
    parsed = _parse_gt_fields(_row_get(row, ("gt_fields",)))
    fields = parsed or _as_dict(_row_get(row, _FIELD_KEYS))

    def clause(key: str, fallback: tuple[str, ...]) -> list[str]:
        """Read parsed clause labels, falling back to row aliases when empty."""
        value = parsed.get(key)
        if value is None or value == [] or value == {}:
            value = _row_get(row, fallback)
        return _as_list(value)

    stage = _row_get(row, _STAGE_KEYS)
    stage_text = str(stage).strip().lower() if stage is not None else ""
    post_retry = _row_get(row, _POST_RETRY_KEYS)
    post_text = str(post_retry).strip() if post_retry is not None else ""
    # A reason alone never establishes a review, even without expected_stage.
    derived_review = stage_text == "review"
    derived_retry = bool(post_text)

    return GroundTruth(
        filename=filename,
        expected=_row_get(row, ("expected", "expected_doc_type", "label", "doc_type")),
        expected_subclass=_row_get(row, ("expected_subclass", "doc_subclass", "subclass")),
        fields=fields,
        cuad_clause_labels=clause("cuad_clause_labels", ("cuad_clause_labels", "cuad_clauses")),
        maud_clause_labels=clause("maud_clause_labels", ("maud_clause_labels", "maud_clauses")),
        retry_expected=_resolve_flag(
            _as_flag(_row_get(row, ("retry_expected",))), derived_retry
        ),
        review_expected=_resolve_flag(
            _as_flag(_row_get(row, ("review_expected",))), derived_review
        ),
        expected_stage=stage,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Decode nonblank UTF-8 JSONL lines, propagating read and parse errors."""
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------- §5 manifest


def load_manifest_sha256(manifest: Any) -> set[str]:
    """Content hashes from a local BERT-training ``documents`` manifest.

    Accepts a JSONL file or a directory of JSONL files (the training set's
    ``documents`` export). Best-effort and offline by design: unreadable or
    malformed inputs are skipped, and no Hub network access is attempted.
    """
    base = Path(manifest)
    if base.is_file():
        files = [base]
    elif base.is_dir():
        files = sorted(base.glob("*.jsonl"))
    else:
        return set()
    shas: set[str] = set()
    for path in files:
        try:
            rows = _read_jsonl(path)
        except (OSError, ValueError):
            continue
        for row in rows:
            sha = _row_get(row, _SHA_KEYS)
            if sha:
                shas.add(str(sha))
    return shas


def bert_manifest_overlap(docs: list[BlindDoc], manifest: Any) -> dict[str, bool]:
    """Report which blind documents appear in the BERT training manifest.

    Spec §5 leakage check: a ``True`` entry means the document's
    ``content_sha256`` is in ``TRAINING_REPO``'s ``documents`` set, so its
    fast-path accuracy must be excluded from the KPIs. ``manifest`` is a
    local JSONL file/dir; an unreadable manifest yields all-``False``.
    """
    known = load_manifest_sha256(manifest)
    return {doc.filename: doc.content_sha256 in known for doc in docs}


# --------------------------------------------------------------------------- loaders


def _load_local(local_dir: Path) -> tuple[list[BlindDoc], dict[str, GroundTruth]]:
    """Load and join the local blind and ground-truth JSONL exports."""
    base = Path(local_dir)
    blind_rows = _read_jsonl(base / "default.jsonl")
    gt_rows = _read_jsonl(base / "ground_truth.jsonl")
    return _join(blind_rows, gt_rows)


def _load_live(
    revision: str, split: str, repo: str = REPO
) -> tuple[list[BlindDoc], dict[str, GroundTruth]]:
    """Load and join Hub configs, requiring the optional datasets package."""
    try:
        import datasets
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "the 'eval' extra is required for live dataset loading: "
            "pip install 'mailroom-reloaded[eval]'"
        ) from exc

    blind_rows = _load_hf_config(datasets, "default", revision, split, repo)
    gt_rows = _load_hf_config(datasets, "ground_truth", revision, split, repo)
    return _join(blind_rows, gt_rows)


def _load_labeled(
    revision: str, split: str, config: str, repo: str
) -> tuple[list[BlindDoc], dict[str, GroundTruth]]:
    """Load a self-contained labeled config (``fixtures``/``bundles``).

    Those configs carry ``doc_text`` and the label columns in one row, so no
    blind/ground-truth join is needed. A config without a declared
    ``content_sha256`` (the ``fixtures`` shape) has its hash computed from the
    text; a declared hash is still verified. This is the positive-label source
    the gate/Jev calibration needs (issue #14) without altering the core
    ``ground_truth`` config.
    """
    try:
        import datasets
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "the 'eval' extra is required for live dataset loading: "
            "pip install 'mailroom-reloaded[eval]'"
        ) from exc

    docs: list[BlindDoc] = []
    gts: dict[str, GroundTruth] = {}
    for row in _load_hf_config(datasets, config, revision, split, repo):
        gt = _ground_truth_from_row(row)
        if gt.filename in gts:
            raise DatasetIntegrityError(f"Duplicate ground truth filename: {gt.filename}")
        text = str(_row_get(row, _TEXT_KEYS, "") or "")
        declared = _row_get(row, _SHA_KEYS)
        if declared is None:
            declared = _metadata_sha(row)
        docs.append(_blind_from_row(row, declared if declared is not None else sha256_text(text)))
        gts[gt.filename] = gt
    return docs, gts


def _load_hf_config(
    datasets: Any, config: str, revision: str, split: str, repo: str = REPO
) -> list[Any]:
    """Load a Hub split, falling back to filtering rows by their split field."""
    try:
        dataset = datasets.load_dataset(repo, config, revision=revision, split=split)
    except Exception:
        dataset = datasets.load_dataset(repo, config, revision=revision)
        splits = dataset.values() if isinstance(dataset, Mapping) else [dataset]
        rows = [
            row for subset in splits for row in subset
            if _row_get(row, ("split",)) == split
        ]
        if not rows:
            raise
        return rows
    return list(dataset)


def _join(
    blind_rows: list[Any], gt_rows: list[Any]
) -> tuple[list[BlindDoc], dict[str, GroundTruth]]:
    """Index labels by filename and verify blind text against available hashes."""
    pairs = [(_ground_truth_from_row(row), _row_get(row, _SHA_KEYS)) for row in gt_rows]
    gts = {gt.filename: gt for gt, _ in pairs}
    sha_by_name = {gt.filename: sha for gt, sha in pairs if sha is not None}
    docs: list[BlindDoc] = []
    for row in blind_rows:
        name = str(_row_get(row, ("filename", "file", "name"), "")).strip()
        docs.append(_blind_from_row(row, sha_by_name.get(name)))
    blind_names = {doc.filename for doc in docs}
    missing_truth = blind_names - gts.keys()
    missing_docs = gts.keys() - blind_names
    if missing_truth or missing_docs:
        raise DatasetIntegrityError(
            f"Filename mismatch: missing ground truth for {sorted(missing_truth)}; "
            f"missing blind documents for {sorted(missing_docs)}"
        )
    return docs, gts


def load_split(
    revision: str = DEFAULT_REVISION,
    split: str = "test",
    *,
    local_dir: Path | None = None,
    repo: str | None = None,
    config: str | None = None,
) -> tuple[list[BlindDoc], dict[str, GroundTruth]]:
    """Load the blind docs and ground truth for one split.

    ``local_dir`` reads ``default.jsonl`` / ``ground_truth.jsonl`` (tests and
    offline runs); otherwise ``datasets.load_dataset`` is used behind the
    ``eval`` extra. Every blind document's ``content_sha256`` is verified against
    its bytes and a mismatch raises :class:`DatasetIntegrityError`.

    ``repo`` overrides the default Hub repository (e.g. the fixtures adaptation
    ``Lucius-Morningstar/mailroom-reloaded-fixtures``). ``config`` loads a
    single self-contained labeled config (``fixtures``/``bundles``) instead of
    the ``default`` + ``ground_truth`` join -- the positive-label source the
    gate/Jev calibration needs (issue #14).
    """
    if local_dir is not None:
        return _load_local(Path(local_dir))
    if config is not None:
        return _load_labeled(revision, split, config, repo or REPO)
    return _load_live(revision, split, repo or REPO)


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
