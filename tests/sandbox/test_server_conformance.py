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
