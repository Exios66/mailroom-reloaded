"""Run scope and attribute vocabulary (trace-replay Task 1)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from openai import APIConnectionError, APITimeoutError, RateLimitError

from mailroom_reloaded.obs import attrs
from mailroom_reloaded.obs.run_context import (
    current_run,
    ensure_run_scope,
    environment_name,
    live_run_id,
    run_scope,
)
from mailroom_reloaded.pipeline.state import NODE_ORDER


def test_no_scope_by_default() -> None:
    assert current_run() is None


def test_nested_scopes_restore() -> None:
    with run_scope("outer", "eval", "eval"):
        assert current_run().run_id == "outer"
        with run_scope("inner"):
            assert current_run().run_id == "inner"
            assert current_run().environment == "live"
        assert current_run().run_id == "outer"
    assert current_run() is None


def test_scope_restored_after_exception() -> None:
    with pytest.raises(RuntimeError), run_scope("boom"):
        raise RuntimeError("x")
    assert current_run() is None


def test_run_id_is_sanitised() -> None:
    with run_scope("a b/c:d\n") as scope:
        assert scope.run_id == "a_b_c_d_"
    with run_scope("ok\n") as scope:  # a trailing newline must not slip past the pattern
        assert scope.run_id == "ok_"
    with run_scope("") as scope:
        assert scope.run_id == "unscoped"
    with run_scope("x" * 200) as scope:
        assert len(scope.run_id) == 64


def test_scope_crosses_asyncio_tasks_and_to_thread() -> None:
    async def inner() -> tuple[str, str]:
        in_task = current_run().run_id
        in_thread = await asyncio.to_thread(lambda: current_run().run_id)
        return in_task, in_thread

    with run_scope("evalrun", "eval"):
        assert asyncio.run(inner()) == ("evalrun", "evalrun")


def test_scope_does_not_cross_thread_pool_so_workers_open_their_own() -> None:
    def worker() -> str:
        # what run_document does inside the pool thread
        assert current_run() is None
        with ensure_run_scope():
            return current_run().run_id

    with run_scope("outside"), ThreadPoolExecutor(max_workers=2) as pool:
        got = list(pool.map(lambda _: worker(), range(4)))
    assert set(got) == {live_run_id()}


def test_ensure_keeps_existing_scope() -> None:
    with run_scope("keepme", "eval"), ensure_run_scope() as scope:
        assert scope.run_id == "keepme"


@pytest.mark.parametrize(
    ("run_id", "expected"),
    [("eval-123", "eval-123"), ("a b/c:d\n", "a_b_c_d_"), ("x" * 200, "x" * 64)],
)
def test_ensure_eval_identifiers_are_consistent(run_id: str, expected: str) -> None:
    with ensure_run_scope(source="eval", run_id=run_id) as scope:
        assert scope.run_id == expected
        assert scope.session_id == f"eval-{expected}"
        assert scope.environment == "eval"
        assert scope.source == "eval"
    assert current_run() is None


def test_live_run_id_daily_bucket_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAILROOM_RUN_ID", raising=False)
    assert live_run_id(datetime(2026, 10, 9, tzinfo=UTC)) == "live-20261009"
    monkeypatch.setenv("MAILROOM_RUN_ID", "pilot 7")
    assert live_run_id() == "pilot_7"


def test_environment_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAILROOM_ENVIRONMENT", raising=False)
    assert environment_name() == "live"
    monkeypatch.setenv("MAILROOM_ENVIRONMENT", "staging")
    assert environment_name() == "staging"


def test_every_node_has_station_and_span_kind() -> None:
    nodes = [*NODE_ORDER, "verify", "boss", "human_review", "grade"]
    for node in nodes:
        assert attrs.station_for(node).id in {s.id for s in attrs.STATIONS}
        assert node in attrs.SPAN_KIND_FOR_NODE
    assert attrs.station_for("ingest").id == "intake"
    assert attrs.station_for("verify").color_token == "--term-station-judge"
    assert attrs.station_for("human_review").color_token == "--term-station-review"


def _status_exc(cls, code: int):
    import httpx

    request = httpx.Request("POST", "http://x/v1")
    response = httpx.Response(code, request=request)
    return cls("m", response=response, body=None)


def test_failure_class_for_exceptions() -> None:
    request = __import__("httpx").Request("POST", "http://x/v1")
    assert attrs.failure_class_for(APITimeoutError(request=request)) == "llm_timeout"
    assert attrs.failure_class_for(TimeoutError()) == "llm_timeout"
    assert attrs.failure_class_for(_status_exc(RateLimitError, 429)) == "llm_rate_limit"
    assert attrs.failure_class_for(APIConnectionError(request=request)) == "llm_transient"
    assert attrs.failure_class_for(FileNotFoundError("/secret/name.pdf")) == "io_error"
    assert attrs.failure_class_for(ValueError("bad json")) == "schema_error"
    assert attrs.failure_class_for(KeyError("k")) == "unexpected"
    auth = type("AuthenticationError", (Exception,), {"status_code": 401})()
    assert attrs.failure_class_for(auth) == "llm_auth"


def test_failure_class_for_reasons_never_echoes_free_text() -> None:
    assert attrs.failure_class_for("deadline_exceeded") == "run_budget"
    assert attrs.failure_class_for("token_budget_exceeded") == "run_budget"
    assert attrs.failure_class_for("ingest_failed:/tmp/Alice Smith.pdf") == "unexpected"
    assert attrs.failure_class_for(None) == "unexpected"
    assert attrs.failure_class_for("anything else") in attrs.FAILURE_CLASSES


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("deadline_exceeded", "deadline_exceeded"),
        ("token_budget_exceeded", "token_budget_exceeded"),
        ("ingest_failed:/tmp/Alice Smith.pdf", "ingest_failed"),
        ("no text extracted", "no_text"),
        ("schema mismatch for field x", "schema_invalid"),
        ("Alice Smith's file exploded", "unexpected"),
        (None, "unexpected"),
        ("", "unexpected"),
    ],
)
def test_failure_reason_is_bounded(raw: str | None, expected: str) -> None:
    got = attrs.failure_reason_for(raw)
    assert got == expected
    assert got in attrs.FAILURE_REASONS


def test_station_table_is_well_formed() -> None:
    ids = [s.id for s in attrs.STATIONS]
    assert len(ids) == len(set(ids))
    assert {s.kind for s in attrs.STATIONS} <= {"main", "detour", "bay"}
    assert all(s.color_token.startswith("--term-") for s in attrs.STATIONS)


def test_run_document_opens_scope_inside_the_worker(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from mailroom_reloaded.pipeline import flow as flow_mod

    seen: list[tuple[str, str, str]] = []

    class FakeFlow:
        def _configure(self, *a, **k) -> None:
            pass

        def _drive(self):
            s = current_run()
            seen.append((s.run_id, s.environment, s.source))
            return "state"

    monkeypatch.setattr(flow_mod, "MailroomFlow", FakeFlow)
    monkeypatch.delenv("MAILROOM_RUN_ID", raising=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(lambda _: flow_mod.run_document(tmp_path / "a.txt", worker_id="w"), range(3)))
    assert out == ["state"] * 3
    assert {r for r, _, _ in seen} == {live_run_id()}
    assert {s for _, _, s in seen} == {"watch"}
    flow_mod.run_document(tmp_path / "a.txt", worker_id="w", eval_ctx=SimpleNamespace(run_id="abc123"))
    assert seen[-1] == ("abc123", "eval", "eval")
    assert current_run() is None


class _NullLedger:
    def count(self, *a, **k):
        return 0

    def open_runs(self, *a, **k):
        return []

    def append(self, *a, **k):
        return True


def test_run_eval_scopes_every_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from mailroom_reloaded.eval import runner

    captured: list[tuple[str, str, str | None]] = []

    async def fake_run_all(cfg, run_id, selected, gts, graded, engine) -> None:
        async def one() -> None:
            s = current_run()
            captured.append((s.run_id, s.environment, s.session_id))

        await asyncio.gather(one(), asyncio.to_thread(lambda: None), one())

    monkeypatch.setattr(runner, "_run_all", fake_run_all)
    monkeypatch.setattr(runner, "load_split", lambda *a, **k: ([], {}))
    monkeypatch.setattr(runner, "sample", lambda *a, **k: [])
    monkeypatch.setattr(runner, "select_graded", lambda *a, **k: set())
    monkeypatch.setattr(runner, "_engine", lambda: object())
    monkeypatch.setattr(runner, "_ensure_table", lambda engine: None)
    monkeypatch.setattr(runner, "_record_dataset", lambda engine, run_id, cfg: None)
    cfg = runner.EvalConfig()
    monkeypatch.setattr(runner.run_ledger, "ledger_for", lambda overrides: _NullLedger())
    run_id = runner.run_eval(cfg)
    assert captured == [(run_id, "eval", f"eval-{run_id}")] * 2
    assert current_run() is None
