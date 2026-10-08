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
    return json.loads((SCHEMAS / name).read_text())


def yml(name):
    return yaml.safe_load((EX / name).read_text())


def validator(name):
    s = load(name)
    jsonschema.Draft202012Validator.check_schema(s)
    return jsonschema.Draft202012Validator(s)


@pytest.mark.parametrize("p", sorted(SCHEMAS.glob("*.json")), ids=lambda p: p.name)
def test_every_schema_is_valid(p):
    jsonschema.Draft202012Validator.check_schema(json.loads(p.read_text()))


@pytest.mark.parametrize("schema,example", [
    ("scenario.v2.json", "scenario_A1.yaml"),
    ("gen_spec.v1.json", "gen_spec.yaml"),
    ("persona_behavior.v1.json", "persona_behavior.yaml"),
    ("registry.v1.json", "registry.yaml"),
])
def test_yaml_examples_validate(schema, example):
    validator(schema).validate(yml(example))


def test_overlay_example_validates():
    line = (EX / "overlay.jsonl").read_text().splitlines()[0]
    validator("overlay.v1.json").validate(json.loads(line))


def test_invalid_scenario_rejected():
    bad = yml("scenario_A1.yaml")
    bad["id"] = "not a valid id"
    with pytest.raises(jsonschema.ValidationError):
        validator("scenario.v2.json").validate(bad)


def test_enums():
    assert len(load("relation_kinds.v1.json")["enum"]) == 8
    ev = validator("event_kinds.v1.json")
    ev.validate("run.state")
    with pytest.raises(jsonschema.ValidationError):
        ev.validate("nope")
    validator("signal_kinds.v1.json").validate("possible_attack")


def test_content_files_lists_relation_header():
    cf = load("content_files.json")
    assert "kind" in cf["files"]["relations/relations_truth.csv"]["header"]


@pytest.mark.parametrize("kind,record,required", [
    ("message", {"provider_message_id": "msg-1", "from": "sender@sandbox.invalid", "auth": {}},
     "provider_message_id"),
    ("message", {"provider_message_id": "msg-1", "from": "sender@sandbox.invalid", "auth": {}},
     "from"),
    ("message", {"provider_message_id": "msg-1", "from": "sender@sandbox.invalid", "auth": {}},
     "auth"),
    ("route", {"virtual": "sender@sandbox.invalid", "inbox": "local-inbox"}, "virtual"),
    ("route", {"virtual": "sender@sandbox.invalid", "inbox": "local-inbox"}, "inbox"),
])
def test_overlay_requires_fields_for_its_kind(kind, record, required):
    document = {"v": 1, "kind": kind, **record}
    schema = validator("overlay.v1.json")
    schema.validate(document)
    del document[required]
    with pytest.raises(jsonschema.ValidationError, match="required property"):
        schema.validate(document)


@pytest.mark.parametrize("address", [
    "person@example.com", "person@sandbox.invalid.evil", "person@evilsandbox.invalid",
    "two words@sandbox.invalid",
])
@pytest.mark.parametrize("kind,field", [("message", "from"), ("route", "virtual")])
def test_overlay_rejects_addresses_outside_synthetic_namespace(address, kind, field):
    document = {"v": 1, "kind": kind}
    if kind == "message":
        document.update(provider_message_id="msg-1", auth={})
    else:
        document["inbox"] = "local-inbox"
    document[field] = address
    with pytest.raises(jsonschema.ValidationError, match="does not match"):
        validator("overlay.v1.json").validate(document)


@pytest.mark.parametrize("field", ["persona_id", "scenario_id", "label"])
def test_overlay_rejects_ground_truth_fields(field):
    document = json.loads((EX / "overlay.jsonl").read_text().splitlines()[0])
    document[field] = "hidden-ground-truth"
    with pytest.raises(jsonschema.ValidationError, match="Additional properties"):
        validator("overlay.v1.json").validate(document)


@pytest.mark.parametrize("field,value", [
    ("verified_domains", ["example.com"]),
    ("verified_addresses", ["person@sandbox.invalid.evil"]),
    ("callback", {"contact": "test", "phone": "+1-555-0200"}),
    ("usual_mix", {"contracts": -0.01}),
    ("usual_mix", {"contracts": 1.01}),
    ("scenario_id", "A1_hidden"),
])
def test_registry_rejects_unsafe_identity_and_weight_fields(field, value):
    document = yml("registry.yaml")
    client = next(iter(document["clients"].values()))
    client[field] = value
    with pytest.raises(jsonschema.ValidationError):
        validator("registry.v1.json").validate(document)


@pytest.mark.parametrize("weight", [0.0, 1.0])
def test_registry_accepts_weight_boundaries(weight):
    document = yml("registry.yaml")
    next(iter(document["clients"].values()))["usual_mix"] = {"contracts": weight}
    validator("registry.v1.json").validate(document)


@pytest.mark.parametrize("field,value", [
    ("pool", "unknown"), ("persona", "invalid persona"), ("id", "bad-id"),
    ("expect", {"intent": "status_request"}),
    ("constraints", {"unexpected": True}), ("style", {"unexpected": True}),
])
def test_generation_spec_rejects_invalid_contract_fields(field, value):
    document = yml("gen_spec.yaml")
    document[field] = value
    with pytest.raises(jsonschema.ValidationError):
        validator("gen_spec.v1.json").validate(document)


@pytest.mark.parametrize("patience,valid", [(0, True), (0.5, True), (-0.1, False)])
def test_persona_patience_must_be_nonnegative(patience, valid):
    document = yml("persona_behavior.yaml")
    document["escalation"]["patience_sim_min"] = patience
    schema = validator("persona_behavior.v1.json")
    if valid:
        schema.validate(document)
    else:
        with pytest.raises(jsonschema.ValidationError, match="less than the minimum"):
            schema.validate(document)


@pytest.mark.parametrize("field,value", [
    ("profile", "unknown"), ("gen", "unknown"), ("unexpected", True),
    ("expect", {"intent": "not_an_intent"}),
    ("expect", {"intent": "status_request", "signals": [{"kind": "status_request", "priority": "urgent"}]}),
    ("expect", {"intent": "status_request", "trust": {"sender_level": "trusted"}}),
])
def test_scenario_rejects_invalid_vocabulary(field, value):
    document = yml("scenario_A1.yaml")
    document[field] = value
    with pytest.raises(jsonschema.ValidationError):
        validator("scenario.v2.json").validate(document)


def test_unknown_relation_is_a_placeholder_not_a_relation_kind():
    with pytest.raises(jsonschema.ValidationError):
        validator("relation_kinds.v1.json").validate("unknown")
