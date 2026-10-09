"""Resolve and wrap the offline content pack for the sandbox server.

Accepts the committed smoke set, an extracted full bundle (``content.json`` at
the root, e.g. the result of ``mailroom sandbox content pull``) or an extracted
smoke export. Never touches the network.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from mailroom_reloaded.sandbox.content.loader import SMOKE_DIR, ContentSet, load_content
from mailroom_reloaded.sandbox.content.lock import ContentLock

__all__ = [
    "PolicyBundle",
    "SandboxContent",
    "load_sandbox_content",
    "resolve_content_spec",
]

POLICY_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "policy"
DEFAULT_PULL_DIR = Path(".sandbox-content")
DEFAULT_LOCK = Path("sandbox/content.lock")


class ContentSpecError(ValueError):
    """The ``--content`` argument could not be resolved to a content directory."""


def resolve_content_spec(
    spec: str, *, lock: Path = DEFAULT_LOCK, pull_dir: Path = DEFAULT_PULL_DIR
) -> Path:
    """``smoke`` | ``locked`` | a directory (or a ``content.lock`` pull result)."""
    if spec == "smoke":
        return SMOKE_DIR
    if spec == "locked":
        if not pull_dir.is_dir() or not (pull_dir / "content.json").is_file():
            raise ContentSpecError(
                f"--content locked needs a pulled bundle at {pull_dir}; run "
                "`mailroom sandbox content pull --from-bundle <file.tar.zst>` first "
                "(the server never downloads anything)"
            )
        pin = ContentLock.read(lock)
        meta = json.loads((pull_dir / "content.json").read_text(encoding="utf-8"))
        if f"v{meta.get('version')}" != pin.tag:
            raise ContentSpecError(
                f"{pull_dir} is content {meta.get('version')}, {lock} pins {pin.tag}"
            )
        return pull_dir
    path = Path(spec)
    if path.is_file() and path.name == "content.lock":
        return resolve_content_spec("locked", lock=path, pull_dir=pull_dir)
    if not path.is_dir():
        raise ContentSpecError(
            f"--content {spec!r}: not smoke|locked|an existing directory"
        )
    return path


def _sha(path: Path) -> str:
    """Return the SHA-256 hex digest of a file for policy provenance."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class PolicyBundle:
    ingress: dict
    recipient: dict
    send_schedule: dict
    delegation: dict[str, dict]
    source: str
    files: dict[str, str] = field(default_factory=dict)  # name -> sha256


def load_policy(root: Path | None) -> PolicyBundle:
    """Policy from the content dir when present, else the vendored v0.5.0 copies."""
    candidates = []
    if root is not None:
        candidates.append(
            (
                "content",
                root / "email" / "ingress_policy.yaml",
                root / "email" / "recipient_policy.yaml",
                root / "email" / "send_schedule.yaml",
                root / "protocol" / "delegation_matrix.csv",
            )
        )
    candidates.append(
        (
            "vendored",
            POLICY_FIXTURES / "ingress_policy.yaml",
            POLICY_FIXTURES / "recipient_policy.yaml",
            POLICY_FIXTURES / "send_schedule.yaml",
            POLICY_FIXTURES / "delegation_matrix.csv",
        )
    )
    for source, ing, rec, sched, deleg in candidates:
        if all(p.is_file() for p in (ing, rec, sched, deleg)):
            with deleg.open(encoding="utf-8", newline="") as fh:
                matrix = {r["issue_class"]: r for r in csv.DictReader(fh)}
            return PolicyBundle(
                ingress=yaml.safe_load(ing.read_text(encoding="utf-8")),
                recipient=yaml.safe_load(rec.read_text(encoding="utf-8")),
                send_schedule=yaml.safe_load(sched.read_text(encoding="utf-8")),
                delegation=matrix,
                source=source,
                files={p.name: _sha(p) for p in (ing, rec, sched, deleg)},
            )
    raise FileNotFoundError(
        "sandbox policy files not found (content dir or vendored copies)"
    )


@dataclass
class SandboxContent:
    root: Path
    cs: ContentSet
    policy: PolicyBundle
    personas: dict[str, dict]
    attachments: dict[str, Path]

    @property
    def kind(self) -> str:
        """Return the loaded content-set kind, such as smoke or full."""
        return self.cs.kind

    @property
    def registry_clients(self) -> dict[str, dict]:
        """Return registry clients, defaulting to an empty mapping."""
        return (self.cs.registry or {}).get("clients", {}) or {}

    def scenario_ids(self) -> list[str]:
        """Return smoke-set scenario order when specified, otherwise sorted names."""
        if self.cs.kind == "smoke":
            ids = (
                yaml.safe_load((self.root / "smoke_set.yaml").read_text("utf-8")) or {}
            ).get("scenario_ids")
            if ids:
                return [i for i in ids if i in self.cs.scenarios]
        return sorted(self.cs.scenarios)

    def template_path(self, name: str) -> Path | None:
        """Find a named Jinja template in either supported content layout."""
        for d in ("templates", "gen/templates"):
            p = self.root / d / f"{name}.j2"
            if p.is_file():
                return p
        return None

    def attachment_path(self, filename: str) -> Path | None:
        """Look up a packaged attachment by filename, returning None if absent."""
        return self.attachments.get(filename)

    def info(self) -> dict:
        """Summarize content metadata, validation results, and policy provenance."""
        meta = self.cs.meta
        return {
            "root": str(self.root),
            "kind": self.cs.kind,
            "version": meta.get("version") or meta.get("content_version"),
            "schema_version": meta.get("schema_version"),
            "dataset_revision": meta.get("dataset_revision"),
            "scenarios": len(self.cs.scenarios),
            "valid": self.cs.report.ok,
            "validation_errors": self.cs.report.errors[:20],
            "policy_source": self.policy.source,
            "policy_files": self.policy.files,
        }


def load_sandbox_content(root: Path | str = SMOKE_DIR) -> SandboxContent:
    """Load local content, policy, personas, and an attachment index without downloads."""
    root = Path(root)
    cs = load_content(root)
    personas: dict[str, dict] = {}
    pcsv = root / "personas" / "personas.csv"
    if pcsv.is_file():
        with pcsv.open(encoding="utf-8", newline="") as fh:
            personas = {r["persona_id"]: r for r in csv.DictReader(fh)}
    attachments: dict[str, Path] = {}
    for d in ("docs", "attachments"):
        base = root / d
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                if p.is_file() and p.name != "manifest.csv":
                    attachments.setdefault(p.name, p)
    return SandboxContent(root, cs, load_policy(root), personas, attachments)
