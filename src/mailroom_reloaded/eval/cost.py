"""Cell cost (GPU-hour and per-token) and prompt-token composition (Task 21).

Two pricing modes, matching spec section 8 ("Cost is computed two ways"):

* ``gpu_hour`` -- busy GPU seconds x ``$`` per GPU-hour x the number of
  concurrently billed GPUs. Local and Modal runs use this.
* ``per_token`` -- ``cost_models`` in ``taxonomy.yaml`` (OpenRouter-style
  per-1M-token prices) applied to prompt and completion tokens.

Both return the same four keys so a card renders one cost table. A missing
input yields ``None`` rather than a fabricated ``$0``.

``token_split`` fits ``prompt_tokens = I x calls + chars / r`` by ordinary
least squares, recovering the fixed instruction/template tokens per call
(``I``) and the characters-per-token ratio (``r``); when the fit is not
identifiable it falls back to :data:`FALLBACK_CHARS_PER_TOKEN`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from mailroom_reloaded.settings import load_taxonomy

__all__ = ["cell_cost", "model_prices", "token_split"]

#: Characters per Qwen3 token for legal / business English (SAND-37 fits
#: measure 4.39-4.58). Used when a run cannot be fitted.
FALLBACK_CHARS_PER_TOKEN = 4.5
_FIT_RATIO_RANGE = (3.0, 6.0)


def model_prices(model: str | None) -> tuple[float, float] | None:
    """``(input_per_million, output_per_million)`` for a model, or ``None``.

    Reads ``cost_models`` from ``taxonomy.yaml``; an exact key wins, then the
    longest matching prefix (so ``openrouter/qwen/...`` resolves).
    """
    if not model:
        return None
    try:
        raw = load_taxonomy().raw
    except Exception:  # noqa: BLE001 - a card must never fail on a config read
        return None
    table = raw.get("cost_models") or {}
    entry = table.get(model)
    if entry is None:
        for key in sorted(table, key=len, reverse=True):
            if model.startswith(key):
                entry = table[key]
                break
    if not isinstance(entry, Mapping):
        return None
    try:
        return (
            float(entry.get("input_per_million") or 0.0),
            float(entry.get("output_per_million") or 0.0),
        )
    except (TypeError, ValueError):
        return None


def _divide(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 8)


def cell_cost(
    wall_s: float | None,
    gpus: int = 1,
    usd_per_hour: float = 0.80,
    ok: int = 0,
    total: int = 0,
    tokens: int | None = None,
    *,
    pricing: Literal["gpu_hour", "per_token"] = "gpu_hour",
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    prices: tuple[float, float] | None = None,
) -> dict[str, float | None]:
    """Cost of one eval cell.

    ``gpu_hour`` (default): ``busy_gpu_usd = wall_s / 3600 x usd_per_hour x
    gpus``. ``per_token``: token cost from ``prices`` (or ``model``'s
    ``cost_models`` entry) over ``prompt_tokens`` / ``completion_tokens``;
    ``busy_gpu_usd`` carries that total.

    The other three keys divide the total by documents, ok documents and
    total tokens so the same card block works for both modes.
    """
    if pricing == "per_token":
        resolved = prices or model_prices(model)
        if resolved is None:
            total_usd = None
        else:
            pin, pout = resolved
            pt = int(prompt_tokens or 0)
            ct = int(completion_tokens or 0)
            total_usd = round(pt * pin / 1_000_000 + ct * pout / 1_000_000, 8)
            tokens = pt + ct if tokens is None else tokens
    else:
        if wall_s is None:
            total_usd = None
        else:
            total_usd = round(
                float(wall_s) / 3600.0 * float(usd_per_hour) * max(1, int(gpus)), 8
            )
    return {
        "busy_gpu_usd": total_usd,
        "usd_per_document": _divide(total_usd, total),
        "usd_per_ok_document": _divide(total_usd, ok),
        "usd_per_million_tokens": (
            round(total_usd / tokens * 1e6, 8)
            if total_usd is not None and tokens
            else None
        ),
    }


def _row_value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def token_split(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Fit ``prompt = I x calls + chars / r`` and return the split.

    Returns ``instruction_per_call``, ``chars_per_token``, ``method``
    (``"fit"`` or ``"fallback"``), plus documentation totals (``calls``,
    ``documents``, ``instruction_tokens``, ``document_tokens``,
    ``completion_tokens`` and a per-document breakdown).
    """
    points: list[tuple[float, float, float, float]] = []
    for row in rows:
        if row.get("ok") is False:
            continue
        prompt = _row_value(row, "prompt_tokens")
        calls = _row_value(row, "calls")
        chars = _row_value(row, "input_chars", "chars")
        completion = _row_value(row, "completion_tokens") or 0
        if not prompt or not calls or not chars:
            continue
        try:
            points.append(
                (float(prompt), float(calls), float(chars), float(completion))
            )
        except (TypeError, ValueError):
            continue
    if not points:
        return {
            "instruction_per_call": None,
            "chars_per_token": None,
            "method": "fallback",
            "documents": 0,
            "calls": 0,
        }

    n = len(points)
    xs = [x for _, _, x, _ in points]
    mean_x = sum(xs) / n
    spread = (sum((x - mean_x) ** 2 for x in xs) / n) ** 0.5 / mean_x if mean_x else 0.0
    inst: float | None = None
    ratio: float | None = None
    method = "fallback"
    if n >= 8 and spread >= 0.1:
        scc = sum(c * c for _, c, _, _ in points)
        scx = sum(c * x for _, c, x, _ in points)
        sxx = sum(x * x for x in xs)
        scp = sum(c * p for p, c, _, _ in points)
        sxp = sum(x * p for p, _, x, _ in points)
        det = scc * sxx - scx * scx
        if det:
            fit_inst = (scp * sxx - scx * sxp) / det
            slope = (scc * sxp - scx * scp) / det
            if (
                slope > 0
                and fit_inst > 0
                and _FIT_RATIO_RANGE[0] <= 1 / slope <= _FIT_RATIO_RANGE[1]
            ):
                inst, ratio, method = fit_inst, 1 / slope, "fit"

    if ratio is None:
        ratio = FALLBACK_CHARS_PER_TOKEN
    prompt_total = sum(p for p, _, _, _ in points)
    completion_total = sum(o for _, _, _, o in points)
    calls_total = sum(c for _, c, _, _ in points)
    document_tokens = min(prompt_total, sum(xs) / ratio)
    if inst is None:
        inst = (prompt_total - document_tokens) / calls_total if calls_total else None
    return {
        "instruction_per_call": inst,
        "chars_per_token": ratio,
        "method": method,
        "documents": n,
        "calls": calls_total,
        "instruction_tokens": prompt_total - document_tokens,
        "document_tokens": document_tokens,
        "completion_tokens": completion_total,
        "per_document": {
            "instruction": (prompt_total - document_tokens) / n,
            "document": document_tokens / n,
            "completion": completion_total / n,
        },
    }
