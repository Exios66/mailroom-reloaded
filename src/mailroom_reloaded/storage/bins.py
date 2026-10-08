from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from mailroom_reloaded.schemas.manifest import Manifest

_BIN_NAMES = ("inbox", "classified", "review", "failed", "archive", "manifests")


def doc_id_for(path: Path) -> str:
    """First 16 hex characters of the file's content sha256."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


class Bins:
    """Filesystem bins under ``base``; directories are created on demand."""

    def __init__(self, base: Path):
        """Store the base path; directories are created when accessed."""
        self.base = Path(base)

    def _dir(self, *parts: str) -> Path:
        """Create a directory and its parents under the base path, then return it."""
        d = self.base.joinpath(*parts)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def inbox(self) -> Path:
        """Return the inbox directory, creating it and its parents if needed."""
        return self._dir("inbox")

    def processing(self, worker_id: str) -> Path:
        """Return the worker processing directory, creating it and its parents if needed."""
        return self._dir("processing", worker_id)

    @property
    def classified(self) -> Path:
        """Return the classified directory, creating it and its parents if needed."""
        return self._dir("classified")

    @property
    def review(self) -> Path:
        """Return the review directory, creating it and its parents if needed."""
        return self._dir("review")

    @property
    def failed(self) -> Path:
        """Return the failed directory, creating it and its parents if needed."""
        return self._dir("failed")

    @property
    def archive(self) -> Path:
        """Return the archive directory, creating it and its parents if needed."""
        return self._dir("archive")

    @property
    def manifests(self) -> Path:
        """Return the manifests directory, creating it and its parents if needed."""
        return self._dir("manifests")

    def claim(self, path: Path, worker_id: str) -> Path | None:
        """Move ``path`` atomically into processing/<worker_id>/ and return its path.

        Create the worker directory first. A missing source or destination during
        rename returns ``None``; other filesystem errors propagate. An existing
        destination file with the same name may be replaced.
        """
        path = Path(path)
        dest = self.processing(worker_id) / path.name
        try:
            os.rename(path, dest)
        except FileNotFoundError:
            return None
        return dest

    def move(self, path: Path, bin_name: str) -> Path:
        """Move a file to a named bin, replacing any file with the same name.

        Return the destination path. Unknown bin names raise ``ValueError``;
        directory creation and move errors propagate.
        """
        if bin_name not in _BIN_NAMES:
            raise ValueError(f"unknown bin: {bin_name}")
        path = Path(path)
        dest = getattr(self, bin_name) / path.name
        os.replace(path, dest)
        return dest


def save_manifest(bins: Bins, m: Manifest) -> Path:
    """Atomic write: temp file in the manifests dir, then rename."""
    path = bins.manifests / f"{m.doc_id}.json"
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(m.model_dump_json(indent=2))
    os.replace(tmp, path)
    return path


def load_manifest(bins: Bins, doc_id: str) -> Manifest | None:
    """Load a document manifest, returning ``None`` when its file is missing.

    Create the manifests directory if needed. Other filesystem errors and
    Pydantic JSON or model validation errors propagate.
    """
    path = bins.manifests / f"{doc_id}.json"
    try:
        return Manifest.model_validate_json(path.read_text())
    except FileNotFoundError:
        return None
