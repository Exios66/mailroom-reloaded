"""Lock validation and streaming bundle integrity checks."""

import hashlib
from dataclasses import asdict, replace

import pytest
import yaml

from mailroom_reloaded.sandbox.content.lock import ContentLock, LockError, sha256_file


@pytest.mark.parametrize("sha", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64])
def test_invalid_digest_cannot_overwrite_lock(content_lock, lock_path, sha):
    original = lock_path.read_bytes()
    with pytest.raises(LockError, match="64 lowercase hex"):
        replace(content_lock, bundle_sha256=sha).write(lock_path)
    assert lock_path.read_bytes() == original


@pytest.mark.parametrize("length", [6, 41])
def test_commit_length_outside_limits_is_rejected(content_lock, length):
    with pytest.raises(LockError, match="7-40 char sha"):
        replace(content_lock, commit="a" * length).validate()


@pytest.mark.parametrize("length", [7, 40])
def test_commit_length_boundaries_roundtrip(content_lock, tmp_path, length):
    lock = replace(content_lock, commit="a" * length)
    path = tmp_path / "pin"
    lock.write(path)
    assert ContentLock.read(path) == lock


@pytest.mark.xfail(
    strict=True,
    raises=LockError,
    reason="ContentLock.write leaves numeric digests unquoted; YAML drops leading zeros on read",
)
def test_numeric_digest_roundtrip_preserves_leading_zeros(content_lock, tmp_path):
    lock = replace(content_lock, bundle_sha256="0" * 64)
    path = tmp_path / "pin"
    lock.write(path)
    assert ContentLock.read(path) == lock


@pytest.mark.parametrize("field", [
    "repo", "tag", "commit", "bundle_sha256", "schema_version", "dataset_revision",
])
def test_each_lock_field_is_required(content_lock, tmp_path, field):
    data = asdict(content_lock)
    del data[field]
    path = tmp_path / "pin"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(LockError, match=rf"missing \['{field}'\]"):
        ContentLock.read(path)


def test_unknown_lock_fields_are_rejected(content_lock, tmp_path):
    path = tmp_path / "pin"
    path.write_text(yaml.safe_dump({**asdict(content_lock), "branch": "main"}))
    with pytest.raises(LockError, match="unexpected \\['branch'\\]"):
        ContentLock.read(path)


@pytest.mark.parametrize("contents,message", [
    (None, "cannot read lock"), ("repo: [", "cannot read lock"), ("", "lock fields"),
])
def test_unreadable_malformed_or_empty_lock(tmp_path, contents, message):
    path = tmp_path / "pin"
    if contents is not None:
        path.write_text(contents)
    with pytest.raises(LockError, match=message):
        ContentLock.read(path)


def test_read_normalizes_yaml_numeric_metadata(content_lock, tmp_path):
    data = {**asdict(content_lock), "schema_version": 2.0, "dataset_revision": 123}
    path = tmp_path / "pin"
    path.write_text(yaml.safe_dump(data))
    lock = ContentLock.read(path)
    assert lock.schema_version == "2.0"
    assert lock.dataset_revision == "123"


def test_read_validates_digest(content_lock, tmp_path):
    path = tmp_path / "pin"
    path.write_text(yaml.safe_dump({**asdict(content_lock), "bundle_sha256": "bad"}))
    with pytest.raises(LockError, match="64 lowercase hex"):
        ContentLock.read(path)


@pytest.mark.parametrize("size", [0, 1 << 20, (1 << 20) + 17])
def test_sha256_includes_bytes_across_chunk_boundary(tmp_path, size):
    payload = (b"0123456789abcdef" * (size // 16 + 1))[:size]
    path = tmp_path / "bundle"
    path.write_bytes(payload)
    assert sha256_file(str(path)) == hashlib.sha256(payload).hexdigest()
