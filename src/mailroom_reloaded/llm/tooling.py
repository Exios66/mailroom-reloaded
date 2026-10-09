"""Standalone tool loop (spec section 7): bounded tool rounds, then a forced final answer.

Tool rounds never carry ``response_format`` (a grammar-constrained turn cannot
emit ``tool_calls`` on vLLM). The caller sends the strict-schema final turn
separately, with ``tool_choice="none"``. Endpoints that reject ``tools`` fall
back to a tool-free prompt with the tool results inlined in the system message.
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import openai
import structlog
from pydantic import BaseModel, ValidationError

from .retry import with_retry
from .usage import Usage

logger = structlog.get_logger(__name__)

MAX_PREFILL_COMBOS = 8


class LengthFinishReasonError(Exception):
    """The model hit its output cap (``finish_reason == "length"``)."""

    def __init__(self, message: str, *, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage if usage is not None else Usage()


@runtime_checkable
class ToolLike(Protocol):
    """What the tool loop needs from a tool; Task 8's ``ToolDef`` satisfies it."""

    name: str
    description: str
    params_model: type[BaseModel]

    def fn(self, **kwargs: Any) -> str: ...


@dataclass
class ToolLoopResult:
    messages: list[dict[str, Any]]
    rounds: int
    usage: Usage
    inline: bool = False
    reject_key: tuple[str, str] | None = (
        None  # set when round 1 was rejected for tool support
    )
    tool_log: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)


# (base_url, model) pairs known to reject ``tools``; later calls skip the doomed request.
_NO_TOOLS: set[tuple[str, str]] = set()


def reset_tool_support_cache() -> None:
    _NO_TOOLS.clear()


_TOOL_REJECTION_MARKERS = ("tool", "function")
# A 400 about request size or our own tool schema is a real error, not missing support.
_NOT_TOOL_SUPPORT_MARKERS = (
    "context length",
    "context window",
    "too many tokens",
    "invalid schema",
)


def is_tool_rejection(exc: Exception) -> bool:
    """A 400 whose body says the endpoint/model does not support tool calling."""
    text = str(exc).lower()
    if any(m in text for m in _NOT_TOOL_SUPPORT_MARKERS):
        return False
    return any(m in text for m in _TOOL_REJECTION_MARKERS)


def mark_no_tools(key: tuple[str, str]) -> None:
    _NO_TOOLS.add(key)


def tool_spec(tool: ToolLike) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.params_model.model_json_schema(),
        },
    }


def execute_tool(
    tools: dict[str, ToolLike], name: str, raw_args: str | dict[str, Any] | None
) -> str:
    """Run one tool call. Failures come back as an error string for the model."""
    tool = tools.get(name)
    if tool is None:
        return (
            f"Error: unknown tool '{name}'. Available tools: {', '.join(sorted(tools))}"
        )
    try:
        args = (
            json.loads(raw_args)
            if isinstance(raw_args, str) and raw_args.strip()
            else (raw_args or {})
        )
        if not isinstance(args, dict):
            raise TypeError("arguments must be a JSON object")
        parsed = tool.params_model(**args)
    except (ValueError, TypeError, ValidationError) as exc:
        return f"Error: invalid arguments for tool '{name}': {exc}"
    try:
        return str(tool.fn(**parsed.model_dump()))
    except Exception as exc:  # noqa: BLE001 - tool errors are model-visible, never fatal
        return f"Error: tool '{name}' failed: {type(exc).__name__}: {exc}"


def chat_create(client: Any, req: dict[str, Any]) -> tuple[Any, Usage]:
    """One chat completion with transport retries; usage counts the successful call only."""
    timing: dict[str, float] = {}

    def _once() -> Any:
        start = time.monotonic()
        resp = client.chat.completions.create(**req)
        timing["s"] = time.monotonic() - start
        return resp

    resp = with_retry(_once)
    u = getattr(resp, "usage", None)
    usage = Usage(
        prompt_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(u, "completion_tokens", 0) or 0),
        latency_s=timing.get("s", 0.0),
        calls=1,
    )
    return resp, usage


def _prefill_arg_sets(tool: ToolLike) -> list[dict[str, Any]] | None:
    """Argument sets a tool can be run with unprompted, or None when it needs free-form input."""
    schema = tool.params_model.model_json_schema()
    required = schema.get("required", [])
    props = schema.get("properties", {})
    if not required:
        return [{}]
    domains: list[list[Any]] = []
    for key in required:
        prop = props.get(key, {})
        if "enum" in prop:
            domains.append(list(prop["enum"]))
        elif "const" in prop:
            domains.append([prop["const"]])
        else:
            return None
    combos = [dict(zip(required, vals)) for vals in itertools.product(*domains)]
    return combos if len(combos) <= MAX_PREFILL_COMBOS else None


def inline_messages(
    messages: list[dict[str, Any]],
    tools: list[ToolLike],
    tool_log: list[tuple[str, dict[str, Any], str]],
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any], str]]]:
    """Tool-free prompt: tool results appended to the system message.

    Results already gathered are kept; tools whose arguments are empty or finite
    (enums) are pre-run for every allowed value; the rest are listed as unavailable.
    """
    by_name = {t.name: t for t in tools}
    log = list(tool_log)
    seen = {(n, json.dumps(a, sort_keys=True)) for n, a, _ in log}
    unavailable: list[ToolLike] = []
    for tool in tools:
        arg_sets = _prefill_arg_sets(tool)
        if arg_sets is None:
            unavailable.append(tool)
            continue
        for args in arg_sets:
            if (tool.name, json.dumps(args, sort_keys=True)) not in seen:
                log.append((tool.name, args, execute_tool(by_name, tool.name, args)))
    lines = [
        "",
        "",
        "## Reference tool results",
        "(Tools cannot be called on this endpoint; their results are supplied here.)",
    ]
    for name, args, result in log:
        lines.append(f"### {name}({json.dumps(args, sort_keys=True)})")
        lines.append(result)
    if unavailable:
        lines.append(
            "### Not available (need arguments only a live tool call could supply)"
        )
        lines.extend(
            f"- {t.name}: {t.description} (not available)" for t in unavailable
        )
    block = "\n".join(lines)
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system":
        out[0]["content"] = f"{out[0].get('content') or ''}{block}"
    else:
        out.insert(0, {"role": "system", "content": block.lstrip()})
    return out, log


def run_tool_loop(
    client: Any, req: dict[str, Any], tools: list[ToolLike], max_rounds: int = 3
) -> ToolLoopResult:
    """Run up to ``max_rounds`` tool rounds against ``req`` (no ``response_format`` is sent).

    ``req`` holds the chat-completion kwargs (model, messages, sampling, extra_body).
    Returns the extended message history for the final structured turn. The loop
    ends early when the model answers without calling a tool.
    """
    base_messages = list(req["messages"])
    usage = Usage()
    key = (str(getattr(client, "base_url", "")), str(req.get("model")))
    tool_map = {t.name: t for t in tools}
    log: list[tuple[str, dict[str, Any], str]] = []
    rounds = 0
    reject_key: tuple[str, str] | None = None
    messages = list(base_messages)
    if key not in _NO_TOOLS:
        specs = [tool_spec(t) for t in tools]
        phase = {
            k: v for k, v in req.items() if k not in ("response_format", "logprobs")
        }
        for _ in range(max_rounds):
            try:
                resp, u = chat_create(
                    client,
                    {
                        **phase,
                        "messages": messages,
                        "tools": specs,
                        "tool_choice": "auto",
                    },
                )
            except openai.BadRequestError as exc:
                if not is_tool_rejection(exc):
                    raise
                logger.warning(
                    "llm_tools_rejected", model=req.get("model"), detail=str(exc)[:200]
                )
                reject_key = key if rounds == 0 else None
                break
            usage = usage + u
            choice = resp.choices[0]
            calls = choice.message.tool_calls or []
            if (
                not calls
            ):  # draft reply is discarded, so a length cap on it is irrelevant
                return ToolLoopResult(messages, rounds, usage, False, None, log)
            if choice.finish_reason == "length":
                raise LengthFinishReasonError(
                    "output hit the length cap during a tool round", usage=usage
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": choice.message.content,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.function.name,
                                "arguments": c.function.arguments,
                            },
                        }
                        for c in calls
                    ],
                }
            )
            for c in calls:
                result = execute_tool(tool_map, c.function.name, c.function.arguments)
                try:
                    shown = json.loads(c.function.arguments or "{}")
                except ValueError:
                    shown = {"_raw": c.function.arguments}
                log.append(
                    (
                        c.function.name,
                        shown if isinstance(shown, dict) else {"_raw": shown},
                        result,
                    )
                )
                messages.append(
                    {"role": "tool", "tool_call_id": c.id, "content": result}
                )
            rounds += 1
        else:
            return ToolLoopResult(messages, rounds, usage, False, None, log)
    # Endpoint rejected (now or earlier) tool calling: inline fallback.
    inlined, log = inline_messages(base_messages, tools, log)
    return ToolLoopResult(inlined, rounds, usage, True, reject_key, log)
