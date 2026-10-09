"""Conformance harness: deterministic split, isolation, and summary arithmetic."""

from __future__ import annotations

from mailroom_reloaded.sandbox.server.conformance import (
    run_conformance,
    split_of,
    summarise,
)


def test_split_is_deterministic_every_third_held_out():
    ids = [f"X{i}" for i in range(9)]
    sp = split_of(list(reversed(ids)))
    assert sp == split_of(ids)
    assert sorted(n for n, s in sp.items() if s == "heldout") == ["X2", "X5", "X8"]


def test_summarise_counts_and_failed_checks():
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
    svc = idle_service.start(worker=False)
    try:
        res = run_conformance(svc)
    finally:
        svc.stop()
    assert res["all"]["total"] == 6 and res["all"]["shed"] == 0
    assert res["all"]["pass"] == 6


def test_lofo_single_family_has_no_training_rate():
    import json

    from mailroom_reloaded.sandbox.server.conformance import format_lofo, lofo

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
