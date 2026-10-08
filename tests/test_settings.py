from pathlib import Path

from mailroom_reloaded.settings import (
    RunConditions,
    Settings,
    get_settings,
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
    assert isinstance(s.base_dir, Path)


def test_settings_env(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
    monkeypatch.setenv("VLLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("MAILROOM_API_TOKEN", "tok")
    monkeypatch.setenv("MAILROOM_TRACE_MASK", "true")
    monkeypatch.setenv("MAILROOM_BASE_DIR", "/tmp/mr")
    s = get_settings()
    assert s.provider == "vllm" and s.vllm_base_url == "http://x/v1"
    assert s.openrouter_api_key == "k" and s.api_token == "tok" and s.trace_mask is True
    assert s.base_dir == Path("/tmp/mr")
