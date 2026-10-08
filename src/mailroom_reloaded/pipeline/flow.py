"""CrewAI ``MailroomFlow`` (spec section 4).

Design decision (see the implementation report): the manifest/audit/deadline and
retry semantics are driven deterministically by :func:`run_document` /
``MailroomFlow._drive``. The ``@start`` / ``@listen`` / ``@router`` methods exist
so the class is a genuine ``Flow[MailroomState]`` (routable, plottable,
``kickoff_async``-able) and are thin wrappers over the guarded node
implementations; the deterministic driver calls those same node implementations
in gate order. CrewAI's event engine is not used for control flow because the
plan's literal ``@listen("extract") extract`` wiring is self-referential and
rejected by CrewAI 1.15.25, and because crash-resume/deadline enforcement need a
single deterministic traversal.

The ``eval_ctx`` seam is typed ``Any | None`` on purpose: Task 20 owns
``mailroom_reloaded.eval.dataset.EvalContext`` and this module must not import it
at import time.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from pathlib import Path
from typing import Any, ClassVar

from crewai.flow.flow import Flow, listen, router, start
from opentelemetry import trace
from pydantic import ValidationError

from mailroom_reloaded.agents.arbiter import arbitrate
from mailroom_reloaded.agents.boss import escalate
from mailroom_reloaded.agents.gate import GateFeatures, load_gate
from mailroom_reloaded.agents.judge import judge_grade, judge_verify
from mailroom_reloaded.agents.sorter import sort as _sort
from mailroom_reloaded.agents.specialists import extract as _extract
from mailroom_reloaded.ingest.bert import (
    Handoff,
    SortMode,
    classify_primary,
    decide_handoff,
)
from mailroom_reloaded.ingest.clerk import ingest as _ingest
from mailroom_reloaded.pipeline.archivist import archive_document
from mailroom_reloaded.pipeline.guards import (
    NODE_DEADLINES,
    NODE_TOKEN_BUDGETS,
    NodeFailed,
    guarded,
)
from mailroom_reloaded.pipeline.report import compile_report
from mailroom_reloaded.pipeline.state import NODE_ORDER, MailroomState
from mailroom_reloaded.schemas.audit import CatalogRecord
from mailroom_reloaded.schemas.manifest import Manifest, next_node
from mailroom_reloaded.settings import get_settings, load_taxonomy
from mailroom_reloaded.storage import audit_log, catalog
from mailroom_reloaded.storage.bins import (
    Bins,
    doc_id_for,
    load_manifest,
    save_manifest,
)
from mailroom_reloaded.tools import ToolContext

__all__ = ["MailroomFlow", "NODE_ORDER", "run_document"]

# successor for the plain work nodes on the main path
_NEXT: dict[str, str] = {
    "ingest": "bert_primary",
    "bert_primary": "sort",
    "sort": "gate_classify",
    "extract": "gate_extract",
}

_DEFAULT_PROMPT_SET = "frozen_v1"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class MailroomFlow(Flow[MailroomState]):
    """The Digital Mailroom pipeline as a CrewAI flow over ``MailroomState``."""

    _skip_auto_memory = True
    _node_deadlines: ClassVar[dict[str, float]] = dict(NODE_DEADLINES)
    _node_token_budgets: ClassVar[dict[str, int]] = dict(NODE_TOKEN_BUDGETS)

    # --------------------------------------------------------------- flow graph
    @start()
    def ingest(self) -> None:
        self._node_ingest()

    @listen(ingest)
    def bert_primary(self) -> None:
        self._node_bert_primary()

    @listen(bert_primary)
    def sort(self) -> None:
        self._node_sort()

    @router(sort)
    def gate_classify(self) -> str:
        return self._classify_route()

    @listen("do_extract")
    def extract(self) -> None:
        self._node_extract()

    @router(extract)
    def gate_extract(self) -> str:
        return self._extract_route()

    @listen("do_verify")
    def verify(self) -> None:
        self._node_verify()

    @listen("do_boss")
    def boss(self) -> None:
        self._node_boss()

    @listen("report")
    def report_catalog_archive(self) -> None:
        self._node_report_catalog_archive()

    @listen("human_review")
    def park(self) -> MailroomState:
        return self._park("flow_human_review")

    # --------------------------------------------------------------- guarded nodes
    @guarded("ingest", NODE_DEADLINES["ingest"], 0)
    def _node_ingest(self) -> None:
        state = self.state
        result = _ingest(Path(state.path))
        state.ingest = result
        state.text = result.text
        if result.error:
            self._fail_node("ingest", f"ingest_failed:{result.error}")

    @guarded("bert_primary", NODE_DEADLINES["bert_primary"], 0)
    def _node_bert_primary(self) -> None:
        state = self.state
        verdict = classify_primary(state.text)
        state.bert = verdict
        state.handoff = decide_handoff(verdict, load_taxonomy().bert)

    @guarded("sort", NODE_DEADLINES["sort"], 0)
    def _node_sort(self) -> None:
        state = self.state
        handoff = state.handoff or Handoff(SortMode.FULL, None, "", "no_handoff")
        state.handoff = handoff
        result = _sort(state.text, handoff, attempt=state.classify_attempts)
        state.sort = result
        state.usage_total = state.usage_total + result.usage
        self._llm_calls += 1

    @guarded("extract", NODE_DEADLINES["extract"], 0)
    def _node_extract(self) -> None:
        state = self.state
        doc_type = self._effective_doc_type()
        doc_subclass = self._effective_subclass()
        kwargs: dict[str, Any] = {"attempt": state.extract_attempts}
        prompt_set = self._overrides.get("prompt_set")
        if prompt_set:
            kwargs["prompt_set"] = prompt_set
        merger_mode = self._overrides.get("merger_mode")
        if merger_mode:
            cond = load_taxonomy().specialist_conditions(doc_type).model_copy(
                update={"merger_mode": merger_mode}
            )
            kwargs["cond"] = cond
        result = _extract(state.text, doc_type, doc_subclass, **kwargs)
        state.extract = result
        state.usage_total = state.usage_total + result.usage
        self._llm_calls += 1

    @guarded("verify", NODE_DEADLINES["verify"], 0)
    def _node_verify(self) -> None:
        state = self.state
        doc_type = self._effective_doc_type()
        data = state.extract.data if state.extract is not None else None
        ctx = ToolContext(doc_text=state.text, doc_id=state.doc_id, eval_mode=False)
        state.verdict = judge_verify(state.text, doc_type, data, ctx)
        state.arbiter = arbitrate(state.text, doc_type, data, state.verdict, ctx)

    @guarded("boss", NODE_DEADLINES["boss"], 0)
    def _node_boss(self) -> None:
        state = self.state
        ctx = ToolContext(doc_text=state.text, doc_id=state.doc_id, eval_mode=False)
        summary = {
            "doc_type": state.sort.doc_type if state.sort is not None else None,
            "doc_subclass": state.sort.doc_subclass if state.sort is not None else None,
            "classify_attempts": state.classify_attempts,
            "extract_attempts": state.extract_attempts,
            "extract_confidence": (
                state.extract.confidence if state.extract is not None else None
            ),
        }
        state.boss = escalate(state.text, summary, ctx)

    @guarded("report_catalog_archive", NODE_DEADLINES["report_catalog_archive"], 0)
    def _node_report_catalog_archive(self) -> None:
        state = self.state
        report = compile_report(state)
        report["llm_calls"] = self._llm_calls
        state.report = report
        result = archive_document(self._bins, self._manifest, state)
        try:
            catalog.upsert(
                CatalogRecord(
                    doc_id=state.doc_id,
                    filename=self._manifest.filename,
                    doc_type=self._effective_doc_type(),
                    doc_subclass=self._effective_subclass(),
                    status="archived",
                    archive_path=str(result.path),
                    file_sha256=result.file_sha256,
                )
            )
        except Exception:  # noqa: BLE001 - catalog is best-effort durability
            pass
        state.status = "archived"
        self._manifest.status = "archived"

    @guarded("grade", NODE_DEADLINES["grade"], 0)
    def _node_grade(self) -> None:
        if self._eval_ctx is None:
            return
        state = self.state
        data = state.extract.data if state.extract is not None else None
        ctx = ToolContext(
            doc_text=state.text,
            doc_id=state.doc_id,
            eval_mode=True,
            ground_truth=self._ground_truth_fn(),
        )
        try:
            state.grade = judge_grade(
                state.text, self._effective_doc_type(), data, ctx
            )
        except Exception:  # noqa: BLE001 - grading must not fail the document
            state.grade = None

    # --------------------------------------------------------------- guard plumbing
    def _guard_node(
        self,
        node_name: str,
        deadline_s: float,
        token_budget: int,
        fn: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if node_name in self._resume_done:
            return None
        deadlines = self._overrides.get("deadlines", {})
        budgets = self._overrides.get("token_budgets", {})
        deadline = deadlines.get(node_name, deadline_s)
        budget = budgets.get(node_name, token_budget)

        start = time.monotonic()
        before_tokens = self.state.usage_total.total_tokens
        with self._tracer.start_as_current_span(f"mailroom.node.{node_name}") as span:
            span.set_attribute("mailroom.doc_id", self.state.doc_id)
            try:
                result = fn(self, *args, **kwargs)
            except NodeFailed:
                raise
            except Exception as exc:  # persist the completed prefix, then surface
                self._manifest.state = self.state.model_dump(mode="json")
                save_manifest(self._bins, self._manifest)
                span.record_exception(exc)
                raise
            elapsed = time.monotonic() - start
            used_tokens = self.state.usage_total.total_tokens - before_tokens
            if deadline and deadline > 0 and elapsed > deadline:
                self._fail_node(node_name, "deadline_exceeded", elapsed)
            if budget and budget > 0 and used_tokens > budget:
                self._fail_node(node_name, "token_budget_exceeded", elapsed)
            self._record_node(node_name, elapsed)
            return result

    def _record_node(self, node_name: str, elapsed: float) -> None:
        manifest = self._manifest
        if node_name not in manifest.completed_nodes:
            manifest.completed_nodes.append(node_name)
        manifest.state = self.state.model_dump(mode="json")
        save_manifest(self._bins, manifest)
        audit_log.append(
            self.state.doc_id, node_name, "completed", {"elapsed_s": round(elapsed, 4)}
        )

    def _fail_node(self, node_name: str, reason: str, elapsed: float = 0.0) -> None:
        state = self.state
        state.status = "failed"
        manifest = self._manifest
        manifest.status = "failed"
        try:
            dest = self._bins.move(Path(state.path), "failed")
            state.path = str(dest)
        except Exception:  # noqa: BLE001 - best effort relocation
            pass
        manifest.state = state.model_dump(mode="json")
        save_manifest(self._bins, manifest)
        audit_log.append(
            state.doc_id,
            node_name,
            "node_failed",
            {"reason": reason, "elapsed_s": round(elapsed, 4)},
        )
        raise NodeFailed(node_name, reason)

    def _park(self, reason: str) -> MailroomState:
        state = self.state
        state.route_trail.append("human_review")
        state.status = "parked"
        manifest = self._manifest
        manifest.status = "parked"
        try:
            dest = self._bins.move(Path(state.path), "review")
            state.path = str(dest)
        except Exception:  # noqa: BLE001 - best effort relocation
            pass
        manifest.state = state.model_dump(mode="json")
        save_manifest(self._bins, manifest)
        audit_log.append(state.doc_id, "human_review", "parked", {"reason": reason})
        return state

    # --------------------------------------------------------------- routes
    def _classify_route(self) -> str:
        state = self.state
        s = state.sort
        b = state.bert
        features = GateFeatures(
            stage="classify",
            doc_type=s.doc_type if s is not None else None,
            confidence=s.confidence if s is not None else 0.0,
            attempts=state.classify_attempts,
            bert_confidence=_opt(b.calibrated_confidence) if b is not None else 0.0,
            bert_margin=_opt(b.margin) if b is not None else 0.0,
            bert_window_agreement=_opt(b.window_agreement) if b is not None else 0.0,
            doc_type_disagree=s.doc_type_disagree if s is not None else False,
            resorted=state.resorted,
        )
        action = self._gate.decide(features).action
        return {
            "proceed": "do_extract",
            "retry": "retry_sort",
            "re_sort": "re_sort",
            "human_review": "human_review",
        }.get(action, "human_review")

    def _extract_route(self) -> str:
        state = self.state
        e = state.extract
        length_capped = bool(
            e is not None and e.error_kind == "LengthFinishReasonError"
        )
        features = GateFeatures(
            stage="extract",
            doc_type=self._effective_doc_type(),
            confidence=(e.confidence if e is not None and e.confidence is not None else 0.0),
            attempts=state.extract_attempts,
            schema_valid=e.schema_valid if e is not None else False,
            field_coverage=1.0 if (e is not None and e.schema_valid) else 0.0,
            length_capped=length_capped,
            doc_type_disagree=(
                state.sort.doc_type_disagree if state.sort is not None else False
            ),
            resorted=state.resorted,
        )
        action = self._gate.decide(features).action
        return {
            "proceed": "report",
            "retry": "retry_extract",
            "verify": "do_verify",
            "boss": "do_boss",
            "human_review": "human_review",
        }.get(action, "human_review")

    def _arbiter_route(self) -> str:
        a = self.state.arbiter
        if a is None:
            return "report"
        return {
            "accept": "report",
            "accept_with_caveats": "report",
            "re_extract": "retry_extract",
            "escalate": "do_boss",
        }.get(a.action, "report")

    # --------------------------------------------------------------- helpers
    def _effective_doc_type(self) -> str:
        boss = self.state.boss
        candidates = [
            self._overrides.get("doc_type"),
            boss.doc_type
            if (boss is not None and boss.action == "reassign_class")
            else None,
            self.state.sort.doc_type if self.state.sort is not None else None,
            self.state.handoff.locked_doc_type
            if self.state.handoff is not None
            else None,
            self.state.bert.doc_type if self.state.bert is not None else None,
            self.state.extract.doc_type if self.state.extract is not None else None,
        ]
        for value in candidates:
            if value:
                return str(value)
        return "unknown"

    def _effective_subclass(self) -> str | None:
        boss = self.state.boss
        candidates = [
            self._overrides.get("doc_subclass"),
            boss.doc_subclass
            if (boss is not None and boss.action == "reassign_class")
            else None,
            self.state.sort.doc_subclass if self.state.sort is not None else None,
        ]
        for value in candidates:
            if value:
                return str(value)
        return None

    def _reset_handoff_full(self) -> None:
        prior = self.state.handoff.prior if self.state.handoff is not None else ""
        self.state.handoff = Handoff(SortMode.FULL, None, prior, "re_sort")

    def _ground_truth_fn(self):
        gt_map = getattr(self._eval_ctx, "ground_truth", None)

        def fetch(doc_id: str) -> dict:
            row = gt_map.get(doc_id) if isinstance(gt_map, dict) else None
            if row is None:
                return {}
            if hasattr(row, "model_dump"):
                return row.model_dump()
            if dataclasses.is_dataclass(row) and not isinstance(row, type):
                return dataclasses.asdict(row)
            return row if isinstance(row, dict) else {}

        return fetch

    # --------------------------------------------------------------- driver
    def _configure(
        self,
        path: Path,
        worker_id: str,
        resume_from: str | None,
        overrides: dict[str, Any] | None,
        eval_ctx: Any | None,
    ) -> None:
        self._bins = (overrides or {}).get("bins") or Bins(get_settings().base_dir)
        self._overrides = dict(overrides or {})
        self._eval_ctx = eval_ctx
        self._resume_from = resume_from
        self._llm_calls = 0
        self._gate = load_gate()
        self._tracer = trace.get_tracer("mailroom.pipeline")

        src = Path(path)
        claimed = self._bins.claim(src, worker_id)
        work = claimed if claimed is not None else src
        if not work.exists():
            raise FileNotFoundError(f"document not found: {path}")

        doc_id = doc_id_for(work)
        content_sha256 = _sha256_file(work)
        manifest = load_manifest(self._bins, doc_id) or Manifest(
            doc_id=doc_id, filename=work.name, content_sha256=content_sha256
        )
        manifest.doc_id = doc_id
        manifest.filename = work.name
        manifest.content_sha256 = content_sha256
        self._manifest = manifest

        base = self.state
        if manifest.state:
            try:
                restored = MailroomState.model_validate(manifest.state)
            except ValidationError:
                restored = None
            if restored is not None:
                for name in MailroomState.model_fields:
                    setattr(base, name, getattr(restored, name))
        base.doc_id = doc_id
        base.path = str(work)
        base.eval_mode = eval_ctx is not None

    def _resume_start(self) -> str | None:
        if self._resume_from:
            self._resume_done = set()
            return self._resume_from
        return next_node(self._manifest, NODE_ORDER)

    def _drive(self) -> MailroomState:
        self._resume_done = set(self._manifest.completed_nodes)
        state = self.state
        node = self._resume_start()
        while node is not None and state.status != "failed":
            if node == "gate_classify":
                state.route_trail.append("gate_classify")
                route = self._classify_route()
                if route == "do_extract":
                    node = "extract"
                elif route == "retry_sort":
                    state.classify_attempts += 1
                    node = "sort"
                elif route == "re_sort":
                    state.resorted = True
                    self._reset_handoff_full()
                    node = "sort"
                else:
                    return self._park("classify_human_review")
            elif node == "gate_extract":
                state.route_trail.append("gate_extract")
                route = self._extract_route()
                if route == "report":
                    node = "report_catalog_archive"
                elif route == "retry_extract":
                    state.extract_attempts += 1
                    node = "extract"
                elif route == "do_verify":
                    state.route_trail.append("verify")
                    try:
                        self._node_verify()
                    except NodeFailed:
                        return state
                    arbiter_route = self._arbiter_route()
                    if arbiter_route == "retry_extract":
                        state.extract_attempts += 1
                        node = "extract"
                    elif arbiter_route == "do_boss":
                        node = "boss"
                    else:
                        node = "report_catalog_archive"
                elif route == "do_boss":
                    node = "boss"
                else:
                    return self._park("extract_human_review")
            elif node == "boss":
                state.route_trail.append("boss")
                try:
                    self._node_boss()
                except NodeFailed:
                    return state
                action = state.boss.action if state.boss is not None else "accept"
                if action == "reassign_class":
                    node = "extract"
                elif action == "human_review":
                    return self._park("boss_human_review")
                else:
                    node = "report_catalog_archive"
            elif node == "report_catalog_archive":
                state.route_trail.append("report_catalog_archive")
                try:
                    self._node_report_catalog_archive()
                except NodeFailed:
                    return state
                node = None
            else:  # ingest / bert_primary / sort / extract
                state.route_trail.append(node)
                try:
                    getattr(self, node)()
                except NodeFailed:
                    return state
                node = _NEXT[node]

        if (
            self._eval_ctx is not None
            and state.status == "archived"
            and "grade" not in self._resume_done
            and "grade" not in self._manifest.completed_nodes
        ):
            try:
                self._node_grade()
            except NodeFailed:
                pass
        return state

    # --------------------------------------------------------------- kickoff
    def kickoff(self, inputs: dict[str, Any] | None = None, input_files: Any = None, **kwargs: Any):
        """Run one document from ``inputs`` (path/worker_id/resume_from/overrides/eval_ctx)."""
        inputs = inputs or {}
        self._configure(
            Path(inputs["path"]),
            inputs.get("worker_id", "flow"),
            inputs.get("resume_from"),
            inputs.get("overrides"),
            inputs.get("eval_ctx"),
        )
        return self._drive()

    async def kickoff_async(self, inputs: dict[str, Any] | None = None, input_files: Any = None, **kwargs: Any):
        return self.kickoff(inputs, input_files, **kwargs)


def _opt(value: float | None) -> float:
    return float(value) if value is not None else 0.0


def run_document(
    path: Path,
    *,
    worker_id: str,
    resume_from: str | None = None,
    overrides: dict[str, Any] | None = None,
    eval_ctx: Any | None = None,
) -> MailroomState:
    """Claim ``path`` and run the pipeline to a terminal bin.

    Returns the final :class:`MailroomState`. Raises the underlying exception if
    a node crashes (the manifest keeps the completed prefix, so a later call
    resumes). A deadline/token-budget failure or an ingest failure returns a
    state with ``status == "failed"``.
    """
    flow = MailroomFlow()
    flow._configure(Path(path), worker_id, resume_from, overrides, eval_ctx)
    return flow._drive()
