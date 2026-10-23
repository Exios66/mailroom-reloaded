"""Existing review read surfaces: nonempty contracts and authentication, offline."""

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mailroom_reloaded.schemas.audit import CatalogRecord
from mailroom_reloaded.schemas.manifest import Manifest
from mailroom_reloaded.storage import catalog, db
from mailroom_reloaded.storage.bins import Bins, save_manifest

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
TOKEN = "review-contract-test-token"
DOC_ID = "review_contract_doc"
TRANSCRIPT = "Authenticated operator transcript <img src=x onerror=alert(1)>"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _contract(name):
    return json.loads((SCHEMAS / name).read_text())


def _validate(value, schema, root=None):
    """Check the complete JSON Schema subset used by these two static contracts."""
    root = schema if root is None else root
    supported = {
        "$schema", "title", "description", "$defs", "$ref", "type", "required",
        "additionalProperties", "properties", "items", "minimum",
    }
    assert set(schema) <= supported, "extend this focused checker for new keywords"
    if "$ref" in schema:
        prefix = "#/$defs/"
        assert schema["$ref"].startswith(prefix)
        return _validate(value, root["$defs"][schema["$ref"][len(prefix):]], root)
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "null": value is None,
    }
    assert not types or any(matches[kind] for kind in types), "contract type mismatch"
    if isinstance(value, dict):
        assert set(schema.get("required", [])) <= value.keys(), "missing contract field"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert value.keys() <= properties.keys(), "unexpected contract field"
        for key, field_schema in properties.items():
            if key in value:
                _validate(value[key], field_schema, root)
    if isinstance(value, list) and "items" in schema:
        for item in value:
            _validate(item, schema["items"], root)
    if "minimum" in schema:
        assert value >= schema["minimum"]


@pytest.fixture
def review_client(tmp_path, monkeypatch):
    from mailroom_reloaded import settings
    from mailroom_reloaded.api.app import create_app

    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("MAILROOM_API_TOKEN", TOKEN)
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "0")
    settings.get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    bins = Bins(tmp_path)
    verdict = {
        "label": "partial", "score": 0.5,
        "field_findings": [{"field": "amount", "verdict": "missing", "rationale": "not recorded"}],
    }
    save_manifest(bins, Manifest(
        doc_id=DOC_ID, filename="parked.txt", content_sha256="a" * 64,
        status="parked", completed_nodes=["ingest", "sort"],
        state={
            "text": TRANSCRIPT, "route_trail": ["ingest", "sort", "human_review"],
            "verdict": verdict,
            "report": {"verdict": verdict, "route_trail": ["ingest", "sort", "human_review"]},
        },
    ))
    catalog.upsert(CatalogRecord(doc_id=DOC_ID, filename="parked.txt", status="parked"))
    catalog.upsert(CatalogRecord(doc_id="already_archived", filename="done.txt", status="archived"))
    try:
        with TestClient(create_app()) as client:
            yield client, bins
    finally:
        if db._default_engine is not None:
            db._default_engine.dispose()
        settings.get_settings.cache_clear()


def test_review_queue_schema_contract_nonempty_parked_only(review_client):
    client, _bins = review_client
    response = client.get("/v1/documents?status=parked", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    _validate(body, _contract("review-queue.schema.json"))
    assert body["count"] == len(body["documents"]) == 1
    assert [row["doc_id"] for row in body["documents"]] == [DOC_ID]
    assert body["documents"][0]["status"] == "parked"
    CatalogRecord.model_validate(body["documents"][0])
    assert TRANSCRIPT not in response.text


def test_review_document_schema_contract_with_recorded_transcript_and_findings(review_client):
    client, _bins = review_client
    response = client.get(f"/v1/documents/{DOC_ID}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    _validate(body, _contract("review-document.schema.json"))
    Manifest.model_validate(body["manifest"])
    CatalogRecord.model_validate(body["catalog"])
    assert body["doc_id"] == DOC_ID
    assert body["status"] == "parked"
    state = body["manifest"]["state"]
    assert state["text"] == TRANSCRIPT
    assert state["route_trail"] == ["ingest", "sort", "human_review"]
    assert state["verdict"]["field_findings"][0]["field"] == "amount"
    assert body["report"]["verdict"] == state["verdict"]
    assert "rubric_scores" not in body["report"]


def test_review_document_schema_contract_catalog_only_has_no_invented_state(review_client):
    client, _bins = review_client
    response = client.get("/v1/documents/already_archived", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    _validate(body, _contract("review-document.schema.json"))
    assert body["manifest"] is None
    assert body["report"] is None
    assert body["catalog"]["doc_id"] == "already_archived"


@pytest.mark.parametrize("path", ["/v1/documents?status=parked", f"/v1/documents/{DOC_ID}"])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_review_schema_contract_missing_or_bad_token_is_401(review_client, path, headers):
    client, _bins = review_client
    response = client.get(path, headers=headers)
    assert response.status_code == 401
    assert TRANSCRIPT not in response.text
    assert DOC_ID not in response.text
    assert "manifest" not in response.json()


def test_review_document_schema_contract_unknown_id_is_404(review_client):
    client, _bins = review_client
    assert client.get("/v1/documents/not_present", headers=HEADERS).status_code == 404


@pytest.mark.parametrize("name,path", [
    ("review-queue.schema.json", "/v1/documents?status=parked"),
    ("review-document.schema.json", f"/v1/documents/{DOC_ID}"),
])
def test_review_schema_contract_checker_rejects_shape_drift(review_client, name, path):
    client, _bins = review_client
    schema = _contract(name)
    body = client.get(path, headers=HEADERS).json()
    _validate(body, schema)
    for key in schema["required"]:
        broken = copy.deepcopy(body)
        del broken[key]
        with pytest.raises(AssertionError, match="missing contract field"):
            _validate(broken, schema)
    broken = copy.deepcopy(body)
    broken["documents" if "documents" in body else "manifest"] = "not an object or array"
    with pytest.raises(AssertionError, match="contract type mismatch"):
        _validate(broken, schema)
    broken = copy.deepcopy(body)
    if "documents" in body:
        broken["documents"][0]["filename"] = []
    else:
        broken["manifest"]["state"] = []
    with pytest.raises(AssertionError, match="contract type mismatch"):
        _validate(broken, schema)
