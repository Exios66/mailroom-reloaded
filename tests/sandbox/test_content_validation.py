"""Loader layouts, schema diagnostics, and smoke manifest integrity."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from mailroom_reloaded.sandbox.content import CompatError, load_content, loader


def test_full_content_layout_loads_each_document_kind(content_dir):
    content = load_content(str(content_dir), strict=True)
    assert content.root == content_dir
    assert content.kind == "content"
    assert content.meta["version"] == "0.5.0"
    assert content.scenarios["A1_status_inquiry"]["expect"]["intent"] == "status_request"
    assert content.personas["biller"]["persona_id"] == "p_lakeshore_biller"
    assert content.gen_specs["vendor"]["pool"] == "free_pool"
    assert content.registry["version"] == 1
    assert content.report.checked == 4
    assert content.report.errors == []


def test_optional_content_directories_can_be_absent(content_dir):
    for name in ("scenarios", "personas", "gen"):
        shutil.rmtree(content_dir / name)
    content = load_content(content_dir, strict=True)
    assert content.scenarios == content.personas == content.gen_specs == {}
    assert content.report.checked == 1


def test_content_metadata_takes_precedence_over_smoke_manifest(content_dir):
    (content_dir / "manifest.json").write_text('{"schema": "unknown"}')
    assert load_content(content_dir, strict=True).kind == "content"


def test_loader_collects_schema_errors_with_file_and_field_paths(content_dir):
    bad = {
        "scenarios/A/A1_status_inquiry.yaml": ("profile", "unknown"),
        "personas/behavior/biller.yaml": ("persona_id", "invalid"),
        "gen/specs/vendor.yaml": ("pool", "unknown"),
        "dist/registry.yaml": ("version", 99),
    }
    for relative, (field, value) in bad.items():
        path = content_dir / relative
        document = yaml.safe_load(path.read_text())
        document[field] = value
        path.write_text(yaml.safe_dump(document))
    report = load_content(content_dir).report
    assert not report.ok
    assert report.checked == 4
    assert len(report.errors) == 4
    for relative, (field, _) in bad.items():
        assert any(error.startswith(f"{relative}: {field}:") for error in report.errors)
    with pytest.raises(ValueError, match="content invalid") as error:
        load_content(content_dir, strict=True)
    for diagnostic in report.errors:
        assert diagnostic in str(error.value)


@pytest.mark.parametrize("registry", [None, "", "{}"])
def test_missing_or_empty_registry_reports_error(content_dir, registry):
    path = content_dir / "dist/registry.yaml"
    if registry is None:
        path.unlink()
    else:
        path.write_text(registry)
    content = load_content(content_dir)
    assert "missing registry: dist/registry.yaml" in content.report.errors
    with pytest.raises(ValueError, match="missing registry"):
        load_content(content_dir, strict=True)


def test_unknown_layout_is_rejected(tmp_path):
    with pytest.raises(FileNotFoundError, match="neither content.json nor manifest.json"):
        load_content(tmp_path)


def test_incompatible_content_is_rejected_before_reading_documents(content_dir):
    (content_dir / "content.json").write_text('{"schema_version": "3.0"}')
    (content_dir / "dist/registry.yaml").write_text("[")
    with pytest.raises(CompatError, match="schema major 3"):
        load_content(content_dir)


@pytest.mark.parametrize("metadata,message", [
    ({"schema": "mailroom.smoke_export/v2", "schema_version": "2.0"}, "unknown smoke manifest"),
    ({"schema": "mailroom.smoke_export/v1", "schema_version": "3.0"}, "smoke schema major"),
])
def test_incompatible_smoke_manifest(tmp_path, metadata, message):
    (tmp_path / "manifest.json").write_text(json.dumps(metadata))
    with pytest.raises(CompatError, match=message):
        load_content(tmp_path)


def test_manifest_collects_missing_and_tampered_files(tmp_path):
    root = tmp_path / "smoke"
    shutil.copytree(loader.SMOKE_DIR, root)
    (root / "templates/status_inquiry.j2").unlink()
    (root / "docs/coi_unit4c.pdf").write_bytes(b"tampered")
    report = load_content(root).report
    assert "manifest: missing file templates/status_inquiry.j2" in report.errors
    assert "manifest: sha256 mismatch docs/coi_unit4c.pdf" in report.errors
    with pytest.raises(ValueError, match="content invalid"):
        load_content(root, strict=True)


def test_smoke_generation_specs_use_gen_directory(tmp_path):
    root = tmp_path / "smoke"
    shutil.copytree(loader.SMOKE_DIR, root)
    (root / "gen").mkdir()
    example = Path(__file__).parent / "examples/gen_spec.yaml"
    shutil.copyfile(example, root / "gen/vendor.yaml")
    assert load_content(root, strict=True).gen_specs["vendor"]["pool"] == "free_pool"


def test_packaged_schemas_are_found_without_source_checkout(tmp_path, monkeypatch):
    module = tmp_path / "site-packages/mailroom_reloaded/sandbox/content/loader.py"
    schemas = module.parents[1] / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "scenario.v2.json").write_text("{}")
    monkeypatch.setattr(loader, "__file__", str(module))
    assert loader.schemas_dir() == schemas


def test_missing_schemas_has_actionable_error(tmp_path, monkeypatch):
    module = tmp_path / "site-packages/mailroom_reloaded/sandbox/content/loader.py"
    monkeypatch.setattr(loader, "__file__", str(module))
    with pytest.raises(FileNotFoundError, match="schemas/ not found"):
        loader.schemas_dir()
