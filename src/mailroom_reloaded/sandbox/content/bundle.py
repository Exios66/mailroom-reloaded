"""Bundle (``.tar.zst``) helpers. Needs the ``zstandard`` module (``sandbox`` extra)."""

from __future__ import annotations

import io
import json
import tarfile
import urllib.request
from pathlib import Path

from mailroom_reloaded.sandbox.content.lock import ContentLock, verify_bundle


def _open(path: Path):
    try:
        import zstandard
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("zstandard is required: install the 'sandbox' extra") from exc
    raw = zstandard.ZstdDecompressor().stream_reader(open(path, "rb"))  # noqa: SIM115 - closed with the tar stream
    return tarfile.open(fileobj=raw, mode="r|")


def read_content_json(bundle: Path | str) -> dict:
    with _open(Path(bundle)) as tf:
        for m in tf:
            if m.name.lstrip("./") == "content.json":
                f = tf.extractfile(m)
                assert f is not None
                return json.loads(f.read())
    raise ValueError("bundle has no content.json")


def extract_bundle(bundle: Path | str, dest: Path | str, lock: ContentLock | None = None) -> Path:
    """Verify (when a lock is given) then safely extract into ``dest``."""
    if lock is not None:
        verify_bundle(bundle, lock)
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with _open(Path(bundle)) as tf:
        for m in tf:
            target = (dest / m.name).resolve()
            if not (m.isreg() or m.isdir()) or not target.is_relative_to(dest):
                raise ValueError(f"refusing unsafe bundle member {m.name!r}")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            f = tf.extractfile(m)
            assert f is not None
            target.write_bytes(f.read())
    return dest


def fetch_url(url: str, out: Path | str, *, timeout: float = 60.0) -> Path:
    """Optional download. Only reachable via ``pull --url --allow-network``."""
    if not url.startswith("https://"):
        raise ValueError("only https URLs are allowed")
    out = Path(out)
    with urllib.request.urlopen(url, timeout=timeout) as r, open(out, "wb") as f:
        f.write(io.BytesIO(r.read()).getbuffer())
    return out
