"""OpenAI-compatible client: provider resolution, CrewAI LLM factory, structured calls.

``call_structured`` talks to the resolved endpoint with the ``openai`` SDK
directly (deterministic control over ``tool_calls``, ``logprobs`` and
``finish_reason``); ``make_llm`` feeds the CrewAI agents.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any

import openai
import structlog

os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")

from crewai import LLM

from mailroom_reloaded.obs.metrics import M
from mailroom_reloaded.settings import get_settings, load_taxonomy

from .tooling import (
    LengthFinishReasonError,
    ToolLike,
    chat_create,
    mark_no_tools,
    run_tool_loop,
)
from .usage import Usage

__all__ = [
    "LLMResult",
    "LengthFinishReasonError",
    "ResolvedModel",
    "call_structured",
    "make_llm",
    "resolve",
]

logger = structlog.get_logger(__name__)

_MOCK_PLACEHOLDER_KEYS = {"mock-key"}
_DEFAULT_URLS = {
    "vllm": "http://localhost:8000/v1",
    "llamafile": "http://localhost:8080/v1",
}
_OPENAI_SAMPLING = {
    "temperature",
    "max_tokens",
    "top_p",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "stop",
}


@dataclass(frozen=True)
class ResolvedModel:
    provider: str
    model: str
    base_url: str
    api_key: str
    supports_tools: bool | None  # None = unknown (probe on first use)
    supports_logprobs: bool


@dataclass(frozen=True)
class LLMResult:
    content: str
    parsed: dict | None
    usage: Usage
    finish_reason: str
    label_logprob: float | None
    tool_rounds: int


def resolve(role: str) -> ResolvedModel:
    """Resolve an agent role to its endpoint. The global ``provider`` setting wins.

    Raises KeyError for an unknown role and ValueError for a missing endpoint or key.
    """
    settings = get_settings()
    tax = load_taxonomy()
    cfg = tax.agent(role)
    provider = settings.provider
    model = cfg.model
    if provider == "vllm":
        base_url = (settings.vllm_base_url or "").strip()
        if not base_url:
            base_url = _DEFAULT_URLS["vllm"]
            logger.warning("vllm_base_url_unset", using=base_url)
        model = str((tax.raw.get("vllm_model_map") or {}).get(model) or model)
        api_key = os.environ.get("VLLM_API_KEY") or "not-needed"
        return ResolvedModel(provider, model, base_url, api_key, True, True)
    if provider == "llamafile":
        base_url = (settings.llamafile_base_url or _DEFAULT_URLS["llamafile"]).strip()
        model = str((tax.raw.get("llamafile_model_map") or {}).get(model) or model)
        return ResolvedModel(provider, model, base_url, "not-needed", None, True)
    if provider == "openrouter":
        key = (settings.openrouter_api_key or "").strip()
        if not key or key in _MOCK_PLACEHOLDER_KEYS:
            raise ValueError(
                "OpenRouter API key not set (or is the mock placeholder): set OPENROUTER_API_KEY"
            )
        base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        return ResolvedModel(provider, model, base_url, key, True, False)
    if provider == "mock":
        base_url = os.environ.get("MOCK_BASE_URL", "").strip()
        if not base_url:
            raise ValueError(
                "provider 'mock' needs MOCK_BASE_URL (point it at the fake OpenAI server)"
            )
        return ResolvedModel(provider, model, base_url, "not-needed", None, True)
    raise ValueError(
        f"Unknown provider: {provider!r}. Available: llamafile, openrouter, vllm, mock"
    )


def make_llm(role: str, **overrides: Any) -> LLM:
    """A ``crewai.LLM`` for ``role`` against the resolved endpoint."""
    r = resolve(role)
    cfg = load_taxonomy().agent(role)
    kwargs: dict[str, Any] = {"temperature": cfg.temperature}
    if cfg.max_tokens is not None:
        kwargs["max_tokens"] = cfg.max_tokens
    kwargs.update(overrides)
    return LLM(
        model=f"openai/{r.model}", base_url=r.base_url, api_key=r.api_key, **kwargs
    )


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _parse_json(content: str) -> dict | None:
    text = content.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except ValueError:
        start = text.find("{")
        if start < 0:
            return None
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _label_logprob(logprobs: Any) -> float | None:
    """Sum of token logprobs over the value of the first ``LABEL:`` line."""
    tokens = getattr(logprobs, "content", None) if logprobs is not None else None
    if not tokens:
        return None
    pieces = [(str(t.token), float(t.logprob)) for t in tokens]
    text = "".join(p for p, _ in pieces)
    for m in re.finditer(r"^[ \t]*LABEL:", text, re.MULTILINE):
        line_end = text.find("\n", m.end())
        line_end = len(text) if line_end < 0 else line_end
        raw = text[m.end() : line_end]
        start = m.end() + (len(raw) - len(raw.lstrip()))
        end = m.end() + len(raw.rstrip())
        if end <= start:
            return None
        total, pos, hit = 0.0, 0, False
        for piece, lp in pieces:
            lo, hi = pos, pos + len(piece)
            pos = hi
            if hi > start and lo < end:
                total += lp
                hit = True
        return total if hit and math.isfinite(total) else None
    return None


def _build_request(
    r: ResolvedModel,
    role: str,
    messages: list[dict[str, Any]],
    sampling: dict[str, Any],
) -> dict[str, Any]:
    """Build chat parameters from role defaults and sampling overrides.

    Omit ``None`` values and place provider-specific options in ``extra_body``.
    Unknown roles raise ``KeyError``; taxonomy loading errors propagate.
    """
    cfg = load_taxonomy().agent(role)
    params: dict[str, Any] = {"temperature": cfg.temperature}
    if cfg.max_tokens is not None:
        params["max_tokens"] = cfg.max_tokens
    params.update(sampling)
    extra: dict[str, Any] = {}
    req: dict[str, Any] = {"model": r.model, "messages": list(messages)}
    for key, value in params.items():
        if value is None:
            continue
        if key in _OPENAI_SAMPLING:
            req[key] = value
        else:  # top_k, min_p, repetition_penalty, ...: server-specific
            extra[key] = value
    if r.provider == "vllm":
        # SAND-37 engine condition: Qwen3 thinking off.
        extra["chat_template_kwargs"] = {"enable_thinking": False}
    elif r.provider == "openrouter" and cfg.reasoning_effort:
        extra["reasoning"] = {"effort": cfg.reasoning_effort}
    if extra:
        req["extra_body"] = extra
    return req


def _token_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost for one call from ``taxonomy.cost_models`` (0.0 when unpriced)."""
    specs = load_taxonomy().raw.get("cost_models") or {}
    spec = specs.get(model) or {}
    per_input = float(spec.get("input_per_million", 0.0) or 0.0)
    per_output = float(spec.get("output_per_million", 0.0) or 0.0)
    return (
        prompt_tokens / 1_000_000 * per_input
        + completion_tokens / 1_000_000 * per_output
    )


def _record_usage_metrics(role: str, r: ResolvedModel, usage: Usage) -> None:
    """Emit the per-call OTel metrics (llm_calls, tokens, duration, cost)."""
    labels = {"role": role, "provider": r.provider, "model": r.model}
    M.llm_calls.add(max(usage.calls, 1), labels)
    token_attrs = {
        "gen_ai.request.model": r.model,
        "gen_ai.provider.name": r.provider,
    }
    M.token_usage.record(
        usage.prompt_tokens, {**token_attrs, "gen_ai.token.type": "input"}
    )
    M.token_usage.record(
        usage.completion_tokens, {**token_attrs, "gen_ai.token.type": "output"}
    )
    M.operation_duration.record(
        usage.latency_s, {**token_attrs, "gen_ai.operation.name": "chat"}
    )
    cost = _token_cost(r.model, usage.prompt_tokens, usage.completion_tokens)
    M.cost_usd.add(cost, labels)


def call_structured(
    role: str,
    messages: list[dict[str, Any]],
    *,
    schema_doc_type: str | None = None,
    response_format: dict | None = None,
    tools: list[ToolLike] | tuple[ToolLike, ...] = (),
    logprobs: bool = False,
    timeout: float = 600.0,
    **sampling: Any,
) -> LLMResult:
    """One structured completion, with up to three tool rounds first.

    Two-phase protocol: tool rounds never carry ``response_format``; the final
    turn carries the strict ``response_format`` (from ``response_format`` or the
    extraction schema of ``schema_doc_type``) and, when tools were offered,
    ``tool_choice="none"``. Raises ``LengthFinishReasonError`` on a length cap.
    """
    if response_format is None and schema_doc_type is not None:
        from mailroom_reloaded.schemas.extraction import response_format as _rf

        response_format = _rf(schema_doc_type)
    r = resolve(role)
    client = openai.OpenAI(
        base_url=r.base_url, api_key=r.api_key, max_retries=0, timeout=timeout
    )
    req = _build_request(r, role, messages, sampling)
    usage = Usage()
    tool_rounds = 0
    final_messages = req["messages"]
    tools_in_play = False
    loop = None
    if tools:
        try:
            loop = run_tool_loop(client, req, list(tools))
        except LengthFinishReasonError as exc:
            _record_usage_metrics(role, r, usage + exc.usage)
            M.length_capped.add(
                1, {"role": role, "provider": r.provider, "model": r.model}
            )
            raise
        usage, tool_rounds, final_messages = (
            usage + loop.usage,
            loop.rounds,
            loop.messages,
        )
        tools_in_play = not loop.inline
    final: dict[str, Any] = {**req, "messages": final_messages}
    if response_format is not None:
        final["response_format"] = response_format
    if (
        tools_in_play and r.provider == "vllm"
    ):  # others reject tool_choice without tools
        final["tool_choice"] = "none"
    if logprobs and r.supports_logprobs:
        final["logprobs"] = True
    resp, u = chat_create(client, final)
    usage = usage + u
    _record_usage_metrics(role, r, usage)
    if loop is not None and loop.reject_key is not None:
        mark_no_tools(
            loop.reject_key
        )  # inline retry worked, so the rejection was about tools
    choice = resp.choices[0]
    finish = choice.finish_reason or "stop"
    M.length_capped.add(
        1 if finish == "length" else 0,
        {"role": role, "provider": r.provider, "model": r.model},
    )
    if finish == "length":
        raise LengthFinishReasonError(f"{role}: output hit the length cap")
    content = choice.message.content or ""
    return LLMResult(
        content=content,
        parsed=_parse_json(content),
        usage=usage,
        finish_reason=finish,
        label_logprob=_label_logprob(getattr(choice, "logprobs", None))
        if final.get("logprobs")
        else None,
        tool_rounds=tool_rounds,
    )
