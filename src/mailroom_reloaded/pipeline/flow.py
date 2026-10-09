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

import structlog
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
from mailroom_reloaded.llm.usage import Usage, add_role_usage
from mailroom_reloaded.obs.metrics import M
from mailroom_reloaded.obs.run_context import ensure_run_scope
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

__all__ = [
    "NODE_ORDER",
    "MailroomFlow",
    "reconcile_archived",
    "reconcile_catalog",
    "run_document",
]

logger = structlog.get_logger(__name__)

# successor for the plain work nodes on the main path
_NEXT: dict[str, str] = {
    "ingest": "bert_primary",
    "bert_primary": "sort",
    "sort": "gate_classify",
    "extract": "gate_extract",
}

_DEFAULT_PROMPT_SET = "frozen_v1"

#: Nodes that call an LLM (their spend can be partly lost when they raise).
_LLM_NODES = frozenset({"ingest", "sort", "extract", "verify", "boss", "grade"})


def _sha256_file(path: Path) -> str:
    """Streaming sha256 hex digest of the file at ``path``."""
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
        """Entry node: ingest the claimed document into state."""
        self._node_ingest()

    @listen(ingest)
    def bert_primary(self) -> None:
        """Run the local ModernBERT classifier and pick a sort handoff."""
        self._node_bert_primary()

    @listen(bert_primary)
    def sort(self) -> None:
        """Run the standalone sorter (LLM call 1)."""
        self._node_sort()

    @router(sort)
    def gate_classify(self) -> str:
        """Route the classify stage via the deterministic gate."""
        return self._classify_route()

    @listen("do_extract")
    def extract(self) -> None:
        """Run the class specialist extraction (LLM call 2)."""
        self._node_extract()

    @router(extract)
    def gate_extract(self) -> str:
        """Route the extract stage via the deterministic gate."""
        return self._extract_route()

    @listen("do_verify")
    def verify(self) -> None:
        """Judge the extraction, then let the arbiter settle it."""
        self._node_verify()

    @listen("do_boss")
    def boss(self) -> None:
        """Escalate to the boss for a class reassignment decision."""
        self._node_boss()

    @listen("report")
    def report_catalog_archive(self) -> None:
        """Compile the report, catalog the record and archive the file."""
        self._node_report_catalog_archive()

    @listen("human_review")
    def park(self) -> MailroomState:
        """Park the document in ``review/`` and return the parked state."""
        return self._park("flow_human_review")

    # --------------------------------------------------------------- guarded nodes
    @guarded("ingest", NODE_DEADLINES["ingest"], 0)
    def _node_ingest(self) -> None:
        """Ingest the file; fail the document when the clerk reports an error."""
        state = self.state
        result = _ingest(Path(state.path))
        state.ingest = result
        state.text = result.text
        self._add_usage("pdf_transcriber", result.usage)  # vision spend, kept even on failure
        if result.error:
            self._fail_node("ingest", f"ingest_failed:{result.error}")

    @guarded("bert_primary", NODE_DEADLINES["bert_primary"], 0)
    def _node_bert_primary(self) -> None:
        """Classify with ModernBERT and derive the sorter handoff."""
        state = self.state
        verdict = classify_primary(state.text, filename=Path(state.path).name)
        state.bert = verdict
        state.handoff = decide_handoff(verdict, load_taxonomy().bert)
        route = (
            "unavailable"
            if not verdict.available
            else "fast_path"
            if state.handoff.mode is SortMode.SUBCLASS_ONLY
            else "defer"
        )
        M.bert_route.add(1, {"route": route})

    @guarded("sort", NODE_DEADLINES["sort"], 0)
    def _node_sort(self) -> None:
        """Run the sorter and accumulate its usage."""
        state = self.state
        handoff = state.handoff or Handoff(SortMode.FULL, None, "", "no_handoff")
        state.handoff = handoff
        result = _sort(state.text, handoff, attempt=state.classify_attempts)
        state.sort = result
        self._add_usage("sorter", result.usage)
        self._llm_calls += 1

    @guarded("extract", NODE_DEADLINES["extract"], 0)
    def _node_extract(self) -> None:
        """Run the class specialist with the current class/subclass and overrides."""
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
        self._add_usage(load_taxonomy().classes[doc_type].specialist, result.usage)
        self._llm_calls += 1
        M.schema_valid.add(1 if result.schema_valid else 0, {"doc_type": doc_type})

    @guarded("verify", NODE_DEADLINES["verify"], 0)
    def _node_verify(self) -> None:
        """Run judge_verify then arbitrate (live mode, no ground truth)."""
        state = self.state
        doc_type = self._effective_doc_type()
        data = state.extract.data if state.extract is not None else None
        ctx = ToolContext(
            doc_text=state.text,
            doc_id=state.doc_id,
            eval_mode=False,
            usage_sink=self._sink(),
        )
        state.verdict = judge_verify(state.text, doc_type, data, ctx)
        state.arbiter = arbitrate(state.text, doc_type, data, state.verdict, ctx)

    @guarded("boss", NODE_DEADLINES["boss"], 0)
    def _node_boss(self) -> None:
        """Escalate to the boss with a summary of the failed classification."""
        state = self.state
        ctx = ToolContext(
            doc_text=state.text,
            doc_id=state.doc_id,
            eval_mode=False,
            usage_sink=self._sink(),
        )
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
        """Compile the report, archive the file and upsert the catalog record."""
        state = self.state
        report = compile_report(state)
        report["llm_calls"] = self._llm_calls
        state.report = report
        result = archive_document(self._bins, self._manifest, state)
        record = CatalogRecord(
            doc_id=state.doc_id,
            filename=self._manifest.filename,
            doc_type=self._effective_doc_type(),
            doc_subclass=self._effective_subclass(),
            status="archived",
            archive_path=str(result.path),
            file_sha256=result.file_sha256,
        )
        try:
            catalog.upsert(record)
        except Exception as exc:  # noqa: BLE001 - recorded for startup reconcile
            logger.warning("catalog_upsert_failed", doc_id=state.doc_id, error=str(exc))
            self._manifest.catalog_pending = record.model_dump(mode="json")
        state.status = "archived"
        self._manifest.status = "archived"

    @guarded("grade", NODE_DEADLINES["grade"], 0)
    def _node_grade(self) -> None:
        """Grade the extraction against ground truth when an eval context is present.

        Keep successful grader usage in ``usage_by_role``, outside ``usage_total``.
        Grading errors clear ``state.grade`` and mark usage partial; the surrounding
        node guard can still raise ``NodeFailed`` if its deadline is exceeded.
        """
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
            self._mark_usage_partial("grade")
        else:
            # grading spend is kept apart: it never inflates the pipeline's usage_total
            add_role_usage(state.usage_by_role, "grader", state.grade.usage)

    # --------------------------------------------------------------- usage plumbing
    def _add_usage(self, role: str, usage: Usage) -> None:
        """Count ``usage`` into the pipeline total and under ``role``."""
        state = self.state
        state.usage_total = state.usage_total + usage
        add_role_usage(state.usage_by_role, role, usage)

    def _sink(self) -> list[tuple[str, Usage]]:
        """The per-run collector the CrewAI agents append to (created on first use)."""
        try:
            return self._usage_sink
        except AttributeError:
            self._usage_sink = []
            return self._usage_sink

    def _drain_usage_sink(self) -> None:
        """Move the CrewAI agents' collected ``(role, usage)`` entries into the state."""
        sink = self._sink()
        entries, sink[:] = list(sink), []
        for role, usage in entries:
            self._add_usage(role, usage)

    def _mark_usage_partial(self, node_name: str) -> None:
        """Flag potentially unrecorded spend once for a known LLM-calling node.

        Ignore other node names; this does not establish where a failure occurred.
        """
        if node_name in _LLM_NODES and node_name not in self.state.usage_partial_nodes:
            self.state.usage_partial_nodes.append(node_name)

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
        """Run a node under resume-skip, manifest/audit, span and budget guards.

        Return the node's result, or ``None`` when consuming a resume skip.
        Per-node overrides replace ``deadline_s`` (seconds) and ``token_budget``
        (the increase in pipeline total tokens). Nonpositive limits are disabled;
        exceeding a positive limit after execution fails the document with
        ``NodeFailed``. Exact limits are allowed; execution is not interrupted.

        Drain collected usage after execution, including on node exceptions.
        Re-raise ``NodeFailed``; for other node errors, mark LLM usage partial and
        save state before re-raising. Persistence and audit errors also propagate.
        """
        if node_name in self._resume_done:
            self._resume_done.remove(node_name)
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
                self._drain_usage_sink()
                raise
            except Exception as exc:  # persist the completed prefix, then surface
                self._drain_usage_sink()
                self._mark_usage_partial(node_name)
                self._manifest.state = self.state.model_dump(mode="json")
                save_manifest(self._bins, self._manifest)
                span.record_exception(exc)
                raise
            self._drain_usage_sink()
            elapsed = time.monotonic() - start
            used_tokens = self.state.usage_total.total_tokens - before_tokens
            if deadline and deadline > 0 and elapsed > deadline:
                self._fail_node(node_name, "deadline_exceeded", elapsed)
            if budget and budget > 0 and used_tokens > budget:
                self._fail_node(node_name, "token_budget_exceeded", elapsed)
            self._record_node(node_name, elapsed)
            return result

    def _record_node(self, node_name: str, elapsed: float) -> None:
        """Mark ``node_name`` complete, snapshot state and append the audit entry."""
        M.node_duration.record(elapsed, {"node": node_name})
        if node_name == "report_catalog_archive":
            M.documents.add(
                1,
                {
                    "stage": "pipeline",
                    "status": "archived",
                    "doc_type": self._effective_doc_type(),
                },
            )
        manifest = self._manifest
        if node_name not in manifest.completed_nodes:
            manifest.completed_nodes.append(node_name)
        manifest.state = self.state.model_dump(mode="json")
        save_manifest(self._bins, manifest)
        audit_log.append(
            self.state.doc_id, node_name, "completed", {"elapsed_s": round(elapsed, 4)}
        )

    def _fail_node(self, node_name: str, reason: str, elapsed: float = 0.0) -> None:
        """Fail the document: move to ``failed/``, update the manifest, audit, raise."""
        state = self.state
        state.status = "failed"
        M.documents.add(1, {"stage": "pipeline", "status": "failed"})
        manifest = self._manifest
        manifest.status = "failed"
        try:
            dest = self._bins.move(Path(state.path), "failed")
            state.path = str(dest)
        except Exception as exc:  # noqa: BLE001 - best effort relocation
            logger.warning(
                "failed_relocation_skipped", doc_id=state.doc_id, error=str(exc)
            )
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
        """Park the document in ``review/`` with manifest/audit updates."""
        state = self.state
        state.route_trail.append("human_review")
        state.status = "parked"
        M.documents.add(1, {"stage": "pipeline", "status": "parked"})
        manifest = self._manifest
        manifest.status = "parked"
        try:
            dest = self._bins.move(Path(state.path), "review")
            state.path = str(dest)
        except Exception as exc:  # noqa: BLE001 - best effort relocation
            logger.warning(
                "review_relocation_skipped", doc_id=state.doc_id, error=str(exc)
            )
        manifest.state = state.model_dump(mode="json")
        save_manifest(self._bins, manifest)
        audit_log.append(state.doc_id, "human_review", "parked", {"reason": reason})
        try:
            catalog.upsert(
                CatalogRecord(
                    doc_id=state.doc_id,
                    filename=manifest.filename,
                    doc_type=self._effective_doc_type(),
                    doc_subclass=self._effective_subclass(),
                    status="parked",
                )
            )
        except Exception as exc:  # noqa: BLE001 - catalog is best-effort durability
            logger.warning("catalog_upsert_failed", doc_id=state.doc_id, error=str(exc))
        return state

    # --------------------------------------------------------------- routes
    def _classify_route(self) -> str:
        """Map the classify-stage gate decision to a driver target."""
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
        decision = self._gate.decide(features)
        action = decision.action
        self._audit_gate("classify", decision, features.confidence)
        M.gate_decisions.add(1, {"stage": "classify", "decision": action})
        return {
            "proceed": "do_extract",
            "retry": "retry_sort",
            "re_sort": "re_sort",
            "human_review": "human_review",
        }.get(action, "human_review")

    def _extract_route(self) -> str:
        """Map the extract-stage gate decision to a driver target."""
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
        decision = self._gate.decide(features)
        action = decision.action
        self._audit_gate("extract", decision, features.confidence)
        M.gate_decisions.add(1, {"stage": "extract", "decision": action})
        return {
            "proceed": "report",
            "retry": "retry_extract",
            "verify": "do_verify",
            "boss": "do_boss",
            "human_review": "human_review",
        }.get(action, "human_review")

    def _audit_gate(self, stage: str, decision, conf: float | None) -> None:
        """Record a gate decision (small, JSON-safe) in the audit chain."""
        payload = {
            "action": str(decision.action),
            "reason": str(getattr(decision, "reason", ""))[:200],
            "source": str(getattr(decision, "source", "")),
            "confidence": round(float(conf), 4) if isinstance(conf, (int, float)) else None,
        }
        audit_log.append(self.state.doc_id, f"gate_{stage}", "gate_decision", payload)

    def _arbiter_route(self) -> str:
        """Map the arbiter decision to a driver target."""
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
        """Current doc_type: explicit override, boss reassignment, sort/handoff/BERT."""
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
        """Current doc_subclass: explicit override, boss reassignment, else sort."""
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
        """Switch the handoff to ``FULL`` for the single re-sort, keeping the prior."""
        prior = self.state.handoff.prior if self.state.handoff is not None else ""
        self.state.handoff = Handoff(SortMode.FULL, None, prior, "re_sort")

    def _ground_truth_fn(self):
        """Build the ``get_ground_truth`` callable over the eval context's labels."""
        gt_map = getattr(self._eval_ctx, "ground_truth", None)

        def fetch(doc_id: str) -> dict:
            """Return the ground-truth row for ``doc_id`` as a plain dict."""
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
        """Claim the file, load its manifest and restore state for this run."""
        self._bins = (overrides or {}).get("bins") or Bins(get_settings().base_dir)
        self._overrides = dict(overrides or {})
        self._eval_ctx = eval_ctx
        self._resume_from = resume_from
        self._llm_calls = 0
        self._usage_sink: list[tuple[str, Usage]] = []
        self._gate = load_gate()
        self._tracer = trace.get_tracer("mailroom.pipeline")

        src = Path(path)
        claimed = self._bins.claim(src, worker_id)
        work = claimed if claimed is not None else src
        if not work.exists():
            raise FileNotFoundError(f"document not found: {path}")

        doc_id = doc_id_for(work)
        content_sha256 = _sha256_file(work)
        manifest = load_manifest(self._bins, doc_id)
        if manifest is None or (
            resume_from is None and manifest.status in {"archived", "failed"}
        ):
            manifest = Manifest(
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
        """First node to run: the explicit ``resume_from`` else the manifest's next."""
        if self._resume_from:
            self._resume_done = set()
            return self._resume_from
        return next_node(self._manifest, NODE_ORDER)

    def _drive(self) -> MailroomState:
        """Open the per-document span and run the deterministic node walk under it."""
        state = self.state
        with self._tracer.start_as_current_span("mailroom.document") as span:
            span.set_attribute("mailroom.doc_id", state.doc_id)
            return self._drive_nodes()

    def _drive_nodes(self) -> MailroomState:
        """Deterministically walk the guarded nodes and gates to a terminal bin."""
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
                    self._resume_done.discard("sort")
                    node = "sort"
                elif route == "re_sort":
                    state.resorted = True
                    self._reset_handoff_full()
                    self._resume_done.discard("sort")
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
                    self._resume_done.discard("extract")
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
                        self._resume_done.discard("extract")
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
                    self._resume_done.discard("extract")
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
        """Async wrapper around :meth:`kickoff` (the driver is synchronous)."""
        return self.kickoff(inputs, input_files, **kwargs)


def _opt(value: float | None) -> float:
    """``value`` as a float, or 0.0 when ``None`` (gate feature default)."""
    return float(value) if value is not None else 0.0


def reconcile_catalog(bins: Bins, manifest: Manifest) -> bool:
    """Retry a ``catalog_pending`` upsert; clear the marker on success."""
    if not manifest.catalog_pending:
        return False
    try:
        catalog.upsert(CatalogRecord.model_validate(manifest.catalog_pending))
    except Exception as exc:  # noqa: BLE001 - stays pending for the next startup
        logger.warning(
            "catalog_reconcile_failed", doc_id=manifest.doc_id, error=str(exc)
        )
        return False
    manifest.catalog_pending = None
    save_manifest(bins, manifest)
    logger.info("catalog_reconciled", doc_id=manifest.doc_id)
    return True


def reconcile_archived(bins: Bins, manifest: Manifest) -> bool:
    """Finish a document that was archived but crashed before its manifest snapshot.

    ``archive_document`` moves the file and appends the ``archived`` audit entry
    before ``_record_node`` checkpoints the manifest. When the audit log holds that
    entry and the archived file exists, mark manifest + state ``archived`` and
    upsert the catalog (a failed upsert is left pending). Returns whether the
    manifest was reconciled.
    """
    archived = [
        e for e in audit_log.entries(manifest.doc_id) if e.event == "archived"
    ]
    if not archived:
        return False
    payload = archived[-1].payload
    archive_path = payload.get("path")
    if not archive_path or not Path(archive_path).is_file():
        return False
    state = dict(manifest.state or {})
    state["status"] = "archived"
    state["path"] = str(archive_path)
    manifest.state = state
    manifest.status = "archived"
    if "report_catalog_archive" not in manifest.completed_nodes:
        manifest.completed_nodes.append("report_catalog_archive")
    sort = state.get("sort") or {}
    manifest.catalog_pending = CatalogRecord(
        doc_id=manifest.doc_id,
        filename=manifest.filename,
        doc_type=str(payload.get("doc_type") or sort.get("doc_type") or "unknown"),
        doc_subclass=sort.get("doc_subclass"),
        status="archived",
        archive_path=str(archive_path),
        file_sha256=str(payload.get("file_sha256", "")),
    ).model_dump(mode="json")
    save_manifest(bins, manifest)
    audit_log.append(
        manifest.doc_id,
        "report_catalog_archive",
        "completed",
        {"elapsed_s": 0.0, "reconciled": True},
    )
    reconcile_catalog(bins, manifest)
    return True


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
    # opened here, in the worker thread: a ContextVar set around a thread pool does not cross it
    eval_run_id = getattr(eval_ctx, "run_id", None) if eval_ctx is not None else None
    with ensure_run_scope("eval" if eval_ctx is not None else "watch", eval_run_id):
        flow._configure(Path(path), worker_id, resume_from, overrides, eval_ctx)
        return flow._drive()
