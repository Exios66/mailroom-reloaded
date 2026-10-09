"""Ledger intake contracts without a writer thread, model calls or wall-clock waits."""

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.obs.run_context import RunScope
from mailroom_reloaded.pipeline import run_ledger
from mailroom_reloaded.pipeline.state import MailroomState
from mailroom_reloaded.schemas.ledger import canonical_json
from mailroom_reloaded.storage.ledger import Ledger


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    for name in ("_open", "_invocations"):
        monkeypatch.setattr(run_ledger, name, {})
    for name in ("_closed", "_rolled"):
        monkeypatch.setattr(run_ledger, name, set())


@pytest.fixture
def ledger(monkeypatch):
    ledger = Mock(spec=Ledger)
    ledger.count.return_value = 0
    ledger.open_runs.return_value = []
    ledger.append.return_value = True
    monkeypatch.setattr(run_ledger.audit_log, "entries", Mock(return_value=[]))
    monkeypatch.setattr(run_ledger.time, "monotonic", lambda: 20.0)
    # Distinct prices make accidental grader inclusion or wrong role pricing visible.
    prices = {"sorter": 0.01, "judge": 0.02, "grader": 0.5}
    monkeypatch.setattr(
        run_ledger,
        "cost_for",
        lambda role, usage: prices.get(role, 0.03) * usage.total_tokens,
    )
    return ledger


def test_snapshot_preserves_usage_before_in_place_state_updates():
    prior = Usage(10, 2, calls=1)
    state = MailroomState(
        usage_total=prior, usage_by_role={"sorter": prior}, usage_partial_nodes=["sort"]
    )
    baseline = run_ledger.snapshot(state)
    state.usage_total += Usage(3, 1, calls=1)
    state.usage_by_role["sorter"] += Usage(3, 1, calls=1)
    state.usage_by_role["judge"] = Usage(2, 1, calls=1)
    state.usage_partial_nodes.append("verify")

    assert baseline.usage_total == prior
    assert baseline.by_role == {"sorter": prior}
    assert baseline.partial_nodes == ("sort",)


@pytest.mark.parametrize("new_partial", [[], ["verify"]])
def test_resumed_document_records_only_new_spend_and_partial_nodes(ledger, new_partial):
    prior = Usage(10, 4, calls=1)
    state = MailroomState(
        doc_id="doc",
        status="archived",
        usage_total=prior,
        usage_by_role={"sorter": prior},
        usage_partial_nodes=["sort"],
    )
    baseline = run_ledger.snapshot(state)
    baseline.started = 17.5
    state.usage_total += Usage(5, 3, calls=2)
    state.usage_by_role.update(
        judge=Usage(5, 3, calls=2), grader=Usage(100, 50, calls=1)
    )
    state.usage_partial_nodes.extend(new_partial)
    state.route_trail.extend(["sort:accept", "extract:accept"])

    run_ledger.record_document(
        ledger,
        RunScope("eval-run", "eval", "eval"),
        state,
        baseline,
        doc_type="correspondence",
    )

    ledger.append.assert_called_once()
    args, kwargs = ledger.append.call_args
    assert args == ("doc_closed", "eval-run")
    assert kwargs["doc_id"] == "doc"
    payload = kwargs["payload"]
    assert payload["usage_by_role"] == {
        "judge": {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "calls": 2,
            "cost_usd": 0.16,
        },
        "grader": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "calls": 1,
            "cost_usd": 75.0,
        },
    }
    assert payload["usage_partial_nodes"] == new_partial
    assert payload["usage_complete"] is (not new_partial)
    assert payload["duration_s"] == 2.5
    assert payload["audit_head"] is None
    assert payload["route_trail"] == ["sort:accept", "extract:accept"]
    metrics = {r.name: (r.value, r.tier) for r in kwargs["metrics"]}
    assert metrics["usage.total_tokens"] == (8, 1)
    assert metrics["usage.calls"] == (2, 1)
    assert metrics["usage.cost_usd"] == (0.16, 1)
    assert metrics["usage.grader.cost_usd"] == (75.0, 2)
    assert payload["rows"] == len(kwargs["metrics"])
    digest_input = [[r.name, r.value, r.tier] for r in kwargs["metrics"]]
    assert (
        payload["metrics_digest"]
        == hashlib.sha256(canonical_json(digest_input).encode("utf-8")).hexdigest()
    )
    state.route_trail.append("later")
    assert payload["route_trail"] == ["sort:accept", "extract:accept"]


@pytest.mark.parametrize(
    "status,aborted,outcome",
    [
        ("archived", False, "completed"),
        ("failed", False, "failed"),
        ("parked", False, "parked"),
        ("processing", False, "aborted"),
        ("archived", True, "aborted"),
    ],
)
def test_document_outcome_and_failure_fields(ledger, status, aborted, outcome):
    run_ledger.record_document(
        ledger,
        RunScope("eval-run", "eval"),
        MailroomState(doc_id="doc", status=status),
        run_ledger.Baseline(started=20),
        doc_type=None,
        failure="token_budget_exceeded",
        aborted=aborted,
    )
    payload = ledger.append.call_args.kwargs["payload"]
    assert payload["outcome"] == outcome
    if outcome in {"failed", "aborted"}:
        assert payload["failure_reason"] == "token_budget_exceeded"
        assert payload["failure_class"] == "run_budget"
    else:
        assert "failure_reason" not in payload
        assert "failure_class" not in payload
    completed = next(
        r
        for r in ledger.append.call_args.kwargs["metrics"]
        if r.name == "doc.completed"
    )
    assert completed.value == (1.0 if outcome == "completed" else 0.0)


def test_metrics_include_zero_confidences_and_preserve_tiers(ledger):
    state = SimpleNamespace(
        classify_attempts=2,
        extract_attempts=3,
        route_trail=["sort", "extract"],
        resorted=True,
        sort=SimpleNamespace(confidence=0.0),
        extract=SimpleNamespace(confidence=0.0),
    )
    rows = run_ledger.doc_metric_rows(
        state,
        duration_s=1.234567,
        deltas={"sorter": Usage(2, 3, calls=1)},
        total=Usage(2, 3, calls=1),
        cost=0.123456789,
        outcome="failed",
    )
    assert {r.name: (r.value, r.tier) for r in rows} == {
        "doc.duration_s": (1.2346, 0),
        "doc.completed": (0.0, 0),
        "usage.total_tokens": (5, 1),
        "usage.calls": (1, 1),
        "usage.cost_usd": (0.12345679, 1),
        "attempts.classify": (2, 1),
        "attempts.extract": (3, 1),
        "route.steps": (2, 1),
        "route.resorted": (1.0, 1),
        "confidence.sort": (0.0, 1),
        "confidence.extract": (0.0, 1),
        "usage.sorter.prompt_tokens": (2, 2),
        "usage.sorter.completion_tokens": (3, 2),
        "usage.sorter.calls": (1, 2),
        "usage.sorter.cost_usd": (0.05, 2),
    }


@pytest.mark.parametrize("extract", [None, SimpleNamespace(confidence=None)])
def test_metrics_omit_missing_confidences(ledger, extract):
    state = SimpleNamespace(
        classify_attempts=0,
        extract_attempts=0,
        route_trail=[],
        resorted=False,
        sort=None,
        extract=extract,
    )
    rows = run_ledger.doc_metric_rows(
        state, duration_s=0, deltas={}, total=Usage(), cost=0, outcome="parked"
    )
    assert len(rows) == 9
    assert not any(r.name.startswith("confidence.") for r in rows)


@pytest.mark.parametrize(
    "failure,reason,failure_class",
    [
        (TimeoutError("private path"), "llm_error", "llm_timeout"),
        (OSError("private path"), "io_error", "io_error"),
        (ValueError("private path"), "schema_invalid", "schema_error"),
        (RuntimeError("private path"), "unexpected", "unexpected"),
    ],
)
def test_aborted_document_bounds_errors_and_retains_known_spend(
    ledger, failure, reason, failure_class
):
    run_ledger.record_aborted(
        ledger,
        "run",
        "doc",
        failure,
        doc_type="correspondence",
        started=18.25,
        usage_by_role={"sorter": Usage(2, 3, calls=1), "judge": Usage()},
    )
    payload = ledger.append.call_args.kwargs["payload"]
    assert payload == {
        "invocation": 1,
        "outcome": "aborted",
        "doc_type": "correspondence",
        "audit_head": None,
        "duration_s": 1.75,
        "usage_complete": False,
        "usage_by_role": {
            "sorter": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "calls": 1,
                "cost_usd": 0.05,
            }
        },
        "failure_reason": reason,
        "failure_class": failure_class,
    }


@pytest.mark.parametrize("schema_valid", [True, False])
@pytest.mark.parametrize(
    "doc_type,role",
    [("correspondence", "correspondence_specialist"), ("unknown", "specialist")],
)
def test_cell_records_schema_outcome_and_specialist_usage(
    ledger, schema_valid, doc_type, role
):
    result = SimpleNamespace(schema_valid=schema_valid, usage=Usage(4, 6, calls=2))
    run_ledger.record_cell(ledger, "run", "doc", doc_type, result, started=19)
    payload = ledger.append.call_args.kwargs["payload"]
    assert payload["outcome"] == ("completed" if schema_valid else "failed")
    assert payload["usage_complete"] is True
    assert payload["duration_s"] == 1
    assert payload["usage_by_role"] == {
        role: {"prompt_tokens": 4, "completion_tokens": 6, "calls": 2, "cost_usd": 0.3}
    }
    if schema_valid:
        assert "failure_reason" not in payload and "failure_class" not in payload
    else:
        assert payload["failure_reason"] == "schema_invalid"
        assert payload["failure_class"] == "schema_error"


def test_reconciliation_links_audit_head_without_claiming_known_spend(
    ledger, monkeypatch
):
    head = SimpleNamespace(seq=9, entry_hash="ab" * 32)
    monkeypatch.setattr(run_ledger.audit_log, "entries", Mock(return_value=[head]))
    run_ledger.record_reconciled(ledger, "run", "doc", "correspondence")
    ledger.append.assert_called_once_with(
        "doc_closed",
        "run",
        doc_id="doc",
        payload={
            "invocation": 1,
            "outcome": "reconciled",
            "doc_type": "correspondence",
            "audit_head": {"seq": 9, "entry_hash": "ab" * 32},
            "usage_complete": False,
        },
    )


def test_invocations_resume_from_persisted_count_and_are_scoped_to_run_and_doc(ledger):
    ledger.count.side_effect = [3, 0, 1]
    for run, doc in [("r1", "d1"), ("r1", "d1"), ("r1", "d2"), ("r2", "d1")]:
        run_ledger.record_reconciled(ledger, run, doc)
    assert [
        c.kwargs["payload"]["invocation"] for c in ledger.append.call_args_list
    ] == [4, 5, 1, 2]
    assert [c.args for c in ledger.count.call_args_list] == [
        ("doc_closed", "r1", "d1"),
        ("doc_closed", "r1", "d2"),
        ("doc_closed", "r2", "d1"),
    ]


@pytest.mark.parametrize("hook", ["document", "aborted", "cell", "reconciled"])
@pytest.mark.parametrize("failure_site", ["audit", "append"])
def test_document_hooks_swallow_ledger_failures(
    ledger, monkeypatch, hook, failure_site
):
    failure = Mock(side_effect=OSError("disk unavailable"))
    if failure_site == "audit":
        monkeypatch.setattr(run_ledger.audit_log, "entries", failure)
    else:
        ledger.append.side_effect = failure
    if hook == "document":
        run_ledger.record_document(
            ledger,
            RunScope("run", "eval"),
            MailroomState(doc_id="doc"),
            run_ledger.Baseline(started=20),
            doc_type=None,
        )
    elif hook == "aborted":
        run_ledger.record_aborted(ledger, "run", "doc", ValueError("original"))
    elif hook == "cell":
        run_ledger.record_cell(
            ledger,
            "run",
            "doc",
            "unknown",
            SimpleNamespace(schema_valid=True, usage=Usage()),
        )
    else:
        run_ledger.record_reconciled(ledger, "run", "doc")
    failure.assert_called_once()
    if failure_site == "audit":
        ledger.append.assert_not_called()


def test_eval_scope_does_not_open_or_roll_live_runs(ledger):
    assert (
        run_ledger.ensure_live_run(ledger, RunScope("eval-run", "eval")) == "eval-run"
    )
    assert ledger.mock_calls == []


def test_persisted_open_run_is_adopted_without_duplicate_boundary(ledger):
    ledger.count.return_value = 1
    ledger.open_runs.return_value = ["run"]
    assert run_ledger.open_run(ledger, "run", "live") is True
    assert run_ledger.open_run(ledger, "run", "live") is True
    ledger.append.assert_not_called()
    ledger.count.assert_called_once_with("run_opened", "run")
    run_ledger.close_run(ledger, "run", expected=0)
    assert ledger.append.call_args.kwargs["payload"] == {
        "closed_by": "completed",
        "expected": 0,
        "counts": {},
    }
    assert run_ledger.open_run(ledger, "run", "live") is False


def test_rollover_distinguishes_owned_and_abandoned_runs(ledger):
    assert run_ledger.open_run(ledger, "owned", "live")
    assert run_ledger.open_run(ledger, "eval-run", "eval")
    ledger.append.reset_mock()
    ledger.open_runs.return_value = ["abandoned", "owned", "today"]
    run_ledger.close_other_live_runs(ledger, "today")
    ledger.open_runs.assert_called_once_with("live")
    assert [
        (c.args[1], c.kwargs["payload"]["closed_by"])
        for c in ledger.append.call_args_list
    ] == [("owned", "completed"), ("abandoned", "interrupted")]


def test_close_run_failure_is_best_effort(ledger):
    ledger.append.side_effect = OSError("disk full")
    run_ledger.close_run(ledger, "run", "interrupted", expected=2, counts={"failed": 1})
    ledger.append.assert_called_once_with(
        "run_closed",
        "run",
        payload={"closed_by": "interrupted", "expected": 2, "counts": {"failed": 1}},
    )
