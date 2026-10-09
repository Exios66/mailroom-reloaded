"""``sandbox/content.lock``: the pin, and the bundle sha256 verifier."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

FIELDS = (
    "repo",
    "tag",
    "commit",
    "bundle_sha256",
    "schema_version",
    "dataset_revision",
)
DEFAULT_LOCK = Path("sandbox/content.lock")


class LockError(Exception):
    pass


@dataclass(frozen=True)
class ContentLock:
    repo: str
    tag: str
    commit: str
    bundle_sha256: str
    schema_version: str
    dataset_revision: str

    @classmethod
    def read(cls, path: Path | str = DEFAULT_LOCK) -> ContentLock:
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise LockError(f"cannot read lock {path}: {exc}") from exc
        missing = [f for f in FIELDS if f not in data]
        extra = [k for k in data if k not in FIELDS]
        if missing or extra:
            raise LockError(f"lock fields: missing {missing}, unexpected {extra}")
        lock = cls(**{f: str(data[f]) for f in FIELDS})
        lock.validate()
        return lock

    def validate(self) -> None:
        sha = self.bundle_sha256
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise LockError("bundle_sha256 must be 64 lowercase hex chars")
        if not (7 <= len(self.commit) <= 40):
            raise LockError("commit must be a 7-40 char sha")

    def write(self, path: Path | str = DEFAULT_LOCK) -> None:
        """Validate and overwrite the lock file as UTF-8 text.

        Raise ``LockError`` for invalid hashes and propagate filesystem errors;
        parent directories are not created.
        """
        self.validate()
        lines = [
            f"{k}: {v!r}" if k == "schema_version" else f"{k}: {v}"
            for k, v in asdict(self).items()
        ]
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_bundle(bundle: Path | str, lock: ContentLock) -> str:
    """Return the bundle's sha256, raising LockError if it differs from the lock."""
    actual = sha256_file(bundle)
    if actual != lock.bundle_sha256:
        raise LockError(
            f"bundle sha256 {actual} does not match lock {lock.bundle_sha256}"
        )
    return actual
