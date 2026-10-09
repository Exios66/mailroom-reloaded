"""Exercise compressed archives and mock only the network boundary."""

import io
import json
import tarfile
from unittest.mock import Mock

import pytest

from mailroom_reloaded.sandbox.content import bundle
from mailroom_reloaded.sandbox.content.lock import LockError


@pytest.mark.parametrize("name", ["content.json", "./content.json"])
def test_read_metadata_after_unrelated_member(make_bundle, name):
    """Find root content metadata after other archive members, with either root spelling."""
    meta = {"schema_version": "2.0", "version": "0.5.0"}
    path = make_bundle([("README", b"test pack"), (name, json.dumps(meta).encode())])
    assert bundle.read_content_json(str(path)) == meta


def test_missing_metadata_is_rejected(make_bundle):
    """Reject archives whose content metadata exists only in a nested directory."""
    path = make_bundle([("nested/content.json", b"{}")])
    with pytest.raises(ValueError, match="no content.json"):
        bundle.read_content_json(path)


def test_malformed_metadata_is_rejected(make_bundle):
    """Propagate JSON decoding errors from malformed bundle metadata."""
    path = make_bundle([("content.json", b"not json")])
    with pytest.raises(json.JSONDecodeError):
        bundle.read_content_json(path)


def test_extracts_directories_and_nested_binary_files(make_bundle, tmp_path):
    """Preserve empty directories and binary payloads when extracting a bundle."""
    directory = tarfile.TarInfo("empty")
    directory.type = tarfile.DIRTYPE
    path = make_bundle([(directory, b""), ("nested/docs/file.bin", b"\x00\xff\n")])
    dest = tmp_path / "out"
    assert bundle.extract_bundle(str(path), str(dest)) == dest.resolve()
    assert (dest / "empty").is_dir()
    assert (dest / "nested/docs/file.bin").read_bytes() == b"\x00\xff\n"


@pytest.mark.parametrize("kind", ["parent", "absolute", "symlink", "hardlink", "fifo", "device"])
def test_unsafe_members_are_rejected_without_writing_outside_destination(
    make_bundle, tmp_path, kind,
):
    """Reject escaping paths, links, and special files while preserving outside data."""
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    member = tarfile.TarInfo("unsafe")
    payload = b"overwrite"
    if kind == "parent":
        member.name = "../outside"
    elif kind == "absolute":
        member.name = str(outside)
    else:
        member.type = {
            "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
            "fifo": tarfile.FIFOTYPE, "device": tarfile.CHRTYPE,
        }[kind]
        member.linkname = "../outside"
        payload = b""
    path = make_bundle([(member, payload)])
    dest = tmp_path / "out"
    with pytest.raises(ValueError, match="refusing unsafe bundle member"):
        bundle.extract_bundle(path, dest)
    assert outside.read_bytes() == b"keep"
    assert list(dest.iterdir()) == []


def test_existing_destination_symlink_cannot_escape(make_bundle, tmp_path):
    """Reject archive paths that escape through an existing destination symlink."""
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "linked").symlink_to(outside, target_is_directory=True)
    path = make_bundle([("linked/escaped.txt", b"unexpected")])
    with pytest.raises(ValueError, match="refusing unsafe bundle member"):
        bundle.extract_bundle(path, dest)
    assert not (outside / "escaped.txt").exists()


def test_hash_mismatch_is_rejected_before_creating_destination(tmp_path, content_lock):
    """Check the pinned digest before creating the extraction directory."""
    path = tmp_path / "not-an-archive"
    path.write_bytes(b"invalid archive with the wrong digest")
    dest = tmp_path / "out"
    with pytest.raises(LockError, match="does not match lock"):
        bundle.extract_bundle(path, dest, content_lock)
    assert not dest.exists()


@pytest.mark.parametrize("url", ["http://example.invalid/a", "file:///etc/passwd", "ftp://host/a"])
def test_fetch_refuses_non_https_before_opening_anything(monkeypatch, tmp_path, url):
    """Reject unsupported URL schemes without opening a connection or output file."""
    request = Mock(side_effect=AssertionError("unexpected network access"))
    monkeypatch.setattr(bundle.urllib.request, "urlopen", request)
    out = tmp_path / "download"
    with pytest.raises(ValueError, match="only https"):
        bundle.fetch_url(url, out)
    request.assert_not_called()
    assert not out.exists()


def test_fetch_writes_response_bytes_and_passes_timeout(monkeypatch, tmp_path):
    """Forward the timeout, save response bytes, and close the download stream."""
    response = io.BytesIO(b"\x00\xffcompressed payload")
    request = Mock(return_value=response)
    monkeypatch.setattr(bundle.urllib.request, "urlopen", request)
    out = tmp_path / "download"
    assert bundle.fetch_url("https://example.invalid/a", str(out), timeout=2.5) == out
    request.assert_called_once_with("https://example.invalid/a", timeout=2.5)
    assert out.read_bytes() == b"\x00\xffcompressed payload"
    assert response.closed


def test_fetch_failure_preserves_existing_file(monkeypatch, tmp_path):
    """Leave an existing output untouched when opening the download fails."""
    monkeypatch.setattr(bundle.urllib.request, "urlopen", Mock(side_effect=OSError("offline")))
    out = tmp_path / "download"
    out.write_bytes(b"keep")
    with pytest.raises(OSError, match="offline"):
        bundle.fetch_url("https://example.invalid/a", out)
    assert out.read_bytes() == b"keep"
