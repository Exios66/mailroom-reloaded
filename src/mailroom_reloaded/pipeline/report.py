"""Deterministic report writer (ported from ``agents/reporter.py``).

``compile_report(state)`` makes no LLM calls: it assembles the classification,
the extraction, the arbiter caveats, the route trail, the usage totals and a
token-cost estimate into a JSON-serialisable dict. The flow attaches the
logical ``llm_calls`` count (sorter + specialist invocations; tool rounds are
reported separately in ``usage.calls``) before persisting the report.
"""

from __future__ import annotations

from typing import Any

from mailroom_reloaded.llm.client import price_role
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline.state import MailroomState
from mailroom_reloaded.settings import load_taxonomy

__all__ = ["compile_report"]


def _classification(state: MailroomState) -> dict[str, Any] | None:
    """Report block for the sorter result (``None`` before classification)."""
    s = state.sort
    if s is None:
        return None
    return {
        "doc_type": s.doc_type,
        "doc_subclass": s.doc_subclass,
        "confidence": s.confidence,
        "raw_confidence": s.raw_confidence,
        "calibrated": s.calibrated,
        "mode": s.mode.value,
        "confidence_source": s.confidence_source,
        "doc_type_disagree": s.doc_type_disagree,
        "disagree_reason": s.disagree_reason,
    }


def _extraction(state: MailroomState) -> dict[str, Any] | None:
    """Report block for the specialist extraction (``None`` before extraction)."""
    e = state.extract
    if e is None:
        return None
    return {
        "doc_type": e.doc_type,
        "schema_valid": e.schema_valid,
        "parse_error": e.parse_error,
        "confidence": e.confidence,
        "error_kind": e.error_kind,
        "calls": e.calls,
        "data": e.data,
    }


def _price(tax: Any, role: str, usage: Usage) -> float:
    """USD cost of ``usage`` at ``role``'s configured model price (0.0 when unpriced).

    Unknown roles raise ``KeyError``; invalid numeric prices propagate conversion
    errors. The caller decides whether to replace these errors with a fallback.
    """
    model = tax.agent(price_role(role)).model
    prices = (tax.raw.get("cost_models") or {}).get(model)
    if not isinstance(prices, dict):
        return 0.0
    return float(
        usage.prompt_tokens / 1_000_000 * float(prices.get("input_per_million", 0.0))
        + usage.completion_tokens
        / 1_000_000
        * float(prices.get("output_per_million", 0.0))
    )


def _cost_usd(state: MailroomState) -> float:
    """USD per-token cost estimate: each role's usage at that role's configured price.

    Roles that report no price cost 0.0, and the eval ``grader`` is excluded (it is
    not pipeline spend). Usage in ``usage_total`` that no role accounts for (a resumed
    pre-capture manifest) is priced at the sorter's rates, as before.
    Return 0.0 when total tokens are zero or any configuration or pricing error
    occurs; an error discards the entire estimate, not just the affected role.
    """
    if state.usage_total.total_tokens == 0:
        return 0.0
    try:
        tax = load_taxonomy()
        roles = {r: u for r, u in state.usage_by_role.items() if r != "grader"}
        # spend restored from a manifest saved before per-role capture has no role: price it
        # at the sorter's rates, as before
        residual = state.usage_total - sum(roles.values(), Usage())
        return _price(tax, "sorter", residual) + sum(_price(tax, r, u) for r, u in roles.items())
    except Exception:  # noqa: BLE001 - report must never raise
        return 0.0


def compile_report(state: MailroomState) -> dict[str, Any]:
    """Assemble the per-document report. Deterministic; no LLM calls."""
    usage = state.usage_total
    caveats = list(state.arbiter.caveats) if state.arbiter is not None else []
    return {
        "doc_id": state.doc_id,
        "status": state.status,
        "classification": _classification(state),
        "extraction": _extraction(state),
        "verdict": state.verdict.model_dump() if state.verdict is not None else None,
        "arbiter": (
            {"action": state.arbiter.action, "caveats": list(state.arbiter.caveats)}
            if state.arbiter is not None
            else None
        ),
        "caveats": caveats,
        "boss": state.boss.model_dump() if state.boss is not None else None,
        "route_trail": list(state.route_trail),
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "calls": usage.calls,
        },
        "cost": {
            "usd": _cost_usd(state),
            "pricing": "per_token_estimate",
        },
        "llm_calls": None,
    }
