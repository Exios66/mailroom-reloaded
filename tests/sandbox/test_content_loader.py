"""M6: loader, lock, compat and CLI. Acceptance: smoke loads with zero network."""
import hashlib
import io
import json
import socket
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mailroom_reloaded import cli
from mailroom_reloaded.sandbox.content import (
    CompatError,
    ContentLock,
    LockError,
    check_compat,
    load_content,
    verify_bundle,
)
from mailroom_reloaded.sandbox.content.loader import SMOKE_DIR

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


@pytest.fixture
def no_network(monkeypatch):
    """Fail immediately if a test attempts socket connection or name resolution."""
    def boom(*a, **k):
        """Reject a patched socket operation as unexpected network access."""
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def test_smoke_loads_with_zero_network(no_network):
    """Load and validate the six committed smoke scenarios without network access."""
    cs = load_content(SMOKE_DIR, strict=True)
    assert cs.kind == "smoke" and cs.report.ok
    assert len(cs.scenarios) == 6 and cs.registry["clients"]
    assert cs.meta["content_version"] == "0.5.0"


def test_smoke_budget():
    """Keep the committed smoke fixture files within the two MiB size budget."""
    assert sum(p.stat().st_size for p in SMOKE_DIR.rglob("*") if p.is_file()) <= 2 * 1024 * 1024


def test_tampered_smoke_detected(tmp_path):
    """Report a manifest digest mismatch after modifying a smoke template."""
    import shutil
    d = tmp_path / "s"
    shutil.copytree(SMOKE_DIR, d)
    (d / "templates" / "status_inquiry.j2").write_text("tampered")
    assert any("sha256 mismatch" in e for e in load_content(d).report.errors)


def test_seeded_lock():
    """Verify the committed lock identifies the expected smoke content release."""
    lock = ContentLock.read(ROOT / "sandbox" / "content.lock")
    assert lock.tag == "v0.5.0" and lock.commit.startswith("f650cfd")
    assert lock.bundle_sha256.startswith("7a32e86e") and lock.schema_version == "2.0"
    assert lock.dataset_revision == "ed7576b6"


def test_lock_roundtrip_and_bad_fields(tmp_path):
    """Round-trip the committed lock and reject a lock missing required fields."""
    lock = ContentLock.read(ROOT / "sandbox" / "content.lock")
    p = tmp_path / "l"
    lock.write(p)
    assert ContentLock.read(p) == lock
    p.write_text("repo: x\n")
    with pytest.raises(LockError):
        ContentLock.read(p)


def test_verify_bundle(tmp_path):
    """Accept matching bundle bytes and reject a mismatched pinned digest."""
    b = tmp_path / "b.bin"
    b.write_bytes(b"hello")
    good = ContentLock("r", "v1", "abcdef0", hashlib.sha256(b"hello").hexdigest(), "2.0", "x")
    assert verify_bundle(b, good) == good.bundle_sha256
    bad = ContentLock("r", "v1", "abcdef0", "0" * 64, "2.0", "x")
    with pytest.raises(LockError):
        verify_bundle(b, bad)


def test_compat():
    """Accept inclusive code version bounds and reject incompatible schemas or versions."""
    meta = {"schema_version": "2.0", "min_code_version": "0.2.0", "max_code_version": "0.3.0"}
    check_compat(meta, code="0.2.0")
    check_compat(meta, code="0.3.0")
    with pytest.raises(CompatError):
        check_compat({**meta, "schema_version": "3.0"}, code="0.2.0")
    with pytest.raises(CompatError):
        check_compat(meta, code="0.1.9")
    with pytest.raises(CompatError):
        check_compat(meta, code="0.4.0")


def test_cli_status_and_validate():
    """Confirm the seeded lock matches smoke content and CLI validation succeeds."""
    r = runner.invoke(cli.app, ["sandbox", "content", "status", "--lock", str(ROOT / "sandbox/content.lock")])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["smoke_matches_lock"] is True
    r = runner.invoke(cli.app, ["sandbox", "content", "validate"])
    assert r.exit_code == 0 and "ok: smoke" in r.output


def test_pull_url_requires_flag(tmp_path):
    """Reject URL pulls without explicit network permission."""
    r = runner.invoke(cli.app, ["sandbox", "content", "pull", "--url", "https://x/y",
                                "--lock", str(ROOT / "sandbox/content.lock"), "--dest", str(tmp_path / "d")])
    assert r.exit_code == 1


def _bundle(tmp_path, meta):
    """Return a temporary Zstandard tar bundle containing the supplied content metadata."""
    zstandard = pytest.importorskip("zstandard")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = json.dumps(meta).encode()
        ti = tarfile.TarInfo("content.json")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    p = tmp_path / "b.tar.zst"
    p.write_bytes(zstandard.ZstdCompressor().compress(buf.getvalue()))
    return p


def test_bump_then_pull_bundle(tmp_path):
    """Pin and pull a local bundle, then reject the same bundle after tampering."""
    meta = {"schema_version": "2.0", "dataset_revision": "abc", "version": "9.9.9",
            "min_code_version": "0.2.0", "max_code_version": "0.3.0"}
    b = _bundle(tmp_path, meta)
    lock = tmp_path / "content.lock"
    r = runner.invoke(cli.app, ["sandbox", "content", "bump", "--bundle", str(b), "--tag", "v9.9.9",
                                "--commit", "abcdef0", "--lock", str(lock)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli.app, ["sandbox", "content", "pull", "--from-bundle", str(b),
                                "--lock", str(lock), "--dest", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "out" / "content.json").is_file()
    b.write_bytes(b.read_bytes() + b"x")
    r = runner.invoke(cli.app, ["sandbox", "content", "pull", "--from-bundle", str(b),
                                "--lock", str(lock), "--dest", str(tmp_path / "out2")])
    assert r.exit_code == 1
