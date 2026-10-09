"""Behavioural conformance suite for every LLM role (spec section 11, Task 24).

The suite runs each role with its designated prompt over ``per_class`` fixture
documents per class from the TRAIN split of ``Lucius-Morningstar/mailroom-dataset``
and checks the spec section 11 invariants:

* **sorter** -- label in vocabulary; ``SUBCLASS_ONLY`` never changes ``doc_type``
  without setting ``doc_type_disagree``.
* **specialists** -- schema-valid; no keys outside the schema; nulls/empties
  where the frozen prompt says so.
* **judge** -- uses ``get_ground_truth`` in grade mode and never in live mode.
* **arbiter, boss** -- return a valid action and call at least one tool when the
  source is needed.

A card reports each role's tool-call success rate and invariant pass rate per
provider as JSON and Markdown under ``runs/conformance/``.

Tool calls are measured by wrapping ``ToolDef.bind`` for the duration of a role
execution; that single seam covers both the ``call_structured`` roles (sorter,
specialists) and the CrewAI roles (judge, arbiter, boss).
"""

from __future__ import annotations

import json
import os
import types
import typing
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from mailroom_reloaded.agents.arbiter import ArbiterDecision, arbitrate
from mailroom_reloaded.agents.boss import BossDecision, escalate
from mailroom_reloaded.agents.judge import JudgeVerdict, judge_grade, judge_verify
from mailroom_reloaded.agents.sorter import sort
from mailroom_reloaded.agents.specialists import extract
from mailroom_reloaded.eval.dataset import (
    DEFAULT_REVISION,
    BlindDoc,
    GroundTruth,
    doc_id_for_sha,
    load_split,
    sample,
)
from mailroom_reloaded.ingest.bert import Handoff, SortMode
from mailroom_reloaded.llm.client import resolve
from mailroom_reloaded.schemas.extraction import get_extraction_schema
from mailroom_reloaded.scoring import subclass_vocab
from mailroom_reloaded.settings import get_settings, load_taxonomy
from mailroom_reloaded.tools import ToolContext, ToolDef

__all__ = [
    "CARD_SCHEMA",
    "DEFAULT_OUT_DIR",
    "INVARIANTS",
    "NA",
    "ConformanceCard",
    "Invariant",
    "RoleRun",
    "RoleStats",
    "ToolCall",
    "build_role_stats",
    "render_card_md",
    "run_conformance",
]

CARD_SCHEMA = "mailroom.conformance/v1"
DEFAULT_OUT_DIR = Path("runs/conformance")

#: The five conformance roles, in spec section 11 table order.
_ROLES: tuple[str, ...] = ("sorter", "specialists", "judge", "arbiter", "boss")

_ARBITER_ACTIONS = frozenset(
    {"accept", "accept_with_caveats", "re_extract", "escalate"}
)
_BOSS_ACTIONS = frozenset({"reassign_class", "accept", "human_review"})

_MISSING = object()

#: The marker rendered/emitted for a rate whose denominator is undefined.
NA = "n/a"


def _rate(value: float | None) -> float | str:
    """A JSON rate: the number itself, or ``NA`` when the denominator is undefined."""
    return NA if value is None else value


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class ToolCall:
    """One tool execution and whether it returned a usable (non-error) result."""

    role: str
    name: str
    ok: bool


@dataclass(frozen=True)
class RoleRun:
    """One role execution over one fixture document.

    ``parsed`` is the role's structured output (for the sorter, the emitted
    fields rather than the re-derived ``SortResult``), ``schema_valid`` is the
    extraction compliance verdict, ``tool_calls`` is the recorded tool traffic
    and ``source_needed`` says whether an arbiter/boss run required the source.
    """

    role: str
    filename: str
    doc_type: str | None
    mode: str
    parsed: dict[str, Any] | None = None
    schema_valid: bool = True
    tool_calls: list[ToolCall] = field(default_factory=list)
    source_needed: bool = True
    result: Any = None


@dataclass(frozen=True)
class Invariant:
    """A named, role-scoped predicate over one :class:`RoleRun`."""

    role: str
    name: str
    check: Callable[[RoleRun], bool]


@dataclass
class RoleStats:
    """Per-role conformance result: tool-call and invariant pass rates.

    A rate is ``None`` when its denominator is undefined -- no tool calls were
    recorded, or no invariants/runs were collected. An undefined denominator is
    never a passing ``1.0``: it serializes as ``"n/a"`` and cannot be mistaken
    for a role that ran cleanly.
    """

    tool_call_success_rate: float | None
    invariant_pass_rate: float | None
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape, with undefined rates rendered as the ``"n/a"`` marker."""
        return {
            "tool_call_success_rate": _rate(self.tool_call_success_rate),
            "invariant_pass_rate": _rate(self.invariant_pass_rate),
            "failures": list(self.failures),
        }


@dataclass
class ConformanceCard:
    """The conformance card one provider run produces."""

    provider: str
    model: str
    roles: dict[str, RoleStats]
    schema: str = CARD_SCHEMA
    revision: str = DEFAULT_REVISION
    split: str = "train"
    per_class: int = 2
    generated_at: str = ""


# --------------------------------------------------------------------------- invariants


def _is_list_annotation(annotation: Any) -> bool:
    """True when the pydantic field annotation is a list type."""
    return typing.get_origin(annotation) is list


def _allows_none(annotation: Any) -> bool:
    """True when the annotation admits ``None`` (``X | None`` / ``Optional``)."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        return type(None) in typing.get_args(annotation)
    return annotation is type(None)


def _is_error(outcome: Any) -> bool:
    """A tool result is an error when its text starts with ``error``."""
    return str(outcome or "").lstrip().lower().startswith("error")


def _sorter_label_in_vocabulary(run: RoleRun) -> bool:
    """The sorter's class is in the taxonomy and its subclass in the class vocab."""
    if run.parsed is None or run.doc_type not in load_taxonomy().classes:
        return False
    subclass = (run.parsed or {}).get("doc_subclass")
    return subclass is None or subclass in subclass_vocab(run.doc_type)


def _sorter_subclass_only_guard(run: RoleRun) -> bool:
    """``SUBCLASS_ONLY`` must flag ``doc_type_disagree`` when it changes class."""
    if run.mode != "subclass_only":
        return True
    emitted = (run.parsed or {}).get("doc_type")
    if emitted is None or emitted == run.doc_type:
        return True
    return bool((run.parsed or {}).get("doc_type_disagree", False))


def _specialist_schema_valid(run: RoleRun) -> bool:
    """The extraction passed the class schema assessment."""
    return bool(run.schema_valid)


def _specialist_no_extra_keys(run: RoleRun) -> bool:
    """No emitted key falls outside the class extraction schema."""
    if not run.doc_type or not isinstance(run.parsed, dict):
        return True
    try:
        names = set(get_extraction_schema(run.doc_type).model_fields)
    except KeyError:
        return False
    return set(run.parsed) <= names


def _specialist_nulls_empties(run: RoleRun) -> bool:
    """List fields are lists and non-nullable fields are not null (frozen prompt)."""
    if not run.doc_type or not isinstance(run.parsed, dict):
        return True
    model = get_extraction_schema(run.doc_type)
    for name, fld in model.model_fields.items():
        value = run.parsed.get(name, _MISSING)
        if value is _MISSING:
            continue
        if _is_list_annotation(fld.annotation) and not isinstance(value, list):
            return False
        if value is None and not _allows_none(fld.annotation):
            return False
    return True


def _judge_grade_uses_ground_truth(run: RoleRun) -> bool:
    """Grade mode must call ``get_ground_truth`` successfully."""
    if run.mode != "grade":
        return True
    return any(tc.name == "get_ground_truth" and tc.ok for tc in run.tool_calls)


def _judge_live_never_ground_truth(run: RoleRun) -> bool:
    """Live mode must never call ``get_ground_truth``."""
    if run.mode != "live":
        return True
    return not any(tc.name == "get_ground_truth" for tc in run.tool_calls)


def _arbiter_valid_action(run: RoleRun) -> bool:
    """The arbiter returned one of its bounded actions."""
    return (run.parsed or {}).get("action") in _ARBITER_ACTIONS


def _boss_valid_action(run: RoleRun) -> bool:
    """The boss returned one of its bounded actions."""
    return (run.parsed or {}).get("action") in _BOSS_ACTIONS


def _tool_when_source_needed(run: RoleRun) -> bool:
    """A run that needed the source called at least one tool successfully."""
    if not run.source_needed:
        return True
    return any(tc.ok for tc in run.tool_calls)


INVARIANTS: tuple[Invariant, ...] = (
    Invariant("sorter", "label_in_vocabulary", _sorter_label_in_vocabulary),
    Invariant("sorter", "subclass_only_doc_type_guard", _sorter_subclass_only_guard),
    Invariant("specialists", "schema_valid", _specialist_schema_valid),
    Invariant("specialists", "no_keys_outside_schema", _specialist_no_extra_keys),
    Invariant("specialists", "nulls_empties_per_prompt", _specialist_nulls_empties),
    Invariant("judge", "grade_uses_ground_truth", _judge_grade_uses_ground_truth),
    Invariant("judge", "live_never_ground_truth", _judge_live_never_ground_truth),
    Invariant("arbiter", "valid_action", _arbiter_valid_action),
    Invariant("arbiter", "tool_when_source_needed", _tool_when_source_needed),
    Invariant("boss", "valid_action", _boss_valid_action),
    Invariant("boss", "tool_when_source_needed", _tool_when_source_needed),
)


def build_role_stats(runs: Iterable[RoleRun]) -> RoleStats:
    """Aggregate tool-call success and invariant pass rates for one role."""
    run_list = list(runs)
    calls = [tc for run in run_list for tc in run.tool_calls]
    tool_rate: float | None = (
        sum(1 for tc in calls if tc.ok) / len(calls) if calls else None
    )

    checked = 0
    passed = 0
    failures: list[str] = []
    for invariant in INVARIANTS:
        for run in run_list:
            if run.role != invariant.role:
                continue
            checked += 1
            if isinstance(run.result, BaseException):
                failures.append(f"{invariant.name}:{run.filename}")
                continue
            try:
                ok = bool(invariant.check(run))
            except Exception:  # noqa: BLE001 - a broken check is a failed check
                ok = False
            if ok:
                passed += 1
            else:
                failures.append(f"{invariant.name}:{run.filename}")
    invariant_rate: float | None = passed / checked if checked else None
    return RoleStats(tool_rate, invariant_rate, failures)


# --------------------------------------------------------------------------- tool recorder


@contextmanager
def _record_tool_calls(role: str, sink: list[ToolCall]):
    """Record every tool execution performed while the context is active."""
    original = ToolDef.bind

    def recording_bind(self: ToolDef, ctx: ToolContext) -> ToolDef:
        if self.bound:
            return self
        tool = original(self, ctx)
        base = tool.fn

        def call(**kwargs: Any) -> str:
            outcome = base(**kwargs)
            sink.append(ToolCall(role=role, name=tool.name, ok=not _is_error(outcome)))
            return outcome

        return replace(tool, fn=call, bound=True)

    ToolDef.bind = recording_bind  # type: ignore[method-assign]
    try:
        yield sink
    finally:
        ToolDef.bind = original  # type: ignore[method-assign]


# --------------------------------------------------------------------------- role runs


def _ground_truth_row(gt: GroundTruth | None) -> dict[str, Any]:
    """The mapping ``get_ground_truth`` hands the judge in grade mode."""
    if gt is None:
        return {}
    return {
        "expected": gt.expected,
        "expected_subclass": gt.expected_subclass,
        **gt.fields,
    }


def _sorter_runs(doc: BlindDoc, gt: GroundTruth | None) -> list[RoleRun]:
    """Run the sorter in FULL and (when a class is known) SUBCLASS_ONLY mode."""
    runs: list[RoleRun] = []
    full_calls: list[ToolCall] = []
    with _record_tool_calls("sorter", full_calls):
        result = sort(
            doc.doc_text, Handoff(SortMode.FULL, None, "", "conformance_full")
        )
    runs.append(
        RoleRun(
            role="sorter",
            filename=doc.filename,
            doc_type=result.doc_type,
            mode="full",
            parsed={
                "doc_type": result.doc_type,
                "doc_subclass": result.doc_subclass,
                "doc_type_disagree": result.doc_type_disagree,
            },
            tool_calls=list(full_calls),
            result=result,
        )
    )

    doc_type = (gt.expected if gt is not None else None) or result.doc_type
    if doc_type in load_taxonomy().classes:
        scope_calls: list[ToolCall] = []
        with _record_tool_calls("sorter", scope_calls):
            scoped = sort(
                doc.doc_text,
                Handoff(SortMode.SUBCLASS_ONLY, doc_type, "", "conformance_subclass"),
            )
        runs.append(
            RoleRun(
                role="sorter",
                filename=doc.filename,
                doc_type=doc_type,
                mode="subclass_only",
                parsed={
                    "doc_type": scoped.doc_type,
                    "doc_subclass": scoped.doc_subclass,
                    "doc_type_disagree": scoped.doc_type_disagree,
                },
                tool_calls=list(scope_calls),
                result=scoped,
            )
        )
    return runs


def _specialist_runs(doc: BlindDoc, gt: GroundTruth | None) -> list[RoleRun]:
    """Run the class specialist over the fixture document."""
    doc_type = gt.expected if gt is not None else None
    if doc_type not in load_taxonomy().classes:
        return []
    calls: list[ToolCall] = []
    with _record_tool_calls("specialists", calls):
        result = extract(
            doc.doc_text,
            doc_type,
            gt.expected_subclass if gt is not None else None,
        )
    return [
        RoleRun(
            role="specialists",
            filename=doc.filename,
            doc_type=doc_type,
            mode="live",
            parsed=result.data,
            schema_valid=result.schema_valid,
            tool_calls=list(calls),
            result=result,
        )
    ]


def _judge_runs(
    doc: BlindDoc, gt: GroundTruth | None, data: dict[str, Any] | None
) -> list[RoleRun]:
    """Run the judge in live (verify) and grade modes."""
    doc_type = gt.expected if gt is not None else None
    if doc_type not in load_taxonomy().classes:
        return []
    doc_id = doc_id_for_sha(doc.content_sha256)
    runs: list[RoleRun] = []

    live_calls: list[ToolCall] = []
    with _record_tool_calls("judge", live_calls):
        verdict = judge_verify(
            doc.doc_text,
            doc_type,
            data,
            ToolContext(doc_text=doc.doc_text, doc_id=doc_id, eval_mode=False),
        )
    runs.append(
        RoleRun(
            role="judge",
            filename=doc.filename,
            doc_type=doc_type,
            mode="live",
            parsed=verdict.model_dump(),
            tool_calls=list(live_calls),
            source_needed=False,
            result=verdict,
        )
    )

    grade_calls: list[ToolCall] = []
    ctx = ToolContext(
        doc_text=doc.doc_text,
        doc_id=doc_id,
        eval_mode=True,
        ground_truth=lambda _doc_id: _ground_truth_row(gt),
    )
    with _record_tool_calls("judge", grade_calls):
        grade = judge_grade(doc.doc_text, doc_type, data, ctx)
    runs.append(
        RoleRun(
            role="judge",
            filename=doc.filename,
            doc_type=doc_type,
            mode="grade",
            parsed=grade.model_dump(exclude={"usage"}),
            tool_calls=list(grade_calls),
            source_needed=True,
            result=grade,
        )
    )
    return runs


def _arbiter_runs(
    doc: BlindDoc, gt: GroundTruth | None, data: dict[str, Any] | None
) -> list[RoleRun]:
    """Run the arbiter over a synthetic partial verdict."""
    doc_type = gt.expected if gt is not None else None
    if doc_type not in load_taxonomy().classes:
        return []
    verdict = JudgeVerdict(label="partial", score=0.6, field_findings=[])
    calls: list[ToolCall] = []
    ctx = ToolContext(doc_text=doc.doc_text, doc_id=doc_id_for_sha(doc.content_sha256))
    with _record_tool_calls("arbiter", calls):
        decision: ArbiterDecision = arbitrate(
            doc.doc_text, doc_type, data, verdict, ctx
        )
    return [
        RoleRun(
            role="arbiter",
            filename=doc.filename,
            doc_type=doc_type,
            mode="live",
            parsed=decision.model_dump(),
            tool_calls=list(calls),
            source_needed=True,
            result=decision,
        )
    ]


def _boss_runs(doc: BlindDoc, gt: GroundTruth | None) -> list[RoleRun]:
    """Run the boss over a minimal escalation summary."""
    doc_type = gt.expected if gt is not None else None
    if doc_type not in load_taxonomy().classes:
        return []
    calls: list[ToolCall] = []
    ctx = ToolContext(doc_text=doc.doc_text, doc_id=doc_id_for_sha(doc.content_sha256))
    with _record_tool_calls("boss", calls):
        decision: BossDecision = escalate(
            doc.doc_text, {"doc_type": doc_type, "attempts": 2}, ctx
        )
    return [
        RoleRun(
            role="boss",
            filename=doc.filename,
            doc_type=doc_type,
            mode="live",
            parsed=decision.model_dump(),
            tool_calls=list(calls),
            source_needed=True,
            result=decision,
        )
    ]


def _failure(
    role: str, doc: BlindDoc, doc_type: str | None, mode: str, exc: BaseException
) -> RoleRun:
    """A failed role run, kept so the card reports the failure rather than hiding it."""
    return RoleRun(
        role=role,
        filename=doc.filename,
        doc_type=doc_type,
        mode=mode,
        parsed=None,
        schema_valid=False,
        source_needed=True,
        result=exc,
    )


def _collect_runs(
    *,
    per_class: int,
    revision: str,
    split: str,
    local_dir: Path | None,
) -> dict[str, list[RoleRun]]:
    """Load the split, sample fixtures and run every role over each document.

    A single role failure is recorded as one failed :class:`RoleRun` so the card
    reports the role as failing its invariants instead of producing no card.
    """
    docs, gts = load_split(revision, split, local_dir=local_dir)
    selected = sample(docs, gts, per_class=per_class)
    runs: dict[str, list[RoleRun]] = {role: [] for role in _ROLES}
    for doc in selected:
        gt = gts.get(doc.filename)
        doc_type = gt.expected if gt is not None else None

        try:
            runs["sorter"].extend(_sorter_runs(doc, gt))
        except Exception as exc:  # noqa: BLE001
            runs["sorter"].append(_failure("sorter", doc, doc_type, "full", exc))

        specialists: list[RoleRun] = []
        try:
            specialists = _specialist_runs(doc, gt)
            runs["specialists"].extend(specialists)
        except Exception as exc:  # noqa: BLE001
            runs["specialists"].append(
                _failure("specialists", doc, doc_type, "live", exc)
            )
        data = specialists[0].parsed if specialists else {}

        for role, runner in (
            ("judge", partial(_judge_runs, doc, gt, data)),
            ("arbiter", partial(_arbiter_runs, doc, gt, data)),
            ("boss", partial(_boss_runs, doc, gt)),
        ):
            try:
                runs[role].extend(runner())
            except Exception as exc:  # noqa: BLE001
                runs[role].append(_failure(role, doc, doc_type, "live", exc))
    return runs


# --------------------------------------------------------------------------- card


def _provider_model() -> str:
    """The model id the resolved provider serves for the sorter role."""
    try:
        return resolve("sorter").model
    except Exception:  # noqa: BLE001 - card must still be written offline
        return load_taxonomy().agent("sorter").model


def _card_dict(card: ConformanceCard) -> dict[str, Any]:
    """Serialize a card to the JSON shape written under ``runs/conformance``."""
    return {
        "schema": card.schema,
        "generated_at": card.generated_at,
        "provider": card.provider,
        "model": card.model,
        "revision": card.revision,
        "split": card.split,
        "per_class": card.per_class,
        "roles": {role: stats.to_dict() for role, stats in card.roles.items()},
    }


def _pct(value: float | None) -> str:
    """Render a 0..1 rate as a percentage, or ``n/a`` when undefined."""
    return NA if value is None else f"{value * 100:.1f}%"


def render_card_md(card: ConformanceCard) -> str:
    """Render a conformance card as a Markdown summary."""
    lines = [
        f"# Behavioural conformance — {card.provider}",
        "",
        (
            f"**Model:** `{card.model}` · **Revision:** `{card.revision}` · "
            f"**Split:** {card.split} · **Per class:** {card.per_class}"
        ),
        "",
        "| Role | Tool-call success | Invariant pass | Failures |",
        "| --- | ---: | ---: | --- |",
    ]
    for role in _ROLES:
        stats = card.roles.get(role)
        if stats is None:
            lines.append(f"| {role} | — | — | not run |")
            continue
        failures = ", ".join(stats.failures) if stats.failures else "—"
        lines.append(
            f"| {role} | {_pct(stats.tool_call_success_rate)} | "
            f"{_pct(stats.invariant_pass_rate)} | {failures} |"
        )
    lines += [
        "",
        f"_Generated {card.generated_at} by mailroom conformance._",
        "",
    ]
    return "\n".join(lines)


def _write_card(card: ConformanceCard, out_dir: Path) -> tuple[Path, Path]:
    """Write the JSON and Markdown cards and return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"conformance-{card.provider or 'default'}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(_card_dict(card), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    md_path.write_text(render_card_md(card), encoding="utf-8")
    return json_path, md_path


def run_conformance(
    provider: str | None = None,
    *,
    per_class: int = 2,
    revision: str = DEFAULT_REVISION,
    split: str = "train",
    local_dir: Path | None = None,
    out_dir: Path | None = None,
) -> ConformanceCard:
    """Run the behavioural conformance suite and write its card.

    ``provider`` selects the endpoint for the run (``DEFAULT_PROVIDER`` during
    the run, restored afterwards); when omitted the configured provider is used.
    Fixtures come from the TRAIN split (``per_class`` documents per class); the
    card is written as JSON and Markdown under ``runs/conformance/``.
    """
    previous = os.environ.get("DEFAULT_PROVIDER")
    if provider:
        os.environ["DEFAULT_PROVIDER"] = provider
        get_settings.cache_clear()
    try:
        resolved = provider or get_settings().provider
        model = _provider_model()
        runs = _collect_runs(
            per_class=per_class,
            revision=revision,
            split=split,
            local_dir=Path(local_dir) if local_dir is not None else None,
        )
    finally:
        if provider:
            if previous is None:
                os.environ.pop("DEFAULT_PROVIDER", None)
            else:
                os.environ["DEFAULT_PROVIDER"] = previous
            get_settings.cache_clear()

    roles = {role: build_role_stats(runs.get(role, [])) for role in _ROLES}
    card = ConformanceCard(
        provider=resolved,
        model=model,
        roles=roles,
        revision=revision,
        split=split,
        per_class=per_class,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    _write_card(card, Path(out_dir) if out_dir is not None else DEFAULT_OUT_DIR)
    return card
