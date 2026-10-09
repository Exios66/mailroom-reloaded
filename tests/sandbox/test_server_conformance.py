"""Conformance harness: deterministic split, isolation, tag selection, and summary arithmetic."""

from __future__ import annotations

from mailroom_reloaded.sandbox.server.conformance import (
    format_lofo,
    heldout_ids,
    lofo,
    run_conformance,
    split_of,
    summarise,
)


def test_split_is_deterministic_every_third_held_out():
    """Verify positional splitting sorts IDs and selects every third scenario."""
    ids = [f"X{i}" for i in range(9)]
    sp = split_of(list(reversed(ids)))
    assert sp == split_of(ids)
    assert sorted(n for n, s in sp.items() if s == "heldout") == ["X2", "X5", "X8"]


def test_summarise_counts_and_failed_checks():
    """Verify summary totals, split counts, and failed-check counts agree."""
    rows = [
        {"scenario": "a", "split": "tuned", "verdict": "pass", "failed_checks": []},
        {
            "scenario": "b",
            "split": "heldout",
            "verdict": "fail",
            "failed_checks": ["intent"],
        },
        {"scenario": "c", "split": "tuned", "verdict": "shed", "failed_checks": []},
    ]
    out = summarise(rows)
    assert out["all"] == {"pass": 1, "fail": 1, "not_run": 0, "shed": 1, "total": 3}
    assert out["heldout"]["fail"] == 1 and out["failed_checks"] == {"intent": 1}


def test_isolated_run_on_smoke_pack(idle_service):
    """Verify isolated smoke scenarios all pass without shedding."""
    svc = idle_service.start(worker=False)
    try:
        res = run_conformance(svc)
    finally:
        svc.stop()
    assert res["all"]["total"] == 6 and res["all"]["shed"] == 0
    assert res["all"]["pass"] == 6


def test_heldout_ids_selects_tagged_scenarios_only():
    """Verify held-out selection includes only scenarios with the explicit tag."""
    scenarios = {
        "A1_tuned": {"tags": ["smoke"]},
        "H1_frozen": {"tags": ["heldout", "wire"]},
        "H2_frozen": {"tags": ["heldout"]},
        "B1_untagged": {},
    }
    assert heldout_ids(scenarios) == ["H1_frozen", "H2_frozen"]
    assert heldout_ids({}) == []


def test_run_conformance_heldout_flag_selects_tagged(idle_service):
    """Verify held-out mode runs and labels only explicitly tagged scenarios."""
    svc = idle_service
    svc.content.cs.scenarios["A1_status_inquiry"]["tags"] = ["heldout"]
    svc = svc.start(worker=False)
    try:
        res = run_conformance(svc, heldout=True)
    finally:
        svc.stop()
    assert res["all"]["total"] == 1
    assert res["heldout"]["total"] == 1 and res["tuned"]["total"] == 0
    assert res["scenarios"][0]["scenario"] == "A1_status_inquiry"
    assert res["scenarios"][0]["split"] == "heldout"


def test_run_conformance_heldout_flag_with_no_tagged_is_empty(idle_service):
    """Verify held-out mode returns empty totals when no scenarios carry the tag."""
    svc = idle_service.start(worker=False)
    try:
        res = run_conformance(svc, heldout=True)
    finally:
        svc.stop()
    assert res["all"]["total"] == 0
    assert res["heldout"]["total"] == 0


def test_lofo_single_family_has_no_training_rate():
    """Verify LOFO reports absent training rates until another family is available."""
    import json

    rows = [
        {"scenario": "A1", "verdict": "pass", "failed_checks": []},
        {"scenario": "A2", "verdict": "fail", "failed_checks": ["intent"]},
    ]
    rep = lofo(rows)
    fold = rep["folds"][0]
    assert fold["training_n"] == fold["training_pass"] == 0
    assert fold["training_rate"] is None
    assert fold["held_out_rate"] == 0.5
    assert rep["macro_mean_held_out_rate"] == rep["micro_pass_rate"] == 0.5
    assert "n/a" in format_lofo(rep)
    assert '"training_rate": null' in json.dumps(rep)

    rows.append({"scenario": "B1", "verdict": "pass", "failed_checks": []})
    rep = lofo(rows)
    assert [f["training_rate"] for f in rep["folds"]] == [1.0, 0.5]
    assert "n/a" not in format_lofo(rep)
    assert format_lofo(rep).endswith(
        "macro mean held-out rate 0.750; micro pass rate 0.667"
    )


def test_lofo_empty_rows_returns_none_rates():
    """Verify empty LOFO input yields no folds and displays unavailable rates."""
    rep = lofo([])
    assert rep["folds"] == []
    assert rep["macro_mean_held_out_rate"] is None
    assert rep["micro_pass_rate"] is None
    assert "n/a" in format_lofo(rep)


def test_lofo_empty_input_has_unavailable_aggregate_rates():
    from mailroom_reloaded.sandbox.server.conformance import format_lofo, lofo

    rep = lofo([])
    assert rep["folds"] == []
    assert rep["macro_mean_held_out_rate"] is None
    assert rep["micro_pass_rate"] is None
    assert format_lofo(rep).endswith(
        "macro mean held-out rate n/a; micro pass rate n/a"
    )
