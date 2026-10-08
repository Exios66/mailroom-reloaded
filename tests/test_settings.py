from pathlib import Path

import pytest

from mailroom_reloaded import settings as settings_mod
from mailroom_reloaded.settings import (
    RunConditions,
    Settings,
    get_settings,
    jev_config,
    load_taxonomy,
)


def test_taxonomy_has_five_live_classes():
    assert set(load_taxonomy().classes) == {
        "contract", "merger_agreement", "corporate_record", "correspondence", "insurance_claim",
    }


def test_confidence_by_class():
    tax = load_taxonomy()
    assert tax.confidence_for("insurance_claim").judge_band_high == 0.92
    assert tax.confidence_for("contract").judge_band_high == 0.97
    assert tax.confidence_for("correspondence").judge_band_high == 0.94
    base = tax.confidence_for(None)
    assert (base.low, base.high, base.retry_max) == (0.70, 0.95, 2)


def test_per_class_overrides_honoured():
    tax = load_taxonomy()
    ins = tax.confidence_for("insurance_claim")
    assert (ins.low, ins.high, ins.judge_band_high, ins.retry_max) == (0.90, 0.98, 0.92, 2)
    corp = tax.confidence_for("corporate_record")
    assert (corp.low, corp.high, corp.judge_band_high) == (0.86, 0.96, 0.97)
    # unknown class falls back to the global defaults
    assert tax.confidence_for("nope") == tax.confidence_for(None)
    assert tax.confidence["conflict_threshold"] == 0.3
    assert tax.confidence["arbiter_retry_max"] == 2
    assert tax.confidence["judge_max_passes"] == 3


def test_specialist_conditions():
    tax = load_taxonomy()
    assert tax.specialist_conditions("contract") == RunConditions(24000, 8192, 0.7, 2, "frozen")
    assert tax.specialist_conditions("merger_agreement").input_cap_chars == 30000
    assert tax.specialist_conditions("insurance_claim").temperature == 0.1


def test_bert_and_agents():
    tax = load_taxonomy()
    assert tax.bert.enabled is False
    assert tax.bert.defer_classes == ["contract", "merger_agreement"]
    assert tax.bert.max_trusted_windows == 1
    assert tax.bert.pass_subclass_hint is False
    assert tax.agent("sorter").model
    for gone in ("gmail_triage", "relations", "sorter_reviewer"):
        try:
            tax.agent(gone)
        except KeyError:
            pass
        else:
            raise AssertionError(gone)


def test_doc_class_fields():
    dc = load_taxonomy().classes["contract"]
    assert dc.specialist == "contracts_specialist"
    assert dc.field_types["effective_date"] == "date"


def test_load_taxonomy_is_cached():
    assert load_taxonomy() is load_taxonomy()


def test_settings_defaults(monkeypatch):
    for k in ("DEFAULT_PROVIDER", "MAILROOM_API_TOKEN", "MAILROOM_TRACE_MASK", "MAILROOM_BASE_DIR"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.provider == "mock"
    assert s.gpu_usd_per_hour == 0.80
    assert s.api_token is None
    assert s.trace_mask is False
    assert isinstance(s.base_dir, Path)


def test_settings_env(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
    monkeypatch.setenv("VLLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("MAILROOM_API_TOKEN", "tok")
    monkeypatch.setenv("MAILROOM_TRACE_MASK", "1")
    monkeypatch.setenv("MAILROOM_BASE_DIR", "/tmp/mr")
    s = get_settings()
    assert s.provider == "vllm" and s.vllm_base_url == "http://x/v1"
    assert s.openrouter_api_key == "k" and s.api_token == "tok" and s.trace_mask is True
    assert s.base_dir == Path("/tmp/mr")


_JEV_ENV = (
    "MAILROOM_JEV_PROVIDER",
    "JEV_PROVIDER",
    "MAILROOM_JEV_MODEL",
    "JEV_MODEL",
    "MAILROOM_JEV_BASE_URL",
    "JEV_BASE_URL",
    "MAILROOM_JEV_API_KEY",
    "JEV_API_KEY",
    "MAILROOM_JEV_TEMPERATURE",
    "JEV_TEMPERATURE",
    "MAILROOM_JEV_ACCEPT_THRESHOLD",
    "JEV_ACCEPT_THRESHOLD",
    "MAILROOM_JEV_TIMEOUT_S",
    "JEV_TIMEOUT_S",
    "MAILROOM_JEV_MAX_RETRIES",
    "JEV_MAX_RETRIES",
    "TYPESAFE_API_KEY",
    "OPENROUTER_API_KEY",
    "MAILROOM_OPENROUTER_API_KEY",
)


def _stub_get_settings(monkeypatch, settings: Settings) -> None:
    """Point ``settings.get_settings`` at an explicit ``Settings`` instance.

    The repository's local ``.env`` is loaded into ``os.environ`` at import time
    by a transitive ``crewai`` -> ``load_dotenv()`` call and is also read by
    ``Settings()`` directly, so a plain ``monkeypatch.delenv`` cannot isolate a
    test from it. Replacing ``get_settings`` (with a ``cache_clear`` no-op so the
    shared ``conftest`` teardown keeps working) makes the "nothing is set" cases
    deterministic regardless of whether a developer has a ``.env``.
    """

    def _get_settings() -> Settings:
        return settings

    _get_settings.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(settings_mod, "get_settings", _get_settings)


def _clear_jev_env(monkeypatch):
    for key in _JEV_ENV:
        monkeypatch.delenv(key, raising=False)
    _stub_get_settings(monkeypatch, Settings(_env_file=None))


def _write_env_file(tmp_path: Path, text: str) -> Path:
    """Write a ``.env``-style file and return its path (no process env used)."""
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_jev_config_defaults(monkeypatch):
    _clear_jev_env(monkeypatch)
    cfg = jev_config()
    assert cfg.provider == "off"
    assert cfg.enabled is False
    assert cfg.model == "" and cfg.base_url == ""
    assert cfg.api_key is None
    assert cfg.temperature == 1.0
    assert cfg.accept_threshold == 0.8
    assert cfg.timeout_s == 10.0
    assert cfg.max_retries == 2


def test_jev_config_env_override(monkeypatch):
    _clear_jev_env(monkeypatch)
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "openrouter")
    monkeypatch.setenv("JEV_MODEL", "custom-model")
    monkeypatch.setenv("MAILROOM_JEV_ACCEPT_THRESHOLD", "0.9")
    monkeypatch.setenv("TYPESAFE_API_KEY", "tk")
    cfg = jev_config()
    assert cfg.provider == "openrouter"
    assert cfg.enabled is True
    assert cfg.model == "custom-model"
    assert cfg.base_url == "https://openrouter.ai/api/alpha/decisions"
    assert cfg.api_key == "tk"
    assert cfg.accept_threshold == 0.9


@pytest.mark.parametrize(
    "provider,model,base_url",
    [
        ("openrouter", "typesafe/jev-1.13", "https://openrouter.ai/api/alpha/decisions"),
        ("typesafe", "jev-latest", "https://api.typesafe.ai/v1/systemone"),
        ("local", "jevk5", "http://127.0.0.1:8090/v1/systemone"),
    ],
)
def test_jev_config_provider_defaults(monkeypatch, provider, model, base_url):
    _clear_jev_env(monkeypatch)
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", provider)
    cfg = jev_config()
    assert (cfg.model, cfg.base_url) == (model, base_url)


def test_jev_config_api_key_order(monkeypatch):
    _clear_jev_env(monkeypatch)
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "local")
    assert jev_config().api_key is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    assert jev_config().api_key == "or"
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts")
    assert jev_config().api_key == "ts"
    monkeypatch.setenv("JEV_API_KEY", "jv")
    assert jev_config().api_key == "jv"
    monkeypatch.setenv("MAILROOM_JEV_API_KEY", "mjv")
    assert jev_config().api_key == "mjv"


@pytest.mark.parametrize(
    "provider,expected",
    [("openrouter", "or"), ("typesafe", "ts")],
)
def test_jev_config_api_key_prefers_provider_key(monkeypatch, provider, expected):
    _clear_jev_env(monkeypatch)
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", provider)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts")

    assert jev_config().api_key == expected


@pytest.mark.parametrize("provider", ["openrouter", "typesafe"])
def test_jev_config_jev_api_key_overrides_provider_key(monkeypatch, provider):
    _clear_jev_env(monkeypatch)
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", provider)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts")
    monkeypatch.setenv("JEV_API_KEY", "jv")

    assert jev_config().api_key == "jv"


# ------------------------------------------------------------------ STEP 0 fix:
# The pydantic ``Settings`` model must carry the JEV_* knobs (with prefixed and
# bare aliases) and ``jev_config`` must fall back to it, so a ``.env``-only
# ``MAILROOM_JEV_PROVIDER`` is no longer ignored.


def test_settings_jev_fields_default_unset(monkeypatch):
    _clear_jev_env(monkeypatch)
    s = Settings(_env_file=None)
    assert (s.jev_provider, s.jev_model, s.jev_base_url, s.jev_api_key) == (
        None,
        None,
        None,
        None,
    )
    assert (
        s.jev_temperature,
        s.jev_accept_threshold,
        s.jev_timeout_s,
        s.jev_max_retries,
    ) == (None, None, None, None)


def test_settings_jev_fields_from_env_file(tmp_path, monkeypatch):
    """Prefixed and bare names in a `.env` file populate the Settings fields."""
    _clear_jev_env(monkeypatch)
    env = _write_env_file(
        tmp_path,
        "MAILROOM_JEV_PROVIDER=openrouter\n"
        "JEV_MODEL=envfile-model\n"
        "JEV_BASE_URL=https://example.test/decisions\n"
        "JEV_TEMPERATURE=1.22\n"
        "JEV_ACCEPT_THRESHOLD=0.9\n"
        "JEV_TIMEOUT_S=3\n"
        "JEV_MAX_RETRIES=4\n",
    )
    s = Settings(_env_file=env)
    assert s.jev_provider == "openrouter"
    assert s.jev_model == "envfile-model"
    assert s.jev_base_url == "https://example.test/decisions"
    assert s.jev_temperature == 1.22
    assert s.jev_accept_threshold == 0.9
    assert s.jev_timeout_s == 3.0
    assert s.jev_max_retries == 4


def test_settings_jev_bare_aliases(tmp_path, monkeypatch):
    _clear_jev_env(monkeypatch)
    env = _write_env_file(tmp_path, "JEV_PROVIDER=typesafe\nJEV_API_KEY=bare-key\n")
    s = Settings(_env_file=env)
    assert s.jev_provider == "typesafe"
    assert s.jev_api_key == "bare-key"


def test_settings_jev_empty_lines_are_unset(tmp_path, monkeypatch):
    """Blank `.env` lines (as shipped in .env.example) mean "unset", not a crash."""
    _clear_jev_env(monkeypatch)
    env = _write_env_file(
        tmp_path, "MAILROOM_JEV_PROVIDER=\nMAILROOM_JEV_TEMPERATURE=\nMAILROOM_JEV_MAX_RETRIES=\n"
    )
    s = Settings(_env_file=env)
    assert s.jev_provider is None
    assert s.jev_temperature is None
    assert s.jev_max_retries is None


def test_jev_config_enabled_from_settings_env_file(tmp_path, monkeypatch):
    """A `.env`-only provider enables Jev and supplies the model (the fix)."""
    _clear_jev_env(monkeypatch)
    env = _write_env_file(
        tmp_path, "MAILROOM_JEV_PROVIDER=openrouter\nJEV_MODEL=envfile-model\n"
    )
    _stub_get_settings(monkeypatch, Settings(_env_file=env))

    cfg = jev_config()
    assert cfg.provider == "openrouter"
    assert cfg.enabled is True
    assert cfg.model == "envfile-model"
    assert cfg.base_url == "https://openrouter.ai/api/alpha/decisions"


def test_jev_env_os_environ_beats_settings_env_file(tmp_path, monkeypatch):
    """os.environ still wins over a Settings/env-file value."""
    _clear_jev_env(monkeypatch)
    env = _write_env_file(tmp_path, "MAILROOM_JEV_PROVIDER=typesafe\n")
    _stub_get_settings(monkeypatch, Settings(_env_file=env))
    monkeypatch.setenv("MAILROOM_JEV_PROVIDER", "openrouter")

    assert jev_config().provider == "openrouter"


def test_jev_config_off_when_nothing_set(tmp_path, monkeypatch):
    """No env, no taxonomy override, blank env file -> Jev stays off."""
    _clear_jev_env(monkeypatch)
    env = _write_env_file(tmp_path, "MAILROOM_JEV_PROVIDER=\n")
    _stub_get_settings(monkeypatch, Settings(_env_file=env))

    cfg = jev_config()
    assert cfg.provider == "off"
    assert cfg.enabled is False
    assert cfg.api_key is None
