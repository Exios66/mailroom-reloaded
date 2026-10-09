"""Small, local content packs and archives for sandbox unit tests."""

import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from mailroom_reloaded.sandbox.content.lock import ContentLock

EXAMPLES = Path(__file__).parent / "examples"


@pytest.fixture
def content_dir(tmp_path):
    """Create a valid content pack with one example of each document kind."""
    root = tmp_path / "content"
    root.mkdir()
    (root / "content.json").write_text(json.dumps({
        "version": "0.5.0", "schema_version": "2.0", "dataset_revision": "test-revision",
    }))
    for source, target in {
        "registry.yaml": "dist/registry.yaml",
        "scenario_A1.yaml": "scenarios/A/A1_status_inquiry.yaml",
        "persona_behavior.yaml": "personas/behavior/biller.yaml",
        "gen_spec.yaml": "gen/specs/vendor.yaml",
    }.items():
        dest = root / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / source, dest)
    return root


@pytest.fixture
def content_lock():
    """Return a synthetic content pin for local bundle and CLI tests."""
    return ContentLock("example/content", "v0.5.0", "abcdef0", "a" * 64, "2.0", "test-revision")


@pytest.fixture
def lock_path(tmp_path, content_lock):
    """Write the synthetic pin to a temporary lock file and return its path."""
    path = tmp_path / "content.lock"
    content_lock.write(path)
    return path


@pytest.fixture
def make_bundle(tmp_path):
    """Archive real tar members, including unsafe members for rejection tests."""
    zstandard = pytest.importorskip("zstandard")

    def make(members):
        """Write the supplied tar members and payloads to a temporary Zstandard bundle."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as archive:
            for member, payload in members:
                if isinstance(member, str):
                    member = tarfile.TarInfo(member)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        path = tmp_path / "bundle.tar.zst"
        path.write_bytes(zstandard.ZstdCompressor().compress(buf.getvalue()))
        return path

    return make
