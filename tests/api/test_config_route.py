"""``GET /v1/config``: token-gated, allow-listed, secret-free settings summary (#64, P6-B)."""

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from mailroom_reloaded.settings import get_settings

TOKEN = "tok-CONFIG-7f3a9c2e"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: Distinctive secret values. None may appear anywhere in a /v1/config response body.
SECRETS = {
    "MAILROOM_API_TOKEN": TOKEN,
    "OPENROUTER_API_KEY": "sk-or-LEAK-91bd04",
    "VLLM_API_KEY": "vllm-LEAK-2c7e11",
    "MAILROOM_JEV_API_KEY": "jev-LEAK-6a0d88",
    "MAILROOM_ANCHOR_KEY": "anchor-LEAK-e3f5a1",
    "MAILROOM_ANCHOR_URL": "postgresql://dbuser:pw-LEAK-55ee@db.leak-host.example:5432/ledger",
    "MAILROOM_SMTP_PASSWORD": "smtp-LEAK-4d9b70",
    "TWILIO_AUTH_TOKEN": "twilio-LEAK-0be2c3",
    "MAILROOM_PAGER_WEBHOOK_SECRET": "hook-LEAK-8f21ad",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector-LEAK-19cd.example:4318",
    # A base URL with userinfo: only scheme://host survives, so the password must not.
    "OPENROUTER_BASE_URL": "https://u-LEAK:pw-LEAK-c0ffee@openrouter.example/api/v1",
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolate the base dir, the default engine and the settings cache per test."""
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("MAILROOM_PAGER_EMBED", "0")
    monkeypatch.delenv("MAILROOM_API_TOKEN", raising=False)
    monkeypatch.delenv("MAILROOM_EMBED_WATCHER", raising=False)
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    from mailroom_reloaded.storage import db

    monkeypatch.setattr(db, "_default_engine", None)
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        if db._default_engine is not None:
            db._default_engine.dispose()
        get_settings.cache_clear()


@pytest.fixture()
def client(env):
    from mailroom_reloaded.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def _with_token(monkeypatch):
    monkeypatch.setenv("MAILROOM_API_TOKEN", TOKEN)
    get_settings.cache_clear()


def test_401_without_token(client, monkeypatch):
    _with_token(monkeypatch)
    resp = client.get("/v1/config")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing or invalid API token"


def test_401_with_wrong_token(client, monkeypatch):
    _with_token(monkeypatch)
    resp = client.get("/v1/config", headers={"Authorization": "Bearer not-the-token"})
    assert resp.status_code == 401


def test_200_with_token(client, monkeypatch):
    _with_token(monkeypatch)
    resp = client.get("/v1/config", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["service"] == "mailroom"
    assert body["api"]["token"] == "set"


def test_open_when_no_token_is_configured(client):
    resp = client.get("/v1/config")
    assert resp.status_code == 200
    assert resp.json()["api"]["token"] == "unset"


def test_top_level_keys_are_exactly_the_allow_list(client):
    body = client.get("/v1/config").json()
    assert sorted(body) == sorted(
        [
            "api",
            "jev",
            "links",
            "pager",
            "provider",
            "run",
            "schema_version",
            "service",
            "storage",
            "taxonomy",
            "tracing",
            "version",
            "watcher",
        ]
    )


def test_no_secret_value_appears_in_the_response(client, monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    resp = client.get("/v1/config", headers=AUTH)
    assert resp.status_code == 200
    text = resp.text
    for name, value in SECRETS.items():
        assert value not in text, f"{name} leaked into /v1/config"
    # The fragments a partial leak (prefix, host with password, DSN) would show are absent too.
    for fragment in ("LEAK", "pw-", "dbuser", "u-LEAK", "91bd04", "7f3a9c2e"):
        assert fragment not in text
    body = json.loads(text)
    assert body["provider"]["openrouter_api_key"] == "set"
    assert body["provider"]["vllm_api_key"] == "set"
    assert body["jev"]["api_key"] == "set"
    assert body["storage"]["anchor_url"] == "set"
    assert body["storage"]["anchor_key"] == "set"
    assert body["pager"]["smtp_password"] == "set"
    assert body["pager"]["twilio_auth_token"] == "set"
    assert body["pager"]["webhook_secret"] == "set"
    assert body["tracing"]["otlp_endpoint"] == "set"
    assert body["provider"]["openrouter_base_host"] == "https://openrouter.example"


def test_secret_fields_are_unset_when_absent(client):
    body = client.get("/v1/config").json()
    assert body["provider"]["openrouter_api_key"] == "unset"
    assert body["provider"]["vllm_api_key"] == "unset"
    assert body["jev"]["api_key"] == "unset"
    assert body["pager"]["smtp_password"] == "unset"
    assert body["tracing"]["otlp_traces_endpoint"] == "unset"
    assert body["provider"]["openrouter_base_host"] == "https://openrouter.ai"


def test_taxonomy_section_lists_classes_and_thresholds(client):
    tax = client.get("/v1/config").json()["taxonomy"]
    assert tax["status"] == "ok"
    assert tax["doc_classes"] and tax["doc_classes"] == sorted(tax["doc_classes"])
    assert set(tax["thresholds"]) == {"low", "high", "judge_band_high", "retry_max"}


def test_private_paths_are_presence_only_and_unlisted_settings_stay_absent(client, monkeypatch):
    monkeypatch.setenv("MAILROOM_TRACE_STORE_PATH", "/private/PRIVATE-path/traces.db")
    monkeypatch.setenv("MAILROOM_ANCHOR_KEY_FILE", "/private/PRIVATE-path/key")
    monkeypatch.setenv("MAILROOM_SMTP_USER", "PRIVATE-smtp-user")
    monkeypatch.setenv("MAILROOM_PAGER_TO", "PRIVATE-recipient")
    monkeypatch.setenv("MAILROOM_API_HOST", "PRIVATE-not-the-actual-bind-address")
    get_settings.cache_clear()
    response = client.get("/v1/config")
    assert response.status_code == 200
    assert "PRIVATE" not in response.text
    body = response.json()
    assert body["storage"]["base_dir"] == "set"
    assert body["storage"]["anchor_key_file"] == "set"
    assert body["tracing"]["trace_store_path"] == "set"
    assert "bind_host" not in body["api"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://user:PRIVATE-password@host.example:8443/PRIVATE-path?token=PRIVATE#PRIVATE", "https://host.example:8443"),
        ("http://[::1]:8080/PRIVATE?secret=PRIVATE", "http://[::1]:8080"),
        ("https://host.example:PRIVATE", "invalid"),
        ("postgresql://PRIVATE:PRIVATE@host.example/db", "invalid"),
        ("https://[PRIVATE", "invalid"),
    ],
)
def test_base_urls_never_include_userinfo_paths_queries_or_fragments(client, monkeypatch, value, expected):
    monkeypatch.setenv("VLLM_BASE_URL", value)
    get_settings.cache_clear()
    response = client.get("/v1/config")
    assert response.status_code == 200
    assert response.json()["provider"]["vllm_base_host"] == expected
    assert "PRIVATE" not in response.text


def test_section_failure_never_echoes_exception_messages(client, monkeypatch):
    from mailroom_reloaded.api.routes import config

    def fail():
        raise ValueError("PRIVATE-key-and-file-content")

    monkeypatch.setattr(config, "jev_config", fail)
    response = client.get("/v1/config")
    assert response.status_code == 200
    assert response.json()["jev"] == {"status": "unavailable", "error": "ValueError"}
    assert "PRIVATE" not in response.text


def test_invalid_provider_is_not_echoed_and_config_is_read_only(client, monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "PRIVATE-provider-typo")
    get_settings.cache_clear()
    response = client.get("/v1/config")
    assert response.json()["provider"]["name"] == "invalid"
    assert "PRIVATE" not in response.text
    assert client.post("/v1/config", json={"provider": "live"}).status_code == 405


def test_replay_export_matches_cli_bytes_and_excludes_document_content(client, env):
    from typer.testing import CliRunner

    from mailroom_reloaded.cli import app
    from mailroom_reloaded.obs.replay.timeline import clear_cache
    from mailroom_reloaded.storage.ledger import reset_ledger
    from mailroom_reloaded.storage.retention import seed_showcase
    from mailroom_reloaded.storage.span_store import SpanStore

    clear_cache()
    reset_ledger()
    try:
        with closing(SpanStore(env / "traces.db")) as store:
            seed_showcase(store)
            row = store.spans_for_run("showcase-clean")[0]
            attrs = dict(row["attrs"])
            attrs.update({"input.value": "PRIVATE-document-text", "output.value": "PRIVATE-completion"})
            store.write([{
                **row,
                "span_id": "private-content-span",
                "attrs": json.dumps(attrs),
                "events": json.dumps(row["events"]),
            }])
        response = client.get("/v1/replay/sessions/run:showcase-clean/export")
        assert response.status_code == 200
        target = env / "cli.replay.json"
        result = CliRunner().invoke(app, ["replay", "export", "run:showcase-clean", "--out", str(target)])
        assert result.exit_code == 0, result.output
        assert target.read_bytes() == response.content + b"\n"
        assert b"PRIVATE" not in response.content
        assert response.json()["version"] == "replay/v1"
    finally:
        clear_cache()
        reset_ledger()
