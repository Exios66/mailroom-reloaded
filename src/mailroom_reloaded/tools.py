"""One tool definition, two surfaces: CrewAI ``BaseTool`` and OpenAI function spec.

Design: ``ToolDef.fn`` is written as ``fn(ctx: ToolContext, **params) -> str``.
``ToolDef.bind(ctx)`` returns a copy whose ``fn(**params)`` closes over ``ctx``,
validates the arguments with ``params_model`` and never raises (errors come back
as ``"error: ..."`` strings). ``tools_for(role, ctx)`` returns bound tools, so
Task 7's tool loop (``ToolLike``: name/description/params_model/fn) simply calls
``tool.fn(**args)``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from mailroom_reloaded.schemas.extraction import get_extraction_schema as _get_schema
from mailroom_reloaded.scoring import subclass_vocab
from mailroom_reloaded.settings import load_taxonomy

SNIPPET_CHARS = 400
MAX_SNIPPETS = 3


@dataclass
class ToolContext:
    doc_text: str = ""
    doc_id: str = ""
    eval_mode: bool = False
    ground_truth: Callable[[str], dict] | None = None


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoParams(_Params):
    pass


class DocTypeParams(_Params):
    doc_type: str


class SearchParams(_Params):
    query: str


class GroundTruthParams(_Params):
    """No model-supplied arguments: the document id comes from the context."""


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    params_model: type[BaseModel]
    fn: Callable[..., str]
    eval_only: bool = False
    bound: bool = field(default=False, compare=False)

    def bind(self, ctx: ToolContext) -> ToolDef:
        """Return a copy whose ``fn(**params)`` is validated and closes over ``ctx``."""
        if self.bound:
            return self
        raw = self.fn
        if self.eval_only and not (ctx.eval_mode and ctx.ground_truth is not None):
            return replace(
                self,
                fn=lambda **_: "error: unavailable outside evaluation mode",
                bound=True,
            )

        def call(**kwargs: Any) -> str:
            try:
                params = self.params_model.model_validate(kwargs)
            except ValidationError as exc:
                msgs = "; ".join(
                    f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}"
                    for e in exc.errors()
                )
                return f"error: invalid arguments for {self.name}: {msgs}"
            try:
                return raw(ctx, **params.model_dump())
            except Exception as exc:  # noqa: BLE001 - tools never raise
                return f"error: {self.name} failed: {exc}"

        return replace(self, fn=call, bound=True)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _get_taxonomy(ctx: ToolContext) -> str:
    tax = load_taxonomy()
    return _dumps(
        {
            "doc_classes": [
                {"key": c.key, "label": c.label, "description": c.description}
                for c in tax.classes.values()
            ],
            "confidence": {
                k: tax.confidence.get(k) for k in ("low", "high", "retry_max")
            },
        }
    )


def _check_doc_type(doc_type: str) -> str | None:
    known = list(load_taxonomy().classes)
    if doc_type not in known:
        return f"error: unknown doc_type {doc_type!r}; expected one of {known}"
    return None


def _list_subclasses(ctx: ToolContext, doc_type: str) -> str:
    return _check_doc_type(doc_type) or _dumps(
        {"doc_type": doc_type, "subclasses": subclass_vocab(doc_type)}
    )


def _get_extraction_schema(ctx: ToolContext, doc_type: str) -> str:
    if (err := _check_doc_type(doc_type)) or (model := _get_schema(doc_type)) is None:
        return err or f"error: no schema for {doc_type!r}"
    return _dumps(
        {
            "doc_type": doc_type,
            "fields": list(model.model_fields),
            "json_schema": model.model_json_schema(),
        }
    )


def _get_field_types(ctx: ToolContext, doc_type: str) -> str:
    return _check_doc_type(doc_type) or _dumps(
        {
            "doc_type": doc_type,
            "field_types": dict(load_taxonomy().classes[doc_type].field_types),
        }
    )


def _search_source(ctx: ToolContext, query: str) -> str:
    text, q = ctx.doc_text, query.strip()
    if not q:
        return "error: empty query"
    low, ql = text.lower(), q.lower()
    snippets: list[str] = []
    pos, last_end = 0, 0
    while len(snippets) < MAX_SNIPPETS:
        i = low.find(ql, pos)
        if i < 0:
            break
        start = max(last_end, i - max(0, SNIPPET_CHARS - len(q)) // 2)
        end = min(len(text), start + SNIPPET_CHARS)
        snippets.append(text[start:end])
        last_end = end
        pos = max(end, i + 1)
    return _dumps({"query": q, "snippets": snippets})


def _get_ground_truth(ctx: ToolContext) -> str:
    if ctx.ground_truth is None:
        return "error: ground truth unavailable"
    return _dumps(ctx.ground_truth(ctx.doc_id))


TOOLS: dict[str, ToolDef] = {
    t.name: t
    for t in (
        ToolDef(
            "get_taxonomy",
            "Pipeline taxonomy: document classes (key, label, description) and confidence thresholds.",
            NoParams,
            _get_taxonomy,
        ),
        ToolDef(
            "list_subclasses",
            "List the canonical subclass keys for a document type.",
            DocTypeParams,
            _list_subclasses,
        ),
        ToolDef(
            "get_extraction_schema",
            "The extraction schema (field names and JSON schema) for a document type.",
            DocTypeParams,
            _get_extraction_schema,
        ),
        ToolDef(
            "get_field_types",
            "Per-field deterministic scoring types for a document type.",
            DocTypeParams,
            _get_field_types,
        ),
        ToolDef(
            "search_source",
            "Case-insensitive search of the source document; returns up to 3 snippets of 400 characters.",
            SearchParams,
            _search_source,
        ),
        ToolDef(
            "get_ground_truth",
            "Ground-truth fields for a document (evaluation mode only; labels may be noisy).",
            GroundTruthParams,
            _get_ground_truth,
            eval_only=True,
        ),
    )
}

_SPECIALIST_TOOLS = ("get_extraction_schema", "get_field_types")
_ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "sorter": ("get_taxonomy", "list_subclasses"),
    "judge": (
        "get_extraction_schema",
        "get_field_types",
        "search_source",
        "get_ground_truth",
    ),
    "arbiter": ("get_taxonomy", "get_extraction_schema", "search_source"),
    "boss": ("get_taxonomy", "list_subclasses", "search_source"),
}


def _role_names(role: str) -> tuple[str, ...]:
    if role in _ROLE_TOOLS:
        return _ROLE_TOOLS[role]
    if role in {c.specialist for c in load_taxonomy().classes.values()}:
        return _SPECIALIST_TOOLS
    return ()


def tools_for(role: str, context: ToolContext) -> list[ToolDef]:
    """Tools for ``role``, bound to ``context``. eval_only tools need eval mode + ground truth."""
    out = []
    for name in _role_names(role):
        td = TOOLS[name]
        if td.eval_only and not (
            context.eval_mode and context.ground_truth is not None
        ):
            continue
        out.append(td.bind(context))
    return out


def openai_spec(td: ToolDef) -> dict[str, Any]:
    """OpenAI chat-completions function spec."""
    params = td.params_model.model_json_schema()
    params.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": td.name,
            "description": td.description,
            "parameters": params,
        },
    }


def crewai_tool(td: ToolDef, *, context: ToolContext):
    """CrewAI ``BaseTool`` instance whose ``_run`` delegates to the bound ``fn``."""
    os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
    from crewai.tools import BaseTool
    from pydantic import PrivateAttr

    bound = td.bind(context)

    class MailroomTool(BaseTool):
        _bound: Any = PrivateAttr(default=None)

        def _run(self, **kwargs: Any) -> str:
            return self._bound.fn(**kwargs)

    tool = MailroomTool(
        name=td.name, description=td.description, args_schema=td.params_model
    )
    tool._bound = bound
    return tool
