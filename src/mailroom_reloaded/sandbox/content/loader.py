"""Load and validate a materialized content directory or the committed smoke set.

Two layouts are accepted: a full content dir (``content.json`` at the root) and a
smoke export (``manifest.json`` with schema ``mailroom.smoke_export/v1``).
No network access happens here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
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
_READ_FAILED = object()


def schemas_dir() -> Path:
    """Return repo-root ``schemas/`` (dev checkout) or the copy shipped in the wheel.

    Raise FileNotFoundError if neither contains scenario.v2.json.
    """
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
        """Return whether no errors were collected, even if nothing was checked."""
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
    """Load a named schema as a Draft 2020-12 validator.

    Missing jsonschema, schema file access, and JSON decoding errors propagate.
    """
    import jsonschema

    schema = json.loads((schemas_dir() / name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _check(v, obj: Any, label: str, rep: ValidationReport) -> None:
    """Count one checked object and append labeled schema errors to ``rep``."""
    rep.checked += 1
    for e in sorted(v.iter_errors(obj), key=lambda e: list(e.path)):
        loc = "/".join(map(str, e.path)) or "<root>"
        rep.errors.append(f"{label}: {loc}: {e.message}")


def _read_document(path: Path, parse: Callable[[str], Any], rep: ValidationReport, root: Path):
    """Report content file read/parse failures and return a sentinel on failure."""
    try:
        return parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        rep.errors.append(f"{path.relative_to(root)}: {type(exc).__name__}: {exc}")
        return _READ_FAILED


def _yaml_dir(
    d: Path, v, rep: ValidationReport, root: Path, checked_files: set[Path] | None = None,
) -> dict[str, dict]:
    """Load recursive .yaml files by stem, recording schema errors in ``rep``.

    Return an empty mapping if ``d`` is absent. Duplicate stems retain the last
    file in sorted path order, including schema-invalid objects. Read and parse
    failures are reported and skipped; paths outside ``root`` raise ValueError when labeled.
    When ``checked_files`` is supplied, report and skip paths outside that set.
    """
    out: dict[str, dict] = {}
    for p in sorted(d.rglob("*.yaml")) if d.is_dir() else []:
        if checked_files is not None and p not in checked_files:
            rep.errors.append(f"manifest: unlisted file {p.relative_to(root)}")
            continue
        obj = _read_document(p, yaml.safe_load, rep, root)
        if obj is _READ_FAILED:
            continue
        _check(v, obj, str(p.relative_to(root)), rep)
        out[p.stem] = obj
    return out


def _verify_manifest(root: Path, manifest: dict, rep: ValidationReport) -> set[Path]:
    """Return paths whose digests were checked against manifest's ``files``.

    Append missing-file and digest errors; ``rep.checked`` is unchanged.
    Returned paths include digest mismatches for non-strict diagnostic loading.
    Errors reading existing files are reported and excluded from returned paths.
    """
    checked_files: set[Path] = set()
    for rel, sha in manifest.get("files", {}).items():
        p = root / rel
        if not p.is_file():
            rep.errors.append(f"manifest: missing file {rel}")
        else:
            try:
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError as exc:
                rep.errors.append(f"{rel}: {type(exc).__name__}: {exc}")
                continue
            if digest != sha:
                rep.errors.append(f"manifest: sha256 mismatch {rel}")
            checked_files.add(p)
    return checked_files


def load_content(root: Path | str = SMOKE_DIR, *, strict: bool = False) -> ContentSet:
    """Load a content directory or smoke export, defaulting to committed smoke fixtures.

    Prefer content.json over manifest.json when both exist. Return parsed data
    and a report of per-file read/parse errors, schema errors, smoke manifest digest/missing-file errors,
    unlisted loadable smoke files, and a missing or empty registry. Unlisted
    smoke files are excluded before parsing. ``strict`` raises ValueError for
    reported errors; otherwise invalid listed objects remain in the returned data.

    Incompatible metadata raises CompatError regardless of ``strict``. Missing
    metadata files or schemas raise FileNotFoundError. Unreadable or malformed
    metadata returns an empty content set with errors. Schema loading and missing
    jsonschema dependency errors propagate; a smoke manifest missing
    schema_version raises KeyError.
    """
    root = Path(root)
    rep = ValidationReport()
    checked_files = None
    if (root / "content.json").is_file():
        kind, meta_path = "content", root / "content.json"
    elif (root / "manifest.json").is_file():
        kind, meta_path = "smoke", root / "manifest.json"
    else:
        raise FileNotFoundError(f"{root}: neither content.json nor manifest.json")
    meta = _read_document(meta_path, json.loads, rep, root)
    if meta is _READ_FAILED:
        if strict:
            raise ValueError("content invalid:\n" + "\n".join(rep.errors))
        return ContentSet(root, kind, {}, {}, {}, {}, {}, rep)
    if kind == "content":
        check_compat(meta)
        reg_path = root / "dist" / "registry.yaml"
        gen_dir = root / "gen" / "specs"
        pers_dir = root / "personas" / "behavior"
        scen_dir = root / "scenarios"
    else:
        if meta.get("schema") != "mailroom.smoke_export/v1":
            raise CompatError(f"unknown smoke manifest schema {meta.get('schema')!r}")
        if schema_major(meta["schema_version"]) != SUPPORTED_SCHEMA_MAJOR:
            raise CompatError(f"smoke schema major {meta['schema_version']} unsupported")
        checked_files = _verify_manifest(root, meta, rep)
        reg_path = root / "registry.yaml"
        gen_dir = root / "gen"
        pers_dir = root / "personas" / "behavior"
        scen_dir = root / "scenarios"

    scenarios = _yaml_dir(scen_dir, _validator("scenario.v2.json"), rep, root, checked_files)
    personas = _yaml_dir(pers_dir, _validator("persona_behavior.v1.json"), rep, root, checked_files)
    gen_specs = _yaml_dir(gen_dir, _validator("gen_spec.v1.json"), rep, root, checked_files)
    registry = {}
    if reg_path.is_file():
        if checked_files is not None and reg_path not in checked_files:
            rep.errors.append(f"manifest: unlisted file {reg_path.relative_to(root)}")
        else:
            registry = _read_document(reg_path, yaml.safe_load, rep, root)
            if registry is _READ_FAILED:
                registry = {}
    if registry:
        _check(_validator("registry.v1.json"), registry, str(reg_path.relative_to(root)), rep)
    else:
        rep.errors.append(f"missing registry: {reg_path.relative_to(root)}")
    cs = ContentSet(root, kind, meta, scenarios, registry, personas, gen_specs, rep)
    if strict and not rep.ok:
        raise ValueError("content invalid:\n" + "\n".join(rep.errors))
    return cs
