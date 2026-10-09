"""M0: worked examples (content-repo-shaped samples) validate against schemas/."""
import json
from pathlib import Path

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
EX = Path(__file__).parent / "examples"


def load(name):
    """Load a JSON file from the schema directory."""
    return json.loads((SCHEMAS / name).read_text())


def yml(name):
    """Load a YAML file from the worked examples directory."""
    return yaml.safe_load((EX / name).read_text())


def validator(name):
    """Check a named schema and return its Draft 2020-12 validator."""
    s = load(name)
    jsonschema.Draft202012Validator.check_schema(s)
    return jsonschema.Draft202012Validator(s)


@pytest.mark.parametrize("p", sorted(SCHEMAS.glob("*.json")), ids=lambda p: p.name)
def test_every_schema_is_valid(p):
    """Verify each schema is valid under JSON Schema Draft 2020-12."""
    jsonschema.Draft202012Validator.check_schema(json.loads(p.read_text()))


@pytest.mark.parametrize("schema,example", [
    ("scenario.v2.json", "scenario_A1.yaml"),
    ("gen_spec.v1.json", "gen_spec.yaml"),
    ("persona_behavior.v1.json", "persona_behavior.yaml"),
    ("registry.v1.json", "registry.yaml"),
])
def test_yaml_examples_validate(schema, example):
    """Verify each worked YAML example satisfies its matching schema."""
    validator(schema).validate(yml(example))


def test_overlay_example_validates():
    """Verify the first overlay JSONL record satisfies the overlay schema."""
    line = (EX / "overlay.jsonl").read_text().splitlines()[0]
    validator("overlay.v1.json").validate(json.loads(line))


def test_invalid_scenario_rejected():
    """Verify an invalid scenario name fails the name pattern constraint."""
    bad = yml("scenario_A1.yaml")
    bad["name"] = "not a valid name"
    with pytest.raises(jsonschema.ValidationError) as exc:
        validator("scenario.v2.json").validate(bad)
    assert list(exc.value.path) == ["name"]
    assert exc.value.validator == "pattern"


def test_gen_spec_requires_forbidden():
    """Verify generation constraints require the forbidden list."""
    bad = yml("gen_spec.yaml")
    del bad["constraints"]["forbidden"]
    with pytest.raises(jsonschema.ValidationError):
        validator("gen_spec.v1.json").validate(bad)


@pytest.mark.parametrize("missing", [
    "real_brands", "real_urls", "working_links", "phone_numbers_outside_555_01xx",
])
def test_gen_spec_requires_each_forbidden_value(missing):
    """Verify removing any required forbidden value invalidates the spec."""
    bad = yml("gen_spec.yaml")
    bad["constraints"]["forbidden"].remove(missing)
    with pytest.raises(jsonschema.ValidationError):
        validator("gen_spec.v1.json").validate(bad)


@pytest.mark.parametrize("extra,valid", [("other_restriction", True), (1, False)])
def test_gen_spec_forbidden_extra_values(extra, valid):
    """Verify reordered forbidden lists allow extra strings but reject numbers."""
    spec = yml("gen_spec.yaml")
    spec["constraints"]["forbidden"].reverse()
    spec["constraints"]["forbidden"].append(extra)
    assert validator("gen_spec.v1.json").is_valid(spec) is valid


@pytest.mark.parametrize("delay,valid", [(-1, False), (0, True), (0.5, True), ("0", False)])
def test_persona_escalation_step_delay(delay, valid):
    """Verify escalation delays accept nonnegative numbers only."""
    persona = yml("persona_behavior.yaml")
    persona["escalation"]["steps"][0]["after_sim_min"] = delay
    assert validator("persona_behavior.v1.json").is_valid(persona) is valid


@pytest.mark.parametrize("path", [
    (), ("attachment_habits",), ("escalation",), ("escalation", "steps", 0),
])
def test_persona_rejects_unknown_fields(path):
    """Verify unknown persona fields are rejected at each tested nesting level."""
    bad = yml("persona_behavior.yaml")
    obj = bad
    for key in path:
        obj = obj[key]
    obj["unknown_field"] = True
    with pytest.raises(jsonschema.ValidationError) as exc:
        validator("persona_behavior.v1.json").validate(bad)
    assert list(exc.value.path) == list(path)
    assert exc.value.validator == "additionalProperties"


def test_scenario_relation_kinds_match_shared_enum():
    """Verify scenario relation kinds match the shared enum and reject unknowns."""
    kinds = load("relation_kinds.v1.json")["enum"]
    scenario_kinds = load("scenario.v2.json")["properties"]["expect"]["properties"][
        "relations"
    ]["items"]["properties"]["kind"]["enum"]
    assert set(scenario_kinds) == set(kinds)
    scenario = yml("scenario_A1.yaml")
    for kind in kinds:
        scenario["expect"]["relations"] = [{"a": "doc_a", "b": "doc_b", "kind": kind}]
        validator("scenario.v2.json").validate(scenario)
    scenario["expect"]["relations"][0]["kind"] = "unknown"
    with pytest.raises(jsonschema.ValidationError):
        validator("scenario.v2.json").validate(scenario)


def test_enums():
    """Check the relation kind count and representative event and signal values."""
    assert len(load("relation_kinds.v1.json")["enum"]) == 8
    ev = validator("event_kinds.v1.json")
    ev.validate("run.state")
    with pytest.raises(jsonschema.ValidationError):
        ev.validate("nope")
    validator("signal_kinds.v1.json").validate("possible_attack")


def test_content_files_lists_relation_header():
    """Verify the relation truth CSV contract includes a kind column."""
    cf = load("content_files.json")
    assert "kind" in cf["files"]["relations/relations_truth.csv"]["header"]
