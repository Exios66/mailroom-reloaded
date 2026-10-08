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
        self.base = Path(base)

    def _dir(self, *parts: str) -> Path:
        d = self.base.joinpath(*parts)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def inbox(self) -> Path:
        return self._dir("inbox")

    def processing(self, worker_id: str) -> Path:
        return self._dir("processing", worker_id)

    @property
    def classified(self) -> Path:
        return self._dir("classified")

    @property
    def review(self) -> Path:
        return self._dir("review")

    @property
    def failed(self) -> Path:
        return self._dir("failed")

    @property
    def archive(self) -> Path:
        return self._dir("archive")

    @property
    def manifests(self) -> Path:
        return self._dir("manifests")

    def claim(self, path: Path, worker_id: str) -> Path | None:
        """Atomically move ``path`` into processing/<worker_id>/; None if another worker won."""
        path = Path(path)
        dest = self.processing(worker_id) / path.name
        try:
            os.rename(path, dest)
        except FileNotFoundError:
            return None
        return dest

    def move(self, path: Path, bin_name: str) -> Path:
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
    path = bins.manifests / f"{doc_id}.json"
    try:
        return Manifest.model_validate_json(path.read_text())
    except FileNotFoundError:
        return None
