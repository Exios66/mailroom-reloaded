"""Load and validate a materialized content directory or the committed smoke set.

Two layouts are accepted: a full content dir (``content.json`` at the root) and a
smoke export (``manifest.json`` with schema ``mailroom.smoke_export/v1``).
No network access happens here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mailroom_reloaded.sandbox.content.compat import (
    SUPPORTED_SCHEMA_MAJOR,
    CompatError,
    check_compat,
    schema_major,
)

SMOKE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "smoke"


def schemas_dir() -> Path:
    """Repo-root ``schemas/`` (dev checkout) or the copy shipped in the wheel."""
    here = Path(__file__).resolve()
    for cand in (here.parents[4] / "schemas", here.parents[1] / "schemas"):
        if (cand / "scenario.v2.json").is_file():
            return cand
    raise FileNotFoundError("schemas/ not found")


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class ContentSet:
    root: Path
    kind: str  # "content" | "smoke"
    meta: dict
    scenarios: dict[str, dict]
    registry: dict
    personas: dict[str, dict]
    gen_specs: dict[str, dict]
    report: ValidationReport


def _validator(name: str):
    import jsonschema

    schema = json.loads((schemas_dir() / name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _check(v, obj: Any, label: str, rep: ValidationReport) -> None:
    rep.checked += 1
    for e in sorted(v.iter_errors(obj), key=lambda e: list(e.path)):
        loc = "/".join(map(str, e.path)) or "<root>"
        rep.errors.append(f"{label}: {loc}: {e.message}")


def _yaml_dir(d: Path, v, rep: ValidationReport, root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in sorted(d.rglob("*.yaml")) if d.is_dir() else []:
        obj = yaml.safe_load(p.read_text(encoding="utf-8"))
        _check(v, obj, str(p.relative_to(root)), rep)
        out[p.stem] = obj
    return out


def _verify_manifest(root: Path, manifest: dict, rep: ValidationReport) -> None:
    for rel, sha in manifest.get("files", {}).items():
        p = root / rel
        if not p.is_file():
            rep.errors.append(f"manifest: missing file {rel}")
        elif hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            rep.errors.append(f"manifest: sha256 mismatch {rel}")


def load_content(root: Path | str = SMOKE_DIR, *, strict: bool = False) -> ContentSet:
    """Load ``root``. Raises CompatError on incompatible metadata; schema and
    manifest problems are collected in ``report`` (raised as ValueError if ``strict``)."""
    root = Path(root)
    rep = ValidationReport()
    if (root / "content.json").is_file():
        kind, meta = (
            "content",
            json.loads((root / "content.json").read_text(encoding="utf-8")),
        )
        check_compat(meta)
        reg_path = root / "dist" / "registry.yaml"
        gen_dir = root / "gen" / "specs"
        pers_dir = root / "personas" / "behavior"
        scen_dir = root / "scenarios"
    elif (root / "manifest.json").is_file():
        kind, meta = (
            "smoke",
            json.loads((root / "manifest.json").read_text(encoding="utf-8")),
        )
        if meta.get("schema") != "mailroom.smoke_export/v1":
            raise CompatError(f"unknown smoke manifest schema {meta.get('schema')!r}")
        if schema_major(meta["schema_version"]) != SUPPORTED_SCHEMA_MAJOR:
            raise CompatError(
                f"smoke schema major {meta['schema_version']} unsupported"
            )
        _verify_manifest(root, meta, rep)
        reg_path = root / "registry.yaml"
        gen_dir = root / "gen"
        pers_dir = root / "personas" / "behavior"
        scen_dir = root / "scenarios"
    else:
        raise FileNotFoundError(f"{root}: neither content.json nor manifest.json")

    scenarios = _yaml_dir(scen_dir, _validator("scenario.v2.json"), rep, root)
    personas = _yaml_dir(pers_dir, _validator("persona_behavior.v1.json"), rep, root)
    gen_specs = _yaml_dir(gen_dir, _validator("gen_spec.v1.json"), rep, root)
    registry = (
        yaml.safe_load(reg_path.read_text(encoding="utf-8"))
        if reg_path.is_file()
        else {}
    )
    if registry:
        _check(
            _validator("registry.v1.json"),
            registry,
            str(reg_path.relative_to(root)),
            rep,
        )
    else:
        rep.errors.append(f"missing registry: {reg_path.relative_to(root)}")
    cs = ContentSet(root, kind, meta, scenarios, registry, personas, gen_specs, rep)
    if strict and not rep.ok:
        raise ValueError("content invalid:\n" + "\n".join(rep.errors))
    return cs
