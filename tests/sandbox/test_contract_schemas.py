"""M0: worked examples (content-repo-shaped samples) validate against schemas/."""
import json
import re
from copy import deepcopy
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
    bad["name"] = "not a valid name"
    with pytest.raises(jsonschema.ValidationError) as exc:
        validator("scenario.v2.json").validate(bad)
    assert list(exc.value.path) == ["name"]
    assert exc.value.validator == "pattern"


def test_gen_spec_requires_forbidden():
    bad = yml("gen_spec.yaml")
    del bad["constraints"]["forbidden"]
    with pytest.raises(jsonschema.ValidationError):
        validator("gen_spec.v1.json").validate(bad)


@pytest.mark.parametrize("missing", [
    "real_brands", "real_urls", "working_links", "phone_numbers_outside_555_01xx",
])
def test_gen_spec_requires_each_forbidden_value(missing):
    bad = yml("gen_spec.yaml")
    bad["constraints"]["forbidden"].remove(missing)
    with pytest.raises(jsonschema.ValidationError):
        validator("gen_spec.v1.json").validate(bad)


@pytest.mark.parametrize("extra,valid", [("other_restriction", True), (1, False)])
def test_gen_spec_forbidden_extra_values(extra, valid):
    spec = yml("gen_spec.yaml")
    spec["constraints"]["forbidden"].reverse()
    spec["constraints"]["forbidden"].append(extra)
    assert validator("gen_spec.v1.json").is_valid(spec) is valid


@pytest.mark.parametrize("delay,valid", [(-1, False), (0, True), (0.5, True), ("0", False)])
def test_persona_escalation_step_delay(delay, valid):
    persona = yml("persona_behavior.yaml")
    persona["escalation"]["steps"][0]["after_sim_min"] = delay
    assert validator("persona_behavior.v1.json").is_valid(persona) is valid


@pytest.mark.parametrize("path", [
    (), ("attachment_habits",), ("escalation",), ("escalation", "steps", 0),
])
def test_persona_rejects_unknown_fields(path):
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
    assert len(load("relation_kinds.v1.json")["enum"]) == 8
    ev = validator("event_kinds.v1.json")
    ev.validate("run.state")
    with pytest.raises(jsonschema.ValidationError):
        ev.validate("nope")
    validator("signal_kinds.v1.json").validate("possible_attack")


def test_content_files_lists_relation_header():
    cf = load("content_files.json")
    assert "kind" in cf["files"]["relations/relations_truth.csv"]["header"]


EXAMPLES = {
    "scenario": ("scenario.v2.json", "scenario_A1.yaml"),
    "gen_spec": ("gen_spec.v1.json", "gen_spec.yaml"),
    "persona": ("persona_behavior.v1.json", "persona_behavior.yaml"),
    "registry": ("registry.v1.json", "registry.yaml"),
}


@pytest.fixture
def contract():
    """Return a validator and fresh, known-valid data for each mutation."""
    def make(kind):
        if kind == "message":
            schema = "overlay.v1.json"
            data = json.loads((EX / "overlay.jsonl").read_text().splitlines()[0])
        elif kind == "route":
            schema = "overlay.v1.json"
            data = {
                "v": 1, "kind": "route",
                "virtual": "sender@client.sandbox.invalid", "inbox": "pool_inbox_01",
            }
        else:
            schema, example = EXAMPLES[kind]
            data = yml(example)
        check = validator(schema)
        check.validate(data)
        return check, data
    return make


def at_path(data, path):
    for part in path:
        data = data[part]
    return data


def assert_rejected(check, data, path, keyword):
    """Require the intended violation, not an unrelated validation failure."""
    errors = list(check.iter_errors(data))
    assert any(
        tuple(error.absolute_path) == path and error.validator == keyword
        for error in errors
    ), [f"{list(error.absolute_path)}: {error.message}" for error in errors]


@pytest.mark.parametrize("kind,path,fields", [
    ("scenario", (), "name title seed profile gen transports timeline expect"),
    ("scenario", ("expect",), "intent"),
    ("scenario", ("expect", "signals", 0), "kind priority"),
    ("scenario", ("expect", "trust"), "sender_level"),
    ("scenario", ("timeline", 0), "at"),
    ("scenario", ("timeline", 0, "client"), "persona channel template"),
    ("gen_spec", (), "id persona target_client archetype goal constraints style pool expect"),
    ("gen_spec", ("expect",), "intent signal"),
    ("persona", (), "persona_id escalation attachment_habits"),
    ("persona", ("escalation",), "patience_sim_min steps"),
    ("persona", ("escalation", "steps", 0), "after_sim_min action"),
    ("registry", (), "version clients"),
    ("registry", ("clients", "brightwater"),
     "display_name verified_domains verified_addresses callback"),
    ("registry", ("clients", "brightwater", "callback"), "contact phone"),
    ("message", (), "v kind provider_message_id from auth"),
    ("route", (), "v kind virtual inbox"),
])
def test_required_fields_cannot_be_omitted(contract, kind, path, fields):
    check, original = contract(kind)
    for field in fields.split():
        data = deepcopy(original)
        del at_path(data, path)[field]
        assert_rejected(check, data, path, "required")


@pytest.mark.parametrize("kind,path,value,keyword", [
    ("scenario", ("name",), "H1_status", "pattern"),
    ("scenario", ("name",), "a1_status", "pattern"),
    ("scenario", ("name",), "A_status", "pattern"),
    ("scenario", ("name",), "A1_Status", "pattern"),
    ("scenario", ("name",), "A1_", "pattern"),
    ("scenario", ("seed",), -1, "minimum"),
    ("scenario", ("seed",), 0.5, "type"),
    ("scenario", ("seed",), True, "type"),
    ("scenario", ("timeline",), [], "minItems"),
    ("scenario", ("transports",), [], "minItems"),
    ("scenario", ("transports", 0), "smtp", "enum"),
    ("scenario", ("gen",), "automatic", "enum"),
    ("scenario", ("profile",), "production", "enum"),
    ("scenario", ("status",), "ready", "enum"),
    ("scenario", ("timeline", 0, "at"), "0:02", "pattern"),
    ("scenario", ("timeline", 0, "at"), "00:02:003", "pattern"),
    ("scenario", ("timeline", 0, "client", "persona"), "p_Client", "pattern"),
    ("scenario", ("timeline", 0, "client", "channel"), "phone", "enum"),
    ("scenario", ("timeline", 0, "client", "template"), "../reply", "pattern"),
    ("scenario", ("expect", "intent"), "unknown", "enum"),
    ("scenario", ("expect", "signals", 0, "kind"), "unknown", "enum"),
    ("scenario", ("expect", "signals", 0, "priority"), "urgent", "enum"),
    ("scenario", ("expect", "signals", 0, "within"), "5m", "pattern"),
    ("scenario", ("expect", "trust", "sender_level"), "trusted", "enum"),
    ("scenario", ("expect", "outbox", 0, "state"), "queued", "enum"),
    ("scenario", ("expect", "outbox", 0, "to_sender"), "true", "type"),
    ("scenario", ("expect", "boss_actions", 0), "send_email", "enum"),
    ("scenario", ("expect", "invariants", 0), "unknown", "enum"),
    ("scenario", ("expect", "overblocking", "benign_hard_actions"), -1, "minimum"),
    ("scenario", ("expect", "overblocking", "benign_hard_actions"), 0.5, "type"),
    ("gen_spec", ("id",), "gen_", "pattern"),
    ("gen_spec", ("persona",), "vendor_spam", "pattern"),
    ("gen_spec", ("target_client",), "", "minLength"),
    ("gen_spec", ("archetype",), "", "minLength"),
    ("gen_spec", ("goal",), "", "minLength"),
    ("gen_spec", ("pool",), "unlimited", "enum"),
    ("gen_spec", ("attack_class",), "unknown", "enum"),
    ("gen_spec", ("expect", "intent"), "unknown", "enum"),
    ("gen_spec", ("expect", "signal"), "unknown", "enum"),
    ("gen_spec", ("expect", "quarantine"), "false", "type"),
    ("gen_spec", ("expect", "reply_to_sender"), 0, "type"),
    ("gen_spec", ("constraints", "forbidden", 0), 42, "type"),
    ("gen_spec", ("constraints", "must_include", 0), None, "type"),
    ("gen_spec", ("style", "language"), ["en"], "type"),
    ("persona", ("persona_id",), "p_", "pattern"),
    ("persona", ("escalation", "patience_sim_min"), -0.01, "minimum"),
    ("persona", ("escalation", "patience_sim_min"), "400", "type"),
    ("persona", ("escalation", "steps", 0, "after_sim_min"), "400", "type"),
    ("persona", ("escalation", "steps", 0, "action"), None, "type"),
    ("persona", ("attachment_habits", "formats", 0), 42, "type"),
    ("persona", ("reply_templates", 0), {}, "type"),
    ("registry", ("version",), 2, "const"),
    ("registry", ("version",), "1", "const"),
    ("registry", ("clients", "brightwater", "callback", "phone"),
     "+1-555-0200", "pattern"),
    ("registry", ("clients", "brightwater", "callback", "phone"),
     "+1-555-01000", "pattern"),
    ("registry", ("clients", "brightwater", "reference_formats", "claim"), 42, "type"),
    ("registry", ("clients", "brightwater", "usual_mix", "contract"), -0.01, "minimum"),
    ("registry", ("clients", "brightwater", "usual_mix", "contract"), 1.01, "maximum"),
    ("registry", ("clients", "brightwater", "usual_mix", "contract"), True, "type"),
    ("message", ("v",), 2, "const"),
    ("route", ("v",), "1", "const"),
    ("message", ("kind",), "unknown", "enum"),
    ("route", ("inbox",), None, "type"),
])
def test_invalid_field_values_rejected(contract, kind, path, value, keyword):
    check, data = contract(kind)
    at_path(data, path[:-1])[path[-1]] = value
    assert_rejected(check, data, path, keyword)


@pytest.mark.parametrize("kind,path", [
    ("scenario", ()), ("scenario", ("expect",)),
    ("scenario", ("timeline", 0)), ("scenario", ("timeline", 0, "client")),
    ("scenario", ("expect", "signals", 0)), ("scenario", ("expect", "trust")),
    ("scenario", ("expect", "outbox", 0)), ("scenario", ("expect", "overblocking")),
    ("gen_spec", ()), ("gen_spec", ("constraints",)),
    ("gen_spec", ("style",)), ("gen_spec", ("expect",)),
    ("registry", ()), ("registry", ("clients", "brightwater")),
    ("message", ()), ("route", ()), ("message", ("auth",)),
])
def test_closed_objects_reject_unknown_fields(contract, kind, path):
    check, data = contract(kind)
    at_path(data, path)["unexpected"] = "value"
    assert_rejected(check, data, path, "additionalProperties")


@pytest.mark.parametrize("kind,path", [
    ("message", ()), ("route", ()), ("registry", ("clients", "brightwater")),
])
@pytest.mark.parametrize("field", ["persona_id", "scenario_id", "labels", "stratum_weights"])
def test_wire_contracts_reject_content_only_metadata(contract, kind, path, field):
    check, data = contract(kind)
    at_path(data, path)[field] = "content_only"
    assert_rejected(check, data, path, "additionalProperties")


@pytest.mark.parametrize("series", list("ABCDEFGST"))
def test_supported_scenario_series(contract, series):
    check, data = contract("scenario")
    data["name"] = f"{series}12_boundary_case_01"
    check.validate(data)


@pytest.mark.parametrize("kind,path,values", [
    ("scenario", ("seed",), [0, 1]),
    ("scenario", ("profile",), ["smoke", "demo", "prod_like", "chaos", "coverage"]),
    ("scenario", ("gen",), ["scripted", "frozen", "live", "loop"]),
    ("scenario", ("status",), ["draft", "review", "frozen", "deprecated"]),
    ("scenario", ("transports",), [["sim"], ["agentmail"], ["gmail"]]),
    ("scenario", ("timeline", 0, "at"), ["00:00", "00:00:01"]),
    ("scenario", ("timeline", 0, "client", "vars"),
     [{"text": "synthetic", "count": 0, "ratio": 0.5, "enabled": False}]),
    ("gen_spec", ("pool",), ["free_pool", "paid", "scripted"]),
    ("gen_spec", ("context",), [{"custom": {"values": [1, "synthetic", None]}}]),
    ("persona", ("escalation", "patience_sim_min"), [0, 0.5]),
    ("persona", ("escalation", "steps"), [[]]),
    ("registry", ("clients", "brightwater", "callback", "phone"),
     ["+1-555-0100", "+1-555-0199"]),
    ("registry", ("clients", "brightwater", "usual_mix"),
     [{"contract": 0, "correspondence": 1}]),
    ("registry", ("clients", "brightwater", "reference_formats"),
     [{"claim": None, "matter": "TC-YYYY-NNNN"}, {"claim": "CL-NNNNNN", "matter": None}]),
])
def test_valid_options_and_boundaries(contract, kind, path, values):
    check, data = contract(kind)
    for value in values:
        at_path(data, path[:-1])[path[-1]] = value
        check.validate(data)


@pytest.mark.parametrize("value", [None, [], {"nested": "value"}])
def test_template_variables_reject_non_scalar_values(contract, value):
    check, data = contract("scenario")
    data["timeline"][0]["client"]["vars"]["custom"] = value
    assert_rejected(check, data, ("timeline", 0, "client", "vars", "custom"), "type")


@pytest.mark.parametrize("actions,keyword", [
    ((), "anyOf"),
    (("client",), None), (("ingress",), None), (("fault",), None),
    (("client", "fault"), None), (("ingress", "fault"), None),
    (("client", "ingress"), "not"), (("client", "ingress", "fault"), "not"),
])
def test_timeline_action_combinations(contract, actions, keyword):
    check, data = contract("scenario")
    options = {
        "client": data["timeline"][0]["client"],
        "ingress": {"file": "statement.pdf"},
        "fault": "duplicate_delivery",
    }
    data["timeline"] = [{"at": "00:00", **{key: options[key] for key in actions}}]
    if keyword:
        assert_rejected(check, data, ("timeline", 0), keyword)
    else:
        check.validate(data)


@pytest.mark.parametrize("location", ["attachment", "ingress"])
@pytest.mark.parametrize("selector,keyword", [
    ({"file": "statement.pdf"}, None),
    ({"class": "insurance_claim", "stratum": "statement"}, None),
    ({}, "oneOf"), ({"class": "insurance_claim"}, "oneOf"),
    ({"stratum": "statement"}, "oneOf"),
    ({"file": "statement.pdf", "class": "insurance_claim", "stratum": "statement"},
     "oneOf"),
])
def test_document_requires_exactly_one_source(contract, location, selector, keyword):
    check, data = contract("scenario")
    if location == "attachment":
        path = ("timeline", 0, "client", "attach", 0)
        data["timeline"][0]["client"]["attach"] = [selector]
    else:
        path = ("timeline", 0, "ingress")
        data["timeline"] = [{"at": "00:00", "ingress": selector}]
    if keyword:
        assert_rejected(check, data, path, keyword)
    else:
        check.validate(data)


@pytest.mark.parametrize("extra", [
    {}, {"file": "statement.pdf"}, {"class": "insurance_claim", "stratum": "statement"},
])
def test_duplicate_attachment_source_is_exclusive(contract, extra):
    check, data = contract("scenario")
    data["timeline"][0]["client"]["attach"] = [{"same_as": "doc_original", **extra}]
    if extra:
        assert_rejected(check, data, ("timeline", 0, "client", "attach", 0), "oneOf")
    else:
        check.validate(data)


@pytest.mark.parametrize("location", ["attachment", "ingress"])
@pytest.mark.parametrize("field,value,keyword", [
    ("ref", "doc_1", None), ("ref", "1doc", "pattern"),
    ("ref", "Doc_1", "pattern"), ("group", "matter_01", None),
    ("group", "matter-01", "pattern"), ("as", "renamed.pdf", None),
    ("as", "", "minLength"), ("in_taxonomy", False, None),
    ("in_taxonomy", "false", "type"), ("unexpected", "value", "additionalProperties"),
])
def test_document_metadata(contract, location, field, value, keyword):
    check, data = contract("scenario")
    document = {"file": "statement.pdf", field: value}
    if location == "attachment":
        path = ("timeline", 0, "client", "attach", 0)
        data["timeline"][0]["client"]["attach"] = [document]
    else:
        path = ("timeline", 0, "ingress")
        data["timeline"] = [{"at": "00:00", "ingress": document}]
    if keyword:
        error_path = path if keyword == "additionalProperties" else (*path, field)
        assert_rejected(check, data, error_path, keyword)
    else:
        check.validate(data)


@pytest.mark.parametrize("confidence,keyword", [
    (0, None), (0.5, None), (1, None),
    (-0.001, "minimum"), (1.001, "maximum"), ("0.5", "type"), (True, "type"),
])
def test_relation_confidence_boundaries(contract, confidence, keyword):
    check, data = contract("scenario")
    data["expect"]["relations"] = [
        {"a": "doc_new", "b": "doc_old", "kind": "supersedes", "min_conf": confidence},
    ]
    if keyword:
        assert_rejected(check, data, ("expect", "relations", 0, "min_conf"), keyword)
    else:
        check.validate(data)


@pytest.mark.parametrize("field", ["a", "b", "kind"])
def test_relation_endpoints_and_kind_required(contract, field):
    check, data = contract("scenario")
    relation = {"a": "doc_new", "b": "matter:synthetic", "kind": "references"}
    data["expect"]["relations"] = [relation]
    check.validate(data)
    del relation[field]
    assert_rejected(check, data, ("expect", "relations", 0), "required")


@pytest.mark.parametrize("kind,path", [
    ("scenario", ("timeline", 0, "client", "claimed_from")),
    ("message", ("from",)), ("route", ("virtual",)),
    ("registry", ("clients", "brightwater", "verified_addresses", 0)),
])
@pytest.mark.parametrize("address,valid", [
    ("sender@client.sandbox.invalid", True),
    ("sender@sub.client.sandbox.invalid", True),
    ("sender@example.com", False),
    ("sender@client.sandbox.invalid.example.com", False),
    ("sender@client-sandbox.invalid", False),
    ("sender name@client.sandbox.invalid", False),
    ("@client.sandbox.invalid", False),
    ("sender@@client.sandbox.invalid", False),
])
def test_sender_addresses_stay_in_sandbox(contract, kind, path, address, valid):
    check, data = contract(kind)
    at_path(data, path[:-1])[path[-1]] = address
    if valid:
        check.validate(data)
    else:
        assert_rejected(check, data, path, "pattern")


@pytest.mark.parametrize("domain,valid", [
    ("sandbox.invalid", True), ("client.sandbox.invalid", True),
    ("sub.client.sandbox.invalid", True), ("example.com", False),
    ("client.sandbox.invalid.example.com", False), ("client-sandbox.invalid", False),
    ("https://client.sandbox.invalid", False),
])
def test_registry_domain_boundaries(contract, domain, valid):
    check, data = contract("registry")
    data["clients"]["brightwater"]["verified_domains"] = [domain]
    if valid:
        check.validate(data)
    else:
        assert_rejected(check, data, ("clients", "brightwater", "verified_domains", 0), "pattern")


@pytest.mark.parametrize("kind", ["scenario", "message"])
@pytest.mark.parametrize("mechanism", ["spf", "dkim", "dmarc"])
@pytest.mark.parametrize("result", ["pass", "fail", "none", "softfail", "temperror", "permerror", "unknown"])
def test_auth_result_vocabulary(contract, kind, mechanism, result):
    check, data = contract(kind)
    path = ("auth",) if kind == "message" else ("timeline", 0, "client", "auth")
    at_path(data, path)[mechanism] = result
    valid = result in {"pass", "fail", "none"} or (
        mechanism != "dmarc" and result in {"softfail", "temperror", "permerror"}
    )
    if valid:
        check.validate(data)
    else:
        assert_rejected(check, data, (*path, mechanism), "enum")


KINDS = {
    "relation": [
        "references", "supersedes", "duplicates", "amends", "answers", "contradicts",
        "withdraws", "completes",
    ],
    "signal": [
        "new_info", "doc_relation", "status_request", "missing_doc", "correction", "complaint",
        "urgent", "possible_attack", "fyi", "legal_notice", "privacy_request", "payment_change",
    ],
    "event": [
        "ingress.sent", "message.received", "message.triaged", "signal.emitted", "boss.action",
        "draft.created", "draft.approved", "send.done", "attachment.handoff", "attachment.held",
        "attachment.quarantined", "generation.attempt", "generation.fallback", "egress.call",
        "fault.injected", "run.state",
    ],
}


@pytest.mark.parametrize("family,values", KINDS.items())
def test_kind_vocabularies_are_complete(family, values):
    check = validator(f"{family}_kinds.v1.json")
    assert sorted(check.schema["enum"]) == sorted(values)
    for value in values:
        check.validate(value)


@pytest.mark.parametrize("family", KINDS)
@pytest.mark.parametrize("value", ["unknown", "", "REFERENCES", None, 1, True, [], {}])
def test_kind_vocabularies_reject_unknown_or_non_string_values(family, value):
    check = validator(f"{family}_kinds.v1.json")
    assert_rejected(check, value, (), "enum" if isinstance(value, str) else "type")


@pytest.mark.parametrize("signal", KINDS["signal"])
def test_signal_kinds_work_in_scenarios_and_generation_specs(contract, signal):
    check, scenario = contract("scenario")
    scenario["expect"]["signals"][0]["kind"] = signal
    check.validate(scenario)
    check, spec = contract("gen_spec")
    spec["expect"]["signal"] = signal
    check.validate(spec)


@pytest.mark.parametrize("relation", [*KINDS["relation"], "unknown"])
def test_scenario_relations_allow_placeholder_besides_known_kinds(contract, relation):
    check, data = contract("scenario")
    data["expect"]["relations"] = [{"a": "doc_a", "b": "doc_b", "kind": relation}]
    check.validate(data)


@pytest.mark.parametrize("fault", [
    "duplicate_delivery", "out_of_order_delivery", "delayed_delivery", "crash_mid_triage",
    "crash_mid_node", "provider_429", "provider_timeout", "watcher_lock_contention",
    "free_pool_429_storm", "free_daily_cap_reached", "all_gen_tiers_down",
    "daily_spend_cap_reached_mid_run", "prompt_contains_dataset_text_hash",
    "egress_to_unallowlisted_host", "draft_to_virtual_address_without_route",
    "sse_client_reconnect_with_last_event_id",
    "production_gmail_token_path_reachable_at_startup", "outbound_send_disabled", "unknown",
])
def test_fault_directives(contract, fault):
    check, data = contract("scenario")
    data["timeline"] = [{"at": "00:00", "fault": fault}]
    if fault == "unknown":
        assert_rejected(check, data, ("timeline", 0, "fault"), "enum")
    else:
        check.validate(data)


@pytest.mark.parametrize("attack", [
    "impersonation", "payment_fraud", "credential_phish", "injection", "exfiltration",
    "malicious_attachment", "other", "unknown",
])
def test_attack_classes_in_scenarios_and_generation_specs(contract, attack):
    for kind, path in [
        ("scenario", ("expect", "signals", 0, "attack_class")),
        ("gen_spec", ("attack_class",)),
    ]:
        check, data = contract(kind)
        at_path(data, path[:-1])[path[-1]] = attack
        if attack == "unknown":
            assert_rejected(check, data, path, "enum")
        else:
            check.validate(data)


@pytest.mark.parametrize("kind", [*EXAMPLES, "message", "route"])
@pytest.mark.parametrize("value", [None, [], "not an object"])
def test_contract_documents_must_be_objects(contract, kind, value):
    check, _ = contract(kind)
    assert_rejected(check, value, (), "type")


@pytest.mark.parametrize("file,header", [
    ("relations/relations_truth.csv",
     "relation_id scenario_id a_ref b_ref kind source min_conf notes"),
    ("relations/dataset_relation_map.csv", "dataset_relation mailroom_kind notes"),
    ("attachments/manifest.csv",
     ("attachment_id file sha256 doc_id class stratum in_taxonomy source "
      "dataset_revision dataset_filename degradation inert notes")),
])
def test_content_csv_column_order(file, header):
    assert load("content_files.json")["files"][file]["header"] == header.split()


@pytest.mark.parametrize("file,valid_id,invalid_id", [
    ("clients/clients.csv", "tricountytitle", "TriCountyTitle"),
    ("clients/client_contacts.csv", "tc_kalvarado", "tc-kalvarado"),
    ("personas/personas.csv", "p_tricounty_real", "tricounty_real"),
])
def test_content_csv_id_patterns(file, valid_id, invalid_id):
    pattern = load("content_files.json")["files"][file]["id_pattern"]
    assert re.search(pattern, valid_id)
    assert re.search(pattern, invalid_id) is None
    assert re.search(pattern, "") is None
    assert re.search(pattern, f"prefix!{valid_id}") is None
    assert re.search(pattern, f"{valid_id}!suffix") is None
