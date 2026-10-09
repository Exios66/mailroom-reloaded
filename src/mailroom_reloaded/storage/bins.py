from __future__ import annotations

import hashlib
import os
import tempfile
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
        """Atomically move ``path`` into processing/<worker_id>/ and return the new path.

        The destination name gets a uuid prefix. Create the worker directory first.
        A missing source returns ``None``; other filesystem errors propagate.
        """
        path = Path(path)
        dest = self.processing(worker_id) / f"{uuid.uuid4().hex}_{path.name}"
        try:
            os.rename(path, dest)
        except FileNotFoundError:
            return None
        return dest

    def enqueue(self, content: bytes, filename: str) -> Path:
        """Publish a complete upload atomically without replacing queued files.

        Hidden staging files are ignored by the watcher. Linking the closed file
        publishes it exclusively, so a concurrent watcher can claim it immediately.
        Callers must derive document identity from content, never reopen the path.
        Publication uses ``os.link``, so the inbox must be on a filesystem that
        supports hard links (some bind mounts and network shares do not); there is
        no copy fallback, and the error propagates to the caller.
        """
        if (not filename or filename.startswith(".") or "\x00" in filename
                or "/" in filename or "\\" in filename):
            raise ValueError("Invalid file name")
        inbox = self.inbox
        stem, suffix = Path(filename).stem, Path(filename).suffix
        fd, name = tempfile.mkstemp(prefix=".upload-", dir=inbox)
        staging = Path(name)
        try:
            os.fchmod(fd, 0o644)  # mkstemp creates 0600; match the previous 0644 uploads
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            counter = 0
            while True:
                dest = inbox / (filename if counter == 0 else f"{stem}-{counter}{suffix}")
                try:
                    os.link(staging, dest)
                    return dest
                except FileExistsError:
                    counter += 1
        finally:
            staging.unlink(missing_ok=True)

    def move(self, path: Path, bin_name: str) -> Path:
        """Move a file into a named bin under a uuid-prefixed name and return the new path.

        Unknown bin names raise ``ValueError``; directory creation and move
        errors propagate.
        """
        if bin_name not in _BIN_NAMES:
            raise ValueError(f"unknown bin: {bin_name}")
        path = Path(path)
        dest = getattr(self, bin_name) / f"{uuid.uuid4().hex}_{path.name}"
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
