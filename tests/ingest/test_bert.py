import sys
import types

import pytest

from mailroom_reloaded.ingest import bert
from mailroom_reloaded.ingest.bert import (
    BertVerdict,
    SortMode,
    classify_primary,
    decide_handoff,
)
from mailroom_reloaded.settings import BertCfg


def cfg(**kw):
    base = {"enabled": True, "defer_classes": ["contract", "merger_agreement"], "max_trusted_windows": 1}
    base.update(kw)
    return BertCfg(**base)


def verdict(**kw):
    base = {
        "available": True, "reason": "ok", "doc_type": "correspondence", "subclass": "email",
        "calibrated_confidence": 0.97, "margin": 0.8, "window_agreement": 1.0, "n_windows": 1,
        "route": "fast_path",
    }
    base.update(kw)
    return BertVerdict(**base)


def install_fake(monkeypatch, fn):
    pkg = types.ModuleType("mailroom_ml")
    inf = types.ModuleType("mailroom_ml.inference")
    inf.classify_document_default = fn
    inf.classify_document = fn
    pkg.inference = inf
    monkeypatch.setitem(sys.modules, "mailroom_ml", pkg)
    monkeypatch.setitem(sys.modules, "mailroom_ml.inference", inf)


# ---- spec §5 table, one test per row ----
def test_unavailable_full():
    h = decide_handoff(BertVerdict(available=False, reason="error"), cfg())
    assert h.mode is SortMode.FULL and h.locked_doc_type is None and h.prior == ""


def test_flag_off_full():
    h = decide_handoff(BertVerdict(available=False, reason="flag_off"), cfg(enabled=False))
    assert h.mode is SortMode.FULL


def test_contract_defers():
    h = decide_handoff(verdict(doc_type="contract", subclass="nda"), cfg())
    assert h.mode is SortMode.FULL and h.locked_doc_type is None
    assert "contract" in h.prior


def test_merger_defers():
    h = decide_handoff(verdict(doc_type="merger_agreement", subclass=None), cfg())
    assert h.mode is SortMode.FULL and "merger_agreement" in h.prior


def test_multiwindow_defers():
    h = decide_handoff(verdict(n_windows=3), cfg())
    assert h.mode is SortMode.FULL and h.locked_doc_type is None
    assert "correspondence" in h.prior


def test_fast_path_correspondence_subclass_only():
    h = decide_handoff(verdict(), cfg())
    assert h.mode is SortMode.SUBCLASS_ONLY
    assert h.locked_doc_type == "correspondence"
    assert "correspondence" in h.prior


def test_otherwise_full_with_prior():
    h = decide_handoff(verdict(route="clerk_only"), cfg())
    assert h.mode is SortMode.FULL and h.locked_doc_type is None
    assert "correspondence" in h.prior


def test_available_without_doc_type_full():
    h = decide_handoff(verdict(doc_type=None, route="fast_path"), cfg())
    assert h.mode is SortMode.FULL and h.prior == ""


def test_subclass_hint_only_when_enabled():
    off = decide_handoff(verdict(), cfg(pass_subclass_hint=False))
    on = decide_handoff(verdict(), cfg(pass_subclass_hint=True))
    assert "email" not in off.prior
    assert "email" in on.prior


# ---- classify_primary ----
def test_flag_off_does_not_import(monkeypatch):
    install_fake(monkeypatch, lambda *a, **k: pytest.fail("called"))
    v = classify_primary("x", cfg(enabled=False))
    assert not v.available and v.reason == "flag_off"


def test_package_missing_reason(monkeypatch):
    monkeypatch.setitem(sys.modules, "mailroom_ml", None)
    monkeypatch.setitem(sys.modules, "mailroom_ml.inference", None)
    v = classify_primary("hello", cfg())
    assert not v.available and v.reason == "no_package"


def test_ok_mapping(monkeypatch):
    def fn(text, **kw):
        return {
            "status": "ok", "doc_type": "correspondence", "subclass": "email",
            "route": "fast_path", "calibrated_confidence": 0.93, "margin": 0.5,
            "agreement": 1.0, "n_windows": 1,
        }

    install_fake(monkeypatch, fn)
    v = classify_primary("hello", cfg())
    assert v.available and v.reason == "ok"
    assert (v.doc_type, v.subclass, v.route) == ("correspondence", "email", "fast_path")
    assert v.calibrated_confidence == 0.93 and v.margin == 0.5
    assert v.window_agreement == 1.0 and v.n_windows == 1


def test_default_seam_receives_filename(monkeypatch):
    seen = {}

    def fn(text, *, filename=None, **kw):
        seen["text"] = text
        seen["filename"] = filename
        return {"doc_type": "contract", "n_windows": 1}

    install_fake(monkeypatch, fn)
    assert classify_primary("hello", cfg()).doc_type == "contract"
    assert seen == {"text": "hello", "filename": None}
    classify_primary("hello", cfg(), filename="agreement.txt")
    assert seen == {"text": "hello", "filename": "agreement.txt"}


def test_prefers_default_seam_over_raw_classify(monkeypatch):
    calls = []
    pkg = types.ModuleType("mailroom_ml")
    inf = types.ModuleType("mailroom_ml.inference")
    inf.classify_document_default = (
        lambda text, *, filename=None: calls.append("default")
        or {"status": "ok", "doc_type": "contract", "n_windows": 1}
    )
    inf.classify_document = lambda *a, **k: calls.append("document")
    pkg.inference = inf
    monkeypatch.setitem(sys.modules, "mailroom_ml", pkg)
    monkeypatch.setitem(sys.modules, "mailroom_ml.inference", inf)
    assert classify_primary("hello", cfg()).doc_type == "contract"
    assert calls == ["default"]


def test_error_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    install_fake(monkeypatch, boom)
    v = classify_primary("hello", cfg())
    assert not v.available and v.reason == "error"


def test_non_dict_result_is_error(monkeypatch):
    install_fake(monkeypatch, lambda *a, **k: None)
    assert classify_primary("hello", cfg()).reason == "error"


def test_missing_model_file(monkeypatch):
    def fn(*a, **k):
        raise FileNotFoundError("bundle")

    install_fake(monkeypatch, fn)
    assert classify_primary("hello", cfg()).reason == "no_model"


def test_missing_model_marker(monkeypatch):
    install_fake(monkeypatch, lambda *a, **k: {"status": "bundle_missing"})
    assert classify_primary("hello", cfg()).reason == "no_model"


@pytest.mark.parametrize("marker", ["no_model", "bundle_missing", "model_missing"])
def test_default_seam_missing_bundle_marker(monkeypatch, marker):
    def fn(text, *, filename=None, **kw):
        return {"status": "failure", "route": "llm", "reason": marker, "doc_type": None}

    install_fake(monkeypatch, fn)
    v = classify_primary("hello", cfg())
    assert not v.available and v.reason == "no_model"
    assert decide_handoff(v, cfg()).mode is SortMode.FULL


def test_route_llm_without_doc_type_is_unavailable(monkeypatch):
    def fn(text, *, filename=None, **kw):
        return {"status": "ok", "route": "llm", "reason": "gate_fail", "doc_type": None}

    install_fake(monkeypatch, fn)
    v = classify_primary("hello", cfg())
    assert not v.available
    assert decide_handoff(v, cfg()).mode is SortMode.FULL


def test_model_side_oversize_maps_too_long(monkeypatch):
    def fn(text, *, filename=None, **kw):
        return {"status": "ok", "route": "llm", "reason": "oversize_chars", "doc_type": None}

    install_fake(monkeypatch, fn)
    assert classify_primary("hello", cfg()).reason == "too_long"


def test_default_seam_ok_maps_verdict_fields(monkeypatch):
    def fn(text, *, filename=None, **kw):
        assert filename == "doc.txt"
        return {
            "status": "ok",
            "doc_type": "correspondence",
            "subclass": "email",
            "route": "fast_path",
            "calibrated_confidence": 0.93,
            "subclass_confidence": 0.8,
            "margin": 0.5,
            "agreement": 1.0,
            "n_windows": 1,
            "n_class_windows": 1,
            "per_head": {"doc_type": {"pred": "correspondence", "windows": ["correspondence"]}},
        }

    install_fake(monkeypatch, fn)
    v = classify_primary("hello", cfg(), filename="doc.txt")
    assert v.available and v.reason == "ok"
    assert (v.doc_type, v.subclass, v.route) == ("correspondence", "email", "fast_path")
    assert v.calibrated_confidence == 0.93 and v.margin == 0.5
    assert v.window_agreement == 1.0 and v.n_windows == 1


def test_long_doc_does_not_call_model_past_cap(monkeypatch):
    calls = []
    install_fake(monkeypatch, lambda *a, **k: calls.append(1) or {})
    text = "x" * 400_000
    v = classify_primary(text, cfg())
    assert calls == []
    assert not v.available and v.reason == "too_long"
    h = decide_handoff(v, cfg())
    assert h.mode is SortMode.FULL and h.reason


def test_at_cap_still_calls(monkeypatch):
    calls = []
    install_fake(monkeypatch, lambda *a, **k: calls.append(1) or {"doc_type": "contract"})
    classify_primary("x" * (8192 * 4), cfg())
    assert calls == [1]


def test_cfg_loaded_from_taxonomy_when_none(monkeypatch):
    seen = {}
    monkeypatch.setattr(bert, "load_taxonomy", lambda: types.SimpleNamespace(bert=cfg(enabled=False)))
    seen["v"] = classify_primary("x")
    assert seen["v"].reason == "flag_off"


def test_legacy_classifier_receives_text_without_filename_keyword(monkeypatch):
    seen = []

    def legacy(text):
        seen.append(text)
        return {"doc_type": "correspondence", "agreement": 0.9, "n_windows": 1}

    install_fake(monkeypatch, legacy)
    monkeypatch.delattr(sys.modules["mailroom_ml.inference"], "classify_document_default")
    result = classify_primary("letter text", cfg(), filename="letter.txt")
    assert result.available
    assert result.doc_type == "correspondence"
    assert result.window_agreement == 0.9
    assert seen == ["letter text"]


@pytest.mark.parametrize("error,reason", [(FileNotFoundError("bundle"), "no_model"), (RuntimeError("failed"), "error")])
def test_legacy_classifier_failure_falls_back_to_full_sort(monkeypatch, error, reason):
    def legacy(text):
        raise error

    install_fake(monkeypatch, legacy)
    monkeypatch.delattr(sys.modules["mailroom_ml.inference"], "classify_document_default")
    result = classify_primary("letter", cfg(), filename="letter.txt")
    assert result == BertVerdict(available=False, reason=reason)
    assert decide_handoff(result, cfg()).mode is SortMode.FULL


def test_installed_package_without_supported_entry_point_is_unavailable(monkeypatch):
    install_fake(monkeypatch, None)
    result = classify_primary("letter", cfg())
    assert result == BertVerdict(available=False, reason="no_package")


@pytest.mark.parametrize("failure", [{"status": "failure"}, {"reason": "bert_error"}])
def test_failure_marker_overrides_plausible_prediction(monkeypatch, failure):
    install_fake(monkeypatch, lambda *args, **kwargs: {
        "doc_type": "correspondence", "route": "fast_path", "calibrated_confidence": 0.99,
        **failure,
    })
    result = classify_primary("letter", cfg())
    assert result == BertVerdict(available=False, reason="error")
    handoff = decide_handoff(result, cfg())
    assert handoff.mode is SortMode.FULL
    assert handoff.locked_doc_type is None
    assert handoff.prior == ""
