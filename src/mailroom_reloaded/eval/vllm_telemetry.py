"""vLLM Prometheus telemetry: scrape, parse and delta (Task 21, spec section 8).

A cell's engine telemetry is the delta of vLLM ``/metrics`` counters per
replica, taken before and after the cell. The counters scraped are listed in
spec section 8. Values are summed across label sets (vLLM emits one series per
finished reason / model); ``length_finishes`` is the
``vllm:request_success_total{finished_reason="length"}`` slice.

Prefix-cache hit rate and mean TTFT are rates, so they are computed from the
*deltas* of their numerator and denominator (never from the raw gauges):
``delta hits / delta queries`` and ``delta sum / delta count``. KV-cache usage
is a gauge, so the after value is reported.
"""

from __future__ import annotations

import re
from collections import namedtuple
from collections.abc import Mapping
from typing import Any

__all__ = ["ReplicaTelemetry", "parse_prometheus", "scrape", "telemetry_delta"]

#: One replica's delta over a cell. ``prefix_cache_hit_rate`` and
#: ``ttft_mean_seconds`` are computed from counter deltas; ``kv_cache_usage_perc``
#: is the after-cell gauge.
ReplicaTelemetry = namedtuple(
    "ReplicaTelemetry",
    [
        "requests",
        "length_finishes",
        "preemptions",
        "prefix_cache_hit_rate",
        "ttft_mean_seconds",
        "kv_cache_usage_perc",
    ],
)

_LINE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN|\+Inf|-Inf)$"
)
_KEEP = frozenset(
    {
        "vllm:request_success_total",
        "vllm:num_preemptions_total",
        "vllm:gpu_cache_usage_perc",
        "vllm:kv_cache_usage_perc",
        "vllm:prefix_cache_hits_total",
        "vllm:prefix_cache_queries_total",
        "vllm:time_to_first_token_seconds_sum",
        "vllm:time_to_first_token_seconds_count",
        "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total",
    }
)


def parse_prometheus(text: str) -> dict[str, float]:
    """Sum kept counters across label sets; add the ``length_finishes`` slice."""
    out: dict[str, float] = {}
    length = 0.0
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match or match.group(1) not in _KEEP:
            continue
        name, labels = match.group(1), match.group(2) or ""
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        out[name] = out.get(name, 0.0) + value
        if name == "vllm:request_success_total" and 'finished_reason="length"' in labels:
            length += value
    out["length_finishes"] = length
    return out


def scrape(url: str, *, timeout: float = 15.0) -> dict[str, float]:
    """GET a vLLM ``/metrics`` URL and return the parsed counters.

    ``url`` may be the ``/metrics`` endpoint itself or a base URL (``/metrics``
    is appended when absent). The caller owns error handling; this raises on a
    transport or HTTP error so a broken scrape is never mistaken for zeros.
    """
    import httpx

    target = str(url).rstrip("/")
    if not target.endswith("/metrics"):
        target = f"{target}/metrics"
    response = httpx.get(target, timeout=timeout)
    response.raise_for_status()
    return parse_prometheus(response.text)


def _as_counters(value: Any) -> dict[str, float]:
    if isinstance(value, Mapping):
        return {str(k): float(v) for k, v in value.items() if _is_number(v)}
    if isinstance(value, str):
        return parse_prometheus(value)
    return {}


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _delta(after: Mapping[str, float], before: Mapping[str, float], key: str) -> float | None:
    if key not in after:
        return None
    return after[key] - (before.get(key) or 0.0)


def telemetry_delta(before: Any, after: Any) -> ReplicaTelemetry:
    """Counters after minus before, plus the two derived rates.

    ``before`` and ``after`` may be raw Prometheus exposition text or the dicts
    returned by :func:`scrape` / :func:`parse_prometheus`.
    """
    b = _as_counters(before)
    a = _as_counters(after)
    d_hits = _delta(a, b, "vllm:prefix_cache_hits_total")
    d_queries = _delta(a, b, "vllm:prefix_cache_queries_total")
    hit_rate = round(d_hits / d_queries, 6) if d_hits is not None and d_queries else None

    d_ttft_sum = _delta(a, b, "vllm:time_to_first_token_seconds_sum")
    d_ttft_count = _delta(a, b, "vllm:time_to_first_token_seconds_count")
    ttft_mean = (
        round(d_ttft_sum / d_ttft_count, 6)
        if d_ttft_sum is not None and d_ttft_count
        else None
    )

    kv = a.get("vllm:kv_cache_usage_perc", a.get("vllm:gpu_cache_usage_perc"))
    return ReplicaTelemetry(
        requests=_delta(a, b, "vllm:request_success_total"),
        length_finishes=_delta(a, b, "length_finishes"),
        preemptions=_delta(a, b, "vllm:num_preemptions_total"),
        prefix_cache_hit_rate=hit_rate,
        ttft_mean_seconds=ttft_mean,
        kv_cache_usage_perc=kv,
    )
